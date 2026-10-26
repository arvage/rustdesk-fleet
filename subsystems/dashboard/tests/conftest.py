"""Test setup: a throwaway data directory, a fresh fleet database per test,
and a TestClient for the dashboard.

The app reads its paths and secrets when it is imported, so the environment
is set here, before anything under app/ is imported.
"""
import base64
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

DASH_DIR = Path(__file__).resolve().parents[1]
SINGLE_TENANT_DIR = DASH_DIR.parent / "single-tenant"
SCHEMA = SINGLE_TENANT_DIR / "schema.sql"

DATA_DIR = Path(tempfile.mkdtemp(prefix="rdf-tests-"))
SESSION_SECRET = "test-secret-" + "x" * 40
SETUP_TOKEN = "test-setup-token"
PASSWORD = "correct-password-1"

os.environ["FLEET_DATA_DIR"] = str(DATA_DIR)
os.environ["SESSION_SECRET"] = SESSION_SECRET
os.environ["SETUP_TOKEN"] = SETUP_TOKEN
sys.path[:0] = [str(DASH_DIR), str(SINGLE_TENANT_DIR)]

import bcrypt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, ratelimit, tenancy  # noqa: E402
from app.deps import get_db, run_migrations  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import client_api  # noqa: E402

HBBS_DB = DATA_DIR / "data" / "db_v2.sqlite3"
# Tenant #1's host: the single-tenant server_config.host before the migration.
HOST = "rds.example.com"


@pytest.fixture(autouse=True)
def fresh_state():
    """Every test starts with an empty, migrated fleet DB, an empty hbbs peer
    table, no rate-limit history and no cached setup/peer state."""
    db = DATA_DIR / "fleet.sqlite3"
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA.read_text())
    conn.execute(
        "INSERT INTO server_config (id, host, pubkey, data_dir, compose_path, status) "
        "VALUES (1, ?, 'PUBKEY1', ?, ?, 'active')",
        (HOST, str(DATA_DIR / "data"), str(DATA_DIR / "docker-compose.yml")),
    )
    conn.commit()
    conn.close()
    run_migrations()

    HBBS_DB.parent.mkdir(parents=True, exist_ok=True)
    HBBS_DB.unlink(missing_ok=True)
    conn = sqlite3.connect(HBBS_DB)
    conn.execute("CREATE TABLE peer (id TEXT PRIMARY KEY, uuid BLOB, info TEXT, created_at TEXT)")
    conn.commit()
    conn.close()

    ratelimit.reset()
    tenancy.invalidate_cache()
    auth._setup_done = False
    client_api._peer_cache = {}
    client_api._peer_cache_at = 0.0
    client_api._peer_read_at = 0.0
    yield


def new_client(host: str = HOST) -> TestClient:
    # The session cookie is https_only, so talk to the app over https.
    return TestClient(app, base_url=f"https://{host}")


@pytest.fixture
def client() -> TestClient:
    return new_client()


def create_user(email: str, role: str, password: str = PASSWORD) -> dict:
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO users (email, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
        (email, email.split("@")[0], role, pw_hash),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return {"id": user_id, "email": email, "password": password}


@pytest.fixture
def admin() -> dict:
    return create_user("admin@example.com", "admin")


@pytest.fixture
def tech() -> dict:
    return create_user("tech@example.com", "tech")


def login(client: TestClient, user: dict, password: str | None = None):
    return client.post(
        "/login",
        data={"email": user["email"], "password": password or user["password"]},
        follow_redirects=False,
    )


def signed_in(user: dict) -> TestClient:
    """A new client (its own cookie jar) signed in as user."""
    c = new_client()
    r = login(c, user)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text[:300]
    return c


def get_status(c: TestClient, path: str):
    """(status, redirect location) for a GET without following redirects."""
    r = c.get(path, follow_redirects=False)
    return r.status_code, r.headers.get("location")


def forge_session(data: dict) -> str:
    """A session cookie signed with the test secret, the way Starlette's
    SessionMiddleware signs one, for cookies the app would never issue."""
    from itsdangerous import TimestampSigner
    payload = base64.b64encode(json.dumps(data).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def add_peer(rustdesk_id: str, uuid: bytes) -> None:
    conn = sqlite3.connect(HBBS_DB)
    conn.execute("INSERT INTO peer (id, uuid, info, created_at) VALUES (?, ?, '{}', datetime('now'))",
                 (rustdesk_id, uuid))
    conn.commit()
    conn.close()


def db_rows(sql: str, *params) -> list:
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows
