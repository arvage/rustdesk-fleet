import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path("/opt/rustdesk-fleet/fleet.sqlite3")
HBBS_DB_PATH = Path("/opt/rustdesk-fleet/data/db_v2.sqlite3")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_hbbs_peers() -> dict[str, dict]:
    """Return {rustdesk_id: {ip, registered_at}} from the hbbs peer DB.
    Returns an empty dict if the DB doesn't exist or can't be read."""
    if not HBBS_DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(HBBS_DB_PATH)
        conn.row_factory = sqlite3.Row
        result: dict[str, dict] = {}
        for r in conn.execute("SELECT id, info, created_at FROM peer"):
            info = json.loads(r["info"] or "{}")
            ip = info.get("ip", "").replace("::ffff:", "")
            result[r["id"]] = {"ip": ip, "registered_at": r["created_at"]}
        conn.close()
        return result
    except Exception:
        return {}


# ── Client-reported device details (see routes/client_api.py) ────────────────

_CPU_NOISE_RE = re.compile(r"\((?:R|TM|tm|r)\)|\bCPU\b|\bProcessor\b")


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    except ValueError:
        return ()


def _pretty_os(raw: str) -> tuple[str, str]:
    """'windows / Windows 10 Pro - 10 (19045)' -> ('Windows 10 Pro', '19045')."""
    if not raw:
        return "", ""
    name = raw.split(" / ", 1)[-1]
    build = ""
    if " - " in name:
        name, rest = name.split(" - ", 1)
        m = re.search(r"\((\d+)\)", rest)
        build = m.group(1) if m else ""
    name = name.strip()
    # Older sysinfo crates report Windows 11 as "Windows 10"; the build number tells.
    if build and int(build) >= 22000 and name.startswith("Windows 10"):
        name = "Windows 11" + name[len("Windows 10"):]
    return name, build


def _pretty_hw(cpu: str, memory: str) -> dict:
    """Split 'Intel(R) Xeon(R) CPU E5-2660 v4 @ 2.00GHz, 1.95GHz, 4/2 cores' and '191.91GB'."""
    cpu_name, cores = "", ""
    if cpu:
        parts = [p.strip() for p in cpu.split(",")]
        cpu_name = re.sub(r"\s+", " ", _CPU_NOISE_RE.sub("", parts[0])).strip()
        m = re.search(r"(\d+)/(\d+) cores", cpu)
        if m:
            logical, physical = int(m.group(1)), int(m.group(2))
            cores = f"{physical} cores" + (f" / {logical} threads" if logical != physical else "")
    ram = ""
    m = re.match(r"([\d.]+)\s*GB", memory or "")
    if m:
        gb = float(m.group(1))
        ram = f"{round(gb)} GB" if gb >= 2 else f"{gb:g} GB"
    return {"cpu_name": cpu_name, "cores": cores, "ram": ram}


def get_device_info() -> dict[str, dict]:
    """Return {rustdesk_id: display-ready details} for devices that report in."""
    try:
        from generate_installer import get_pinned_version
        pinned = _version_tuple(get_pinned_version())
    except Exception:
        pinned = ()

    conn = get_db()
    rows = conn.execute(
        """SELECT *, (julianday('now') - julianday(heartbeat_at)) * 86400 AS hb_age
           FROM device_info"""
    ).fetchall()
    conn.close()

    info: dict[str, dict] = {}
    for r in rows:
        os_name, os_build = _pretty_os(r["os"] or "")
        ver = r["client_version"] or ""
        info[r["rustdesk_id"]] = {
            "hostname": r["hostname"] or "",
            "username": r["username"] or "",
            "os_name": os_name,
            "os_build": os_build,
            "client_version": ver,
            "client_outdated": bool(ver and pinned and _version_tuple(ver) < pinned),
            **_pretty_hw(r["cpu"] or "", r["memory"] or ""),
            "active_conns": r["active_conns"] or 0,
            "heartbeat_at": r["heartbeat_at"] or "",
            "sysinfo_at": r["sysinfo_at"] or "",
            "hb_age": r["hb_age"],
        }
    return info


