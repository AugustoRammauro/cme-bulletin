"""
OPTIONAL: append bulletin rows straight into the Google Sheet.

The default pipeline does not use this file. It writes data/history.csv and
the sheet pulls that in with IMPORTDATA, which needs no credentials at all.

This module is here if you later want rows written directly into an existing
tab instead. It needs a Google service account, its JSON key in the
GOOGLE_SERVICE_ACCOUNT_JSON environment variable, the sheet shared with the
service account's email as Editor, and the two commented-out lines in
requirements.txt uncommented.
"""

from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Columns A-J of the tracking sheet. Columns K onward hold your own
# day/week-change formulas and are never touched.
HEADER = [
    "Instrument", "Date", "Volume (total)", "Open Interest (total)",
    "Open Interest change", "Settlement Price", "Settlement Change",
    "Settlement % Chg", "Delta RTH", "Signal",
]


def _client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return gspread.authorize(creds)


def open_worksheet(spreadsheet_id: str, worksheet_gid: int):
    return _client().open_by_key(spreadsheet_id).get_worksheet_by_id(worksheet_gid)


def existing_keys(ws) -> set:
    """
    {(instrument, dd/mm/yyyy)} already present.

    Used to make the job idempotent: it can run every 15 minutes all day and
    will only ever write a given instrument/date once.
    """
    keys = set()
    for row in ws.get_values("A2:B"):
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            keys.add((row[0].strip().upper(), row[1].strip()))
    return keys


def _fmt(row) -> list:
    """One extractor Row -> the ten cell values, matching the sheet's style."""
    return [
        row.instrument,
        row.trade_date.strftime("%d/%m/%Y"),
        f"{row.volume_total:,.0f}",
        f"{row.oi_total:,.0f}",
        f"{row.oi_change:,.0f}",
        f"{row.settlement:.10g}",
        f"{row.settlement_change:.10g}",
        f"{row.settlement_pct / 100:.6f}",   # written as a fraction so the
        f"{row.delta_rth / 100:.6f}",        # sheet's % format renders it
        row.signal,
    ]


def append_rows(ws, rows) -> list:
    """Append only the rows not already present. Returns what was written."""
    have = existing_keys(ws)
    fresh = [r for r in rows
             if (r.instrument.upper(), r.trade_date.strftime("%d/%m/%Y")) not in have]
    if fresh:
        ws.append_rows([_fmt(r) for r in fresh],
                       value_input_option="USER_ENTERED",
                       table_range="A1")
    return fresh
