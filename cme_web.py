"""
CME's own product-page endpoints — the self-heal source.

The daily pipeline reads the bulletin PDFs, which only ever describe the
current trade date. So any day the pipeline misses is gone the moment the
next bulletin replaces it. These endpoints are the only free source that
still serves a few days back, which is what makes gaps recoverable without
anyone noticing them.

Lineage: the request recipe here (cookie warming, browser headers, the two
endpoint shapes, the product ids) comes from backfill_cme.py, the manual
repair script, where it was developed against the live site. The Signal and
contract-code logic is imported from cme_bulletin rather than copied, so the
two sources cannot drift apart.

Caveat worth remembering: open interest here comes from the Volume endpoint's
totals and does NOT always match the bulletin. On 15 Oct gold's OI change was
+1,411 here against -1,002 in the bulletin. Rows filled from this source are
tagged 'cme-web' in the Source column for exactly that reason.
"""

from __future__ import annotations

import datetime as dt
import gzip
import http.cookiejar
import io
import json
import time
import urllib.error
import urllib.request

import cme_bulletin as cb
import fetch

BASE = "https://www.cmegroup.com"

# product id, and the page visited first so the session carries the cookies a
# browser would have. Without that warm-up the JSON endpoints refuse.
PRODUCTS = {
    "GC": (437, "/markets/metals/precious/gold"),
    "SI": (458, "/markets/metals/precious/silver"),
    "CL": (425, "/markets/energy/crude-oil/light-sweet-crude"),
    "ES": (133, "/markets/equities/sp/e-mini-sandp500"),
    "NQ": (146, "/markets/equities/nasdaq/e-mini-nasdaq-100"),
    "YM": (318, "/markets/equities/dow-jones/e-mini-dow"),
}


class WebError(RuntimeError):
    pass


