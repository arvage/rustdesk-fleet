"""Provider-level tables for the provider console (TENANCY.md). None of these
belong to a tenant. Called from deps.run_migrations(); idempotent."""
import sqlite3

PROVIDER_TABLES_SQL = """
    -- Provider staff. Passkey sign-in only (no passwords).
    CREATE TABLE IF NOT EXISTS provider_users (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        email                TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
        display_name         TEXT NOT NULL DEFAULT '',
        session_version      INTEGER NOT NULL DEFAULT 1,
        webauthn_user_handle TEXT,
        created_at           TEXT NOT NULL DEFAULT (datetime('now')),
        last_sign_in_at      TEXT,
        disabled_at          TEXT
    );
    CREATE TABLE IF NOT EXISTS provider_webauthn_credentials (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_user_id INTEGER NOT NULL REFERENCES provider_users(id),
        credential_id    TEXT NOT NULL UNIQUE,
        public_key       BLOB NOT NULL,
        sign_count       INTEGER NOT NULL DEFAULT 0,
        transports       TEXT,
        nickname         TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        last_used_at     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_provider_creds_user ON provider_webauthn_credentials(provider_user_id);
    -- One-time passkey enrollment links (CLI `add`/`enroll-link`, console invites).
    CREATE TABLE IF NOT EXISTS provider_enrollments (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_user_id INTEGER NOT NULL REFERENCES provider_users(id),
        token_hash       TEXT NOT NULL UNIQUE,
        expires_at       TEXT NOT NULL,
        used_at          TEXT,
        created_by       TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    -- Provider-wide settings (SMTP for provider emails, alert thresholds...).
    CREATE TABLE IF NOT EXISTS provider_settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL DEFAULT ''
    );
"""


def ensure_provider_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(PROVIDER_TABLES_SQL)
    # Console actions go to the audit log with source = 'provider' (tenant_id
    # NULL, or the tenant acted on). Tenants' own Logs pages leave them out.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(provisioning_events)")]
    if "source" not in cols:
        conn.execute("ALTER TABLE provisioning_events ADD COLUMN source TEXT")
    conn.commit()
