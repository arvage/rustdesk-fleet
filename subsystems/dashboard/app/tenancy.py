"""Which tenant a request (or a background job) is working for. See TENANCY.md.

Every request is matched to a tenant by its Host header, looked up in
tenant_domains (TenantMiddleware). Routes take the tenant explicitly:

    def page(tenant: Tenant = Depends(get_tenant), ...):
        conn.execute("SELECT ... WHERE tenant_id = ?", (tenant.id, ...))

There is no global "current tenant". Background jobs loop over
active_tenants() and pass each one down.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request

from app.tenant_migration import LEGACY_TENANT_ID
from app.tenant_schema import normalize_host

# Tenant statuses whose dashboard, sign-in and download links work.
OPEN_STATUSES = ("trial", "active")

# Device-reporting endpoints. They answer for every tenant status, and for an
# unknown host they still answer (rejecting the report) instead of 404ing,
# because the RustDesk client can't do anything useful with a 404.
DEVICE_API_PATHS = frozenset({
    "/api/heartbeat", "/api/sysinfo", "/api/sysinfo_ver", "/api/audit/conn", "/api/audit/file",
})

# Set by nginx on the legacy port-21114 vhost only (and stripped from client
# requests on every vhost). Devices reaching that listener may send any Host,
# often an IP address, so an unknown host there means tenant #1.
LISTENER_HEADER = "x-fleet-listener"
LEGACY_LISTENER = "legacy-api"


@dataclass(frozen=True)
class Relay:
    id: int
    tenant_id: int
    host: str
    hbbs_port: int
    hbbr_port: int
    pubkey: str | None
    data_dir: str
    compose_path: str
    hbbs_db_path: str
    status: str

    @property
    def ports(self) -> frozenset[int]:
        """TCP ports hbbs and hbbr listen on (TENANCY.md, Port layout): for
        hbbs_port P, the NAT test P-1, rendezvous P and websocket P+2; for
        hbbr_port R, the relay R and its websocket R+2. For the defaults
        (21116, 21117) that's 21115-21119."""
        return frozenset({self.hbbs_port - 1, self.hbbs_port, self.hbbs_port + 2,
                          self.hbbr_port, self.hbbr_port + 2})


@dataclass(frozen=True)
class Tenant:
    id: int
    slug: str
    display_name: str
    status: str
    relay: Relay | None
    hostname: str = ""      # the domain this request came in on ("" for jobs)
    trial_ends_at: str | None = None   # UTC, while on trial (or suspended at its end)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def files_dir(self) -> Path:
        return tenant_files_dir(self.id)

    @property
    def hbbs_db(self) -> Path | None:
        return Path(self.relay.hbbs_db_path) if self.relay else None


def tenant_files_dir(tenant_id: int) -> Path:
    """Where a tenant's installers, group icons and email logo live. Tenant #1
    keeps the data directory itself, where those files always were. Shared
    with the scripts in subsystems/single-tenant (tenant_paths.py)."""
    from tenant_paths import tenant_files_dir as _dir
    return _dir(tenant_id)


# ── Lookups ──────────────────────────────────────────────────────────────────

def _relay_from_row(r) -> Relay:
    return Relay(id=r["id"], tenant_id=r["tenant_id"], host=r["host"], hbbs_port=r["hbbs_port"],
                 hbbr_port=r["hbbr_port"], pubkey=r["pubkey"], data_dir=r["data_dir"],
                 compose_path=r["compose_path"], hbbs_db_path=r["hbbs_db_path"], status=r["status"])


def _load(conn, tenant_id: int, hostname: str = "") -> Tenant | None:
    t = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if t is None:
        return None
    r = conn.execute("SELECT * FROM relays WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return Tenant(id=t["id"], slug=t["slug"], display_name=t["display_name"], status=t["status"],
                  relay=_relay_from_row(r) if r else None, hostname=hostname,
                  trial_ends_at=t["trial_ends_at"] if "trial_ends_at" in t.keys() else None)


def load_tenant(tenant_id: int, conn=None) -> Tenant | None:
    from app.deps import get_db
    own = conn is None
    conn = conn or get_db()
    try:
        return _load(conn, tenant_id)
    finally:
        if own:
            conn.close()


def active_tenants() -> list[Tenant]:
    """Tenants background jobs should work for: everything but cancelled.
    Suspended tenants' devices still report in, so their status and session
    bookkeeping keeps going; notifications check is_open separately."""
    from app.deps import get_db
    conn = get_db()
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM tenants WHERE status <> 'cancelled' ORDER BY id")]
        return [t for t in (_load(conn, i) for i in ids) if t is not None]
    finally:
        conn.close()


# Host -> tenant lookups are cached briefly: domains change rarely, and this
# runs on every request (devices heartbeat every few seconds).
_CACHE_TTL_S = 30.0
_cache: dict[str, tuple[float, Tenant | None]] = {}
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


def resolve_host(raw_host: str) -> Tenant | None:
    hostname = normalize_host(raw_host)
    if not hostname:
        return None
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(hostname)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    from app.deps import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT tenant_id FROM tenant_domains WHERE hostname = ?", (hostname,)).fetchone()
        tenant = _load(conn, row["tenant_id"], hostname) if row else None
    finally:
        conn.close()
    with _cache_lock:
        if len(_cache) > 1000:
            _cache.clear()
        _cache[hostname] = (now, tenant)
    return tenant


def legacy_tenant() -> Tenant | None:
    """Tenant #1, for the legacy port-21114 listener."""
    now = time.monotonic()
    key = "\0legacy"
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    tenant = load_tenant(LEGACY_TENANT_ID)
    with _cache_lock:
        _cache[key] = (now, tenant)
    return tenant


