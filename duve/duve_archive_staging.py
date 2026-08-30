#!/usr/bin/env python3
"""
Archive event_staging and webinar_staging.

Both were created from Task 4b of the original handoff spec, which assumed
conference_name / webinar_name were write-once SOURCE fields that a form
submission could destroy. The model has since changed: those fields are now
"Latest" and are meant to be overwritten, with sourcing protected by separate
"First" properties and history by the Touchpoints properties. That removes the
reason staging existed, and nothing ever used them.

Guards before archiving each property:
  * 0 contacts hold a value
  * 0 automation flows reference it by name

Archiving is a soft delete — HubSpot retains it and it can be restored.

Dry run by default; --apply to archive.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE, OBJ = "https://api.hubapi.com", "contacts"
TARGETS = ["event_staging", "webinar_staging"]


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


def all_flow_ids(token):
    ids, after = [], None
    while True:
        path = "/automation/v4/flows?limit=100" + ("&after=%s" % after if after else "")
        s, b = api("GET", path, token)
        if s != 200:
            raise Halt("list flows -> %s" % s)
        ids += [f["id"] for f in b.get("results", [])]
        after = ((b.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    print("Fetching flows for a reference check...")
    ids = all_flow_ids(token)
    blobs = []
    for i in ids:
        s, f = api("GET", "/automation/v4/flows/%s" % i, token)
        if s == 200:
            blobs.append(json.dumps(f))
    print("scanned %d flows\n" % len(blobs))

    plan = []
    for name in TARGETS:
        s, p = api("GET", "/crm/v3/properties/%s/%s" % (OBJ, name), token)
        if s == 404:
            print("%-18s already archived / not found — skipping" % name)
            continue
        if s != 200:
            raise Halt("GET %s -> %s" % (name, s))

        s, b = api("POST", "/crm/v3/objects/contacts/search", token,
                   {"filterGroups": [{"filters": [
                       {"propertyName": name, "operator": "HAS_PROPERTY"}]}],
                    "limit": 1})
        if s != 200:
            raise Halt("search %s -> %s" % (name, s))
        contacts = b.get("total", 0)
        refs = sum(1 for x in blobs if name in x)

        print("%-18s label=%-26r contacts=%-4d flow refs=%d"
              % (name, p.get("label"), contacts, refs))
        if contacts:
            raise Halt("%s holds %d contact values — not archiving."
                       % (name, contacts))
        if refs:
            raise Halt("%s is referenced by %d flow(s) — not archiving."
                       % (name, refs))
        plan.append(name)

    if not plan:
        print("\nNothing to archive.")
        return 0
    if not a.apply:
        print("\nDRY RUN — would archive: %s" % ", ".join(plan))
        return 0

    for name in plan:
        s, b = api("DELETE", "/crm/v3/properties/%s/%s" % (OBJ, name), token)
        if s not in (200, 204):
            raise Halt("DELETE %s -> %s: %s" % (name, s, json.dumps(b)[:300]))
        s2, _ = api("GET", "/crm/v3/properties/%s/%s" % (OBJ, name), token)
        print("archived %-18s (GET now returns %s)" % (name, s2))

    print("\nDone. Archiving is reversible in HubSpot if either is needed again.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
