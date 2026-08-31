#!/usr/bin/env python3
"""
Duve — Event Tracking Property Build (portal 25420191, EU data residency).

Creates and relabels CONTACT properties via the HubSpot CRM Properties API.

Safety model (from the handoff spec):
  * Internal names are never changed. Task 1 sends label + description only.
  * Nothing is ever deleted — no property, no option.
  * PATCH on `options` replaces the whole array, so Task 5 GETs first, merges,
    and PATCHes the full list back.
  * Dry run is the default. Writes require --apply.
  * Every create GETs first and skips if the property already exists, so the
    script is safely re-runnable.
  * The token is read from HUBSPOT_PRIVATE_APP_TOKEN, never hardcoded.
  * Any 4xx that is not a clean 404-on-GET halts the run. No blind retries,
    no endpoint fallbacks.

Usage:
    export HUBSPOT_PRIVATE_APP_TOKEN=...
    python3 duve_event_properties.py                  # dry run (default)
    python3 duve_event_properties.py --apply          # writes; halts before Task 5
    python3 duve_event_properties.py --apply --confirm-task5
    python3 duve_event_properties.py --apply --tasks 1,2
"""

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = "https://api.hubapi.com"
OBJECT_TYPE = "contacts"
EXPECTED_PORTAL = 25420191

# Preference order for the property group used by the three new properties.
# The first group present in the portal wins. Reported in the summary.
GROUP_PREFERENCE = [
    "contact_activity",
    "conversion_information",
    "contactinformation",
]


class Halt(Exception):
    """Raised on any condition the spec says to stop and report on."""


# --------------------------------------------------------------------------
# Task definitions
# --------------------------------------------------------------------------

TASK1_RELABELS = [
    (
        "conference_name",
        "Conference — Source",
        "The conference that sourced this contact. Written once at creation, "
        "never overwritten.",
    ),
    (
        "webinar_name",
        "Webinar — Source",
        "The webinar that sourced this contact. Written once at creation, "
        "never overwritten.",
    ),
]

ENGAGEMENT_TYPE_OPTIONS = [
    ("Booked meeting", "booked_meeting"),
    ("Booth conversation", "booth_conversation"),
    ("Booth scan", "booth_scan"),
    ("Dinner", "dinner"),
    ("Happy hour", "happy_hour"),
    ("Party", "party"),
    ("Other conference activity", "other_activity"),
    ("Registration", "registration"),
]

# Task 4: current webinar_name values, minus the concatenated data-entry
# artefact. Value and label are identical, matching the existing property's
# convention. The 06/2025 entry uses 06/2025 for BOTH value and label — on the
# live property that value carries a label reading 07/2025. Flagged in the
# report; the real date is unconfirmed.
WEBINAR_TOUCHPOINT_VALUES = [
    "Hospitality Revenue Leaders - 05/2025",
    "Operational Excellence: Streamlining Guest Experience - 06/2025",
    "Luxury Guest Experience - 09/2025",
    "IT Webinar - 11/2025",
    "Maximizing Revenue - Jan 2026",
    "Data-Driven Decisions - March 2026",
    "Scaling The Guest Experience - 05/2026",
    "Optimiser les revenus - 07/2026",
]

EXCLUDED_WEBINAR_VALUE = (
    "Operational Excellence: Streamlining Guest Experience - 07/2025, "
    "Luxury Guest Experience - 09/2025"
)

# Task 4b: staging properties the workflows write into. Plain text, NOT
# enumeration — they receive raw values before validation.
STAGING_PROPERTIES = [
    (
        "event_staging",
        "Event staging (system)",
        "System field. Holds the conference value for the interaction currently "
        "being processed. Written by forms and imports, cleared by workflow. "
        "Do not report on this.",
    ),
    (
        "webinar_staging",
        "Webinar staging (system)",
        "System field. Holds the webinar value for the interaction currently "
        "being processed. Written by the Zoom integration or forms, cleared by "
        "workflow. Do not report on this.",
    ),
]

