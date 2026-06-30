import sqlite3
from pathlib import Path

DB_PATH = Path("/opt/rustdesk-fleet/fleet.sqlite3")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_migrations() -> None:
    conn = get_db()
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "password_hash" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        conn.commit()
    conn.close()
