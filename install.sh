#!/usr/bin/env bash
#
# install.sh — stand up the whole RustDesk Fleet system on a fresh Ubuntu box.
#
# Installs dependencies, brings up the hbbs/hbbr relay, and configures the
# management dashboard (systemd + nginx + TLS) and the nightly backup timer.
# Idempotent: safe to re-run to repair or reconfigure an install.
#
# Run it as a normal sudo-capable user (NOT root) from inside the repo:
#
#     git clone https://github.com/your-org/rustdesk-fleet.git
#     cd rustdesk-fleet
#     ./install.sh --host rds.example.com --email you@example.com
#
# Options (all optional; you'll be prompted for the host otherwise):
#   --host <domain-or-ip>   Public host clients connect to (and the dashboard URL)
#   --email <addr>          Email for Let's Encrypt (enables a real cert)
#   --tls <mode>            letsencrypt | selfsigned | none   (default: auto)
#                             auto = letsencrypt when --email + a real domain,
#                             else selfsigned. "none" is HTTP only and the
#                             dashboard login WILL NOT work (cookie is HTTPS-only).
#   --client-port           Open/serve the legacy client-reporting port 21114
#   --no-client-port        Don't open it (default)
#   --with-installer-assets Download the RustDesk client binaries now so you can
#                             build installers immediately (large; needs network)
#   --provider-domain <d>   Provider domain for the provider console and tenants
#                             (admin.<d>, <slug>.<d>, <slug>.relay.<d>); needs
#                             wildcard DNS for *.<d> and *.relay.<d>
#   --provider-only         Only set up the provider console pieces (certbot,
#                             rdf-helper + sudoers, nginx include dir, the
#                             console's certificate and vhost) on a box that is
#                             already installed; touches nothing else
#   --yes                   Non-interactive: accept defaults, no prompts
#
# Run with no flags for an interactive wizard (prompts for domain, TLS, DNS
# setup help for Let's Encrypt, etc.).
#
set -euo pipefail

# ── Config / args ─────────────────────────────────────────────────────────────
HOST="${RDF_HOST:-}"
LE_EMAIL="${RDF_LE_EMAIL:-}"
TLS_MODE="${RDF_TLS:-auto}"
CLIENT_PORT="${RDF_CLIENT_PORT:-}"       # unset -> ask (interactive) / default off
INSTALLER_ASSETS="${RDF_INSTALLER_ASSETS:-}"
PROVIDER_DOMAIN="${RDF_PROVIDER_DOMAIN:-}"
PROVIDER_ONLY=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --email) LE_EMAIL="$2"; shift 2;;
    --tls) TLS_MODE="$2"; shift 2;;
    --client-port) CLIENT_PORT=1; shift;;
    --no-client-port) CLIENT_PORT=0; shift;;
    --with-installer-assets) INSTALLER_ASSETS=1; shift;;
    --provider-domain) PROVIDER_DOMAIN="$2"; shift 2;;
    --provider-only) PROVIDER_ONLY=1; shift;;
    --yes|-y) ASSUME_YES=1; shift;;
    -h|--help) sed -n '2,37p' "$0" | sed 's/^#\s\?//'; exit 0;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Preflight ───────────────────────────────────────────────────────────────
[[ "$(id -u)" -eq 0 ]] && die "Run as a normal sudo user, not root (the dashboard runs as this user)."
command -v sudo >/dev/null || die "sudo is required."
sudo -n true 2>/dev/null || sudo -v || die "This user needs sudo privileges."

RUN_USER="$(id -un)"
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ST_DIR="$REPO_DIR/subsystems/single-tenant"
DASH_DIR="$REPO_DIR/subsystems/dashboard"
FLEET_ROOT="/opt/rustdesk-fleet"
ETC_DIR="/etc/rustdesk-fleet"

[[ -f "$ST_DIR/setup_server.py" ]] || die "Run this from the repo root (can't find subsystems/single-tenant/setup_server.py)."

if grep -qi ubuntu /etc/os-release 2>/dev/null; then :; else warn "This is tuned for Ubuntu; continuing anyway."; fi

is_ip() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; }
resolve_ip() { getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1{print $1}'; }
detect_public_ip() {
  curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null \
    || curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}'
}
ask() {  # ask "prompt" "default(y/n)" -> returns 0 for yes
  local prompt="$1" def="${2:-n}" ans
  local hint="[y/N]"; [[ "$def" == "y" ]] && hint="[Y/n]"
  read -rp "$prompt $hint " ans || true
  ans="${ans:-$def}"
  [[ "$ans" =~ ^[Yy]$ ]]
}

