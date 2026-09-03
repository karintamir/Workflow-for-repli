#!/usr/bin/env python3
"""
hs_audit.py — HubSpot portal audit helper for the MCP-blind areas.

The HubSpot MCP connector exposes CRM objects (contacts, companies, deals,
properties, owners, lists) but NOT: workflows/automation, email send
statistics, subscription types, sender authentication (DKIM/SPF/domains),
or forms. This script fills that gap by calling the HubSpot public APIs
directly, and emits a structured JSON + human-readable report that feeds
audit Areas 1 (sender auth), 5 (Workflows & Automation), and 7 (Marketing
Hub email health).

Auth: a HubSpot Private App access token with (at minimum) these scopes:
    automation                     -> workflows
    content                        -> forms, domains
    marketing-email                -> email sends & statistics
    communication_preferences.read -> subscription types
    crm.schemas.*                  -> (optional) schema context

Usage:
    export HUBSPOT_TOKEN="pat-eu1-xxxxxxxx"          # private app token
    python3 hs_audit.py --portal wisestamp --out ./audit_out

    # Or route through the Flask OAuth proxy in hubspot_gpt_proxy/ instead
    # of a private-app token:
    export HUBSPOT_PROXY_BASE="https://<your-repl>.repl.co"
    export HUBSPOT_ACCESS_TOKEN="Bearer CI..."       # OAuth access token
    python3 hs_audit.py --portal wisestamp --via-proxy

The script never fabricates data. If a scope is missing or an endpoint is
unauthorized, the corresponding section is marked "unavailable" with the
HTTP status so the audit can score it ⚪ UNKNOWN rather than a hollow value.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("This script requires the 'requests' package: pip install requests")

API_BASE = "https://api.hubapi.com"
STALE_DAYS = 90
OLD_LOGIC_DAYS = 365


def _now():
    return datetime.now(timezone.utc)


def _age_days(ts_millis):
    """Age in days from an epoch-millis timestamp (int or ISO string)."""
    if ts_millis is None:
        return None
    try:
        if isinstance(ts_millis, str):
            # ISO 8601
            dt = datetime.fromisoformat(ts_millis.replace("Z", "+00:00"))
        else:
            dt = datetime.fromtimestamp(ts_millis / 1000, tz=timezone.utc)
        return (_now() - dt).days
    except (ValueError, OSError, TypeError):
        return None


class HubSpotClient:
    def __init__(self, token=None, proxy_base=None, access_token=None):
        self.proxy_base = proxy_base.rstrip("/") if proxy_base else None
        self.session = requests.Session()
        if self.proxy_base:
            # Route through the Flask proxy (hubspot_gpt_proxy/main.py).
            # The proxy expects the OAuth token in the Authorization header
            # and rewrites api.hubapi.com/<path> -> /hubspot/<path>.
            self.auth_header = access_token
        else:
            self.auth_header = f"Bearer {token}"

    def get(self, path, params=None):
        if self.proxy_base:
            url = f"{self.proxy_base}/hubspot/{path.lstrip('/')}"
        else:
            url = f"{API_BASE}/{path.lstrip('/')}"
        headers = {"Authorization": self.auth_header, "Content-Type": "application/json"}
        for attempt in range(4):
            try:
                r = self.session.get(url, headers=headers, params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == 3:
                    return None, -1, str(e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:  # rate limited
                time.sleep(2 ** attempt)
                continue
            try:
                body = r.json()
            except ValueError:
                body = r.text
            return body, r.status_code, None
        return None, -1, "retries exhausted"

    def paginate(self, path, params=None, results_key="results", limit=100):
        """Follow HubSpot v3 paging (paging.next.after) and collect results."""
        params = dict(params or {})
        params.setdefault("limit", limit)
        out = []
        after = None
        while True:
            if after:
                params["after"] = after
            body, status, err = self.get(path, params)
            if status != 200 or not isinstance(body, dict):
                return out, status, err or f"HTTP {status}"
            out.extend(body.get(results_key, []))
            after = (body.get("paging", {}) or {}).get("next", {}).get("after")
            if not after:
                break
        return out, 200, None


# ---------------------------------------------------------------------------
# Area 5 — Workflows & Automation
# ---------------------------------------------------------------------------
def audit_workflows(client):
    section = {"available": False, "source": "automation/v3/workflows"}
    body, status, err = client.get("automation/v3/workflows")
    if status != 200 or not isinstance(body, dict):
        section["error"] = f"HTTP {status}: {err or 'unauthorized — check the automation scope'}"
        return section

    workflows = body.get("workflows", [])
    section["available"] = True
    section["total"] = len(workflows)

    on = [w for w in workflows if w.get("enabled")]
    off = [w for w in workflows if not w.get("enabled")]
    stale_on, old_logic, unnamed = [], [], []
    by_type = {}
    inventory = []
    # Hygiene-critical property targets we care about
    HYGIENE_PROPS = ("lifecyclestage", "hs_lead_status", "hubspot_owner_id")
    props_written_by = {}  # property name -> [workflow names] (redundancy/conflict detection)

    vague_tokens = ("workflow ", "test", "copy of", "untitled", "new workflow", "temp")
    for w in workflows:
        wtype = w.get("type", "UNKNOWN")
        by_type[wtype] = by_type.get(wtype, 0) + 1
        name = (w.get("name") or "").strip()
        low = name.lower()
        if not name or any(t in low for t in vague_tokens):
            unnamed.append(name or f"(id {w.get('id')})")
        updated_age = _age_days(w.get("updatedAt") or w.get("migrationTimestamp"))
        if w.get("enabled") and updated_age is not None and updated_age > OLD_LOGIC_DAYS:
            old_logic.append({"name": name, "days_since_modified": updated_age})

        # Pull the full definition to see actions, enrollment, re-enrollment
        action_types = {}
        props_set = []
        detail, s2, _ = client.get(f"automation/v3/workflows/{w.get('id')}")
        re_enroll = None
        only_manual = None
        if s2 == 200 and isinstance(detail, dict):
            last_activity = _age_days(detail.get("updatedAt"))
            if w.get("enabled") and last_activity is not None and last_activity > STALE_DAYS:
                stale_on.append({"name": name, "days_since_activity": last_activity})
            only_manual = detail.get("onlyEnrollsManually")
            re_enroll = bool(detail.get("reEnrollmentTriggerSets"))
            for a in detail.get("actions", []) or []:
                at = a.get("type", "UNKNOWN")
                action_types[at] = action_types.get(at, 0) + 1
                pn = a.get("propertyName")
                if pn:
                    props_set.append(pn)
                    props_written_by.setdefault(pn, []).append(name)

        inventory.append({
            "id": w.get("id"),
            "name": name,
            "enabled": w.get("enabled"),
            "action_types": action_types,
            "properties_set": sorted(set(props_set)),
            "sets_hygiene_props": sorted(set(p for p in props_set if p in HYGIENE_PROPS)),
            "re_enrollment": re_enroll,
            "only_enrolls_manually": only_manual,
            "days_since_modified": updated_age,
        })

    # Which hygiene-critical automations exist anywhere?
    hygiene_coverage = {
        p: sorted(set(wf["name"] for wf in inventory if p in wf["properties_set"]))
        for p in HYGIENE_PROPS
    }
    # Properties written by more than one workflow = potential race/overwrite
    conflicts = {p: v for p, v in props_written_by.items() if len(set(v)) > 1}

    section["counts"] = {
        "on": len(on),
        "off": len(off),
        "stale_on_90d": len(stale_on),
        "old_logic_365d": len(old_logic),
        "unnamed_or_vague": len(unnamed),
    }
    section["by_type"] = by_type
    section["inventory"] = inventory
    section["hygiene_coverage"] = hygiene_coverage
    section["property_write_conflicts"] = conflicts
    section["stale_on"] = stale_on[:50]
    section["old_logic"] = old_logic[:50]
    section["unnamed"] = unnamed[:50]
    section["note"] = (
        "Per-workflow error state is not exposed by the public v3 API; confirm "
        "error banners in the HubSpot UI (Automation > Workflows > 'Needs review'). "
        "hygiene_coverage shows which workflows (if any) set lifecycle stage, lead "
        "status, or owner — empty lists mean that automation is absent."
    )
    return section


# ---------------------------------------------------------------------------
# Area 1 — Sender authentication & connected domains
# ---------------------------------------------------------------------------
def audit_sender_auth(client):
    section = {"available": False}
    domains, status, err = client.paginate("cms/v3/domains")
    if status != 200:
        section["error"] = f"HTTP {status}: {err or 'unauthorized — check the content scope'}"
        return section
    section["available"] = True
    section["connected_domains"] = [
        {
            "domain": d.get("domain"),
            "is_email": d.get("isUsedForEmail"),
            "is_primary": d.get("isPrimary"),
            "dns_correct": d.get("isDnsCorrect"),
            "ssl_enabled": d.get("isSslEnabled"),
        }
        for d in domains
    ]
    email_domains = [d for d in domains if d.get("isUsedForEmail")]
    section["email_sending_domain_count"] = len(email_domains)
    section["unverified_dns"] = [d.get("domain") for d in domains if not d.get("isDnsCorrect")]
    return section


# ---------------------------------------------------------------------------
# Area 7 — Marketing email stats, subscription types, forms
# ---------------------------------------------------------------------------
def audit_email_stats(client):
    section = {"available": False, "source": "marketing/v3/emails"}
    emails, status, err = client.paginate(
        "marketing/v3/emails", params={"limit": 50, "includeStats": "true"}
    )
    if status != 200:
        section["error"] = f"HTTP {status}: {err or 'unauthorized — check the marketing-email scope'}"
        return section
    section["available"] = True
    section["total_emails"] = len(emails)
    sends = opens = clicks = bounces = delivered = 0
    for e in emails:
        stats = (e.get("stats", {}) or {}).get("counters", {}) or {}
        sends += stats.get("sent", 0)
        delivered += stats.get("delivered", 0)
        opens += stats.get("open", 0)
        clicks += stats.get("click", 0)
        bounces += stats.get("bounce", 0)
    section["aggregate"] = {
        "sent": sends,
        "delivered": delivered,
        "open_rate": round(opens / delivered, 4) if delivered else None,
        "click_to_open": round(clicks / opens, 4) if opens else None,
        "bounce_rate": round(bounces / sends, 4) if sends else None,
    }
    return section


def audit_subscriptions(client):
    section = {"available": False, "source": "communication-preferences/v3/definitions"}
    body, status, err = client.get("communication-preferences/v3/definitions")
    if status != 200 or not isinstance(body, dict):
        section["error"] = f"HTTP {status}: {err or 'unauthorized — check communication_preferences.read'}"
        return section
    subs = body.get("subscriptionDefinitions", [])
    section["available"] = True
    section["count"] = len(subs)
    section["types"] = [{"name": s.get("name"), "id": s.get("id")} for s in subs]
    section["default_only"] = len(subs) <= 1
    return section


def audit_forms(client):
    section = {"available": False, "source": "marketing/v3/forms"}
    forms, status, err = client.paginate("marketing/v3/forms")
    if status != 200:
        section["error"] = f"HTTP {status}: {err or 'unauthorized — check the forms/content scope'}"
        return section
    section["available"] = True
    section["total"] = len(forms)
    archived = [f for f in forms if f.get("archived")]
    section["active"] = len(forms) - len(archived)
    section["archived"] = len(archived)
    return section


def main():
    ap = argparse.ArgumentParser(description="HubSpot MCP-blind audit helper")
    ap.add_argument("--portal", default="portal", help="label for output files")
    ap.add_argument("--out", default="./audit_out", help="output directory")
    ap.add_argument("--via-proxy", action="store_true", help="route through the Flask OAuth proxy")
    args = ap.parse_args()

    proxy_base = os.getenv("HUBSPOT_PROXY_BASE") if args.via_proxy else None
    token = os.getenv("HUBSPOT_TOKEN")
    access_token = os.getenv("HUBSPOT_ACCESS_TOKEN")

    if args.via_proxy and not (proxy_base and access_token):
        sys.exit("--via-proxy requires HUBSPOT_PROXY_BASE and HUBSPOT_ACCESS_TOKEN")
    if not args.via_proxy and not token:
        sys.exit("Set HUBSPOT_TOKEN to a HubSpot Private App access token (or use --via-proxy)")

    client = HubSpotClient(token=token, proxy_base=proxy_base, access_token=access_token)

    report = {
        "portal": args.portal,
        "generated_at": _now().isoformat(),
        "area_1_sender_auth": audit_sender_auth(client),
        "area_5_workflows": audit_workflows(client),
        "area_7_email_stats": audit_email_stats(client),
        "area_7_subscriptions": audit_subscriptions(client),
        "area_7_forms": audit_forms(client),
    }

    os.makedirs(args.out, exist_ok=True)
    json_path = os.path.join(args.out, f"{args.portal}_hs_audit.json")
    with open(json_path, "w") as fh:
        json.dump(report, fh, indent=2)

    # Human-readable summary to stdout
    print(f"\n=== HubSpot MCP-blind audit — {args.portal} ===")
    print(f"generated_at: {report['generated_at']}\n")
    for key, sec in report.items():
        if not isinstance(sec, dict):
            continue
        status = "OK" if sec.get("available") else f"UNAVAILABLE ({sec.get('error','')})"
        print(f"[{key}] {status}")
        for k, v in sec.items():
            if k in ("available", "error", "source", "note", "inventory"):
                continue
            print(f"    {k}: {v}")
        # Pretty-print workflow inventory (names + what they write)
        if key == "area_5_workflows" and sec.get("inventory"):
            print("    --- workflow inventory ---")
            for wf in sec["inventory"]:
                state = "ON " if wf["enabled"] else "OFF"
                acts = ",".join(f"{a}x{n}" for a, n in wf["action_types"].items()) or "(none)"
                hyg = ",".join(wf["sets_hygiene_props"]) or "-"
                print(f"    [{state}] {wf['name']}")
                print(f"           actions: {acts}")
                print(f"           sets: {','.join(wf['properties_set']) or '(no property writes)'}")
                print(f"           hygiene-props set: {hyg} | re-enroll: {wf['re_enrollment']}")
        print()
    print(f"Full JSON written to: {json_path}")


if __name__ == "__main__":
    main()
