# Dashboard

FastAPI + Jinja2 web UI served by uvicorn behind nginx (TLS terminated by nginx, Let's Encrypt cert). Runs as a systemd service (`rustdesk-dashboard.service`) on the Lightsail box.

Live at `https://rds.example.com`.

## Stack

- **Runtime**: Python 3.12 / FastAPI 0.138 / uvicorn
- **Auth**: per-user bcrypt-hashed passwords stored in `users` table; session cookie (HTTPS-only, `SameSite=Lax`); session secret from env
- **DB**: `/opt/rustdesk-fleet/fleet.sqlite3` — same SQLite file as `setup_server.py`
- **Static assets**: `/app/static/style.css` — custom CSS, no framework (Inter font, indigo accent)
- **Reverse proxy**: nginx (`/etc/nginx/sites-available/rds.example.com`) proxies `:443` → `127.0.0.1:8000`, plus a `:21114` block that exposes only the client-reporting endpoints (see `DEPLOYMENT.md`)

## Service file

`rustdesk-dashboard.service` — managed by systemd, `WorkingDirectory` is the repo checkout, `PYTHONPATH` set to `subsystems/single-tenant/` so `setup_server.py` and `generate_installer.py` are importable.

Env vars loaded from `/etc/rustdesk-fleet/dashboard.env`:
- `SESSION_SECRET` — random hex string, required in production
- `DASHBOARD_PASSWORD` — legacy, no longer consulted (auth now uses the `users` table)

## First-run setup

If the `users` table is empty, all routes redirect to `/setup`. The setup page creates the first admin account (email + bcrypt-hashed password). After that, `/setup` is permanently locked out.

## Routes

Staff pages (all require login):

| Route | Description |
|---|---|
| `GET /` | Server Status: a **Server Health** section (live CPU/memory/disk/uptime, hbbs/hbbr containers, devices online — polled from `/api/server/health`, with alert-threshold markers), a **Server Info** section (status, host, ports, server/client versions with update banners, public key), and a **Backups** section (last-run status, Back up now, download latest) |
| `POST /server/update` | Update hbbs/hbbr to the latest release (`update_server`) |
| `POST /installers/update-version` | Move installers to the latest RustDesk client release, optionally rebuilding every group's installers in the background (`update_server`) |
| `POST /server/backup`, `GET /server/backup/download/latest` | Run a backup now / download the newest archive (`manage_backups`) |
| `GET /devices` | Device inventory: Computer / OS / client version (client-reported), live online · in session · offline status, group filter; **ⓘ details** modal per device (full reported details + recent sessions) |
| `POST /devices/{id}/edit` · `/delete`, `POST /devices/sync` | Label/group a device (`manage_devices`); delete hides it and clears its details/sessions — a verified heartbeat later auto-un-hides it; import peers from hbbs |
| `GET /groups`, `POST /groups` | Group list (with live "X / Y online"), create group |
| `GET /groups/{slug}` | Group detail: devices, Client Settings, installers, download links |
| `POST /groups/{slug}/edit` | Rename / change slug / unattended password |
| `POST /groups/{slug}/client-settings` | Per-group managed client settings (auto-update, remote settings changes) |
| `POST /groups/{slug}/build`, `POST /groups/{slug}/installers/{id}/delete` | Build / delete installers |
| `POST /groups/{slug}/links`, `/links/{id}/email`, `/links/{id}/revoke` | Create, email, revoke client download links |
| `GET /download/{filename}` | Staff installer download (path-traversal safe, DB-gated) |
| `GET /users` + `POST /users/...` | User management: assign roles, group access, password reset, passkeys, MFA reminders (`manage_users`) |
| `GET /admin` | Admin landing: tool cards + fleet stats (any admin-area capability) |
| `GET /admin/backup` + `POST /admin/backup/save` · `/test` · `/run` · `/restore`, `GET /admin/backup/download` | Backup destination config (S3/Wasabi/B2/Spaces/MinIO), test, run, restore, per-archive download (`manage_backups`) |
| `GET /admin/roles` + `POST /admin/roles...` | Roles & permissions: create/edit/delete permission levels via a capability matrix (`manage_roles`) |
| `GET /admin/export/devices.csv` | Device inventory CSV (`manage_devices`) |
| `POST /admin/restart-relay`, `POST /admin/logs/prune` | Restart hbbs/hbbr; prune old audit events (`manage_system`) |
| `GET /account` + `POST /account/...` | Own profile, password, passkeys, sign-in method |
| `GET /notifications` + `POST /notifications/...` | SMTP settings, per-event toggles, server-health alert thresholds, test email, send log (`manage_notifications`) |
| `GET /audit` | **Logs** — categorized tabs (All / Clients / Remote sessions / Installers / Groups / Users & auth / Server), per-tab filter; the Remote sessions tab reads the session audit log |
| `GET /sessions` | Redirects to `/audit?cat=sessions` (kept for old links) |
| `GET /api/devices`, `GET /api/devices/status`, `GET /api/devices/{id}/sessions`, `GET /api/server/health` | JSON for the live tables, per-device session history, and server-health tiles |
| `GET /setup`, `GET/POST /login`, `/login/webauthn/...`, `GET /logout` | First-run setup, password and passkey sign-in |

Public, no login:

| Route | Description |
|---|---|
| `GET /d/{token}` | Client-facing download page for a shared link (noindex, no-referrer) |
| `GET /d/{token}/{platform}` | The installer itself; counts toward the link's download limit |
| `POST /api/sysinfo`, `/api/heartbeat`, `/api/sysinfo_ver` | RustDesk client reporting. Accepted only when id + uuid match the hbbs peer table. Also served on port 21114 (see `DEPLOYMENT.md` §7) |

## Features

- **Auth**: per-user passwords (bcrypt) and passkeys (WebAuthn), optional
  per-user MFA.
- **Roles & permissions** (`app/permissions.py`, Admin → Roles): capability-
  based permission levels, not a fixed admin/tech pair. Capabilities —
  manage_devices, manage_groups, update_server, manage_notifications,
  manage_backups, manage_system, manage_users, manage_roles — are enforced
  server-side on every write route; viewing is always allowed, so a role with
  no capabilities is read-only. Built-in roles: Administrator (locked = all),
  Technician (devices + groups), Viewer (read-only). Admins can create custom
  roles via a checkbox matrix and assign them on the Users page. The UI hides
  controls a role can't use.
- **Device inventory**: hbbs peers merged with the fleet DB; labels and
  groups; hide/restore. Each reporting client sends its computer name,
  OS + build, logged-in user, CPU, RAM and RustDesk version; versions older
  than the installers' are flagged.
- **Status**: heartbeat-based (online within ~60 s, *in session* while
  connected), falling back to connection/registration signals if the
  reporting channel goes quiet fleet-wide. Shown on Devices, group pages
  and the group list.
- **Installers**: Windows x64/ARM64 (NSIS), macOS and Linux scripts per
  group, with the group's unattended password and client settings baked
  in. Client version pinned and updatable from the dashboard. Upgrades
  over an existing install wait for RustDesk's own install to finish.
- **Download links**: per-group `/d/<token>` links with expiry, download
  limit, revoke, optional email to the client; always serve the newest
  build and show which RustDesk version that is.
- **Managed client settings** (per group: not managed / on / off):
  `allow-auto-update` and `allow-remote-config-modification`, pushed to
  reporting devices via the heartbeat response and written into new
  installers.
- **Server health** (`/api/server/health`): live CPU, memory, swap, disk,
  host uptime, hbbs/hbbr container state and devices-online, shown as tiles
  on the Server Status page (stdlib only — reads `/proc`, `statvfs`,
  `docker ps`). Meters mark the configured alert thresholds.
- **Session audit**: the heartbeat's active-connection list is turned into a
  `device_sessions` log (open/close/duration), surfaced on the Logs "Remote
  sessions" tab and per-device in the details modal. Records which device was
  accessed and for how long (the OSS client doesn't report the controller).
- **Backups** (`subsystems/single-tenant/backup.py`): nightly systemd timer
  (as root) + on-demand, snapshotting the hbbs/hbbr keypair, hbbs peer DB and
  the dashboard DB (consistent SQLite online-backup copies), with rotation,
  optional gpg encryption, restore, and off-site upload to S3-compatible
  storage (AWS S3 / Wasabi / Backblaze B2 / DigitalOcean Spaces / MinIO)
  configured entirely in the GUI via `backup_remote.py`.
- **Notifications** (email): device registered / deleted / offline,
  installer built / deleted / downloaded via link, user created / deleted,
  new server version, new client version, and **server memory / disk above a
  configurable threshold** (debounced, with hysteresis).
- **Logs**: categorized audit log (clients, remote sessions, installers,
  groups, users & auth, server) of provisioning and admin actions.

Heartbeat/sysinfo requests are excluded from the uvicorn access log
(`app/main.py`) — devices post every few seconds.

**Out of scope (by design):**
- In-browser remote control — requires RustDesk Server Pro (paid); evaluated and ruled out
- Installer code signing — see `subsystems/signing/` (not built yet)