# ── Provider console pieces (also run alone with --provider-only) ───────────
# rdf-helper is the dashboard's only root access: relays (docker compose,
# with compose files it renders itself), certificates and nginx vhosts for
# tenant domains and the console. It is copied out of the repo (root never
# runs repo files) and the sudoers entry allows exactly it. The dashboard
# user is NOT in the docker group: that would be root by another name.
HELPER=/usr/local/sbin/rdf-helper
TENANT_VHOST_DIR=/etc/nginx/rdf-tenants

reload_nginx_checked() {   # nginx -t, reload, and no [emerg] after the reload
  sudo nginx -t
  local before; before=$(sudo stat -c %s /var/log/nginx/error.log 2>/dev/null || echo 0)
  sudo systemctl reload nginx
  sleep 2
  if sudo tail -c +$((before + 1)) /var/log/nginx/error.log 2>/dev/null | grep -q '\[emerg\]'; then
    sudo tail -c +$((before + 1)) /var/log/nginx/error.log | grep '\[emerg\]' >&2
    die "nginx refused the new configuration (it keeps running the old one). Fix the error above and re-run."
  fi
}

install_helper() {
  sudo install -m 0755 -o root -g root "$REPO_DIR/subsystems/provisioning/rdf_helper.py" "$HELPER"
  echo "$RUN_USER ALL=(root) NOPASSWD: $HELPER" | sudo tee /etc/sudoers.d/rdf-helper >/dev/null
  sudo chmod 0440 /etc/sudoers.d/rdf-helper
  sudo visudo -cf /etc/sudoers.d/rdf-helper >/dev/null || die "the rdf-helper sudoers entry is invalid"
  info "Installed $HELPER and its sudoers entry (for $RUN_USER)."
}

DOCKER_GROUP_DROPPED=0
drop_docker_group() {   # older installs added the dashboard user to it
  if id -nG "$RUN_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    sudo gpasswd -d "$RUN_USER" docker >/dev/null
    DOCKER_GROUP_DROPPED=1
    info "Removed $RUN_USER from the docker group (Docker now goes through $HELPER)."
  fi
}

# Re-render every relay's existing compose file through the helper, so it is
# root-owned and exactly the helper's template. Tenant #1's comes out
# byte-for-byte the same; nothing is restarted or recreated.
secure_relay_compose() {
  local rows
  rows=$(sudo python3 - "$FLEET_ROOT/fleet.sqlite3" <<'PY'
import re, sqlite3, sys
from pathlib import Path
db = Path(sys.argv[1])
if not db.exists():
    sys.exit()
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relays'").fetchone():
    sys.exit()
for tid, path in conn.execute("SELECT tenant_id, compose_path FROM relays ORDER BY tenant_id"):
    try:
        m = re.search(r"rustdesk/rustdesk-server:(\d+\.\d+\.\d+)\b", Path(path).read_text())
    except OSError:
        continue
    if m:
        print(tid, m.group(1))
PY
)
  local tid version
  while read -r tid version; do
    [[ -n "$tid" ]] || continue
    sudo "$HELPER" relay-write-compose "$tid" "$version" >/dev/null \
      || die "rdf-helper couldn't re-render tenant $tid's compose file (see the message above)."
  done <<< "$rows"
}

# Dashboard locations, shared by both vhosts. The sign-in endpoints get a
# per-IP limit on POSTs (zone and map defined at the top of the vhost file);
# GETs, the device API and everything else are not limited. The app applies
# its own per-email and per-IP lockouts on top of this. Every location drops
# a client-sent X-Fleet-Listener (an empty value isn't passed upstream): only
# the port-21114 block may set it.
dash_locations() {
  cat <<'NGX'
    # Support sign-in links carry a one-time token in the query: log the path
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
NGX
}

# http-level log format for /support/enter (dash_locations): the request
# line without its query string, which holds a one-time sign-in token.
write_log_format() {
  sudo tee /etc/nginx/conf.d/rdf-log-format.conf >/dev/null <<'NGX'
# RustDesk Fleet (install.sh): access-log format without the query string.
log_format rdf_noquery '$remote_addr - $remote_user [$time_local] "$request_method $uri $server_protocol" '
                       '$status $body_bytes_sent "$http_referer" "$http_user_agent"';
NGX
}

