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
                 run=None, sleep=time.sleep):
        self.provider_domain = provider_domain
        self.data_dir = Path(data_dir)
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
        if not DOMAIN_RE.match(domain):
            raise HelperError("PROVIDER_DOMAIN is missing or invalid in dashboard.env")
        return cls(domain, Path(values.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet"))


def _run(args: list[str], timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def check_host(cfg: Config, host: str) -> str:
    """HOST if it's a tenant web domain or the console host, else HelperError."""
    if not isinstance(host, str) or host != host.strip().lower():
        raise HelperError(f"invalid host {host!r}")
    suffix = "." + cfg.provider_domain
    if not host.endswith(suffix):
        raise HelperError(f"{host!r} is not under {cfg.provider_domain}")
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


COMMANDS = {
    "issue-cert": (issue_cert, (1, 2)),
    "write-vhost": (write_vhost, (1, 1)),
    "remove-vhost": (remove_vhost, (1, 1)),
    "revoke-cert": (revoke_cert, (1, 1)),
    "reload-nginx": (reload_nginx, (0, 0)),
    "purge-relay-data": (purge_relay_data, (1, 1)),
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
