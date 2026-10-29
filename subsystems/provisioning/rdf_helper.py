#!/usr/bin/python3 -I
"""rdf-helper: the only things the dashboard may do as root.

Installed by install.sh as /usr/local/sbin/rdf-helper (root:root, 0755) with a
sudoers entry that lets the dashboard's user run exactly this file. It is
self-contained (standard library only) and never imports code from the
repository, because the repository is writable by that user.

    rdf-helper issue-cert HOST [EMAIL]   Let's Encrypt certificate (HTTP-01, webroot)
    rdf-helper write-vhost HOST          nginx server block for HOST (needs its cert)
    rdf-helper remove-vhost HOST
    rdf-helper revoke-cert HOST          revoke and delete HOST's certificate
    rdf-helper release-domain DOMAIN     a custom domain's vhost and certificate, gone
    rdf-helper reload-nginx              nginx -t, reload, then check for [emerg]
    rdf-helper purge-relay-data ID       delete tenant ID's relay data (root-owned keypair, hbbs DB)
    rdf-helper relay-write-compose ID VERSION   (re)write tenant ID's compose file
    rdf-helper relay-up ID | relay-start ID | relay-stop ID | relay-restart ID | relay-down ID
    rdf-helper relay-status              {container name: status} of running containers, as JSON

Relays: the dashboard user has no Docker access. The helper renders every
compose file itself, from the template below and values it reads (read-only)
from the control database for that tenant id, each one validated: the relay
host (<slug>.relay.<PROVIDER_DOMAIN>, or tenant #1's recorded host), ports
inside RELAY_PORT_BASE..RELAY_PORT_MAX (tenant #1: 21116/21117), the data
directory (FLEET_DATA_DIR/tenants/<id>/relay/data, tenant #1: FLEET_DATA_DIR/data).
Compose files are written root-owned. Before every compose command the file is
read once, checked byte for byte against a fresh rendering, and that checked
text is what docker reads (on stdin), so a file the dashboard user swapped in
is refused and can't be raced in. The image is always
rustdesk/rustdesk-server:<VERSION> with VERSION like 1.1.16.

HOST must be <slug>.<PROVIDER_DOMAIN> for a valid, non-reserved slug, the
console host admin.<PROVIDER_DOMAIN>, or a tenant's custom domain. A custom
domain is accepted only when the control database (read-only) has it as kind
'custom' for a trial or active tenant, and for issue-cert and write-vhost
only when the helper's own DNS check passes (the rules below): the dashboard's
verdict is never trusted, because this is the last gate before a certificate
is issued for someone's domain. release-domain removes a custom domain's
vhost and certificate whatever its DNS or tenant status. Arguments are
validated strictly and never passed through a shell.
PROVIDER_DOMAIN and FLEET_DATA_DIR come from /etc/rustdesk-fleet/dashboard.env
(root-readable), never from the caller's environment.
"""
from __future__ import annotations

import ipaddress as _ip
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ENV_FILE = Path("/etc/rustdesk-fleet/dashboard.env")
VHOST_DIR = Path("/etc/nginx/rdf-tenants")
ACME_WEBROOT = Path("/var/www/certbot")
LE_LIVE = Path("/etc/letsencrypt/live")
NGINX_ERROR_LOG = Path("/var/log/nginx/error.log")

# Keep in sync with app/settings.py (a test checks they match).
RESERVED_SLUGS = frozenset({
    "admin", "relay", "www", "api", "mail", "status", "support", "billing", "app", "static",
    "console", "provider", "portal", "dashboard", "help", "docs", "cdn", "assets", "ws",
    "smtp", "imap", "pop", "ftp", "ns1", "ns2", "mx", "autodiscover", "autoconfig",
    "default", "localhost", "test", "dev", "staging", "invite", "login", "setup",
})
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
DOMAIN_RE = re.compile(r"^(?=.{4,200}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")

# The dashboard's nginx locations: the same as install.sh's dash_locations
# (a test checks they stay identical).
DASH_LOCATIONS = """    # Support sign-in links carry a one-time token in the query: log the path
    # only (log_format rdf_noquery, conf.d/rdf-log-format.conf).
    location = /support/enter {
        access_log /var/log/nginx/access.log rdf_noquery;
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  "";
        proxy_read_timeout 60s;
    }
    location = /login {
        limit_req        zone=rdf_signin burst=20 nodelay;
        limit_req_status 429;
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  "";
        proxy_read_timeout 60s;
    }
    # No trailing slash: a proxied prefix ending in / makes nginx 301
    # /login/webauthn -> /login/webauthn/, and the app redirects back (loop).
    location ^~ /login/webauthn {
        limit_req        zone=rdf_signin burst=20 nodelay;
        limit_req_status 429;
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  "";
        proxy_read_timeout 60s;
    }
    location = /setup {
        limit_req        zone=rdf_signin burst=20 nodelay;
        limit_req_status 429;
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  "";
        proxy_read_timeout 60s;
    }
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  "";
        proxy_read_timeout 60s;
    }
"""


