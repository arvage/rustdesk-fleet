import sqlite3
from pathlib import Path

DB_PATH = Path("/opt/rustdesk-fleet/fleet.sqlite3")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
