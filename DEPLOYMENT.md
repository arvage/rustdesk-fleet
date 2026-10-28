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
- Docker + Compose, Python 3.12, nsis (for installer builds), Pillow (for
  client branding icons), nginx + certbot (for the dashboard's TLS):
  ```bash
  sudo apt update && sudo apt install -y docker.io docker-compose-v2 \
      python3 python3-pip python3-pil nsis nginx certbot python3-certbot-nginx
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
SETUP_TOKEN=<generate with: python3 -c "import secrets; print(secrets.token_urlsafe(24))">
EOF
sudo chmod 600 /etc/rustdesk-fleet/dashboard.env
```

| Variable | Required | Meaning |
|---|---|---|
| `SESSION_SECRET` | yes | Signs session cookies. The dashboard refuses to start if it is missing, shorter than 32 characters, or the old `change-me-in-production` placeholder. |
| `SETUP_TOKEN` | until the first admin exists | Unlocks `/setup` (Section 5). Ignored once a user exists; the line can then be deleted. |
| `SESSION_MAX_AGE` | no | Session lifetime in seconds. Default 43200 (12 hours). |
| `FLEET_DATA_DIR` | no | Data directory. Default `/opt/rustdesk-fleet`; the tests point it at a temp dir. |
| `FLEET_TENANT1_SLUG`, `FLEET_TENANT1_NAME` | no | Slug (lowercase letters, digits, hyphens) and display name given to tenant #1 when the tenancy migration creates it. Default `tenant1` / `Tenant 1`. Read once; afterwards the `tenants` row is what counts. |
| `PROVIDER_DOMAIN` | for the provider console | e.g. `rds.example.com`. The console is `admin.<domain>`; each tenant gets `<slug>.<domain>` (web) and `<slug>.relay.<domain>` (relay). Empty: no console, no new tenants. Also read by `rdf-helper` (as root) to validate hostnames. |
| `PROVIDER_SESSION_MAX_AGE` | no | Provider console session lifetime in seconds. Default 28800 (8 hours). |
| `PROVIDER_ALLOWED_IPS` | no | Comma-separated IPs or CIDRs allowed to reach the console (checked on `X-Real-IP`). Empty: no restriction. |
| `RELAY_PORT_BASE`, `RELAY_PORT_MAX` | no | Port range for tenant relays. Default 21200–21999 (80 blocks of 10). Keep it open in the cloud firewall. |
| `RDF_HELPER` | no | Path of the root helper. Default `/usr/local/sbin/rdf-helper`. |

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
# Per-IP limit on sign-in POSTs (must be outside any server block). Only
# POSTs get a key, and requests with an empty key aren't limited, so page
# loads (GET) never count.
map $request_method $rdf_auth_key {
    POST    $binary_remote_addr;
    default "";
}
limit_req_zone $rdf_auth_key zone=rdf_signin:10m rate=30r/m;

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

    # Sign-in endpoints: 30 POSTs/min per IP, burst 20, then 429 (sized for
    # an office signing in from one NAT IP at shift start). The app
    # also locks an email after 5 failures and an IP after 20 (15 min each).
    # Repeat this block for `location ^~ /login/webauthn` (no trailing
    # slash, or nginx and the app redirect each other in a loop) and
    # `location = /setup`. Don't limit the device API (/api/heartbeat etc.).
    # Every location that proxies to the dashboard must clear
    # X-Fleet-Listener (an empty value isn't passed on): only the port-21114
    # block below may set it.
    location = /login {
        limit_req        zone=rdf_signin burst=20 nodelay;
        limit_req_status 429;
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  "";   # drop any client-sent copy
        proxy_read_timeout 60s;
    }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Fleet-Listener  "";   # drop any client-sent copy
        proxy_read_timeout 60s;
    }
}

# RustDesk client reporting (sysinfo + heartbeat + connection/file audit) for clients
# that use the default http://<rendezvous-host>:21114. Only the client
# reporting endpoints are exposed — the dashboard itself is not reachable on this port.
# Remove this block once every client reports over HTTPS (see Section 7).
# It belongs to tenant #1: devices here may send any Host (often an IP), so it
# tags requests with X-Fleet-Listener and the dashboard maps an unknown host
# on this listener to tenant #1 (subsystems/dashboard/TENANCY.md).
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
        proxy_set_header   X-Fleet-Listener  legacy-api;   # overrides any client-sent value
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

