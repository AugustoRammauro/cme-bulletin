"""
CME Daily Bulletin extractor.

Parses the CME Group Daily Information Bulletin PDFs and returns one row per
tracked instrument:

    Instrument, Date, Volume (total), Open Interest (total), OI Change,
    Settlement Price, Settlement Change, Delta RTH, Signal

Only emits data when the bulletin header says FINAL.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime

import pdfplumber

# --------------------------------------------------------------------------
# Instrument catalogue
# --------------------------------------------------------------------------
# Sections 61 (energy) and 62 (metals) label blocks by ticker ("CL FUT").
# Section 11 (equity index) labels them by product name ("EMINI S&P FUT")
# and uses two different column geometries, so each instrument declares which
# layout its rows follow.

SECTIONS = {
    "energy": "Section61_Energy_Futures_Products",
    "metals": "Section62_Metals_Futures_Products",
    "equity": "Section11_Equity_And_Index_Futures",
}

INSTRUMENTS = {
    "CL": dict(section="energy", block="CL FUT",            layout="globex"),
    "GC": dict(section="metals", block="GC FUT",            layout="globex"),
    "SI": dict(section="metals", block="SI FUT",            layout="globex"),
    "ES": dict(section="equity", block="EMINI S&P FUT",     layout="equity_pit"),
    "NQ": dict(section="equity", block="EMINI NASD FUT",    layout="equity_globex"),
    "YM": dict(section="equity", block="MINI $5 DOW FUT",   layout="equity_globex"),
}

# Column windows, keyed by layout, as (name, x0_lo, x0_hi).
# A word belongs to a column when x0_lo <= word.x0 < x0_hi.
# Groups ending in "_grp" hold several tokens: value, sign, magnitude.
LAYOUTS = {
    # Sections 61 / 62: GLOBEX OPEN | HIGH/LOW | SETT & CHGE | VOL | PNT | OI
    "globex": [
        ("month",     0,   45),
        ("open",    100,  190),
        ("highlow", 190,  275),
        ("sett_grp",275,  400),
        ("volume",  400,  460),
        ("pnt",     460,  520),
        ("oi_grp",  520,  620),
    ],
    # Section 11, E-mini S&P block: PIT OPEN RANGE | PIT HIGH | PIT LOW |
    # PIT CLOSING RANGE | SETT & CHGE | TRADES CLEARED | VOLUME | OI | CONTRACT
    "equity_pit": [
        ("month",     0,   40),
        ("open",     40,  100),
        ("high",    100,  160),
        ("low",     160,  210),
        ("closing", 210,  260),
        ("sett_grp",260,  395),
        ("cleared", 395,  420),
        ("volume",  420,  455),
        ("oi_grp",  455,  516),
        ("contract",516, 9999),
    ],
    # Section 11, remaining blocks: GLOBEX OPEN | HIGH | LOW | SETT & CHGE | ...
    "equity_globex": [
        ("month",     0,   40),
        ("open",     60,  160),
        ("high",    160,  210),
        ("low",     210,  270),
        ("sett_grp",270,  395),
        ("cleared", 395,  420),
        ("volume",  420,  460),
        ("oi_grp",  460,  516),
        ("contract",516, 9999),
    ],
}

MONTH_RE = re.compile(r"^[A-Z]{3}\d{2}$")

# Standard futures delivery-month letters.
MONTH_CODES = {"JAN": "F", "FEB": "G", "MAR": "H", "APR": "J",
               "MAY": "K", "JUN": "M", "JUL": "N", "AUG": "Q",
               "SEP": "U", "OCT": "V", "NOV": "X", "DEC": "Z"}


def contract_code(instrument: str, month: str) -> str:
    """
    'CL' + 'OCT26' -> 'CLV26', the standard futures symbol.

    Also solves a real problem downstream: a spreadsheet reading a CSV will
    happily parse the bare string 'OCT26' as 1 October 2026 and store a date
    serial, so the contract month arrives as '46321'. 'CLV26' cannot be
    mistaken for a date.
    """
    stem, year = month[:3].upper(), month[3:]
    letter = MONTH_CODES.get(stem)
    if letter is None:
        return f"{instrument}-{month}"
    return f"{instrument}{letter}{year}"
# Price/volume flags the bulletin appends or prepends to a number:
#   B=bid A=ask N=nominal P=post-settlement R=record #=new high *=new low
FLAG_RE = re.compile(r"^[#*]?(-?[\d,.]+)[BANPR]*$")
ROW_TOL = 3.0          # pt; vertical tolerance when clustering words into rows
WRAP_MIN, WRAP_MAX = 4.0, 8.0   # pt below a row where an overflow digit lands


class BulletinError(RuntimeError):
    """Raised when a bulletin cannot be parsed or is not FINAL."""


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

def _num(token: str):
    """Parse a bulletin number, stripping flags. '----' and 'UNCH' -> None/0."""
    token = token.strip()
    if token in ("----", "---", ""):
        return None
    if token == "UNCH":
        return 0.0
    m = FLAG_RE.match(token)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _decimals(token: str) -> int:
    """Number of decimal places shown in a printed price."""
    m = FLAG_RE.match(token.strip())
    if not m or "." not in m.group(1):
        return 0
    return len(m.group(1).split(".")[1])


def _signed(tokens, price_token=None):
    """
    Read a '<value> <sign> <magnitude>' group.

    Returns (value, change). Handles 'UNCH', missing changes, and the
    Section 11 quirk where a point change is printed without its decimal
    point ('+ 2225' against a settlement of 7692.00 means +22.25).
    """
    if not tokens:
        return None, None
    value = _num(tokens[0])
    change = None
    rest = tokens[1:]

    if rest and rest[0] == "UNCH":
        return value, 0.0
    if rest and rest[0] in ("+", "-"):
        sign = 1.0 if rest[0] == "+" else -1.0
        if len(rest) > 1:
            raw = rest[1]
            if raw == "NEW":          # newly listed contract, no prior settle
                return value, None
            mag = _num(raw)
            if mag is not None:
                # No printed decimal point: the magnitude is scaled to the
                # price's own precision.
                if "." not in raw:
                    mag = mag / (10 ** _decimals(price_token or tokens[0]))
                change = sign * mag
    elif rest and rest[0] == "UNCH":
        change = 0.0
    return value, change


def _cluster_rows(page):
    """
    Group a page's words into visual rows, then re-attach overflow fragments.

    The report writes each field into a fixed-width, right-aligned box. When a
    value is one character too wide for its box (e.g. NQ's 29276.75) the final
    character is pushed onto the next line, right-aligned to the same edge.
    Those orphans are stitched back onto the value they belong to.
    """
    buckets = defaultdict(list)
    for w in page.extract_words():
        placed = False
        for top in buckets:
            if abs(w["top"] - top) <= ROW_TOL:
                buckets[top].append(w)
                placed = True
                break
        if not placed:
            buckets[w["top"]].append(w)

    rows = []
    for top in sorted(buckets):
        rows.append((top, sorted(buckets[top], key=lambda w: w["x0"])))

    merged, i = [], 0
    while i < len(rows):
        top, words = rows[i]
        # An overflow line holds nothing but one or two bare digits.
        if (merged and words
                and all(re.fullmatch(r"\d{1,2}", w["text"]) for w in words)):
            prev_top, prev_words = merged[-1]
            if WRAP_MIN <= top - prev_top <= WRAP_MAX:
                for frag in words:
                    # Attach to the word sharing this fragment's right edge.
                    target = min(prev_words,
                                 key=lambda w: abs(w["x1"] - frag["x1"]))
                    if abs(target["x1"] - frag["x1"]) <= 1.5:
                        target["text"] += frag["text"]
                        target["x1"] = frag["x1"]
                i += 1
                continue
        merged.append((top, words))
        i += 1
    return merged


def _split_sign(text: str):
    """
    Separate a sign that got glued to its number.

    On TOTAL rows the open-interest figure and its change sign are sometimes
    typeset with no gap ('2045669+', '301987-'), so they extract as one word.
    """
    m = re.fullmatch(r"([\d,.]+)([+-])", text)
    if m:
        return [m.group(1), m.group(2)]
    m = re.fullmatch(r"([+-])([\d,.]+)", text)
    if m:
        return [m.group(1), m.group(2)]
    return [text]


def _columns(words, layout):
    """Bucket a row's words into the named columns of a layout."""
    out = defaultdict(list)
    for w in words:
        for name, lo, hi in LAYOUTS[layout]:
            if lo <= w["x0"] < hi:
                out[name].extend(_split_sign(w["text"]))
                break
    return out


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"[A-Z][a-z]{2},\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})")