_INFO_KEYS = ("hostname", "username", "os_name", "os_build", "client_version",
              "client_outdated", "cpu_name", "cores", "ram", "heartbeat_at",
              "sysinfo_at", "active_conns")


def merge_device_info(device: dict, info: dict[str, dict]) -> dict:
    """Add client-reported fields (blank when the device doesn't report)."""
    i = info.get(device.get("rustdesk_id") or "", {})
    for k in _INFO_KEYS:
        device[k] = i.get(k, False if k == "client_outdated" else "")
    device["reporting"] = bool(i.get("sysinfo_at"))
    return device


def get_devices(group: str = "") -> tuple[list[dict], int]:
    """Return (devices_list, peer_count), optionally filtered by group slug.

    Merges hbbs peer registry with fleet DB.  Devices marked hidden=1 in
    the fleet DB are suppressed even if they still appear in the peer table.
    """
    peers = get_hbbs_peers()

    conn = get_db()
    fleet_rows = conn.execute(
        """SELECT d.*, cg.display_name AS group_name, cg.slug AS group_slug
           FROM devices d
           LEFT JOIN client_groups cg ON cg.id = d.group_id"""
    ).fetchall()
    conn.close()

    hidden_ids = {r["rustdesk_id"] for r in fleet_rows if r["hidden"]}
    fleet_by_id = {
        r["rustdesk_id"]: dict(r)
        for r in fleet_rows
        if r["rustdesk_id"] and not r["hidden"]
    }

    all_ids = (set(peers) | set(fleet_by_id)) - hidden_ids
    devices: list[dict] = []
    for rid in all_ids:
        peer = peers.get(rid, {})
        fleet = fleet_by_id.get(rid, {})
        devices.append({
            "rustdesk_id": rid,
            "label": fleet.get("label") or "",
            "ip": peer.get("ip") or "—",
            "status": "registered" if rid in peers else (fleet.get("status") or "unknown"),
            "registered_at": (peer.get("registered_at") or "")[:10] or "—",
            "last_seen": fleet.get("last_seen") or "—",
            "group_name": fleet.get("group_name") or "",
            "group_slug": fleet.get("group_slug") or "",
            "group_id": fleet.get("group_id"),
        })

    if group:
        devices = [d for d in devices if d["group_slug"] == group]

    info = get_device_info()
    for d in devices:
        merge_device_info(d, info)

    devices.sort(key=lambda d: d["last_seen"] or "", reverse=True)
    return devices, len(peers)


def log_event(conn, event: str, detail: str = "", user_email: str = "") -> None:
    conn.execute(
        "INSERT INTO provisioning_events (event, detail, user_email) VALUES (?, ?, ?)",
        (event, detail or None, user_email or None),
    )
    conn.commit()


