#!/usr/bin/env python3
"""
Duve — follow-up property updates after Marina's cleanup answers (2026-08-30).

Three changes, all on portal 25420191 contacts:

  A. conference_name        += 14 cleaned option values (2024-2026 events).
                              Needed so a single event_staging value matches
                              BOTH conference_name and conference_touchpoints.
  B. webinar_touchpoints     : correct the one option 06/2025 -> 07/2025.
                              Marina confirmed with Lena the webinar ran in
                              July. Safe: 0 contacts hold a value.
  C. conference_touchpoints += 4 events Marina categorised as Conference.
                              Values reuse the EXISTING conference_name values
                              so the two vocabularies stay aligned.

Same safety model as the main build script: GET, merge, PATCH the full array,
verify afterwards. Nothing is deleted from conference_name. Dry run default.
"""

import argparse, json, os, sys, urllib.error, urllib.request

BASE, OBJ = "https://api.hubapi.com", "contacts"

# A. clean values missing from conference_name (value, label)
CONFERENCE_NAME_ADDITIONS = [
    ("fht_paris_2024", "FHT Paris 2024"),
    ("fht_paris_2025", "FHT Paris 2025"),
    ("fht_paris_2026", "FHT Paris 2026"),
    ("itb_berlin_2024", "ITB Berlin 2024"),
    ("itb_berlin_2025", "ITB Berlin 2025"),
    ("itb_berlin_2026", "ITB Berlin 2026"),
    ("equiphotel_paris_2024", "EquipHotel Paris 2024"),
    ("equiphotel_paris_2026", "EquipHotel Paris 2026"),
    ("fitur_2026", "Fitur 2026"),
    ("ihtf_uk_2026", "IHTF UK 2026"),
    ("smdl_accor_paris_2025", "SMDL Accor Paris 2025"),
    ("fiba_florida_2025", "Fiba Florida 2025"),
    ("accor_phuket_2026", "Accor Phuket Conference 2026"),
    ("unfold_ams_2026", "Unfold AMS 2026"),
]

# B. webinar date correction
WEBINAR_OLD = "Operational Excellence: Streamlining Guest Experience - 06/2025"
WEBINAR_NEW = "Operational Excellence: Streamlining Guest Experience - 07/2025"

# C. events Marina categorised as Conference; values match conference_name
CONFERENCE_TOUCHPOINT_ADDITIONS = [
    ("hospitality_pioneers_speed_dating_2022", "Speed Dating (Hospitality Pioneers) 2022"),
    ("duve_thais_fr_march23", "Dans de Lobby FR - Sept 2023"),
    ("influence_house_france_2023", "Influence House France 2023"),
    ("mews_partners_june_2024", "Mews Partner Events June 2024"),
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


def get_prop(name, token):
    s, b = api("GET", "/crm/v3/properties/%s/%s" % (OBJ, name), token)
    if s == 200:
        return b
    raise Halt("GET %s returned %s: %s" % (name, s, json.dumps(b)[:400]))


def patch_options(name, merged, token):
    s, b = api("PATCH", "/crm/v3/properties/%s/%s" % (OBJ, name), token,
               {"options": merged})
    if s != 200:
        raise Halt("PATCH %s returned %s: %s" % (name, s, json.dumps(b)[:400]))
    return b


def renumber(opts):
    out = []
    for i, o in enumerate(opts):
        d = dict(o)
        d["displayOrder"] = i
        out.append(d)
    return out


def add_options(name, additions, token, apply_):
    prop = get_prop(name, token)
    existing = prop.get("options") or []
    have = {o.get("value") for o in existing}
    todo = [(v, l) for v, l in additions if v not in have]
    print("\n%s: %d existing options" % (name, len(existing)))
    if not todo:
        print("  SKIPPED — all %d values already present" % len(additions))
        return
    merged = renumber(existing) + [
        {"label": l, "value": v, "displayOrder": len(existing) + i}
        for i, (v, l) in enumerate(todo)
    ]
    for v, l in todo:
        print("  + %-40s %s" % (v, l))
    if not apply_:
        print("  DRY RUN — would PATCH to %d options" % len(merged))
        return
    patch_options(name, merged, token)
    after = get_prop(name, token)
    vals = {o.get("value") for o in (after.get("options") or [])}
    missing = [o.get("value") for o in existing if o.get("value") not in vals]
    if missing:
        raise Halt("Original options lost from %s: %s" % (name, missing))
    still = [v for v, _ in todo if v not in vals]
    if still:
        raise Halt("Additions missing from %s after PATCH: %s" % (name, still))
    print("  PATCHED — %d -> %d options, all originals intact"
          % (len(existing), len(vals)))


def fix_webinar(token, apply_):
    name = "webinar_touchpoints"
    prop = get_prop(name, token)
    existing = prop.get("options") or []
    print("\n%s: %d existing options" % (name, len(existing)))
    if any(o.get("value") == WEBINAR_NEW for o in existing):
        print("  SKIPPED — already corrected to 07/2025")
        return
    if not any(o.get("value") == WEBINAR_OLD for o in existing):
        raise Halt("Neither the 06/2025 nor the 07/2025 option is present.")

    # Safety: this rewrites an option VALUE, so confirm nothing holds data.
    s, b = api("POST", "/crm/v3/objects/contacts/search", token,
               {"filterGroups": [{"filters": [
                   {"propertyName": name, "operator": "HAS_PROPERTY"}]}],
                "limit": 1})
    if s != 200:
        raise Halt("Contact search returned %s: %s" % (s, json.dumps(b)[:300]))
    if b.get("total"):
        raise Halt("%d contacts hold %s values — rewriting the option value "
                   "would orphan them. Stopping." % (b["total"], name))
    print("  contacts holding a value: 0 (safe to rewrite)")

    merged = []
    for o in existing:
        d = dict(o)
        if d.get("value") == WEBINAR_OLD:
            d["value"] = WEBINAR_NEW
            d["label"] = WEBINAR_NEW
        merged.append(d)
    merged = renumber(merged)
    print("  06/2025 -> 07/2025")
    if not apply_:
        print("  DRY RUN — would PATCH %d options" % len(merged))
        return
    patch_options(name, merged, token)
    after = {o.get("value") for o in (get_prop(name, token).get("options") or [])}
    if WEBINAR_NEW not in after or WEBINAR_OLD in after:
        raise Halt("Correction did not apply cleanly.")
    print("  PATCHED — corrected, still %d options" % len(after))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--steps", default="A,B,C",
                   help="which updates to run: A=conference_name, "
                        "B=webinar date fix, C=conference_touchpoints")
    a = p.parse_args()
    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2
    print("=" * 66)
    print("Duve follow-up updates — %s"
          % ("APPLY" if a.apply else "DRY RUN (no writes)"))
    print("=" * 66)
    steps = {x.strip().upper() for x in a.steps.split(",") if x.strip()}
    if "A" in steps:
        add_options("conference_name", CONFERENCE_NAME_ADDITIONS, token, a.apply)
    if "B" in steps:
        fix_webinar(token, a.apply)
    if "C" in steps:
        add_options("conference_touchpoints", CONFERENCE_TOUCHPOINT_ADDITIONS,
                    token, a.apply)
    print("\n" + "=" * 66)
    for n in ("conference_name", "conference_touchpoints", "webinar_touchpoints"):
        pr = get_prop(n, token)
        print("  %-24s %d options" % (n, len(pr.get("options") or [])))
    if not a.apply:
        print("\n  Dry run only — nothing written.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as e:
        print("\nHALTED: %s" % e, file=sys.stderr)
        sys.exit(1)
