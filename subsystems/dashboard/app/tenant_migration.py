"""The one-time move from the single-tenant schema to tenancy (TENANCY.md).

migrate(conn) makes the current deployment tenant #1 and gives every
tenant-owned table a NOT NULL tenant_id (nullable only on the audit log),
rebuilding each table in its final shape. It is:

- idempotent: a table that already has tenant_id is left alone, tenant #1,
  its relay and its domain are only inserted when missing, and a second run
  changes nothing;
- atomic: everything happens in one transaction, so an interruption leaves
  the old schema exactly as it was and the old code still works on it.

server_config is left in place, unused, for one release in case of a
rollback to the single-tenant code. Nothing reads it after this migration.
"""
from __future__ import annotations

import sqlite3

from app.tenant_schema import ensure_tenancy_tables, normalize_host

# The deployment that existed before tenancy becomes tenant #1.
LEGACY_TENANT_ID = 1
LEGACY_TENANT_SLUG = "pacificmit"
LEGACY_TENANT_NAME = "Pacific MIT"

# Final shape of every tenant-owned table, parents before children. "{t}" is
# the table name: rebuilds create "<name>__tenancy" and rename it.
TENANT_TABLES: dict[str, str] = {
    "client_groups": """
        CREATE TABLE "{t}" (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
            slug            TEXT NOT NULL,
            display_name    TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','suspended','archived')),
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            unattended_password TEXT,
            auto_update     TEXT CHECK (auto_update IN ('Y','N')),
            auto_update_ts  INTEGER,
            remote_config   TEXT CHECK (remote_config IN ('Y','N')),
            policy_ts       INTEGER,
            brand_app_name  TEXT,
            UNIQUE (tenant_id, slug),
            UNIQUE (tenant_id, id)
        )""",
    "users": """
        CREATE TABLE "{t}" (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
            email           TEXT NOT NULL,
            display_name    TEXT,
            role            TEXT NOT NULL DEFAULT 'tech',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            password_hash   TEXT,
            auth_method     TEXT NOT NULL DEFAULT 'password'
                                CHECK (auth_method IN ('password','passkey','both')),
            webauthn_user_handle TEXT,
            session_version INTEGER NOT NULL DEFAULT 1,
            UNIQUE (tenant_id, email),
            UNIQUE (tenant_id, id)
        )""",
    "installers": """
        CREATE TABLE "{t}" (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
            group_id        INTEGER NOT NULL,
            platform        TEXT NOT NULL CHECK (platform IN ('windows-x64','macos-arm64','macos-x64','linux','android-arm64')),
            rustdesk_version TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','built','signing','signed','failed')),
            unsigned_path   TEXT,
            signed_path     TEXT,
            sha256_unsigned TEXT,
            sha256_signed   TEXT,
            error_message   TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            signed_at       TEXT,
            FOREIGN KEY (tenant_id, group_id) REFERENCES client_groups(tenant_id, id)
        )""",
    "devices": """
        CREATE TABLE "{t}" (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
            group_id        INTEGER,
            rustdesk_id     TEXT,
            hostname        TEXT,
            os              TEXT,
            last_seen       TEXT,
            status          TEXT DEFAULT 'unknown',
            installer_id    INTEGER REFERENCES installers(id),
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            label           TEXT,
            hidden          INTEGER NOT NULL DEFAULT 0,
            UNIQUE (tenant_id, rustdesk_id),
            FOREIGN KEY (tenant_id, group_id) REFERENCES client_groups(tenant_id, id)
        )""",
    "download_links": """
        CREATE TABLE "{t}" (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
            token         TEXT NOT NULL UNIQUE,
            group_id      INTEGER NOT NULL,
            note          TEXT,
            expires_at    TEXT,
            max_uses      INTEGER,
            use_count     INTEGER NOT NULL DEFAULT 0,
            revoked       INTEGER NOT NULL DEFAULT 0,
            created_by    TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at  TEXT,
            emailed_to    TEXT,
            emailed_at    TEXT,
            FOREIGN KEY (tenant_id, group_id) REFERENCES client_groups(tenant_id, id)
        )""",
    "user_group_access": """
        CREATE TABLE "{t}" (
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
            user_id         INTEGER NOT NULL,
            group_id        INTEGER NOT NULL,
            PRIMARY KEY (user_id, group_id),
            FOREIGN KEY (tenant_id, user_id) REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, group_id) REFERENCES client_groups(tenant_id, id)
        )""",
    "webauthn_credentials": """
        CREATE TABLE "{t}" (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
            user_id       INTEGER NOT NULL,
            credential_id TEXT NOT NULL UNIQUE,
            public_key    BLOB NOT NULL,
            sign_count    INTEGER NOT NULL DEFAULT 0,
            transports    TEXT,
            nickname      TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at  TEXT,
            FOREIGN KEY (tenant_id, user_id) REFERENCES users(tenant_id, id)
        )""",
    "roles": """
        CREATE TABLE "{t}" (
            tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
            key          TEXT NOT NULL,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            permissions  TEXT NOT NULL DEFAULT '[]',   -- JSON list, or '*' for all
            is_system    INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, key)
        )""",
    "device_info": """
        CREATE TABLE "{t}" (
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
            rustdesk_id     TEXT NOT NULL,
            hostname        TEXT,
            os              TEXT,
            username        TEXT,
            cpu             TEXT,
            memory          TEXT,
            client_version  TEXT,
            extra           TEXT,
            report_ip       TEXT,
            active_conns    INTEGER NOT NULL DEFAULT 0,
            sysinfo_at      TEXT,
            heartbeat_at    TEXT,
            policy_ts       INTEGER,
            PRIMARY KEY (tenant_id, rustdesk_id)
        )""",
    "device_sessions": """
        CREATE TABLE "{t}" (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
            rustdesk_id   TEXT NOT NULL,
            conn_ref      TEXT NOT NULL,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at      TEXT,
            peer_id       TEXT,
            peer_name     TEXT,
            peer_ip       TEXT,
            conn_type     INTEGER
        )""",
    "device_file_transfers": """
        CREATE TABLE "{t}" (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
            rustdesk_id  TEXT NOT NULL,
            session_id   INTEGER,
            conn_ref     TEXT,
            peer_id      TEXT,
            peer_name    TEXT,
            peer_ip      TEXT,
            direction    INTEGER NOT NULL,
            path         TEXT,
            is_file      INTEGER NOT NULL DEFAULT 0,
            file_count   INTEGER NOT NULL DEFAULT 0,
            files        TEXT,
            nonce        TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (tenant_id, nonce)
        )""",
    "connect_clicks": """
        CREATE TABLE "{t}" (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
            rustdesk_id TEXT NOT NULL,
            user_email  TEXT NOT NULL,
            clicked_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    "rustdesk_peer_users": """
        CREATE TABLE "{t}" (
            tenant_id  INTEGER NOT NULL REFERENCES tenants(id),
            peer_id    TEXT NOT NULL,
            user_email TEXT NOT NULL,
            PRIMARY KEY (tenant_id, peer_id)
        )""",
    "dynamic_groups": """
        CREATE TABLE "{t}" (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            match        TEXT NOT NULL DEFAULT 'all' CHECK (match IN ('all','any')),
            rules        TEXT NOT NULL DEFAULT '[]',
            created_by   TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    # One row per tenant (was a CHECK (id = 1) singleton). UNIQUE rather than
    # INTEGER PRIMARY KEY: a rowid alias would quietly invent an id for an
    # insert that forgot tenant_id instead of refusing it.
    "notification_settings": """
        CREATE TABLE "{t}" (
            tenant_id   INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
            enabled     INTEGER NOT NULL DEFAULT 0,
            smtp_host   TEXT NOT NULL DEFAULT '',
            smtp_port   INTEGER NOT NULL DEFAULT 587,
            smtp_tls    TEXT NOT NULL DEFAULT 'starttls',
            smtp_user   TEXT NOT NULL DEFAULT '',
            smtp_pass   TEXT NOT NULL DEFAULT '',
            from_addr   TEXT NOT NULL DEFAULT '',
            to_addrs    TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            mem_threshold  INTEGER NOT NULL DEFAULT 90,
            disk_threshold INTEGER NOT NULL DEFAULT 90
        )""",
    "notification_events": """
        CREATE TABLE "{t}" (
            tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
            event_type  TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (tenant_id, event_type)
        )""",
    "notification_log": """
        CREATE TABLE "{t}" (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
            event_type  TEXT NOT NULL,
            subject     TEXT NOT NULL,
            recipients  TEXT NOT NULL,
            status      TEXT NOT NULL,
            error       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    "email_templates": """
        CREATE TABLE "{t}" (
            tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
            key          TEXT NOT NULL,
            subject      TEXT,
            heading      TEXT,
            intro        TEXT,
            outro        TEXT,
            button_text  TEXT,
            show_details INTEGER NOT NULL DEFAULT 1,
            updated_by   TEXT,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, key)
        )""",
    # One row per tenant (was a CHECK (id = 1) singleton).
    "email_design": """
        CREATE TABLE "{t}" (
            tenant_id          INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
            brand_name         TEXT,
            client_brand_name  TEXT,
            show_logo_team     INTEGER,
            show_logo_client   INTEGER,
            logo_height        INTEGER,
            header_bg          TEXT,
            header_text        TEXT,
            accent             TEXT,
            page_bg            TEXT,
            font               TEXT,
            footer_text        TEXT,
            client_footer_text TEXT,
            updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    # The audit log. tenant_id NULL = a provider event (backups, signing...).
    "provisioning_events": """
        CREATE TABLE "{t}" (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER REFERENCES tenants(id),
            event           TEXT NOT NULL,
            detail          TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            user_email      TEXT
        )""",
}

# Tables where tenant_id may be NULL (everything else is NOT NULL).
NULLABLE_TENANT_TABLES = {"provisioning_events"}

# Indexes for the rebuilt tables (the old ones go with the old tables).
TENANT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_devices_group ON devices(tenant_id, group_id)",
    "CREATE INDEX IF NOT EXISTS idx_installers_group ON installers(tenant_id, group_id)",
    "CREATE INDEX IF NOT EXISTS idx_download_links_group ON download_links(tenant_id, group_id)",
    "CREATE INDEX IF NOT EXISTS idx_webauthn_credentials_user ON webauthn_credentials(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_device_sessions_rid ON device_sessions(tenant_id, rustdesk_id)",
    "CREATE INDEX IF NOT EXISTS idx_device_sessions_open ON device_sessions(tenant_id, ended_at)",
    "CREATE INDEX IF NOT EXISTS idx_device_sessions_started ON device_sessions(tenant_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_connect_clicks_rid ON connect_clicks(tenant_id, rustdesk_id, clicked_at)",
    "CREATE INDEX IF NOT EXISTS idx_file_transfers_rid ON device_file_transfers(tenant_id, rustdesk_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_dynamic_groups_tenant ON dynamic_groups(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_notification_log_tenant ON notification_log(tenant_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_tenant_time ON provisioning_events(tenant_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_tenant_event ON provisioning_events(tenant_id, event)",
]