def read_header(pdf) -> tuple[bool, date | None]:
    """Return (is_final, trade_date) from a bulletin's first page."""
    text = pdf.pages[0].extract_text() or ""
    head = "\n".join(text.splitlines()[:12])
    is_final = bool(re.search(r"\bFINAL\b", head))
    if re.search(r"\bPRELIMINARY\b", head):
        is_final = False
    trade_date = None
    m = _DATE_RE.search(text)
    if m:
        try:
            trade_date = datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
        except ValueError:
            pass
    return is_final, trade_date


# --------------------------------------------------------------------------
# Block extraction
# --------------------------------------------------------------------------

@dataclass
class Contract:
    month: str
    open: float | None
    settle: float | None
    settle_change: float | None
    volume: float | None
    open_interest: float | None


@dataclass
class Block:
    instrument: str
    contracts: list
    total_volume: float | None
    total_oi: float | None
    total_oi_change: float | None


def _is_block_start(words, block: str) -> bool:
    text = " ".join(w["text"] for w in words)
    return text.startswith(block + " ") or text == block


def _is_block_total(words, block: str) -> bool:
    text = " ".join(w["text"] for w in words)
    return bool(re.match(rf"^TOTAL\s+{re.escape(block)}\b", text))


def extract_block(pdf, block: str, layout: str) -> Block:
    """Pull every contract row plus the TOTAL row for one product block."""
    contracts, totals, inside = [], None, False

    for page in pdf.pages:
        if block not in (page.extract_text() or "") and not inside:
            continue
        for _top, words in _cluster_rows(page):
            if not words:
                continue
            if _is_block_total(words, block):
                cols = _columns(words, layout)
                vol = _num(cols["volume"][0]) if cols.get("volume") else None
                oi, oi_chg = _signed(cols.get("oi_grp", []))
                totals = (vol, oi, oi_chg)
                inside = False
                break
            if _is_block_start(words, block):
                inside = True
                continue
            if not inside:
                continue
            if not MONTH_RE.match(words[0]["text"]):
                continue                      # page furniture inside a block

            cols = _columns(words, layout)
            sett_tokens = cols.get("sett_grp", [])
            settle, chg = _signed(sett_tokens,
                                  sett_tokens[0] if sett_tokens else None)
            contracts.append(Contract(
                month=words[0]["text"],
                open=_num(cols["open"][0]) if cols.get("open") else None,
                settle=settle,
                settle_change=chg,
                volume=_num(cols["volume"][0]) if cols.get("volume") else None,
                open_interest=(_signed(cols.get("oi_grp", []))[0]),
            ))
        if totals is not None:
            break

    if totals is None:
        raise BulletinError(f"No TOTAL row found for block {block!r}")
    return Block(block, contracts, *totals)


