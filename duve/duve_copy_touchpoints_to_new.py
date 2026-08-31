#!/usr/bin/env python3
"""
Copy Conference — Touchpoints (Archive) into the new Conferences Touchpoints.

Two touchpoints properties now exist:
    conference_touchpoints   "Conference — Touchpoints (Archive)"  — backfilled
    conferences_touchpoints  "Conferences Touchpoints"             — what the
                                                                     flow appends to

The flow writes to the new one, so the recovered multi-conference history in
the archived one has to move across or it is stranded.

Every value in the archive exists as an option on the new property (checked at
runtime), so this is a straight copy. Existing values on the destination are
merged, never replaced, and a contact that already holds everything is skipped
— so the script is safely re-runnable.

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
SRC = "conference_touchpoints"
DST = "conferences_touchpoints"


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    src_opts, dst_opts = options(SRC, token), options(DST, token)
    missing = sorted(src_opts - dst_opts)
    print("%s options: %d" % (SRC, len(src_opts)))
    print("%s options: %d" % (DST, len(dst_opts)))
    if missing:
        raise Halt("These values exist on %s but not %s, so copying would drop "
                   "them: %s" % (SRC, DST, missing))
    print("every source value exists on the destination — safe to copy\n")

    ids, after = [], 0
    while True:
        s, b = api("POST", "/crm/v3/objects/contacts/search", token,
                   {"filterGroups": [{"filters": [
                       {"propertyName": SRC, "operator": "HAS_PROPERTY"}]}],
                    "properties": ["email", SRC, DST], "limit": 100,
                    "after": after})
        if s != 200:
            raise Halt("search -> %s: %s" % (s, json.dumps(b)[:300]))
        ids += [(r["id"], r["properties"] or {}) for r in b["results"]]
        nxt = ((b.get("paging") or {}).get("next") or {}).get("after")
        if not nxt:
            break
        after = int(nxt)
        time.sleep(0.04)
    print("contacts with a value in %s: %d\n" % (SRC, len(ids)))

    plan, already, dropped = [], 0, set()
    for cid, p in ids:
        src = [v for v in (p.get(SRC) or "").split(";") if v.strip()]
        dst = [v for v in (p.get(DST) or "").split(";") if v.strip()]
        keep = [v for v in src if v in dst_opts]
        dropped.update(v for v in src if v not in dst_opts)
        merged = dst + [v for v in keep if v not in dst]
        if len(merged) == len(dst):
            already += 1
            continue
        plan.append((cid, p.get("email") or cid, ";".join(merged), len(merged)))

    print("contacts to copy       : %d" % len(plan))
    print("already complete       : %d" % already)
    if dropped:
        print("values with no option on the destination: %s" % sorted(dropped))
    multi = [x for x in plan if x[3] > 1]
    print("of those, carrying more than one conference: %d" % len(multi))
    print("\nsample (multi-conference first):")
    for cid, em, val, n in (multi[:6] or plan[:6]):
        print("   %-36s -> %s" % (em[:35], val))

    if not plan:
        print("\nNothing to copy.")
        return 0
    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    print("\napplying ...")
    done = 0
    for k in range(0, len(plan), 100):
        chunk = plan[k:k + 100]
        s, b = api("POST", "/crm/v3/objects/contacts/batch/update", token,
                   {"inputs": [{"id": cid, "properties": {DST: val}}
                               for cid, _, val, _ in chunk]})
        if s not in (200, 207):
            raise Halt("batch update -> %s: %s" % (s, json.dumps(b)[:400]))
        done += len(chunk)
        print("  updated %d/%d" % (done, len(plan)))
        time.sleep(0.1)

    print("\nverifying by direct read ...")
    bad = []
    order = [cid for cid, _, _, _ in plan]
    want = {cid: val for cid, _, val, _ in plan}
    for k in range(0, len(order), 100):
        s, b = api("POST", "/crm/v3/objects/contacts/batch/read", token,
                   {"properties": ["email", DST],
                    "inputs": [{"id": i} for i in order[k:k + 100]]})
        for r in b.get("results", []):
            got = set(((r["properties"] or {}).get(DST) or "").split(";"))
            exp = set(want[r["id"]].split(";"))
            if not exp <= got:
                bad.append((r["id"], sorted(exp - got)))
        time.sleep(0.05)
    print("  copied  : %d" % done)
    print("  mismatch: %d %s" % (len(bad), bad[:5]))
    if bad:
        raise Halt("some contacts did not receive the full value")
    print("\nDone — %d contacts copied to %s." % (done, DST))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
