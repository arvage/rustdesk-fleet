"""Tenant relays (hbbs/hbbr stacks): port blocks, provisioning, start / stop /
restart / upgrade, health. Port layout and why: TENANCY.md, "Port layout".

Every docker call goes through rdf-helper (console.system.SYSTEM.relay, faked
in tests), which renders the compose files itself: the dashboard only
records the relay's host, ports and directories in the database.
Long operations run as console jobs (see the handlers at the bottom).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from app import settings
from app.console import jobs, system
from app.deps import get_db, provider_log
from app.tenancy import Tenant, invalidate_cache, load_tenant, tenant_files_dir

STRIDE = 10                 # ports per block (5 used, 5 spare)
DEFAULT_BLOCK_BASE = 21115  # tenant #1: RustDesk's default ports 21115-21119
IMAGE_TAG_RE = re.compile(r"rustdesk/rustdesk-server:([\w.\-]+)")
PUBKEY_WAIT_S = 60

RELAY_TABLES_SQL = """
    -- Port blocks handed to relays. Rows are never deleted, so a block is
    -- never handed out twice (old installers may still use its ports).
    CREATE TABLE IF NOT EXISTS relay_port_blocks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        base_port     INTEGER NOT NULL UNIQUE,
        tenant_id     INTEGER,
        slug          TEXT,
        note          TEXT NOT NULL DEFAULT '',
        allocated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""


class RelayError(Exception):
    pass


@dataclass(frozen=True)
class Block:
    base: int

    @property
    def nat(self) -> int:
        return self.base

    @property
    def hbbs(self) -> int:
        return self.base + 1

    @property
    def hbbr(self) -> int:
        return self.base + 2

    @property
    def ws_id(self) -> int:
        return self.base + 3

    @property
    def ws_relay(self) -> int:
        return self.base + 4


def ensure_tables(conn) -> None:
    conn.executescript(RELAY_TABLES_SQL)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(relays)")]
    if "server_version" not in cols:
        conn.execute("ALTER TABLE relays ADD COLUMN server_version TEXT")
    # relays.hbbs_port is hbbs's rendezvous port (-p). Tenant #1's row was
    # copied from server_config, which recorded 21115, the NAT-test port of a
    # default hbbs that really listens on 21116 (TENANCY.md, Port layout).
    conn.execute("UPDATE relays SET hbbs_port = 21116 WHERE tenant_id = 1 AND hbbs_port = 21115")
    if conn.execute("SELECT 1 FROM relays WHERE tenant_id = 1").fetchone() and not conn.execute(
            "SELECT 1 FROM relay_port_blocks WHERE base_port = ?", (DEFAULT_BLOCK_BASE,)).fetchone():
        conn.execute("INSERT INTO relay_port_blocks (base_port, tenant_id, slug, note) VALUES (?, 1, "
                     "(SELECT slug FROM tenants WHERE id = 1), 'RustDesk default ports (tenant #1)')",
                     (DEFAULT_BLOCK_BASE,))
    conn.commit()


def allocate_block(conn, tenant_id: int, slug: str) -> Block:
    """The next never-used block in RELAY_PORT_BASE..RELAY_PORT_MAX. Doesn't
    commit; the caller commits with the rest of the step."""
    base, top = settings.relay_port_base(), settings.relay_port_max()
    row = conn.execute("SELECT MAX(base_port) FROM relay_port_blocks WHERE base_port >= ?", (base,)).fetchone()
    nxt = base if row[0] is None else row[0] + STRIDE
    if nxt + 4 > top:
        raise RelayError(f"No relay ports left: every block in {base}–{top} has been used. "
                         "Raise RELAY_PORT_MAX (and open the new range in the firewall).")
    conn.execute("INSERT INTO relay_port_blocks (base_port, tenant_id, slug) VALUES (?, ?, ?)", (nxt, tenant_id, slug))
    return Block(nxt)