class HelperError(Exception):
    pass


class Config:
    """Paths and the provider domain. Tests build one pointing at a temp dir."""

    def __init__(self, provider_domain: str, data_dir: Path, vhost_dir: Path = VHOST_DIR,
                 webroot: Path = ACME_WEBROOT, le_live: Path = LE_LIVE, error_log: Path = NGINX_ERROR_LOG,
                 run=None, sleep=time.sleep, port_base: int = 21200, port_max: int = 21999,
                 public_ipv4: str = "", public_ipv6: str = "", resolvers: tuple = (), dns=None):
        self.provider_domain = provider_domain
        self.public_ipv4, self.public_ipv6 = public_ipv4, public_ipv6
        self.resolvers = tuple(resolvers) or DEFAULT_RESOLVERS
        self.dns = dns          # query(resolver, name, type); None = dig through run
        self.data_dir = Path(data_dir)
        self.port_base = port_base
        self.port_max = port_max
        self.vhost_dir = Path(vhost_dir)
        self.webroot = Path(webroot)
        self.le_live = Path(le_live)
        self.error_log = Path(error_log)
        self.run = run or _run
        self.sleep = sleep

    @classmethod
    def from_env_file(cls, path: Path = ENV_FILE) -> "Config":
        values = {}
        try:
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
        except OSError as e:
            raise HelperError(f"can't read {path}: {e}")
        domain = values.get("PROVIDER_DOMAIN", "").lower().strip().rstrip(".")
        if domain and not DOMAIN_RE.match(domain):
            raise HelperError("PROVIDER_DOMAIN is invalid in dashboard.env")
        try:
            base = int(values.get("RELAY_PORT_BASE") or 21200)
            top = int(values.get("RELAY_PORT_MAX") or 21999)
        except ValueError:
            raise HelperError("RELAY_PORT_BASE / RELAY_PORT_MAX must be numbers")
        if not 1024 < base < top <= 65535:
            raise HelperError("RELAY_PORT_BASE / RELAY_PORT_MAX are out of range")
        ips = {}
        for key, version in (("PUBLIC_IPV4", 4), ("PUBLIC_IPV6", 6)):
            raw = values.get(key, "")
            if raw:
                try:
                    addr = _ip.ip_address(raw)
                except ValueError:
                    addr = None
                if addr is None or addr.version != version:
                    raise HelperError(f"{key} is not an IPv{version} address in dashboard.env")
                raw = str(addr)
            ips[key] = raw
        return cls(domain, Path(values.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet"), port_base=base, port_max=top,
                   public_ipv4=ips["PUBLIC_IPV4"], public_ipv6=ips["PUBLIC_IPV6"],
                   resolvers=parse_resolvers(values.get("DNS_CHECK_RESOLVERS", "")))

    def need_domain(self) -> str:
        """Tenant domains need PROVIDER_DOMAIN; tenant #1's relay doesn't."""
        if not DOMAIN_RE.match(self.provider_domain or ""):
            raise HelperError("PROVIDER_DOMAIN is missing or invalid in dashboard.env")
        return self.provider_domain


def _run(args: list[str], timeout: int = 300, stdin: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} timed out after {timeout}s"
    except OSError as e:
        return 127, f"can't run {args[0]}: {e}"
    return p.returncode, (p.stdout + p.stderr).strip()


def check_host(cfg: Config, host: str) -> str:
    """HOST if it's a tenant web domain or the console host, else HelperError."""
    if not isinstance(host, str) or host != host.strip().lower():
        raise HelperError(f"invalid host {host!r}")
    domain = cfg.need_domain()
    suffix = "." + domain
    if not host.endswith(suffix):
        raise HelperError(f"{host!r} is not under {domain}")
    label = host[: -len(suffix)]
    if label == "admin":
        return host
    if not SLUG_RE.match(label) or label in RESERVED_SLUGS:
        raise HelperError(f"{host!r} is not a tenant domain")
    return host


def _custom_row(cfg: Config, host: str):
    """(tenant id, tenant status, verify token, the tenant's own web host) for
    a custom domain in the control database, or HelperError."""
    import sqlite3
    db = cfg.data_dir / "fleet.sqlite3"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT d.tenant_id, d.kind, d.verify_token, t.status FROM tenant_domains d"
                " JOIN tenants t ON t.id = d.tenant_id WHERE d.hostname = ?", (host,)).fetchone()
            home = conn.execute(
                "SELECT hostname, kind FROM tenant_domains WHERE tenant_id = ? AND kind <> 'custom'"
                " ORDER BY kind = 'provider_subdomain' DESC, id LIMIT 1", (row[0],)).fetchone() if row else None
        finally:
            conn.close()
    except Exception as e:
        raise HelperError(f"can't read the control database: {e}")
    if row is None or row[1] != "custom":
        raise HelperError(f"{host!r} is not a tenant's custom domain")
    if home is None:
        raise HelperError(f"tenant {row[0]} has no web host of its own")
    home_host, home_kind = home
    if home_kind == "provider_subdomain":
        check_host(cfg, home_host)
    elif not DOMAIN_RE.match(home_host or ""):
        raise HelperError(f"tenant {row[0]}'s web host is invalid")
    return row[0], row[3], row[2] or "", home_host