TASK5_NEW_OPTIONS = [
    ("Partner", "Partner"),
    ("Employee Referral", "Employee Referral"),
    ("Customer Referral", "Customer Referral"),
]
TASK5_EXPECTED_BEFORE = 18
TASK5_EXPECTED_AFTER = 21
FIRST_TOUCH_EXPECTED = 21


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def api(method, path, token, body=None):
    """Return (status, parsed_body). Never raises on HTTP status."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}
    except urllib.error.URLError as exc:
        raise Halt("Network error calling %s %s: %s" % (method, path, exc))


def get_property(name, token):
    """Return the property dict, or None on a clean 404. Halt on anything else."""
    status, body = api("GET", "/crm/v3/properties/%s/%s" % (OBJECT_TYPE, name), token)
    if status == 200:
        return body
    if status == 404:
        return None
    raise Halt(
        "GET %s returned %s (only a clean 404 is tolerated). Body: %s"
        % (name, status, json.dumps(body)[:600])
    )


def write_property(method, path, token, body):
    status, resp = api(method, path, token, body)
    if status not in (200, 201):
        raise Halt(
            "%s %s returned %s. Body: %s"
            % (method, path, status, json.dumps(resp)[:600])
        )
    return resp


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def options_of(prop):
    return prop.get("options") or []


def build_options(pairs):
    """pairs: iterable of (label, value) -> option dicts with displayOrder."""
    return [
        {"label": label, "value": value, "displayOrder": i}
        for i, (label, value) in enumerate(pairs)
    ]


def show(title, body):
    print("    would send: %s" % title)
    for line in json.dumps(body, indent=2, ensure_ascii=False).splitlines():
        print("      " + line)


def banner(text):
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


class Report:
    def __init__(self):
        self.lines = []
        self.notes = []
        self.group = None

    def add(self, task, outcome, detail=""):
        self.lines.append((task, outcome, detail))
        print("  [%s] %s %s" % (outcome, task, detail))

    def note(self, text):
        self.notes.append(text)


# --------------------------------------------------------------------------
# Group selection
# --------------------------------------------------------------------------


def pick_group(token, override, report):
    if override:
        report.group = override
        print("  Property group (from --group): %s" % override)
        return override

    status, body = api("GET", "/crm/v3/properties/%s/groups" % OBJECT_TYPE, token)
    if status != 200:
        raise Halt("GET property groups returned %s: %s" % (status, json.dumps(body)[:400]))

    groups = body.get("results", [])
    names = [g.get("name") for g in groups]
    print("  Existing contact property groups (%d):" % len(groups))
    for g in groups:
        print("    - %-38s %s" % (g.get("name"), g.get("label")))

    for candidate in GROUP_PREFERENCE:
        if candidate in names:
            report.group = candidate
            print("  Selected existing group: %s" % candidate)
            return candidate

    raise Halt(
        "None of the preferred groups %s exist in this portal. Re-run with "
        "--group <name> choosing from the list above. No group will be created."
        % GROUP_PREFERENCE
    )


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


def task1(token, apply_, report):
    banner("TASK 1 — Relabel conference_name and webinar_name (labels only)")
    for name, label, description in TASK1_RELABELS:
        prop = get_property(name, token)
        if prop is None:
            report.add("task1:%s" % name, "FAIL", "property not found — nothing patched")
            continue

        before_count = len(options_of(prop))
        before_name = prop.get("name")
        print("  %s: label=%r, options before=%d" % (name, prop.get("label"), before_count))

        # label + description ONLY. No options/type/fieldType/groupName.
        body = {"label": label, "description": description}

        if not apply_:
            show("PATCH /crm/v3/properties/%s/%s" % (OBJECT_TYPE, name), body)
            report.add(
                "task1:%s" % name,
                "DRY-RUN",
                "would relabel %r -> %r; options untouched (%d)"
                % (prop.get("label"), label, before_count),
            )
            continue

        write_property(
            "PATCH", "/crm/v3/properties/%s/%s" % (OBJECT_TYPE, name), token, body
        )

        after = get_property(name, token)
        after_count = len(options_of(after))
        if after.get("name") != before_name:
            raise Halt("Internal name of %s changed! before=%s after=%s"
                       % (name, before_name, after.get("name")))
        if after_count != before_count:
            raise Halt(
                "Option count for %s changed during relabel: %d -> %d"
                % (name, before_count, after_count)
            )
        report.add(
            "task1:%s" % name,
            "PATCHED",
            "name unchanged (%s), label now %r, options %d -> %d"
            % (after.get("name"), after.get("label"), before_count, after_count),
        )


def create_enum(name, label, description, group, field_type, options,
                token, apply_, report, task):
    existing = get_property(name, token)
    if existing is not None:
        report.add(
            task,
            "SKIPPED",
            "%s already exists (fieldType=%s, %d options) — not overwritten"
            % (name, existing.get("fieldType"), len(options_of(existing))),
        )
        return

    body = {
        "name": name,
        "label": label,
        "description": description,
        "groupName": group,
        "type": "enumeration",
        "fieldType": field_type,
        "options": options,
    }

    if not apply_:
        show("POST /crm/v3/properties/%s" % OBJECT_TYPE, body)
        report.add(task, "DRY-RUN", "would create %s with %d options (fieldType=%s)"
                   % (name, len(options), field_type))
        return

    resp = write_property("POST", "/crm/v3/properties/%s" % OBJECT_TYPE, token, body)

    returned_field_type = resp.get("fieldType")
    if returned_field_type != field_type:
        raise Halt(
            "%s was created but fieldType came back as %r, expected %r. "
            "Stopping rather than patching — inspect the property in the UI."
            % (name, returned_field_type, field_type)
        )
    report.add(
        task,
        "CREATED",
        "%s (fieldType=%s, %d options, group=%s)"
        % (name, returned_field_type, len(options_of(resp)), resp.get("groupName")),
    )


def task2(token, apply_, report, group):
    banner("TASK 2 — Create engagement_type")
    create_enum(
        "engagement_type",
        "Engagement Type",
        "The kind of interaction at an event. Captured at the point of contact, "
        "not inferred.",
        group,
        "select",
        build_options(ENGAGEMENT_TYPE_OPTIONS),
        token, apply_, report, "task2:engagement_type",
    )


def load_csv(path, report):
    if not os.path.exists(path):
        report.add(
            "task3:conference_touchpoints",
            "BLOCKED",
            "%s not found — Task 3 not run (per spec)" % path,
        )
        return None
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            value = (row.get("value") or "").strip()
            label = (row.get("label") or "").strip()
            if value and label:
                rows.append((label, value))
    if not rows:
        report.add(
            "task3:conference_touchpoints", "BLOCKED", "%s has no usable rows" % path
        )
        return None
    return rows


def task3(token, apply_, report, group, csv_path):
    banner("TASK 3 — Create conference_touchpoints (from CSV)")
    rows = load_csv(csv_path, report)
    if rows is None:
        return
    print("  Loaded %d options from %s" % (len(rows), csv_path))
    create_enum(
        "conference_touchpoints",
        "Conference — Touchpoints",
        "Every conference this contact has engaged with. Appends, never replaces.",
        group,
        "checkbox",
        build_options(rows),
        token, apply_, report, "task3:conference_touchpoints",
    )


def task4(token, apply_, report, group):
    banner("TASK 4 — Create webinar_touchpoints")
    pairs = [(v, v) for v in WEBINAR_TOUCHPOINT_VALUES]
    print("  %d options; excluding the concatenated artefact:" % len(pairs))
    print("    %r" % EXCLUDED_WEBINAR_VALUE)
    create_enum(
        "webinar_touchpoints",
        "Webinar — Touchpoints",
        "Every webinar this contact has engaged with. Appends, never replaces.",
        group,
        "checkbox",
        build_options(pairs),
        token, apply_, report, "task4:webinar_touchpoints",
    )


def task4b(token, apply_, report, group):
    banner("TASK 4b — Create event_staging and webinar_staging (plain text)")
    for name, label, description in STAGING_PROPERTIES:
        task = "task4b:%s" % name
        existing = get_property(name, token)
        if existing is not None:
            report.add(
                task,
                "SKIPPED",
                "%s already exists (type=%s, fieldType=%s) — not overwritten"
                % (name, existing.get("type"), existing.get("fieldType")),
            )
            continue

        body = {
            "name": name,
            "label": label,
            "description": description,
            "groupName": group,
            "type": "string",
            "fieldType": "text",
        }

        if not apply_:
            show("POST /crm/v3/properties/%s" % OBJECT_TYPE, body)
            report.add(task, "DRY-RUN", "would create %s as string/text" % name)
            continue

        resp = write_property("POST", "/crm/v3/properties/%s" % OBJECT_TYPE,
                              token, body)
        if resp.get("type") != "string" or resp.get("fieldType") != "text":
            raise Halt(
                "%s was created but came back as type=%r fieldType=%r, expected "
                "string/text. Stopping rather than patching."
                % (name, resp.get("type"), resp.get("fieldType"))
            )
        report.add(task, "CREATED", "%s (type=%s, fieldType=%s, group=%s)"
                   % (name, resp.get("type"), resp.get("fieldType"),
                      resp.get("groupName")))


def task5(token, apply_, report, confirmed):
    banner("TASK 5 — Append three options to lead_capture_route")

    last = get_property("lead_capture_route", token)
    if last is None:
        report.add("task5:lead_capture_route", "FAIL", "property not found")
        return
    existing = options_of(last)
    before = len(existing)
    print("  lead_capture_route options before: %d" % before)

    first = get_property("lead_capture_route__first_touch", token)
    first_count = len(options_of(first)) if first else 0
    print("  lead_capture_route__first_touch options: %d" % first_count)

    if before != TASK5_EXPECTED_BEFORE:
        raise Halt(
            "lead_capture_route has %d options, expected %d. The portal has "
            "changed since the spec was written — re-check the merge before "
            "running Task 5." % (before, TASK5_EXPECTED_BEFORE)
        )
    if first_count != FIRST_TOUCH_EXPECTED:
        report.note(
            "lead_capture_route__first_touch had %d options, spec expected %d."
            % (first_count, FIRST_TOUCH_EXPECTED)
        )

    existing_values = [o.get("value") for o in existing]
    to_append = [(l, v) for l, v in TASK5_NEW_OPTIONS if v not in existing_values]
    if not to_append:
        report.add("task5:lead_capture_route", "SKIPPED",
                   "all three options already present (%d total)" % before)
        return

    # Rebuild the full array: all originals first, unchanged and in order.
    merged = []
    for i, opt in enumerate(existing):
        item = dict(opt)
        item["displayOrder"] = i
        merged.append(item)
    for j, (label, value) in enumerate(to_append):
        merged.append({"label": label, "value": value,
                       "displayOrder": len(existing) + j})

    body = {"options": merged}
    print("  Merged array — %d existing + %d new = %d total:" % (
        before, len(to_append), len(merged)))
    for opt in merged:
        marker = "NEW " if opt["value"] in [v for _, v in to_append] else "    "
        print("    %s%2d  %-28s %s" % (marker, opt["displayOrder"],
                                       opt["value"], opt.get("label")))

    if not apply_:
        show("PATCH /crm/v3/properties/%s/lead_capture_route" % OBJECT_TYPE, body)
        report.add("task5:lead_capture_route", "DRY-RUN",
                   "would merge to %d options" % len(merged))
        return

    if not confirmed:
        report.add(
            "task5:lead_capture_route",
            "HALTED",
            "awaiting human confirmation — re-run with --confirm-task5 to apply",
        )
        return

    write_property(
        "PATCH", "/crm/v3/properties/%s/lead_capture_route" % OBJECT_TYPE, token, body
    )

    after_prop = get_property("lead_capture_route", token)
    after_values = [o.get("value") for o in options_of(after_prop)]
    after = len(after_values)
    missing = [v for v in existing_values if v not in after_values]
    if missing:
        raise Halt("Original options went missing after PATCH: %s" % missing)
    if after != TASK5_EXPECTED_AFTER:
        raise Halt("Expected %d options after merge, got %d"
                   % (TASK5_EXPECTED_AFTER, after))
    report.add("task5:lead_capture_route", "PATCHED",
               "%d -> %d options, all %d originals still present"
               % (before, after, len(existing_values)))


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def verify(token, csv_path, report):
    banner("VERIFICATION (live GETs)")
    csv_rows = None
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            csv_rows = sum(1 for r in csv.DictReader(fh) if (r.get("value") or "").strip())

    checks = []

    for name, expected_label in (
        ("conference_name", "Conference — Source"),
        ("webinar_name", "Webinar — Source"),
    ):
        p = get_property(name, token)
        if p is None:
            checks.append((name, False, "not found"))
        else:
            ok = p.get("name") == name and p.get("label") == expected_label
            checks.append((name, ok, "name=%s label=%r options=%d"
                           % (p.get("name"), p.get("label"), len(options_of(p)))))

    p = get_property("engagement_type", token)
    checks.append(("engagement_type", bool(p) and p.get("fieldType") == "select"
                   and len(options_of(p)) == 8,
                   "missing" if not p else "fieldType=%s options=%d"
                   % (p.get("fieldType"), len(options_of(p)))))

    p = get_property("conference_touchpoints", token)
    expect = csv_rows if csv_rows is not None else -1
    checks.append(("conference_touchpoints",
                   bool(p) and p.get("fieldType") == "checkbox"
                   and len(options_of(p)) == expect,
                   "missing" if not p else "fieldType=%s options=%d (csv=%s)"
                   % (p.get("fieldType"), len(options_of(p)), csv_rows)))

    p = get_property("webinar_touchpoints", token)
    checks.append(("webinar_touchpoints",
                   bool(p) and p.get("fieldType") == "checkbox"
                   and len(options_of(p)) == 8,
                   "missing" if not p else "fieldType=%s options=%d"
                   % (p.get("fieldType"), len(options_of(p)))))

    for name, _label, _desc in STAGING_PROPERTIES:
        p = get_property(name, token)
        checks.append((name, bool(p) and p.get("type") == "string",
                       "missing" if not p else "type=%s fieldType=%s"
                       % (p.get("type"), p.get("fieldType"))))

    p = get_property("lead_capture_route", token)
    checks.append(("lead_capture_route",
                   bool(p) and len(options_of(p)) == TASK5_EXPECTED_AFTER,
                   "missing" if not p else "options=%d" % len(options_of(p))))

    for name, ok, detail in checks:
        print("  %-24s %-5s %s" % (name, "PASS" if ok else "FAIL", detail))
    return checks


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def print_payloads(csv_path, group):
    """Build and print every create/patch body without touching the network.

    Lets the payloads be reviewed before a token exists. Task 5's body cannot
    be shown here because it must be merged from the live options array.
    """
    banner("OFFLINE PAYLOAD PREVIEW — no network calls, nothing written")
    print("  groupName placeholder: %s\n" % group)

    for name, label, description in TASK1_RELABELS:
        show("PATCH /crm/v3/properties/%s/%s" % (OBJECT_TYPE, name),
             {"label": label, "description": description})

    show("POST /crm/v3/properties/%s  (engagement_type)" % OBJECT_TYPE, {
        "name": "engagement_type", "label": "Engagement Type",
        "description": "The kind of interaction at an event. Captured at the "
                       "point of contact, not inferred.",
        "groupName": group, "type": "enumeration", "fieldType": "select",
        "options": build_options(ENGAGEMENT_TYPE_OPTIONS)})

    rows = []
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                v, l = (row.get("value") or "").strip(), (row.get("label") or "").strip()
                if v and l:
                    rows.append((l, v))
        show("POST /crm/v3/properties/%s  (conference_touchpoints)" % OBJECT_TYPE, {
            "name": "conference_touchpoints", "label": "Conference — Touchpoints",
            "description": "Every conference this contact has engaged with. "
                           "Appends, never replaces.",
            "groupName": group, "type": "enumeration", "fieldType": "checkbox",
            "options": build_options(rows)})
    else:
        print("    conference_touchpoints: BLOCKED — %s not found" % csv_path)

    show("POST /crm/v3/properties/%s  (webinar_touchpoints)" % OBJECT_TYPE, {
        "name": "webinar_touchpoints", "label": "Webinar — Touchpoints",
        "description": "Every webinar this contact has engaged with. Appends, "
                       "never replaces.",
        "groupName": group, "type": "enumeration", "fieldType": "checkbox",
        "options": build_options([(v, v) for v in WEBINAR_TOUCHPOINT_VALUES])})

    for name, label, description in STAGING_PROPERTIES:
        show("POST /crm/v3/properties/%s  (%s)" % (OBJECT_TYPE, name), {
            "name": name, "label": label, "description": description,
            "groupName": group, "type": "string", "fieldType": "text"})

    print("\n  Task 5 body is omitted — it must be merged from the live "
          "options array and cannot be built offline.")
    print("\n  Counts: engagement_type=%d, conference_touchpoints=%d, "
          "webinar_touchpoints=%d, staging=%d"
          % (len(ENGAGEMENT_TYPE_OPTIONS), len(rows),
             len(WEBINAR_TOUCHPOINT_VALUES), len(STAGING_PROPERTIES)))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually write. Without this the script only prints "
                             "the request bodies it would send.")
    parser.add_argument("--confirm-task5", action="store_true",
                        help="human confirmation for the lead_capture_route merge. "
                             "Required in addition to --apply for Task 5.")
    parser.add_argument("--group", help="property group for the new properties. "
                                        "Omit to auto-select an existing group.")
    parser.add_argument("--csv", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "conference_options.csv"),
        help="path to conference_options.csv")
    parser.add_argument("--tasks", default="1,2,3,4,4b,5",
                        help="comma-separated task ids to run (1,2,3,4,4b,5)")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--print-payloads", action="store_true",
                        help="build and print every request body offline, with "
                             "no token and no network calls. Review aid only.")
    args = parser.parse_args()

    if args.print_payloads:
        return print_payloads(args.csv, args.group or "<group chosen at runtime>")

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN is not set in the environment.",
              file=sys.stderr)
        print("Export a private app token with crm.schemas.contacts.read and "
              "crm.schemas.contacts.write, then re-run.", file=sys.stderr)
        return 2

    tasks = {t.strip().lower() for t in args.tasks.split(",") if t.strip()}
    report = Report()

    mode = "APPLY (writes enabled)" if args.apply else "DRY RUN (no writes)"
    banner("Duve event property build — %s" % mode)

    # Confirm connectivity and portal identity before anything else.
    status, who = api("GET", "/account-info/v3/details", token)
    if status == 200:
        pid = who.get("portalId")
        print("  Connected to portal %s (%s)" % (pid, who.get("uiDomain")))
        if pid and int(pid) != EXPECTED_PORTAL:
            raise Halt("Token belongs to portal %s, expected %s. Stopping."
                       % (pid, EXPECTED_PORTAL))
    elif status == 401:
        raise Halt("401 from api.hubapi.com. The token is wrong, or this EU "
                   "portal needs a different API host — check before retrying.")
    else:
        print("  (account-info returned %s; continuing to the properties check)"
              % status)

    probe = get_property("conference_name", token)
    if probe is None:
        raise Halt("conference_name not found — wrong portal or wrong scopes.")
    print("  First GET succeeded (conference_name, %d options)."
          % len(options_of(probe)))

    group = None
    if {"2", "3", "4", "4b"} & tasks:
        banner("PROPERTY GROUP SELECTION")
        group = pick_group(token, args.group, report)

    if "1" in tasks:
        task1(token, args.apply, report)
    if "2" in tasks:
        task2(token, args.apply, report, group)
    if "3" in tasks:
        task3(token, args.apply, report, group, args.csv)
    if "4" in tasks:
        task4(token, args.apply, report, group)
    if "4b" in tasks:
        task4b(token, args.apply, report, group)
    if "5" in tasks:
        task5(token, args.apply, report, args.confirm_task5)

    if not args.skip_verify:
        verify(token, args.csv, report)

    banner("SUMMARY")
    print("  Property group used: %s" % (report.group or "n/a"))
    for task, outcome, detail in report.lines:
        print("  %-32s %-9s %s" % (task, outcome, detail))
    print("\n  Known discrepancy (carried forward from the spec):")
    print("    webinar_name option value 'Operational Excellence: Streamlining")
    print("    Guest Experience - 06/2025' carries a label reading 07/2025.")
    print("    webinar_touchpoints uses 06/2025 for both value and label.")
    print("    The real date has not been confirmed.")
    for note in report.notes:
        print("  Note: %s" % note)
    if not args.apply:
        print("\n  Dry run only — nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Halt as exc:
        print("\nHALTED: %s" % exc, file=sys.stderr)
        sys.exit(1)
