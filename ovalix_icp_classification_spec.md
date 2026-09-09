# Ovalix — ICP Title Classification: Build Spec for Claude Code

Portal **143175417** (EU) · Contact object · September 2026
Prepared by Glare Marketing Technologies

---

## What this is

Classify the legacy Marketing Qualified Lead population in Ovalix's HubSpot portal by job title, writing three contact properties. Rules L1–L4 have already been applied by hand through HubSpot segments. This spec covers **L5 through L12**, plus cleanup of known gaps.

Do not run an AI agent per contact. The classification is rule-based and deterministic by design — every identical title must receive an identical tier, which per-record model calls cannot guarantee and which would cost thousands of credits.

---

## Current state — verify before writing anything

Scope is `lifecyclestage = "marketingqualifiedlead"`, total **8,356** contacts.

| `icp_title_tier` | Count | Source |
|---|---|---|
| *(null)* | 3,969 | **to be classified by this script** |
| `tier_1` | 2,686 | L1 — CISO variants |
| `tier_2` | 2 | hand-graded |
| `tier_3` | 561 | L3 — security architects, plus hand-graded |
| `out_of_scope` | 1,138 | L2 (CIO) + L4 (adjacent domains) + hand-graded |

**First thing the script should do is re-run this count and confirm it matches.** If the null count has drifted materially, someone has been editing in the UI and the assumptions below may be stale.

---

## Properties written

| Property | Type | Values |
|---|---|---|
| `icp_title_tier` | enumeration | `tier_1`, `tier_2`, `tier_3`, `out_of_scope`, `unknown` |
| `icp_seller_flag` | boolean | `true` / `false` |
| `icp_classification_reason` | string | one line, 15 words or fewer |

Internal values are lowercase snake case. The labels shown in the HubSpot UI ("Tier 1", "Out of scope") will not write. `icp_seller_flag` is a boolean with no null state — it reads `false` on every unclassified record, so **never use it to determine whether a record has been processed**. Use `icp_title_tier is null` for that.

Write nothing else. Do not touch lifecycle stage, contact type, lead status, or owner.

---

## Setup

Create a private app in portal 143175417 with:

- `crm.objects.contacts.read`
- `crm.objects.contacts.write`

Endpoints:

- Read: `POST /crm/v3/objects/contacts/search` — paginate on `after`, 100 per page, filter `lifecyclestage EQ marketingqualifiedlead`. Request `hs_object_id`, `jobtitle`, `icp_title_tier`, `email`, plus the associated company domain if cheap to get.
- Write: `POST /crm/v3/objects/contacts/batch/update` — 100 records per call.

Rate limit is 100 requests per 10 seconds. At 100 records per call the full run is roughly 80 write calls, so a couple of minutes with modest throttling. Back off on 429.

---

## How the rules work

Normalise once: lowercase, collapse whitespace, strip HTML entities (the data contains `&amp;`), treat `&`, `and` and `,` as equivalent separators.

Then evaluate **in order, first match wins.** A record that matches L5 is never tested against L6. Everything unmatched at the end goes `unknown`.

Match on substring, not tokens. This matters: HubSpot's own segment builder matches on token boundaries and consequently missed `cio/ciso`, `cto/ciso`, `vp-ciso`, `d/ciso`, `dciso`, `vciso manager` and `chief information officer/ciso` during L1 and L2. Plain substring matching in Python fixes those, and the cleanup rules below catch them.

Define two reusable term sets:

```
SECURITY = ["security", "cyber", "infosec", "privacy",
            "data protection", "grc", "governance"]

SECURITY_NARROW = ["security", "cyber", "infosec"]
```

---

## Cleanup rules — run these first

These correct records already written by L1–L4 and repair the token-boundary misses. They deliberately run before L5, and they are the only rules permitted to overwrite an existing value.

### C1 — Glued CISO titles missed by L1

Where `jobtitle` contains any of `ciso`, `vciso`, `dciso` **and** `icp_title_tier` is null.

Set `tier_1`, reason *CISO variant, executive security leadership.*

Exception: if the title also contains `executive assistant`, `executive business partner`, `chief of staff` or `administrative`, set `out_of_scope` instead with reason *Executive support role, not a security owner.* There is at least one such record: `executive business partner to cpto, chief marketing officer, chief revenue officer, ciso and evp's product leadership teams`.

### C2 — Vendor-side CISOs already in Tier 1

Where `icp_title_tier = "tier_1"` **and** `jobtitle` contains any of `field ciso`, `vciso`, `fractional`, `advisor`, `adviser`, `consultant`.

Leave the tier alone — a genuine CISO title at a vendor stays Tier 1 per the rubric — and set `icp_seller_flag = true`, reason *Vendor-side or advisory CISO role.* Roughly fifteen records.