def check_web_host(cfg: Config, host: str, dns: bool = False, any_status: bool = False) -> str:
    """HOST if it's a provider host (check_host) or a custom domain the helper
    has checked for itself: kind 'custom' for a trial or active tenant (any
    status with any_status), and with dns=True pointed at that tenant now."""
    if not isinstance(host, str) or host != host.strip().lower():
        raise HelperError(f"invalid host {host!r}")
    domain = cfg.need_domain()
    if host == domain or host.endswith("." + domain):
        return check_host(cfg, host)
    problem = custom_domain_problem(host, domain)
    if problem:
        raise HelperError(f"{host!r}: {problem}")
    tid, status, token, home = _custom_row(cfg, host)
    if not any_status and status not in ("trial", "active"):
        raise HelperError(f"{host} belongs to tenant {tid}, which is {status}")
    if dns:
        query = cfg.dns or (lambda r, n, t: dig_query(cfg.run, r, n, t))
        result = check_domain(query, host, home, domain, token, cfg.public_ipv4, cfg.public_ipv6, cfg.resolvers)
        if not result.ok:
            raise HelperError(f"DNS for {host} doesn't point at tenant {tid}: {result.reason}")
    return host


def is_custom(cfg: Config, host: str) -> bool:
    domain = cfg.need_domain()
    return not (host == domain or host.endswith("." + domain))


def vhost_text(cfg: Config, host: str) -> str:
    live = cfg.le_live / host
    # A custom domain has no wildcard port-80 block (rdf-provider.conf covers
    # *.<PROVIDER_DOMAIN> only): give it one, for HTTPS redirects and renewals.
    http = "" if not is_custom(cfg, host) else f"""server {{
    listen 80;
    listen [::]:80;
    server_name {host};
    location /.well-known/acme-challenge/ {{ root {cfg.webroot}; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
"""
    return f"""# Written by rdf-helper for {host}. Do not edit by hand.
{http}server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {host};
    ssl_certificate     {live}/fullchain.pem;
    ssl_certificate_key {live}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    client_max_body_size 8m;
{DASH_LOCATIONS}}}
"""


def issue_cert(cfg: Config, host: str, email: str = "") -> str:
    check_web_host(cfg, host, dns=True)
    if email and not EMAIL_RE.match(email):
        raise HelperError("invalid email")
    cfg.webroot.mkdir(parents=True, exist_ok=True)
    args = ["certbot", "certonly", "--webroot", "-w", str(cfg.webroot), "-d", host, "--cert-name", host,
            "--non-interactive", "--agree-tos", "--keep-until-expiring"]
    args += ["--email", email] if email else ["--register-unsafely-without-email"]
    code, out = cfg.run(args, 300)
    if code != 0:
        raise HelperError(f"certbot failed for {host}: {out[-800:]}")
    return f"certificate ready for {host}"


def write_vhost(cfg: Config, host: str) -> str:
    check_web_host(cfg, host, dns=True)
    live = cfg.le_live / host
    if not (live / "fullchain.pem").exists():
        raise HelperError(f"no certificate for {host}; run issue-cert first")
    cfg.vhost_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.vhost_dir / f"{host}.conf"
    tmp = target.with_suffix(".conf.tmp")
    tmp.write_text(vhost_text(cfg, host))
    os.chmod(tmp, 0o644)
    tmp.replace(target)
    return f"wrote {target}"


def remove_vhost(cfg: Config, host: str) -> str:
    check_web_host(cfg, host)
    target = cfg.vhost_dir / f"{host}.conf"
    target.unlink(missing_ok=True)
    return f"removed {target}"


def revoke_cert(cfg: Config, host: str) -> str:
    check_web_host(cfg, host)
    return _revoke(cfg, host)


def _revoke(cfg: Config, host: str) -> str:
    if not (cfg.le_live / host).exists():
        return f"no certificate for {host}"
    code, out = cfg.run(["certbot", "revoke", "--cert-name", host, "--delete-after-revoke",
                         "--reason", "cessationofoperation", "--non-interactive"], 300)
    if code != 0:
        code, out = cfg.run(["certbot", "delete", "--cert-name", host, "--non-interactive"], 120)
        if code != 0:
            raise HelperError(f"certbot couldn't revoke or delete {host}: {out[-800:]}")
        return f"deleted the certificate for {host} (revoking failed)"
    return f"revoked and deleted the certificate for {host}"


