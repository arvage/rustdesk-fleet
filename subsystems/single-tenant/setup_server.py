"""
setup_server.py — single-tenant RustDesk fleet bootstrap.

Replaces the old per-tenant allocate_tenant.py allocator. There is now
exactly one hbbs/hbbr pair for the whole fleet. This script:

    1. Stands up that one server (idempotent — safe to re-run)
    2. Captures its keypair into server_config
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
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

FLEET_ROOT = Path("/opt/rustdesk-fleet")
DB_PATH = FLEET_ROOT / "fleet.sqlite3"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
COMPOSE_SRC = Path(__file__).parent / "docker-compose.yml"
COMPOSE_DST = FLEET_ROOT / "docker-compose.yml"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")


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


def log_event(conn: sqlite3.Connection, event: str, detail: str = "", user_email: str = "") -> None:
    conn.execute(
        "INSERT INTO provisioning_events (event, detail, user_email) VALUES (?, ?, ?)",
        (event, detail, user_email or None),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Server lifecycle (singleton)
# ---------------------------------------------------------------------------

def docker_compose_up() -> None:
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_DST), "up", "-d"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ProvisioningError(f"docker compose up failed:\n{result.stderr}")


IMAGE_TAG_RE = re.compile(r"rustdesk/rustdesk-server:([\w.\-]+)")

_latest_version_cache: dict = {"version": None, "checked_at": 0.0}
_LATEST_VERSION_TTL_S = 3600


def get_server_version() -> str | None:
    """Current image tag pinned in the deployed compose file."""
    if not COMPOSE_DST.exists():
        return None
    match = IMAGE_TAG_RE.search(COMPOSE_DST.read_text())
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


def update_server(version: str, user_email: str = "") -> None:
    """Pin the compose file to `version`, pull the new image, and restart the stack."""
    if not COMPOSE_DST.exists():
        raise ProvisioningError("Server not initialised — nothing to update.")

    conn = get_db()
    current = get_server_version()

    text = COMPOSE_DST.read_text()
    new_text = IMAGE_TAG_RE.sub(f"rustdesk/rustdesk-server:{version}", text)
    COMPOSE_DST.write_text(new_text)

    log_event(conn, "server_update_start", f"{current} -> {version}", user_email)
    try:
        pull = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_DST), "pull"],
            capture_output=True, text=True,
        )
        if pull.returncode != 0:
            raise ProvisioningError(f"docker compose pull failed:\n{pull.stderr}")

        docker_compose_up()
        conn.execute("UPDATE server_config SET updated_at=datetime('now') WHERE id=1")
        conn.commit()
        log_event(conn, "server_updated", f"{current} -> {version}", user_email)
    except Exception as e:
        log_event(conn, "server_update_failed", str(e)[:500], user_email)
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


def init_server(host: str, hbbs_port: int = 21115, hbbr_port: int = 21117) -> dict:
    conn = get_db()
    ensure_schema(conn)

    (FLEET_ROOT / "data").mkdir(parents=True, exist_ok=True)
    COMPOSE_DST.write_text(COMPOSE_SRC.read_text())
    log_event(conn, "compose_written", str(COMPOSE_DST))

    existing = conn.execute("SELECT * FROM server_config WHERE id = 1").fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO server_config (id, host, hbbs_port, hbbr_port, data_dir, compose_path, status)
            VALUES (1, ?, ?, ?, ?, ?, 'provisioning')
            """,
            (host, hbbs_port, hbbr_port, str(FLEET_ROOT / "data"), str(COMPOSE_DST)),
        )
        conn.commit()

    log_event(conn, "stack_up_attempt")
    try:
        docker_compose_up()
        log_event(conn, "stack_up")

        pubkey = wait_for_pubkey()
        log_event(conn, "key_captured")

        conn.execute(
            "UPDATE server_config SET status='active', pubkey=?, host=?, updated_at=datetime('now') WHERE id=1",
            (pubkey, host),
        )
        conn.commit()
    except Exception as e:
        log_event(conn, "provisioning_failed", str(e))
        raise

    row = conn.execute("SELECT * FROM server_config WHERE id=1").fetchone()
    return dict(row)


def get_status() -> dict | None:
    conn = get_db()
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM server_config WHERE id=1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Client group management
# ---------------------------------------------------------------------------

def create_group(
    slug: str, display_name: str, unattended_password: str | None = None, user_email: str = ""
) -> dict:
    if not SLUG_RE.match(slug):
        raise ProvisioningError(f"Invalid slug '{slug}'. Use lowercase letters, digits, hyphens; 3-50 chars.")

    conn = get_db()
    ensure_schema(conn)

    if conn.execute("SELECT 1 FROM client_groups WHERE slug = ?", (slug,)).fetchone():
        raise ProvisioningError(f"Client group '{slug}' already exists.")

    cur = conn.execute(
        "INSERT INTO client_groups (slug, display_name, unattended_password) VALUES (?, ?, ?)",
        (slug, display_name, unattended_password or None),
    )
    conn.commit()
    log_event(conn, "group_created", slug, user_email)
    return {"id": cur.lastrowid, "slug": slug, "display_name": display_name}


def list_groups() -> list[sqlite3.Row]:
    conn = get_db()
    ensure_schema(conn)
    return conn.execute(
        "SELECT slug, display_name, status, created_at FROM client_groups ORDER BY created_at"
    ).fetchall()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RustDesk single-tenant fleet setup")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Stand up the single hbbs/hbbr server (idempotent)")
    p_init.add_argument("--host", required=True, help="Public host/IP clients will connect to")
    p_init.add_argument("--hbbs-port", type=int, default=21115)
    p_init.add_argument("--hbbr-port", type=int, default=21117)

    sub.add_parser("status", help="Show current server config/status")

    p_group = sub.add_parser("group", help="Manage client groups")
    group_sub = p_group.add_subparsers(dest="group_cmd", required=True)

    p_gcreate = group_sub.add_parser("create")
    p_gcreate.add_argument("--slug", required=True)
    p_gcreate.add_argument("--display-name", required=True)

    group_sub.add_parser("list")

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
        print(f"  TCP+UDP {result['hbbs_port']}-{result['hbbs_port']+4}, TCP {result['hbbr_port']}, {result['hbbr_port']+2}")
        print("  (network_mode: host is used, so the container binds RustDesk's own default port range)")

    elif args.cmd == "status":
        row = get_status()
        if row is None:
            print("Server not yet initialized. Run `init` first.")
        else:
            for k, v in row.items():
                print(f"  {k}: {v}")

    elif args.cmd == "group":
        if args.group_cmd == "create":
            try:
                result = create_group(args.slug, args.display_name)
            except ProvisioningError as e:
                sys.exit(f"Failed: {e}")
            print(f"Group '{result['slug']}' created.")
        elif args.group_cmd == "list":
            rows = list_groups()
            if not rows:
                print("No client groups yet.")
            for r in rows:
                print(f"{r['slug']:<28} {r['status']:<10} {r['display_name']}")


if __name__ == "__main__":
    main()