# Default servers: a request for a hostname nginx has no server block for
# gets nothing. Port 80 answers only ACME challenges (so a certificate can be
# issued for a custom domain before its vhost exists) and closes everything
# else (444); port 443 refuses the TLS handshake, so an unknown name never
# sees another site's certificate. Debian's stock "default" site claims
# default_server on port 80, so it goes; any other default_server is left
# alone and reported.
write_default_servers() {
  if [[ -L /etc/nginx/sites-enabled/default \
        && "$(readlink -f /etc/nginx/sites-enabled/default)" == /etc/nginx/sites-available/default ]]; then
    sudo rm -f /etc/nginx/sites-enabled/default
    info "Disabled nginx's stock default site (replaced by /etc/nginx/conf.d/rdf-default.conf)."
  fi
  if sudo grep -lE 'listen[^;]*default_server' /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf 2>/dev/null \
       | grep -vx /etc/nginx/conf.d/rdf-default.conf | grep -q .; then
    warn "Another nginx server claims default_server; not writing /etc/nginx/conf.d/rdf-default.conf. Remove that default_server and re-run."
    return 0
  fi
  sudo mkdir -p /var/www/certbot
  default_servers | sudo tee /etc/nginx/conf.d/rdf-default.conf >/dev/null
}

default_servers() {
  cat <<'NGX'
# RustDesk Fleet (install.sh): hostnames with no server block of their own.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 444; }
}
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
}
NGX
}