def release_domain(cfg: Config, host: str) -> str:
    """A custom domain's vhost and certificate, gone, and nginx reloaded if a
    vhost was removed: for removal, purge and domains whose DNS left us, so it
    works whatever the DNS says now and whatever the tenant's status. Only
    custom domains: never a provider host or tenant #1's."""
    domain = cfg.need_domain()
    if host == domain or host.endswith("." + domain):
        raise HelperError(f"{host} is not a custom domain")
    check_web_host(cfg, host, any_status=True)
    target = cfg.vhost_dir / f"{host}.conf"
    had_vhost = target.exists()
    target.unlink(missing_ok=True)
    done = [f"removed {target}" if had_vhost else "no vhost"]
    if had_vhost:
        done.append(reload_nginx(cfg))
    done.append(_revoke(cfg, host))
    return "; ".join(done)


def reload_nginx(cfg: Config) -> str:
    """nginx -t, reload, then fail if the error log gained an [emerg] line:
    nginx keeps the old configuration running when a reload is refused, and
    nginx -t can pass while the reload itself fails."""
    code, out = cfg.run(["nginx", "-t"], 60)
    if code != 0:
        raise HelperError(f"nginx -t failed; nothing was reloaded:\n{out[-1500:]}")
    try:
        offset = cfg.error_log.stat().st_size
    except OSError:
        offset = 0
    code, out = cfg.run(["systemctl", "reload", "nginx"], 60)
    if code != 0:
        raise HelperError(f"systemctl reload nginx failed:\n{out[-1500:]}")
    cfg.sleep(2)
    try:
        with cfg.error_log.open("rb") as f:
            f.seek(offset)
            new = f.read().decode("utf-8", "replace")
    except OSError:
        new = ""
    emerg = [line for line in new.splitlines() if "[emerg]" in line]
    if emerg:
        raise HelperError("nginx refused the new configuration (the old one is still running):\n" + "\n".join(emerg[-10:]))
    return "nginx reloaded"


def purge_relay_data(cfg: Config, tenant_id: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,8}", tenant_id or "") or int(tenant_id) == 1:
        raise HelperError("invalid tenant id (tenant #1's relay is never purged)")
    base = (cfg.data_dir / "tenants").resolve()
    target = (base / tenant_id / "relay").resolve()
    if target.parent.parent != base:
        raise HelperError("refusing a path outside the tenants directory")
    if target.exists():
        shutil.rmtree(target)
    return f"removed {target}"


# ── Relays (docker compose) ──────────────────────────────────────────────────

LEGACY_TENANT_ID = 1
LEGACY_PORTS = (21116, 21117)
LEGACY_PROJECT = "rustdesk-fleet"   # docker's default for /opt/rustdesk-fleet: keep it
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
IMAGE_TAG_RE = re.compile(r"rustdesk/rustdesk-server:([\w.\-]+)")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Tenant #1's file exactly as setup_server.py has always deployed it, so
# rendering it again changes nothing (tests compare it with a fixture).
LEGACY_COMPOSE = """# RustDesk fleet — single-tenant server.
#
# One hbbs/hbbr pair for the entire fleet. All clients (internal staff,
# all external client groups) register against this one server and share
# one Ed25519 keypair. Client/group separation is handled entirely at the
# application layer (dashboard, client_groups table) — NOT by this stack.
#
# Using network_mode: host since there's exactly one instance on this
# box — no port collision risk, and it's the configuration RustDesk's own
# docs recommend as simplest when you're not running multiple tenants.

services:
  hbbs:
    image: rustdesk/rustdesk-server:{version}
    container_name: hbbs
    command: hbbs
    network_mode: host
    volumes:
      - ./data:/root
    restart: unless-stopped
    depends_on:
      - hbbr

  hbbr:
    image: rustdesk/rustdesk-server:{version}
    container_name: hbbr
    command: hbbr
    network_mode: host
    volumes:
      - ./data:/root
    restart: unless-stopped
"""

TENANT_COMPOSE = """# Generated by the RustDesk Fleet provider console for tenant {tid} ({slug}).
# Do not edit by hand: rdf-helper renders it and refuses to run a changed copy.
# Ports: TENANCY.md, "Port layout".
#   {nat} NAT test · {hbbs} rendezvous (TCP+UDP) · {hbbr} relay
#   {ws_id} websocket rendezvous · {ws_relay} websocket relay
name: rdf-tenant-{tid}

services:
  hbbs:
    image: rustdesk/rustdesk-server:{version}
    container_name: rdf-t{tid}-hbbs
    command: hbbs -p {hbbs} -r {host}:{hbbr}
    network_mode: host
    volumes:
      - ./data:/root
    restart: unless-stopped
    depends_on:
      - hbbr

  hbbr:
    image: rustdesk/rustdesk-server:{version}
    container_name: rdf-t{tid}-hbbr
    command: hbbr -p {hbbr}
    network_mode: host
    volumes:
      - ./data:/root
    restart: unless-stopped
"""


class Relay:
    """A tenant's relay as the helper trusts it: validated values only."""

    def __init__(self, tid: int, project: str, project_dir: Path, render):
        self.tid, self.project, self.project_dir, self._render = tid, project, project_dir, render

    @property
    def compose_path(self) -> Path:
        return self.project_dir / "docker-compose.yml"

    def render(self, version: str) -> str:
        check_version(version)
        return self._render(version)