### Updating the dashboard

The service runs straight from the repo checkout, so an update is a pull and
a restart:

```bash
cd ~/rustdesk-fleet && git pull
sudo systemctl restart rustdesk-dashboard
systemctl is-active rustdesk-dashboard          # active
journalctl -u rustdesk-dashboard -n 30          # "Application startup complete."
```

Database changes apply themselves on startup (`run_migrations()` is
idempotent; most releases only add tables or columns), and nothing else in
`/opt/rustdesk-fleet` is touched. Template and CSS edits take effect without
a restart, but Python changes need one. A release that rebuilds tables (the
tenancy release below) says so and comes with a check to run on a copy first.

**Upgrading to the sign-in hardening release** (session versions, setup
token, rate limits):

- Everyone is signed out once. Sessions now carry a `session_version`
  that older cookies don't have.
- Sessions expire after 12 hours (`SESSION_MAX_AGE`), not 14 days.
- Sign out is a POST from the sidebar button. Visiting `/logout` directly
  now just goes to `/` without signing out.
- The dashboard won't start if `SESSION_SECRET` is shorter than 32
  characters. Check before restarting:
  `sudo awk -F= '/^SESSION_SECRET=/{print length($2)}' /etc/rustdesk-fleet/dashboard.env`
- No `SETUP_TOKEN` is needed on a box that already has users.
- The nginx rate limit is not applied by a `git pull`. Add the
  `map` and `limit_req_zone` lines and the three sign-in `location` blocks
  shown in Section 4 to your vhost, then `sudo nginx -t && sudo systemctl reload nginx`.
  If your vhost already has a `rdf_auth` zone keyed on `$binary_remote_addr`,
  replace it with the `rdf_signin` one rather than re-keying it: nginx won't
  change an existing zone's key on reload. It keeps running the old config
  and only logs an `[emerg]` line in `/var/log/nginx/error.log`.

**Upgrading to the tenancy release** (phase 1a, see
[`subsystems/dashboard/TENANCY.md`](subsystems/dashboard/TENANCY.md)): on
startup the dashboard makes the existing deployment **tenant #1** and
rebuilds every tenant-owned table with a `tenant_id`, in one transaction.
URLs, the relay, its keys, devices, users, passkeys, sessions and installers
stay exactly as they are; nobody is signed out. `server_config` is kept,
unused, for one release in case of a rollback. Do it in this order:

1. **Back up**, with the current code still running:

   ```bash
   sudo python3 ~/rustdesk-fleet/subsystems/single-tenant/backup.py run    # full archive
   # plus a plain copy of the dashboard DB, taken with SQLite's online backup:
   python3 -c "import sqlite3; s = sqlite3.connect('file:/opt/rustdesk-fleet/fleet.sqlite3?mode=ro', uri=True); d = sqlite3.connect('$HOME/fleet-pre-tenancy.sqlite3'); s.backup(d); d.close()"
   ```

2. **Check the migration on a copy**, using the new code from a separate
   worktree, so the running checkout is untouched:

   ```bash
   cd ~/rustdesk-fleet && git fetch origin
   git worktree add ~/rdf-tenancy-check origin/main
   cp ~/fleet-pre-tenancy.sqlite3 /tmp/fleet-check.sqlite3
   FLEET_TENANT1_SLUG=acme-it FLEET_TENANT1_NAME="Acme IT" \
     python3 ~/rdf-tenancy-check/subsystems/dashboard/scripts/check_migration.py /tmp/fleet-check.sqlite3
   ```

   It prints row counts per table before and after (they must match, apart
   from the new `tenants`, `relays` and `tenant_domains` rows), any row left
   without a `tenant_id`, the foreign-key and integrity checks, tenant #1's
   relay and domain, and whether a second run changed anything. It ends with
   `All checks passed.` and exits 0, or lists the problems and exits 1. It
   refuses to open the live database. Don't go further until it passes.

