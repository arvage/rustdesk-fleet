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
os.environ["FLEET_DISABLE_WATCHERS"] = "1"
os.environ["PROVIDER_DOMAIN"] = "rds.example.com"   # console at admin.rds.example.com   # no background threads on the test DB
sys.path[:0] = [str(DASH_DIR), str(SINGLE_TENANT_DIR)]

import bcrypt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, permissions, ratelimit, tenancy  # noqa: E402
from app.deps import get_db, run_migrations  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import client_api  # noqa: E402

HBBS_DB = DATA_DIR / "data" / "db_v2.sqlite3"
# Tenant #1's host: the single-tenant server_config.host before the migration.
HOST = "rds.example.com"
CONSOLE_HOST = "admin.rds.example.com"


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

    # Tenant #1's relay stack, as setup_server.py init writes it.
    (DATA_DIR / "docker-compose.yml").write_text(
        "services:\n  hbbs:\n    image: rustdesk/rustdesk-server:1.1.16\n    container_name: hbbs\n"
        "  hbbr:\n    image: rustdesk/rustdesk-server:1.1.16\n    container_name: hbbr\n")
    import shutil
    shutil.rmtree(DATA_DIR / "tenants", ignore_errors=True)
    ratelimit.reset()
    tenancy.invalidate_cache()
    permissions.invalidate_cache()
    auth._setup_done.clear()
    client_api._peer_caches.clear()
    yield


def new_client(host: str = HOST) -> TestClient:
    # The session cookie is https_only, so talk to the app over https.
    return TestClient(app, base_url=f"https://{host}")


@pytest.fixture
def client() -> TestClient:
    return new_client()


def create_user(email: str, role: str, password: str = PASSWORD, tenant_id: int = 1) -> dict:
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO users (tenant_id, email, display_name, role, password_hash) VALUES (?, ?, ?, ?, ?)",
        (tenant_id, email, email.split("@")[0], role, pw_hash),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return {"id": user_id, "email": email, "password": password, "tenant_id": tenant_id}


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


def forge_session(data: dict, secret: str = SESSION_SECRET) -> str:
    """A session cookie signed with the test secret, the way Starlette's
    SessionMiddleware signs one, for cookies the app would never issue."""
    from itsdangerous import TimestampSigner
    payload = base64.b64encode(json.dumps(data).encode())
    return TimestampSigner(secret).sign(payload).decode()


def provider_client(email: str = "ops@example.com") -> TestClient:
    """A client signed in to the provider console as a (new) provider admin."""
    from app.console.auth import add_provider_user
    conn = get_db()
    row = conn.execute("SELECT id FROM provider_users WHERE email = ?", (email,)).fetchone()
    uid = row["id"] if row else add_provider_user(conn, email, email.split("@")[0])
    conn.close()
    c = new_client(CONSOLE_HOST)
    c.cookies.set("rdf_provider_session",
                  forge_session({"provider_user_id": uid, "provider_sv": 1}, "provider-console:" + SESSION_SECRET))
    return c


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


# ── Two tenants (tests for tenant isolation) ─────────────────────────────────

HOST_B = "b.rds.example.com"


def _hbbs_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE peer (id TEXT PRIMARY KEY, uuid BLOB, info TEXT, created_at TEXT)")
    conn.commit()
    conn.close()


def add_tenant(tenant_id: int, slug: str, host: str, status: str = "active") -> Path:
    """A tenant with a relay (own hbbs database) and one web domain. Returns
    the hbbs database path."""
    from app.tenant_migration import seed_tenant
    hbbs = DATA_DIR / "tenants" / str(tenant_id) / "data" / "db_v2.sqlite3"
    _hbbs_db(hbbs)
    conn = get_db()
    conn.execute("INSERT INTO tenants (id, slug, display_name, status) VALUES (?, ?, ?, ?)",
                 (tenant_id, slug, slug.title(), status))
    conn.execute(
        "INSERT INTO relays (tenant_id, host, hbbs_port, hbbr_port, pubkey, data_dir, compose_path, hbbs_db_path, status)"
        " VALUES (?, ?, 21125, 21127, ?, ?, ?, ?, 'active')",
        (tenant_id, f"relay-{slug}.example.com", f"PUBKEY{tenant_id}", str(hbbs.parent),
         str(hbbs.parent.parent / "docker-compose.yml"), str(hbbs)),
    )
    conn.execute("INSERT INTO tenant_domains (tenant_id, hostname, kind, is_primary) VALUES (?, ?, 'provider_subdomain', 1)",
                 (tenant_id, host))
    seed_tenant(conn, tenant_id)
    conn.commit()
    conn.close()
    tenancy.invalidate_cache()
    return hbbs