def front_contract(block: Block) -> Contract:
    """
    The first listed contract that actually traded.

    The bulletin lists an expiring month first even when it is dormant (all
    '----'). Skipping rows with no opening price reproduces the convention of
    taking "the first or second row depending on whether rollover happened".
    """
    for c in block.contracts:
        if c.open is not None and c.settle is not None:
            return c
    raise BulletinError(f"No tradeable front contract in {block.instrument!r}")


def most_liquid_contract(block: Block) -> Contract:
    """The contract carrying the largest open interest."""
    live = [c for c in block.contracts if c.open_interest and c.settle]
    if not live:
        raise BulletinError(f"No contract with open interest in {block.instrument!r}")
    return max(live, key=lambda c: c.open_interest)


# --------------------------------------------------------------------------
# Derived measures
# --------------------------------------------------------------------------

def delta_rth(contract: Contract):
    """
    (settlement - session open) / session open, as a percentage.

    One rule for all six instruments: the open is the first price CME
    publishes for that product's own trading session. Sections 61 and 62
    label that column GLOBEX OPEN; Section 11 labels the E-mini S&P block
    PIT OPEN RANGE, but that label is vestigial - the S&P pit closed years
    ago, and the block's volume (1.0m against 2.0m open interest, and 2.05x
    the Nasdaq's) is plainly whole-session, not open-outcry. The published
    session open is therefore the same measurement in every section.

    This is not order-flow delta, which the bulletin does not carry. It
    differs from the settlement change by exactly the gap between the prior
    settlement and the session open.
    """
    if contract.open in (None, 0) or contract.settle is None:
        return None
    return (contract.settle - contract.open) / contract.open * 100.0