def check_version(version: str) -> str:
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise HelperError(f"invalid version {version!r} (expected e.g. 1.1.16)")
    return version


def _tenant_id(tid: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,8}", tid or ""):
        raise HelperError("invalid tenant id")
    return int(tid)


def _no_symlinks(path: Path) -> None:
    """The path and every parent are real directories (no symlink swapped in)."""
    if os.path.realpath(path) != str(path):
        raise HelperError(f"{path} is not a plain directory path")


def load_relay(cfg: Config, tid_arg: str) -> Relay:
    tid = _tenant_id(tid_arg)
    db = cfg.data_dir / "fleet.sqlite3"
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            t = r = None
            if "relays" in tables:
                t = conn.execute("SELECT slug, status FROM tenants WHERE id = ?", (tid,)).fetchone()
                r = conn.execute("SELECT host, hbbs_port, hbbr_port, data_dir FROM relays WHERE tenant_id = ?",
                                 (tid,)).fetchone()
            elif tid == LEGACY_TENANT_ID and "server_config" in tables:
                # A fresh install before the dashboard's first migration:
                # setup_server.py init records tenant #1's relay here.
                r = conn.execute("SELECT host, hbbs_port, hbbr_port, data_dir FROM server_config WHERE id = 1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        raise HelperError(f"can't read the control database: {e}")
    if r is None or (tid != LEGACY_TENANT_ID and t is None):
        raise HelperError(f"tenant {tid} has no relay")
    host, hbbs, hbbr, data_dir = r
    if not isinstance(host, str) or not HOSTNAME_RE.match(host) or not isinstance(hbbs, int) or not isinstance(hbbr, int):
        raise HelperError(f"tenant {tid}'s relay row is invalid")
    if tid == LEGACY_TENANT_ID:
        project_dir = cfg.data_dir
        if (hbbs, hbbr) != LEGACY_PORTS:
            raise HelperError("tenant #1's relay must be on 21116/21117")
        if data_dir != str(project_dir / "data"):
            raise HelperError("tenant #1's relay data directory isn't FLEET_DATA_DIR/data")
        render = lambda v: LEGACY_COMPOSE.format(version=v)
        project = LEGACY_PROJECT
    else:
        slug, status = t
        if not isinstance(slug, str) or not SLUG_RE.match(slug) or slug in RESERVED_SLUGS or status == "purged":
            raise HelperError(f"tenant {tid} has no usable slug")
        if host != f"{slug}.relay.{cfg.need_domain()}":
            raise HelperError(f"tenant {tid}'s relay host must be {slug}.relay.{cfg.provider_domain}")
        base = hbbs - 1
        if hbbr != hbbs + 1 or base < cfg.port_base or base + 4 > cfg.port_max:
            raise HelperError(f"tenant {tid}'s relay ports are outside {cfg.port_base}-{cfg.port_max}")
        project_dir = cfg.data_dir / "tenants" / str(tid) / "relay"
        if data_dir != str(project_dir / "data"):
            raise HelperError(f"tenant {tid}'s relay data directory must be {project_dir / 'data'}")
        project = f"rdf-tenant-{tid}"
        render = lambda v: TENANT_COMPOSE.format(tid=tid, slug=slug, host=host, version=v, nat=base, hbbs=hbbs,
                                                 hbbr=hbbr, ws_id=hbbs + 2, ws_relay=hbbr + 2)
    return Relay(tid, project, project_dir, render)


def _checked_compose(cfg: Config, relay: Relay) -> str:
    """The compose file's text, if it is exactly what the helper renders for
    this relay (at the version it names); else HelperError."""
    _no_symlinks(relay.project_dir)
    path = relay.compose_path
    try:
        st = os.lstat(path)
        text = path.read_text()
    except OSError:
        raise HelperError(f"tenant {relay.tid} has no compose file; run relay-write-compose first")
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        raise HelperError(f"{path} is not a regular file")
    m = IMAGE_TAG_RE.search(text)
    if not m or text != relay.render(m.group(1)):
        raise HelperError(f"{path} isn't the helper's rendering; refusing to run it "
                          f"(relay-write-compose {relay.tid} VERSION writes a fresh one)")
    data = relay.project_dir / "data"
    if data.exists():
        _no_symlinks(data)
    return text


def relay_write_compose(cfg: Config, tid: str, version: str) -> str:
    relay = load_relay(cfg, tid)
    text = relay.render(version)
    # The directories are the dashboard's (it restores backups into data/);
    # the compose file is root's.
    relay.project_dir.mkdir(parents=True, exist_ok=True)
    _no_symlinks(relay.project_dir)
    tmp = relay.project_dir / ".docker-compose.yml.rdf-helper"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    if os.geteuid() == 0:
        os.chown(tmp, 0, 0)
    os.replace(tmp, relay.compose_path)
    return f"wrote {relay.compose_path} (rustdesk-server {version})"


def _compose(cfg: Config, tid: str, *args: str, timeout: int = 300) -> str:
    relay = load_relay(cfg, tid)
    text = _checked_compose(cfg, relay)
    code, out = cfg.run(["docker", "compose", "-p", relay.project, "--project-directory", str(relay.project_dir),
                         "-f", "-", *args], timeout=timeout, stdin=text)
    if code != 0:
        raise HelperError(f"docker compose {' '.join(args)} failed: {out[-600:]}")
    return out or "ok"


def relay_up(cfg: Config, tid: str) -> str:
    return _compose(cfg, tid, "up", "-d", timeout=900)


def relay_start(cfg: Config, tid: str) -> str:
    return _compose(cfg, tid, "start")


def relay_stop(cfg: Config, tid: str) -> str:
    return _compose(cfg, tid, "stop")


def relay_restart(cfg: Config, tid: str) -> str:
    return _compose(cfg, tid, "restart")


def relay_down(cfg: Config, tid: str) -> str:
    if _tenant_id(tid) == LEGACY_TENANT_ID:
        raise HelperError("tenant #1's relay is never taken down")
    return _compose(cfg, tid, "down", "--remove-orphans")


def relay_status(cfg: Config) -> str:
    import json
    code, out = cfg.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"], timeout=10)
    if code != 0:
        raise HelperError(f"docker ps failed: {out[-300:]}")
    states = {}
    for line in out.splitlines():
        name, _, status = line.partition("\t")
        if CONTAINER_RE.match(name):
            states[name] = status.strip()[:100]
    return json.dumps(states, sort_keys=True)



# ── Custom domains: DNS checks ───────────────────────────────────────────────
# A tenant's own domain (tenant_domains kind 'custom') is "pointed at us" when
#   - its CNAME chain reaches that tenant's own <slug>.<PROVIDER_DOMAIN>, or
#   - its A/AAAA records are exactly this box's public addresses (PUBLIC_IPV4,
#     PUBLIC_IPV6) and the TXT record _rdf-verify.<domain> holds the tenant's
#     verification token for that domain,
# according to every resolver in DNS_CHECK_RESOLVERS (they must agree), and
# its CAA records (if any) let Let's Encrypt issue. The dashboard uses these
# same functions (it loads this file), so the two can't disagree; the helper
# runs them itself before issuing a certificate or writing a vhost.

DEFAULT_RESOLVERS = ("1.1.1.1", "8.8.8.8")
MAX_CUSTOM_DOMAIN_LEN = 200
VERIFY_LABEL = "_rdf-verify"
TOKEN_RE = re.compile(r"^rdf-verify-[A-Za-z0-9_-]{16,64}$")
# Names that only mean something on a local network.
LOCAL_SUFFIXES = ("localhost", "local", "localdomain", "internal", "invalid", "arpa", "lan", "home", "test")

# CDN edge networks: a domain whose A/AAAA records land here is proxied, and
# the proxy (not us) would answer. Cloudflare (cloudflare.com/ips), Fastly,
# Amazon CloudFront's most common ranges.
CDN_NETWORKS = tuple(_ip.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
    "151.101.0.0/16", "199.232.0.0/16", "2a04:4e40::/32",
    "13.32.0.0/15", "13.224.0.0/14", "18.64.0.0/14", "54.230.0.0/16", "54.239.128.0/18", "99.84.0.0/16",
    "205.251.192.0/19", "2600:9000::/28",
))


