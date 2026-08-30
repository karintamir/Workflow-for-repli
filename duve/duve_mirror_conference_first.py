#!/usr/bin/env python3
"""
Rebuild conference__first ("Conference — First (Archive)") option set to
mirror conference_name ("Conference — Latest").

Why: a HubSpot copy-property action requires every value on the SOURCE to
exist as an ACTIVE option on the DESTINATION. A hidden option on the
destination reads as missing, which is why the flow rejected the copy with
"the following internal values are missing".

conference__first holds ZERO contacts, so its options can be rebuilt outright
rather than patched. Mirroring conference_name also drops the stale values
left behind by the UI merges (equiphotel_2024, itb_2025, ITB_2026, FHT_2026,
Accor Phuket Confernece 2026), which would otherwise keep drifting apart.

All mirrored options are written ACTIVE (hidden=False) — that is the whole
point of the fix.

Refuses to run if any contact holds a conference__first value.
Dry run by default; --apply to write.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE, OBJ = "https://api.hubapi.com", "contacts"
SOURCE, DEST = "conference_name", "conference__first"


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


def get_prop(name, token):
    s, b = api("GET", "/crm/v3/properties/%s/%s" % (OBJ, name), token)
    if s != 200:
        raise Halt("GET %s -> %s: %s" % (name, s, json.dumps(b)[:300]))
    return b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    s, b = api("POST", "/crm/v3/objects/contacts/search", token,
               {"filterGroups": [{"filters": [
                   {"propertyName": DEST, "operator": "HAS_PROPERTY"}]}],
                "limit": 1})
    if s != 200:
        raise Halt("contact search -> %s: %s" % (s, json.dumps(b)[:300]))
    if b.get("total"):
        raise Halt("%d contacts hold %s values — rebuilding its options would "
                   "orphan them. Stopping." % (b["total"], DEST))
    print("contacts holding %s: 0 (safe to rebuild)\n" % DEST)

    src = get_prop(SOURCE, token)
    dst = get_prop(DEST, token)
    before = {o["value"] for o in dst["options"]}
    src_vals = {o["value"] for o in src["options"]}

    print("%-20s %r  %d options (%d hidden)"
          % (SOURCE, src["label"], len(src["options"]),
             sum(1 for o in src["options"] if o.get("hidden"))))
    print("%-20s %r  %d options (%d hidden)"
          % (DEST, dst["label"], len(dst["options"]),
             sum(1 for o in dst["options"] if o.get("hidden"))))

    merged = [{"label": o["label"], "value": o["value"],
               "displayOrder": i, "hidden": False}
              for i, o in enumerate(src["options"])]

    add = sorted(src_vals - before)
    drop = sorted(before - src_vals)
    unhide = sorted(o["value"] for o in dst["options"]
                    if o.get("hidden") and o["value"] in src_vals)

    print("\nwould ADD (%d):" % len(add))
    for v in add:
        print("   + %s" % v)
    print("would UNHIDE (%d):" % len(unhide))
    for v in unhide:
        print("   ~ %s" % v)
    print("would DROP as stale (%d):" % len(drop))
    for v in drop:
        print("   - %s" % v)

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    s, b = api("PATCH", "/crm/v3/properties/%s/%s" % (OBJ, DEST), token,
               {"options": merged})
    if s != 200:
        raise Halt("PATCH %s -> %s: %s" % (DEST, s, json.dumps(b)[:400]))

    after = get_prop(DEST, token)
    avals = {o["value"] for o in after["options"]}
    hidden = [o["value"] for o in after["options"] if o.get("hidden")]
    if not src_vals <= avals:
        raise Halt("Still missing after PATCH: %s" % sorted(src_vals - avals))
    if hidden:
        raise Halt("Options still hidden after PATCH: %s" % hidden)

    print("\nPATCHED — %s now has %d options, 0 hidden."
          % (DEST, len(after["options"])))
    print("every %s value present and active in %s: True" % (SOURCE, DEST))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
