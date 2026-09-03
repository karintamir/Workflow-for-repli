"""
Replacement workflow pull for hs_audit.py — Area 5.

WHY THIS EXISTS
---------------
The original pull used GET /automation/v3/workflows. That endpoint is legacy and
returns only contact-based, older-style workflows. On the Hush Security portal it
returned 5 workflows (all type DRIP_DELAY) against 20 that are actually ON.
The audit then reported "automation does not govern the CRM" — which was false.

v4 (GET /automation/v4/flows) returns every workflow regardless of object type or
editor version, but only key fields. Full specs come from a second batch call.

Scope required: `automation` (already on the Glare audit token).
Note: /automation/v4 is a public BETA API and may change.
Note: flows containing sensitive-data properties require additional sensitive-data
      scopes; those flows may 403 individually on batch read.

USAGE
-----
    flows = fetch_workflows(TOKEN)
    assert_workflow_pull_sane(flows, v3_count=len(legacy_result))
"""

import time
import requests

BASE = "https://api.hubapi.com"


def _get(token, path, params=None):
    for attempt in range(5):
        r = requests.get(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited repeatedly on {path}")


def _post(token, path, body):
    for attempt in range(5):
        r = requests.post(
            f"{BASE}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited repeatedly on {path}")


def list_flow_ids(token):
    """GET /automation/v4/flows — every workflow, key fields only."""
    out, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        page = _get(token, "/automation/v4/flows", params)
        out.extend(page.get("results", []))
        after = page.get("paging", {}).get("next", {}).get("after")
        if not after:
            return out


def fetch_flow_details(token, flow_ids, chunk=50):
    """POST /automation/v4/flows/batch/read — full spec per workflow.

    Chunk size is a guess; HubSpot does not document the batch ceiling here.
    If you get a 400 on large chunks, drop it to 10.
    """
    details, failed = [], []
    for i in range(0, len(flow_ids), chunk):
        batch = flow_ids[i:i + chunk]
        body = {"inputs": [{"flowId": str(f), "type": "FLOW_ID"} for f in batch]}
        try:
            resp = _post(token, "/automation/v4/flows/batch/read", body)
            details.extend(resp.get("results", []))
            # partial failures come back here, not as an exception
            for e in resp.get("errors", []) or []:
                failed.append(e)
        except requests.HTTPError as exc:
            failed.append({"batch_start": i, "error": str(exc)})
    return details, failed


def fetch_workflows(token):
    """Full Area 5 dataset: every workflow with its actions and enrollment."""
    stubs = list_flow_ids(token)
    ids = [s["id"] for s in stubs]
    details, failed = fetch_flow_details(token, ids)

    enabled_by_id = {str(s["id"]): s.get("isEnabled") for s in stubs}
    for d in details:
        d["_isEnabled"] = enabled_by_id.get(str(d.get("id")))

    return {
        "stub_count": len(stubs),
        "detail_count": len(details),
        "workflows": details,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# The guard. This is the part that would have caught the Hush error.
# ---------------------------------------------------------------------------

class WorkflowPullError(RuntimeError):
    pass


def assert_workflow_pull_sane(result, v3_count=None, min_expected=1):
    """Fail loudly rather than reporting a truncated pull as portal state.

    An audit that under-reports automation does not produce a cautious finding.
    It produces a confident, wrong one — which is worse than no finding at all.
    """
    problems = []
    wf = result["workflows"]

    if result["stub_count"] != result["detail_count"]:
        problems.append(
            f"Detail fetch incomplete: {result['detail_count']} of "
            f"{result['stub_count']} workflows returned full specs. "
            f"{len(result['failed'])} batch errors."
        )

    if result["stub_count"] < min_expected:
        problems.append(f"Only {result['stub_count']} workflows returned.")

    # A real portal mixes object types and trigger styles. Uniformity means
    # a filtered endpoint, not a uniform portal.
    obj_types = {w.get("objectTypeId") for w in wf}
    if len(wf) > 3 and len(obj_types) == 1:
        problems.append(
            f"All {len(wf)} workflows share objectTypeId {obj_types.pop()} — "
            "this is the v3 signature. Confirm you are hitting v4."
        )

    types = {w.get("type") for w in wf}
    if len(wf) > 3 and len(types) == 1:
        problems.append(
            f"All {len(wf)} workflows are type {types.pop()} — suspiciously uniform."
        )

    if v3_count is not None and result["stub_count"] <= v3_count:
        problems.append(
            f"v4 returned {result['stub_count']} workflows, v3 returned {v3_count}. "
            "v4 should be a superset. Something is filtering the result."
        )

    if problems:
        raise WorkflowPullError(
            "Workflow pull failed sanity check — do NOT score Area 5 from this "
            "data. Mark it UNKNOWN and investigate:\n  - " + "\n  - ".join(problems)
        )

    return True


# ---------------------------------------------------------------------------
# Coverage analysis — what the audit actually needs to know
# ---------------------------------------------------------------------------

# actionTypeId values worth checking. "0-5" is SET_PROPERTY.
SET_PROPERTY = "0-5"


def coverage_gaps(result):
    """Which governance properties does NO workflow write?

    Replaces the old hardcoded 'nothing sets lifecycle/lead status/owner' claim
    with something actually derived from the flow specs.
    """
    written = set()
    for w in result["workflows"]:
        for action in w.get("actions", []) or []:
            if action.get("actionTypeId") != SET_PROPERTY:
                continue
            fields = action.get("fields", {}) or {}
            prop = fields.get("property_name") or fields.get("propertyName")
            if prop:
                written.add(prop)

    watched = {
        "lifecyclestage": "Sets Lifecycle Stage",
        "hs_lead_status": "Sets Lead Status",
        "hubspot_owner_id": "Assigns Owner",
    }
    return {
        "properties_written_by_workflows": sorted(written),
        "gaps": [label for prop, label in watched.items() if prop not in written],
    }


if __name__ == "__main__":
    import json
    import os
    import sys

    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        sys.exit("HUBSPOT_TOKEN not set")

    res = fetch_workflows(token)
    print(f"Workflows returned: {res['stub_count']} "
          f"(details: {res['detail_count']}, failures: {len(res['failed'])})")

    try:
        assert_workflow_pull_sane(res)
        print("Sanity check: PASSED")
    except WorkflowPullError as exc:
        print(f"\nSANITY CHECK FAILED\n{exc}")
        sys.exit(1)

    print(json.dumps(coverage_gaps(res), indent=2))