def custom_domain_problem(domain: str, provider_domain: str) -> str | None:
    """Why DOMAIN can't be a tenant's custom domain (format only; the
    dashboard also checks who else has it), or None."""
    if not isinstance(domain, str) or not domain or domain != domain.strip().lower():
        return "Enter the domain in lowercase, without spaces."
    if len(domain) > MAX_CUSTOM_DOMAIN_LEN:
        return f"That's longer than {MAX_CUSTOM_DOMAIN_LEN} characters."
    if "*" in domain:
        return "Wildcard domains can't be used; enter one hostname, like rds.abc.example."
    try:
        _ip.ip_address(domain)
        return "Enter a domain name, not an IP address."
    except ValueError:
        pass
    if not DOMAIN_RE.match(domain):
        return "That isn't a valid domain name. Enter a hostname like rds.abc.example."
    if domain.rsplit(".", 1)[-1] in LOCAL_SUFFIXES or domain.split(".", 1)[0] == "localhost":
        return "That name only works on a local network; use a public domain."
    pd = (provider_domain or "").lower()
    if pd and (domain == pd or domain.endswith("." + pd)):
        return f"Domains under {pd} are ours; use a domain you own."
    return None


def parse_resolvers(raw: str) -> tuple[str, ...]:
    """DNS_CHECK_RESOLVERS: comma-separated IP addresses (nothing else ever
    reaches dig's command line)."""
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(str(_ip.ip_address(part)))
            except ValueError:
                raise HelperError(f"DNS_CHECK_RESOLVERS: {part!r} is not an IP address")
    return tuple(out) or DEFAULT_RESOLVERS


