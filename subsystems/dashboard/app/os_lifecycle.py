"""
os_lifecycle.py — Windows and macOS end-of-support dates, kept current
automatically.

Dates come from endoflife.date (a public, community-maintained tracker of
Microsoft's lifecycle pages and Apple's security-update history), downloaded at most once a day by the update
watcher (routes/home.py) and cached in CACHE_PATH, so a new Windows release
gets its date without a code change. If the API is unreachable the last
cached copy keeps being used; with no cache at all, fleet.py falls back to
its built-in table.

lookup(os_name, os_build) matches a device to a release record:
  * Windows Server — by year in the name ("2019", "2012 R2"), else by build
  * Windows client — by build number, then edition:
      LTSC/LTSB -> the LTSC record (IoT LTSC -> IoT), Enterprise/Education ->
      "(E)", everything else (Home, Pro, Pro Education, Pro for Workstations)
      -> "(W)"; older releases without the split have a single record
  * macOS — by major version ("MacOS 26.6.2" -> 26, "10.15.7" -> 10.15).
    Apple never announces end dates: endoflife.date marks the releases still
    getting security updates (usually the newest three) as not ended, with no
    date, and records the date each older one stopped. So a supported Mac has
    no end date to count down to; it moves to "ended" once Apple drops it.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone

from app import paths

CACHE_PATH = paths.OS_LIFECYCLE_CACHE
SOURCE = "endoflife.date"
_URLS = {
    "windows": "https://endoflife.date/api/windows.json",
    "windows-server": "https://endoflife.date/api/windows-server.json",
    "macos": "https://endoflife.date/api/macos.json",
}
REFRESH_EVERY_S = 24 * 3600
_TIMEOUT_S = 20

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime = 0.0


# ── Fetch & cache ────────────────────────────────────────────────────────────

def _fetch(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "rustdesk-fleet-dashboard", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = json.loads(resp.read(2_000_000))
    rows = [r for r in data if isinstance(r, dict) and r.get("cycle") and "eol" in r and r.get("latest")]
    if len(rows) < 5:
        raise ValueError(f"unexpected response from {url}")
    return [{k: r.get(k) for k in ("cycle", "releaseLabel", "latest", "releaseDate", "eol", "lts")} for r in rows]


def refresh(force: bool = False) -> tuple[bool, str]:
    """Download fresh data if the cache is older than a day (or force).
    Returns (changed, message). Never raises."""
    current = load()
    age = time.time() - (current or {}).get("fetched_ts", 0)
    complete = current and set(current.get("products", {})) >= set(_URLS)
    if complete and age < REFRESH_EVERY_S and not force:
        return False, "cache is fresh"
    try:
        products = {name: _fetch(url) for name, url in _URLS.items()}
    except Exception as exc:
        return False, f"download failed: {exc}"
    now = datetime.now(timezone.utc)
    new = {"source": SOURCE, "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "fetched_ts": now.timestamp(), "products": products}
    changed = not current or current.get("products") != products
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(new))
    tmp.replace(CACHE_PATH)
    return changed, "updated" if changed else "no changes"


def load() -> dict | None:
    """The cached data (re-read when the file changes), or None."""
    global _cache, _cache_mtime
    try:
        mtime = CACHE_PATH.stat().st_mtime
    except OSError:
        return None
    with _lock:
        if _cache is None or mtime != _cache_mtime:
            try:
                _cache = json.loads(CACHE_PATH.read_text())
                _cache_mtime = mtime
            except (OSError, ValueError):
                return None
        return _cache


def info() -> dict:
    """{'source', 'fetched_at'} for display, or {} if never downloaded."""
    c = load()
    return {"source": c["source"], "fetched_at": c["fetched_at"]} if c else {}


# ── Matching ─────────────────────────────────────────────────────────────────

def _build_of(latest: str) -> str:
    parts = str(latest).split(".")
    return parts[2] if len(parts) >= 3 else ""


def _eol(rec: dict) -> tuple[str, bool | None]:
    """(date, None) for a dated record; ('', True/False) when only a flag."""
    v = rec.get("eol")
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        return v, None
    return "", bool(v)


def _pick_client(cands: list[dict], os_name: str) -> dict | None:
    if not cands:
        return None
    lts = "LTSC" in os_name or "LTSB" in os_name
    iot = "IoT" in os_name
    ent = ("Enterprise" in os_name or "Education" in os_name) and "Pro Education" not in os_name

    def score(r):
        c = r["cycle"]
        s = 0
        s += 8 if bool(r.get("lts")) == lts else 0
        s += 4 if ("iot" in c) == iot else 0
        if c.endswith("-e") or "-e-" in c:
            s += 2 if ent else -2
        elif c.endswith("-w"):
            s += 2 if not ent else -2
        return s
    return max(cands, key=score)


def lookup(os_name: str, os_build: str) -> tuple[str, bool | None] | None:
    """Match a device's OS. Returns (eol_date, eol_flag) — see _eol — or None
    when there's no data or no matching release."""
    c = load()
    if not c:
        return None
    if os_name.lower().startswith(("macos", "mac os")):
        return _lookup_macos(c, os_name)
    if not os_name.startswith("Windows"):
        return None
    build = str(os_build or "").strip()

    if os_name.startswith("Windows Server"):
        rows = c["products"].get("windows-server", [])
        m = re.search(r"Server (\d{4})( R2)?", os_name)
        if m:
            key = m.group(1) + ("-r2" if m.group(2) else "")
            hits = [r for r in rows if r["cycle"] == key] or \
                   [r for r in rows if r["cycle"].startswith(key + "-") and not r["cycle"].startswith(key + "-r2")]
            if hits:
                return _eol(hits[0])
        hits = [r for r in rows if build and _build_of(r["latest"]) == build]
        return _eol(hits[0]) if hits else None

    rows = c["products"].get("windows", [])
    if build:
        rec = _pick_client([r for r in rows if _build_of(r["latest"]) == build], os_name)
        if rec:
            return _eol(rec)
    # No build reported: match old releases by name ("Windows 8.1", "Windows 7").
    for prefix, cycle in (("Windows 8.1", "8.1"), ("Windows 8", "8"), ("Windows 7", "7-sp1")):
        if os_name.startswith(prefix):
            hits = [r for r in rows if r["cycle"] == cycle]
            return _eol(hits[0]) if hits else None
    return None


def _lookup_macos(c: dict, os_name: str) -> tuple[str, bool | None] | None:
    m = re.search(r"(\d+)(?:\.(\d+))?", os_name)
    if not m:
        return None
    major = int(m.group(1))
    cycle = f"10.{m.group(2)}" if major == 10 and m.group(2) else str(major)
    hits = [r for r in c["products"].get("macos", []) if str(r["cycle"]) == cycle]
    return _eol(hits[0]) if hits else None