class MigrationError(RuntimeError):
    pass


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def needs_migration(conn) -> bool:
    """True while any tenant-owned table is missing tenant_id, or tenant #1
    doesn't exist yet."""
    if not _table_exists(conn, "tenants"):
        return True
    if conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0:
        return True
    return any(
        not _table_exists(conn, t) or "tenant_id" not in _columns(conn, t)
        for t in TENANT_TABLES
    )


def _sequence(conn, table: str) -> int | None:
    if not _table_exists(conn, "sqlite_sequence"):
        return None
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    return row[0] if row else None


def _rebuild(conn, table: str, ddl: str) -> None:
    """Recreate `table` in its final shape, copying every row with tenant_id = 1.
    AUTOINCREMENT counters are carried over, so ids of deleted rows are never
    handed out again (a reused user id could match an old session cookie)."""
    if not _table_exists(conn, table):
        conn.execute(ddl.format(t=table))
        return
    old_cols = _columns(conn, table)
    if "tenant_id" in old_cols:
        return
    tmp = f"{table}__tenancy"
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(ddl.format(t=tmp))
    new_cols = _columns(conn, tmp)
    copy = [c for c in new_cols if c in old_cols]
    seq = _sequence(conn, table)
    conn.execute(
        f'INSERT INTO "{tmp}" (tenant_id, {", ".join(copy)}) '
        f'SELECT ?, {", ".join(copy)} FROM "{table}"',
        (LEGACY_TENANT_ID,),
    )
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    if seq is not None:
        current = _sequence(conn, table)
        if current is None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, seq))
        elif current < seq:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (seq, table))


