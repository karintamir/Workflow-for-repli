#!/usr/bin/env python3
"""
Ovalix — ICP Title Classification (portal 143175417, EU)
Glare Marketing Technologies · September 2026

Deterministic implementation of icp_title_rubric.md (the ICP Title Agent
instructions), scoped by ovalix_icp_classification_spec.md to the legacy MQL
population. Rules L1-L4 were applied by hand and are not re-implemented; the
cleanup rules C1-C3 repair their token-boundary misses.

The rubric is a FOUR-STAGE method and this script follows that order, not the
spec's flat first-match-wins list:

    Step 1  normalise            normalise()
    Step 2  assign a tier        assign_tier()
    Step 3  apply exclusions     apply_exclusions()   <- runs AFTER Step 2
    Step 4  seller flag          seller_flag()        <- independent of tier
    Step 5  reason               carried on each rule

Matching direction matters. Tier assignment matches on SUBSTRING, per the spec,
because that is what catches the token-boundary misses HubSpot's segment builder
made ('cio/ciso', 'vp-ciso', 'dciso'). Exclusions match on WORD BOUNDARY, per the
rubric's "never exclude on a substring", because that is what stops 'soc'
matching inside 'Associate' and 'assistant' matching 'Assistant Director'.

Writes exactly three contact properties:
    icp_title_tier, icp_seller_flag, icp_classification_reason

Never writes icp_fit, contact_type, lifecyclestage or hs_lead_status.

Usage:
    export HUBSPOT_PRIVATE_APP_TOKEN=pat-eu1-...
    python ovalix_icp_classify.py                   # dry run -> CSVs
    python ovalix_icp_classify.py --no-company      # skip company enrichment
    python ovalix_icp_classify.py --include-manager # L8 open decision ON
    python ovalix_icp_classify.py --write           # apply to CRM
    python ovalix_icp_classify.py --write --rollback ovalix_icp_before.csv
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

# Guarded at the batch layer so a later edit to a rule table cannot leak one
# of these into a payload. Hard constraint 1 of the rubric.
FORBIDDEN_PROPS = {"icp_fit", "contact_type", "lifecyclestage", "hs_lead_status"}

READ_PROPS = ["hs_object_id", "jobtitle", TIER_PROP, FLAG_PROP, REASON_PROP, "email"]
COMPANY_PROPS = ["name", "domain", "industry"]

EXPECTED_TOTAL = 8356
EXPECTED_BASELINE = {
    None: 3969, "tier_1": 2686, "tier_2": 2, "tier_3": 561, "out_of_scope": 1138,
}
EXPECTED_UNKNOWN = 384
EXPECTED_L3_SEGMENT = 561

DRYRUN_CSV = "ovalix_icp_dryrun.csv"
BEFORE_CSV = "ovalix_icp_before.csv"
RESIDUE_CSV = "ovalix_icp_residue.csv"
GRADED_CSV = "ovalix_icp_residue_graded.csv"
GRADED_FIXED_CSV = "ovalix_icp_residue_graded_fixed.csv"

ALLOWED_TIERS = {"tier_1", "tier_2", "tier_3", "out_of_scope", "unknown"}

# ==========================================================================
# Step 1 — Normalisation
# ==========================================================================

# Acronyms the rubric names explicitly. Expansions are APPENDED rather than
# substituted, so both the acronym and its expansion are matchable and no
# original substring is destroyed.
ACRONYMS = {
    "ciso": "chief information security officer",
    "infosec": "information security",
    "grc": "governance risk compliance",
    "cpo": "chief privacy officer",
    "caio": "chief ai officer",
    "vp": "vice president",
    "svp": "senior vice president",
    "evp": "executive vice president",
    "biso": "business information security officer",
}

_WS = re.compile(r"\s+")
_SEP = re.compile(r"\band\b|&|,|/|\||\(|\)|-")
_OF = re.compile(r"\bof\b|\bthe\b")


def normalise(raw):
    """Step 1. Formatting must never change the tier.

    Order is load-bearing: entities are unescaped before '&' becomes a
    separator, and acronyms expand after separators flatten so 'cio/ciso'
    has already become 'cio ciso' and both halves expand.
    """
    if raw is None:
        return ""
    t = html.unescape(str(raw)).lower()
    t = _SEP.sub(" ", t)          # & / and / , / slash / pipe / parens / hyphen
    t = _OF.sub(" ", t)           # "Director of Information Security" == "Director Information Security"
    t = _WS.sub(" ", t).strip()

    words = set(t.split())
    for acro, expansion in ACRONYMS.items():
        if acro in words:
            t += " " + expansion
    return _WS.sub(" ", t).strip()


def has(t, terms):
    """Substring match — used for TIER ASSIGNMENT only."""
    return any(term in t for term in terms)


def has_word(t, terms):
    """Word-boundary match — used for EXCLUSIONS only.

    This is the rubric's "never exclude on a substring": it is what stops
    'soc' matching inside 'associate'.
    """
    return any(re.search(r"\b%s\b" % re.escape(term), t) for term in terms)


def hit_word(t, terms):
    for term in terms:
        if re.search(r"\b%s\b" % re.escape(term), t):
            return term
    return None


# ==========================================================================
# Step 2 — Term sets for tier assignment
# ==========================================================================

# 'of'/'the' are stripped in normalisation, so "head of" is written "head".
# Tier 1's remit per the rubric: "security, cyber, information security,
# privacy, data, or AI governance". Note what is ABSENT -- bare 'risk' and
# bare 'governance'. Those appear only in Tier 2's remit, so promoting on them
# grades an IT VP with a vendor-risk remit as executive security leadership.
TIER1_REMIT = ["security", "cyber", "infosec", "privacy",
               "data protection", "ai governance"]

# Tier 2's remit adds risk, governance and GRC. Also used by the architecture
# and senior-contributor rules, which the rubric places in security broadly.
SECURITY = TIER1_REMIT + ["grc", "governance", "risk"]

ARCHITECTURE = ["architect", "architecture"]
EXEC = ["chief", "cso", "evp", "svp", "executive vice president",
        "senior vice president", "vice president", "vp"]
AVP = ["avp", "assistant vice president", "associate vice president",
       "assistant vp", "associate vp"]
MIDSENIOR = ["director", "head", "deputy", "officer", "lead"]
SENIOR_TECH = ["senior manager", "engineer"]
DATA_AI_EXEC = ["chief data officer", "chief ai officer", "chief data",
                "chief artificial intelligence", "chief ai"]
AI_GOV = ["head ai", "ai governance", "artificial intelligence head",
          "ai security", "ai risk"]
BARE_GENERIC = {"director", "manager", "consultant", "analyst", "it"}


def assign_tier(t, include_manager=False):
    """Step 2. Returns (tier, reason, rule) or None if nothing places it."""

    # Architecture first. The rubric names "Director of Security Architecture",
    # "Lead Information Security Architect" and "Lead Cybersecurity Architect"
    # as Tier 3 examples, so architecture outranks the Director/Lead reading of
    # Tier 2. Confirmed decision: the named examples win.
    if has(t, SECURITY) and has(t, ARCHITECTURE):
        return ("tier_3", "Security architecture role, senior technical contributor.", "S2-arch")

    # AVP before exec: 'vp' is a substring of 'avp'.
    if has(t, SECURITY) and has(t, AVP):
        return ("tier_2", "Assistant VP owning a security function.", "S2-avp")

    # Deputy is Tier 2 level. Tested before exec because expanding CISO to
    # "chief information security officer" puts 'chief' into every CISO title,
    # which would otherwise promote "Deputy CISO" to Tier 1.
    if has(t, SECURITY) and has(t, ["deputy"]):
        return ("tier_2", "Deputy-level security function owner.", "S2-deputy")

    if has(t, TIER1_REMIT) and has(t, EXEC):
        return ("tier_1", "Executive security leadership.", "S2-exec")

    if has(t, DATA_AI_EXEC):
        return ("tier_1", "Data or AI executive, in-scope buying committee role.", "S2-dataai")

    if has(t, SECURITY) and has(t, MIDSENIOR):
        return ("tier_2", "Director-level security function owner.", "S2-mid")

    # Senior technical contributors. Uses the BROAD security set, not the
    # spec's SECURITY_NARROW: the rubric lists "Senior Manager, AI Governance"
    # as Tier 3, and 'governance' is not in the narrow set.
    tech = list(SENIOR_TECH) + (["manager"] if include_manager else [])
    if has(t, SECURITY) and has(t, tech):
        return ("tier_3", "Senior security practitioner, evaluates and influences.", "S2-tech")

    if has(t, AI_GOV) and "innovation" not in t:
        return ("tier_2", "AI governance owner, named champion role.", "S2-aigov")

    return None


# ==========================================================================
# Step 3 — Exclusions (word-boundary, applied AFTER Step 2)
# ==========================================================================

EXCL_ADJACENT = [
    (["iam", "identity access management", "identity management"],
     "Identity and access management role, adjacent domain excluded."),
    (["appsec", "application security", "product security"],
     "Application security role, adjacent domain excluded from ICP."),
    (["soc", "security operations center", "security operations centre"],
     "Security operations centre role, adjacent domain excluded."),
    (["cloud security"],
     "Cloud security role, adjacent domain excluded from ICP."),
    (["audit", "auditing", "assurance", "auditor"],
     "Audit or assurance role, adjacent domain excluded."),
    (["legal", "counsel", "attorney"],
     "Legal role, adjacent domain excluded from ICP."),
    (["partner", "partners", "partnerships", "channel", "alliance",
      "alliances", "reseller", "distributor"],
     "Partner or alliance role, selling rather than buying."),
]

EXCL_NONSECURITY = [
    (["sales", "business development", "account executive", "account manager"],
     "Sales role, selling rather than buying."),
    (["consulting", "consultancy", "consultant"],
     "Consulting role, outside the security buying committee."),
    (["program management", "project management", "programme management",
      "program manager", "project manager", "delivery"],
     "Delivery or programme role, outside security buying committee."),
    (["chief of staff", "executive assistant", "executive business partner",
      "administrative"],
     "Executive support role, not a security owner."),
    (["cloud architect", "cloud engineer", "cloud finops",
      "cloud infrastructure manager"],
     "Cloud infrastructure role, outside security buying committee."),
    (["chief medical information officer", "cmio"],
     "Clinical informatics role, outside security buying committee."),
]

EXCL_STRATEGY = [
    (["innovation", "product strategy", "product development", "data strategy"],
     "Innovation or strategy role, outside security buying committee."),
]

CIO_TERMS = ["cio", "chief information officer"]
SOLUTION_ARCHITECT = ["solution architect", "solutions architect"]
VENDOR_INDUSTRY_HINTS = ["security", "software", "information technology",
                         "computer", "consulting", "internet", "saas"]
VENDOR_NAME_HINTS = ["security", "cyber", "technologies", "systems",
                     "solutions", "consulting", "partners", "mssp"]


def looks_like_vendor(company):
    """Best-effort vendor detection from company context (Step 3 / Step 4).

    Company data is context only. Hard constraint 3: it may never raise a tier.
    It is used solely to exclude and to set the seller flag.
    """
    if not company:
        return None  # unknown, not False — the caller routes these to residue
    blob = " ".join(filter(None, [
        (company.get("name") or "").lower(),
        (company.get("industry") or "").lower().replace("_", " "),
        (company.get("domain") or "").lower(),
    ]))
    if not blob.strip():
        return None
    if any(h in blob for h in VENDOR_INDUSTRY_HINTS + VENDOR_NAME_HINTS):
        return True
    return False


def apply_exclusions(t, tier, company):
    """Step 3. Returns (tier, reason, rule) or None to keep the Step 2 result.

    Exclusions only override when the role is genuinely a different job.
    """
    # CIO — but Step 1 outranks this. "CIO/CISO" classifies on the CISO half
    # and stays Tier 1, so the CIO exclusion never fires when ciso is present.
    # The guard is a security-term test, not a literal 'ciso' test: the dual
    # role is often spelled out in full ("Chief Information Officer & Chief
    # Information Security Officer"), where the acronym never appears.
    if has_word(t, CIO_TERMS) and not has(t, SECURITY):
        return ("out_of_scope", "CIO remit is IT, not security.", "S3-cio")

    # "Office of the CISO" — normalisation strips 'of'/'the', so this is a
    # co-occurrence test rather than a phrase match.
    if has_word(t, ["office"]) and "ciso" in t:
        return ("out_of_scope", "Office of the CISO role, not the security owner.", "S3-officeciso")

    # Executive Assistant excludes; Assistant Director of Information Security
    # does not. Both are word-boundary matched on the full phrase.
    for terms, reason in EXCL_NONSECURITY + EXCL_ADJACENT + EXCL_STRATEGY:
        # "Engineer" is not an exclusion: a security engineer is Tier 3. Only
        # non-security engineering is excluded, and those carry no security
        # term so Step 2 never placed them anyway.
        if has_word(t, terms):
            # "Advisor" and a sitting CISO: the CISO wins, seller flag handles it.
            if tier == "tier_1" and "ciso" in t and has_word(t, ["consultant", "consulting", "consultancy"]):
                continue
            return ("out_of_scope", reason, "S3-excl")

    # Solution Architect is vendor-side pre-sales unless the company clearly
    # is not a security vendor and the title otherwise reads as in-house.
    if has_word(t, SOLUTION_ARCHITECT):
        vendor = looks_like_vendor(company)
        if vendor is None:
            return ("RESIDUE", "Solution architect, no company context to judge vendor side.", "S3-sa-unknown")
        if vendor:
            return ("out_of_scope", "Pre-sales solution architect at security vendor.", "S3-sa")
        return None  # in-house, keep the Step 2 tier

    return None


# ==========================================================================
# Step 4 — Seller flag (independent of tier)
# ==========================================================================

SELLER_TITLE = ["sales", "business development", "account executive",
                "account manager", "channel", "reseller", "distributor",
                "partnerships", "alliance", "alliances", "pre sales",
                "presales", "sales engineer"]
SELLER_VENDOR_TITLE = ["solution architect", "solutions architect",
                       "sales engineer", "field ciso", "vciso", "fractional",
                       "advisor", "adviser"]
SELLER_COMPANY = ["reseller", "mssp", "systems integrator", "consultancy",
                  "consulting", "distributor"]


def flag_rests_on_evidence(t, company):
    """Step 4: "Default to No. Uncertainty is not evidence."

    A seller title is evidence on its own -- signal 1 needs no company data,
    and a vendor-side title like "Field CISO" names the vendor relationship
    in the title itself. Signals 2 and 3 rest on company context, so if there
    is no company context at all the flag rests on nothing and is dropped.
    """
    if has_word(t, SELLER_TITLE) or has_word(t, SELLER_VENDOR_TITLE):
        return True
    return bool(company and (company.get("domain") or company.get("name")
                             or company.get("industry")))


def seller_flag(t, company):
    """Step 4. Default to No. Uncertainty is not evidence."""
    if has_word(t, SELLER_TITLE):
        return True
    if has_word(t, SELLER_VENDOR_TITLE) and looks_like_vendor(company) is True:
        return True
    if company:
        blob = " ".join(filter(None, [
            (company.get("name") or "").lower(),
            (company.get("industry") or "").lower().replace("_", " "),
        ]))
        if any(h in blob for h in SELLER_COMPANY):
            return True
    return False


# ==========================================================================
# Cleanup rules C1-C3 — the only rules permitted to overwrite
# ==========================================================================

EXEC_SUPPORT = ["executive assistant", "executive business partner",
                "chief of staff", "administrative"]
VENDOR_CISO = ["field ciso", "vciso", "fractional", "advisor", "adviser",
               "consultant"]


def cleanup(t, current_tier):
    """C1-C3. Returns (tier, flag, reason, rule) or None."""
    # C1 — glued CISO titles L1's token-boundary segment missed.
    if "ciso" in t and current_tier is None:
        if has_word(t, EXEC_SUPPORT):
            return ("out_of_scope", False,
                    "Executive support role, not a security owner.", "C1-exception")
        if has_word(t, ["office"]):
            return ("out_of_scope", False,
                    "Office of the CISO role, not the security owner.", "C1-office")
        return None  # tier comes from Step 2, so Deputy/Regional CISO grade correctly

    # C3 before C2: both target existing tier_1, and a title carrying both
    # 'executive assistant' and 'consultant' must land out_of_scope rather
    # than as a seller-flagged tier_1. C3 is the stricter correction.
    if current_tier == "tier_1" and has_word(t, EXEC_SUPPORT[:2]):
        return ("out_of_scope", False,
                "Executive support role, not a security owner.", "C3")

    # C2 — vendor-side CISO. Tier is deliberately left alone; a genuine CISO
    # title at a vendor stays Tier 1 per the rubric. Only the flag is set.
    if current_tier == "tier_1" and has_word(t, VENDOR_CISO):
        return ("tier_1", True, "Vendor-side or advisory CISO role.", "C2")

    return None


# ==========================================================================
# Full pipeline for one record
# ==========================================================================

class Outcome:
    __slots__ = ("tier", "flag", "reason", "rule", "residue")

    def __init__(self, tier=None, flag=False, reason="", rule="", residue=None):
        self.tier = tier
        self.flag = flag
        self.reason = reason
        self.rule = rule
        self.residue = residue


def process(title_raw, current_tier, company, include_manager=False):
    t = normalise(title_raw)

    cl = cleanup(t, current_tier)
    if cl:
        tier, flag, reason, rule = cl
        return Outcome(tier, flag or seller_flag(t, company), reason, rule)

    # Anything already carrying a value and not caught by cleanup is left alone.
    if current_tier is not None:
        return Outcome()

    if not t:
        return Outcome("unknown", False, "Title blank, needs enrichment.", "S2-blank")

    if t in BARE_GENERIC:
        return Outcome(residue="Bare generic title with no qualifier")
    if len(t) < 4:
        return Outcome(residue="Title under 4 characters")

    placed = assign_tier(t, include_manager)
    tier = placed[0] if placed else "out_of_scope"
    reason = placed[1] if placed else "Role outside the security buying committee."
    rule = placed[2] if placed else "S2-catchall"

    # A bare architect title Step 2 could not place has no security term in it
    # at all. It is out of scope for that reason, not because it is vendor-side
    # -- the employer may be an ordinary enterprise. Say so accurately.
    if not placed and has(t, ARCHITECTURE):
        reason = "Architect role outside security remit."
        rule = "S2-arch-catchall"

    excl = apply_exclusions(t, tier, company)
    if excl:
        if excl[0] == "RESIDUE":
            return Outcome(residue=excl[1])
        tier, reason, rule = excl

    flag = seller_flag(t, company)

    # A security term present but nothing placed it is a judgment call.
    if not placed and has(t, SECURITY) and rule == "S2-catchall":
        return Outcome(residue="Security term present but matched no tier rule")

    return Outcome(tier, flag, reason, rule)


# ==========================================================================
# HubSpot client
# ==========================================================================

class HubSpot:
    def __init__(self, token, dry_run=True):
        self.token = token
        self.dry_run = dry_run

    def _request(self, method, path, payload=None, attempt=0):
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 5:
                wait = min(2 ** attempt, 16)
                sys.stderr.write("  %s from HubSpot, backing off %ss\n" % (e.code, wait))
                time.sleep(wait)
                return self._request(method, path, payload, attempt + 1)
            sys.stderr.write("HTTP %s on %s %s\n%s\n"
                             % (e.code, method, path, e.read().decode()[:800]))
            raise

    def search_contacts(self):
        out, after, page = [], None, 0
        while True:
            payload = {
                "filterGroups": [{"filters": [{
                    "propertyName": "lifecyclestage",
                    "operator": "EQ", "value": SCOPE_LIFECYCLE}]}],
                "properties": READ_PROPS, "limit": 100,
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
                sys.stderr.write("WARNING: hit the 10,000-result search ceiling.\n")
                break
            time.sleep(0.22)  # search endpoint is 5 req/s
        return out

    def company_context(self, contact_ids):
        """contact_id -> {name, domain, industry}. Best effort."""
        assoc = {}
        for i in range(0, len(contact_ids), 100):
            chunk = contact_ids[i:i + 100]
            res = self._request(
                "POST", "/crm/v4/associations/contacts/companies/batch/read",
                {"inputs": [{"id": c} for c in chunk]})
            for row in res.get("results", []):
                targets = row.get("to") or []
                if targets:
                    # toObjectId is an int; the companies batch keys by string.
                    # Mismatching these silently yields {} for every contact.
                    assoc[row["from"]["id"]] = str(targets[0]["toObjectId"])
            time.sleep(0.12)

        company_ids = sorted(set(assoc.values()))
        sys.stderr.write("  resolving %d companies...\n" % len(company_ids))
        companies = {}
        for i in range(0, len(company_ids), 100):
            chunk = company_ids[i:i + 100]
            res = self._request(
                "POST", "/crm/v3/objects/companies/batch/read",
                {"properties": COMPANY_PROPS,
                 "inputs": [{"id": str(c)} for c in chunk]})
            for row in res.get("results", []):
                companies[row["id"]] = row.get("properties", {})
            time.sleep(0.12)

        return {cid: companies.get(comp_id, {}) for cid, comp_id in assoc.items()}

    def batch_update(self, inputs):
        for start in range(0, len(inputs), 100):
            chunk = inputs[start:start + 100]
            for item in chunk:
                leaked = FORBIDDEN_PROPS & set(item["properties"])
                if leaked:
                    raise RuntimeError("Refusing to write forbidden properties: %s"
                                       % sorted(leaked))
            if self.dry_run:
                continue
            self._request("POST", "/crm/v3/objects/contacts/batch/update",
                          {"inputs": chunk})
            sys.stderr.write("  wrote %d/%d\n"
                             % (min(start + 100, len(inputs)), len(inputs)))
            time.sleep(0.12)


# ==========================================================================
# Reporting
# ==========================================================================

def preflight(contacts):
    counts = Counter()
    for c in contacts:
        counts[c["properties"].get(TIER_PROP) or None] += 1

    print("\nBaseline check — scope lifecyclestage = %s" % SCOPE_LIFECYCLE)
    print("  %-14s %8s %8s %8s" % ("tier", "found", "expected", "delta"))
    ok = True
    for tier, expected in EXPECTED_BASELINE.items():
        found = counts.get(tier, 0)
        if found != expected:
            ok = False
        print("  %-14s %8d %8d %+8d" % (tier or "(null)", found, expected, found - expected))
    total = sum(counts.values())
    print("  %-14s %8d %8d %+8d" % ("TOTAL", total, EXPECTED_TOTAL, total - EXPECTED_TOTAL))
    if total != EXPECTED_TOTAL:
        ok = False

    if not ok:
        print("\n  Baseline has DRIFTED. Someone has been editing in the UI, or")
        print("  L1-L4 have moved. C1-C3's assumptions may be stale.")
        print("  NOTE: the two source documents already disagree — the spec says")
        print("  8,356 and the classification document says 8,344, twice.")
    else:
        print("\n  Baseline matches the spec exactly.")
    return ok


def validate(rows, residue):
    proposed = Counter(r["proposed_tier"] for r in rows if r["proposed_tier"])
    rules = Counter(r["rule_fired"] for r in rows if r["rule_fired"])

    print("\nProposed changes by rule")
    for rule, n in sorted(rules.items(), key=lambda kv: -kv[1]):
        print("  %-16s %6d" % (rule, n))

    print("\nProposed tier distribution")
    for tier in ["tier_1", "tier_2", "tier_3", "out_of_scope", "unknown"]:
        print("  %-16s %6d" % (tier, proposed.get(tier, 0)))

    print("\nValidation against the manual phase")
    unknown = proposed.get("unknown", 0)
    drift = unknown - EXPECTED_UNKNOWN
    print("  unknown %d vs expected ~%d (delta %+d)%s"
          % (unknown, EXPECTED_UNKNOWN, drift,
             "" if abs(drift) <= 20 else "   <-- INVESTIGATE"))
    print("  tier_3 from this run: %d. The spec asks to compare this against the"
          % proposed.get("tier_3", 0))
    print("  ICP L3 segment of %d, but that check cannot run as written: L3 was"
          % EXPECTED_L3_SEGMENT)
    print("  applied by hand and is already on the records. The meaningful check")
    print("  is existing %d + this run's tier_3 = final tier_3." % EXPECTED_L3_SEGMENT)
    print("  %d records held back to %s — not written." % (len(residue), RESIDUE_CSV))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print("  wrote %s (%d rows)" % (path, len(rows)))


def load_graded(path, residue_ids):
    """Load hand-graded residue records and validate them hard.

    These bypass every rule in this file, so they get stricter checking than
    anything the rules produce: a typo here writes a bad tier with no rule to
    blame. Returns {id: (tier, flag, reason)}.
    """
    graded, problems = {}, []
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            cid = (row.get("hs_object_id") or "").strip()
            tier = (row.get(TIER_PROP) or "").strip().lower()
            flag = (row.get(FLAG_PROP) or "").strip().lower()
            reason = (row.get(REASON_PROP) or "").strip()

            if not cid:
                problems.append("line %d: no hs_object_id" % i)
                continue
            if cid in graded:
                problems.append("line %d: duplicate hs_object_id %s" % (i, cid))
                continue
            if cid not in residue_ids:
                problems.append("line %d: id %s is not in the residue set -- "
                                "grading a record the rules already decided" % (i, cid))
                continue
            if tier not in ALLOWED_TIERS:
                problems.append("line %d: tier %r not one of %s"
                                % (i, tier, sorted(ALLOWED_TIERS)))
                continue
            if flag not in ("true", "false"):
                problems.append("line %d: seller flag %r is not true/false" % (i, flag))
                continue
            if not reason:
                problems.append("line %d: empty reason" % i)
                continue
            if len(reason.split()) > 15:
                problems.append("line %d: reason is %d words, limit is 15"
                                % (i, len(reason.split())))
                continue
            graded[cid] = (tier, flag == "true", reason)

    if problems:
        sys.stderr.write("\nGraded file rejected -- %d problem(s):\n" % len(problems))
        for p in problems[:20]:
            sys.stderr.write("  %s\n" % p)
        sys.exit("Refusing to write. Fix %s and re-run." % path)

    ungraded = residue_ids - set(graded)
    if ungraded:
        print("  NOTE: %d residue records are not in the graded file and will "
              "stay unwritten." % len(ungraded))
    return graded


def rollback(client, path):
    inputs = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            inputs.append({"id": row["hs_object_id"], "properties": {
                TIER_PROP: row.get(TIER_PROP, "") or "",
                FLAG_PROP: row.get(FLAG_PROP, "") or "false",
                REASON_PROP: row.get(REASON_PROP, "") or "",
            }})
    print("Restoring %d records from %s" % (len(inputs), path))
    client.batch_update(inputs)
    print("Rollback complete.")


# ==========================================================================

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="apply changes to the CRM (default is dry run)")
    ap.add_argument("--rollback", metavar="FILE",
                    help="restore the three properties from a before-file")
    ap.add_argument("--exclude-manager", action="store_true",
                    help="revert the resolved manager decision: drop 'manager' "
                         "from the Tier 3 rule, sending security managers to "
                         "residue instead of tier_3")
    ap.add_argument("--no-company", action="store_true",
                    help="skip company enrichment; solution-architect and "
                         "vendor-seller cases then go to residue")
    args = ap.parse_args()

    token = (os.environ.get("HUBSPOT_TOKEN")
             or os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN"))
    if not token:
        sys.exit("Set HUBSPOT_TOKEN or HUBSPOT_PRIVATE_APP_TOKEN "
                 "(portal %s, contacts read+write)." % PORTAL_ID)

    client = HubSpot(token, dry_run=not args.write)

    if args.rollback:
        if not args.write:
            sys.exit("--rollback also needs --write. It is a write operation.")
        return rollback(client, args.rollback)

    print("Fetching MQL population from portal %s..." % PORTAL_ID)
    contacts = client.search_contacts()
    print("  %d contacts in scope" % len(contacts))

    baseline_ok = preflight(contacts)

    companies = {}
    if not args.no_company:
        print("\nFetching company context (Step 3 and Step 4 need it)...")
        candidates = [c["id"] for c in contacts
                      if (c["properties"].get(TIER_PROP) or None) in (None, "tier_1")]
        print("  %d records need company context (unclassified + tier_1 cleanup)"
              % len(candidates))
        companies = client.company_context(candidates)
        print("  company context for %d of %d contacts" % (len(companies), len(contacts)))
    else:
        print("\n  --no-company: seller detection is title-only; solution")
        print("  architects and vendor-ambiguous titles go to residue.")

    include_manager = not args.exclude_manager
    if include_manager:
        print("\n  RESOLVED: security managers grade tier_3 — they influence and")
        print("  evaluate without owning budget, which is what tier_3 is for.")
    else:
        print("\n  --exclude-manager: reverting the resolved decision; security")
        print("  managers go to residue rather than tier_3.")

    rows, residue, before, updates = [], [], [], []
    originals, dropped_flags = {}, 0

    for c in contacts:
        props = c["properties"]
        cid = c["id"]
        title = props.get("jobtitle") or ""
        current = props.get(TIER_PROP) or None
        company = companies.get(cid) or {}

        out = process(title, current, company, include_manager)
        if out.tier is None and out.residue is None:
            continue

        # Step 4 guard, applied before anything is recorded.
        if out.flag and not flag_rests_on_evidence(normalise(title), company):
            out.flag = False
            dropped_flags += 1

        originals[cid] = props

        rows.append({
            "hs_object_id": cid,
            "email": props.get("email") or "",
            "jobtitle": title,
            "company_domain": company.get("domain") or "",
            "current_tier": current or "",
            "proposed_tier": "" if out.residue else out.tier,
            "proposed_seller_flag": "" if out.residue else str(out.flag).lower(),
            "proposed_reason": "" if out.residue else out.reason,
            "rule_fired": "RESIDUE" if out.residue else out.rule,
        })

        if out.residue:
            residue.append({
                "hs_object_id": cid,
                "email": props.get("email") or "",
                "jobtitle": title,
                "company_domain": company.get("domain") or "",
                "current_tier": current or "",
                "residue_reason": out.residue,
            })
            continue

        before.append({
            "hs_object_id": cid,
            TIER_PROP: props.get(TIER_PROP) or "",
            FLAG_PROP: props.get(FLAG_PROP) or "",
            REASON_PROP: props.get(REASON_PROP) or "",
        })
        updates.append({"id": cid, "properties": {
            TIER_PROP: out.tier,
            FLAG_PROP: "true" if out.flag else "false",
            REASON_PROP: out.reason,
        }})

    print("\nWriting output files")
    write_csv(DRYRUN_CSV,
              ["hs_object_id", "email", "jobtitle", "company_domain",
               "current_tier", "proposed_tier", "proposed_seller_flag",
               "proposed_reason", "rule_fired"], rows)
    write_csv(RESIDUE_CSV,
              ["hs_object_id", "email", "jobtitle", "company_domain",
               "current_tier", "residue_reason"], residue)

    residue_ids = {r["hs_object_id"] for r in residue}
    graded = {}
    # Prefer a repaired grading file when one exists. Spreadsheet round-trips
    # turn 12-digit contact ids into scientific notation ("1.10638E+11"),
    # which silently truncates them to 6 significant figures.
    graded_path = (GRADED_FIXED_CSV if os.path.exists(GRADED_FIXED_CSV)
                   else GRADED_CSV)
    if os.path.exists(graded_path):
        print("\nMerging hand-graded residue from %s" % graded_path)
        graded = load_graded(graded_path, residue_ids)
        for cid, (tier, flag, reason) in graded.items():
            props = originals.get(cid, {})
            before.append({
                "hs_object_id": cid,
                TIER_PROP: props.get(TIER_PROP) or "",
                FLAG_PROP: props.get(FLAG_PROP) or "",
                REASON_PROP: props.get(REASON_PROP) or "",
            })
            updates.append({"id": cid, "properties": {
                TIER_PROP: tier,
                FLAG_PROP: "true" if flag else "false",
                REASON_PROP: reason,
            }})
        print("  %d graded records merged into the write set" % len(graded))
    else:
        print("\n  No %s found -- the %d residue records stay unwritten."
              % (GRADED_CSV, len(residue)))

    # A record must never be written twice. The rule set and the graded file
    # are disjoint by construction (graded ids must be residue ids, and
    # residue ids are excluded from updates), but assert it rather than trust it.
    ids = [u["id"] for u in updates]
    if len(set(ids)) != len(ids):
        dupes = [i for i, n in Counter(ids).items() if n > 1]
        sys.exit("ABORT: %d id(s) appear twice in the write set: %s"
                 % (len(dupes), dupes[:10]))

    if dropped_flags:
        print("\n  Step 4 guard: dropped %d seller flag(s) resting on no "
              "evidence." % dropped_flags)

    write_csv(BEFORE_CSV, ["hs_object_id"] + WRITTEN_PROPS, before)

    validate(rows, residue)

    if not args.write:
        print("\nDRY RUN — nothing was written. %d records would change." % len(updates))
        print("Review %s, then re-run with --write." % DRYRUN_CSV)
        return

    if not baseline_ok:
        sys.exit("\nRefusing to write: baseline count does not match the spec.")

    print("\nWriting %d records to portal %s..." % (len(updates), PORTAL_ID))
    client.batch_update(updates)
    print("Done. Rollback with: --write --rollback %s" % BEFORE_CSV)


if __name__ == "__main__":
    main()
