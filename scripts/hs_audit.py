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

Area 5 uses GET /automation/v4/flows (public BETA) plus a batch detail read.
It previously used GET /automation/v3/workflows, which is legacy and returns
only older contact-based workflows: on one portal it reported 5 workflows
where 20 existed, and the audit wrongly concluded that automation did not
govern the CRM. The pull now carries a sanity guard that marks Area 5
unavailable rather than scoring a truncated result.

Note: no HubSpot API exposes a workflow's LAST-RUN time. `updatedAt` is the
last edit. Do not present it as evidence that a workflow has or has not
fired — confirm activity in the HubSpot UI.

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

    def post(self, path, body):
        if self.proxy_base:
            url = f"{self.proxy_base}/hubspot/{path.lstrip('/')}"
        else:
            url = f"{API_BASE}/{path.lstrip('/')}"
        headers = {"Authorization": self.auth_header, "Content-Type": "application/json"}
        for attempt in range(4):
            try:
                r = self.session.post(url, headers=headers, json=body, timeout=30)
            except requests.RequestException as e:
                if attempt == 3:
                    return None, -1, str(e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            try:
                payload = r.json()
            except ValueError:
                payload = r.text
            return payload, r.status_code, None
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
SET_PROPERTY_ACTION = "0-5"          # actionTypeId for "set property"
HYGIENE_PROPS = {
    "lifecyclestage": "Sets Lifecycle Stage",
    "hs_lead_status": "Sets Lead Status",
    "hubspot_owner_id": "Assigns Owner",
}
OBJECT_LABELS = {"0-1": "Contact", "0-2": "Company", "0-3": "Deal", "0-5": "Ticket"}


class WorkflowPullError(RuntimeError):
    pass


def _list_flows(client):
    """GET /automation/v4/flows — every workflow, key fields only."""
    out, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        body, status, err = client.get("automation/v4/flows", params)
        if status != 200 or not isinstance(body, dict):
            return out, status, err or f"HTTP {status}"
        out.extend(body.get("results", []))
        after = (body.get("paging", {}) or {}).get("next", {}).get("after")
        if not after:
            return out, 200, None


def _flow_details(client, flow_ids, chunk=50):
    """POST /automation/v4/flows/batch/read — full spec per workflow.

    Partial failures come back inside the payload, not as an HTTP error, so
    they are collected rather than raised. Flows holding sensitive-data
    properties can fail individually without additional scopes.
    """
    details, failed = [], []
    for i in range(0, len(flow_ids), chunk):
        batch = flow_ids[i:i + chunk]
        body = {"inputs": [{"flowId": str(f), "type": "FLOW_ID"} for f in batch]}
        payload, status, err = client.post("automation/v4/flows/batch/read", body)
        if status != 200 or not isinstance(payload, dict):
            failed.append({"batch_start": i, "error": err or f"HTTP {status}"})
            continue
        details.extend(payload.get("results", []))
        for e in payload.get("errors", []) or []:
            failed.append(e)
    return details, failed


def _sanity_problems(stub_count, detail_count, flows, failed):
    """Reasons this pull must NOT be scored.

    An audit that under-reports automation does not produce a cautious
    finding — it produces a confident, wrong one.
    """
    problems = []
    if stub_count != detail_count:
        problems.append(
            f"Detail fetch incomplete: {detail_count} of {stub_count} workflows "
            f"returned full specs ({len(failed)} batch errors)."
        )
    if stub_count < 1:
        problems.append("No workflows returned at all.")
    # A real portal mixes object types and trigger styles. Uniformity across
    # many flows means a filtered endpoint, not a uniform portal.
    if len(flows) > 3:
        obj_types = {f.get("objectTypeId") for f in flows}
        if len(obj_types) == 1:
            problems.append(
                f"All {len(flows)} workflows share objectTypeId "
                f"{obj_types.pop()} — that is the legacy v3 signature."
            )
        types = {f.get("type") for f in flows}
        if len(types) == 1:
            problems.append(
                f"All {len(flows)} workflows are type {types.pop()} — "
                "suspiciously uniform."
            )
    return problems


def audit_workflows(client):
    section = {"available": False, "source": "automation/v4/flows"}

    stubs, status, err = _list_flows(client)
    if status != 200:
        section["error"] = f"HTTP {status}: {err or 'unauthorized — check the automation scope'}"
        return section

    details, failed = _flow_details(client, [s["id"] for s in stubs])
    enabled_by_id = {str(s["id"]): s.get("isEnabled") for s in stubs}
    for d in details:
        d["_isEnabled"] = enabled_by_id.get(str(d.get("id")))

    problems = _sanity_problems(len(stubs), len(details), details, failed)
    if problems:
        section["error"] = (
            "Workflow pull failed its sanity check; Area 5 must be scored "
            "UNKNOWN, not green: " + "; ".join(problems)
        )
        section["sanity_problems"] = problems
        return section

    section["available"] = True
    section["sanity_check"] = "passed"
    section["failed_reads"] = failed

    written, inventory = set(), []
    vague_tokens = ("workflow ", "test", "copy of", "untitled", "new workflow", "temp")
    unnamed = []

    for w in details:
        name = (w.get("name") or "").strip()
        low = name.lower()
        if not name or any(t in low for t in vague_tokens):
            unnamed.append(name or f"(id {w.get('id')})")

        action_types, props_set = {}, []
        for a in w.get("actions") or []:
            at = a.get("actionTypeId", "UNKNOWN")
            action_types[at] = action_types.get(at, 0) + 1
            if at != SET_PROPERTY_ACTION:
                continue
            fields = a.get("fields", {}) or {}
            prop = fields.get("property_name") or fields.get("propertyName")
            if prop:
                props_set.append(prop)
                written.add(prop)

        enrol = w.get("enrollmentCriteria") or {}
        inventory.append({
            "id": w.get("id"),
            "name": name,
            "enabled": bool(w.get("_isEnabled")),
            "object_type": OBJECT_LABELS.get(w.get("objectTypeId"), w.get("objectTypeId")),
            "flow_type": w.get("type"),
            "action_types": action_types,
            "properties_set": sorted(set(props_set)),
            "sets_hygiene_props": sorted({p for p in props_set if p in HYGIENE_PROPS}),
            "re_enrollment": bool(enrol.get("shouldReEnroll")),
            "has_enrollment_criteria": bool(
                enrol.get("listFilterBranch") or enrol.get("eventFilterBranches")
            ),
            # last EDIT, not last run. HubSpot exposes no last-run field.
            "days_since_modified": _age_days(w.get("updatedAt")),
        })

    on = [w for w in inventory if w["enabled"]]
    section["counts"] = {
        "total": len(inventory),
        "on": len(on),
        "off": len(inventory) - len(on),
        "unnamed_or_vague": len(unnamed),
        "no_enrollment_criteria": sum(1 for w in inventory if not w["has_enrollment_criteria"]),
    }
    section["by_object_type"] = {
        lbl: sum(1 for w in inventory if w["object_type"] == lbl)
        for lbl in {w["object_type"] for w in inventory}
    }
    section["properties_written_by_workflows"] = sorted(written)
    section["hygiene_coverage"] = {
        prop: sorted({w["name"] for w in inventory if prop in w["properties_set"]})
        for prop in HYGIENE_PROPS
    }
    section["governance_gaps"] = [
        label for prop, label in HYGIENE_PROPS.items() if prop not in written
    ]
    section["inventory"] = inventory
    section["unnamed"] = unnamed[:50]
    section["note"] = (
        "Per-workflow error state is not exposed by the public API; confirm "
        "error banners in the HubSpot UI (Automation > Workflows > 'Needs "
        "review'). days_since_modified is the last EDIT, never the last run — "
        "no HubSpot API returns a workflow's last-run time, so do not infer "
        "that a workflow is dormant from it."
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

    # isDnsCorrect is False only when DNS genuinely fails. When HubSpot omits
    # the field entirely, treating absent as failing flags every domain on
    # every portal, so absent is reported separately rather than as a failure.
    section["unverified_dns"] = [
        d.get("domain") for d in domains if d.get("isDnsCorrect") is False
    ]
    section["dns_state_not_reported"] = [
        d.get("domain") for d in domains if d.get("isDnsCorrect") is None
    ]

    # HubSpot-issued subdomains are not an authenticated company sending
    # domain. A portal with no custom domain has no DKIM/SPF of its own,
    # which the DNS flags above will never surface.
    HUBSPOT_SUFFIXES = (
        ".hs-sites.com", ".hs-sites-eu1.com", ".hubspotpagebuilder.com",
        ".hubspotpagebuilder.eu", ".hs-sites-na2.com", ".hubspotpagebuilder.net",
    )
    custom = [
        d.get("domain") for d in domains
        if d.get("domain") and not any(str(d["domain"]).endswith(s) for s in HUBSPOT_SUFFIXES)
    ]
    section["custom_domains"] = custom
    section["has_custom_sending_domain"] = bool(custom)
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
                print(f"    [{state}] {wf['name']}  ({wf['object_type']})")
                print(f"           actions: {acts}")
                print(f"           sets: {','.join(wf['properties_set']) or '(no property writes)'}")
                print(f"           hygiene-props set: {hyg} | re-enroll: {wf['re_enrollment']}"
                      f" | enrollment criteria: {wf['has_enrollment_criteria']}")
        print()
    print(f"Full JSON written to: {json_path}")


if __name__ == "__main__":
    main()