def settle_pct(contract: Contract):
    if contract.settle is None or contract.settle_change is None:
        return None
    prior = contract.settle - contract.settle_change
    if not prior:
        return None
    return contract.settle_change / prior * 100.0


# Signal thresholds, as percentages. Tune these to taste.
QUIET_PCT = 0.30    # below this the day is noise, whatever open interest did
STRONG_PCT = 1.50   # at or above this the price move stands on its own


def signal(sett_pct, oi_change, d_rth,
           quiet: float = QUIET_PCT, strong: float = STRONG_PCT) -> str:
    """
    Direction, confirmed by where open interest went.

    A move backed by RISING open interest is new money taking a side and
    earns a directional call. The same move on FALLING open interest is
    existing positions closing out - a short-covering rally or a long
    liquidation break - which reads as Sideways. Note this turns on the sign
    of the open-interest change alone, not on whether it agrees with the
    price: positions closing is weak evidence in either direction.

    Two overrides sit on top:

    * Below `quiet` the day is noise and nothing else matters.
    * At or above `strong` the price move is decisive enough to stand on its
      own. A 3% collapse is information about the market whether it came from
      new shorts or from longs running for the exit, so open interest gets no
      veto there.

    Delta RTH vetoes throughout: if the session itself travelled against the
    close-to-close change, the move happened overnight and the day's own
    trading did not confirm it.
    """
    if sett_pct is None:
        return "Sideways"
    if abs(sett_pct) < quiet:
        return "Sideways"

    direction = 1 if sett_pct > 0 else -1
    call = "Bullish" if direction > 0 else "Bearish"

    if d_rth is not None and d_rth != 0 and (d_rth > 0) != (direction > 0):
        return "Sideways"                      # overnight gap, not session-driven
    if abs(sett_pct) >= strong:
        return call
    if oi_change is None or oi_change <= 0:
        return "Sideways"                      # covering / liquidation / flat
    return call


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------

@dataclass
class Row:
    instrument: str
    trade_date: date
    contract: str
    volume_total: float
    oi_total: float
    oi_change: float
    settlement: float
    settlement_change: float
    settlement_pct: float
    session_open: float          # the open Delta RTH is measured from
    delta_rth: float
    signal: str


def extract(paths: dict, require_final: bool = True,
            picker=most_liquid_contract) -> list:
    """
    paths: {'energy': <pdf path>, 'metals': ..., 'equity': ...}
    Returns one Row per instrument in INSTRUMENTS order.
    """
    pdfs, dates = {}, {}
    for key, path in paths.items():
        pdf = pdfplumber.open(path)
        is_final, trade_date = read_header(pdf)
        if require_final and not is_final:
            raise BulletinError(f"{key}: bulletin is not FINAL yet")
        if trade_date is None:
            raise BulletinError(f"{key}: could not read trade date from header")
        pdfs[key], dates[key] = pdf, trade_date

    if len(set(dates.values())) != 1:
        raise BulletinError(f"Sections disagree on trade date: {dates}")
    trade_date = next(iter(dates.values()))

    rows = []
    for ticker, spec in INSTRUMENTS.items():
        block = extract_block(pdfs[spec["section"]], spec["block"], spec["layout"])
        c = picker(block)
        pct, d = settle_pct(c), delta_rth(c)
        rows.append(Row(
            instrument=ticker,
            trade_date=trade_date,
            contract=c.month,
            volume_total=block.total_volume,
            oi_total=block.total_oi,
            oi_change=block.total_oi_change,
            settlement=c.settle,
            settlement_change=c.settle_change,
            settlement_pct=pct,
            session_open=c.open,
            delta_rth=d,
            signal=signal(pct, block.total_oi_change, d),
        ))
    for pdf in pdfs.values():
        pdf.close()
    return rows
