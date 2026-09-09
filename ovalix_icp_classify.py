#!/usr/bin/env python3
"""
Ovalix — ICP Title Classification (portal 143175417, EU)
Glare Marketing Technologies · September 2026

Rule-based, deterministic classification of the legacy MQL population by job
title. Implements cleanup rules C1-C3 and classification rules L5-L13 from
ovalix_icp_classification_spec.md. L1-L4 were applied by hand and are not
re-implemented here.

Writes exactly three contact properties:
    icp_title_tier, icp_seller_flag, icp_classification_reason

Never writes icp_fit, contact_type, lifecyclestage or hs_lead_status —
separate workflows own those.

Default mode is a dry run. Nothing reaches the CRM without --write.

Usage:
    export HUBSPOT_PRIVATE_APP_TOKEN=pat-eu1-...
    python ovalix_icp_classify.py                      # dry run -> CSVs
    python ovalix_icp_classify.py --include-manager    # L8 open decision ON
    python ovalix_icp_classify.py --write              # apply to CRM
    python ovalix_icp_classify.py --rollback ovalix_icp_before.csv
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

BASE = "https://api.hubapi.com"
PORTAL_ID = "143175417"
SCOPE_LIFECYCLE = "marketingqualifiedlead"

TIER_PROP = "icp_title_tier"
FLAG_PROP = "icp_seller_flag"
REASON_PROP = "icp_classification_reason"
WRITTEN_PROPS = [TIER_PROP, FLAG_PROP, REASON_PROP]

# Properties we refuse to write, defensively. Guarded at the batch layer so a
# future edit to the rule table cannot leak one of these into a payload.
FORBIDDEN_PROPS = {"icp_fit", "contact_type", "lifecyclestage", "hs_lead_status"}

READ_PROPS = ["hs_object_id", "jobtitle", TIER_PROP, FLAG_PROP, REASON_PROP, "email"]

# Expected counts from the manual phase. Used for the pre-flight assertion.
EXPECTED_TOTAL = 8356
EXPECTED_BASELINE = {
    None: 3969,
    "tier_1": 2686,
    "tier_2": 2,
    "tier_3": 561,
    "out_of_scope": 1138,
}
EXPECTED_UNKNOWN = 384
EXPECTED_L3_SEGMENT = 561

DRYRUN_CSV = "ovalix_icp_dryrun.csv"
BEFORE_CSV = "ovalix_icp_before.csv"
RESIDUE_CSV = "ovalix_icp_residue.csv"

# --------------------------------------------------------------------------
# Term sets
# --------------------------------------------------------------------------

SECURITY = ["security", "cyber", "infosec", "privacy",
            "data protection", "grc", "governance"]

SECURITY_NARROW = ["security", "cyber", "infosec"]

# Residue-detection sets. The spec names identity / cloud / application as the
# ambiguous terms and the rubric's out-of-scope row names the excluded domains;
# neither document gives a closed list, so these are inferred. Widening them
# only ever moves records to the residue file for a human — never to a tier.
ARCHITECTURE = ["architect", "architecture"]
EXCLUDED_DOMAIN = ["identity", "iam", "cloud", "application", "appsec",
                   "soc", "audit", "endpoint", "network"]
BARE_GENERIC = ["director", "manager", "consultant", "analyst", "it"]

CISO_TERMS = ["ciso", "vciso", "dciso"]
EXEC_SUPPORT = ["executive assistant", "executive business partner",
                "chief of staff", "administrative"]
VENDOR_TERMS = ["field ciso", "vciso", "fractional", "advisor", "adviser",
                "consultant"]

AVP_TERMS = ["avp", "assistant vice president", "associate vice president"]
EXEC_TERMS = ["chief", "cso", "evp", "svp", "executive vice president",
              "senior vice president", "vice president", "vp"]
MIDSENIOR_TERMS = ["director", "head of", "deputy", "officer", "lead"]
SENIOR_TECH_TERMS = ["senior manager", "engineer"]
DATA_AI_EXEC_TERMS = ["chief data officer", "chief ai officer", "chief data",
                      "chief artificial intelligence", "chief ai"]
AI_GOV_TERMS = ["head of ai", "ai governance", "artificial intelligence head",
                "ai security", "ai risk"]
SELLER_TERMS = ["sales", "business development", "account executive",
                "account manager", "channel", "reseller", "partnerships",
                "alliance", "pre-sales", "presales", "sales engineer"]

# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_SEP = re.compile(r"\band\b|&|,|/|\|")


def normalise(raw):
    """Lowercase, unescape HTML entities, flatten separators, collapse space.

    Entities are unescaped before separators are flattened, so '&amp;' becomes
    '&' and then a space rather than surviving as literal text. '&', the word
    'and', ',', '/' and '|' are all equivalent separators — '/' matters because
    the token-boundary misses the spec calls out ('cio/ciso', 'vp-ciso') are
    slash- and hyphen-joined.
    """
    if raw is None:
        return ""
    t = html.unescape(str(raw))
    t = t.lower()
    t = _SEP.sub(" ", t)
    t = t.replace("-", " ") if False else t  # hyphens kept: 'pre-sales' is a term
    t = _WS.sub(" ", t).strip()
    return t


def has_any(title, terms):
    """Substring match, not token match. This is the point of the rewrite."""
    return any(term in title for term in terms)


def matched(title, terms):
    return [term for term in terms if term in title]


# --------------------------------------------------------------------------
# Rule engine
# --------------------------------------------------------------------------

class Result:
    __slots__ = ("tier", "flag", "reason", "rule", "overwrite")

    def __init__(self, tier, flag, reason, rule, overwrite=False):
        self.tier = tier
        self.flag = flag
        self.reason = reason
        self.rule = rule
        self.overwrite = overwrite


def classify(title_raw, current_tier, include_manager=False):
    """Evaluate cleanup rules then L5-L13, in order, first match wins.

    Returns a Result, or None when no rule applies (which cannot happen —
    L12/L13 between them are total over the input domain).
    """
    t = normalise(title_raw)

    # ---------------- Cleanup rules — run first, and are the only rules
    # ---------------- permitted to overwrite an existing value.

    # C1 — glued CISO titles missed by L1's token-boundary matching.
    if has_any(t, CISO_TERMS) and current_tier is None:
        if has_any(t, EXEC_SUPPORT):
            return Result("out_of_scope", False,
                          "Executive support role, not a security owner.",
                          "C1-exception", overwrite=True)
        return Result("tier_1", False,
                      "CISO variant, executive security leadership.",
                      "C1", overwrite=True)

    # C3 before C2: both target existing tier_1, and an executive assistant
    # whose title also contains 'consultant' must leave as out_of_scope rather
    # than as a flagged tier_1. C3 is the stricter correction, so it wins.
    if current_tier == "tier_1" and has_any(t, EXEC_SUPPORT[:2]):
        return Result("out_of_scope", False,
                      "Executive support role, not a security owner.",
                      "C3", overwrite=True)

    # C2 — vendor-side CISOs. Tier is deliberately left alone; only the seller
    # flag is set. A genuine CISO title at a vendor stays Tier 1 per the rubric.
    if current_tier == "tier_1" and has_any(t, VENDOR_TERMS):
        return Result("tier_1", True,
                      "Vendor-side or advisory CISO role.",
                      "C2", overwrite=True)

    # Records already carrying a value and not caught by cleanup are left
    # untouched. Only C1-C3 may overwrite.
    if current_tier is not None:
        return None

    # ---------------- Classification rules L5-L13

    # L5 must precede L6: 'vp' is a substring of 'avp'.
    if has_any(t, SECURITY) and has_any(t, AVP_TERMS):
        return Result("tier_2", False,
                      "Assistant VP owning a security function.", "L5")

    if has_any(t, SECURITY) and has_any(t, EXEC_TERMS):
        return Result("tier_1", False, "Executive security leadership.", "L6")

    if has_any(t, SECURITY) and has_any(t, MIDSENIOR_TERMS):
        return Result("tier_2", False,
                      "Director-level security function owner.", "L7")

    l8_terms = list(SENIOR_TECH_TERMS) + (["manager"] if include_manager else [])
    if has_any(t, SECURITY_NARROW) and has_any(t, l8_terms):
        return Result("tier_3", False,
                      "Senior security practitioner, evaluates and influences.",
                      "L8")

    if has_any(t, DATA_AI_EXEC_TERMS):
        return Result("tier_1", False,
                      "Data or AI executive, in-scope buying committee role.",
                      "L9")

    if has_any(t, AI_GOV_TERMS) and "innovation" not in t:
        return Result("tier_2", False,
                      "AI governance owner, named champion role.", "L10")

    if has_any(t, SELLER_TERMS):
        return Result("out_of_scope", True,
                      "Sales or channel role, selling rather than buying.",
                      "L11")

    if t:
        return Result("out_of_scope", False,
                      "Role outside the security buying committee.", "L12")

    # L13 — blank title. The enrichment queue.
    return Result("unknown", False, "Title blank, needs enrichment.", "L13")


# --------------------------------------------------------------------------
# Residue detection — rules cannot do judgment
# --------------------------------------------------------------------------

def residue_reason(title_raw, result):
    """Return a string when the record needs a human, else None.

    Residue records are reported and excluded from the write set entirely.
    """
    t = normalise(title_raw)
    if not t:
        return None  # blank titles are L13's job, not residue

    # Conflicting directions: an architecture term alongside a director-level
    # security term, or an excluded-domain term alongside an executive term.
    if has_any(t, ARCHITECTURE) and has_any(t, SECURITY) and has_any(t, MIDSENIOR_TERMS):
        return "Architecture term with director-level security term"
    if has_any(t, EXCLUDED_DOMAIN) and has_any(t, SECURITY) and has_any(t, EXEC_TERMS):
        return "Excluded-domain term with executive security term"

    # Fell through to the L12 catch-all but carries a security term — no rule
    # placed it, and it is not obviously out of scope.
    if result and result.rule == "L12" and has_any(t, SECURITY):
        return "Security term present but matched no classification rule"

    # Too short, or a bare generic with no qualifier. These belong in unknown,
    # not in a guessed tier — but a human decides, so they are not written.
    if len(t) < 4:
        return "Title under 4 characters"
    if t in BARE_GENERIC:
        return "Bare generic title with no qualifier"

    return None


# --------------------------------------------------------------------------
# HubSpot client
# --------------------------------------------------------------------------

class HubSpot:
    def __init__(self, token, dry_run=True):
        self.token = token
        self.dry_run = dry_run
        self.calls = 0

    def _request(self, method, path, payload=None, attempt=0):
        url = BASE + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.calls += 1
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 5:
                wait = min(2 ** attempt, 16)
                sys.stderr.write(
                    "  %s from HubSpot, backing off %ss (attempt %d)\n"
                    % (e.code, wait, attempt + 1))
                time.sleep(wait)
                return self._request(method, path, payload, attempt + 1)
            sys.stderr.write("HTTP %s on %s %s\n%s\n"
                             % (e.code, method, path, e.read().decode()[:1000]))
            raise

    def search_contacts(self):
        """Page the MQL population. Search caps at 10k results; 8,356 fits."""
        out, after, page = [], None, 0
        while True:
            payload = {
                "filterGroups": [{"filters": [{
                    "propertyName": "lifecyclestage",
                    "operator": "EQ",
                    "value": SCOPE_LIFECYCLE,
                }]}],
                "properties": READ_PROPS,
                "limit": 100,
            }
            if after:
                payload["after"] = after
            res = self._request("POST", "/crm/v3/objects/contacts/search", payload)
            out.extend(res.get("results", []))
            page += 1
            if page % 10 == 0:
                sys.stderr.write("  fetched %d contacts...\n" % len(out))
            after = res.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
            if len(out) >= 10000:
                sys.stderr.write(
                    "WARNING: hit the 10,000-result search ceiling. "
                    "Scope is larger than the spec assumes — stopping.\n")
                break
            time.sleep(0.22)  # search endpoint is 5 req/s, stricter than 100/10s
        return out

    def batch_update(self, inputs):
        """100 records per call, with the forbidden-property guard."""
        for chunk_start in range(0, len(inputs), 100):
            chunk = inputs[chunk_start:chunk_start + 100]
            for item in chunk:
                leaked = FORBIDDEN_PROPS & set(item["properties"])
                if leaked:
                    raise RuntimeError(
                        "Refusing to write forbidden properties: %s" % sorted(leaked))
            if self.dry_run:
                continue
            self._request("POST", "/crm/v3/objects/contacts/batch/update",
                          {"inputs": chunk})
            sys.stderr.write("  wrote %d/%d\n"
                             % (min(chunk_start + 100, len(inputs)), len(inputs)))
            time.sleep(0.12)  # ~8 req/s, well inside 100 per 10s


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def preflight(contacts):
    """Re-run the baseline count and confirm it matches the spec."""
    counts = Counter()
    for c in contacts:
        counts[c["properties"].get(TIER_PROP) or None] += 1

    print("\nBaseline check — scope lifecyclestage = %s" % SCOPE_LIFECYCLE)
    print("  %-14s %8s %8s %8s" % ("tier", "found", "expected", "delta"))
    ok = True
    for tier, expected in EXPECTED_BASELINE.items():
        found = counts.get(tier, 0)
        delta = found - expected
        if delta:
            ok = False
        print("  %-14s %8d %8d %+8d"
              % (tier or "(null)", found, expected, delta))
    total = sum(counts.values())
    print("  %-14s %8d %8d %+8d"
          % ("TOTAL", total, EXPECTED_TOTAL, total - EXPECTED_TOTAL))
    if total != EXPECTED_TOTAL:
        ok = False

    if not ok:
        print("\n  Baseline has DRIFTED from the spec. Someone has been editing")
        print("  in the UI, or L1-L4 have moved. The assumptions behind C1-C3")
        print("  may be stale — investigate before writing.")
    else:
        print("\n  Baseline matches the spec exactly.")
    return ok


def validate(rows):
    """Post-dry-run comparison against the manual phase's counts."""
    proposed = Counter(r["proposed_tier"] for r in rows if r["proposed_tier"])
    rules = Counter(r["rule_fired"] for r in rows if r["rule_fired"])

    print("\nProposed changes by rule")
    for rule in ["C1", "C1-exception", "C2", "C3",
                 "L5", "L6", "L7", "L8", "L9", "L10", "L11", "L12", "L13"]:
        if rules.get(rule):
            print("  %-14s %6d" % (rule, rules[rule]))

    print("\nProposed tier distribution")
    for tier in ["tier_1", "tier_2", "tier_3", "out_of_scope", "unknown"]:
        print("  %-14s %6d" % (tier, proposed.get(tier, 0)))

    print("\nValidation against the manual phase")
    unknown = proposed.get("unknown", 0)
    print("  unknown %d vs expected ~%d (delta %+d)%s"
          % (unknown, EXPECTED_UNKNOWN, unknown - EXPECTED_UNKNOWN,
             "" if abs(unknown - EXPECTED_UNKNOWN) <= 20 else "   <-- INVESTIGATE"))
    print("  NOTE: the spec's third check — script-assigned tier_3 vs the ICP L3")
    print("  segment of %d — cannot be run as written. L5-L13 contain no"
          % EXPECTED_L3_SEGMENT)
    print("  architecture rule; L3 was applied by hand and is already on the")
    print("  records. Script tier_3 comes from L8 only (%d records). The"
          % proposed.get("tier_3", 0))
    print("  meaningful check is existing 561 + L8 output = final tier_3.")


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("  wrote %s (%d rows)" % (path, len(rows)))


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------

