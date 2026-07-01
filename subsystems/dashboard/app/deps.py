import json
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

    conn.close()
