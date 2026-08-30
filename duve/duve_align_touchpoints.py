#!/usr/bin/env python3
"""
Align conference_touchpoints option VALUES to conference_name.

WF-1 writes one event_staging string into both conference_name (copy) and
conference_touchpoints (append), so the same event must carry the same value
in both properties.

conference_name holds 2,546 contacts and is Salesforce-synced, so its values
are fixed. conference_touchpoints holds ZERO contacts, so it is the free side
to change. This rewrites the 14 mismatched touchpoint values to match
conference_name. Labels are unchanged.

Refuses to run if any contact holds a conference_touchpoints value.
Dry run by default; --apply to write.
"""
import argparse, json, os, re, sys, urllib.error, urllib.request

BASE, OBJ = "https://api.hubapi.com", "contacts"


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


def norm(s):
    s = s.lower().replace("confernece", "conference")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    for a, b in [("fht paris", "fht"), ("itb berlin", "itb"),
                 ("equiphotel paris", "equiphotel"), ("vr world summit", "vrws"),
                 ("arabian travel market", "atm"),
                 ("short term rental forum", "shorttermrental")]:
        s = s.replace(a, b)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    cn = [o for o in get_prop("conference_name", token)["options"]
          if not o.get("hidden")]
    ctp = get_prop("conference_touchpoints", token)
    ct = ctp["options"]
    print("conference_name visible options : %d" % len(cn))
    print("conference_touchpoints options  : %d" % len(ct))

    s, b = api("POST", "/crm/v3/objects/contacts/search", token,
               {"filterGroups": [{"filters": [
                   {"propertyName": "conference_touchpoints",
                    "operator": "HAS_PROPERTY"}]}], "limit": 1})
    if s != 200:
        raise Halt("contact search -> %s" % s)
    if b.get("total"):
        raise Halt("%d contacts hold conference_touchpoints values; rewriting "
                   "option values would orphan them. Stopping." % b["total"])
    print("contacts holding touchpoints    : 0 (safe to rewrite)\n")

    idx = {}
    for o in cn:
        idx.setdefault(norm(o["label"]), o)

    merged, changes, unmatched = [], [], []
    for i, t in enumerate(ct):
        m = idx.get(norm(t["label"])) or idx.get(norm(t["value"]))
        d = dict(t)
        d["displayOrder"] = i
        if m is None:
            unmatched.append(t)
        elif m["value"] != t["value"]:
            changes.append((t["value"], m["value"], t["label"]))
            d["value"] = m["value"]
        merged.append(d)

    if unmatched:
        raise Halt("No conference_name match for: %s"
                   % [u["value"] for u in unmatched])

    print("value changes (%d):" % len(changes))
    for old, new, lab in changes:
        print("   %-26s -> %-30s %s" % (old, new, lab))
    print("\nunchanged: %d" % (len(ct) - len(changes)))

    if not changes:
        print("\nAlready aligned — nothing to do.")
        return 0
    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    s, b = api("PATCH", "/crm/v3/properties/%s/conference_touchpoints" % OBJ,
               token, {"options": merged})
    if s != 200:
        raise Halt("PATCH -> %s: %s" % (s, json.dumps(b)[:400]))

    after = get_prop("conference_touchpoints", token)["options"]
    avals = {o["value"] for o in after}
    cnvals = {o["value"] for o in cn}
    missing = [n for _, n, _ in changes if n not in avals]
    if missing:
        raise Halt("Expected values absent after PATCH: %s" % missing)
    if len(after) != len(ct):
        raise Halt("Option count changed: %d -> %d" % (len(ct), len(after)))
    orphan = [v for v in avals if v not in cnvals]
    print("\nPATCHED — %d options, %d values rewritten." % (len(after), len(changes)))
    print("touchpoint values with no matching conference_name option: %d"
          % len(orphan))
    if orphan:
        print("   %s" % orphan)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
