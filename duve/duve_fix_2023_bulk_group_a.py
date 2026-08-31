#!/usr/bin/env python3
"""
Correct Conference — Latest for the three contacts caught by the March 2023
bulk edit (Group A).

Their history reads: EquipHotel Paris 2022 (import, Nov 2022) -> FHT Paris
2023 (import, 16 Mar 2023) -> EquipHotel Paris 2022 again (bulk action, 20
Mar 2023, by a since-deactivated user). The bulk action reverted a more
recent import four days later, so their current Latest is stale. Client
confirmed FHT Paris 2023 is correct for this pattern.

Group B (four contacts whose bulk edit landed the SAME day as their form
submission, GuestyVal vs Short Stay Summit) is deliberately NOT touched —
a same-day change reads as a deliberate reclassification, not an accident.

Each contact is verified against the expected history shape before writing,
so a record that has moved on since the review is skipped rather than
overwritten. Conference — First and Touchpoints are untouched; the backfill
derives First from the earliest history entry, which is unaffected by this.

Dry run by default; --apply to write.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "https://api.hubapi.com"
SRC = "conference_name"

EXPECT_CURRENT = "equiphotel_paris_2022"
CORRECT_TO = "fht_paris_2023"

GROUP_A = [
    "philippe@hiphophostels.com",
    "alj@homequalityclub.com",
    "ngerschel@gmail.com",
]


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    opts = {o["value"] for o in
            api("GET", "/crm/v3/properties/contacts/%s" % SRC, token)[1]["options"]}
    if CORRECT_TO not in opts:
        raise Halt("%r is not an option on %s" % (CORRECT_TO, SRC))

    plan = []
    for email in GROUP_A:
        s, b = api("POST", "/crm/v3/objects/contacts/search", token,
                   {"filterGroups": [{"filters": [
                       {"propertyName": "email", "operator": "EQ",
                        "value": email}]}],
                    "properties": ["email"], "limit": 1})
        if s != 200 or not b.get("results"):
            print("  %-38s NOT FOUND — skipping" % email)
            continue
        cid = b["results"][0]["id"]

        s, b = api("POST", "/crm/v3/objects/contacts/batch/read", token,
                   {"propertiesWithHistory": [SRC], "properties": [SRC],
                    "inputs": [{"id": cid}]})
        rec = b["results"][0]
        cur = (rec.get("properties") or {}).get(SRC)
        hist = sorted((rec.get("propertiesWithHistory") or {}).get(SRC) or [],
                      key=lambda x: x["timestamp"])

        # Guard: only touch a record still showing the reviewed shape.
        latest = hist[-1] if hist else None
        ok = (cur == EXPECT_CURRENT
              and latest is not None
              and latest.get("sourceType") == "CRM_UI_BULK_ACTION"
              and latest["timestamp"][:4] == "2023"
              and any(x["value"] == CORRECT_TO for x in hist))
        print("  %-38s current=%-24s %s"
              % (email, cur, "OK" if ok else "SKIP — no longer matches review"))
        for x in hist:
            print("        %s  %-26s %s"
                  % (x["timestamp"][:10], x["value"], x.get("sourceType")))
        if ok:
            plan.append((cid, email))

    if not plan:
        print("\nNothing to change.")
        return 0
    if not a.apply:
        print("\nDRY RUN — would set %s = %r on %d contact(s)."
              % (SRC, CORRECT_TO, len(plan)))
        return 0

    s, b = api("POST", "/crm/v3/objects/contacts/batch/update", token,
               {"inputs": [{"id": cid, "properties": {SRC: CORRECT_TO}}
                           for cid, _ in plan]})
    if s not in (200, 207):
        raise Halt("batch update -> %s: %s" % (s, json.dumps(b)[:400]))

    print("\nverifying ...")
    s, b = api("POST", "/crm/v3/objects/contacts/batch/read", token,
               {"properties": [SRC], "inputs": [{"id": c} for c, _ in plan]})
    bad = []
    for rec in b.get("results", []):
        v = (rec.get("properties") or {}).get(SRC)
        if v != CORRECT_TO:
            bad.append((rec["id"], v))
    for cid, email in plan:
        print("  %-38s -> %s" % (email, CORRECT_TO))
    if bad:
        raise Halt("did not take on: %s" % bad)
    print("\n%d contact(s) corrected and verified." % len(plan))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
