-- RustDesk fleet database — single-tenant architecture.
--
-- One hbbs/hbbr pair, one keypair, one device table for the whole fleet.
-- "Clients" (Acme Corp, internal staff, etc.) are a grouping/tag on
-- devices and on installers, NOT separate infrastructure. There is no
-- per-tenant isolation at the server level — every device shares the
-- same server key and the same hbbs instance.

PRAGMA foreign_keys = ON;

-- Singleton table: exactly one row, describing the one hbbs/hbbr stack.
-- Modeled as a table rather than hardcoded constants so the dashboard
-- can display/edit it without a code change.
CREATE TABLE IF NOT EXISTS server_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- enforces singleton
    host            TEXT NOT NULL,
    hbbs_port       INTEGER NOT NULL DEFAULT 21115,
    hbbr_port       INTEGER NOT NULL DEFAULT 21117,
    pubkey          TEXT,
    data_dir        TEXT NOT NULL,
    compose_path    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'provisioning'
                        CHECK (status IN ('provisioning','active','stopped')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Client groups: the grouping mechanism that replaces per-tenant stacks.
-- Purely organizational — does not gate server access in any way.
CREATE TABLE IF NOT EXISTS client_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,        -- e.g. "acme-corp", "govirtual365-internal"
    display_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','suspended','archived')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Devices register against the single shared server. group_id tags which
-- client they belong to for dashboard filtering/RBAC — it is an
-- application-layer label, not a security boundary.
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER REFERENCES client_groups(id),
    rustdesk_id     TEXT UNIQUE,
    hostname        TEXT,
    os              TEXT,
    last_seen       TEXT,
    status          TEXT DEFAULT 'unknown',
    installer_id    INTEGER REFERENCES installers(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One installer can be built per client_group (same server, branded /
-- pre-tagged per group) plus per platform/version.
CREATE TABLE IF NOT EXISTS installers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES client_groups(id),
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
    signed_at       TEXT
);

-- RBAC: techs/admins, each scoped to one or more client_groups (app-layer
-- only — every tech can technically reach every device via the native
-- client/keys, since there's only one server; this is a dashboard-level
-- visibility/permission control, not a hard security boundary).
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    role            TEXT NOT NULL DEFAULT 'tech' CHECK (role IN ('tech','admin')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_group_access (
    user_id         INTEGER NOT NULL REFERENCES users(id),
    group_id        INTEGER NOT NULL REFERENCES client_groups(id),
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE IF NOT EXISTS provisioning_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event           TEXT NOT NULL,
    detail           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_devices_group ON devices(group_id);
CREATE INDEX IF NOT EXISTS idx_installers_group ON installers(group_id);
