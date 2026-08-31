#!/usr/bin/env python3
"""
Build a review workbook of every pending contact change, so nothing is
written to the portal until a human has checked it.

Sheets:
  README                  what each sheet is, and how to mark decisions
  Summary                 live counts driven by the Approve columns
  2023 Bulk Edits         the 24 contacts touched by the 2023 bulk edits
  Backfill Multi          backfill rows recovering more than one conference
  Backfill All            the full backfill plan

Read-only against HubSpot. Writes an .xlsx and nothing else.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BASE = "https://api.hubapi.com"
SRC = "conference_name"
PORTAL = "25420191"

MERGED_FORWARD = {
    "equiphotel_2024": "EquipHotel Paris 2024",
    "itb_2025": "ITB Berlin 2025",
    "ITB_2026": "ITB Berlin 2026",
    "FHT_2026": "FHT Paris 2026",
    "Accor Phuket Confernece 2026": "Accor Phuket Conference 2026",
    "FHT 2023": "fht_paris_2023",
    "ITB-2026": "ITB Berlin 2026",
}
JUNK = {"pre - short stay", "pre - short stay summit", "pre short stay",
        "short stay"}

HDR = PatternFill("solid", fgColor="1F3864")
HDRF = Font(name="Arial", size=10, bold=True, color="FFFFFF")
BODY = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
INPUT = PatternFill("solid", fgColor="FFFF00")
WARN = PatternFill("solid", fgColor="FCE4D6")


def api(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def normalise(raw):
    out = []
    for part in str(raw).split(";"):
        v = part.strip()
        if not v or v in JUNK:
            continue
        out.append(MERGED_FORWARD.get(v, v))
    return out


def gather(token):
    ids, after = [], 0
    while True:
        b = api("POST", "/crm/v3/objects/contacts/search", token,
                {"filterGroups": [{"filters": [
                    {"propertyName": SRC, "operator": "HAS_PROPERTY"}]}],
                 "properties": ["email"], "limit": 100, "after": after})
        ids += [r["id"] for r in b["results"]]
        nxt = ((b.get("paging") or {}).get("next") or {}).get("after")
        if not nxt:
            break
        after = int(nxt)
        time.sleep(0.04)

    recs = []
    for k in range(0, len(ids), 50):
        res = api("POST", "/crm/v3/objects/contacts/batch/read", token,
                  {"propertiesWithHistory": [SRC],
                   "properties": ["email", "firstname", "lastname",
                                  SRC, "conference_first_new",
                                  "conference_touchpoints"],
                   "inputs": [{"id": i} for i in ids[k:k + 50]]})
        recs += res.get("results", [])
        time.sleep(0.04)
    return recs


def hist_of(r):
    return sorted((r.get("propertiesWithHistory") or {}).get(SRC) or [],
                  key=lambda x: x["timestamp"])


def flat_hist(h):
    return " | ".join("%s %s (%s)" % (x["timestamp"][:10], x["value"],
                                      (x.get("sourceType") or "")[:16])
                      for x in h)


def style(ws, headers, widths, freeze="A2"):
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = HDR, HDRF
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = freeze
    ws.row_dimensions[1].height = 30


def main():
    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("HUBSPOT_PRIVATE_APP_TOKEN not set", file=sys.stderr)
        return 2

    first_opts = {o["value"] for o in
                  api("GET", "/crm/v3/properties/contacts/conference_first_new",
                      token)["options"] if not o.get("hidden")}
    tp_opts = {o["value"] for o in
               api("GET", "/crm/v3/properties/contacts/conference_touchpoints",
                   token)["options"] if not o.get("hidden")}

    print("reading contacts and property history ...")
    recs = gather(token)
    print("  %d contacts\n" % len(recs))

    bulk_rows, back_rows = [], []
    for r in recs:
        p = r.get("properties") or {}
        h = hist_of(r)
        email = p.get("email") or r["id"]
        link = "https://app-eu1.hubspot.com/contacts/%s/contact/%s" % (PORTAL, r["id"])
        cur = p.get(SRC)

        seq = []
        for x in h:
            for v in normalise(x.get("value")):
                if v not in seq:
                    seq.append(v)

        # --- 2023 bulk edit review ---
        old_bulk = [x for x in h if x.get("sourceType") == "CRM_UI_BULK_ACTION"
                    and x["timestamp"][:4] == "2023"]
        if old_bulk:
            nonbulk = [x for x in h if x.get("sourceType") != "CRM_UI_BULK_ACTION"]
            cand = nonbulk[-1]["value"] if nonbulk else None
            still = cur == old_bulk[-1]["value"]
            if still and cand and cand != cur:
                days = (old_bulk[-1]["timestamp"][:10], nonbulk[-1]["timestamp"][:10])
                group = "A" if days[0] != days[1] else "B"
                why = ("Bulk edit %s reverted a more recent import from %s"
                       % (days[0], days[1])) if group == "A" else \
                      ("Bulk edit and form submission on the SAME day (%s) — "
                       "may be a deliberate reclassification" % days[0])
                proposed = cand
            else:
                group, why, proposed = "no action", (
                    "A later value already superseded the bulk edit"
                    if cand else "No non-bulk entry to fall back to"), ""
            bulk_rows.append([email, r["id"], link, cur, proposed, group, why,
                              flat_hist(h), ""])

        # --- backfill plan ---
        if not seq or p.get("conference_first_new"):
            continue
        first = next((v for v in seq if v in first_opts), None)
        if first is None:
            continue
        tps = [v for v in seq if v in tp_opts]
        dropped = [v for v in seq if v not in tp_opts]
        back_rows.append([email, r["id"], link, cur, first, ";".join(tps),
                          len(tps), len(seq), ";".join(dropped),
                          flat_hist(h), ""])

    bulk_rows.sort(key=lambda x: (x[5] != "A", x[5] != "B", x[0]))
    # sort by conferences seen in history, so the richest records read first
    back_rows.sort(key=lambda x: (-x[7], -x[6], x[0]))
    # every contact whose history shows more than one conference — including
    # those where one of them has no Touchpoints option and gets dropped,
    # which are exactly the rows most worth a human eye
    multi = [r for r in back_rows if r[7] > 1]

    wb = Workbook()

    # README -----------------------------------------------------------
    ws = wb.active
    ws.title = "README"
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 105
    lines = [
        ("Duve — pending contact changes for review", ""),
        ("Portal", PORTAL),
        ("", ""),
        ("NOTHING HAS BEEN WRITTEN", "Every change listed here is a proposal. "
         "The portal is untouched until you say so."),
        ("", ""),
        ("How to use this", "Put Y or N in the yellow 'Approve?' column on each "
         "sheet. Leave it blank to decide later."),
        ("Example", "Approve? = Y   -> apply this row.   Approve? = N   -> skip it."),
        ("", ""),
        ("Sheet: 2023 Bulk Edits", "24 contacts whose Conference — Latest was "
         "changed by a bulk action in 2023 by a now-deactivated user. Group A "
         "reverted a more recent import days later and looks wrong. Group B "
         "happened the same day as the form submission and may be a deliberate "
         "reclassification — check before approving."),
        ("Sheet: Backfill Multi", "Backfill rows where property history shows "
         "MORE THAN ONE conference. These are the ones worth checking: the extra "
         "conferences exist only in history and cannot be recovered later. Where "
         "'Dropped' is shaded, one of those conferences has no Touchpoints option "
         "and will NOT be written."),
        ("Sheet: Backfill All", "The full backfill plan. Conference — First and "
         "Conference — Touchpoints are both empty today, so nothing is "
         "overwritten."),
        ("", ""),
        ("How First is chosen", "The earliest value in the contact's recorded "
         "property history."),
        ("How Touchpoints are chosen", "Every distinct value in history, mapped "
         "forward through renamed options and de-duplicated."),
        ("Values dropped", "A value with no matching Touchpoints option is "
         "listed in 'Dropped' rather than written — the two non-conference "
         "activities (Eklo Party, Oracle meetup) and HIC Evening Camp, which is "
         "still pending Marina."),
        ("Junk excluded", "The four 'short stay' test values Marina confirmed "
         "dead are ignored everywhere."),
    ]
    for a, b in lines:
        ws.append([a, b])
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=2):
        for c in row:
            c.font = BODY
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws["A1"].font = Font(name="Arial", size=14, bold=True)
    ws["A4"].font = Font(name="Arial", size=10, bold=True, color="C00000")
    for r in (6, 7):
        ws.cell(row=r, column=1).font = BOLD
    for r in (9, 10, 11):
        ws.cell(row=r, column=1).font = BOLD

    # Bulk edits -------------------------------------------------------
    ws = wb.create_sheet("2023 Bulk Edits")
    style(ws, ["Email", "Contact ID", "HubSpot link", "Current Latest",
               "Proposed Latest", "Group", "Why", "Property history",
               "Approve?"],
          [34, 14, 16, 30, 30, 10, 46, 74, 11])
    for row in bulk_rows:
        ws.append(row)
    for i in range(2, ws.max_row + 1):
        for c in range(1, 10):
            ws.cell(row=i, column=c).font = BODY
            ws.cell(row=i, column=c).alignment = Alignment(vertical="top",
                                                           wrap_text=(c in (7, 8)))
        lc = ws.cell(row=i, column=3)
        lc.hyperlink, lc.value = lc.value, "open"
        lc.font = Font(name="Arial", size=10, color="0563C1", underline="single")
        ws.cell(row=i, column=9).fill = INPUT
        if ws.cell(row=i, column=6).value == "B":
            ws.cell(row=i, column=6).fill = WARN

    # Backfill multi ---------------------------------------------------
    for title, rows in (("Backfill Multi", multi), ("Backfill All", back_rows)):
        ws = wb.create_sheet(title)
        style(ws, ["Email", "Contact ID", "HubSpot link", "Current Latest",
                   "Proposed First", "Proposed Touchpoints", "# Touchpoints",
                   "# in history", "Dropped (no option)", "Property history",
                   "Approve?"],
              [34, 14, 16, 28, 28, 52, 13, 12, 26, 74, 11])
        for row in rows:
            ws.append(row)
        for i in range(2, ws.max_row + 1):
            for c in range(1, 12):
                ws.cell(row=i, column=c).font = BODY
                ws.cell(row=i, column=c).alignment = Alignment(
                    vertical="top", wrap_text=(c in (6, 10)))
            lc = ws.cell(row=i, column=3)
            lc.hyperlink, lc.value = lc.value, "open"
            lc.font = Font(name="Arial", size=10, color="0563C1", underline="single")
            ws.cell(row=i, column=11).fill = INPUT
            if ws.cell(row=i, column=9).value:
                ws.cell(row=i, column=9).fill = WARN

    # Summary ----------------------------------------------------------
    ws = wb.create_sheet("Summary", 1)
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 14
    ws.append(["Counts update as you fill the Approve? columns", "", "", ""])
    ws.append(["", "Rows", "Approved (Y)", "Rejected (N)"])
    spec = [
        ("2023 bulk edits — Group A (looks wrong)", "'2023 Bulk Edits'", "F", "A", "I"),
        ("2023 bulk edits — Group B (check first)", "'2023 Bulk Edits'", "F", "B", "I"),
        ("2023 bulk edits — no action needed", "'2023 Bulk Edits'", "F", "no action", "I"),
        ("Backfill — multi-conference contacts", "'Backfill Multi'", None, None, "K"),
        ("Backfill — all contacts", "'Backfill All'", None, None, "K"),
    ]
    for label, sheet, gcol, gval, acol in spec:
        r = ws.max_row + 1
        ws.cell(row=r, column=1, value=label)
        if gcol:
            ws.cell(row=r, column=2, value='=COUNTIF(%s!%s:%s,"%s")' % (sheet, gcol, gcol, gval))
            ws.cell(row=r, column=3, value='=COUNTIFS(%s!%s:%s,"%s",%s!%s:%s,"Y")' % (sheet, gcol, gcol, gval, sheet, acol, acol))
            ws.cell(row=r, column=4, value='=COUNTIFS(%s!%s:%s,"%s",%s!%s:%s,"N")' % (sheet, gcol, gcol, gval, sheet, acol, acol))
        else:
            ws.cell(row=r, column=2, value='=COUNTA(%s!A:A)-1' % sheet)
            ws.cell(row=r, column=3, value='=COUNTIF(%s!%s:%s,"Y")' % (sheet, acol, acol))
            ws.cell(row=r, column=4, value='=COUNTIF(%s!%s:%s,"N")' % (sheet, acol, acol))
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=4):
        for c in row:
            c.font = BODY
    ws["A1"].font = Font(name="Arial", size=12, bold=True)
    for c in range(1, 5):
        ws.cell(row=2, column=c).font = BOLD

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "duve_pending_changes_review.xlsx")
    wb.save(out)
    print("bulk-edit rows : %d" % len(bulk_rows))
    print("backfill rows  : %d (multi-conference: %d)" % (len(back_rows), len(multi)))
    print("written        : %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
