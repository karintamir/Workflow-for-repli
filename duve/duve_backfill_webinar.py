#!/usr/bin/env python3
"""
Backfill Webinar (First) and Webinar — Touchpoints from the recorded property
history of webinar_name (Webinar (Latest)).

Same reasoning as the conference backfill: webinar_name is single-select, so a
contact who attended several webinars kept only the most recent. HubSpot
retains every past value, so the first webinar and the full set are both
recoverable from history — and only from history. Going-forward the per-webinar
registration workflows maintain these, but they cannot reconstruct the past.

Per contact:
    Webinar (First)       = the EARLIEST-DATED webinar in history
    Webinar — Touchpoints = EVERY distinct webinar in history

Note the difference from the conference backfill, which used record order.
webinar_name is declared single-select but an automation uses it as an
ACCUMULATOR: each history entry rewrites the whole semicolon-joined list, and
the order inside that list is not chronological. tmcvacationrentals@gmail.com
is typical — the value is "Maximizing Revenue - Jan 2026" at 17:38:16 and
"IT Webinar - 11/2025;Maximizing Revenue - Jan 2026" three seconds later,
because the automation back-filled the older registration. Record order would
name Jan 2026 as their first webinar when in fact it was Nov 2025; this
affects 345 of the 903 multi-webinar contacts (38%).

Every webinar carries its own date in its name, so the real order is known
outright rather than inferred. WEBINAR_DATE below holds it, and First is the
earliest by that date. Any option missing from that map halts the run rather
than falling back to a guess.

Complications handled:
  * One option is a CONCATENATION of two real webinars:
        "Operational Excellence: Streamlining Guest Experience - 07/2025,
         Luxury Guest Experience - 09/2025"
    It is already hidden on both select properties and has no Touchpoints
    option. It is split into its two real webinars.
  * The Operational Excellence option's stored VALUE is "- 06/2025" while its
    LABEL reads "- 07/2025". History can carry either string, so the label form
    is mapped forward onto the stored value.
  * A few records hold semicolon-joined values in this single-select field;
    those are split into their parts.
  * Only values that still exist as an active option on the destination are
    written; anything unrecognised is reported, never guessed at.

Only writes where Webinar (First) is currently unknown. Never overwrites.
Touchpoints is a checkbox and is merged with whatever is already there.
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
SRC = "webinar_name"
DST_FIRST = "webinar_first"
DST_TP = "webinar_touchpoints"

OPEX = "Operational Excellence: Streamlining Guest Experience - 06/2025"
OPEX_LABEL = "Operational Excellence: Streamlining Guest Experience - 07/2025"
LUX = "Luxury Guest Experience - 09/2025"
COMPOUND = OPEX_LABEL + ", " + LUX

# Each webinar's actual date, taken from its own name. This is what orders
# them — see the note above on why record order does not.
WEBINAR_DATE = {
    "Hospitality Revenue Leaders - 05/2025": (2025, 5),
    OPEX: (2025, 6),
    LUX: (2025, 9),
    "IT Webinar - 11/2025": (2025, 11),
    "Maximizing Revenue - Jan 2026": (2026, 1),
    "Data-Driven Decisions - March 2026": (2026, 3),
    "Scaling The Guest Experience - 05/2026": (2026, 5),
    "Optimiser les revenus - 07/2026": (2026, 7),
    "2027 Forecast: The Future of the Guest Experience - 09/2026": (2026, 9),
}

# One historical string -> the list of real webinars it actually represents.
# Order matters: it is the order the two webinars happened in.
FORWARD = {
    COMPOUND: [OPEX, LUX],
    OPEX_LABEL: [OPEX],
}


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
    return {o["value"] for o in b["options"] if not o.get("hidden")}


def normalise(raw):
    """One history value -> list of clean, forward-mapped webinar values.

    Split on ';' only. The compound value uses ', ' as its separator, but a
    blanket comma split would be unsafe if a real webinar name ever contains
    one, so it is handled by explicit mapping instead.
    """
    out = []
    for part in str(raw).split(";"):
        v = part.strip()
        if not v:
            continue
        for mapped in FORWARD.get(v, [v]):
            out.append(mapped)
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
        ids += [(r["id"], (r["properties"] or {}).get("email"))
                for r in b["results"]]
        nxt = ((b.get("paging") or {}).get("next") or {}).get("after")
        if not nxt:
            return ids
        after = int(nxt)
        time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    first_active = options_of(DST_FIRST, token)
    tp_active = options_of(DST_TP, token)
    print("active options — First: %d, Touchpoints: %d\n"
          % (len(first_active), len(tp_active)))
    for v in (OPEX, LUX):
        if v not in tp_active:
            raise Halt("split target %r has no Touchpoints option" % v)
    undated = sorted((first_active | tp_active) - set(WEBINAR_DATE))
    if undated:
        raise Halt("no date known for these active options, so they cannot be "
                   "ordered: %s" % undated)

    print("collecting contacts with %s ..." % SRC)
    ids = collect_ids(token)
    if a.limit:
        ids = ids[:a.limit]
    print("  %d contacts\n" % len(ids))
    idmap = dict(ids)

    plan = []
    stats = {"no_history": 0, "already_set": 0, "single": 0, "multi": 0,
             "tp_multi": 0, "compound_seen": 0, "reordered": 0}
    unmapped = {}
    seq_len = {}

    order = [i for i, _ in ids]
    # property-history batch reads are capped at 50 inputs by HubSpot
    for k in range(0, len(order), 50):
        s, b = api("POST", "/crm/v3/objects/contacts/batch/read", token,
                   {"propertiesWithHistory": [SRC],
                    "properties": [DST_FIRST, DST_TP, "email"],
                    "inputs": [{"id": i} for i in order[k:k + 50]]})
        if s not in (200, 207):
            raise Halt("batch read -> %s: %s" % (s, json.dumps(b)[:300]))
        for r in b.get("results", []):
            cid = r["id"]
            props = r.get("properties") or {}
            hist = (r.get("propertiesWithHistory") or {}).get(SRC) or []
            real = sorted(hist, key=lambda h: h["timestamp"])
            seq = []
            for h in real:
                if str(h.get("value") or "").strip() == COMPOUND:
                    stats["compound_seen"] += 1
                for v in normalise(h.get("value")):
                    if v not in seq:
                        seq.append(v)
            if not seq:
                stats["no_history"] += 1
                continue
            # Order by the webinar's own date, NOT by when it was recorded.
            recorded_first = seq[0]
            seq.sort(key=lambda v: WEBINAR_DATE.get(v, (9999, 99)))
            if len(seq) > 1 and seq[0] != recorded_first:
                stats["reordered"] += 1
            seq_len[len(seq)] = seq_len.get(len(seq), 0) + 1
            if props.get(DST_FIRST):
                stats["already_set"] += 1
                continue

            first = next((v for v in seq if v in first_active), None)
            if first is None:
                for v in seq:
                    unmapped[v] = unmapped.get(v, 0) + 1
                continue
            for v in seq:
                if v not in tp_active:
                    unmapped[v] = unmapped.get(v, 0) + 1
            cur_tp = [v for v in (props.get(DST_TP) or "").split(";")
                      if v.strip()]
            tps = list(cur_tp) + [v for v in seq
                                  if v in tp_active and v not in cur_tp]
            stats["multi" if len(seq) > 1 else "single"] += 1
            if len(tps) > 1:
                stats["tp_multi"] += 1
            plan.append((cid, idmap.get(cid) or props.get("email"),
                         first, tps, seq))
        time.sleep(0.05)

    print("=" * 74)
    print("WEBINAR BACKFILL PLAN")
    print("=" * 74)
    print("  contacts to update              : %d" % len(plan))
    print("    from a single webinar         : %d" % stats["single"])
    print("    with multiple webinars        : %d  <- history recovers these"
          % stats["multi"])
    print("  contacts receiving >1 touchpoint: %d" % stats["tp_multi"])
    print("  skipped, First already set      : %d" % stats["already_set"])
    print("  skipped, no usable history      : %d" % stats["no_history"])
    print("  compound value seen in history  : %d entries (split into 2)"
          % stats["compound_seen"])
    print("  First differs from record order : %d  <- accumulator artefact"
          % stats["reordered"])
    print("\n  webinars per contact:")
    for n in sorted(seq_len):
        print("    %d webinar(s): %d contacts" % (n, seq_len[n]))
    if unmapped:
        print("\n  UNRECOGNISED history values (not written anywhere):")
        for v, n in sorted(unmapped.items(), key=lambda x: -x[1]):
            print("    %6d  %r" % (n, v))

    multis = [p for p in plan if len(p[4]) > 1]
    print("\n  sample — multi-webinar contacts (the ones that matter):")
    for cid, em, first, tps, seq in multis[:12]:
        print("    %-38s first=%s" % ((em or cid)[:37], first))
        for t in tps:
            print("        + %s" % t)
    print("\n  sample — single-webinar contacts:")
    for cid, em, first, tps, seq in [p for p in plan if len(p[4]) == 1][:5]:
        print("    %-38s first=%s" % ((em or cid)[:37], first))

    if not plan:
        print("\nNothing to do.")
        return 0
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

    # Verify by direct read — the search index lags a batch write.
    print("\nverifying by direct read ...")
    want = {cid: (first, set(tps)) for cid, _, first, tps, _ in plan}
    bad = []
    order = list(want)
    for k in range(0, len(order), 100):
        s, b = api("POST", "/crm/v3/objects/contacts/batch/read", token,
                   {"properties": ["email", DST_FIRST, DST_TP],
                    "inputs": [{"id": i} for i in order[k:k + 100]]})
        if s not in (200, 207):
            raise Halt("verify read -> %s" % s)
        for r in b.get("results", []):
            pr = r.get("properties") or {}
            wf, wt = want[r["id"]]
            got = set((pr.get(DST_TP) or "").split(";"))
            if pr.get(DST_FIRST) != wf or not wt <= got:
                bad.append((r["id"], pr.get("email"), pr.get(DST_FIRST)))
        time.sleep(0.05)
    print("  updated  : %d" % done)
    print("  mismatch : %d %s" % (len(bad), bad[:5]))
    if bad:
        raise Halt("some contacts did not receive the full value")
    print("\nDone — %d contacts backfilled." % done)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
