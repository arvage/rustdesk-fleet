"""Support access: provider staff in a tenant's dashboard, only with the
tenant's consent and only until the time the tenant chose.

- The tenant (manage_support_access, built-in admins) grants access for a
  period, optionally including unattended-access passwords. One active grant
  at a time; granting again changes its end, note or password flag.
- With a grant active, a provider admin opens the dashboard from the console:
  the console stores a single-use token (hashed, valid TOKEN_TTL_S) and sends
  the browser to https://<tenant domain>/support/enter?token=…, which starts a
  support session in the tenant's own cookie. No row is ever added to the
  tenant's users table.
- Every request in a support session re-checks the grant, the session, the
  4-hour cap (SUPPORT_SESSION_MAX_AGE) and the provider user (enabled, same
  session_version). Anything off ends the session.
- A support session acts as an admin except for SUPPORT_FORBIDDEN_PATHS and
  the capabilities in SUPPORT_WITHHELD_PERMS, and never sees unattended
  passwords unless the grant includes them. Its audit entries are
  attributed to "support:<provider email>".
"""
from __future__ import annotations

import hashlib
import secrets
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.deps import get_db, log_event

TOKEN_TTL_S = 60
SUPPORT_SESSION_MAX_AGE = 4 * 3600
MAX_GRANT_DAYS = 366          # "12 months", allowing for a leap year
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")   # what the dashboard shows times in

DURATIONS = {                 # form value -> label
    "4h": "4 hours", "1d": "1 day", "3d": "3 days", "7d": "7 days", "30d": "30 days",
    "90d": "90 days", "6m": "6 months", "12m": "12 months",
}

# Paths a support session may never use, whatever its permissions: user,
# role and support-access management, the user's own account (support has
# none), and the cancelled-tenant export page (that's for the tenant).
SUPPORT_FORBIDDEN_PATHS = ("/users", "/admin/roles", "/admin/support", "/account", "/export")
SUPPORT_WITHHELD_PERMS = {"manage_users", "manage_roles", "manage_support_access"}

SCHEMA = """
    -- A tenant's consent to provider support access, until ends_at.
    CREATE TABLE IF NOT EXISTS support_grants (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id         INTEGER NOT NULL REFERENCES tenants(id),
        granted_by        INTEGER NOT NULL,          -- users.id of the tenant admin
        granted_by_email  TEXT NOT NULL DEFAULT '',
        starts_at         TEXT NOT NULL,
        ends_at           TEXT NOT NULL,
        note              TEXT NOT NULL DEFAULT '',
        include_passwords INTEGER NOT NULL DEFAULT 0,
        revoked_at        TEXT,
        revoked_by        TEXT,                      -- email of whoever revoked it
        expiry_notified_at TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_support_grants_tenant ON support_grants(tenant_id, ends_at);

    -- One-time sign-in tokens issued by the console (hash only).
    CREATE TABLE IF NOT EXISTS support_tokens (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        token_hash        TEXT NOT NULL UNIQUE,
        provider_user_id  INTEGER NOT NULL,
        tenant_id         INTEGER NOT NULL REFERENCES tenants(id),
        grant_id          INTEGER NOT NULL,
        reason            TEXT NOT NULL,
        expires_at        TEXT NOT NULL,
        used_at           TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Support sessions in a tenant's dashboard.
    CREATE TABLE IF NOT EXISTS support_sessions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id         INTEGER NOT NULL REFERENCES tenants(id),
        grant_id          INTEGER NOT NULL,
        provider_user_id  INTEGER NOT NULL,
        provider_email    TEXT NOT NULL DEFAULT '',
        provider_name     TEXT NOT NULL DEFAULT '',
        reason            TEXT NOT NULL,
        started_at        TEXT NOT NULL DEFAULT (datetime('now')),
        ended_at          TEXT,
        end_reason        TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_support_sessions_tenant ON support_sessions(tenant_id, ended_at);
"""

SUPPORT_TABLES = ("support_grants", "support_tokens", "support_sessions")


class SupportError(Exception):
    pass


def ensure_tables(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    year, month = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, monthrange(year, month)[1]))


