"""Passkey sign-in handoff for custom domains (phase 1d; TENANCY.md,
"Passkeys on custom domains").

A tenant's passkeys belong to its own host (webauthn_config.rp_id_for). On
a custom domain, browsers that support WebAuthn Related Origin Requests use
them directly (/.well-known/webauthn lists the custom domains). Others get
a SecurityError; then the sign-in page sends the user to the tenant's own
host to sign in there, and brings them back with a token:

    custom   /login/webauthn  ->  home  /login/handoff/start?to=<custom>
    home     (sign in as usual)  ->  /login/handoff/go
    home     issues a token (hashed, 60 seconds, single use, for that user,
             tenant and custom host)  ->  custom  /login/handoff?token=…
    custom   spends it and signs the user in  ->  /

The token's path is logged without its query (uvicorn filter, nginx).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.deps import get_db

TOKEN_TTL_S = 60

SCHEMA = """
    CREATE TABLE IF NOT EXISTS login_handoffs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
        user_id      INTEGER NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        target_host  TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        used_at      TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (tenant_id, user_id) REFERENCES users(tenant_id, id)
    );
"""


class HandoffError(Exception):
    pass


def ensure_table(conn) -> None:
    conn.executescript(SCHEMA)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def valid_target(conn, tenant_id: int, host: str) -> bool:
    """A served custom domain of this tenant (the only place a token may go)."""
    from app import domains
    return host in domains.served_custom_hosts(conn, tenant_id)


def issue(tenant_id: int, user_id: int, target_host: str) -> str:
    conn = get_db()
    try:
        if not valid_target(conn, tenant_id, target_host):
            raise HandoffError("That domain isn't one of this account's.")
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM login_handoffs WHERE tenant_id = ? AND expires_at < ?",
                     (tenant_id, _fmt(_now() - timedelta(days=1))))
        conn.execute("INSERT INTO login_handoffs (tenant_id, user_id, token_hash, target_host, expires_at)"
                     " VALUES (?, ?, ?, ?, ?)",
                     (tenant_id, user_id, _hash(token), target_host, _fmt(_now() + timedelta(seconds=TOKEN_TTL_S))))
        conn.commit()
        return token
    finally:
        conn.close()


def spend(tenant_id: int, host: str, token: str):
    """The users row a token signs in, on this tenant and host. The token is
    spent whatever happens next. HandoffError if it's unknown, used, expired,
    for another tenant or host, or the user is gone."""
    if not token or len(token) > 200:
        raise HandoffError("This sign-in link isn't valid.")
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM login_handoffs WHERE token_hash = ? AND tenant_id = ?",
                           (_hash(token), tenant_id)).fetchone()
        if row is None:
            raise HandoffError("This sign-in link isn't valid.")
        cur = conn.execute("UPDATE login_handoffs SET used_at = ? WHERE id = ? AND tenant_id = ? AND used_at IS NULL",
                           (_fmt(_now()), row["id"], tenant_id))
        conn.commit()
        if cur.rowcount != 1:
            raise HandoffError("This sign-in link was already used.")
        if row["expires_at"] < _fmt(_now()):
            raise HandoffError("This sign-in link has expired.")
        if row["target_host"] != host or not valid_target(conn, tenant_id, host):
            raise HandoffError("This sign-in link is for another address.")
        user = conn.execute("SELECT * FROM users WHERE id = ? AND tenant_id = ?", (row["user_id"], tenant_id)).fetchone()
        if user is None:
            raise HandoffError("This account no longer exists.")
        return user
    finally:
        conn.close()
