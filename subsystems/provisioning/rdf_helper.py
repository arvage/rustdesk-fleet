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

HOST must be <slug>.<PROVIDER_DOMAIN> for a valid, non-reserved slug, or the
console host admin.<PROVIDER_DOMAIN>. Later phases will add verified custom
domains. Arguments are validated strictly and never passed through a shell.
PROVIDER_DOMAIN and FLEET_DATA_DIR come from /etc/rustdesk-fleet/dashboard.env
(root-readable), never from the caller's environment.
"""
from __future__ import annotations

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
DASH_LOCATIONS = """    location = /login {
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
                 run=None, sleep=time.sleep, port_base: int = 21200, port_max: int = 21999):
        self.provider_domain = provider_domain
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
        return cls(domain, Path(values.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet"), port_base=base, port_max=top)

    def need_domain(self) -> str:
        """Tenant domains need PROVIDER_DOMAIN; tenant #1's relay doesn't."""
        if not DOMAIN_RE.match(self.provider_domain or ""):
            raise HelperError("PROVIDER_DOMAIN is missing or invalid in dashboard.env")
        return self.provider_domain


def _run(args: list[str], timeout: int = 300, stdin: str | None = None) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin)
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


def vhost_text(cfg: Config, host: str) -> str:
    live = cfg.le_live / host
    return f"""# Written by rdf-helper for {host}. Do not edit by hand.
server {{
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
    check_host(cfg, host)
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
    check_host(cfg, host)
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
    check_host(cfg, host)
    target = cfg.vhost_dir / f"{host}.conf"
    target.unlink(missing_ok=True)
    return f"removed {target}"


def revoke_cert(cfg: Config, host: str) -> str:
    check_host(cfg, host)
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


COMMANDS = {
    "issue-cert": (issue_cert, (1, 2)),
    "write-vhost": (write_vhost, (1, 1)),
    "remove-vhost": (remove_vhost, (1, 1)),
    "revoke-cert": (revoke_cert, (1, 1)),
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