def rollback(client, path):
    inputs = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            inputs.append({
                "id": row["hs_object_id"],
                "properties": {
                    TIER_PROP: row.get(TIER_PROP, "") or "",
                    FLAG_PROP: row.get(FLAG_PROP, "") or "false",
                    REASON_PROP: row.get(REASON_PROP, "") or "",
                },
            })
    print("Restoring %d records from %s" % (len(inputs), path))
    client.batch_update(inputs)
    print("Rollback complete.")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="apply changes to the CRM (default is dry run only)")
    ap.add_argument("--rollback", metavar="FILE",
                    help="restore the three properties from a before-file")
    ap.add_argument("--include-manager", action="store_true",
                    help="OPEN DECISION: add 'manager' to L8, moving ~150 "
                         "security managers from out_of_scope to tier_3")
    args = ap.parse_args()

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        sys.exit("Set HUBSPOT_PRIVATE_APP_TOKEN (portal %s, contacts read+write)."
                 % PORTAL_ID)

    client = HubSpot(token, dry_run=not args.write)

    if args.rollback:
        if not args.write:
            sys.exit("--rollback also needs --write. It is a write operation.")
        return rollback(client, args.rollback)

    print("Fetching MQL population from portal %s..." % PORTAL_ID)
    contacts = client.search_contacts()
    print("  %d contacts in scope" % len(contacts))

    baseline_ok = preflight(contacts)

    if args.include_manager:
        print("\n  L8 OPEN DECISION: 'manager' INCLUDED — security managers -> tier_3")
    else:
        print("\n  L8 OPEN DECISION: 'manager' excluded (spec default) — security")
        print("  managers fall to L12 and are marked out_of_scope. ~150 records.")

    rows, residue, before, updates = [], [], [], []

    for c in contacts:
        props = c["properties"]
        cid = c["id"]
        title = props.get("jobtitle") or ""
        current = props.get(TIER_PROP) or None

        result = classify(title, current, include_manager=args.include_manager)
        if result is None:
            continue  # already classified, no cleanup rule applies

        flagged = residue_reason(title, result)

        rows.append({
            "hs_object_id": cid,
            "email": props.get("email") or "",
            "jobtitle": title,
            "company_domain": "",
            "current_tier": current or "",
            "proposed_tier": "" if flagged else result.tier,
            "proposed_seller_flag": "" if flagged else str(result.flag).lower(),
            "proposed_reason": "" if flagged else result.reason,
            "rule_fired": "RESIDUE" if flagged else result.rule,
        })

        if flagged:
            residue.append({
                "hs_object_id": cid,
                "email": props.get("email") or "",
                "jobtitle": title,
                "current_tier": current or "",
                "would_have_fired": result.rule,
                "would_have_set": result.tier,
                "residue_reason": flagged,
            })
            continue

        before.append({
            "hs_object_id": cid,
            TIER_PROP: props.get(TIER_PROP) or "",
            FLAG_PROP: props.get(FLAG_PROP) or "",
            REASON_PROP: props.get(REASON_PROP) or "",
        })
        updates.append({
            "id": cid,
            "properties": {
                TIER_PROP: result.tier,
                FLAG_PROP: "true" if result.flag else "false",
                REASON_PROP: result.reason,
            },
        })

    print("\nWriting output files")
    write_csv(DRYRUN_CSV,
              ["hs_object_id", "email", "jobtitle", "company_domain",
               "current_tier", "proposed_tier", "proposed_seller_flag",
               "proposed_reason", "rule_fired"], rows)
    write_csv(RESIDUE_CSV,
              ["hs_object_id", "email", "jobtitle", "current_tier",
               "would_have_fired", "would_have_set", "residue_reason"], residue)

    validate(rows)
    print("\n  %d records held back to %s for human review — not written."
          % (len(residue), RESIDUE_CSV))

    if not args.write:
        print("\nDRY RUN — nothing was written. %d records would change."
              % len(updates))
        print("Review %s, then re-run with --write." % DRYRUN_CSV)
        return

    if not baseline_ok:
        sys.exit("\nRefusing to write: the baseline count does not match the "
                 "spec. Re-read the drift warning above.")

    write_csv(BEFORE_CSV, ["hs_object_id"] + WRITTEN_PROPS, before)
    print("\nWriting %d records to portal %s..." % (len(updates), PORTAL_ID))
    client.batch_update(updates)
    print("Done. Rollback with: --write --rollback %s" % BEFORE_CSV)


if __name__ == "__main__":
    main()
