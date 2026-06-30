-- RustDesk fleet provisioning database
-- One row per tenant; ports/keys/paths are derived once at creation and frozen.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,       -- e.g. "govirtual365-internal", "acme-corp"
    display_name    TEXT NOT NULL,              -- shown in dashboard / installer branding
    status          TEXT NOT NULL DEFAULT 'provisioning'
                        CHECK (status IN ('provisioning','active','suspended','decommissioned')),

    -- networking — frozen at allocation time, never reused even after decommission
    port_index      INTEGER NOT NULL UNIQUE,    -- the "N" in the offset scheme
    host            TEXT NOT NULL,              -- public host/IP clients will connect to
    hbbs_port       INTEGER NOT NULL UNIQUE,    -- base port (the -p value passed to hbbs)
    hbbr_port       INTEGER NOT NULL UNIQUE,    -- base port (the -p value passed to hbbr)

    -- identity
    pubkey          TEXT,                       -- contents of id_ed25519.pub once generated
    pubkey_fingerprint TEXT,

    -- filesystem
    data_dir        TEXT NOT NULL,              -- e.g. /opt/rustdesk-fleet/tenants/acme-corp
    compose_path    TEXT NOT NULL,              -- rendered docker-compose.yml path

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS installers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
    platform        TEXT NOT NULL CHECK (platform IN ('windows-x64','macos-arm64','macos-x64','linux','android-arm64')),
    rustdesk_version TEXT NOT NULL,             -- which upstream release this was built from
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

CREATE TABLE IF NOT EXISTS provisioning_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER REFERENCES tenants(id),
    event           TEXT NOT NULL,               -- e.g. "stack_up", "stack_down", "key_captured"
    detail          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_installers_tenant ON installers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant ON provisioning_events(tenant_id);