def resolve_relay_host(raw_host: str) -> Tenant | None:
    """The tenant whose relay host is this (device API only). Installers point
    devices at the relay host, never at a web domain (TENANCY.md), so a
    device whose api-server is its relay host lands on the right tenant even
    when that name is not one of the tenant's web domains."""
    hostname = normalize_host(raw_host)
    if not hostname:
        return None
    key = "\0relay:" + hostname
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    from app.deps import get_db
    conn = get_db()
    try:
        match = next((r["tenant_id"] for r in conn.execute("SELECT tenant_id, host FROM relays")
                      if normalize_host(r["host"]) == hostname), None)
        tenant = _load(conn, match, hostname) if match is not None else None
    finally:
        conn.close()
    with _cache_lock:
        _cache[key] = (now, tenant)
    return tenant


class _Console:
    """tenant_for_request()'s answer for the provider console host: not a
    tenant. Requests there go to the console app (app/console), never to a
    tenant route."""

    def __repr__(self) -> str:
        return "CONSOLE"


CONSOLE = _Console()


def tenant_for_request(host: str, path: str, listener: str) -> "Tenant | _Console | None":
    """Host -> tenant, or CONSOLE for admin.<PROVIDER_DOMAIN>. Device API
    requests may also match a relay host, and on the legacy port-21114
    listener an unknown host means tenant #1. Nothing else ever falls back
    to tenant #1."""
    from app import settings
    console = settings.console_host()
    if console and normalize_host(host) == console:
        return CONSOLE
    tenant = resolve_host(host)
    if tenant is None and path in DEVICE_API_PATHS:
        tenant = resolve_relay_host(host)
        if tenant is None and listener == LEGACY_LISTENER:
            tenant = legacy_tenant()
    return tenant


# ── Request plumbing ─────────────────────────────────────────────────────────

class UnknownTenant(Exception):
    """No tenant for this request (only reachable on the device API)."""


def get_tenant(request: Request) -> Tenant:
    """FastAPI dependency: the tenant TenantMiddleware matched to this request.
    Outside the device API the middleware has already answered 404 for an
    unknown host and the inactive page for a closed tenant."""
    tenant = request.scope.get("state", {}).get("tenant")
    if tenant is None:
        raise UnknownTenant()
    return tenant


def request_tenant(request: Request) -> Tenant | None:
    """The request's tenant, or None (device API with an unknown host)."""
    return request.scope.get("state", {}).get("tenant")


_NOT_FOUND_HTML = b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Not found</title>
<style>body{font-family:system-ui,sans-serif;background:#f8fafc;color:#0f172a;margin:0;
display:grid;place-items:center;min-height:100vh}main{max-width:28rem;padding:2rem;text-align:center}
p{color:#64748b}</style></head><body><main><h1>Not found</h1>
<p>There is nothing at this address.</p></main></body></html>"""

_INACTIVE_HTML = b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Account not active</title>
<meta name="robots" content="noindex"><style>body{font-family:system-ui,sans-serif;background:#f8fafc;
color:#0f172a;margin:0;display:grid;place-items:center;min-height:100vh}main{max-width:28rem;
padding:2rem;text-align:center}p{color:#64748b}</style></head><body><main>
<h1>This account is not active</h1><p>Sign-in and downloads for this account are turned off.
If you think this is a mistake, contact your IT provider.</p></main></body></html>"""


_FORBIDDEN_HTML = b"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Forbidden</title>
<meta name="robots" content="noindex"></head><body style="font-family:system-ui,sans-serif;text-align:center;
padding:4rem"><h1>Not allowed from here</h1></body></html>"""


class TenantMiddleware:
    """Pure ASGI middleware: resolve the tenant from Host for every HTTP
    request and store it in scope["state"]["tenant"].

    - unknown host -> 404 page (device API: passes through with no tenant,
      and the handler rejects the report as it would an unknown device);
    - suspended / cancelled tenant -> "not active" page, except the device
      API, which keeps accepting reports.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        path = scope.get("path", "")
        tenant = tenant_for_request(headers.get("host", ""), path, headers.get(LISTENER_HEADER, ""))
        if tenant is CONSOLE:
            # The provider console is a separate app with its own routes and
            # session cookie: no tenant route is reachable on its host.
            from app import settings
            client = scope.get("client") or ("", 0)
            if not settings.ip_allowed(headers.get("x-real-ip") or client[0]):
                return await _plain(send, 403, _FORBIDDEN_HTML)
            from app.console.main import console_app
            return await console_app(scope, receive, send)
        is_device_api = path in DEVICE_API_PATHS
        if tenant is None and not is_device_api:
            return await _plain(send, 404, _NOT_FOUND_HTML)
        if tenant is not None and not tenant.is_open and not is_device_api:
            # A cancelled tenant's admins may still sign in and export their
            # data until the purge; everything else is closed.
            if tenant.status == "cancelled" and _export_allowed(path):
                if path == "/":
                    return await _redirect(send, "/export")
            else:
                return await _plain(send, 403, _INACTIVE_HTML)
        scope.setdefault("state", {})["tenant"] = tenant
        return await self.app(scope, receive, send)


def _export_allowed(path: str) -> bool:
    """Paths a cancelled tenant can still reach: sign-in and the export page."""
    return (path in ("/", "/login", "/logout", "/export") or path.startswith(("/login/webauthn", "/static/", "/export/")))


async def _redirect(send, location: str) -> None:
    await send({"type": "http.response.start", "status": 303,
                "headers": [(b"location", location.encode()), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


async def _plain(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"x-robots-tag", b"noindex"),
    ]})
    await send({"type": "http.response.body", "body": body})
