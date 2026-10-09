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
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --email) LE_EMAIL="$2"; shift 2;;
    --tls) TLS_MODE="$2"; shift 2;;
    --client-port) CLIENT_PORT=1; shift;;
    --no-client-port) CLIENT_PORT=0; shift;;
    --with-installer-assets) INSTALLER_ASSETS=1; shift;;
    --yes|-y) ASSUME_YES=1; shift;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^#\s\?//'; exit 0;;
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
sudo -v || die "This user needs sudo privileges."

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
PKGS=(docker.io docker-compose-v2 python3 python3-pip nsis nginx gnupg)
[[ "$TLS_MODE" == "letsencrypt" ]] && PKGS+=(certbot python3-certbot-nginx)
[[ "$TLS_MODE" == "selfsigned" ]] && PKGS+=(openssl)
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PKGS[@]}"
sudo systemctl enable --now docker

log "Adding $RUN_USER to the docker group"
sudo usermod -aG docker "$RUN_USER"   # takes effect in new logins; we use `sg docker` below

# ── 3. Python dependencies (system-wide; PEP 668 override) ───────────────────
log "Installing Python dependencies"
sudo pip3 install --break-system-packages -q \
  fastapi uvicorn jinja2 bcrypt python-multipart itsdangerous requests webauthn boto3

# ── 4. Fleet data dir (owned by the dashboard user, NOT root) ────────────────
log "Preparing $FLEET_ROOT"
sudo mkdir -p "$FLEET_ROOT"
sudo chown "$RUN_USER:$RUN_USER" "$FLEET_ROOT"
sudo mkdir -p "$ETC_DIR"

# ── 5. Bring up the relay (hbbs/hbbr) ────────────────────────────────────────
log "Starting the hbbs/hbbr relay (this also generates the server keypair)"
# `sg docker` runs with the docker group active without needing a re-login.
sg docker -c "cd '$ST_DIR' && python3 setup_server.py init --host '$HOST'"
sg docker -c "cd '$ST_DIR' && python3 setup_server.py status" || true

# ── 6. Dashboard env (generate a session secret once; never overwrite) ───────
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
log "Configuring nginx"
VHOST="/etc/nginx/sites-available/rustdesk-fleet"

client_port_block() {
  [[ $CLIENT_PORT -eq 1 ]] || return 0
  cat <<'NGX'

# Legacy client reporting (older installers post to http://<host>:21114).
# New installers report over 443. Remove once no client uses 21114.
server {
    listen 21114;
    listen [::]:21114;
    server_name _;
    client_max_body_size 64k;
    access_log off;
    location ~ ^/api/(heartbeat|sysinfo|sysinfo_ver)$ {
        limit_except POST { deny all; }
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 15s;
    }
    location / { return 404; }
}
NGX
}

write_http_vhost() {   # used as the base; certbot rewrites it for LE
  sudo tee "$VHOST" >/dev/null <<EOF
server {
    listen 80;
    server_name $HOST;
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
$(client_port_block)
EOF
}

write_tls_vhost() {    # self-signed: full 80->443 + 443 vhost
  local crt="$1" key="$2"
  sudo tee "$VHOST" >/dev/null <<EOF
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
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
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

# ── 10. Firewall (only if ufw is active; cloud security groups are separate) ─
if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  log "Opening ports in ufw"
  sudo ufw allow 22/tcp >/dev/null 2>&1 || true
  for p in 21115 21116 21117 21118 21119; do sudo ufw allow "$p"/tcp >/dev/null 2>&1 || true; sudo ufw allow "$p"/udp >/dev/null 2>&1 || true; done
  sudo ufw allow 80/tcp >/dev/null 2>&1 || true
  sudo ufw allow 443/tcp >/dev/null 2>&1 || true
  [[ $CLIENT_PORT -eq 1 ]] && sudo ufw allow 21114/tcp >/dev/null 2>&1 || true
else
  warn "ufw not active — if this box is behind a cloud firewall/security group, open TCP+UDP 21115-21119, TCP 443 and 80$([[ $CLIENT_PORT -eq 1 ]] && echo ', and TCP 21114')."
fi

# ── 11. Installer assets (optional, large download) ──────────────────────────
if [[ $INSTALLER_ASSETS -eq 1 ]]; then
  log "Downloading RustDesk client binaries for installer builds"
  sg docker -c "cd '$ST_DIR' && python3 generate_installer.py update-version" \
    || warn "Could not fetch client binaries now — do it later from the dashboard (Server Status → Update installers) or: python3 $ST_DIR/generate_installer.py update-version"
else
  info "Skipping installer-asset download (run with --with-installer-assets to fetch now, or do it from the dashboard later)."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
SCHEME="https"; [[ "$TLS_MODE" == "none" ]] && SCHEME="http"
log "Done."
cat <<EOF

RustDesk Fleet is deployed.

  Dashboard:  $SCHEME://$HOST/
  First run:  open it and create the first admin account (the /setup page).

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
