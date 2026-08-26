"""
Checks on the parts that have to behave correctly on an unattended run.

Run with:  python -m pytest test_pipeline.py -q
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
import types

import pytest

import cme_bulletin as cb
import run_daily

PDF_DIR = pathlib.Path(__file__).parent / "samples"
PATHS = {k: PDF_DIR / f"{v}.pdf" for k, v in cb.SECTIONS.items()}
have_samples = all(p.exists() for p in PATHS.values())
needs_samples = pytest.mark.skipif(not have_samples, reason="sample PDFs absent")

# Values read by eye from the FINAL bulletin of Tue 25 Aug 2026.
EXPECTED = {
    "CL": dict(contract="OCT26", volume=757_809, oi=1_906_740, oi_chg=-3_644,
               settle=82.36,     chg=-2.65),
    "GC": dict(contract="DEC26", volume=210_909, oi=427_957,   oi_chg=+1_269,
               settle=4694.50,   chg=-3.30),
    "SI": dict(contract="DEC26", volume=85_355,  oi=113_801,   oi_chg=-1_722,
               settle=69.470,    chg=+0.089),
    "ES": dict(contract="SEP26", volume=1_002_413, oi=2_045_669, oi_chg=+5_533,
               settle=7692.00,   chg=+22.25),
    "NQ": dict(contract="SEP26", volume=487_902, oi=301_987,   oi_chg=-448,
               settle=29276.75,  chg=+171.00),
    "YM": dict(contract="SEP26", volume=53_574,  oi=88_454,    oi_chg=-529,
               settle=53645.00,  chg=+156.00),
}


# ---------------------------------------------------------------- parsing ---

@needs_samples
@pytest.mark.parametrize("ticker", sorted(EXPECTED))
def test_matches_the_printed_bulletin(ticker):
    rows = {r.instrument: r for r in cb.extract(PATHS)}
    got, want = rows[ticker], EXPECTED[ticker]
    assert got.contract == want["contract"]
    assert got.volume_total == want["volume"]
    assert got.oi_total == want["oi"]
    assert got.oi_change == want["oi_chg"]
    assert got.settlement == pytest.approx(want["settle"])
    assert got.settlement_change == pytest.approx(want["chg"])


@needs_samples
def test_nq_settlement_survives_the_column_overflow():
    """NQ's 29276.75 is typeset as '29276.7' plus a '5' on the next line."""
    rows = {r.instrument: r for r in cb.extract(PATHS)}
    assert rows["NQ"].settlement == pytest.approx(29276.75)


@needs_samples
def test_point_change_without_a_decimal_point_is_rescaled():
    """Section 11 prints ES's +22.25 as '2225'. Rescaling must undo that."""
    rows = {r.instrument: r for r in cb.extract(PATHS)}
    for t, prior in (("ES", 7669.75), ("NQ", 29105.75), ("YM", 53489.00)):
        r = rows[t]
        assert r.settlement - r.settlement_change == pytest.approx(prior)


def test_sign_glued_to_its_number_is_split():
    assert cb._split_sign("2045669+") == ["2045669", "+"]
    assert cb._split_sign("301987-") == ["301987", "-"]
    assert cb._split_sign("53645.00") == ["53645.00"]


def test_flagged_prices_parse():
    assert cb._num("7905.00B") == 7905.0
    assert cb._num("#3852.00") == 3852.0
    assert cb._num("*3828.30A") == 3828.30
    assert cb._num("----") is None
    assert cb._num("UNCH") == 0.0


# ----------------------------------------------------------- FINAL gating ---

@needs_samples
def test_preliminary_bulletin_is_refused(monkeypatch):
    """A PRELIMINARY header must never produce a row."""
    real = cb.read_header
    monkeypatch.setattr(cb, "read_header",
                        lambda pdf: (False, real(pdf)[1]))
    with pytest.raises(cb.BulletinError, match="not FINAL"):
        cb.extract(PATHS, require_final=True)


@needs_samples
def test_sections_must_agree_on_the_trade_date(monkeypatch):
    """A cached copy of one section from another day must be caught."""
    real = cb.read_header
    seen = {"n": 0}

    def fake(pdf):
        final, day = real(pdf)
        seen["n"] += 1
        if seen["n"] == 2:                      # one section lags a day
            day = day - dt.timedelta(days=1)
        return final, day

    monkeypatch.setattr(cb, "read_header", fake)
    with pytest.raises(cb.BulletinError, match="disagree on trade date"):
        cb.extract(PATHS, require_final=True)


# --------------------------------------------------------------- guards ----

def test_stale_bulletin_is_refused(monkeypatch, capsys):
    """A FINAL file from three weeks ago must not be written as today's row."""
    old = dt.date.today() - dt.timedelta(days=21)
    monkeypatch.setattr(run_daily.cb, "extract",
                        lambda *a, **k: [_row("CL", old)])
    monkeypatch.setattr(run_daily.sys, "argv",
                        ["run_daily.py", "--pdf-dir", "."])
    assert run_daily.main() == 1
    assert "stale" in capsys.readouterr().out.lower()


def test_clock_guard_blocks_early_runs():
    early = dt.datetime(2026, 8, 26, 9, 30, tzinfo=run_daily.CHICAGO)
    late = dt.datetime(2026, 8, 26, 10, 30, tzinfo=run_daily.CHICAGO)
    assert run_daily.before_release(early) is True
    assert run_daily.before_release(late) is False


# ---------------------------------------------------------- idempotency ----

