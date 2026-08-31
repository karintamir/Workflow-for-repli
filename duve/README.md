# Duve — Event Tracking Property Build

Portal **25420191** (Duve, EU data residency — `app-eu1.hubspot.com`).

Implements `duve-event-properties-CLAUDE-CODE-SPEC.md`. Workflows are a
separate spec and are deliberately **not** built here — the properties must
exist and pass verification first, because every flow references them by
internal name.

## Files

| File | Purpose |
|---|---|
| `duve_event_properties.py` | The build script. Dry run by default. |
| `conference_options.csv` | Cleaned conference list for Task 3 (38 rows). |

## Running it

```bash
export HUBSPOT_PRIVATE_APP_TOKEN=...      # scopes: crm.schemas.contacts.read + .write

python3 duve_event_properties.py                    # dry run — prints every body, writes nothing
python3 duve_event_properties.py --apply            # writes Tasks 1–4b; halts before Task 5
python3 duve_event_properties.py --apply --confirm-task5   # includes the Task 5 merge
```

Useful flags: `--tasks 1,2` to run a subset, `--group <name>` to force the
property group, `--print-payloads` to inspect every request body offline with
no token and no network calls.

The script is safely re-runnable: every create GETs first and skips if the
property already exists.

## Safety properties

- Task 1 sends **only** `label` and `description`. No `options`, `type`,
  `fieldType` or `groupName` — so there is no way for a relabel to touch the
  options array. It re-GETs afterwards and halts if `name` or the option
  count changed.
- Task 5 GETs the live options, rebuilds the array with all 18 originals
  first in their original order, appends the 3 new ones, and PATCHes the full
  merged list. It halts if the before-count is not 18, and after writing it
  asserts the count is 21 and that every original value survived.
- Task 5 additionally refuses to write without `--confirm-task5`, even under
  `--apply`.
- Nothing is ever deleted. Any 4xx that is not a clean 404-on-GET halts the
  run — no retries, no endpoint fallbacks.
- The token is read from the environment only.

## Run record — 2026-08-16, APPLIED

Property group used: **`contact_activity`** (existing; no group was created).

| Property | Before | Action | After |
|---|---|---|---|
| `conference_name` | label "Conference Name", 51 options | PATCHED (label + description) | label "Conference — Source", **51 options, name unchanged** |
| `webinar_name` | label "Webinar Name", 9 options | PATCHED (label + description) | label "Webinar — Source", **9 options, name unchanged** |
| `engagement_type` | did not exist | CREATED | enumeration/select, 8 options |
| `conference_touchpoints` | did not exist | CREATED | enumeration/checkbox, 38 options (= CSV rows) |
| `webinar_touchpoints` | did not exist | CREATED | enumeration/checkbox, 8 options |
| `event_staging` | did not exist | CREATED | string/text |
| `webinar_staging` | did not exist | CREATED | string/text |
| `lead_capture_route` | 18 options | PATCHED (merged array) | **21 options, all 18 originals present** |

All 8 end-of-run verification GETs returned PASS. The result was then
independently re-read through a separate credential to confirm; the original
option *labels* on `lead_capture_route` survived the array replace intact
(`HTR` → "Hotel Tech Report", `Outbound SDR` → "Outbound SDR (external)",
`Integrations` → "Integrations form").

`lead_capture_route` and `lead_capture_route__first_touch` now both carry 21
options and are aligned.

Re-running the script is a no-op: the creates report SKIPPED, and Task 5
detects the three values are already present and skips the merge.

## Known discrepancy — webinar 06/2025 vs 07/2025

Confirmed live on `webinar_name`. The option whose **value** is:

```
Operational Excellence: Streamlining Guest Experience - 06/2025
```

carries a **label** reading `... - 07/2025`. The two disagree on the portal
today.

`webinar_touchpoints` uses **06/2025** for both value and label, per the spec.
**The real date has not been confirmed** and this should be resolved with the
client.

Separately, the concatenated data-entry artefact:

```
Operational Excellence: Streamlining Guest Experience - 07/2025, Luxury Guest Experience - 09/2025
```

is present on `webinar_name`, holds one record, and is **excluded** from
`webinar_touchpoints`. It is being resolved manually — this script does not
delete it.

## Property group

The script GETs `/crm/v3/properties/contacts/groups`, prints every existing
group, and selects the first match from `contact_activity`,
`conversion_information`, `contactinformation`. It never creates a group. If
none of the three exist it halts and asks for `--group`. The selected group is
printed and included in the run summary.

---

# Run record — webinar backfill (applied)

`duve_backfill_webinar.py --apply`

Derived Webinar (First) and Webinar — Touchpoints from `webinar_name`
property history.

| | |
|---|---|
| contacts with `webinar_name` | 2,081 |
| contacts written | 2,081 (100% coverage) |
| from a single webinar | 1,169 |
| with multiple webinars | 903 (44%) |
| unrecognised history values | 0 |
| verification | direct read, 0 mismatches |

Webinars recovered per contact: 1 → 1,169 · 2 → 749 · 3 → 102 · 4 → 38 ·
5 → 13 · 6 → 1.

## Why First is ordered by webinar date, not by record order

`webinar_name` is declared single-select, but an automation uses it as an
**accumulator**: each history entry rewrites the whole semicolon-joined list,
and the order inside that list is not chronological. On one typical contact
the value is `Maximizing Revenue - Jan 2026` at 17:38:16 and becomes
`IT Webinar - 11/2025;Maximizing Revenue - Jan 2026` three seconds later,
because the automation back-filled an earlier registration.

Record order therefore names the wrong first webinar for **345 of the 903**
multi-webinar contacts (38%). Every webinar carries its own date in its name,
so `WEBINAR_DATE` in the script holds the real order and First is the earliest
by that date. The run halts if any active option is missing from that map.

This differs deliberately from the conference backfill, where history is
single-valued and record order is correct.

## Two data problems this exposed

1. **`Webinar (Latest)` is not a latest.** 553 of 2,081 contacts (27%) hold
   several webinars semicolon-joined in that single-select field. Now that
   Touchpoints is populated, the accumulator automation should write there and
   leave Latest holding one value — or Latest should become a checkbox and be
   renamed. Worth settling before the next webinar runs.

2. **Salesforce and bulk edits are corrupting the field.** A
   `CRM_UI_BULK_ACTION` on 2025-09-25 10:56 reset a batch of contacts to a
   single webinar, dropping others. Salesforce has been seen blanking the
   field and then writing the concatenated
   `07/2025, Luxury Guest Experience - 09/2025` string. The backfill repairs
   both from history, because it takes the union of every recorded value.