def free_blocks(conn) -> int:
    """How many more relay port blocks allocate_block() can hand out."""
    base, top = settings.relay_port_base(), settings.relay_port_max()
    row = conn.execute("SELECT MAX(base_port) FROM relay_port_blocks WHERE base_port >= ?", (base,)).fetchone()
    nxt = base if row[0] is None else row[0] + STRIDE
    return 0 if nxt + 4 > top else (top - 4 - nxt) // STRIDE + 1


def block_of(relay) -> Block:
    return Block(relay.hbbs_port - 1)


def relay_dir(tenant_id: int) -> Path:
    return tenant_files_dir(tenant_id) / "relay"


def current_server_version() -> str | None:
    """The rustdesk-server version new relays get: the provider setting,
    else whatever tenant #1's relay runs."""
    from app import provider_config
    v = provider_config.get("rustdesk_server_version")
    if v:
        return v
    t1 = load_tenant(1)
    if t1 and t1.relay:
        return compose_version(t1.relay.compose_path)
    return None


def compose_version(compose_path: str) -> str | None:
    try:
        m = IMAGE_TAG_RE.search(Path(compose_path).read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def _relay(tenant_id: int, action: str, *args: str, timeout: int = 300):
    r = system.SYSTEM.relay(action, tenant_id, *args, timeout=timeout)
    if not r.ok:
        raise RelayError(f"rdf-helper relay-{action} failed: {r.error()}")
    return r


def provision(conn, tenant_id: int, slug: str, block: Block, version: str) -> str:
    """Write the tenant's compose file, start the stack, wait for its public
    key and record the relay. Returns the public key."""
    relay_host = settings.tenant_relay_host(slug)
    d = relay_dir(tenant_id)
    (d / "data").mkdir(parents=True, exist_ok=True)
    compose = d / "docker-compose.yml"
    if not conn.execute("SELECT 1 FROM relays WHERE tenant_id = ?", (tenant_id,)).fetchone():
        conn.execute(
            """INSERT INTO relays (tenant_id, host, hbbs_port, hbbr_port, data_dir, compose_path, hbbs_db_path,
                                   status, server_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'provisioning', ?)""",
            (tenant_id, relay_host, block.hbbs, block.hbbr, str(d / "data"), str(compose),
             str(d / "data" / "db_v2.sqlite3"), version))
        conn.commit()
    _relay(tenant_id, "write-compose", version)
    _relay(tenant_id, "up", timeout=900)
    pub = d / "data" / "id_ed25519.pub"
    deadline = time.monotonic() + PUBKEY_WAIT_S
    while time.monotonic() < deadline:
        if pub.exists() and pub.read_text().strip():
            break
        time.sleep(1)
    else:
        raise RelayError(f"The relay started but wrote no public key within {PUBKEY_WAIT_S}s.")
    key = pub.read_text().strip()
    conn.execute("UPDATE relays SET pubkey = ?, status = 'active', updated_at = datetime('now') WHERE tenant_id = ?",
                 (key, tenant_id))
    conn.commit()
    invalidate_cache()
    return key


def remove(tenant_id: int) -> None:
    """Take a tenant's stack down for good (containers and network). Its data
    directory (root-owned keypair and database) is removed by the helper."""
    if (relay_dir(tenant_id) / "docker-compose.yml").exists():
        _relay(tenant_id, "down")


def _set_status(tenant_id: int, status: str, version: str | None = None) -> None:
    conn = get_db()
    if version:
        conn.execute("UPDATE relays SET status = ?, server_version = ?, updated_at = datetime('now') WHERE tenant_id = ?",
                     (status, version, tenant_id))
    else:
        conn.execute("UPDATE relays SET status = ?, updated_at = datetime('now') WHERE tenant_id = ?", (status, tenant_id))
    conn.commit()
    conn.close()
    invalidate_cache()


def start(tenant: Tenant) -> None:
    _relay(tenant.id, "up", timeout=900)
    _set_status(tenant.id, "active")


def stop(tenant: Tenant) -> None:
    """Stop the containers; keep them, the keypair and the data."""
    _relay(tenant.id, "stop")
    _set_status(tenant.id, "stopped")


def restart(tenant: Tenant) -> None:
    _relay(tenant.id, "restart")
    _set_status(tenant.id, "active")


def upgrade(tenant: Tenant, version: str) -> None:
    """Pin the stack to `version` (the helper rewrites the compose file) and
    recreate the containers on it (compose pulls the new image)."""
    if not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version or ""):
        raise RelayError(f"Not a rustdesk-server version: {version!r}.")
    _relay(tenant.id, "write-compose", version)
    _relay(tenant.id, "up", timeout=900)
    running = health(tenant)
    if not all(c["running"] for c in running.values()):
        raise RelayError(f"After the upgrade the containers aren't all running: {running}")
    _set_status(tenant.id, "active", version)