def _clean(name: str) -> str:
    return name.strip().lower().rstrip(".")


def dig_query(run, resolver: str, name: str, rtype: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(status, [(owner, type, data)]) for one query at one resolver, through
    dig. status is the DNS rcode (NOERROR, NXDOMAIN, SERVFAIL…) or "TIMEOUT"."""
    _ip.ip_address(resolver)
    bare = name[len(VERIFY_LABEL) + 1:] if name.startswith(VERIFY_LABEL + ".") else name
    if rtype not in ("A", "AAAA", "CNAME", "TXT", "CAA") or not DOMAIN_RE.match(bare):
        raise HelperError(f"bad DNS query {name!r} {rtype}")
    code, out = run(["dig", f"@{resolver}", "+noall", "+comments", "+answer", "+time=3", "+tries=2",
                     name, rtype], 20)
    m = re.search(r"status: ([A-Z]+)", out)
    if not m:
        return "TIMEOUT", []
    records = []
    for line in out.splitlines():
        if not line.strip() or line.startswith(";"):
            continue
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[2] == "IN":
            records.append((_clean(parts[0]), parts[3], parts[4].strip()))
    return m.group(1), records


def _txt_strings(data: str) -> str:
    """'"abc" "def"' -> 'abcdef' (a TXT record split into strings)."""
    pieces = re.findall(r'"((?:[^"\\]|\\.)*)"', data)
    return "".join(pieces) if pieces else data.strip()


def _in_cdn(addresses) -> bool:
    for a in addresses:
        try:
            addr = _ip.ip_address(a)
        except ValueError:
            continue
        if any(addr in net for net in CDN_NETWORKS):
            return True
    return False


class DnsResult:
    """ok, the method that passed ('cname' or 'txt'), a machine code for a
    failure and a plain-words reason the tenant can act on."""

    def __init__(self, ok: bool, code: str = "", reason: str = "", method: str = ""):
        self.ok, self.code, self.reason, self.method = ok, code, reason, method

    def __repr__(self) -> str:
        return f"DnsResult(ok={self.ok}, code={self.code!r}, method={self.method!r})"


def _check_one(query, resolver: str, domain: str, target: str, provider_domain: str, token: str,
               ipv4: str, ipv6: str) -> DnsResult:
    status4, rec4 = query(resolver, domain, "A")
    status6, rec6 = query(resolver, domain, "AAAA")
    if status4 not in ("NOERROR", "NXDOMAIN") or status6 not in ("NOERROR", "NXDOMAIN"):
        bad = status4 if status4 not in ("NOERROR", "NXDOMAIN") else status6
        return DnsResult(False, "lookup_failed",
                         f"Looking up {domain} failed ({bad}). Check the domain at your DNS host; "
                         "we'll keep trying.")
    records = rec4 + rec6
    cnames = {owner: _clean(data) for owner, rtype, data in records if rtype == "CNAME"}
    chain, name = [], domain
    while name in cnames and len(chain) < 10:
        name = cnames[name]
        chain.append(name)
    addrs4 = sorted({d for o, t, d in rec4 if t == "A"})
    addrs6 = sorted({d.lower() for o, t, d in rec6 if t == "AAAA"})
    if chain:
        if target in chain:
            return DnsResult(True, method="cname")
        pd = provider_domain.lower()
        if any(n == pd or n.endswith("." + pd) for n in chain):
            return DnsResult(False, "other_account",
                             f"{domain} points at a different account. Change the CNAME's target to {target}.")
        if _in_cdn(addrs4 + addrs6):
            return DnsResult(False, "cdn", _CDN_REASON.format(domain=domain))
        return DnsResult(False, "cname_elsewhere",
                         f"{domain} is a CNAME for {chain[0]}, not {target}. Change the CNAME's target to {target}.")
    if not addrs4 and not addrs6:
        return DnsResult(False, "no_record",
                         f"We couldn't find a DNS record for {domain} yet. Create the CNAME record shown below; "
                         "if you just did, it can take a few minutes to show up.")
    if _in_cdn(addrs4 + addrs6):
        return DnsResult(False, "cdn", _CDN_REASON.format(domain=domain))
    if not ipv4:
        return DnsResult(False, "a_mismatch",
                         f"{domain} has A records ({', '.join(addrs4 + addrs6)}). Use a CNAME to {target} instead.")
    if addrs4 != [ipv4]:
        found = ", ".join(addrs4) or "none"
        return DnsResult(False, "a_mismatch",
                         f"{domain}'s A record ({found}) doesn't point at this server ({ipv4}). Use a CNAME to "
                         f"{target}, or set the A record to {ipv4} and add the TXT record.")
    ok6 = {ipv6.lower()} if ipv6 else set()
    if addrs6 and not set(addrs6) <= ok6:
        want = f"set it to {ipv6}" if ipv6 else "remove it"
        return DnsResult(False, "aaaa_mismatch",
                         f"{domain} has an AAAA (IPv6) record ({', '.join(addrs6)}) that doesn't point at this "
                         f"server: {want}.")
    txt_name = f"{VERIFY_LABEL}.{domain}"
    status, rec = query(resolver, txt_name, "TXT")
    if status not in ("NOERROR", "NXDOMAIN"):
        return DnsResult(False, "lookup_failed", f"Looking up {txt_name} failed ({status}); we'll keep trying.")
    values = [_txt_strings(d) for o, t, d in rec if t == "TXT" and o == txt_name]
    if not values:
        return DnsResult(False, "txt_missing",
                         f"The A record is right, but the TXT record {txt_name} is missing. Add it with the value "
                         "shown below.")
    if not token or token not in values:
        return DnsResult(False, "txt_wrong",
                         f"The TXT record {txt_name} doesn't hold this domain's verification code. Set it to the "
                         "value shown below.")
    return DnsResult(True, method="txt")


_CDN_REASON = ("{domain} is proxied through a CDN (Cloudflare or similar), so the CDN answers instead of us. "
               "Switch the record to “DNS only” (in Cloudflare: the grey cloud), then check again.")


def check_caa(query, resolver: str, domain: str) -> DnsResult:
    """RFC 8659: the CAA set that applies is the first one found climbing from
    the domain towards the top. It allows us if it has no `issue` property or
    one naming letsencrypt.org."""
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        status, rec = query(resolver, name, "CAA")
        if status not in ("NOERROR", "NXDOMAIN"):
            return DnsResult(False, "lookup_failed", f"Looking up the CAA records of {name} failed ({status}); "
                                                     "we'll keep trying.")
        caa = [d for o, t, d in rec if t == "CAA"]
        if not caa:
            continue
        issuers = []
        for data in caa:
            parts = data.split(None, 2)
            if len(parts) == 3 and parts[1].lower() == "issue":
                issuers.append(_txt_strings(parts[2]).split(";", 1)[0].strip().lower())
        if not issuers or "letsencrypt.org" in issuers:
            return DnsResult(True)
        return DnsResult(False, "caa",
                         f"The CAA records on {name} don't allow Let's Encrypt to issue certificates for {domain}. "
                         f'Add a CAA record on {name}: 0 issue "letsencrypt.org".')
    return DnsResult(True)


def check_domain(query, domain: str, target: str, provider_domain: str, token: str, ipv4: str = "",
                 ipv6: str = "", resolvers=DEFAULT_RESOLVERS) -> DnsResult:
    """Is DOMAIN pointed at us, for the tenant whose web host is TARGET, by
    every resolver? query(resolver, name, type) -> (status, records)."""
    results = [_check_one(query, r, domain, target, provider_domain, token, ipv4, ipv6) for r in resolvers]
    if all(r.ok for r in results):
        methods = {r.method for r in results}
        if len(methods) != 1:
            return DnsResult(False, "disagree", _DISAGREE.format(domain=domain, detail="they see different records"))
    else:
        codes = {r.code for r in results}
        if len(codes) == 1:
            return next(r for r in results if not r.ok)
        detail = "; ".join(f"{res}: {r.reason if not r.ok else 'points here'}" for res, r in zip(resolvers, results))
        return DnsResult(False, "disagree", _DISAGREE.format(domain=domain, detail=detail))
    for r in resolvers:
        caa = check_caa(query, r, domain)
        if not caa.ok:
            return caa
    return results[0]


_DISAGREE = ("DNS servers give different answers for {domain} ({detail}). DNS changes can take a while to "
             "spread; we'll keep checking.")

COMMANDS = {
    "issue-cert": (issue_cert, (1, 2)),
    "write-vhost": (write_vhost, (1, 1)),
    "remove-vhost": (remove_vhost, (1, 1)),
    "revoke-cert": (revoke_cert, (1, 1)),
    "release-domain": (release_domain, (1, 1)),
    "reload-nginx": (reload_nginx, (0, 0)),
    "purge-relay-data": (purge_relay_data, (1, 1)),
    "relay-write-compose": (relay_write_compose, (2, 2)),
    "relay-up": (relay_up, (1, 1)),
    "relay-start": (relay_start, (1, 1)),
    "relay-stop": (relay_stop, (1, 1)),
    "relay-restart": (relay_restart, (1, 1)),
    "relay-down": (relay_down, (1, 1)),
    "relay-status": (relay_status, (0, 0)),
}


def main(argv: list[str], cfg: Config | None = None) -> int:
    if not argv or argv[0] not in COMMANDS:
        print("usage: rdf-helper {" + ",".join(COMMANDS) + "} ARGS", file=sys.stderr)
        return 2
    fn, (lo, hi) = COMMANDS[argv[0]]
    args = argv[1:]
    if not lo <= len(args) <= hi:
        print(f"rdf-helper {argv[0]}: wrong number of arguments", file=sys.stderr)
        return 2
    try:
        cfg = cfg or Config.from_env_file()
        print(fn(cfg, *args))
        return 0
    except HelperError as e:
        print(f"rdf-helper {argv[0]}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