### C3 — Executive assistant in Tier 1

Where `icp_title_tier = "tier_1"` and the title contains `executive assistant` or `executive business partner`. Set `out_of_scope`, reason *Executive support role, not a security owner.*

---

## Classification rules L5–L12

### L5 — Assistant VP level → `tier_2`
`SECURITY` **and** any of `avp`, `assistant vice president`, `associate vice president`
Reason: *Assistant VP owning a security function.*
Must precede L6, because `vp` is a substring of `avp`.

### L6 — Executive security leadership → `tier_1`
`SECURITY` **and** any of `chief`, `cso`, `evp`, `svp`, `executive vice president`, `senior vice president`, `vice president`, `vp`
Reason: *Executive security leadership.*

### L7 — Mid-senior ownership → `tier_2`
`SECURITY` **and** any of `director`, `head of`, `deputy`, `officer`, `lead`
Reason: *Director-level security function owner.*
Expected to be the largest remaining block.

### L8 — Senior technical contributors → `tier_3`
`SECURITY_NARROW` **and** any of `senior manager`, `engineer`
Reason: *Senior security practitioner, evaluates and influences.*

**Open decision, see below.** Whether `manager` joins this list changes roughly 150 records.

### L9 — Data and AI executives → `tier_1`
Any of `chief data officer`, `chief ai officer`, `chief data`, `chief artificial intelligence`, `chief ai`
Reason: *Data or AI executive, in-scope buying committee role.*
These carry no security term, so nothing above catches them. Without this rule they fall to L11 and are wrongly excluded — the rubric lists Chief Data Officer and Chief AI Officer as Tier 1.

