"""
provisioning/allocate_tenant.py

Owns the full lifecycle of standing up a new tenant's isolated hbbs/hbbr stack:

    1. Pick the next free port_index and derive that tenant's port block
    2. Lay out its data directory
    3. Render its docker-compose.yml from the shared template
    4. `docker compose up -d`
    5. Wait for hbbs to generate its keypair, capture the public key
    6. Persist everything to the fleet DB

This is deliberately a library, not a one-shot script — the dashboard's
"create tenant" action and any CLI usage both call into the same functions,
so there is exactly one code path that can allocate a port block.

Run directly for CLI usage:
    python3 allocate_tenant.py create --slug acme-corp --display-name "Acme Corp"
    python3 allocate_tenant.py list
    python3 allocate_tenant.py destroy --slug acme-corp   # decommission
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    sys.exit("Missing dependency. Install with: pip install jinja2 --break-system-packages")


# ---------------------------------------------------------------------------
# Configuration — adjust these for your actual Lightsail layout
# ---------------------------------------------------------------------------

FLEET_ROOT = Path("/opt/rustdesk-fleet")           # base dir for all tenant data (on the server, NOT in the repo)
DB_PATH = FLEET_ROOT / "fleet.sqlite3"
SCHEMA_PATH = Path(__file__).parent.parent.parent / "shared" / "db" / "schema.sql"
TEMPLATE_DIR = Path(__file__).parent / "compose-templates"
TEMPLATE_NAME = "tenant-stack.yml.j2"

PUBLIC_HOST = "rustdesk.example.com"               # the host clients will be told to use
RUSTDESK_SERVER_TAG = "1.1.14"                      # pin a version; bump deliberately, not via :latest

# Port scheme: tenant N occupies a 10-port block starting at this base.
# index 0 -> 21100, index 1 -> 21110, etc. Deliberately offset from
# RustDesk's own defaults (21115-21119) to avoid any accidental collision
# with a stray default-config instance someone spins up for testing.
PORT_BASE = 21100
PORT_STRIDE = 10

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")


class ProvisioningError(RuntimeError):
    pass


@dataclass
class TenantPorts:
    port_index: int
    hbbs_port: int
    hbbr_port: int


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def log_event(conn: sqlite3.Connection, tenant_id: int | None, event: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO provisioning_events (tenant_id, event, detail) VALUES (?, ?, ?)",
        (tenant_id, event, detail),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

def next_port_index(conn: sqlite3.Connection) -> int:
    """
    Next free index = (max existing index) + 1. We never reuse an index,
    even from a decommissioned tenant — reusing a port block for a new
    tenant after an old one is torn down is exactly the kind of subtle
    mistake that could let a stale installer or cached client config
    register against the wrong tenant later.
    """
    row = conn.execute("SELECT MAX(port_index) AS m FROM tenants").fetchone()
    return 0 if row["m"] is None else row["m"] + 1


def derive_ports(port_index: int) -> TenantPorts:
    base = PORT_BASE + (port_index * PORT_STRIDE)
    return TenantPorts(
        port_index=port_index,
        hbbs_port=base,          # base, base+1, base+1/udp, base+3 used by hbbs
        hbbr_port=base + 5,      # base+5, base+7 used by hbbr — kept clear of hbbs' range
    )


# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------

def render_compose(slug: str, ports: TenantPorts, data_dir: Path) -> Path:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), trim_blocks=True, lstrip_blocks=True)
    tmpl = env.get_template(TEMPLATE_NAME)
    rendered = tmpl.render(
        slug=slug,
        port_index=ports.port_index,
        hbbs_port=ports.hbbs_port,
        hbbr_port=ports.hbbr_port,
        data_dir=str(data_dir),
        rustdesk_server_tag=RUSTDESK_SERVER_TAG,
    )
    compose_path = data_dir / "docker-compose.yml"
    compose_path.write_text(rendered)
    return compose_path


def docker_compose_up(compose_path: Path) -> None:
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "up", "-d"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ProvisioningError(f"docker compose up failed:\n{result.stderr}")


def docker_compose_down(compose_path: Path) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "down"],
        capture_output=True, text=True,
    )


def wait_for_pubkey(data_dir: Path, timeout_s: int = 30) -> str:
    """
    hbbs writes id_ed25519.pub into its data volume on first start.
    Poll for it rather than sleeping a fixed amount — container startup
    time isn't guaranteed, and a fixed sleep is exactly the kind of thing
    that works in testing and flakes in production under load.
    """
    pubkey_file = data_dir / "data" / "id_ed25519.pub"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if pubkey_file.exists():
            content = pubkey_file.read_text().strip()
            if content:
                return content
        time.sleep(1)
    raise ProvisioningError(
        f"Timed out waiting for {pubkey_file} after {timeout_s}s. "
        "Check `docker logs hbbs-<slug>` for startup errors."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_tenant(slug: str, display_name: str) -> dict:
    if not SLUG_RE.match(slug):
        raise ProvisioningError(
            f"Invalid slug '{slug}'. Use lowercase letters, digits, hyphens; 3-50 chars."
        )

    conn = get_db()
    ensure_schema(conn)

    if conn.execute("SELECT 1 FROM tenants WHERE slug = ?", (slug,)).fetchone():
        raise ProvisioningError(f"Tenant slug '{slug}' already exists.")

    port_index = next_port_index(conn)
    ports = derive_ports(port_index)
    data_dir = FLEET_ROOT / "tenants" / slug
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "data").mkdir(exist_ok=True)

    # Insert as 'provisioning' first so a crash mid-setup leaves a visible,
    # diagnosable row rather than silently consuming a port index.
    cur = conn.execute(
        """
        INSERT INTO tenants (slug, display_name, status, port_index, host,
                              hbbs_port, hbbr_port, data_dir, compose_path)
        VALUES (?, ?, 'provisioning', ?, ?, ?, ?, ?, ?)
        """,
        (slug, display_name, port_index, PUBLIC_HOST,
         ports.hbbs_port, ports.hbbr_port, str(data_dir), str(data_dir / "docker-compose.yml")),
    )
    tenant_id = cur.lastrowid
    conn.commit()
    log_event(conn, tenant_id, "tenant_created", f"port_index={port_index}")

    try:
        compose_path = render_compose(slug, ports, data_dir)
        log_event(conn, tenant_id, "compose_rendered", str(compose_path))

        docker_compose_up(compose_path)
        log_event(conn, tenant_id, "stack_up")

        pubkey = wait_for_pubkey(data_dir)
        log_event(conn, tenant_id, "key_captured")

        conn.execute(
            "UPDATE tenants SET status='active', pubkey=?, updated_at=datetime('now') WHERE id=?",
            (pubkey, tenant_id),
        )
        conn.commit()

    except Exception as e:
        conn.execute(
            "UPDATE tenants SET status='provisioning', updated_at=datetime('now') WHERE id=?",
            (tenant_id,),
        )
        conn.commit()
        log_event(conn, tenant_id, "provisioning_failed", str(e))
        raise

    return {
        "tenant_id": tenant_id,
        "slug": slug,
        "host": PUBLIC_HOST,
        "hbbs_port": ports.hbbs_port,
        "hbbr_port": ports.hbbr_port,
        "pubkey": pubkey,
        "data_dir": str(data_dir),
    }


def decommission_tenant(slug: str, *, delete_data: bool = False) -> None:
    conn = get_db()
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise ProvisioningError(f"No tenant with slug '{slug}'")

    docker_compose_down(Path(row["compose_path"]))
    log_event(conn, row["id"], "stack_down")

    if delete_data:
        shutil.rmtree(row["data_dir"], ignore_errors=True)
        log_event(conn, row["id"], "data_deleted")

    conn.execute(
        "UPDATE tenants SET status='decommissioned', updated_at=datetime('now') WHERE id=?",
        (row["id"],),
    )
    conn.commit()


def list_tenants() -> list[sqlite3.Row]:
    conn = get_db()
    ensure_schema(conn)
    return conn.execute(
        "SELECT slug, display_name, status, host, hbbs_port, hbbr_port, pubkey, created_at "
        "FROM tenants ORDER BY port_index"
    ).fetchall()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RustDesk fleet tenant provisioning")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Provision a new tenant's hbbs/hbbr stack")
    p_create.add_argument("--slug", required=True)
    p_create.add_argument("--display-name", required=True)

    sub.add_parser("list", help="List all tenants")

    p_destroy = sub.add_parser("destroy", help="Tear down a tenant's stack")
    p_destroy.add_argument("--slug", required=True)
    p_destroy.add_argument("--delete-data", action="store_true",
                            help="Also delete the tenant's data directory (irreversible)")

    args = parser.parse_args()

    if args.cmd == "create":
        try:
            result = create_tenant(args.slug, args.display_name)
        except ProvisioningError as e:
            sys.exit(f"Provisioning failed: {e}")
        print(f"Tenant '{result['slug']}' is active.")
        print(f"  host:       {result['host']}")
        print(f"  hbbs port:  {result['hbbs_port']}")
        print(f"  hbbr port:  {result['hbbr_port']}")
        print(f"  pubkey:     {result['pubkey']}")
        print(f"  data dir:   {result['data_dir']}")
        print()
        print("Open these ports in the Lightsail networking tab if not already open:")
        print(f"  TCP+UDP {result['hbbs_port']}-{result['hbbs_port']+3}, "
              f"TCP {result['hbbr_port']}, {result['hbbr_port']+2}")

    elif args.cmd == "list":
        rows = list_tenants()
        if not rows:
            print("No tenants yet.")
        for r in rows:
            print(f"{r['slug']:<24} {r['status']:<14} {r['host']}:{r['hbbs_port']}/{r['hbbr_port']}")

    elif args.cmd == "destroy":
        try:
            decommission_tenant(args.slug, delete_data=args.delete_data)
        except ProvisioningError as e:
            sys.exit(f"Decommission failed: {e}")
        print(f"Tenant '{args.slug}' decommissioned.")


if __name__ == "__main__":
    main()