def run_migrations() -> None:
    conn = get_db()

    users_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "password_hash" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        conn.commit()

    groups_cols = {row[1] for row in conn.execute("PRAGMA table_info(client_groups)").fetchall()}
    if "unattended_password" not in groups_cols:
        conn.execute("ALTER TABLE client_groups ADD COLUMN unattended_password TEXT")
        conn.commit()

    devices_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)").fetchall()}
    if "label" not in devices_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN label TEXT")
        conn.commit()
    if "hidden" not in devices_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    events_cols = {row[1] for row in conn.execute("PRAGMA table_info(provisioning_events)").fetchall()}
    if "user_email" not in events_cols:
        conn.execute("ALTER TABLE provisioning_events ADD COLUMN user_email TEXT")
        conn.commit()

    if "auth_method" not in users_cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'password' "
            "CHECK (auth_method IN ('password','passkey','both'))"
        )
        conn.commit()
    if "webauthn_user_handle" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN webauthn_user_handle TEXT")
        conn.commit()

    # The original users table hard-codes CHECK (role IN ('tech','admin')), which
    # blocks custom/viewer roles. SQLite can't drop a constraint, so rebuild the
    # table without it (keeping the auth_method check). Idempotent: only runs
    # while the restrictive constraint is still present.
    users_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()[0]
    if "role IN ('tech','admin')" in users_sql.replace('"', "'"):
        existing = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        new_cols = ["id", "email", "display_name", "role", "created_at",
                    "password_hash", "auth_method", "webauthn_user_handle"]
        copy = ",".join(c for c in new_cols if c in existing)
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("""
                CREATE TABLE users_rebuild (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    email           TEXT NOT NULL UNIQUE,
                    display_name    TEXT,
                    role            TEXT NOT NULL DEFAULT 'tech',
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    password_hash   TEXT,
                    auth_method     TEXT NOT NULL DEFAULT 'password'
                                        CHECK (auth_method IN ('password','passkey','both')),
                    webauthn_user_handle TEXT
                )
            """)
            conn.execute(f"INSERT INTO users_rebuild ({copy}) SELECT {copy} FROM users")
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_rebuild RENAME TO users")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    # WebAuthn/passkey credentials (idempotent CREATE IF NOT EXISTS)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS webauthn_credentials (
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
        CREATE INDEX IF NOT EXISTS idx_webauthn_credentials_user ON webauthn_credentials(user_id);
    """)
    conn.commit()

    # Notification tables (idempotent CREATE IF NOT EXISTS)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notification_settings (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            enabled     INTEGER NOT NULL DEFAULT 0,
            smtp_host   TEXT NOT NULL DEFAULT '',
            smtp_port   INTEGER NOT NULL DEFAULT 587,
            smtp_tls    TEXT NOT NULL DEFAULT 'starttls',
            smtp_user   TEXT NOT NULL DEFAULT '',
            smtp_pass   TEXT NOT NULL DEFAULT '',
            from_addr   TEXT NOT NULL DEFAULT '',
            to_addrs    TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS notification_events (
            event_type  TEXT PRIMARY KEY,
            enabled     INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS notification_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            subject     TEXT NOT NULL,
            recipients  TEXT NOT NULL,
            status      TEXT NOT NULL,
            error       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    # Server-health alert thresholds (percent used) for the memory_high /
    # disk_high alerts — configurable from the Notifications page.
    notif_cols = {row[1] for row in conn.execute("PRAGMA table_info(notification_settings)").fetchall()}
    if "mem_threshold" not in notif_cols:
        conn.execute("ALTER TABLE notification_settings ADD COLUMN mem_threshold INTEGER NOT NULL DEFAULT 90")
        conn.commit()
    if "disk_threshold" not in notif_cols:
        conn.execute("ALTER TABLE notification_settings ADD COLUMN disk_threshold INTEGER NOT NULL DEFAULT 90")
        conn.commit()

    # Remote-session audit log. One row per active connection ("conn") a device
    # reports in its heartbeat (routes/client_api.py): opened when the conn first
    # appears, closed when it disappears or the device stops heartbeating. The
    # RustDesk client reports the device-side connection, not the controller's
    # identity, so this records which device was accessed, when, and for how long
    # — not which tech connected (not available from the OSS client).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS device_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            rustdesk_id   TEXT NOT NULL,
            conn_ref      TEXT NOT NULL,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_device_sessions_rid ON device_sessions(rustdesk_id);
        CREATE INDEX IF NOT EXISTS idx_device_sessions_open ON device_sessions(ended_at);
        CREATE INDEX IF NOT EXISTS idx_device_sessions_started ON device_sessions(started_at);
    """)
    conn.commit()

    # Off-site backup destination (singleton). provider 's3' covers AWS S3,
    # Wasabi, Backblaze B2, DigitalOcean Spaces and any S3-compatible store
    # (boto3, native); provider 'rclone' covers Google Drive, OneDrive,
    # Dropbox, etc. via an rclone remote the admin configures once on the box.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS backup_config (
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
        );
    """)
    conn.commit()
    # retention = local archives kept; offsite_retention = off-site copies kept (0 = keep all).
    backup_cols = {row[1] for row in conn.execute("PRAGMA table_info(backup_config)").fetchall()}
    if "offsite_retention" not in backup_cols:
        conn.execute("ALTER TABLE backup_config ADD COLUMN offsite_retention INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Code-signing config (singleton). Azure Trusted Signing via jsign on this
    # box; the client secret is stored here like the backup secret.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signing_config (
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
    """)
    conn.commit()

    # Roles ("permission levels"): a named capability set users reference by key.
    # See app/permissions.py for the capability catalogue and seeded defaults.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS roles (
            key          TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            permissions  TEXT NOT NULL DEFAULT '[]',   -- JSON list, or '*' for all
            is_system    INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    # Seed the built-in roles once (idempotent). Imported lazily to avoid a
    # circular import at module load.
    import json as _json
    from app.permissions import SYSTEM_ROLES
    for key, spec in SYSTEM_ROLES.items():
        perms = "*" if spec["perms"] == "*" else _json.dumps(spec["perms"])
        conn.execute(
            "INSERT OR IGNORE INTO roles (key, name, description, permissions, is_system)"
            " VALUES (?, ?, ?, ?, 1)",
            (key, spec["name"], spec["description"], perms),
        )
    conn.commit()

    # Public installer download links sent to clients (idempotent)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS download_links (
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
        );
        CREATE INDEX IF NOT EXISTS idx_download_links_group ON download_links(group_id);
    """)
    conn.commit()
    # Details reported by the RustDesk client itself (routes/client_api.py)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS device_info (
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
        );
    """)
    conn.commit()

    # Per-group RustDesk auto-update policy, pushed to reporting clients
    # (routes/client_api.py) and written into new installers.
    #   auto_update: NULL = not managed, 'Y' = on, 'N' = off
    #   auto_update_ts: epoch seconds of the last change — clients echo it
    #   back as "modified_at" once applied, so each change is sent once.
    groups_cols = {row[1] for row in conn.execute("PRAGMA table_info(client_groups)").fetchall()}
    if "auto_update" not in groups_cols:
        conn.execute("ALTER TABLE client_groups ADD COLUMN auto_update TEXT CHECK (auto_update IN ('Y','N'))")
        conn.execute("ALTER TABLE client_groups ADD COLUMN auto_update_ts INTEGER")
        conn.commit()
    # Second managed client setting: allow-remote-config-modification (lets
    # whoever is controlling the device use RustDesk's own window/settings).
    # policy_ts covers every managed setting — clients keep only one
    # "strategy_timestamp", so all settings are sent together under it.
    if "remote_config" not in groups_cols:
        conn.execute("ALTER TABLE client_groups ADD COLUMN remote_config TEXT CHECK (remote_config IN ('Y','N'))")
        conn.execute("ALTER TABLE client_groups ADD COLUMN policy_ts INTEGER")
        conn.execute("UPDATE client_groups SET policy_ts = auto_update_ts WHERE auto_update_ts IS NOT NULL")
        conn.commit()
    info_cols = {row[1] for row in conn.execute("PRAGMA table_info(device_info)").fetchall()}
    if "policy_ts" not in info_cols:
        conn.execute("ALTER TABLE device_info ADD COLUMN policy_ts INTEGER")
        conn.commit()

    link_cols = {row[1] for row in conn.execute("PRAGMA table_info(download_links)").fetchall()}
    if "emailed_to" not in link_cols:
        conn.execute("ALTER TABLE download_links ADD COLUMN emailed_to TEXT")
        conn.execute("ALTER TABLE download_links ADD COLUMN emailed_at TEXT")
        conn.commit()

    conn.close()
