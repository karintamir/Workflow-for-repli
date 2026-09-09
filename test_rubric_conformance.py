"""Offline check of every named example in icp_title_rubric.md.
Pure functions only -- no network, no HubSpot."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ovalix_icp_classify import process

VENDOR = {"name": "Acme Security Ltd", "industry": "COMPUTER_NETWORK_SECURITY", "domain": "acme-sec.com"}
INHOUSE = {"name": "Northwind Bank", "industry": "BANKING", "domain": "northwind.com"}

CASES = [
    # (title, expected_tier, company, label)
    # ---- Tier 1 examples
    ("CISO", "tier_1", INHOUSE, "T1"),
    ("Global CISO", "tier_1", INHOUSE, "T1"),
    ("Group CISO", "tier_1", INHOUSE, "T1"),
    ("Chief Information Security Officer", "tier_1", INHOUSE, "T1"),
    ("Chief Cybersecurity Officer", "tier_1", INHOUSE, "T1"),
    ("Chief Cyber Officer", "tier_1", INHOUSE, "T1"),
    ("Chief Privacy Officer", "tier_1", INHOUSE, "T1"),
    ("Chief Data Officer", "tier_1", INHOUSE, "T1"),
    ("Chief AI Officer", "tier_1", INHOUSE, "T1"),
    ("SVP Information Security", "tier_1", INHOUSE, "T1"),
    ("SVP Cyber Risk", "tier_1", INHOUSE, "T1"),
    ("VP Cybersecurity", "tier_1", INHOUSE, "T1"),
    ("VP Information Security & Risk", "tier_1", INHOUSE, "T1"),
    ("VP AI Governance", "tier_1", INHOUSE, "T1"),
    ("Vice President, Chief Information Security Officer", "tier_1", INHOUSE, "T1"),
    # ---- Tier 2 examples
    ("Deputy CISO", "tier_2", INHOUSE, "T2"),
    ("Director of Information Security", "tier_2", INHOUSE, "T2"),
    ("Senior Director, Cybersecurity", "tier_2", INHOUSE, "T2"),
    ("Head of Information Security", "tier_2", INHOUSE, "T2"),
    ("Head of Cyber Security", "tier_2", INHOUSE, "T2"),
    ("Head of Data Governance", "tier_2", INHOUSE, "T2"),
    ("Head of AI Governance", "tier_2", INHOUSE, "T2"),
    ("Head of Data Protection", "tier_2", INHOUSE, "T2"),
    ("Head of Privacy", "tier_2", INHOUSE, "T2"),
    ("Head of GRC", "tier_2", INHOUSE, "T2"),
    ("Director of Governance, Risk & Compliance", "tier_2", INHOUSE, "T2"),
    ("Director of AI Security", "tier_2", INHOUSE, "T2"),
    ("Director of Data Protection", "tier_2", INHOUSE, "T2"),
    ("Head of AI Risk", "tier_2", INHOUSE, "T2"),
    ("AI Governance Lead", "tier_2", INHOUSE, "T2"),
    ("AI Security Lead", "tier_2", INHOUSE, "T2"),
    ("GenAI Security Lead", "tier_2", INHOUSE, "T2"),
    ("Agentic AI Security Lead", "tier_2", INHOUSE, "T2"),
    ("Business Information Security Officer (BISO)", "tier_2", INHOUSE, "T2"),
    ("Information Security Officer, Head of AI", "tier_2", INHOUSE, "T2"),
    # ---- Tier 3 examples
    ("Information Security Architect", "tier_3", INHOUSE, "T3"),
    ("Cyber Security Architect", "tier_3", INHOUSE, "T3"),
    ("Enterprise Security Architect", "tier_3", INHOUSE, "T3"),
    ("Principal Security Architect", "tier_3", INHOUSE, "T3"),
    ("Staff Security Architect", "tier_3", INHOUSE, "T3"),
    ("Senior Security Architect", "tier_3", INHOUSE, "T3"),
    ("Lead Information Security Architect", "tier_3", INHOUSE, "T3"),
    ("Lead Cybersecurity Architect", "tier_3", INHOUSE, "T3"),
    ("Director of Security Architecture", "tier_3", INHOUSE, "T3"),
    ("AI Security Architect", "tier_3", INHOUSE, "T3"),
    ("AI Governance Architect", "tier_3", INHOUSE, "T3"),
    ("Enterprise AI Security Architect", "tier_3", INHOUSE, "T3"),
    ("Senior Manager, Information Security", "tier_3", INHOUSE, "T3"),
    ("Senior Manager, Cybersecurity", "tier_3", INHOUSE, "T3"),
    ("Senior Manager, AI Governance", "tier_3", INHOUSE, "T3"),
    ("Cybersecurity Engineer", "tier_3", INHOUSE, "T3"),
    # ---- Step 1 dual-role rule
    ("Chief Information Officer & Chief Information Security Officer", "tier_1", INHOUSE, "Step1"),
    ("CIO/CISO", "tier_1", INHOUSE, "Step1"),
    ("CIO & CISO", "tier_1", INHOUSE, "Step1"),
    ("Director, Information Security", "tier_2", INHOUSE, "Step1"),
    ("Director Information Security", "tier_2", INHOUSE, "Step1"),
    ("VP & CISO", "tier_1", INHOUSE, "Step1"),
    ("VP and CISO", "tier_1", INHOUSE, "Step1"),
    ("Cyber-Security Architect", "tier_3", INHOUSE, "Step1"),
    ("CAIO", "tier_1", INHOUSE, "Step1-acronym"),
    ("Chief Information Security Officer (CISO)", "tier_1", INHOUSE, "Step1-paren"),
    # ---- Step 3 substring overrules
    ("Associate Vice President, Information Security", "tier_2", INHOUSE, "S3-no-soc-in-associate"),
    ("Associate VP, Information Security", "tier_2", INHOUSE, "S3-no-soc-in-associate"),
    ("Assistant Director of Information Security", "tier_2", INHOUSE, "S3-assistant-ok"),
    ("Executive Assistant", "out_of_scope", INHOUSE, "S3-exec-assistant"),
    ("Cloud Engineer", "out_of_scope", INHOUSE, "S3-nonsec-engineer"),
    ("Software Engineer", "out_of_scope", INHOUSE, "S3-nonsec-engineer"),
    ("SOC Analyst", "out_of_scope", INHOUSE, "S3-soc"),
    # ---- Step 3 excluded adjacent domains
    ("Director, Cloud Security", "out_of_scope", INHOUSE, "S3-adjacent"),
    ("Head of Identity and Access Management", "out_of_scope", INHOUSE, "S3-adjacent"),
    ("Director, Application Security", "out_of_scope", INHOUSE, "S3-adjacent"),
    ("VP, AppSec", "out_of_scope", INHOUSE, "S3-adjacent"),
    ("Director of Security Audit", "out_of_scope", INHOUSE, "S3-adjacent"),
    ("Director, Cybersecurity Alliances", "out_of_scope", INHOUSE, "S3-adjacent"),
    # ---- Step 3 excluded non-security
    ("Chief Information Officer", "out_of_scope", INHOUSE, "S3-cio"),
    ("Deputy CIO", "out_of_scope", INHOUSE, "S3-cio"),
    ("Global CIO", "out_of_scope", INHOUSE, "S3-cio"),
    ("SVP & CIO", "out_of_scope", INHOUSE, "S3-cio"),
    ("Office of the CISO", "out_of_scope", INHOUSE, "S3-officeciso"),
    ("Chief of Staff", "out_of_scope", INHOUSE, "S3-nonsec"),
    ("Cloud Architect", "out_of_scope", INHOUSE, "S3-nonsec"),
    ("Chief Medical Information Officer", "out_of_scope", INHOUSE, "S3-nonsec"),
    ("Director, Security Consulting", "out_of_scope", VENDOR, "S3-nonsec"),
    # ---- Step 3 strategy/innovation
    ("Head of Innovation", "out_of_scope", INHOUSE, "S3-strategy"),
    ("Head of AI and Innovation", "out_of_scope", INHOUSE, "S3-strategy"),
    ("Global Head of Data Strategy", "out_of_scope", INHOUSE, "S3-strategy"),
    # ---- Solution architect distinction
    ("Cybersecurity Solution Architect", "out_of_scope", VENDOR, "S3-sa-vendor"),
    ("Enterprise Security Architect", "tier_3", INHOUSE, "S3-sa-inhouse"),
    # ---- Unknown
    ("", "unknown", INHOUSE, "Unknown-blank"),
    (None, "unknown", INHOUSE, "Unknown-blank"),
]

SELLER_CASES = [
    ("VP of Sales", True, INHOUSE, "S4-title"),
    ("Business Development Manager", True, INHOUSE, "S4-title"),
    ("Channel Account Manager", True, INHOUSE, "S4-title"),
    ("Cybersecurity Solution Architect", True, VENDOR, "S4-vendor-sa"),
    ("Director of Information Security", False, INHOUSE, "S4-default-no"),
    # CONFIRMED DECISION: narrow reading. Step 4's "a Tier 1 title at a
    # competing vendor is still Tier 1 with seller_flag: Yes" is NOT fired on
    # company industry alone -- doing so would flag every CISO at any security
    # or software company and remove real buyers from marketing. The flag
    # fires on vendor-side TITLES (field CISO, vCISO, fractional, advisor)
    # per spec C2, and on explicit reseller/MSSP/SI/consultancy companies.
    ("CISO", False, VENDOR, "S4-tier1-at-vendor-narrow"),
    ("Field CISO", True, VENDOR, "S4-vendor-side-title"),
    ("Virtual CISO (vCISO)", True, VENDOR, "S4-vendor-side-title"),
]

RESIDUE_CASES = ["Director", "Manager", "Consultant", "Analyst", "IT"]

fails = []
for title, expected, company, label in CASES:
    out = process(title, None, company)
    got = out.residue and "RESIDUE" or out.tier
    if got != expected:
        fails.append("  %-14s %-52r got %-14s want %s" % (label, title, got, expected))

print("TIER: %d/%d passed" % (len(CASES) - len(fails), len(CASES)))
for f in fails:
    print(f)

sfails = []
for title, expected, company, label in SELLER_CASES:
    out = process(title, None, company)
    if out.flag != expected:
        sfails.append("  %-18s %-42r seller_flag=%s want %s" % (label, title, out.flag, expected))
print("\nSELLER: %d/%d passed" % (len(SELLER_CASES) - len(sfails), len(SELLER_CASES)))
for f in sfails:
    print(f)

rfails = []
for title in RESIDUE_CASES:
    out = process(title, None, INHOUSE)
    if not out.residue:
        rfails.append("  %-14r not routed to residue (tier=%s)" % (title, out.tier))
print("\nRESIDUE: %d/%d passed" % (len(RESIDUE_CASES) - len(rfails), len(RESIDUE_CASES)))
for f in rfails:
    print(f)

# Reason length constraint: 15 words or fewer
long = []
for title, _, company, _ in CASES:
    out = process(title, None, company)
    if out.reason and len(out.reason.split()) > 15:
        long.append((title, out.reason))
print("\nREASON <=15 words: %s" % ("OK" if not long else long))

print("\nTOTAL FAILURES: %d" % (len(fails) + len(sfails) + len(rfails)))
