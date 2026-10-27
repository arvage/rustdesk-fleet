"""
setup_server.py — single-tenant RustDesk fleet bootstrap.

Stands up and maintains a tenant's hbbs/hbbr relay. Each tenant has one
relay, described by its row in `relays` (host, ports, keypair, data dir,
compose file). Today only tenant #1's relay exists, the one this script has
always managed; creating relays for new tenants is phase 1b. This script:

    1. Stands up tenant #1's relay (idempotent — safe to re-run)
    2. Captures its keypair into its relays row
    3. Provides client_group management (create/list groups — these are
       just labels for devices/installers, not separate infrastructure)

Run directly for CLI usage:
    python3 setup_server.py init --host rds.example.com
    python3 setup_server.py status
    python3 setup_server.py group create --slug acme-corp --display-name "Acme Corp"
    python3 setup_server.py group list
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

FLEET_ROOT = Path(os.environ.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet")
DB_PATH = FLEET_ROOT / "fleet.sqlite3"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
COMPOSE_SRC = Path(__file__).parent / "docker-compose.yml"
# Tenant #1's relay stack (the one `init` stands up).
COMPOSE_DST = FLEET_ROOT / "docker-compose.yml"
LEGACY_TENANT_ID = 1

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")


# Characters that can't be embedded safely in the installers: the password is
# pasted inside NSIS strings ("'` delimit, $ expands), shell PW="..." ($ ` \
# expand) and RustDesk.toml password = "..." (" \ end/escape the string).
PASSWORD_FORBIDDEN = set('"\'`$\\')


def password_problem(pw: str | None) -> str | None:
    """Why an unattended password can't go into an installer, or None if it's fine."""
    if not pw:
        return None
    bad = sorted({c for c in pw if c in PASSWORD_FORBIDDEN})
    if bad:
        return ("Unattended password can't contain " + " ".join(bad)
                + " — these break the installers. Other symbols and spaces are fine.")
    if any(ord(c) < 32 or ord(c) == 127 for c in pw):
        return "Unattended password can't contain line breaks or other control characters."
    return None


class ProvisioningError(RuntimeError):
    pass


def get_db() -> sqlite3.Connection:
    FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def log_event(conn: sqlite3.Connection, event: str, detail: str = "", user_email: str = "",
              *, tenant_id: int | None) -> None:
    conn.execute(
        "INSERT INTO provisioning_events (tenant_id, event, detail, user_email) VALUES (?, ?, ?, ?)",
        (tenant_id, event, detail, user_email or None),
    )
    conn.commit()


def _has_tenancy(conn: sqlite3.Connection) -> bool:
    """Whether the dashboard's tenancy migration has run on this database.
    On a brand-new install `init` runs before the dashboard ever starts; it
    then records the relay in server_config, and the dashboard's migration
    turns that into tenant #1's relays row and domain."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'relays'"
    ).fetchone() is not None and "tenant_id" in [
        r[1] for r in conn.execute("PRAGMA table_info(provisioning_events)")]


