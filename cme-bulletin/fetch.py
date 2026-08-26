"""Download today's bulletin sections, defeating CDN caching."""

from __future__ import annotations

import pathlib
import time
import urllib.request

BASE = "https://www.cmegroup.com/daily_bulletin/current/"

HEADERS = {
    # The bulletin at .../current/ is overwritten in place through the day,
    # so an intermediary holding a PRELIMINARY copy would otherwise serve it
    # long after the FINAL one is published.
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
    "User-Agent": "Mozilla/5.0 (compatible; bulletin-reader/1.0)",
}


class FetchError(RuntimeError):
    pass


def fetch(section_file: str, dest_dir: pathlib.Path, attempts: int = 3) -> pathlib.Path:
    """Download one section PDF and return the local path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{section_file}.pdf"
    # A unique query string means no cache along the way can match this request.
    url = f"{BASE}{section_file}.pdf?nocache={int(time.time() * 1000)}"

    last = None
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read()
            if not body.startswith(b"%PDF"):
                raise FetchError(f"{section_file}: response is not a PDF "
                                 f"({body[:40]!r})")
            dest.write_bytes(body)
            return dest
        except Exception as exc:              # noqa: BLE001 - retried below
            last = exc
            time.sleep(2 ** n)
    raise FetchError(f"{section_file}: download failed after {attempts} "
                     f"attempts: {last}")


def fetch_all(sections: dict, dest_dir: pathlib.Path) -> dict:
    """sections: {'energy': 'Section61_...', ...} -> {'energy': Path, ...}"""
    return {key: fetch(name, dest_dir) for key, name in sections.items()}