def _legacy_server_config(conn) -> sqlite3.Row | None:
    """The single-tenant relay settings. Read only here, to seed tenant #1."""
    if not _table_exists(conn, "server_config"):
        return None
    return conn.execute("SELECT * FROM server_config WHERE id = 1").fetchone()


def _seed_tenant_one(conn, hbbs_db_path: str) -> None:
    if conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO tenants (id, slug, display_name, status) VALUES (?, ?, ?, 'active')",
            (LEGACY_TENANT_ID, LEGACY_TENANT_SLUG, LEGACY_TENANT_NAME),
        )
    if not conn.execute("SELECT 1 FROM tenants WHERE id = ?", (LEGACY_TENANT_ID,)).fetchone():
        return  # tenants were created some other way; nothing to inherit
    sc = _legacy_server_config(conn)
    if sc is None:
        return  # fresh install: setup_server.py init creates the relay row
    if not conn.execute("SELECT 1 FROM relays WHERE tenant_id = ?", (LEGACY_TENANT_ID,)).fetchone():
        conn.execute(
            """INSERT INTO relays (tenant_id, host, hbbs_port, hbbr_port, pubkey, data_dir,
                                   compose_path, hbbs_db_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (LEGACY_TENANT_ID, sc["host"], sc["hbbs_port"], sc["hbbr_port"], sc["pubkey"],
             sc["data_dir"], sc["compose_path"], hbbs_db_path, sc["status"],
             sc["created_at"], sc["updated_at"]),
        )
    hostname = normalize_host(sc["host"] or "")
    has_domain = conn.execute(
        "SELECT 1 FROM tenant_domains WHERE tenant_id = ?", (LEGACY_TENANT_ID,)
    ).fetchone()
    if hostname and not has_domain:
        conn.execute(
            """INSERT INTO tenant_domains (tenant_id, hostname, kind, verified_at, tls_status, is_primary)
               VALUES (?, ?, 'legacy', datetime('now'), 'active', 1)""",
            (LEGACY_TENANT_ID, hostname),
        )


def migrate(conn: sqlite3.Connection, hbbs_db_path: str) -> bool:
    """Run the tenancy migration on an open connection. Returns True if it
    changed anything. Raises MigrationError (after rolling back) if the result
    would break a foreign key."""
    ensure_tenancy_tables(conn)
    conn.commit()
    if not needs_migration(conn):
        return False

    old_isolation = conn.isolation_level
    conn.commit()
    conn.isolation_level = None            # explicit BEGIN/COMMIT, DDL included
    conn.execute("PRAGMA foreign_keys = OFF")   # only takes effect outside a transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _seed_tenant_one(conn, hbbs_db_path)
            for table, ddl in TENANT_TABLES.items():
                _rebuild(conn, table, ddl)
            for sql in TENANT_INDEXES:
                conn.execute(sql)
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise MigrationError(f"foreign key violations after the migration: {problems[:20]}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = old_isolation
    return True


def seed_tenant(conn, tenant_id: int) -> None:
    """Built-in rows every tenant starts with (idempotent): the system roles.
    Email templates, design and notification settings need no rows; their
    defaults live in code. Doesn't commit."""
    import json
    from app.permissions import SYSTEM_ROLES
    for key, spec in SYSTEM_ROLES.items():
        perms = "*" if spec["perms"] == "*" else json.dumps(spec["perms"])
        conn.execute(
            "INSERT OR IGNORE INTO roles (tenant_id, key, name, description, permissions, is_system)"
            " VALUES (?, ?, ?, ?, ?, 1)",
            (tenant_id, key, spec["name"], spec["description"], perms),
        )
