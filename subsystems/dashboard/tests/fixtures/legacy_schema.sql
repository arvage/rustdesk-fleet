-- The fleet database as it was before tenancy (schema.sql plus every
-- dashboard migration up to phase 0), for tests/test_migration.py.
-- Generated once; do not edit to match newer code.

CREATE TABLE backup_config (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            enabled       INTEGER NOT NULL DEFAULT 0,
            provider      TEXT NOT NULL DEFAULT 's3',
            s3_provider   TEXT NOT NULL DEFAULT 'aws',
            s3_endpoint   TEXT NOT NULL DEFAULT '',
            s3_region     TEXT NOT NULL DEFAULT '',
            s3_bucket     TEXT NOT NULL DEFAULT '',
            s3_prefix     TEXT NOT NULL DEFAULT '',
            s3_access_key TEXT NOT NULL DEFAULT '',
            s3_secret_key TEXT NOT NULL DEFAULT '',
            rclone_remote TEXT NOT NULL DEFAULT '',
            passphrase    TEXT NOT NULL DEFAULT '',
            retention     INTEGER NOT NULL DEFAULT 14,
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        , offsite_retention INTEGER NOT NULL DEFAULT 0);

CREATE TABLE client_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,        -- e.g. "acme-corp", "acme-internal"
    display_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','suspended','archived')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
, unattended_password TEXT, auto_update TEXT CHECK (auto_update IN ('Y','N')), auto_update_ts INTEGER, remote_config TEXT CHECK (remote_config IN ('Y','N')), policy_ts INTEGER, brand_app_name TEXT);

CREATE TABLE connect_clicks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rustdesk_id TEXT NOT NULL,
            user_email  TEXT NOT NULL,
            clicked_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE TABLE device_file_transfers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
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
            nonce        TEXT UNIQUE,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE TABLE device_info (
            rustdesk_id     TEXT PRIMARY KEY,
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
            heartbeat_at    TEXT
        , policy_ts INTEGER);

CREATE TABLE device_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            rustdesk_id   TEXT NOT NULL,
            conn_ref      TEXT NOT NULL,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at      TEXT
        , peer_id TEXT, peer_name TEXT, peer_ip TEXT, conn_type INTEGER);

CREATE TABLE devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER REFERENCES client_groups(id),
    rustdesk_id     TEXT UNIQUE,
    hostname        TEXT,
    os              TEXT,
    last_seen       TEXT,
    status          TEXT DEFAULT 'unknown',
    installer_id    INTEGER REFERENCES installers(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
, label TEXT, hidden INTEGER NOT NULL DEFAULT 0);

CREATE TABLE download_links (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            token         TEXT NOT NULL UNIQUE,
            group_id      INTEGER NOT NULL REFERENCES client_groups(id),
            note          TEXT,
            expires_at    TEXT,
            max_uses      INTEGER,
            use_count     INTEGER NOT NULL DEFAULT 0,
            revoked       INTEGER NOT NULL DEFAULT 0,
            created_by    TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at  TEXT
        , emailed_to TEXT, emailed_at TEXT);

CREATE TABLE dynamic_groups (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            match        TEXT NOT NULL DEFAULT 'all' CHECK (match IN ('all','any')),
            rules        TEXT NOT NULL DEFAULT '[]',
            created_by   TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE TABLE email_design (
            id                 INTEGER PRIMARY KEY CHECK (id = 1),
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
        );

CREATE TABLE email_templates (
            key          TEXT PRIMARY KEY,
            subject      TEXT,
            heading      TEXT,
            intro        TEXT,
            outro        TEXT,
            button_text  TEXT,
            show_details INTEGER NOT NULL DEFAULT 1,
            updated_by   TEXT,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE TABLE installers (
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

CREATE TABLE notification_events (
    event_type  TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE notification_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    recipients  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('sent','failed')),
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE notification_settings (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    enabled     INTEGER NOT NULL DEFAULT 0,
    smtp_host   TEXT NOT NULL DEFAULT '',
    smtp_port   INTEGER NOT NULL DEFAULT 587,
    smtp_tls    TEXT NOT NULL DEFAULT 'starttls'
                    CHECK (smtp_tls IN ('starttls','ssl','none')),
    smtp_user   TEXT NOT NULL DEFAULT '',
    smtp_pass   TEXT NOT NULL DEFAULT '',
    from_addr   TEXT NOT NULL DEFAULT '',
    to_addrs    TEXT NOT NULL DEFAULT '',   -- comma-separated recipient list
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
, mem_threshold INTEGER NOT NULL DEFAULT 90, disk_threshold INTEGER NOT NULL DEFAULT 90);

CREATE TABLE provisioning_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event           TEXT NOT NULL,
    detail           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
, user_email TEXT);

CREATE TABLE roles (
            key          TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            permissions  TEXT NOT NULL DEFAULT '[]',   -- JSON list, or '*' for all
            is_system    INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE TABLE rustdesk_peer_users (
            peer_id    TEXT PRIMARY KEY,
            user_email TEXT NOT NULL
        );

CREATE TABLE server_config (
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

CREATE TABLE signing_config (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            enabled       INTEGER NOT NULL DEFAULT 0,
            auto_sign     INTEGER NOT NULL DEFAULT 1,
            provider      TEXT NOT NULL DEFAULT 'azure_trusted_signing',
            azure_tenant_id     TEXT NOT NULL DEFAULT '',
            azure_client_id     TEXT NOT NULL DEFAULT '',
            azure_client_secret TEXT NOT NULL DEFAULT '',
            endpoint      TEXT NOT NULL DEFAULT '',
            account_name  TEXT NOT NULL DEFAULT '',
            profile_name  TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE TABLE user_group_access (
    user_id         INTEGER NOT NULL REFERENCES users(id),
    group_id        INTEGER NOT NULL REFERENCES client_groups(id),
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE "users" (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    email           TEXT NOT NULL UNIQUE,
                    display_name    TEXT,
                    role            TEXT NOT NULL DEFAULT 'tech',
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    password_hash   TEXT,
                    auth_method     TEXT NOT NULL DEFAULT 'password'
                                        CHECK (auth_method IN ('password','passkey','both')),
                    webauthn_user_handle TEXT
                , session_version INTEGER NOT NULL DEFAULT 1);

CREATE TABLE webauthn_credentials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id),
            credential_id TEXT NOT NULL UNIQUE,
            public_key    BLOB NOT NULL,
            sign_count    INTEGER NOT NULL DEFAULT 0,
            transports    TEXT,
            nickname      TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at  TEXT
        );

CREATE INDEX idx_connect_clicks_rid ON connect_clicks(rustdesk_id, clicked_at);
CREATE INDEX idx_device_sessions_open ON device_sessions(ended_at);
CREATE INDEX idx_device_sessions_rid ON device_sessions(rustdesk_id);
CREATE INDEX idx_device_sessions_started ON device_sessions(started_at);
CREATE INDEX idx_devices_group ON devices(group_id);
CREATE INDEX idx_download_links_group ON download_links(group_id);
CREATE INDEX idx_file_transfers_rid ON device_file_transfers(rustdesk_id, created_at);
CREATE INDEX idx_installers_group ON installers(group_id);
CREATE INDEX idx_webauthn_credentials_user ON webauthn_credentials(user_id);
