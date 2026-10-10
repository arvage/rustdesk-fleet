# Deployment

How this fleet is actually deployed and how to reproduce it on a new box.
Reflects the deployment pattern used in production, generalized here —
substitute your own host/domain wherever you see `rds.example.com`.

For architecture background (why single-tenant, what each subsystem does),
see [`README.md`](README.md). This doc is just the mechanics of standing
it up.

## Quick install (one script)

On a fresh Ubuntu box, [`install.sh`](install.sh) does everything below —
packages, swap, the hbbs/hbbr relay, the dashboard (systemd + nginx + TLS)
and the nightly backup timer. Run it as a normal sudo user (not root) from
the repo, and it's idempotent (safe to re-run):

```bash
git clone https://github.com/your-org/rustdesk-fleet.git
cd rustdesk-fleet
./install.sh --host rds.example.com --email you@example.com
```

- With `--email` and a real domain it obtains a **Let's Encrypt** cert; with
  just an IP or no email it falls back to a **self-signed** cert (the
  dashboard's session cookie is HTTPS-only, so some TLS is required).
- Add `--with-installer-assets` to download the RustDesk client binaries so
  you can build installers immediately; otherwise do it later from the
  dashboard. `--help` lists all options.
- Still open the relay ports in your **cloud firewall / security group**
  (TCP+UDP 21115-21119, TCP 443/80, and 21114 if keeping legacy reporting) —
  the script only touches `ufw` when it's active.

Then open `https://<host>/` and create the first admin account. The rest of
this document explains what the script does, for manual or custom installs.

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
git clone git@github.com:your-org/rustdesk-fleet.git ~/rustdesk-fleet
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
  Also TCP 443 (and 80 for certbot) for the dashboard, and — only while
  older clients still need it — **TCP 21114** for device reporting (see
  [Section 7](#7-client-device-reporting)).
- **OS firewall**, only if `ufw` is active (`sudo ufw status`) — allow
  22/tcp first, then the same ports.

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

# RustDesk client reporting (sysinfo + heartbeat) for clients that use the
# default http://<rendezvous-host>:21114. Only the three client endpoints are
# exposed — the dashboard itself is not reachable on this port.
# Remove this block once every client reports over HTTPS (see Section 7).
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

    location / {
        return 404;
    }
}
```

Enable it:

```bash
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

The client version is pinned in `/opt/rustdesk-fleet/rustdesk_version.txt`
and read at build time. To move to a newer RustDesk client release, use
**Server Status → Update installers to X** in the dashboard (admins; it
downloads the binaries, moves the pin and can rebuild every group's
installers in the background), or on the CLI:

```bash
python3 generate_installer.py update-version   # downloads binaries, moves the pin
# then rebuild each group's installers
```

To get clients onto it, send a download link from the group page (links
always serve the newest build) or turn on the group's **Automatic RustDesk
updates** (§7). Details of what the installer does, and the config it
writes: [`subsystems/single-tenant/README.md`](subsystems/single-tenant/README.md#installer-generation--built-and-verified-2026-06-30).

**Troubleshooting — device still shows the old version after reinstalling.**
Check the installer's build date on the group page. Windows installers
built before 2026-10-08 (commit `8db6d42`) don't wait for RustDesk's own
upgrade to finish and abort it, leaving the old version running; fresh
installs were unaffected. Rebuild the group's installer and run it again —
it now takes ~15–40 s because it waits for the new version to be
registered and the service to be back up. The device's version updates on
the Devices page within a minute of the install.

## 7. Client device reporting

The community RustDesk client reports its own details — hostname, OS,
logged-in user, CPU, RAM, client version — plus a heartbeat every ~15 s
(every 3 s during a session) to its "API server". The dashboard receives
these at `/api/sysinfo`, `/api/heartbeat` and `/api/sysinfo_ver`
(`subsystems/dashboard/app/routes/client_api.py`) and uses them for the
Computer/OS columns and for online / in-session status.

Where a client sends its reports:

- **Installers built from the current templates** set
  `api-server = "https://<host>"`, so those clients report over TLS
  through the normal 443 vhost. Nothing extra to open.
- **Clients installed from older installers** have no `api-server` set
  and fall back to `http://<host>:21114` (plain HTTP — hostnames and
  usernames cross the internet unencrypted). These need TCP 21114 open in
  the Lightsail firewall plus the port-21114 nginx block from Section 4.

To drop port 21114: rebuild each group's installer, reinstall it on the
older clients (a download link from the group page makes this easy),
then remove the 21114 nginx block, reload nginx and close TCP 21114 in
the Lightsail firewall. Devices still on old installers will then stop
reporting and fall back to the old status detection — they keep working
for remote access.

The endpoints are unauthenticated (the client sends no credentials), so
the receiver only accepts a report when its RustDesk id **and** uuid
match the hbbs peer table. It never sends "disconnect" commands, and the
only config ("strategy") it ever pushes back is the two managed client
settings below.

### Managed client settings

Each client group has a **Client Settings** section on its page, with two
settings that can each be **Not managed** (default — nothing is pushed;
devices keep what they have), **On** or **Off**:

| Setting | RustDesk option | What it does |
|---|---|---|
| Automatic RustDesk updates | `allow-auto-update` | The RustDesk service (Windows) checks ~30 s after it starts and then daily, downloads the latest release from `github.com/rustdesk/rustdesk`, and installs it only when no session is active. Config — including the server settings — is preserved. Devices go to RustDesk's **latest** release, not the version pinned for installers. |
| Remote settings changes | `allow-remote-config-modification` | When off (RustDesk's default), RustDesk masks its own window with a gray overlay and blocks input whenever the *remote* mouse is over it, so a controller can't change its settings. When on, whoever is connected can use RustDesk's window — including changing its password and server settings. |

Managed values are pushed to the group's verified reporting devices in the
heartbeat response (`strategy.config_options`), together, under one
per-group `policy_ts`. Clients echo it back as `modified_at` once applied,
so each change is sent once; the group page shows how many devices have
applied it.

New installers write both options as `"Y"` unless the group sets that
option to Off.

## 8. Backups

`subsystems/single-tenant/backup.py` snapshots the data that can't be
regenerated — the hbbs/hbbr **keypair** (`data/id_ed25519*`), the hbbs peer
registry (`data/db_v2.sqlite3`) and the dashboard database
(`fleet.sqlite3`), plus `docker-compose.yml` and `rustdesk_version.txt`.
Installer binaries and upstream assets are excluded (large, reproducible).
Both live SQLite databases are copied with SQLite's online-backup API, so
each archive is internally consistent. Archives land in
`/opt/rustdesk-fleet/backups/` and the newest N are kept — set as *Keep this
many local archives* under Admin → Backup & Restore (default 14; falls back to
`BACKUP_RETENTION`). Off-site copies have their own limit, *Keep this many
off-site copies* (default 0 = keep all): after each successful upload the
oldest `rustdesk-fleet-*` objects under the prefix beyond that number are
deleted, which requires `s3:DeleteObject` on the access key. With 0, use an
S3 lifecycle rule to expire old copies instead.

The Backup & Restore page also lists the **off-site archives** with Restore
(downloaded to a temp dir, then restored like a local archive; needs
`s3:GetObject`) and Delete (needs `s3:DeleteObject`). A restore's safety
snapshot is always local-only — it is never uploaded and never triggers
off-site pruning.

A systemd timer runs it nightly **as root** (so it also captures the
container's root-only `RustDesk.toml`):

```bash
cd ~/rustdesk-fleet/subsystems/single-tenant
sudo cp rustdesk-backup.service rustdesk-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rustdesk-backup.timer
systemctl list-timers rustdesk-backup.timer   # confirm next run
python3 backup.py run      # make one now (CLI); also: status | list
```

The dashboard Home page shows the last-run status and has **Back up now**
and **Download latest** (admins). A dashboard-triggered backup runs as the
`ubuntu` user and skips the root-only `RustDesk.toml` (recorded in the
status); the nightly root run captures it.

**Off-box copy (strongly recommended).** Local-only backups don't survive
loss of this box — which is the whole point, since the keypair can't be
regenerated and every client is pinned to it.

Everything is configured from the dashboard — **Admin → Backup &
Restore** — with no server access required. Both the dashboard and the
nightly timer use whatever destination is set.

Supported destinations are **S3-compatible object storage**: Amazon S3,
Wasabi, Backblaze B2, DigitalOcean Spaces, and MinIO / any S3-compatible
endpoint. These authenticate with an access key + secret, so they're set
up entirely in the GUI (pick the provider, enter region/bucket/keys; the
endpoint is auto-derived for the presets, or typed for MinIO/custom). Set
an **encryption passphrase** on the same page to gpg-encrypt archives
before upload, and use **Test connection** to verify before enabling.

(OAuth clouds such as Google Drive / OneDrive are intentionally not offered
in the UI: OAuth can't be completed from a web form without server-side
setup, which this deployment avoids. Use an S3-compatible bucket — Wasabi
and Backblaze B2 are inexpensive options.)

For automation without the dashboard DB, the engine also honours
`/etc/rustdesk-fleet/backup.env` as a fallback when no destination is
enabled in the dashboard:

```bash
sudo tee /etc/rustdesk-fleet/backup.env >/dev/null <<'EOF'
BACKUP_RETENTION=14
# Encrypt archives (gpg AES-256) before they leave the box:
BACKUP_PASSPHRASE=<a long random passphrase stored somewhere safe>
# Ship each archive off-box; {path} is substituted. Needs the tool installed
# and its credentials configured for the user the timer runs as (root):
BACKUP_OFFSITE_CMD=rclone copy "{path}" fleet-s3:rustdesk-backups
EOF
sudo chmod 600 /etc/rustdesk-fleet/backup.env
```

Restore: extract an archive, put `data/` and `fleet.sqlite3` back under
`/opt/rustdesk-fleet/` (decrypt first with `gpg -d` if encrypted), then
`cd /opt/rustdesk-fleet && docker compose up -d`. Keeping the same keypair
means existing clients reconnect without reconfiguration.

## 9. Backup & restore runbook

Day-to-day operation of backups, from the dashboard. The keypair is the one
thing that cannot be regenerated — every client is pinned to it — so the goal
is: an encrypted copy exists off this server, and you can restore it.

### One-time setup (do these once, then verify)

1. **Enable off-site copy.** Admin → Backup & Restore: pick the S3 provider,
   enter region / bucket / access key / secret, **Save & test connection**,
   then switch **Off-site copy ON** and Save. The toggle saves immediately.
2. **Set an encryption passphrase** (Local archives card). Every local and
   off-site archive is then gpg-encrypted. **Store the passphrase somewhere
   off this server** (password manager). Without it, no backup can be
   restored — losing the server would lose the backups too.
3. **Set retention.** *Keep this many local archives* (default 14) and *Keep
   this many off-site copies* (0 = keep all). Off-site pruning needs
   `s3:DeleteObject`; with 0, add an S3 lifecycle rule instead.
4. **Scope the IAM user to the bucket** — only these four actions:
   `s3:ListBucket`, `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`
   (drop DeleteObject if you prune with a lifecycle rule and want a leaked
   key unable to erase backups). If the bucket uses a customer-managed KMS
   key, also grant `kms:GenerateDataKey`.
5. **(Recommended) protect against deletion** — enable bucket versioning +
   a lifecycle rule that expires noncurrent versions after ~30 days, so a
   deleted or overwritten backup stays recoverable for a month.

### Routine checks

- **Daily/weekly:** Home page backup card shows **last backup ok**,
  **offsite ok**, **encrypted**, and the kept-on-server / kept-off-site
  counts. Nightly run is 03:30 UTC (`systemctl list-timers rustdesk-backup.timer`).
- **Off-site list:** Admin → Backup & Restore → **Off-site archives** lists
  what's actually in the bucket, with Restore and Delete per archive.
- **After any config change to a group's backup settings** the count on the
  page catches up on the next run.

### Restore (dashboard)

Both local and off-site restore take a **local safety snapshot first**,
replace the keypair + hbbs peer DB + dashboard DB, and restart the relay.
You may be logged out; sign back in.

- **From a local archive:** Local archives card → **Restore**.
- **From the bucket:** Off-site archives card → **Restore** (downloads it
  first; needs `s3:GetObject`). If the archive is encrypted, the passphrase
  must be set or the restore fails.
- **Restoring an older backup also restores the backup settings** it
  contains (they live in the dashboard DB) — re-check the Backup page after.

### Restore (manual, server shell)

If the dashboard is unavailable:

```bash
cd /opt/rustdesk-fleet
# fetch the archive (e.g. from S3) then, if it ends in .gpg:
gpg -d -o backup.tar.gz backup.tar.gz.gpg      # prompts for the passphrase
tar xzf backup.tar.gz                           # yields rustdesk-fleet/
cp -a rustdesk-fleet/data/. data/ && cp rustdesk-fleet/fleet.sqlite3 .
docker compose up -d
```

### Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Test connection fails, `AccessDenied` on list | `s3:ListBucket` must be on the **bucket** ARN, not `bucket/*`. |
| Home shows **offsite incomplete** | Toggle is on but bucket/keys missing — finish the destination and Save. |
| Home shows **offsite failed** | Upload error; hover the badge for the message (often keys or `s3:PutObject`). |
| Restore from bucket fails, `403` | Access key lacks `s3:GetObject`. |
| Off-site cleanup error on Home | Retention > 0 but key lacks `s3:DeleteObject` — grant it or set retention 0 + lifecycle rule. |
| "encrypted but no passphrase is configured" | Set the passphrase that archive was made with before restoring. |

## Verify everything

```bash
docker ps                                   # hbbs, hbbr Up
systemctl status rustdesk-dashboard          # active (running)
sudo nginx -t                                # config ok
curl -I https://rds.example.com/            # 303 to /login (expected, unauthenticated)
journalctl -u rustdesk-dashboard -n 50       # no tracebacks

# Device reporting (if the port-21114 block is enabled)
curl -s -o /dev/null -w '%{http_code}\n' http://rds.example.com:21114/        # 404 — dashboard not exposed
curl -s -X POST -d '{}' http://rds.example.com:21114/api/heartbeat             # {} — receiver reachable
sqlite3 /opt/rustdesk-fleet/fleet.sqlite3 \
  "select count(*) from device_info where heartbeat_at > datetime('now','-1 minute')"  # devices checking in
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
| `/opt/rustdesk-fleet/backups` | Backup archives + `last_backup.json` status (see Section 8) |
| `/etc/rustdesk-fleet/dashboard.env` | Dashboard secrets (`SESSION_SECRET`) — not in the repo |
| `/etc/rustdesk-fleet/backup.env` | Backup config (retention, passphrase, offsite cmd) — not in the repo |
| `/etc/systemd/system/rustdesk-dashboard.service` | Dashboard process supervisor |
| `/etc/nginx/sites-available/rds.example.com` | TLS + reverse proxy, plus the port-21114 client-reporting block |

## Out of scope here

- `subsystems/provisioning/` — superseded per-tenant design, kept as
  reference only, not part of this deployment.
- Installer code-signing (`subsystems/signing/`) — not built yet;
  installers are currently distributed unsigned.
