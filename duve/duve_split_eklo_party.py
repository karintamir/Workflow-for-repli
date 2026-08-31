#!/usr/bin/env python3
"""
Split the compound value "FHT 2025 Eklo Party" into its two real facts.

Client confirmed: FHT = Food Hotel Tech Paris 2025, and the Eklo party was an
activity run with another company DURING that conference. So the single
conference_name value packs a conference and an activity together — exactly
what the Conference / Engagement Type split exists to separate.

Per affected contact:
    Conference — Latest       "FHT 2025 Eklo Party" -> fht_2025
    Conference — First        "FHT 2025 Eklo Party" -> fht_2025
    Conference — Touchpoints  add fht_2025 (merged with anything already there)
    Engagement Type           set to "party", only where currently empty

fht_2025 ("FHT Paris 2025") already exists on all three properties, so this is
a remap onto an existing option, not a new one.

Touchpoints is a checkbox: existing values are read and merged, never replaced.
Engagement Type is never overwritten — a contact who already has one keeps it,
since it records their most recent interaction.

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
COMPOUND = "FHT 2025 Eklo Party"
CONFERENCE = "fht_2025"
ENGAGEMENT = "party"

LATEST, FIRST, TP, ENG = ("conference_name", "conference_first_new",
                          "conference_touchpoints", "engagement_type")


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


def options(name, token):
    s, b = api("GET", "/crm/v3/properties/contacts/%s" % name, token)
    if s != 200:
        raise Halt("GET %s -> %s" % (name, s))
    return {o["value"] for o in b["options"] if not o.get("hidden")}


def find(prop, token):
    out, after = {}, 0
    while True:
        s, b = api("POST", "/crm/v3/objects/contacts/search", token,
                   {"filterGroups": [{"filters": [
                       {"propertyName": prop, "operator": "EQ",
                        "value": COMPOUND}]}],
                    "properties": ["email", LATEST, FIRST, TP, ENG],
                    "limit": 100, "after": after})
        if s != 200:
            raise Halt("search %s -> %s: %s" % (prop, s, json.dumps(b)[:300]))
        for r in b["results"]:
            out[r["id"]] = r["properties"] or {}
        nxt = ((b.get("paging") or {}).get("next") or {}).get("after")
        if not nxt:
            return out
        after = int(nxt)
        time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    for prop in (LATEST, FIRST, TP):
        if CONFERENCE not in options(prop, token):
            raise Halt("%r is not an active option on %s" % (CONFERENCE, prop))
    if ENGAGEMENT not in options(ENG, token):
        raise Halt("%r is not an option on %s" % (ENGAGEMENT, ENG))
    print("target options verified on all four properties\n")

    by_latest = find(LATEST, token)
    by_first = find(FIRST, token)
    everyone = dict(by_first)
    everyone.update(by_latest)
    print("contacts with Latest = %r : %d" % (COMPOUND, len(by_latest)))
    print("contacts with First  = %r : %d" % (COMPOUND, len(by_first)))
    print("distinct contacts affected  : %d\n" % len(everyone))

    plan, eng_skipped = [], 0
    for cid, p in everyone.items():
        props = {}
        if p.get(LATEST) == COMPOUND:
            props[LATEST] = CONFERENCE
        if p.get(FIRST) == COMPOUND:
            props[FIRST] = CONFERENCE

        # Touchpoints is multi-select: merge, never replace.
        cur_tp = [v for v in (p.get(TP) or "").split(";") if v.strip()]
        if CONFERENCE not in cur_tp:
            props[TP] = ";".join(cur_tp + [CONFERENCE])

        # Engagement Type records the most recent interaction — don't clobber.
        if not p.get(ENG):
            props[ENG] = ENGAGEMENT
        else:
            eng_skipped += 1

        if props:
            plan.append((cid, p.get("email") or cid, props))

    print("contacts to update            : %d" % len(plan))
    print("engagement type left as-is    : %d (already set)" % eng_skipped)
    print("\nsample:")
    for cid, em, props in plan[:6]:
        print("  %-36s %s" % (em[:35], json.dumps(props)))

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
        s, b = api("POST", "/crm/v3/objects/contacts/batch/update", token,
                   {"inputs": [{"id": cid, "properties": props}
                               for cid, _, props in chunk]})
        if s not in (200, 207):
            raise Halt("batch update -> %s: %s" % (s, json.dumps(b)[:400]))
        done += len(chunk)
        print("  updated %d/%d" % (done, len(plan)))
        time.sleep(0.1)

    # Verify by DIRECT READ, not search. The search index is eventually
    # consistent and lags a batch write by a minute or more, which makes a
    # search-based check report every record as unchanged.
    print("\nverifying by direct read ...")
    stuck = []
    order = [cid for cid, _, _ in plan]
    for k in range(0, len(order), 100):
        s, b = api("POST", "/crm/v3/objects/contacts/batch/read", token,
                   {"properties": ["email", LATEST, FIRST, TP, ENG],
                    "inputs": [{"id": i} for i in order[k:k + 100]]})
        if s not in (200, 207):
            raise Halt("verify read -> %s" % s)
        for r in b.get("results", []):
            pr = r.get("properties") or {}
            if COMPOUND in (pr.get(LATEST), pr.get(FIRST), pr.get(TP) or ""):
                stuck.append((r["id"], pr.get("email"), pr.get(LATEST)))
        time.sleep(0.05)

    print("  contacts updated : %d" % done)
    print("  still compound   : %d" % len(stuck))
    for cid, em, latest in stuck:
        print("     %-36s Latest=%s" % (em, latest))
    if stuck:
        print("\n  NOTE: conference_name is mapped to the Salesforce sync, which")
        print("  can write a stale value back within seconds of a HubSpot-side")
        print("  change. A record listed above needs fixing in Salesforce (or")
        print("  the field mapping changed) — re-running this will not hold.")
        print("  conference_first_new and conference_touchpoints are NOT in the")
        print("  sync, so those values are safe.")
    else:
        print("\n  The %r option can now be hidden on all three conference "
              "properties." % COMPOUND)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
