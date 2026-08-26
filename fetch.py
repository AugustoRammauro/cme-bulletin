"""
Download today's bulletin sections.

Two obstacles, both handled here.

1. Caching. The bulletin at /current/ is overwritten in place through the
   day, so an intermediary holding a PRELIMINARY copy would otherwise keep
   serving it after the FINAL one is published.

2. Bot filtering. cmegroup.com sits behind Akamai, which rejects requests
   that don't look like a browser with 403 Forbidden. A plain urllib request
   with a library-ish User-Agent is refused. Sending a complete, coherent set
   of browser headers gets served normally, so we try a few profiles in turn
   and report which one worked.
"""

from __future__ import annotations

import gzip
import io
import pathlib
import time
import urllib.error
import urllib.request
import zlib

BASE = "https://www.cmegroup.com/daily_bulletin/current/"
REFERER = "https://www.cmegroup.com/tools-information/daily-bulletin.html"

NO_CACHE = {
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}

# Header sets are tried in order. Each must be internally consistent - a
# Chrome User-Agent paired with Firefox's Sec-Fetch headers is itself a
# fingerprint, so each profile is copied from one real browser.
PROFILES = [
    ("chrome-windows", {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": REFERER,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }),
    ("firefox-windows", {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) "
                       "Gecko/20100101 Firefox/129.0"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Referer": REFERER,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }),
    ("safari-mac", {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.6 Safari/605.1.15"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": REFERER,
        "Connection": "keep-alive",
    }),
]


class FetchError(RuntimeError):
    pass


def _decode(resp) -> bytes:
    """Read a response, undoing any content encoding."""
    raw = resp.read()
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _try(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers={**headers, **NO_CACHE})
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = _decode(resp)
    if not body.startswith(b"%PDF"):
        raise FetchError(f"expected a PDF, got {len(body)} bytes "
                         f"starting {body[:60]!r}")
    return body


def fetch(section_file: str, dest_dir: pathlib.Path,
          attempts: int = 2, verbose: bool = True) -> pathlib.Path:
    """Download one section PDF and return the local path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{section_file}.pdf"
    tried = []

    for name, headers in PROFILES:
        for n in range(attempts):
            # A unique query string means no cache anywhere can match this.
            url = f"{BASE}{section_file}.pdf?nocache={int(time.time() * 1000)}"
            try:
                body = _try(url, headers)
                dest.write_bytes(body)
                if verbose:
                    print(f"    {section_file}: ok via {name} "
                          f"({len(body):,} bytes)", flush=True)
                return dest
            except urllib.error.HTTPError as exc:
                tried.append(f"{name}: HTTP {exc.code} {exc.reason}")
                if exc.code in (401, 403, 451):
                    break              # refused - a retry won't change it
                time.sleep(2 ** n)
            except Exception as exc:   # noqa: BLE001 - recorded and retried
                tried.append(f"{name}: {type(exc).__name__}: {exc}")
                time.sleep(2 ** n)

    detail = "\n      ".join(tried)
    raise FetchError(
        f"{section_file}: every request was refused.\n      {detail}\n"
        f"    All browser profiles failed, so this is unlikely to be the "
        f"User-Agent. The host is probably refusing this network."
    )


def fetch_all(sections: dict, dest_dir: pathlib.Path) -> dict:
    """sections: {'energy': 'Section61_...', ...} -> {'energy': Path, ...}"""
    return {key: fetch(name, dest_dir) for key, name in sections.items()}