# Vhosts written before phase 1c lack the /support/enter location: add it to
# the dashboard's own vhost and re-render the helper's (tenants, console).
refresh_vhosts() {
  local f block
  block="$(dash_locations | sed -n '/# Support sign-in links/,/^    }$/p')"
  for f in /etc/nginx/sites-available/*; do
    sudo grep -q 'proxy_pass         http://127.0.0.1:8000' "$f" 2>/dev/null || continue
    sudo grep -q 'location = /support/enter' "$f" && continue
    printf '%s\n' "$block" | sudo python3 -c '
import sys
from pathlib import Path
p, block = Path(sys.argv[1]), sys.stdin.read()
text = p.read_text()
i = text.find("location = /login {")
if i >= 0:
    i = text.rfind("\n", 0, i) + 1
    p.write_text(text[:i] + block + text[i:])
' "$f"
    info "Added the /support/enter location to $f."
  done
  for f in "$TENANT_VHOST_DIR"/*.conf; do
    [[ -e "$f" ]] || continue
    sudo "$HELPER" write-vhost "$(basename "$f" .conf)" >/dev/null || warn "Couldn't re-render $f."
  done
}

# certbot renews every certificate (console, tenants' subdomains and custom
# domains) on its own timer; nginx only serves a renewed one after a reload.
write_renewal_hook() {
  local dir=/etc/letsencrypt/renewal-hooks/deploy
  sudo mkdir -p "$dir"
  if sudo grep -lsq 'reload nginx' "$dir"/* && ! sudo test -f "$dir/rdf-reload-nginx.sh"; then
    return 0      # a hook that reloads nginx is already there
  fi
  sudo tee "$dir/rdf-reload-nginx.sh" >/dev/null <<'SH'
#!/bin/sh
# RustDesk Fleet (install.sh): load renewed certificates.
nginx -t -q && systemctl reload nginx
SH
  sudo chmod 0755 "$dir/rdf-reload-nginx.sh"
}

provider_setup() {
  log "Setting up the provider console pieces"
  install_helper
  write_log_format
  write_default_servers
  write_renewal_hook
  drop_docker_group
  secure_relay_compose
  [[ $DOCKER_GROUP_DROPPED -eq 1 ]] && sudo systemctl restart rustdesk-dashboard.service

  if [[ -n "$PROVIDER_DOMAIN" ]]; then
    PROVIDER_DOMAIN="${PROVIDER_DOMAIN,,}"
    if sudo grep -q '^PROVIDER_DOMAIN=' "$ETC_DIR/dashboard.env"; then
      info "PROVIDER_DOMAIN is already set in $ETC_DIR/dashboard.env — leaving it."
      PROVIDER_DOMAIN="$(sudo sed -n 's/^PROVIDER_DOMAIN=//p' "$ETC_DIR/dashboard.env" | head -n1)"
    else
      echo "PROVIDER_DOMAIN=$PROVIDER_DOMAIN" | sudo tee -a "$ETC_DIR/dashboard.env" >/dev/null
      info "Added PROVIDER_DOMAIN=$PROVIDER_DOMAIN to $ETC_DIR/dashboard.env."
    fi
  else
    PROVIDER_DOMAIN="$(sudo sed -n 's/^PROVIDER_DOMAIN=//p' "$ETC_DIR/dashboard.env" 2>/dev/null | head -n1)"
  fi
  if [[ -z "$PROVIDER_DOMAIN" ]]; then
    warn "No PROVIDER_DOMAIN (use --provider-domain): skipping the console host and tenant domains."
    return 0
  fi

  # Port 80 for every tenant/console host: ACME challenges, else HTTPS.
  # Tenant and console vhosts (443) are written by rdf-helper into
  # $TENANT_VHOST_DIR. The sign-in rate-limit zone they use (rdf_signin) is
  # defined by the main vhost.
  sudo mkdir -p "$TENANT_VHOST_DIR" /var/www/certbot
  sudo tee /etc/nginx/conf.d/rdf-provider.conf >/dev/null <<EOF
# RustDesk Fleet provider domains (install.sh). Tenant and console vhosts:
# $TENANT_VHOST_DIR, written by rdf-helper.
server {
    listen 80;
    listen [::]:80;
    server_name *.$PROVIDER_DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
include $TENANT_VHOST_DIR/*.conf;
EOF
  refresh_vhosts
  reload_nginx_checked
  sudo systemctl restart rustdesk-dashboard.service
  local console="admin.$PROVIDER_DOMAIN"
  if sudo test -f "$TENANT_VHOST_DIR/$console.conf"; then
    info "The console vhost for $console already exists."
  elif sudo "$HELPER" issue-cert "$console" ${LE_EMAIL:+"$LE_EMAIL"} && sudo "$HELPER" write-vhost "$console" \
       && sudo "$HELPER" reload-nginx; then
    info "Provider console: https://$console/"
  else
    warn "Couldn't set up https://$console/ yet (DNS for *.$PROVIDER_DOMAIN must point here, port 80 open). Re-run with --provider-only."
  fi
  info "Create the first provider admin:  python3 $DASH_DIR/scripts/provider_admin.py --domain $PROVIDER_DOMAIN add you@example.com 'Your Name'"
}

if [[ $PROVIDER_ONLY -eq 1 ]]; then
  [[ -f "$ETC_DIR/dashboard.env" ]] || die "--provider-only needs an installed box ($ETC_DIR/dashboard.env is missing)."
  log "Installing certbot, reportlab and dnsutils"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot python3-reportlab dnsutils
  provider_setup
  log "Done (provider pieces only)."
  exit 0
fi

INTERACTIVE=0
if [[ $ASSUME_YES -eq 0 && -t 0 ]]; then INTERACTIVE=1; fi

PUBLIC_IP="$(detect_public_ip || true)"

# ── Interactive setup wizard ─────────────────────────────────────────────────
if [[ $INTERACTIVE -eq 1 ]]; then
  cat <<EOF

────────────────────────────────────────────────────────────────────────
 RustDesk Fleet — setup
────────────────────────────────────────────────────────────────────────
 This server's public IP looks like: ${PUBLIC_IP:-<unknown>}
EOF

  # 1) Host
  if [[ -z "$HOST" ]]; then
    echo
    echo "What address will RustDesk clients and admins use to reach this server?"
    echo "  • A DOMAIN (e.g. rds.example.com) — needed for a trusted Let's Encrypt cert."
    echo "  • An IP address — works, but only with a self-signed cert (browser warning)."
    read -rp "Host (domain or IP)${PUBLIC_IP:+ [$PUBLIC_IP]}: " HOST
    HOST="${HOST:-$PUBLIC_IP}"
  fi
  [[ -n "$HOST" ]] || die "A host is required."

  # 2) TLS choice
  if [[ "$TLS_MODE" == "auto" ]]; then
    if is_ip "$HOST"; then
      echo; info "\"$HOST\" is an IP address — Let's Encrypt needs a domain, so a self-signed certificate will be used."
      TLS_MODE="selfsigned"
    else
      echo
      if ask "Use a free Let's Encrypt certificate (trusted by browsers)?" y; then
        TLS_MODE="letsencrypt"
      else
        TLS_MODE="selfsigned"
      fi
    fi
  fi

  # 3) Let's Encrypt: DNS guidance + verification
  if [[ "$TLS_MODE" == "letsencrypt" ]]; then
    sub="${HOST%%.*}"
    cat <<EOF

 Let's Encrypt needs two things before it can issue a certificate:
   1. A DNS A record pointing your domain at THIS server, and
   2. Inbound TCP ports 80 and 443 open to the internet (cloud firewall too).

 Create this DNS record at your DNS provider (GoDaddy, Cloudflare, Route 53, …):

     Type:  A
     Name:  ${HOST}.        (often entered as just "${sub}" inside the ${HOST#*.} zone)
     Value: ${PUBLIC_IP:-<this server public IP>}
     TTL:   300 (5 min) while setting up

 DNS can take a few minutes (sometimes longer) to take effect.
EOF
    while true; do
      got="$(resolve_ip "$HOST" || true)"
      if [[ -n "$got" && -n "$PUBLIC_IP" && "$got" == "$PUBLIC_IP" ]]; then
        info "DNS OK: $HOST → $got"; break
      fi
      echo
      warn "$HOST currently resolves to: ${got:-<nothing>} (need ${PUBLIC_IP:-this server IP})."
      echo "   [Enter] re-check   ·   type 's' to use a self-signed cert instead   ·   'c' to continue anyway"
      read -rp "   > " choice || true
      case "$choice" in
        s|S) TLS_MODE="selfsigned"; info "Switching to a self-signed certificate."; break;;
        c|C) warn "Continuing — certbot will fail if DNS/ports aren't ready; you can re-run it later."; break;;
        *) : ;;  # loop and re-check
      esac
    done
  fi

  # 4) Let's Encrypt email
  if [[ "$TLS_MODE" == "letsencrypt" && -z "$LE_EMAIL" ]]; then
    echo
    read -rp "Email for Let's Encrypt (renewal/expiry notices): " LE_EMAIL
    [[ -n "$LE_EMAIL" ]] || { warn "No email given — using a self-signed certificate instead."; TLS_MODE="selfsigned"; }
  fi

  # 5) Legacy client port
  if [[ -z "$CLIENT_PORT" ]]; then
    echo
    echo "Installers built by this system report to the server over HTTPS (port 443)."
    echo "Only clients from OLD installers use plain port 21114."
    if ask "Open the legacy client-reporting port 21114?" n; then CLIENT_PORT=1; else CLIENT_PORT=0; fi
  fi

  # 6) Installer assets
  if [[ -z "$INSTALLER_ASSETS" ]]; then
    echo
    if ask "Download the RustDesk client binaries now (~100 MB) so you can build installers immediately?" n; then
      INSTALLER_ASSETS=1; else INSTALLER_ASSETS=0; fi
  fi
fi

# ── Non-interactive resolution of anything still unset ───────────────────────
[[ -n "$HOST" ]] || die "--host is required (non-interactive)."
if [[ "$TLS_MODE" == "auto" ]]; then
  if [[ -n "$LE_EMAIL" ]] && ! is_ip "$HOST"; then TLS_MODE="letsencrypt"; else TLS_MODE="selfsigned"; fi
fi
case "$TLS_MODE" in letsencrypt|selfsigned|none) ;; *) die "--tls must be letsencrypt, selfsigned or none.";; esac
[[ "$TLS_MODE" == "letsencrypt" && -z "$LE_EMAIL" ]] && die "--email is required for Let's Encrypt."
CLIENT_PORT="${CLIENT_PORT:-0}"
INSTALLER_ASSETS="${INSTALLER_ASSETS:-0}"

log "RustDesk Fleet installer"
info "repo:        $REPO_DIR"
info "run as user: $RUN_USER"
info "host:        $HOST"
info "TLS:         $TLS_MODE$([[ "$TLS_MODE" == letsencrypt ]] && echo " ($LE_EMAIL)")"
info "client port 21114: $([[ $CLIENT_PORT -eq 1 ]] && echo enabled || echo disabled)"
info "download client binaries: $([[ $INSTALLER_ASSETS -eq 1 ]] && echo yes || echo no)"
if [[ $INTERACTIVE -eq 1 ]]; then read -rp $'\nProceed with these settings? [y/N] ' a; [[ "$a" =~ ^[Yy]$ ]] || exit 1; fi

# ── 1. Swap (small boxes spike during Docker + installer builds) ─────────────
log "Checking swap"
if [[ "$(swapon --show --noheadings | wc -l)" -eq 0 ]]; then
  MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
  if [[ "$MEM_MB" -lt 1536 ]]; then
    if [[ ! -f /swapfile ]]; then
      info "Low RAM (${MEM_MB}MB) and no swap — creating a 2G swapfile."
      sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
      sudo chmod 600 /swapfile; sudo mkswap /swapfile
    fi
    sudo swapon /swapfile 2>/dev/null || true
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  else
    info "RAM ${MEM_MB}MB — no swap needed."
  fi
else
  info "Swap already active."
fi

# ── 2. System packages ───────────────────────────────────────────────────────
log "Installing system packages"
sudo apt-get update -qq
# certbot issues tenants' and the console's certificates (webroot),
# reportlab builds tenants' client PDF guides, and dig (dnsutils) checks
# tenants' custom domains.
PKGS=(docker.io docker-compose-v2 python3 python3-pip nsis nginx gnupg certbot python3-reportlab dnsutils)
[[ "$TLS_MODE" == "letsencrypt" ]] && PKGS+=(python3-certbot-nginx)
[[ "$TLS_MODE" == "selfsigned" ]] && PKGS+=(openssl)
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PKGS[@]}"
sudo systemctl enable --now docker

drop_docker_group

# ── 3. Python dependencies (system-wide; PEP 668 override) ───────────────────
log "Installing Python dependencies"
sudo pip3 install --break-system-packages -q \
  fastapi uvicorn jinja2 bcrypt python-multipart itsdangerous requests webauthn boto3

# ── 4. Fleet data dir (owned by the dashboard user, NOT root) ────────────────
log "Preparing $FLEET_ROOT"
sudo mkdir -p "$FLEET_ROOT"
sudo chown "$RUN_USER:$RUN_USER" "$FLEET_ROOT"
# Relay private keys, peer DBs and the fleet DB (secrets) live here, and the
# relay containers create their files world-readable: close the directory.
sudo chmod 0750 "$FLEET_ROOT"
sudo mkdir -p "$ETC_DIR"

# ── 5. Dashboard env (generate a session secret once; never overwrite) ───────
log "Configuring the dashboard"
if [[ ! -f "$ETC_DIR/dashboard.env" ]]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  sudo tee "$ETC_DIR/dashboard.env" >/dev/null <<EOF
SESSION_SECRET=$SECRET
EOF
  sudo chmod 600 "$ETC_DIR/dashboard.env"
  info "Wrote $ETC_DIR/dashboard.env with a fresh SESSION_SECRET."
else
  info "$ETC_DIR/dashboard.env already exists — leaving it."
fi

# ── 6. Bring up the relay (hbbs/hbbr) ────────────────────────────────────────
log "Starting the hbbs/hbbr relay (this also generates the server keypair)"
# setup_server.py runs as the dashboard user and asks rdf-helper to render
# tenant #1's compose file and run docker compose on it.
install_helper
(cd "$ST_DIR" && python3 setup_server.py init --host "$HOST")
(cd "$ST_DIR" && python3 setup_server.py status) || true

# A setup token locks /setup until the first admin exists. Add one only while
# there are no users yet and the env file has none; never touch other lines.
fleet_user_count() {
  sudo python3 - "$FLEET_ROOT/fleet.sqlite3" <<'PY'
import sqlite3, sys
from pathlib import Path
db = Path(sys.argv[1])
if not db.exists():
    print(0); sys.exit()
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
print(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] if has else 0)
PY
}
SETUP_TOKEN_ADDED=0
if ! sudo grep -q '^SETUP_TOKEN=' "$ETC_DIR/dashboard.env" && [[ "$(fleet_user_count)" == "0" ]]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  echo "SETUP_TOKEN=$TOKEN" | sudo tee -a "$ETC_DIR/dashboard.env" >/dev/null
  SETUP_TOKEN_ADDED=1
  info "Added a SETUP_TOKEN to $ETC_DIR/dashboard.env for first-run setup."
fi

# ── 7. Dashboard systemd service ─────────────────────────────────────────────
UVICORN="$(command -v uvicorn || echo /usr/local/bin/uvicorn)"
sudo tee /etc/systemd/system/rustdesk-dashboard.service >/dev/null <<EOF
[Unit]
Description=RustDesk Fleet Dashboard
After=network.target docker.service

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$DASH_DIR
ExecStart=$UVICORN app.main:app --host 127.0.0.1 --port 8000 --workers 1
EnvironmentFile=$ETC_DIR/dashboard.env
Environment=PYTHONPATH=$ST_DIR
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=rustdesk-dashboard

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now rustdesk-dashboard.service
# enable --now doesn't restart a running service; it must re-read the new token.
[[ $SETUP_TOKEN_ADDED -eq 1 ]] && sudo systemctl restart rustdesk-dashboard.service

# ── 8. Nightly backup timer (runs as root so it can read the keypair) ────────
log "Installing the nightly backup timer"
sudo tee /etc/systemd/system/rustdesk-backup.service >/dev/null <<EOF
[Unit]
Description=RustDesk Fleet backup (keypair + databases)
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
User=root
EnvironmentFile=-$ETC_DIR/backup.env
ExecStart=/usr/bin/python3 $ST_DIR/backup.py run
EOF
sudo tee /etc/systemd/system/rustdesk-backup.timer >/dev/null <<EOF
[Unit]
Description=Daily RustDesk Fleet backup

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now rustdesk-backup.timer

# ── 9. nginx + TLS ───────────────────────────────────────────────────────────
write_log_format   # dash_locations logs /support/enter with it
write_default_servers
log "Configuring nginx"
VHOST="/etc/nginx/sites-available/rustdesk-fleet"

client_port_block() {
  [[ $CLIENT_PORT -eq 1 ]] || return 0
  cat <<'NGX'

# Legacy client reporting (older installers post to http://<host>:21114).
# New installers report over 443. Remove once no client uses 21114.
# Devices here may send any Host (often an IP), so this listener tags its
# requests and the dashboard maps an unknown host on it to tenant #1. The
# value overrides any X-Fleet-Listener a client sends.
server {
    listen 21114;
    listen [::]:21114;
    server_name _;
    client_max_body_size 64k;
    access_log off;
    location ~ ^/api/(heartbeat|sysinfo|sysinfo_ver|audit/conn|audit/file)$ {
        limit_except POST { deny all; }
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  legacy-api;
        proxy_read_timeout 15s;
    }
    location / { return 404; }
}
NGX
}

# Only POSTs get a key, so only POSTs count: an empty key isn't limited.
# Sized for a whole office signing in from one NAT IP at shift start (one
# passkey sign-in is about 5 requests). The zone is "rdf_signin" because an
# older vhost used "rdf_auth" keyed on the IP, and nginx refuses a reload
# that changes an existing zone's key (it logs an [emerg] and keeps the old
# config, even though nginx -t passes).
AUTH_LIMIT_ZONE='map $request_method $rdf_auth_key {
    POST    $binary_remote_addr;
    default "";
}
limit_req_zone $rdf_auth_key zone=rdf_signin:10m rate=30r/m;'

write_http_vhost() {   # used as the base; certbot rewrites it for LE
  sudo tee "$VHOST" >/dev/null <<EOF
$AUTH_LIMIT_ZONE

server {
    listen 80;
    server_name $HOST;
$(dash_locations)
}
$(client_port_block)
EOF
}

write_tls_vhost() {    # self-signed: full 80->443 + 443 vhost
  local crt="$1" key="$2"
  sudo tee "$VHOST" >/dev/null <<EOF
$AUTH_LIMIT_ZONE

server {
    listen 80;
    server_name $HOST;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl;
    server_name $HOST;
    ssl_certificate     $crt;
    ssl_certificate_key $key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
$(dash_locations)
}
$(client_port_block)
EOF
}

case "$TLS_MODE" in
  letsencrypt)
    write_http_vhost
    sudo ln -sf "$VHOST" /etc/nginx/sites-enabled/rustdesk-fleet
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t && sudo systemctl reload nginx
    info "Requesting a Let's Encrypt certificate (port 80 must be reachable and DNS must point here)…"
    sudo certbot --nginx -d "$HOST" -m "$LE_EMAIL" --agree-tos --redirect -n \
      || warn "certbot failed — the site is up on HTTP; fix DNS/port 80 and re-run: sudo certbot --nginx -d $HOST"
    ;;
  selfsigned)
    CRT="$ETC_DIR/selfsigned.crt"; KEY="$ETC_DIR/selfsigned.key"
    if [[ ! -f "$CRT" || ! -f "$KEY" ]]; then
      info "Generating a self-signed certificate for $HOST."
      sudo openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout "$KEY" -out "$CRT" -subj "/CN=$HOST" >/dev/null 2>&1
      sudo chmod 600 "$KEY"
    fi
    write_tls_vhost "$CRT" "$KEY"
    sudo ln -sf "$VHOST" /etc/nginx/sites-enabled/rustdesk-fleet
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t && sudo systemctl reload nginx
    warn "Using a SELF-SIGNED cert — browsers will show a warning. For a trusted cert, point DNS here and re-run with --tls letsencrypt --email you@example.com."
    ;;
  none)
    write_http_vhost
    sudo ln -sf "$VHOST" /etc/nginx/sites-enabled/rustdesk-fleet
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t && sudo systemctl reload nginx
    warn "TLS disabled — the dashboard session cookie is HTTPS-only, so LOGIN WILL NOT WORK over plain HTTP. Use --tls selfsigned or letsencrypt."
    ;;
esac

provider_setup

# ── 10. Firewall (only if ufw is active; cloud security groups are separate) ─
if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  log "Opening ports in ufw"
  sudo ufw allow 22/tcp >/dev/null 2>&1 || true
  for p in 21115 21116 21117 21118 21119; do sudo ufw allow "$p"/tcp >/dev/null 2>&1 || true; sudo ufw allow "$p"/udp >/dev/null 2>&1 || true; done
  sudo ufw allow 80/tcp >/dev/null 2>&1 || true
  sudo ufw allow 443/tcp >/dev/null 2>&1 || true
  [[ $CLIENT_PORT -eq 1 ]] && sudo ufw allow 21114/tcp >/dev/null 2>&1 || true
  # Tenant relays (provider console): RELAY_PORT_BASE-RELAY_PORT_MAX.
  if [[ -n "$PROVIDER_DOMAIN" ]]; then
    sudo ufw allow 21200:21999/tcp >/dev/null 2>&1 || true
    sudo ufw allow 21200:21999/udp >/dev/null 2>&1 || true
  fi
else
  warn "ufw not active — if this box is behind a cloud firewall/security group, open TCP+UDP 21115-21119, TCP 443 and 80$([[ $CLIENT_PORT -eq 1 ]] && echo ', and TCP 21114')$([[ -n "$PROVIDER_DOMAIN" ]] && echo ', and TCP+UDP 21200-21999 for tenant relays')."
fi

# ── 11. Installer assets (optional, large download) ──────────────────────────
if [[ $INSTALLER_ASSETS -eq 1 ]]; then
  log "Downloading RustDesk client binaries for installer builds"
  (cd "$ST_DIR" && python3 generate_installer.py update-version) \
    || warn "Could not fetch client binaries now — do it later from the dashboard (Server Status → Update installers) or: python3 $ST_DIR/generate_installer.py update-version"
else
  info "Skipping installer-asset download (run with --with-installer-assets to fetch now, or do it from the dashboard later)."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
SCHEME="https"; [[ "$TLS_MODE" == "none" ]] && SCHEME="http"
if [[ "$(fleet_user_count)" == "0" ]]; then
  SETUP_TOKEN_VALUE="$(sudo sed -n 's/^SETUP_TOKEN=//p' "$ETC_DIR/dashboard.env" | head -n1)"
  FIRST_RUN="  First run:  create the first admin account at
              $SCHEME://$HOST/setup?token=$SETUP_TOKEN_VALUE"
else
  FIRST_RUN="  Sign in:    $SCHEME://$HOST/login"
  if sudo grep -q '^SETUP_TOKEN=' "$ETC_DIR/dashboard.env"; then
    FIRST_RUN="$FIRST_RUN
              (An admin exists, so SETUP_TOKEN in $ETC_DIR/dashboard.env is
              no longer used. You can delete that line.)"
  fi
fi
log "Done."
cat <<EOF

RustDesk Fleet is deployed.

  Dashboard:  $SCHEME://$HOST/
$FIRST_RUN

Services:
  sudo systemctl status rustdesk-dashboard
  docker ps                 # hbbs and hbbr should be Up
  systemctl list-timers rustdesk-backup.timer

Next steps:
  • Create a client group:  cd $ST_DIR && python3 setup_server.py group create --slug acme --display-name "Acme"
  • Configure off-site backups in the dashboard: Admin → Backup & Restore
  • Build installers from a group page (needs the client binaries — see --with-installer-assets)

Note: you were added to the 'docker' group. Log out/in before running docker
commands yourself without sudo.
EOF