3. **nginx first** (safe with the old code, which ignores the header). In the
   live vhost, add `proxy_set_header X-Fleet-Listener "";` to every
   `location` that proxies to the dashboard, and
   `proxy_set_header X-Fleet-Listener legacy-api;` to the port-21114 block
   (Section 4 shows both). Then:

   ```bash
   date -u                                   # note the time
   sudo nginx -t && sudo systemctl reload nginx
   sudo tail -n 20 /var/log/nginx/error.log  # no [emerg] after that time
   ```

   Without this, devices on the old port-21114 channel that send an IP
   address as their Host would be rejected after the upgrade.

4. **Name tenant #1**: add `FLEET_TENANT1_SLUG=…` and `FLEET_TENANT1_NAME=…`
   (the values you used in step 2) to `/etc/rustdesk-fleet/dashboard.env`
   (`sudo nano` it; keep it mode 600). Skip this and tenant #1 is called
   `tenant1` / "Tenant 1".

   **Deploy and restart:**

   ```bash
   cd ~/rustdesk-fleet && git pull --ff-only
   sudo systemctl restart rustdesk-dashboard
   journalctl -u rustdesk-dashboard -n 50    # "Application startup complete.", no traceback
   ```

5. **Check the live site** (still signed in from before: the session must
   survive):

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://rds.example.com/login                      # 200
   curl -s -X POST -d '{}' https://rds.example.com/api/heartbeat                                # {}
   curl -s -X POST -d '{}' http://rds.example.com:21114/api/heartbeat                           # {} (if 21114 is open)
   curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: unknown.example.net' https://127.0.0.1/  # 404
   python3 -c "import sqlite3; c = sqlite3.connect('file:/opt/rustdesk-fleet/fleet.sqlite3?mode=ro', uri=True); print(c.execute(\"SELECT COUNT(*), MAX(heartbeat_at) FROM device_info WHERE tenant_id = 1 AND heartbeat_at > datetime('now', '-2 minutes')\").fetchone())"
   ```

   The last one should show devices heartbeating in the last two minutes.
   In the browser: Devices, a group page, Logs and Server Status look as
   before, and you are still signed in.

6. Remove the check worktree: `git -C ~/rustdesk-fleet worktree remove ~/rdf-tenancy-check`.

**Rollback:** stop the dashboard, put the pre-tenancy copy back, check out the
last pre-tenancy commit, start again. The nginx header lines can stay.

```bash
sudo systemctl stop rustdesk-dashboard
cp ~/fleet-pre-tenancy.sqlite3 /opt/rustdesk-fleet/fleet.sqlite3
rm -f /opt/rustdesk-fleet/fleet.sqlite3-wal /opt/rustdesk-fleet/fleet.sqlite3-shm
cd ~/rustdesk-fleet && git checkout <last commit before the tenancy release>
sudo systemctl start rustdesk-dashboard
```

Anything recorded between the upgrade and the rollback (sessions, device
reports, edits) is lost with the copy; the relay itself is never touched.

## 5. First-run setup

Open the setup URL that `install.sh` prints at the end:
`https://rds.example.com/setup?token=<SETUP_TOKEN>`. With an empty `users`
table every route redirects to `/setup`, a one-time page to create the
first admin account. It only accepts the form with the `SETUP_TOKEN` from
`/etc/rustdesk-fleet/dashboard.env`, so nobody who finds the server first
can claim it. Without a token configured, `/setup` explains how to add one
and accepts nothing. Lost the URL? Read the token from the env file:
`sudo grep SETUP_TOKEN /etc/rustdesk-fleet/dashboard.env`.

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

Each incoming remote connection is also reported to `/api/audit/conn`:
once when it opens (with the connecting IP), once when it's authorized
(with the connecting computer's RustDesk ID and name and the connection
type), and once when it closes. The dashboard attaches these to the
matching session, which is how Logs → Remote sessions shows **Connected
by**, **From IP** and **Type**. The 21114 nginx block must include
`audit/conn` in its `location` regex — if it doesn't, nginx answers 404
(silently, since that block has `access_log off`) and those columns stay
empty for clients on older installers.