def _row(ticker, day):
    return types.SimpleNamespace(
        instrument=ticker, trade_date=day, contract="OCT26",
        volume_total=1.0, oi_total=2.0, oi_change=3.0, settlement=4.0,
        settlement_change=0.5, settlement_pct=1.0, session_open=3.9,
        delta_rth=1.0, signal="Bullish")


@pytest.fixture
def history(tmp_path, monkeypatch):
    path = tmp_path / "history.csv"
    monkeypatch.setattr(run_daily, "HISTORY", path)
    return path


def _run(monkeypatch, rows):
    monkeypatch.setattr(run_daily.cb, "extract", lambda *a, **k: rows)
    monkeypatch.setattr(run_daily.sys, "argv",
                        ["run_daily.py", "--pdf-dir", "."])
    return run_daily.main()


def test_rows_are_written_with_a_header(history, monkeypatch):
    day = dt.date.today()
    assert _run(monkeypatch, [_row("CL", day), _row("GC", day)]) == 0
    lines = history.read_text().strip().splitlines()
    assert lines[0].startswith("Instrument,Date,")
    assert len(lines) == 3


def test_a_second_run_the_same_day_adds_nothing(history, monkeypatch):
    day = dt.date.today()
    _run(monkeypatch, [_row("CL", day)])
    first = history.read_text()
    _run(monkeypatch, [_row("CL", day)])            # the 10:30, 10:45, ... runs
    assert history.read_text() == first


def test_a_new_instrument_is_added_without_disturbing_the_rest(history, monkeypatch):
    day = dt.date.today()
    _run(monkeypatch, [_row("CL", day)])
    _run(monkeypatch, [_row("CL", day), _row("SI", day)])
    rows = list(csv.reader(history.read_text().splitlines()))[1:]
    assert sorted(r[0] for r in rows) == ["CL", "SI"]


def test_history_stays_in_date_order(history, monkeypatch):
    _run(monkeypatch, [_row("CL", dt.date.today())])
    _run(monkeypatch, [_row("CL", dt.date.today() - dt.timedelta(days=1))])
    rows = list(csv.reader(history.read_text().splitlines()))[1:]
    dates = [dt.datetime.strptime(r[1], "%d/%m/%Y").date() for r in rows]
    assert dates == sorted(dates)


def test_csv_has_no_thousands_separators(history, monkeypatch):
    """Commas inside numbers would break the CSV the sheet imports."""
    day = dt.date.today()
    row = _row("CL", day)
    row.volume_total, row.oi_total = 1_002_413.0, 2_045_669.0
    _run(monkeypatch, [row])
    body = history.read_text().splitlines()[1]
    assert "1002413" in body and "2045669" in body
    assert body.count(",") == len(run_daily.COLUMNS) - 1


# --------------------------------------------------------------- signal ----

@pytest.mark.parametrize("pct,oi,drth,expected", [
    (+1.20, +5000, +1.0, "Bullish"),    # price up, new longs, session agrees
    (-1.20, +5000, -1.0, "Bearish"),    # price down, new shorts
    (+1.20, -5000, +1.0, "Sideways"),   # rally on shrinking OI = covering
    (-1.20, -5000, -1.0, "Sideways"),   # break on shrinking OI = liquidation
    (+1.20,     0, +1.0, "Sideways"),   # no change in OI confirms nothing
    (+0.10, +5000, +0.1, "Sideways"),   # inside the quiet threshold
    (+1.20, +5000, -1.0, "Sideways"),   # gapped overnight, session disagreed
    # Above STRONG_PCT the price move stands on its own. This is today's CL:
    # down 3.12% on OI that fell 3,644 out of 1.9m, which is not a reason to
    # call a 3% collapse Sideways.
    (-3.12, -3644, -3.14, "Bearish"),
    (+2.00, -5000, +1.5, "Bullish"),
    # ...but Delta RTH still vetoes, however big the move.
    (-3.12, -3644, +0.50, "Sideways"),
])
def test_signal_rules(pct, oi, drth, expected):
    assert cb.signal(pct, oi, drth) == expected


# ------------------------------------------------------- spreadsheet safety --

@pytest.mark.parametrize("inst,month,expected", [
    ("CL", "OCT26", "CL OCT26"),
    ("ES", "SEP26", "ES SEP26"),
    ("GC", "DEC26", "GC DEC26"),
    ("SI", "MAR27", "SI MAR27"),
])
def test_contract_code(inst, month, expected):
    assert cb.contract_code(inst, month) == expected


def test_no_csv_value_can_be_read_as_a_date_by_a_spreadsheet(history, monkeypatch):
    """
    'OCT26' in a CSV is silently parsed as 1 Oct 2026 and stored as the serial
    46321, which is how the contract month arrived in the sheet as a number.
    Only the Date column may look like a date.
    """
    import re as _re
    day = dt.date.today()
    _run(monkeypatch, [_row("CL", day)])
    rows = list(csv.reader(history.read_text().splitlines()))
    date_col = run_daily.COLUMNS.index("Date")
    # Only a bare month name triggers the coercion. 'GC DEC26' is safe because
    # the instrument prefix stops it looking like a date; 'DEC26' is not.
    months = "|".join(cb.MONTH_CODES)
    monthish = _re.compile(rf"^({months})-?\d{{2}}$", _re.IGNORECASE)
    for row in rows[1:]:
        for i, cell in enumerate(row):
            if i == date_col:
                continue
            assert not monthish.match(cell), f"{cell!r} in {run_daily.COLUMNS[i]!r}"
