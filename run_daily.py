#!/usr/bin/env python3
"""
Daily CME bulletin -> history.csv (which your sheet pulls in).

Safe to run as often as you like. It appends a row only when all of the
following hold, and otherwise exits quietly with status 0:

  * every section's header says FINAL (not PRELIMINARY)
  * all three sections report the same trade date
  * that trade date is recent, so a stale cached copy cannot be mistaken
    for today's bulletin
  * the instrument/date pair is not already recorded
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import pathlib
import sys
import tempfile
from zoneinfo import ZoneInfo

import cme_bulletin as cb
import fetch

CHICAGO = ZoneInfo("America/Chicago")
RELEASE_HOUR, RELEASE_MIN = 10, 15      # bulletin goes FINAL around 10:00 CT
MAX_STALENESS_DAYS = 5                  # refuse a bulletin older than this
HISTORY = pathlib.Path("history.csv")   # repo root, so no folder is needed

# Column order matches the tracking sheet, with two audit columns appended so
# the settlement and Delta RTH can always be traced back to a contract month
# and an opening price.
COLUMNS = [
    "Instrument", "Date", "Volume (total)", "Open Interest (total)",
    "Open Interest change", "Settlement Price", "Settlement Change",
    "Settlement % Chg", "Delta RTH", "Signal", "Contract", "Session Open",
]


def log(msg: str) -> None:
    stamp = dt.datetime.now(CHICAGO).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{stamp}] {msg}", flush=True)


def before_release(now=None) -> bool:
    now = now or dt.datetime.now(CHICAGO)
    return (now.hour, now.minute) < (RELEASE_HOUR, RELEASE_MIN)


def _cells(r) -> list:
    """
    One Row -> CSV cells.

    Plain numbers, no thousands separators, so the file stays valid CSV and
    arrives in the sheet as numbers rather than text.

    Percentages are written as FRACTIONS: -0.0312 means -3.12%. That is what
    a spreadsheet's Percent format expects, since it multiplies by 100 on
    display. Writing 3.12 for 3.12% would render as 312%.
    """
    return [
        r.instrument,
        r.trade_date.strftime("%d/%m/%Y"),
        f"{r.volume_total:.0f}",
        f"{r.oi_total:.0f}",
        f"{r.oi_change:.0f}",
        f"{r.settlement:.10g}",
        f"{r.settlement_change:.10g}",
        f"{r.settlement_pct / 100:.6f}",
        f"{r.delta_rth / 100:.6f}",
        r.signal,
        cb.contract_code(r.instrument, r.contract),
        f"{r.session_open:.10g}",
    ]


def read_history() -> tuple[list, set]:
    """Existing rows and the {(instrument, date)} already recorded."""
    if not HISTORY.exists():
        return [], set()
    with HISTORY.open(newline="") as fh:
        rows = list(csv.reader(fh))
    if rows and rows[0] and rows[0][0] == COLUMNS[0]:
        rows = rows[1:]
    seen = {(r[0].strip().upper(), r[1].strip()) for r in rows if len(r) >= 2}
    return rows, seen


def write_history(existing: list, fresh: list) -> None:
    """Rewrite the file with old and new rows, oldest date first."""
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    all_rows = existing + fresh

    def key(row):
        try:
            return (dt.datetime.strptime(row[1], "%d/%m/%Y").date(), row[0])
        except (ValueError, IndexError):
            return (dt.date.min, row[0] if row else "")

    all_rows.sort(key=key)
    with HISTORY.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerows(all_rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and print, write nothing")
    ap.add_argument("--ignore-clock", action="store_true",
                    help="skip the 'too early in the day' guard")
    ap.add_argument("--pdf-dir", default=None,
                    help="parse local PDFs instead of downloading")
    args = ap.parse_args()

    if not args.ignore_clock and not args.pdf_dir and before_release():
        log("Before 10:15 CT - the FINAL bulletin is not out yet. Nothing to do.")
        return 0

    if args.pdf_dir:
        base = pathlib.Path(args.pdf_dir)
        paths = {k: base / f"{v}.pdf" for k, v in cb.SECTIONS.items()}
        tmp = None
    else:
        tmp = tempfile.TemporaryDirectory()
        log("Downloading bulletin sections...")
        try:
            paths = fetch.fetch_all(cb.SECTIONS, pathlib.Path(tmp.name))
        except fetch.FetchError as exc:
            tmp.cleanup()
            log(f"DOWNLOAD FAILED: {exc}")
            return 1
        log("Downloaded all three sections.")

    try:
        rows = cb.extract(paths, require_final=True)
    except cb.BulletinError as exc:
        # Not final yet is the normal case on early runs, not a failure.
        log(f"Not ready: {exc}")
        return 0
    finally:
        if tmp:
            tmp.cleanup()

    trade_date = rows[0].trade_date
    age = (dt.datetime.now(CHICAGO).date() - trade_date).days
    if age > MAX_STALENESS_DAYS:
        log(f"REFUSING: bulletin trade date {trade_date} is {age} days old - "
            f"probably a stale cached file.")
        return 1
    log(f"FINAL bulletin for {trade_date:%d/%m/%Y} parsed - {len(rows)} instruments.")

    for r in rows:
        log(f"  {r.instrument} {r.contract}  vol {r.volume_total:>11,.0f}  "
            f"OI {r.oi_total:>11,.0f} ({r.oi_change:+,.0f})  "
            f"open {r.session_open:.10g} -> settle {r.settlement:.10g} "
            f"({r.settlement_change:+.10g}, {r.settlement_pct:+.2f}%)  "
            f"dRTH {r.delta_rth:+.2f}%  {r.signal}")

    if args.dry_run:
        log("Dry run - nothing written.")
        return 0

    existing, seen = read_history()
    fresh = [_cells(r) for r in rows
             if (r.instrument.upper(), r.trade_date.strftime("%d/%m/%Y")) not in seen]
    if not fresh:
        log("All rows for this date are already recorded. Nothing written.")
        return 0

    write_history(existing, fresh)
    log(f"Wrote {len(fresh)} row(s) to {HISTORY}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