def get_relay(tenant_id: int) -> dict | None:
    """A tenant's relays row, or None."""
    conn = get_db()
    try:
        if not _has_tenancy(conn):
            return None
        row = conn.execute("SELECT * FROM relays WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def relay_compose_path(relay: dict | None) -> Path:
    return Path(relay["compose_path"]) if relay and relay.get("compose_path") else COMPOSE_DST


_CONTAINER_NAME_RE = re.compile(r"^\s*container_name:\s*['\"]?([\w.-]+)", re.M)


def relay_containers(relay: dict | None) -> list[str]:
    """Container names in a relay's compose file (["hbbs", "hbbr"] for
    tenant #1's)."""
    path = relay_compose_path(relay)
    try:
        names = _CONTAINER_NAME_RE.findall(path.read_text())
    except OSError:
        names = []
    return names or ["hbbs", "hbbr"]


# ---------------------------------------------------------------------------
# Server lifecycle (singleton)
# ---------------------------------------------------------------------------

def docker_compose_up(compose_path: Path = COMPOSE_DST) -> None:
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "up", "-d"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ProvisioningError(f"docker compose up failed:\n{result.stderr}")


IMAGE_TAG_RE = re.compile(r"rustdesk/rustdesk-server:([\w.\-]+)")

_latest_version_cache: dict = {"version": None, "checked_at": 0.0}
_LATEST_VERSION_TTL_S = 3600


def get_server_version(relay: dict | None) -> str | None:
    """Current image tag pinned in a relay's deployed compose file."""
    path = relay_compose_path(relay)
    if not path.exists():
        return None
    match = IMAGE_TAG_RE.search(path.read_text())
    return match.group(1) if match else None


def get_latest_server_version() -> str | None:
    """Latest rustdesk-server release tag from GitHub, cached for an hour.

    Returns None on any network/API failure rather than raising — this is
    a best-effort check, not something that should break the status page.
    """
    now = time.time()
    if _latest_version_cache["version"] and now - _latest_version_cache["checked_at"] < _LATEST_VERSION_TTL_S:
        return _latest_version_cache["version"]

    try:
        resp = requests.get(
            "https://api.github.com/repos/rustdesk/rustdesk-server/releases/latest",
            timeout=5,
        )
        resp.raise_for_status()
        tag = resp.json()["tag_name"].lstrip("v")
    except Exception:
        return _latest_version_cache["version"]

    _latest_version_cache["version"] = tag
    _latest_version_cache["checked_at"] = now
    return tag


def update_server(tenant_id: int, version: str, user_email: str = "") -> None:
    """Pin a tenant's relay compose file to `version`, pull the new image, and
    restart that relay."""
    relay = get_relay(tenant_id)
    compose = relay_compose_path(relay)
    if relay is None or not compose.exists():
        raise ProvisioningError("Server not initialised — nothing to update.")

    conn = get_db()
    current = get_server_version(relay)

    text = compose.read_text()
    new_text = IMAGE_TAG_RE.sub(f"rustdesk/rustdesk-server:{version}", text)
    compose.write_text(new_text)

    log_event(conn, "server_update_start", f"{current} -> {version}", user_email, tenant_id=tenant_id)
    try:
        pull = subprocess.run(
            ["docker", "compose", "-f", str(compose), "pull"],
            capture_output=True, text=True,
        )
        if pull.returncode != 0:
            raise ProvisioningError(f"docker compose pull failed:\n{pull.stderr}")

        docker_compose_up(compose)
        conn.execute("UPDATE relays SET updated_at=datetime('now') WHERE tenant_id=?", (tenant_id,))
        conn.commit()
        log_event(conn, "server_updated", f"{current} -> {version}", user_email, tenant_id=tenant_id)
    except Exception as e:
        log_event(conn, "server_update_failed", str(e)[:500], user_email, tenant_id=tenant_id)
        raise


def wait_for_pubkey(timeout_s: int = 30) -> str:
    pubkey_file = FLEET_ROOT / "data" / "id_ed25519.pub"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if pubkey_file.exists():
            content = pubkey_file.read_text().strip()
            if content:
                return content
        time.sleep(1)
    raise ProvisioningError(
        f"Timed out waiting for {pubkey_file} after {timeout_s}s. "
        "Check `docker logs hbbs` for startup errors."
    )


def init_server(host: str, hbbs_port: int = 21116, hbbr_port: int = 21117) -> dict:
    """Stand up tenant #1's relay and record it. Idempotent."""
    conn = get_db()
    ensure_schema(conn)
    tenancy = _has_tenancy(conn)
    tid = LEGACY_TENANT_ID

    def event(name: str, detail: str = "") -> None:
        if tenancy:
            log_event(conn, name, detail, tenant_id=tid)
        else:
            conn.execute("INSERT INTO provisioning_events (event, detail) VALUES (?, ?)", (name, detail))
            conn.commit()

    data_dir = FLEET_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    COMPOSE_DST.write_text(COMPOSE_SRC.read_text())
    event("compose_written", str(COMPOSE_DST))

    if tenancy:
        if not conn.execute("SELECT 1 FROM relays WHERE tenant_id = ?", (tid,)).fetchone():
            conn.execute(
                """INSERT INTO relays (tenant_id, host, hbbs_port, hbbr_port, data_dir, compose_path,
                                       hbbs_db_path, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'provisioning')""",
                (tid, host, hbbs_port, hbbr_port, str(data_dir), str(COMPOSE_DST),
                 str(data_dir / "db_v2.sqlite3")),
            )
        _ensure_legacy_domain(conn, host)
    elif conn.execute("SELECT 1 FROM server_config WHERE id = 1").fetchone() is None:
        # Brand-new database the dashboard hasn't migrated yet: the migration
        # reads this row once to create tenant #1's relay and domain.
        conn.execute(
            """
            INSERT INTO server_config (id, host, hbbs_port, hbbr_port, data_dir, compose_path, status)
            VALUES (1, ?, ?, ?, ?, ?, 'provisioning')
            """,
            (host, hbbs_port, hbbr_port, str(data_dir), str(COMPOSE_DST)),
        )
    conn.commit()

    event("stack_up_attempt")
    try:
        docker_compose_up()
        event("stack_up")

        pubkey = wait_for_pubkey()
        event("key_captured")

        if tenancy:
            conn.execute(
                "UPDATE relays SET status='active', pubkey=?, host=?, updated_at=datetime('now') WHERE tenant_id=?",
                (pubkey, host, tid),
            )
        else:
            conn.execute(
                "UPDATE server_config SET status='active', pubkey=?, host=?, updated_at=datetime('now') WHERE id=1",
                (pubkey, host),
            )
        conn.commit()
    except Exception as e:
        event("provisioning_failed", str(e))
        raise

    if tenancy:
        return dict(conn.execute("SELECT * FROM relays WHERE tenant_id = ?", (tid,)).fetchone())
    return dict(conn.execute("SELECT * FROM server_config WHERE id = 1").fetchone())


def _ensure_legacy_domain(conn: sqlite3.Connection, host: str) -> None:
    """Give tenant #1 its host as a web domain if it has none yet (a fresh
    install where the dashboard started before the relay existed)."""
    if conn.execute("SELECT 1 FROM tenant_domains WHERE tenant_id = ?", (LEGACY_TENANT_ID,)).fetchone():
        return
    hostname = host.strip().lower().split("://")[-1].split("/")[0]
    if hostname.count(":") == 1:
        hostname = hostname.split(":")[0]
    if hostname:
        conn.execute(
            """INSERT OR IGNORE INTO tenant_domains (tenant_id, hostname, kind, verified_at, tls_status, is_primary)
               VALUES (?, ?, 'legacy', datetime('now'), 'active', 1)""",
            (LEGACY_TENANT_ID, hostname),
        )


def get_status(tenant_id: int = LEGACY_TENANT_ID) -> dict | None:
    """A tenant's relay (host, ports, pubkey, status), or None."""
    return get_relay(tenant_id)


# ---------------------------------------------------------------------------
# Client group management
# ---------------------------------------------------------------------------

def create_group(
    tenant_id: int, slug: str, display_name: str, unattended_password: str | None = None, user_email: str = ""
) -> dict:
    if not SLUG_RE.match(slug):
        raise ProvisioningError(f"Invalid slug '{slug}'. Use lowercase letters, digits, hyphens; 3-50 chars.")
    problem = password_problem(unattended_password)
    if problem:
        raise ProvisioningError(problem)

    conn = get_db()
    ensure_schema(conn)

    if conn.execute("SELECT 1 FROM client_groups WHERE tenant_id = ? AND slug = ?", (tenant_id, slug)).fetchone():
        raise ProvisioningError(f"Client group '{slug}' already exists.")

    cur = conn.execute(
        "INSERT INTO client_groups (tenant_id, slug, display_name, unattended_password) VALUES (?, ?, ?, ?)",
        (tenant_id, slug, display_name, unattended_password or None),
    )
    conn.commit()
    log_event(conn, "group_created", slug, user_email, tenant_id=tenant_id)
    return {"id": cur.lastrowid, "slug": slug, "display_name": display_name}


def list_groups(tenant_id: int) -> list[sqlite3.Row]:
    conn = get_db()
    ensure_schema(conn)
    return conn.execute(
        "SELECT slug, display_name, status, created_at FROM client_groups WHERE tenant_id = ? ORDER BY created_at",
        (tenant_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RustDesk single-tenant fleet setup")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Stand up the single hbbs/hbbr server (idempotent)")
    p_init.add_argument("--host", required=True, help="Public host/IP clients will connect to")
    p_init.add_argument("--hbbs-port", type=int, default=21116)
    p_init.add_argument("--hbbr-port", type=int, default=21117)

    sub.add_parser("status", help="Show current server config/status")

    p_group = sub.add_parser("group", help="Manage client groups")
    group_sub = p_group.add_subparsers(dest="group_cmd", required=True)

    p_gcreate = group_sub.add_parser("create")
    p_gcreate.add_argument("--tenant", type=int, default=LEGACY_TENANT_ID, help="Tenant id (default: 1)")
    p_gcreate.add_argument("--slug", required=True)
    p_gcreate.add_argument("--display-name", required=True)

    p_glist = group_sub.add_parser("list")
    p_glist.add_argument("--tenant", type=int, default=LEGACY_TENANT_ID, help="Tenant id (default: 1)")

    args = parser.parse_args()

    if args.cmd == "init":
        try:
            result = init_server(args.host, args.hbbs_port, args.hbbr_port)
        except ProvisioningError as e:
            sys.exit(f"Setup failed: {e}")
        print(f"Server is {result['status']}.")
        print(f"  host:      {result['host']}")
        print(f"  hbbs port: {result['hbbs_port']}")
        print(f"  hbbr port: {result['hbbr_port']}")
        print(f"  pubkey:    {result['pubkey']}")
        print()
        print("Open these ports in the Lightsail networking tab if not already open:")
        print(f"  TCP {result['hbbs_port']-1}-{result['hbbr_port']+2}, UDP {result['hbbs_port']}")
        print("  (network_mode: host is used, so the container binds RustDesk's own default port range)")

    elif args.cmd == "status":
        row = get_status()
        if row is None:
            conn = get_db()
            ensure_schema(conn)
            if not _has_tenancy(conn):
                legacy = conn.execute("SELECT * FROM server_config WHERE id = 1").fetchone()
                row = dict(legacy) if legacy else None
            conn.close()
        if row is None:
            print("Server not yet initialized. Run `init` first.")
        else:
            for k, v in row.items():
                print(f"  {k}: {v}")

    elif args.cmd == "group":
        if args.group_cmd == "create":
            try:
                result = create_group(args.tenant, args.slug, args.display_name)
            except ProvisioningError as e:
                sys.exit(f"Failed: {e}")
            print(f"Group '{result['slug']}' created.")
        elif args.group_cmd == "list":
            rows = list_groups(args.tenant)
            if not rows:
                print("No client groups yet.")
            for r in rows:
                print(f"{r['slug']:<28} {r['status']:<10} {r['display_name']}")


if __name__ == "__main__":
    main()