### L10 — AI governance ownership → `tier_2`
Any of `head of ai`, `ai governance`, `artificial intelligence head`, `ai security`, `ai risk`
**and** not containing `innovation`
Reason: *AI governance owner, named champion role.*
The innovation exclusion separates "Head of AI" (a champion in Ovalix's messaging document) from "Head of AI and Innovation" (an excluded strategy role).

### L11 — Sellers → `out_of_scope`, `icp_seller_flag = true`
Any of `sales`, `business development`, `account executive`, `account manager`, `channel`, `reseller`, `partnerships`, `alliance`, `pre-sales`, `presales`, `sales engineer`
Reason: *Sales or channel role, selling rather than buying.*
This is where the real seller population lives — roughly a hundred records. Most have no security term in the title, so L4 never saw them.

### L12 — Everything else with a title → `out_of_scope`
`jobtitle` is non-empty
Reason: *Role outside the security buying committee.*
Catch-all. CEOs, CTOs, controllers, attorneys, interns.

### L13 — No title → `unknown`
`jobtitle` is empty or null. Expected **384**.
Reason: *Title blank, needs enrichment.*
This is the enrichment queue. Per section 6 of the classification document these records will not re-evaluate on their own once data improves, so ownership of re-processing needs to sit with someone.

---

## Required behaviours

### Dry run first, always

Default to no writes. Produce `ovalix_icp_dryrun.csv` with one row per record:

```
hs_object_id, email, jobtitle, company_domain,
current_tier, proposed_tier, proposed_seller_flag,
proposed_reason, rule_fired
```

Only write when invoked with an explicit `--write` flag. This is the same pattern used on the Adaptive6 stage-clock work and it exists so the classification can be reviewed — and sent to the client for approval — before it touches the CRM.

### Capture rollback state

Before any write, dump `ovalix_icp_before.csv` with `hs_object_id` and the current value of all three properties. Every record in scope is currently null on `icp_title_tier`, so rollback is a batch update setting them back to empty. Provide a `--rollback <file>` mode that does exactly that.

This matters because one decision in this work — see below — may be reversed by the client after review.

### Residue file

Rules cannot do judgment. Write `ovalix_icp_residue.csv` containing any record where:

- The title matched **more than one** rule's terms in conflicting directions — for example an architecture term alongside a director-level security term, or an excluded-domain term alongside an executive security term.
- The title matched **no rule** but is non-empty and contains a security term.
- The title is under 4 characters, or is a bare generic (`director`, `manager`, `consultant`, `analyst`, `it`) with no qualifier — these should go `unknown`, not be guessed at.

Do not write these records. They go to a human.

The reason this matters concretely: 32 architect records were hand-graded during the manual phase because the terms `identity`, `cloud` and `application` appear both as a person's actual domain (excluded) and as scope inside a broader in-house architecture role (Tier 3). `Senior Director and Group VP, Head of Security Architecture, Identity Management, and Engineering` is not an IAM specialist. No rule distinguishes those two cases; a person reading the title does it instantly.

### Validate against the existing segments

The manual work already produced counts that the script must reproduce. After the dry run, compare:

- Records the script would assign `tier_3` via architecture rules against the existing "ICP L3" segment — should be **561** including the hand-graded.
- The `unknown` bucket should land close to **384**.
- Total processed plus already-classified should equal **8,356**.

Disagreement means one of the two implementations is wrong. Investigate before writing rather than after.

---

---

## Resolved decisions

Three questions came up while building the script. All three are settled; they
are recorded here so the next reader does not have to re-derive them.

### Regional CISO is Tier 1, not Tier 2

The rubric contradicts itself. Step 1 says region and scope prefixes — Global,
Group, Regional, Corporate, Enterprise, EMEA, APAC — do not change the tier.
The Tier 2 example list contains "Regional CISO".

**Resolved: Tier 1.** Two reasons. The rubric states its own example lists are
"calibration examples, not the boundary", so an explicit Step 1 rule outranks
an entry in an example list. And L1 has already written `emea ciso`,
`americas ciso`, `ny ciso` and `divisional ciso` as `tier_1`. Grading new
regional CISOs `tier_2` would split identical titles across two tiers, which
is the failure mode the whole positive-assignment rebuild exists to prevent.

### Director-level architects are Tier 3, not Tier 2

Also a rubric self-contradiction. The Tier 2 description covers "Director,
Senior Director, or Head level, owning a security ... function". The Tier 3
example list explicitly contains "Director of Security Architecture", plus
"Lead Information Security Architect" and "Lead Cybersecurity Architect".

**Resolved: Tier 3.** Architecture is tested before the Director/Head rule, so
any architecture title grades Tier 3 regardless of its rank prefix. Same
consistency argument as Regional CISO: an architect is an architect, and
splitting `Security Architect` from `Director of Security Architecture` across
two tiers would put identical work in different buckets.

Note this differs from the Regional CISO reasoning in which side won. There the
conflict was between a Step 1 *rule* and an example, and the rule won. Here it
is between a tier *description* and an example, and the example won, because a
description is a summary of the examples rather than a rule that overrides them.

### Security managers are Tier 3, not out of scope

`information security manager` (100), `it security manager` (19),
`information technology security manager` (18), `cyber security manager` (6),
`cybersecurity manager` (4) clear none of the three tiers as the rubric stands,
because Tier 3 is architect-grade plus *Senior* Manager only.

**Resolved: Tier 3.** Measured against the live portal the population is
roughly 230, not the ~150 estimated below — too many to lose, and Tier 3's
own definition ("influence and evaluate but usually do not own budget") is an
accurate description of the role. This is now the script's default; pass
`--exclude-manager` to revert it.

---

## One open decision

### ~~Security managers — ~150 records, decide before running L8~~ — RESOLVED

**Settled: Tier 3.** See Resolved decisions above. The analysis below is kept
for its reasoning; the count was measured at ~230, not ~150.

`information security manager` (100), `it security manager` (19), `information technology security manager` (18), `cyber security manager` (6), `cybersecurity manager` (4) clear none of the three tiers as the rubric stands. Tier 3 is architect-grade plus *Senior* Manager only. So they fall through to L12 and are marked out of scope.

That may be correct for a platform sold to CISOs. It also makes an information security manager at a 5,000-person bank invisible to marketing. Adding `manager` to L8 moves them to Tier 3.

Make this call deliberately. Do not let L12 make it by default.

It was made deliberately: Tier 3. The script no longer lets the catch-all decide
— unmatched security managers go to the residue file, never silently out of scope.

### CIO exclusion — 1,044 records, needs client sign-off

L2 marked 1,044 contacts — an eighth of the legacy MQL population — as out of scope on the grounds that they are CIOs. The basis is Ovalix's own messaging and positioning document, which names CISO as decision maker and champion, lists Head of Information Security and Head of AI as champions, and places IT in the **blocker** row. No CIO appears anywhere as a buyer.

That is a defensible reading and the rubric produces it without amendment. It is also the only decision in this work that is commercial rather than mechanical, and it should be confirmed explicitly by Kamela and Elana rather than absorbed as a side effect of a classification script.

If it is reversed, the rollback file plus a re-run with CIO mapped to `tier_1` or `tier_3` is the remedy. Do not hand-edit.

---

## What not to do

- Do not write `icp_fit`, `contact_type`, `lifecyclestage` or `hs_lead_status`. Separate workflows own those and two processes writing one property is the defect this entire rebuild exists to fix.
- Do not infer a tier from company size, country or industry. Those are workflow gates applied later by `Ops: ICP Fit`.
- Do not invent a title where the field is blank. Blank means `unknown`.
- Do not guess when torn between `out_of_scope` and a tier. `unknown` feeds an enrichment queue; a wrong tier feeds the sales team.
