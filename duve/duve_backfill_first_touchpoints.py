#!/usr/bin/env python3
"""
Backfill Conference — First and Conference — Touchpoints from the recorded
property history of conference_name (Conference — Latest).

Why history rather than the current value: conference_name is single-select,
so a contact who attended two conferences kept only the most recent. HubSpot
retains every past value, so the true first conference and the full set of
conferences are both recoverable — but only from history, and no
going-forward workflow can reconstruct them later.

Per contact:
    Conference — First       = EARLIEST value in history
    Conference — Touchpoints = EVERY distinct value in history

Complications handled:
  * Recent UI option merges (FHT_2026 -> "FHT Paris 2026") appear in history
    as value changes. They rename the same event, so they are collapsed by
    mapping values forward and de-duplicating — NOT by filtering on source
    type, which would also discard genuine 2023 bulk edits that are the only
    record of a real conference for 21 contacts.
  * Historical values that were merged away no longer exist as options, so
    they are mapped forward to their surviving equivalent.
  * A few records hold semicolon-joined values in this single-select field;
    those are split into their parts.
  * Junk test values are dropped.
  * Only values that still exist as options on the destination property are
    written — Touchpoints deliberately has no option for the two non-
    conference activities or the pending HIC row.

Only writes where Conference — First is currently unknown. Never overwrites.
Dry run by default; --apply to write.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.hubapi.com"
SRC = "conference_name"
DST_FIRST = "conference_first_new"
DST_TP = "conference_touchpoints"

# Values merged away in the UI -> their surviving equivalent.
MERGED_FORWARD = {
    "equiphotel_2024": "EquipHotel Paris 2024",
    "itb_2025": "ITB Berlin 2025",
    "ITB_2026": "ITB Berlin 2026",
    "FHT_2026": "FHT Paris 2026",
    "Accor Phuket Confernece 2026": "Accor Phuket Conference 2026",
    "FHT 2023": "fht_paris_2023",
    # hyphen typo variant found only in history, never an option
    "ITB-2026": "ITB Berlin 2026",
}

# Confirmed dead test values.
JUNK = {"pre - short stay", "pre - short stay summit", "pre short stay",
        "short stay"}


class Halt(Exception):
    pass


def api(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def options_of(name, token):
    s, b = api("GET", "/crm/v3/properties/contacts/%s" % name, token)
    if s != 200:
        raise Halt("GET %s -> %s" % (name, s))
    return {o["value"] for o in b["options"] if not o.get("hidden")}, \
           {o["value"] for o in b["options"]}


def normalise(raw):
    """One history value -> list of clean, forward-mapped values."""
    out = []
    for part in str(raw).split(";"):
        v = part.strip()
        if not v or v in JUNK:
            continue
        out.append(MERGED_FORWARD.get(v, v))
    return out


def collect_ids(token):
    ids, after = [], 0
    while True:
        s, b = api("POST", "/crm/v3/objects/contacts/search", token,
                   {"filterGroups": [{"filters": [
                       {"propertyName": SRC, "operator": "HAS_PROPERTY"}]}],
                    "properties": ["email"], "limit": 100, "after": after})
        if s != 200:
            raise Halt("search -> %s: %s" % (s, json.dumps(b)[:300]))
        ids += [(r["id"], (r["properties"] or {}).get("email")) for r in b["results"]]
        nxt = ((b.get("paging") or {}).get("next") or {}).get("after")
        if not nxt:
            return ids
        after = int(nxt)
        time.sleep(0.05)


def read_history(batch, token):
    s, b = api("POST", "/crm/v3/objects/contacts/batch/read", token,
               {"propertiesWithHistory": [SRC],
                "properties": [DST_FIRST, DST_TP, "email"],
                "inputs": [{"id": i} for i in batch]})
    if s not in (200, 207):
        raise Halt("batch read -> %s: %s" % (s, json.dumps(b)[:300]))
    return b.get("results", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap contacts processed (for a quick look)")
    a = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    first_active, _ = options_of(DST_FIRST, token)
    tp_active, _ = options_of(DST_TP, token)
    print("valid options — First: %d, Touchpoints: %d\n"
          % (len(first_active), len(tp_active)))

    print("collecting contacts with %s ..." % SRC)
    ids = collect_ids(token)
    if a.limit:
        ids = ids[:a.limit]
    print("  %d contacts\n" % len(ids))

    idmap = dict(ids)
    plan, stats = [], {
        "no_history": 0, "already_set": 0, "single": 0, "multi": 0,
        "tp_unmapped": set(), "first_unmapped": set(), "tp_written": 0,
    }

    order = [i for i, _ in ids]
    # property-history batch reads are capped at 50 inputs by HubSpot
    for k in range(0, len(order), 50):
        for r in read_history(order[k:k + 50], token):
            cid = r["id"]
            props = r.get("properties") or {}
            hist = (r.get("propertiesWithHistory") or {}).get(SRC) or []
            # Do NOT filter by sourceType. Bulk actions are not only the recent
            # option merges — there are genuine 2023 bulk edits, and 21 contacts
            # hold a real conference recorded ONLY in one of those. The merge
            # renames collapse on their own once values are mapped forward,
            # so normalisation handles them without discarding history.
            real = sorted(hist, key=lambda h: h["timestamp"])
            seq = []
            for h in real:
                for v in normalise(h.get("value")):
                    if v not in seq:
                        seq.append(v)
            if not seq:
                stats["no_history"] += 1
                continue
            if props.get(DST_FIRST):
                stats["already_set"] += 1
                continue

            first = next((v for v in seq if v in first_active), None)
            if first is None:
                stats["first_unmapped"].update(seq)
                continue
            tps = [v for v in seq if v in tp_active]
            stats["tp_unmapped"].update(v for v in seq if v not in tp_active)
            stats["multi" if len(seq) > 1 else "single"] += 1
            if len(tps) > 1:
                stats["tp_written"] += 1
            plan.append((cid, idmap.get(cid) or props.get("email"), first, tps, seq))
        time.sleep(0.05)

    print("=" * 72)
    print("BACKFILL PLAN")
    print("=" * 72)
    print("  contacts to update            : %d" % len(plan))
    print("    from a single conference    : %d" % stats["single"])
    print("    with multiple conferences   : %d  <- history recovers these"
          % stats["multi"])
    print("  contacts receiving >1 touchpoint: %d" % stats["tp_written"])
    print("  skipped, First already set    : %d" % stats["already_set"])
    print("  skipped, no usable history    : %d" % stats["no_history"])
    if stats["first_unmapped"]:
        print("  values with no First option   : %s"
              % sorted(stats["first_unmapped"]))
    if stats["tp_unmapped"]:
        print("  values with no Touchpoint option (dropped): %s"
              % sorted(stats["tp_unmapped"]))

    multis = [p for p in plan if len(p[4]) > 1]
    print("\n  sample — multi-conference contacts (the ones that matter):")
    for cid, em, first, tps, seq in multis[:12]:
        print("    %-38s first=%-28s touchpoints=%s"
              % ((em or cid)[:37], first, tps))
    print("\n  sample — single-conference contacts:")
    for cid, em, first, tps, seq in [p for p in plan if len(p[4]) == 1][:5]:
        print("    %-38s first=%-28s touchpoints=%s"
              % ((em or cid)[:37], first, tps))

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    print("\napplying ...")
    done = 0
    for k in range(0, len(plan), 100):
        chunk = plan[k:k + 100]
        inputs = [{"id": cid, "properties": {
            DST_FIRST: first,
            **({DST_TP: ";".join(tps)} if tps else {})}}
            for cid, _, first, tps, _ in chunk]
        s, b = api("POST", "/crm/v3/objects/contacts/batch/update", token,
                   {"inputs": inputs})
        if s not in (200, 207):
            raise Halt("batch update -> %s: %s" % (s, json.dumps(b)[:400]))
        done += len(chunk)
        print("  updated %d/%d" % (done, len(plan)))
        time.sleep(0.1)
    print("\nDone — %d contacts updated." % done)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
