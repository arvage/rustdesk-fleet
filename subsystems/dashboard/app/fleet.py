"""
fleet.py — one enriched snapshot of every device, shared by Dynamic Groups
(routes/dynamic_groups.py) and Reports (routes/reports.py).

get_devices() gives the display fields; this adds the derived facts those
features filter and report on: OS support status, RAM/cores as numbers, live
status, days since last seen, and the last remote session.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from app.deps import get_db, get_devices

# ── OS end-of-support dates ─────────────────────────────────────────────────
# The live source is app/os_lifecycle.py (endoflife.date, refreshed daily), so
# new Windows releases get their dates automatically. The table below is only
# the FALLBACK for when that data has never been downloaded or has no match.
# Matched by name prefix, first hit wins, so the more specific names come
# first. Windows 11 is per feature release (by build); Enterprise/Education
# editions get an extra year.
_EOS_BY_PREFIX = [
    ("Windows Server 2025", "2034-10-10"),
    ("Windows Server 2022", "2031-10-14"),
    ("Windows Server 2019", "2029-01-09"),
    ("Windows Server 2016", "2027-01-12"),
    ("Windows Server 2012", "2023-10-10"),   # also 2012 R2
    ("Windows Server 2008", "2020-01-14"),   # also 2008 R2
    ("Windows 10", "2025-10-14"),
    ("Windows 8.1", "2023-01-10"),
    ("Windows 8", "2016-01-12"),
    ("Windows 7", "2020-01-14"),
]
# Windows 11 build -> (Home/Pro end, Enterprise/Education end)
_WIN11_EOS_BY_BUILD = {
    22000: ("2023-10-10", "2024-10-08"),   # 21H2
    22621: ("2024-10-08", "2025-10-14"),   # 22H2
    22631: ("2025-11-11", "2026-11-10"),   # 23H2
    26100: ("2026-10-13", "2027-10-12"),   # 24H2
    26200: ("2027-10-12", "2028-10-10"),   # 25H2
}
EOS_SOON_DAYS = 182   # "ending soon" = within ~6 months

OS_SUPPORT_LABELS = {
    "ended": "End of support",
    "ending_soon": "Ending within 6 months",
    "supported": "Supported",
    "unknown": "Unknown",
}


def _fallback_end_of_support(os_name: str, os_build: str) -> str:
    """Built-in end-of-support date (YYYY-MM-DD), or '' if unknown."""
    if not os_name or "LTSC" in os_name or "LTSB" in os_name:
        return ""
    if os_name.startswith("Windows 11"):
        try:
            dates = _WIN11_EOS_BY_BUILD.get(int(os_build))
        except (TypeError, ValueError):
            dates = None
        if not dates:
            return ""
        return dates[1] if ("Enterprise" in os_name or "Education" in os_name) else dates[0]
    for prefix, eos in _EOS_BY_PREFIX:
        if os_name.startswith(prefix):
            return eos
    return ""


def _newer_than_known_win11(os_name: str, os_build: str) -> bool:
    """A Windows 11 build newer than any release in the table (e.g. Insider)."""
    try:
        return os_name.startswith("Windows 11") and int(os_build) > max(_WIN11_EOS_BY_BUILD)
    except (TypeError, ValueError):
        return False


def os_support(os_name: str, os_build: str, today: date | None = None) -> tuple[str, str]:
    """(end-of-support date or '', status) for a device's OS. Uses the live
    lifecycle data first, then the built-in table."""
    from app import os_lifecycle
    hit = os_lifecycle.lookup(os_name or "", os_build or "")
    if hit is not None:
        eos, flag = hit
        if eos:
            return eos, os_support_status(eos, today)
        return "", ("ended" if flag else "supported")
    eos = _fallback_end_of_support(os_name or "", os_build or "")
    if not eos and _newer_than_known_win11(os_name or "", os_build or ""):
        return "", "supported"
    return eos, os_support_status(eos, today)


def os_support_status(eos: str, today: date | None = None) -> str:
    if not eos:
        return "unknown"
    today = today or date.today()
    days_left = (date.fromisoformat(eos) - today).days
    if days_left < 0:
        return "ended"
    if days_left <= EOS_SOON_DAYS:
        return "ending_soon"
    return "supported"


# ── Number/date parsing ──────────────────────────────────────────────────────

def _first_number(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else None


def parse_dt(s: str) -> datetime | None:
    """UTC 'YYYY-MM-DD[ HH:MM:SS]' -> aware datetime, or None for blank/'—'."""
    s = (s or "").strip()
    if not s or s == "—":
        return None
    try:
        clean = s.replace("T", " ").split(".")[0][:19]
        fmt = "%Y-%m-%d %H:%M:%S" if len(clean) > 10 else "%Y-%m-%d"
        return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def days_since(s: str) -> int | None:
    dt = parse_dt(s)
    if dt is None:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


# ── Snapshot ─────────────────────────────────────────────────────────────────

def _last_sessions(tenant_id: int) -> dict[str, str]:
    conn = get_db()
    rows = conn.execute(
        "SELECT rustdesk_id, MAX(started_at) AS last FROM device_sessions WHERE tenant_id = ? GROUP BY rustdesk_id",
        (tenant_id,),
    ).fetchall()
    conn.close()
    return {r["rustdesk_id"]: r["last"] for r in rows}


def _live_status(tenant) -> dict[str, str]:
    try:
        from app.routes.api import _compute_status
        return _compute_status(tenant)
    except Exception:
        return {}


def fleet_snapshot(tenant) -> list[dict]:
    """Every visible device of one tenant (label order) with derived fields
    added: ram_gb, cores_n, os_eos, os_support, live_status, last_seen_days,
    registered_days, last_session, last_session_days."""
    devices, _ = get_devices(tenant)
    sessions = _last_sessions(tenant.id)
    status = _live_status(tenant)
    today = date.today()
    for d in devices:
        rid = d["rustdesk_id"]
        d["ram_gb"] = _first_number(d.get("ram", ""))
        d["cores_n"] = _first_number(d.get("cores", ""))
        d["os_eos"], d["os_support"] = os_support(d.get("os_name", ""), d.get("os_build", ""), today)
        d["live_status"] = status.get(rid, "offline")
        # Fleet last_seen is bumped on every heartbeat; fall back to the raw
        # heartbeat for devices that never got a fleet row.
        seen = d.get("last_seen") if d.get("last_seen") not in ("", "—") else d.get("heartbeat_at", "")
        d["last_seen_days"] = days_since(seen)
        d["registered_days"] = days_since(d.get("registered_at", ""))
        d["last_session"] = sessions.get(rid, "")
        d["last_session_days"] = days_since(d["last_session"])
    return devices


def device_name(d: dict) -> str:
    return d.get("label") or d.get("hostname") or d.get("rustdesk_id", "")