def health(tenant: Tenant, running: dict | None | bool = False) -> dict[str, dict]:
    """{'hbbs': {running, status, name}, 'hbbr': {...}} for one relay. Pass a
    running_containers() result to check many relays with one docker ps."""
    from setup_server import relay_containers
    import dataclasses
    names = relay_containers(dataclasses.asdict(tenant.relay)) if tenant.relay else []
    by_role = {("hbbr" if "hbbr" in n else "hbbs"): n for n in names}
    if running is False:
        running = system.SYSTEM.running_containers()
    out = {}
    for role in ("hbbs", "hbbr"):
        name = by_role.get(role, role)
        if running is None:
            out[role] = {"running": None, "status": "unknown", "name": name}
        elif name in running:
            out[role] = {"running": True, "status": running[name], "name": name}
        else:
            out[role] = {"running": False, "status": "not running", "name": name}
    return out


def running_version(tenant: Tenant) -> str | None:
    return (tenant.relay and compose_version(tenant.relay.compose_path)) or None


# ── Jobs ─────────────────────────────────────────────────────────────────────

def _tenant(job) -> Tenant:
    t = load_tenant(job.tenant_id)
    if t is None or t.relay is None:
        raise jobs.JobFailed("That tenant or its relay no longer exists.")
    return t


@jobs.handler("relay_action")
def _relay_action(job) -> None:
    action = job.params["action"]
    t = _tenant(job)
    with job.step(f"{action.capitalize()} {t.slug}'s relay"):
        try:
            if action == "start":
                start(t)
            elif action == "stop":
                stop(t)
            elif action == "restart":
                restart(t)
            elif action == "upgrade":
                upgrade(t, job.params["version"])
            else:
                raise jobs.JobFailed(f"Unknown relay action {action!r}.")
        except RelayError as e:
            raise jobs.JobFailed(str(e))
    conn = get_db()
    provider_log(conn, f"relay_{action}", f"{t.slug} {job.params.get('version', '')}".strip(), job.created_by, t.id)
    conn.close()


@jobs.handler("relay_upgrade_all")
def _relay_upgrade_all(job) -> None:
    """Upgrade every relay to the version, one at a time; stop at the first failure."""
    from app import provider_config
    from app.tenancy import active_tenants
    version = job.params["version"]
    for t in active_tenants():
        if not t.relay:
            continue
        if running_version(t) == version:
            job.note(f"{t.slug}: already on {version}", "done")
            continue
        with job.step(f"Upgrade {t.slug} to {version}"):
            try:
                if t.relay.status == "stopped":
                    # A stopped (cancelled) relay gets the new tag and stays stopped.
                    _relay(t.id, "write-compose", version)
                    _set_status(t.id, "stopped", version)
                else:
                    upgrade(t, version)
            except RelayError as e:
                raise jobs.JobFailed(f"{t.slug}: {e} (later relays were not touched)")
        conn = get_db()
        provider_log(conn, "relay_upgrade", f"{t.slug} {version}", job.created_by, t.id)
        conn.close()
    provider_config.set_many({"rustdesk_server_version": version})
