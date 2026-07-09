# Deployment

How this fleet is actually deployed and how to reproduce it on a new box.
Reflects the deployment pattern used in production, generalized here —
substitute your own host/domain wherever you see `rds.example.com`.

For architecture background (why single-tenant, what each subsystem does),
see [`README.md`](README.md). This doc is just the mechanics of standing
it up.

## Prerequisites

- Ubuntu 24.04 (Lightsail or equivalent). At least ~1GB RAM; add swap if
  the box is under ~1.5GB — Docker + a native installer build can spike
  memory:
  ```bash
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```
- Docker + Compose, Python 3.12, nsis (for installer builds), nginx +
  certbot (for the dashboard's TLS):
  ```bash
  sudo apt update && sudo apt install -y docker.io docker-compose-v2 \
      python3 python3-pip nsis nginx certbot python3-certbot-nginx
  sudo systemctl enable --now docker
  sudo usermod -aG docker $USER   # re-login (or `sg docker -c ...`) to take effect
  ```
- Python packages (installed system-wide, not a venv — Debian/Ubuntu's
  PEP 668 guard means you'll likely need `--break-system-packages`):
  ```bash
  pip3 install --break-system-packages \
      fastapi uvicorn jinja2 bcrypt python-multipart itsdangerous requests webauthn
  ```

## 1. Get the code

```bash
git clone git@github.com:arvage/rustdesk-fleet.git ~/rustdesk-fleet
```

## 2. Bring up the RustDesk relay (hbbs/hbbr)

```bash
cd ~/rustdesk-fleet/subsystems/single-tenant
python3 setup_server.py init --host rds.example.com
```

Idempotent — safe to re-run. Verify:

```bash
python3 setup_server.py status     # should show status: active, a pubkey
docker ps                          # hbbs and hbbr both Up
```

Image version is pinned in `docker-compose.yml`; the dashboard's status
page (see below) shows the running version and offers a one-click update
once logged in, or update manually:

```bash
cd /opt/rustdesk-fleet && docker compose pull && docker compose up -d
```

Create the client group(s) devices/installers will be labeled with:

```bash
python3 setup_server.py group create --slug acme-corp --display-name "Acme Corp"
```

## 3. Firewall

Two layers, both required:

- **Lightsail networking tab** (AWS console) — open TCP+UDP
  21115-21119 (RustDesk's default range; confirm actual bindings with
  `docker port hbbs` / `docker port hbbr` if you changed the defaults).
- **OS firewall**, only if `ufw` is active (`sudo ufw status`) — allow
  22/tcp first, then the same port range.

Test reachability from outside the box: `nc -zv rds.example.com 21115`.

## 4. Dashboard

```bash
sudo mkdir -p /etc/rustdesk-fleet
sudo tee /etc/rustdesk-fleet/dashboard.env >/dev/null <<'EOF'
SESSION_SECRET=<generate with: python3 -c "import secrets; print(secrets.token_hex(32))">
EOF
sudo chmod 600 /etc/rustdesk-fleet/dashboard.env
```

Install the systemd unit:

```bash
sudo tee /etc/systemd/system/rustdesk-dashboard.service >/dev/null <<'EOF'
[Unit]
Description=RustDesk Fleet Dashboard
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/rustdesk-fleet/subsystems/dashboard
ExecStart=/usr/local/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
EnvironmentFile=/etc/rustdesk-fleet/dashboard.env
Environment=PYTHONPATH=/home/ubuntu/rustdesk-fleet/subsystems/single-tenant
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
```

`PYTHONPATH` points at `subsystems/single-tenant` so the dashboard can
`import setup_server` / `generate_installer` directly — it delegates to
those modules rather than duplicating provisioning logic.

nginx (TLS termination, reverse proxy to uvicorn on 127.0.0.1:8000):

```bash
sudo certbot --nginx -d rds.example.com   # issues the cert, can also write the vhost
```

Resulting vhost (`/etc/nginx/sites-available/rds.example.com`):

```nginx
server {
    listen 80;
    server_name rds.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name rds.example.com;

    ssl_certificate     /etc/letsencrypt/live/rds.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/rds.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
sudo ln -s /etc/nginx/sites-available/rds.example.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 5. First-run setup

Visit `https://rds.example.com/` — with an empty `users` table every
route redirects to `/setup`, a one-time page to create the first admin
account.

**There is no default username/password.** `/setup` prompts for the
email and password to use for that first admin account — whatever you
enter there becomes the login. `/setup` locks permanently once a user
exists, so pick real credentials the first time; there's no factory
reset short of clearing the `users` table directly in
`fleet.sqlite3`.

## 6. Installer generation (for client devices)

Needs the actual RustDesk Windows client binaries staged locally:

```bash
mkdir -p /opt/rustdesk-fleet/installer-assets
# download rustdesk-<version>-x86_64.exe / -aarch64.exe from
# https://github.com/rustdesk/rustdesk/releases into that directory
```

Then build from the dashboard (`/groups/{slug}` → Build) or the CLI:

```bash
cd ~/rustdesk-fleet/subsystems/single-tenant
python3 generate_installer.py build --group acme-corp
```

## Verify everything

```bash
docker ps                                   # hbbs, hbbr Up
systemctl status rustdesk-dashboard          # active (running)
sudo nginx -t                                # config ok
curl -I https://rds.example.com/            # 303 to /login (expected, unauthenticated)
journalctl -u rustdesk-dashboard -n 50       # no tracebacks
```

## Where things live

| Path | What |
|---|---|
| `~/rustdesk-fleet` | Repo checkout — code only, no secrets/data |
| `/opt/rustdesk-fleet/fleet.sqlite3` | Dashboard + hbbs peer data (single-tenant schema) |
| `/opt/rustdesk-fleet/docker-compose.yml` | Deployed compose file (may drift from the repo template after an in-place server update — see Section 2) |
| `/opt/rustdesk-fleet/data` | hbbs/hbbr keypair + runtime state (container's `/root`) |
| `/opt/rustdesk-fleet/installer-assets` | Upstream RustDesk client binaries used as installer input |
| `/opt/rustdesk-fleet/installers` | Generated per-group installer output |
| `/etc/rustdesk-fleet/dashboard.env` | Dashboard secrets (`SESSION_SECRET`) — not in the repo |
| `/etc/systemd/system/rustdesk-dashboard.service` | Dashboard process supervisor |
| `/etc/nginx/sites-available/rds.example.com` | TLS + reverse proxy |

## Out of scope here

- `subsystems/provisioning/` — superseded per-tenant design, kept as
  reference only, not part of this deployment.
- Installer code-signing (`subsystems/signing/`) — not built yet;
  installers are currently distributed unsigned.