def end_time(duration: str, custom: str = "", now: datetime | None = None) -> datetime:
    """When a grant chosen as `duration` (a DURATIONS key, or "custom" with a
    YYYY-MM-DDTHH:MM in the dashboard's time zone) ends."""
    now = now or _now()
    if duration == "custom":
        try:
            local = datetime.strptime(custom.strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=DISPLAY_TZ)
        except ValueError:
            raise SupportError("Pick a valid end date and time.")
        end = local.astimezone(timezone.utc)
    elif duration.endswith("h") and duration in DURATIONS:
        end = now + timedelta(hours=int(duration[:-1]))
    elif duration.endswith("d") and duration in DURATIONS:
        end = now + timedelta(days=int(duration[:-1]))
    elif duration.endswith("m") and duration in DURATIONS:
        end = _add_months(now, int(duration[:-1]))
    else:
        raise SupportError("Pick how long support can get in.")
    if end <= now:
        raise SupportError("The end must be in the future.")
    if end > now + timedelta(days=MAX_GRANT_DAYS):
        raise SupportError("Support access can be granted for at most 12 months.")
    return end


# ── Grants ───────────────────────────────────────────────────────────────────

def active_grant(conn, tenant_id: int, now: datetime | None = None):
    now = now or _now()
    return conn.execute(
        "SELECT * FROM support_grants WHERE tenant_id = ? AND revoked_at IS NULL AND starts_at <= ? AND ends_at > ?"
        " ORDER BY id DESC LIMIT 1", (tenant_id, fmt(now), fmt(now))).fetchone()