File transfers (the file transfer window, and files copy-pasted through
the clipboard) are reported to `/api/audit/file` with the direction,
folder, file count and the largest 10 file names and sizes. They appear as
markers on the device's connection timeline. The 21114 block needs
`audit/file` in its regex too.

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
Backups are run by the provider and cover every relay in the `relays`
table: tenant #1's relay is stored as above, any other relay under
`relays/<tenant id>/` in the same archive. The backup controls are in the
provider console (Backups).
Installer binaries and upstream assets are excluded (large, reproducible).

**Purged tenants and backups.** Purging a tenant (provider console, 30 days
after cancellation) deletes its data from this box, but archives taken
before the purge still contain it: its rows in `fleet.sqlite3` and its
relay's keypair and peer database. They disappear as backup retention
rotates those archives out (local: *Keep this many local archives*;
off-site: *Keep this many off-site copies* or the bucket's lifecycle rule).
Restoring such an archive would bring the purged tenant back as it was.
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

## 10. Code signing (Azure Trusted Signing)

Unsigned installers trigger a Windows SmartScreen "unknown publisher" warning.
The dashboard signs Windows installers with **Azure Trusted Signing** (renamed
**Azure Artifact Signing** in Jan 2026 — the portal may show either name),
using `jsign` **on this Linux box** — no Windows runner or GitHub Actions
needed. Signing is configured entirely in Admin → Signing once the Azure side
is set up.

### Server prerequisites (one-time, already done on this box)

```bash
sudo apt-get install -y default-jre-headless          # Java runtime for jsign
sudo mkdir -p /opt/rustdesk-fleet/tools
sudo curl -fsSL -o /opt/rustdesk-fleet/tools/jsign-7.1.jar \
  https://repo1.maven.org/maven2/net/jsign/jsign/7.1/jsign-7.1.jar
```

The signing engine (`subsystems/single-tenant/sign_installer.py`) expects the
jar at `/opt/rustdesk-fleet/tools/jsign-7.1.jar`. jsign 7.0+ is required (it
added the `TRUSTEDSIGNING` store type).

### Azure setup (one-time, in the Azure portal)

Trusted Signing needs an **Azure subscription**. Identity validation can take
days for the first certificate profile — start it early.

1. **Register the provider:** Subscription → Resource providers → register
   `Microsoft.CodeSigning`.
2. **Create a Trusted Signing / Artifact Signing account.** Note its **region**
   — the signing endpoint is regional.
3. **Validate an identity:** in the account, create an **Identity validation**
   (Public Trust for software shipped to customers). This is where the wait is.
   It needs the **Trusted Signing Identity Verifier** role, which only works in
   the portal.
4. **Create a certificate profile** (Public Trust) under the account once the
   identity is validated. Note its **name**.
5. **Create a service principal:** Entra ID → App registrations → New. Note the
   **Directory (tenant) ID** and **Application (client) ID**. Under the app →
   Certificates & secrets, create a **client secret** and copy its value now
   (shown once).
6. **Grant the signer role:** on the signing **account** → Access control (IAM)
   → Add role assignment → **Trusted Signing Certificate Profile Signer**
   (a.k.a. *Artifact Signing Certificate Profile Signer*) → assign to the
   service principal from step 5. This role is what actually permits signing.
7. **Copy the endpoint URI** from the account's **Overview** page — it looks
   like `https://eus.codesigning.azure.net` (the region host differs: `eus`,
   `weu`, `wus2`, …).

### Dashboard configuration

Admin → **Signing**:

| Field | Value |
|---|---|
| Directory (tenant) ID | step 5 |
| Application (client) ID | step 5 |
| Client secret | step 5 (blank on later saves keeps the stored one) |
| Endpoint | the host from step 7, e.g. `eus.codesigning.azure.net` |
| Account name | the signing account name (case-sensitive) |
| Certificate profile | the profile name from step 4 |

Turn **Code signing ON**, leave **auto-sign** on to sign every build, then
**Save & test** — it mints an Azure token and checks jsign. With auto-sign on,
`Back up now`–style rebuilds come out signed; otherwise use the **Sign** button
on each installer (group page). Download links serve the signed file
automatically (links prefer `signed_path`). Signing appends an RFC3161
timestamp from `http://timestamp.acs.microsoft.com`, so signatures stay valid
after the certificate rotates.

### Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Test fails, `AADSTS7000215` | Wrong client secret — recreate it and re-save. |
| Test fails, `AADSTS700016` / `AADSTS90002` | Wrong client ID or tenant ID. |
| Sign fails, `Unauthorized` / 403 | Service principal missing the **Certificate Profile Signer** role on the account. |
| Sign fails, endpoint/account error | Endpoint host doesn't match the account's region, or account name case is wrong. |
| Test fails, "jsign jar not found" | Install the jar (server prerequisites above). |
| Signs but still warns on download | Identity validation / certificate profile not fully provisioned yet. |

## 11. Provider console and tenants

The provider console (`https://admin.<PROVIDER_DOMAIN>/`) is where we create
and manage tenants (MSPs). Design: [`subsystems/dashboard/TENANCY.md`](subsystems/dashboard/TENANCY.md).

### One-time setup

1. **DNS.** Two wildcard A records pointing at this box (no DNS API is used):

   ```
   *.rds.example.com          A   <box IP>     # admin.…, <slug>.… (web)
   *.relay.rds.example.com    A   <box IP>     # <slug>.relay.… (relays)
   ```

   Moving one tenant's relay to another box later is one more specific
   record (`<slug>.relay.rds.example.com`).

2. **Firewall.** In the cloud firewall (Lightsail networking tab, by hand —
   the console never calls the AWS API), open the relay range once, up front:
   **TCP 21200–21999 and UDP 21200–21999** (or your `RELAY_PORT_BASE`–
   `RELAY_PORT_MAX`). Each tenant uses 5 ports of a 10-port block: TCP B–B+4
   and UDP B+1 (TENANCY.md, "Port layout"). The console's Relays and Settings
   pages show the range it expects. Ports 80 and 443 must already be open
   (certificates use Let's Encrypt HTTP-01).

3. **The helper, certbot and the console's certificate.** On a box that is
   already installed:

   ```bash
   cd ~/rustdesk-fleet
   ./install.sh --provider-only --provider-domain rds.example.com --email you@example.com
   ```

   This touches nothing else (not the existing vhost, relay or units). It:
   - installs `certbot` and `python3-reportlab` (client PDF guides);
   - copies `subsystems/provisioning/rdf_helper.py` to
     `/usr/local/sbin/rdf-helper` (root:root, 0755) and writes
     `/etc/sudoers.d/rdf-helper`:
     `<dashboard user> ALL=(root) NOPASSWD: /usr/local/sbin/rdf-helper`.
     That helper is the dashboard's only root access: `issue-cert`,
     `write-vhost`, `remove-vhost`, `revoke-cert`, `reload-nginx`,
     `purge-relay-data`, each validating its arguments (hostnames must be
     `<slug>.<PROVIDER_DOMAIN>` or the console host). Root never runs files
     from the repo checkout;
   - adds `PROVIDER_DOMAIN` to `/etc/rustdesk-fleet/dashboard.env` if missing
     and restarts the dashboard;
   - writes `/etc/nginx/conf.d/rdf-provider.conf`: a port-80 server for
     `*.<PROVIDER_DOMAIN>` (ACME challenges from `/var/www/certbot`, else a
     redirect to HTTPS) and `include /etc/nginx/rdf-tenants/*.conf;`, where the
     helper writes one 443 vhost per tenant domain and the console. Those
     vhosts use the `rdf_signin` rate-limit zone defined by the main vhost;
   - issues the console's certificate and writes its vhost. Every nginx
     reload checks `nginx -t` and fails on any new `[emerg]` in the error log.

   Re-run it after changing `--email` or if DNS wasn't ready the first time.

4. **The first provider admin.** Provider admins sign in with passkeys only:

   ```bash
   python3 ~/rustdesk-fleet/subsystems/dashboard/scripts/provider_admin.py --domain rds.example.com add you@example.com "Your Name"
   ```

   `--domain` goes before the command. It's needed because
   `/etc/rustdesk-fleet/dashboard.env` is readable by root only, so the script
   can't take `PROVIDER_DOMAIN` from it (setting `PROVIDER_DOMAIN` in the
   environment works too). It prints a one-time enrollment link (valid 1 hour). Open it on the device
   you'll sign in from and create a passkey. More admins: invite them from the
   console (Provider admins), or `provider_admin.py --domain rds.example.com enroll-link EMAIL`
   for a new link, `disable EMAIL`, `list`.

5. **Provider email.** Console → Settings: SMTP for provider alerts (to every
   provider admin), console invitations and emails to tenants' admins;
   host-health thresholds; the Let's Encrypt account email.

### Creating a tenant

Console → Tenants → New tenant: name, slug (3–32 characters, a letter first;
`admin`, `relay`, `www` and the like are reserved, and slugs of purged tenants
stay taken) and the first admin's email. The job (shown live) creates the
tenant (trial), provisions its relay on the next port block, issues the
certificate and vhost for `<slug>.<PROVIDER_DOMAIN>`, builds the tenant's
PDF guides, and emails a one-time invitation (72 hours) to
`https://<slug>.<PROVIDER_DOMAIN>/invite/<token>`; the link is also shown in
the job and on the tenant's page (Resend invite). If a step fails, everything
is undone except the port block; fix the cause and create it again.

Tenants' installers point at `<slug>.relay.<PROVIDER_DOMAIN>:<ports>` with
the tenant's own key and `api-server = https://<slug>.<PROVIDER_DOMAIN>`.

### Lifecycle

New tenants start on a 14-day trial (its users see a banner with the days
left; admins are emailed 3 days before) and are suspended when it ends.
**Activate** moves one to active; **Extend trial** adds days. Suspend (dashboard and links off, relay keeps running), reactivate, cancel
(relay stops now, the admins can only export CSVs, purge after 30 days;
admins are emailed at cancellation and 7 days before), extend the grace
period, purge now (type the slug). Tenant #1 can't be cancelled or purged.
What purging keeps and removes: TENANCY.md, "Lifecycle". Backups taken
before a purge still hold the tenant's data (Section 8).

### Upgrading the live box to phase 1b

Pacific MIT keeps its URL, relay, ports, keys, devices, installers and
sessions. What changes for it: backups, signing, host health, the client
version and relay upgrades/restarts move to the console; its Server Info
shows hbbs port **21116** (the rendezvous port; 21115 was the NAT-test port).

1. Back up, and keep a plain copy of the database (as in the phase 1a
   upgrade, Section 4):
   `sudo python3 ~/rustdesk-fleet/subsystems/single-tenant/backup.py run`, then
   the `sqlite3` online-backup one-liner to `~/fleet-pre-1b.sqlite3`.
2. Check the migrations on a copy with the new code (from a worktree of the
   new commit): `python3 <worktree>/subsystems/dashboard/scripts/check_migration.py /tmp/fleet-check.sqlite3`.
   It must end with `All checks passed.`
3. Add the DNS records and open the firewall range (above). Wait until
   `getent hosts admin.rds.example.com` resolves to the box.
4. `git pull --ff-only` (or merge the branch) in `~/rustdesk-fleet`, then
   `./install.sh --provider-only --provider-domain rds.example.com --email you@example.com`.
   It restarts the dashboard, which runs the migrations (provider tables,
   tenants status `purged`, relay port blocks, tenant #1's hbbs port fix).
5. `python3 subsystems/dashboard/scripts/provider_admin.py --domain rds.example.com add you@example.com "Your Name"`,
   enroll your passkey, sign in to `https://admin.rds.example.com/`, set
   Settings (SMTP, Let's Encrypt email).
6. Check: tenant #1's site and sign-in work without signing in again,
   devices still heartbeat, `nginx -t` passes, no new `[emerg]` in
   `/var/log/nginx/error.log`, the console's Tenants, Relays and Host health
   pages show tenant #1.

Rollback: stop the dashboard, restore `~/fleet-pre-1b.sqlite3`, check out the
previous commit, remove `/etc/nginx/conf.d/rdf-provider.conf` and
`/etc/nginx/rdf-tenants/`, reload nginx, start the dashboard.

### End-to-end checklist (after deploy)

1. Console → New tenant: a test tenant (`e2etest`), with your own email as
   admin. The job finishes; `https://e2etest.rds.example.com/` loads with a
   valid certificate.
2. Accept the invitation; you land in the test tenant's dashboard (not
   Pacific MIT's). Create a client group and build its **Windows** installer.
3. Install it on a spare machine. Within a minute the device shows up under
   the test tenant's Devices, with its details (hostname, OS: the device API
   over `https://e2etest…`), and **not** in Pacific MIT's.
4. On a technician machine, set RustDesk's ID server to
   `e2etest.relay.rds.example.com:<hbbs port>`, relay
   `e2etest.relay.rds.example.com:<hbbr port>` and the test tenant's key
   (console → tenant page). Connect to the device; a session appears in the
   test tenant's Logs.
5. Console → cancel the tenant. The device goes offline within a minute or
   two; the test tenant's dashboard shows only the export page.
6. Reactivate. The device comes back online by itself (same keys and ports).
7. Cancel again, then "Purge now" (type the slug). `https://e2etest…` stops
   answering, the tenant shows as purged, and its port block stays reserved.

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
curl -s -X POST -d '{}' http://rds.example.com:21114/api/audit/conn            # REJECTED — audit receiver reachable (not nginx 404)
curl -s -X POST -d '{}' http://rds.example.com:21114/api/audit/file            # REJECTED — file audit receiver reachable
sqlite3 /opt/rustdesk-fleet/fleet.sqlite3 \
  "select count(*) from device_info where heartbeat_at > datetime('now','-1 minute')"  # devices checking in
```

## Where things live

| Path | What |
|---|---|
| `~/rustdesk-fleet` | Repo checkout — code only, no secrets/data |
| `/opt/rustdesk-fleet/fleet.sqlite3` | The control database: every tenant's dashboard data, with a `tenant_id` on each tenant-owned row, plus `tenants`, `relays` and `tenant_domains` (see `subsystems/dashboard/TENANCY.md`) |
| `/opt/rustdesk-fleet/docker-compose.yml` | Deployed compose file (may drift from the repo template after an in-place server update — see Section 2) |
| `/opt/rustdesk-fleet/data` | Tenant #1's relay: hbbs/hbbr keypair, hbbs peer DB + runtime state (container's `/root`). Every relay's paths are in its `relays` row |
| `/opt/rustdesk-fleet/installer-assets` | Upstream RustDesk client binaries used as installer input |
| `/opt/rustdesk-fleet/installers` | Generated per-group installer output (tenant #1; other tenants: `tenants/<id>/installers`) |
| `/opt/rustdesk-fleet/backups` | Backup archives + `last_backup.json` status (see Section 8) |
| `/opt/rustdesk-fleet/branding` | Tenant #1's per-group installer icons (`<group_id>.png/.ico`) and email logo (`email-logo.png`, set in Admin → Email templates → Design) |
| `/opt/rustdesk-fleet/tenants/<id>/` | Any other tenant's `installers/` and `branding/` (none yet) |
| `/opt/rustdesk-fleet/tools` | `jsign` jar for installer signing (see Section 10) |
| `/opt/rustdesk-fleet/os-lifecycle.json` | Cached Windows end-of-support dates (from endoflife.date, refreshed daily by the dashboard; safe to delete — it's re-downloaded) |
| `/etc/rustdesk-fleet/dashboard.env` | Dashboard secrets (`SESSION_SECRET`) — not in the repo |
| `/etc/rustdesk-fleet/backup.env` | Backup config (retention, passphrase, offsite cmd) — not in the repo |
| `/etc/systemd/system/rustdesk-dashboard.service` | Dashboard process supervisor |
| `/etc/nginx/sites-available/rds.example.com` | TLS + reverse proxy, plus the port-21114 client-reporting block |

## Out of scope here

- `subsystems/provisioning/` — superseded per-tenant design, kept as
  reference only, not part of this deployment.