def add_peer_to(hbbs: Path, rustdesk_id: str, uuid: bytes) -> None:
    conn = sqlite3.connect(hbbs)
    conn.execute("INSERT INTO peer (id, uuid, info, created_at) VALUES (?, ?, '{\"ip\": \"198.51.100.7\"}', datetime('now'))",
                 (rustdesk_id, uuid))
    conn.commit()
    conn.close()


def populate(tenant_id: int, hbbs: Path, tag: str) -> dict:
    """One of everything for a tenant, each row marked with `tag` so a test
    can tell whose data a response shows. Returns the ids."""
    rid = {"a": "100000001", "b": "200000002"}.get(tag, "300000003")
    uuid = f"uuid-{tag}".encode()
    add_peer_to(hbbs, rid, uuid)
    admin_user = create_user(f"admin@{tag}.example.com", "admin", tenant_id=tenant_id)
    tech_user = create_user(f"tech@{tag}.example.com", "tech", tenant_id=tenant_id)
    conn = get_db()
    gid = conn.execute(
        "INSERT INTO client_groups (tenant_id, slug, display_name, unattended_password, brand_app_name)"
        " VALUES (?, ?, ?, 'pw', ?)",
        (tenant_id, f"group-{tag}", f"Group-{tag.upper()}-secret", f"App {tag}"),
    ).lastrowid
    conn.execute("INSERT INTO devices (tenant_id, rustdesk_id, group_id, label, status) VALUES (?, ?, ?, ?, 'registered')",
                 (tenant_id, rid, gid, f"Device-{tag.upper()}-secret"))
    conn.execute("INSERT INTO device_info (tenant_id, rustdesk_id, hostname, heartbeat_at, sysinfo_at)"
                 " VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                 (tenant_id, rid, f"HOST-{tag.upper()}-secret"))
    sid = conn.execute("INSERT INTO device_sessions (tenant_id, rustdesk_id, conn_ref, peer_id, peer_name)"
                       " VALUES (?, ?, 'c1', '555', ?)", (tenant_id, rid, f"Peer-{tag.upper()}-secret")).lastrowid
    conn.execute("INSERT INTO device_file_transfers (tenant_id, rustdesk_id, session_id, direction, file_count, nonce, path)"
                 " VALUES (?, ?, ?, 0, 1, 'n1', ?)", (tenant_id, rid, sid, f"C:/Files-{tag.upper()}-secret"))
    out_dir = tenancy.tenant_files_dir(tenant_id) / "installers"
    out_dir.mkdir(parents=True, exist_ok=True)
    inst_file = out_dir / f"RemoteSupport-group-{tag}-1.4.9-linux.sh"
    inst_file.write_text(f"#!/bin/sh\n# installer {tag}\n")
    iid = conn.execute(
        "INSERT INTO installers (tenant_id, group_id, platform, rustdesk_version, status, unsigned_path, sha256_unsigned)"
        " VALUES (?, ?, 'linux', '1.4.9', 'built', ?, 'abc')", (tenant_id, gid, str(inst_file))).lastrowid
    token = f"token-{tag}-" + "x" * 20
    lid = conn.execute("INSERT INTO download_links (tenant_id, token, group_id, note) VALUES (?, ?, ?, ?)",
                       (tenant_id, token, gid, f"Note-{tag.upper()}-secret")).lastrowid
    dgid = conn.execute("INSERT INTO dynamic_groups (tenant_id, name, rules) VALUES (?, ?, ?)",
                        (tenant_id, f"Dyn-{tag.upper()}-secret",
                         json.dumps([{"field": "hostname", "op": "contains", "value": "HOST"}]))).lastrowid
    conn.execute("INSERT INTO roles (tenant_id, key, name, permissions) VALUES (?, ?, ?, '[]')",
                 (tenant_id, f"role_{tag}", f"Role-{tag.upper()}-secret"))
    cred = conn.execute("INSERT INTO webauthn_credentials (tenant_id, user_id, credential_id, public_key, nickname)"
                        " VALUES (?, ?, ?, x'00', ?)",
                        (tenant_id, tech_user["id"], f"Y3JlZC0{tag}", f"Key-{tag.upper()}-secret")).lastrowid
    conn.execute("INSERT INTO user_group_access (tenant_id, user_id, group_id) VALUES (?, ?, ?)",
                 (tenant_id, tech_user["id"], gid))
    conn.execute("INSERT INTO email_templates (tenant_id, key, subject) VALUES (?, 'device_offline', ?)",
                 (tenant_id, f"Subject-{tag.upper()}-secret"))
    conn.execute("INSERT INTO notification_settings (tenant_id, enabled, smtp_host, to_addrs) VALUES (?, 0, ?, ?)",
                 (tenant_id, f"smtp-{tag}.example.com", f"ops@{tag}.example.com"))
    conn.execute("INSERT INTO provisioning_events (tenant_id, event, detail) VALUES (?, 'group_created', ?)",
                 (tenant_id, f"Event-{tag.upper()}-secret"))
    conn.commit()
    from app.console.tenants import create_invite
    invite_token = create_invite(conn, tenant_id, f"t{tag}", f"new@{tag}.example.com", "New", "test").rsplit("/", 1)[1]
    conn.close()
    return {"tenant_id": tenant_id, "admin": admin_user, "tech": tech_user, "group_id": gid,
            "group_slug": f"group-{tag}", "rid": rid, "uuid": uuid, "installer_id": iid,
            "installer_file": inst_file.name, "link_id": lid, "token": token, "dynamic_group_id": dgid,
            "role_key": f"role_{tag}", "cred_id": cred, "user_id": tech_user["id"], "session_id": sid,
            "hbbs": hbbs, "secret": f"{tag.upper()}-secret", "invite_token": invite_token}


@pytest.fixture
def two_tenants() -> dict:
    """Tenant A = #1 on HOST (the migrated deployment), tenant B = #2 on HOST_B,
    each with its own relay, users, groups, devices and everything else."""
    a = populate(1, HBBS_DB, "a")
    hbbs_b = add_tenant(2, "globex", HOST_B)
    b = populate(2, hbbs_b, "b")
    return {"a": a, "b": b}


def signed_in_at(user: dict, host: str) -> TestClient:
    c = new_client(host)
    r = login(c, user)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text[:300]
    return c


def all_routes() -> list[tuple[str, str]]:
    """[(method, path)] for every API route in the app, however the routers
    are nested (FastAPI 0.138 wraps included routers, so app.routes alone
    doesn't list them)."""
    from fastapi.routing import APIRoute, iter_route_contexts
    out = []
    for ctx in iter_route_contexts(app.routes):
        if isinstance(ctx.route, APIRoute):
            for method in sorted(ctx.methods or ()):
                out.append((method, ctx.path))
    return out


# ── Fake system calls for the provider console (docker, rdf-helper) ─────────

class FakeSystem:
    """Stands in for app.console.system.SYSTEM: records every command, keeps
    track of which compose stacks are up, writes the public key hbbs would
    write on `up`, and fails any command containing a string in `fail`."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail: dict[str, str] = {}
        self.up: dict[str, list[str]] = {}      # compose path -> container names

    def _result(self, args):
        from app.console.system import Result
        joined = " ".join(args)
        for needle, msg in self.fail.items():
            if needle in joined:
                return Result(1, "", msg)
        return Result(0, "", "")

    def run(self, args, timeout=300):
        self.calls.append(list(args))
        if args[:2] == ["docker", "ps"]:
            from app.console.system import Result
            lines = [f"{n}\tUp 5 minutes" for names in self.up.values() for n in names]
            return Result(0, "\n".join(lines), "")
        return self._result(args)

    def compose(self, compose_path, *args, timeout=300):
        r = self.run(["docker", "compose", "-f", compose_path, *args], timeout)
        if r.ok and args and args[0] in ("up", "restart", "start"):
            import re
            text = Path(compose_path).read_text() if Path(compose_path).exists() else ""
            names = re.findall(r"container_name:\s*(\S+)", text) or ["hbbs", "hbbr"]
            self.up[compose_path] = names
            pub = Path(compose_path).parent / "data" / "id_ed25519.pub"
            if not pub.exists():
                pub.parent.mkdir(parents=True, exist_ok=True)
                pub.write_text("PUB-" + Path(compose_path).parent.parent.name)
        elif r.ok and args and args[0] in ("stop", "down"):
            self.up.pop(compose_path, None)
        return r

    def helper(self, *args, timeout=300):
        return self.run(["sudo", "-n", "/usr/local/sbin/rdf-helper", *args], timeout)

    def running_containers(self):
        return {n: "Up 5 minutes" for names in self.up.values() for n in names}

    def commands(self, prefix: str) -> list[str]:
        return [" ".join(c) for c in self.calls if " ".join(c).startswith(prefix)]


@pytest.fixture
def fake_system(monkeypatch):
    from app.console import system
    fake = FakeSystem()
    monkeypatch.setattr(system, "SYSTEM", fake)
    return fake
