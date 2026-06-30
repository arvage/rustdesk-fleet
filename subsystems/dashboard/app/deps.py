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


def run_migrations() -> None:
    conn = get_db()
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "password_hash" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        conn.commit()
    conn.close()
