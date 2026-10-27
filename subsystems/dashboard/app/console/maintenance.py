"""The provider console's periodic work, in one background thread:

- tenant_usage_daily: one row per tenant per day (devices, technicians,
  remote-session minutes), so usage history survives deletions and purges;
  billing will read it later;
- the certificate expiry of every tenant web domain (read over TLS);
- lifecycle: purge-warning emails and the purge of tenants past their grace
  period (console/lifecycle.py).
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

from app import paths
from app.deps import get_db

USAGE_SQL = """
    CREATE TABLE IF NOT EXISTS tenant_usage_daily (
        day             TEXT NOT NULL,              -- YYYY-MM-DD (UTC)
        tenant_id       INTEGER NOT NULL,           -- no foreign key: rows outlive purged tenants' data
        devices         INTEGER NOT NULL DEFAULT 0,
        technicians     INTEGER NOT NULL DEFAULT 0,
        session_minutes INTEGER NOT NULL DEFAULT 0,
        recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (day, tenant_id)
    );
"""

INTERVAL_S = 3600


def ensure_table(conn) -> None:
    conn.executescript(USAGE_SQL)


def session_minutes(conn, tenant_id: int, start: str, end: str) -> int:
    """Minutes of remote sessions overlapping [start, end) (UTC 'YYYY-MM-DD HH:MM:SS')."""
    row = conn.execute(
        """SELECT COALESCE(SUM(MAX(0, (julianday(MIN(COALESCE(ended_at, datetime('now')), ?))
                                       - julianday(MAX(started_at, ?))) * 1440)), 0)
           FROM device_sessions WHERE tenant_id = ? AND started_at < ? AND COALESCE(ended_at, datetime('now')) > ?""",
        (end, start, tenant_id, end, start),
    ).fetchone()
    return int(round(row[0] or 0))


def record_usage(day: date | None = None) -> int:
    """Write (or refresh) one tenant_usage_daily row per live tenant for `day`
    (default: today, UTC). Returns the number of rows written."""
    day = day or datetime.now(timezone.utc).date()
    start = f"{day.isoformat()} 00:00:00"
    end = f"{(day + timedelta(days=1)).isoformat()} 00:00:00"
    conn = get_db()
    n = 0
    for (tid,) in conn.execute("SELECT id FROM tenants WHERE status <> 'purged'").fetchall():
        devices = conn.execute("SELECT COUNT(*) FROM devices WHERE tenant_id = ? AND hidden = 0", (tid,)).fetchone()[0]
        techs = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tid,)).fetchone()[0]
        conn.execute(
            """INSERT INTO tenant_usage_daily (day, tenant_id, devices, technicians, session_minutes)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(day, tenant_id) DO UPDATE SET devices = excluded.devices,
                   technicians = excluded.technicians, session_minutes = excluded.session_minutes,
                   recorded_at = datetime('now')""",
            (day.isoformat(), tid, devices, techs, session_minutes(conn, tid, start, end)))
        n += 1
    conn.commit()
    conn.close()
    return n


def refresh_cert_expiry() -> None:
    from app.console import web
    conn = get_db()
    hosts = [r[0] for r in conn.execute(
        "SELECT hostname FROM tenant_domains WHERE kind IN ('provider_subdomain','custom')")]
    conn.close()
    for host in hosts:
        exp = web.cert_expiry(host)
        if exp:
            conn = get_db()
            conn.execute("UPDATE tenant_domains SET cert_expires_at = ? WHERE hostname = ?", (exp, host))
            conn.commit()
            conn.close()


def run_once() -> None:
    today = datetime.now(timezone.utc).date()
    for step in (lambda: record_usage(today - timedelta(days=1)) if _missing(today - timedelta(days=1)) else None,
                 lambda: record_usage(today),
                 refresh_cert_expiry,
                 _lifecycle):
        try:
            step()
        except Exception:
            pass


def _missing(day: date) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM tenant_usage_daily WHERE day = ? LIMIT 1", (day.isoformat(),)).fetchone()
    conn.close()
    return row is None


def _lifecycle() -> None:
    from app.console import lifecycle
    lifecycle.daily()


_started = False


def start() -> None:
    global _started
    if _started or not paths.WATCHERS_ENABLED:
        return
    _started = True

    def loop():
        time.sleep(120)
        while True:
            run_once()
            time.sleep(INTERVAL_S)

    threading.Thread(target=loop, daemon=True, name="provider-maintenance").start()