def grant(tenant_id: int, user: dict, end: datetime, note: str, include_passwords: bool) -> tuple[str, int]:
    """Create the tenant's grant, or change the active one. Returns
    ("created" | "changed", grant id)."""
    note = (note or "").strip()[:300]
    conn = get_db()
    try:
        current = active_grant(conn, tenant_id)
        if current:
            conn.execute("UPDATE support_grants SET ends_at = ?, note = ?, include_passwords = ?,"
                         " updated_at = datetime('now') WHERE id = ? AND tenant_id = ?",
                         (fmt(end), note, int(include_passwords), current["id"], tenant_id))
            kind, gid = "changed", current["id"]
        else:
            gid = conn.execute(
                "INSERT INTO support_grants (tenant_id, granted_by, granted_by_email, starts_at, ends_at, note,"
                " include_passwords) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, user["id"], user["email"], fmt(_now()), fmt(end), note, int(include_passwords))).lastrowid
            kind = "created"
        detail = f"until {fmt(end)} UTC" + (", including unattended passwords" if include_passwords else "") + \
                 (f"; note: {note}" if note else "")
        log_event(conn, f"support_access_{'granted' if kind == 'created' else 'changed'}", detail, user["email"],
                  tenant_id=tenant_id)
        conn.commit()
    finally:
        conn.close()
    _notify(tenant_id, f"Support access {'granted' if kind == 'created' else 'changed'}", [
        f"{user['email']} {'granted' if kind == 'created' else 'changed'} provider support access to this dashboard.",
        f"Support can sign in until {_local(end)}"
        + (", and can see unattended-access passwords." if include_passwords else ". Unattended-access passwords stay hidden."),
        *( [f"Note: {note}"] if note else []),
        "You can revoke it at any time on the Support access page.",
    ])
    return kind, gid


def revoke(tenant_id: int, user: dict) -> bool:
    conn = get_db()
    try:
        current = active_grant(conn, tenant_id)
        if not current:
            return False
        conn.execute("UPDATE support_grants SET revoked_at = datetime('now'), revoked_by = ?, updated_at = datetime('now')"
                     " WHERE id = ? AND tenant_id = ?", (user["email"], current["id"], tenant_id))
        ended = _end_sessions(conn, tenant_id, "access revoked", grant_id=current["id"])
        log_event(conn, "support_access_revoked", f"{ended} support session(s) ended" if ended else "",
                  user["email"], tenant_id=tenant_id)
        conn.commit()
    finally:
        conn.close()
    _notify(tenant_id, "Support access revoked", [
        f"{user['email']} revoked provider support access to this dashboard. Any support session has ended."])
    return True


def history(tenant_id: int, limit: int = 50) -> tuple[list, list]:
    conn = get_db()
    grants = conn.execute("SELECT * FROM support_grants WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                          (tenant_id, limit)).fetchall()
    sessions = conn.execute("SELECT * FROM support_sessions WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                            (tenant_id, limit)).fetchall()
    conn.close()
    return grants, sessions


# ── Tokens and sessions ──────────────────────────────────────────────────────

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(conn, tenant_id: int, grant_id: int, provider_user_id: int, reason: str) -> str:
    """A one-time token for /support/enter (the console calls this)."""
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO support_tokens (token_hash, provider_user_id, tenant_id, grant_id, reason, expires_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)",
                 (_hash(token), provider_user_id, tenant_id, grant_id, reason.strip()[:300],
                  fmt(_now() + timedelta(seconds=TOKEN_TTL_S))))
    conn.commit()
    return token


def _provider_user(conn, pid: int):
    return conn.execute("SELECT id, email, display_name, session_version, disabled_at FROM provider_users WHERE id = ?",
                        (pid,)).fetchone()


def enter(tenant_id: int, token: str) -> dict:
    """Use a token: returns the session values to store in the tenant cookie,
    or raises SupportError. The token is spent whatever happens next."""
    if not token or len(token) > 200:
        raise SupportError("This support link is not valid.")
    conn = get_db()
    try:
        now = _now()
        row = conn.execute("SELECT * FROM support_tokens WHERE token_hash = ? AND tenant_id = ?",
                           (_hash(token), tenant_id)).fetchone()
        if row is None or row["used_at"]:
            raise SupportError("This support link is not valid or has already been used.")
        conn.execute("UPDATE support_tokens SET used_at = ? WHERE id = ? AND tenant_id = ? AND used_at IS NULL",
                     (fmt(now), row["id"], tenant_id))
        conn.commit()
        if parse(row["expires_at"]) <= now:
            raise SupportError("This support link has expired. Open the dashboard from the console again.")
        g = active_grant(conn, tenant_id, now)
        if g is None or g["id"] != row["grant_id"]:
            raise SupportError("Support access to this dashboard isn't granted right now.")
        p = _provider_user(conn, row["provider_user_id"])
        if p is None or p["disabled_at"]:
            raise SupportError("This support link is not valid.")
        sid = conn.execute(
            "INSERT INTO support_sessions (tenant_id, grant_id, provider_user_id, provider_email, provider_name, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_id, g["id"], p["id"], p["email"], p["display_name"] or p["email"], row["reason"])).lastrowid
        log_event(conn, "support_session_started", f"reason: {row['reason']}", f"support:{p['email']}",
                  tenant_id=tenant_id)
        from app.deps import provider_log
        provider_log(conn, "support_session_started", f"session {sid}; reason: {row['reason']}", p["email"], tenant_id)
        conn.commit()
        values = {"support_session_id": sid, "grant_id": g["id"], "support_provider_user_id": p["id"],
                  "provider_sv": p["session_version"]}
        name, email = p["display_name"] or p["email"], p["email"]
    finally:
        conn.close()
    _notify(tenant_id, "Provider support signed in", [
        f"{name} ({email}) from your provider signed in to this dashboard with your support access grant.",
        f"Reason given: {row['reason']}",
        "Everything they change is in the Logs, marked support:" + email + ". You can revoke access at any time.",
    ])
    return values


def session_user(session: dict, tenant) -> dict | None:
    """The current_user for a support session, or None after ending it (the
    caller clears the cookie)."""
    sid = session.get("support_session_id")
    conn = get_db()
    try:
        now = _now()
        s = conn.execute("SELECT * FROM support_sessions WHERE id = ? AND tenant_id = ?", (sid, tenant.id)).fetchone()
        if s is None or s["ended_at"] or s["grant_id"] != session.get("grant_id") \
                or s["provider_user_id"] != session.get("support_provider_user_id"):
            return None
        g = conn.execute("SELECT * FROM support_grants WHERE id = ? AND tenant_id = ?", (s["grant_id"], tenant.id)).fetchone()
        p = _provider_user(conn, s["provider_user_id"])
        cap = parse(s["started_at"]) + timedelta(seconds=SUPPORT_SESSION_MAX_AGE)
        why = None
        if g is None or g["revoked_at"]:
            why = "access revoked"
        elif parse(g["ends_at"]) <= now:
            why = "access expired"
        elif cap <= now:
            why = "4-hour limit"
        elif p is None or p["disabled_at"]:
            why = "provider user disabled"
        elif p["session_version"] != session.get("provider_sv"):
            why = "provider user signed out"
        elif not tenant.is_open:
            why = f"tenant {tenant.status}"
        if why:
            conn.execute("UPDATE support_sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND tenant_id = ?"
                         " AND ended_at IS NULL", (fmt(now), why, s["id"], tenant.id))
            log_event(conn, "support_session_ended", why, f"support:{s['provider_email']}", tenant_id=tenant.id)
            conn.commit()
            return None
        from app.permissions import CAP_KEYS
        ends = min(parse(g["ends_at"]), cap)
        return {
            "id": None,
            "tenant_id": tenant.id,
            "role": "admin",
            "role_name": "Provider support",
            "permissions": set(CAP_KEYS) - SUPPORT_WITHHELD_PERMS,
            "name": f"Provider support ({s['provider_name']})",
            "email": f"support:{s['provider_email']}",
            "is_support": True,
            "support_name": s["provider_name"],
            "support_ends_at": fmt(ends),
            "support_session_id": s["id"],
            "include_passwords": bool(g["include_passwords"]),
        }
    finally:
        conn.close()


def forbidden_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p + ".") for p in SUPPORT_FORBIDDEN_PATHS)


def can_see_passwords(user: dict) -> bool:
    """Whether this user may see or change unattended-access passwords."""
    return not user.get("is_support") or bool(user.get("include_passwords"))


def end_session(tenant_id: int, session_id: int, reason: str, actor: str) -> None:
    conn = get_db()
    conn.execute("UPDATE support_sessions SET ended_at = datetime('now'), end_reason = ? WHERE id = ? AND tenant_id = ?"
                 " AND ended_at IS NULL", (reason, session_id, tenant_id))
    log_event(conn, "support_session_ended", reason, actor, tenant_id=tenant_id)
    conn.commit()
    conn.close()


def _end_sessions(conn, tenant_id: int, reason: str, grant_id: int | None = None) -> int:
    sql = "UPDATE support_sessions SET ended_at = datetime('now'), end_reason = ? WHERE tenant_id = ? AND ended_at IS NULL"
    params: tuple = (reason, tenant_id)
    if grant_id is not None:
        sql += " AND grant_id = ?"
        params += (grant_id,)
    return conn.execute(sql, params).rowcount


def end_all_sessions(tenant_id: int, reason: str) -> int:
    """End a tenant's support sessions (suspend, cancel, purge)."""
    conn = get_db()
    n = _end_sessions(conn, tenant_id, reason)
    if n:
        log_event(conn, "support_session_ended", f"{n} session(s): {reason}", "", tenant_id=tenant_id)
    conn.commit()
    conn.close()
    return n


# ── Expiry (hourly maintenance) and email ────────────────────────────────────

def sweep(now: datetime | None = None) -> None:
    """Email the admins of tenants whose grant ran out, and close its
    sessions in the history."""
    now = now or _now()
    conn = get_db()
    rows = conn.execute("SELECT id, tenant_id, ends_at FROM support_grants WHERE revoked_at IS NULL"
                        " AND expiry_notified_at IS NULL AND ends_at <= ?", (fmt(now),)).fetchall()
    conn.close()
    for g in rows:
        conn = get_db()
        conn.execute("UPDATE support_grants SET expiry_notified_at = ? WHERE id = ? AND tenant_id = ?",
                     (fmt(now), g["id"], g["tenant_id"]))
        _end_sessions(conn, g["tenant_id"], "access expired", grant_id=g["id"])
        log_event(conn, "support_access_expired", f"ended {g['ends_at']} UTC", "", tenant_id=g["tenant_id"])
        conn.commit()
        conn.close()
        _notify(g["tenant_id"], "Support access ended", [
            "The provider support access you granted to this dashboard has ended.",
            "Grant it again on the Support access page if support still needs to get in."])


def _local(dt: datetime) -> str:
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _notify(tenant_id: int, subject: str, lines: list[str]) -> None:
    """Email the tenant's admins (provider mail). Best effort."""
    try:
        from app.console import mail
        conn = get_db()
        to = [r[0] for r in conn.execute("SELECT email FROM users WHERE tenant_id = ? AND role = 'admin'", (tenant_id,))]
        name = (conn.execute("SELECT display_name FROM tenants WHERE id = ?", (tenant_id,)).fetchone() or [""])[0]
        conn.close()
        if to:
            mail.send(to, f"{name}: {subject}", mail.simple_html(subject, lines), event="support_access_email")
    except Exception:
        pass