def num(s):
    """CME writes numbers as strings with commas, '+' signs and '-' for null."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("+", "")
    if t in ("", "-", "--", "----", "N/A", "UNCH"):
        return 0.0 if t == "UNCH" else None
    try:
        return float(t)
    except ValueError:
        return None


def _pick(row: dict, *keys):
    """First present, non-empty value among several candidate field names."""
    for k in keys:
        if k in row:
            v = num(row[k])
            if v is not None:
                return v
    return None


class Cme:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.warmed = {}          # page -> the header profile that worked
        self.warm_log = []
        self.seen_settlement_keys = None   # logged once, to confirm field names

    def _get(self, url, headers):
        req = urllib.request.Request(url, headers=headers)
        with self.opener.open(req, timeout=60) as r:
            return fetch._decode(r)          # handles gzip and deflate

    def _xhr_headers(self, profile: dict, page: str) -> dict:
        """
        XHR headers that stay consistent with the browser we warmed as.

        The identity fields (User-Agent, client hints, language) must match
        the page request; only the fetch-metadata changes, because this is
        a background JSON call rather than a navigation.
        """
        carry = ("User-Agent", "Accept-Language", "Accept-Encoding",
                 "Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform",
                 "Connection")
        h = {k: v for k, v in profile.items() if k in carry}
        h.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE + page + ".settlements.html",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        return h

    def warm(self, page):
        """
        Load the settlements page so the session carries a browser's cookies.

        Uses the same complete header profiles as the bulletin downloader,
        which are known to be accepted from a GitHub runner. An abbreviated
        header set is refused with 403: Akamai judges the shape of the whole
        request, not just the User-Agent.
        """
        if page in self.warmed:
            return self.warmed[page]

        url = BASE + page + ".settlements.html"
        tried = []
        for name, headers in fetch.PROFILES:
            profile = dict(headers)
            profile["Referer"] = BASE + page + ".html"
            for attempt in range(2):
                try:
                    self._get(url, {**profile, **fetch.NO_CACHE})
                    self.warmed[page] = profile
                    self.warm_log.append(f"{page}: warmed via {name}")
                    return profile
                except urllib.error.HTTPError as exc:
                    tried.append(f"{name}: HTTP {exc.code}")
                    break                    # refused; a retry won't help
                except Exception as exc:     # noqa: BLE001 - transient
                    tried.append(f"{name}: {type(exc).__name__}")
                    time.sleep(2 + attempt * 3)
        raise WebError(f"could not open {url} ({'; '.join(tried)})")

    def json(self, path, page):
        last = None
        for attempt in range(3):
            try:
                profile = self.warm(page)
                return json.loads(self._get(BASE + path,
                                            self._xhr_headers(profile, page)))
            except WebError:
                raise                        # warming failed; already explained
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                # A 403 here but a warmed page means the cookie went stale.
                self.warmed.pop(page, None)
                time.sleep(2 + attempt * 3)
            except (urllib.error.URLError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(2 + attempt * 3)
        raise WebError(f"CME did not answer {path}: {last}")

    def settlements(self, pid, page, day):
        p = (f"/CmeWS/mvc/Settlements/Futures/Settlements/{pid}/FUT"
             f"?strategy=DEFAULT&tradeDate={day:%m/%d/%Y}&pageSize=500")
        rows = self.json(p, page).get("settlements") or []
        if rows and self.seen_settlement_keys is None:
            self.seen_settlement_keys = sorted(rows[0].keys())
        return rows

    def volume(self, pid, page, day):
        d = f"{day:%Y%m%d}"
        p = f"/CmeWS/mvc/Volume/Details/F/{pid}/{d}/P?tradeDate={d}&pageSize=500"
        return self.json(p, page)


def build_row(inst: str, day: dt.date, vol_json: dict, settle_rows: list):
    """
    One history row for this instrument/day, or (None, reason).

    Mirrors the bulletin rules: the contract with the largest open interest,
    Globex volume only, and the shared Signal implementation.
    """
    totals = (vol_json or {}).get("totals") or {}
    months = (vol_json or {}).get("monthData") or []
    if not months or num(totals.get("globex")) is None:
        return None, "no volume / open-interest data"
    if not settle_rows:
        return None, "no settlement prices (CME no longer serves this day)"

    # CME mixes a "Total" summary row in with the contract months, and it
    # would otherwise win the max() below and then fail to parse.
    months = [m for m in months
              if len(str(m.get("month", "")).split()) == 2
              and str(m["month"]).split()[0].isalpha()]
    if not months:
        return None, "no contract months in the volume data"

    top = max(months, key=lambda m: num(m.get("atClose")) or 0)
    mon, yr = top["month"].split()                  # "DEC 2026"
    short = f"{mon} {yr[-2:]}"                      # Settlements writes "DEC 26"
    srow = next((r for r in settle_rows if r.get("month") == short), None)
    if srow is None:
        return None, f"no settlement row for {short}"

    settle = _pick(srow, "settle")
    change = _pick(srow, "change")
    opn = _pick(srow, "open")
    # Confirmed live: a settlements entry carries month, open, high, low,
    # last, change, settle, volume, openInterest. Expiring months show "-",
    # which num() reads as missing.
    high = _pick(srow, "high")
    low = _pick(srow, "low")
    if settle is None or change is None:
        return None, f"settlement for {short} not published"

    prior = settle - change
    sett_pct = change / prior * 100 if prior else None
    d_rth = (settle - opn) / opn * 100 if opn else None
    oi_total = num(totals.get("atClose"))
    oi_chg = num(totals.get("change"))

    row = [
        inst,
        f"{day:%d/%m/%Y}",
        f"{num(totals['globex']):.0f}",
        f"{oi_total:.0f}" if oi_total is not None else "",
        f"{oi_chg:.0f}" if oi_chg is not None else "",
        f"{settle:.10g}",
        f"{round(change, 6):.10g}",
        f"{sett_pct / 100:.6f}" if sett_pct is not None else "",
        f"{d_rth / 100:.6f}" if d_rth is not None else "",
        cb.signal(sett_pct, oi_chg, d_rth),
        cb.contract_code(inst, f"{mon}{yr[-2:]}"),
        f"{opn:.10g}" if opn is not None else "",
        f"{high:.10g}" if high is not None else "",
        f"{low:.10g}" if low is not None else "",
        "cme-web",
    ]
    return row, None


def trade_days_back(today: dt.date, n: int) -> list:
    """The n most recent weekdays strictly before `today`, oldest first."""
    out, d = [], today - dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def fetch_missing(missing: dict, client=None):
    """
    missing: {instrument: [date, ...]}
    Returns (rows, problems). Never raises for a single day's failure.
    """
    cme = client or Cme()
    rows, problems = [], []
    for inst, days in missing.items():
        pid, page = PRODUCTS[inst]
        for day in days:
            try:
                vol = cme.volume(pid, page, day)
                settle = cme.settlements(pid, page, day)
            except Exception as exc:                 # noqa: BLE001
                problems.append(f"{inst} {day:%d/%m}: {exc}")
                continue
            try:
                row, why = build_row(inst, day, vol, settle)
            except Exception as exc:                 # noqa: BLE001
                problems.append(f"{inst} {day:%d/%m}: bad data ({exc})")
                continue
            if row:
                rows.append(row)
            elif why and "no volume" not in why:     # holidays have no volume
                problems.append(f"{inst} {day:%d/%m}: {why}")
    return rows, problems, cme
