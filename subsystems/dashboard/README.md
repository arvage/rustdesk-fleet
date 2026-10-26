# Dashboard

FastAPI + Jinja2 web UI served by uvicorn behind nginx (TLS terminated by nginx, Let's Encrypt cert). Runs as a systemd service (`rustdesk-dashboard.service`) on the Lightsail box.

Live at `https://rds.example.com`.

## Stack

- **Runtime**: Python 3.12 / FastAPI 0.138 / uvicorn
- **Auth**: per-user bcrypt-hashed passwords stored in `users` table; session cookie (HTTPS-only, `SameSite=Lax`); session secret from env
- **DB**: `/opt/rustdesk-fleet/fleet.sqlite3` — same SQLite file as `setup_server.py`
- **Static assets**: `app/static/style.css` — custom CSS, no framework (Inter font, indigo accent). Colors are CSS variables with a light set on `:root` and a dark set on `:root[data-theme="dark"]` (see *Themes* below), plus the `--viz-*`/`--status-*` data-viz palette used by the reports
- **Reverse proxy**: nginx (`/etc/nginx/sites-available/rds.example.com`) proxies `:443` → `127.0.0.1:8000`, plus a `:21114` block that exposes only the client-reporting endpoints (see `DEPLOYMENT.md`)

## Service file

`rustdesk-dashboard.service` — managed by systemd, `WorkingDirectory` is the repo checkout, `PYTHONPATH` set to `subsystems/single-tenant/` so `setup_server.py` and `generate_installer.py` are importable.

Env vars loaded from `/etc/rustdesk-fleet/dashboard.env`:
- `SESSION_SECRET` — random hex string, at least 32 characters; the app won't start without it
- `SETUP_TOKEN` — unlocks `/setup` while no users exist (written by `install.sh`)
- `SESSION_MAX_AGE` — session lifetime in seconds, default 12 hours
- `FLEET_DATA_DIR` — data directory, default `/opt/rustdesk-fleet` (see `app/paths.py`)
- `DASHBOARD_PASSWORD` — legacy, no longer consulted (auth now uses the `users` table)

## First-run setup

If the `users` table is empty, all routes redirect to `/setup`. The setup page creates the first admin account (email + bcrypt-hashed password) and only accepts the form with the right `SETUP_TOKEN` (`/setup?token=…` pre-fills it). With no token configured it accepts nothing. After the first admin exists, `/setup` is permanently locked out and the token is ignored.

## Security

- **Sessions.** The cookie only holds the user id and a `session_version`.
  `require_auth()` loads the user from the database on every request, so a
  deleted user is signed out and a role change applies on the next request.
  Bumping `users.session_version` ends all of a user's sessions: it happens
  on a role change, a password reset or change, a sign-in method change,
  **Sign out everywhere** (My Account) and **Sign out** (Users page). A
  self-service password or sign-in method change keeps the current session.
  Sessions expire after `SESSION_MAX_AGE` (12 h).
- **Sign-in limits** (`app/ratelimit.py`). 5 failures for one email in
  15 minutes lock it for 15 minutes (unknown emails too, so lockouts don't
  reveal accounts); 20 failures from one IP lock the IP. Covers password and
  passkey sign-in and `/setup`. Locked requests get a 429 before any bcrypt
  work, and each lockout is logged as `login_locked`. The counters are in
  memory: they reset on restart and need shared storage if more than one
  uvicorn worker is ever used. nginx adds a per-IP request limit on the same
  paths (DEPLOYMENT.md, Section 4).
- **Sign out** is a POST. `GET /logout` only redirects to `/`.
- **Device API** (`/api/heartbeat` etc.) is unauthenticated by design but
  only accepts reports whose id and uuid match the hbbs peer table. Unknown
  ids re-read that table at most every 5 seconds.
- **Backups** pass the gpg passphrase on stdin, never on the command line.
- **Not yet:** CSRF tokens (`SameSite=Lax` is the current protection).

Tests: `python3 -m pip install -r requirements-dev.txt`, then
`python3 -m pytest subsystems/dashboard/tests` from the repo root.

## Routes

Staff pages (all require login):

| Route | Description |
|---|---|
| `GET /` | Signed out: the public landing page (`site/index.html` at the repo root). Signed in: Server Status: a **Server Health** section (live CPU/memory/disk/uptime, hbbs/hbbr containers, devices online — polled from `/api/server/health`, with alert-threshold markers), a **Server Info** section (status, host, ports, server/client versions with update banners, public key), and a **Backups** section (last-run status, Back up now, download latest) |
| `POST /server/update` | Update hbbs/hbbr to the latest release (`update_server`) |
| `POST /installers/update-version` | Move installers to the latest RustDesk client release, optionally rebuilding every group's installers in the background (`update_server`) |
| `POST /server/backup`, `GET /server/backup/download/latest` | Run a backup now / download the newest archive (`manage_backups`) |
| `GET /devices` | Device inventory sorted by label (unlabeled last): Computer / OS / client version (client-reported), live online · in session · offline status, group filter; **Recently registered** panel (last 7 days, no client group yet); **ⓘ details** modal per device (full reported details + recent sessions) |
| `POST /devices/{id}/edit` · `/delete`, `POST /devices/sync` | Label/group a device (`manage_devices`); delete hides it and clears its details/sessions/file transfers — a verified heartbeat later auto-un-hides it; import peers from hbbs. Edit and delete accept a `next` local path to return to (the group page uses it) |
| `POST /devices/{id}/assign-group` | Set a device's client group and, if a `label` field is sent, its label (Recently registered panel; `manage_devices`) |
| `GET /groups`, `POST /groups` | Group list (with live "X / Y online"), create group |
| `GET /groups/{slug}` | Group detail: devices (sorted by label, with the same **Edit** modal as Devices), Client Settings, branding, installers, download links |
| `POST /groups/{slug}/edit` | Rename / change slug / unattended password |
| `POST /groups/{slug}/client-settings` | Per-group managed client settings (auto-update, remote settings changes) |
| `POST /groups/{slug}/build`, `POST /groups/{slug}/installers/{id}/delete` | Build / delete installers |
| `POST /groups/{slug}/installers/{id}/sign` | Sign / re-sign a Windows installer with Azure Trusted Signing (`manage_groups`) |
| `POST /groups/{slug}/branding`, `GET /groups/{slug}/branding/icon.png` | Per-group installer app name + icon (`manage_groups`) |
| `POST /groups/{slug}/links`, `/links/{id}/email`, `/links/{id}/revoke` | Create, email, revoke client download links |
| `GET /download/{filename}` | Staff installer download (path-traversal safe, DB-gated) |
| `GET /users` + `POST /users/...` | User management: assign roles, group access, password reset, passkeys, MFA reminders (`manage_users`) |
| `GET /admin` | Admin landing: tool cards + fleet stats (any admin-area capability) |
| `GET /admin/signing` + `POST /admin/signing/save` · `/test` | Installer signing config (Azure Trusted Signing) and connection test |
| `GET /admin/reports`, `GET /admin/reports/{key}`, `GET /admin/reports/{key}/{section}.csv` | Reports catalogue, one report (`?group=<slug>\|__none__&days=7\|30\|90\|180\|365`), and any of its tables as CSV (any admin-area capability) |
| `GET /admin/emails`, `GET/POST /admin/emails/{key}`, `GET/POST /admin/emails/design`, `GET /admin/emails/logo.png` | Email templates: list, per-email editor (save / reset), shared design + logo upload (`manage_notifications`) |
| `POST /admin/emails/preview`, `POST /admin/emails/{key}/test` | Render an unsaved draft with sample data (JSON → HTML); send the saved version to the signed-in user |
| `GET /dynamic-groups`, `GET /dynamic-groups/{id}`, `GET /dynamic-groups/{id}/export.csv` | Dynamic (rule-based) groups: list with live counts, members, CSV (any signed-in user) |
| `GET /dynamic-groups/new`, `GET /dynamic-groups/{id}/edit`, `POST /dynamic-groups/save` · `/{id}/delete`, `POST /dynamic-groups/preview` | Rule builder, save/delete, live match preview (`manage_devices`) |
| `GET /admin/backup` + `POST /admin/backup/save` · `/test` · `/run` · `/restore`, `GET /admin/backup/download` | Backup destination config (S3/Wasabi/B2/Spaces/MinIO), test, run, restore, per-archive download (`manage_backups`) |
| `GET /admin/roles` + `POST /admin/roles...` | Roles & permissions: create/edit/delete permission levels via a capability matrix (`manage_roles`) |
| `GET /admin/export/devices.csv` | Device inventory CSV (`manage_devices`) |
| `POST /admin/restart-relay`, `POST /admin/logs/prune` | Restart hbbs/hbbr; prune old audit events (`manage_system`) |
| `GET /account` + `POST /account/...` | Own profile, password, passkeys, sign-in method |
| `GET /notifications` + `POST /notifications/...` | SMTP settings, server-health alert thresholds, test email (`manage_notifications`) |
| `POST /notifications/events/{event_type}` | Auto-save a single event-trigger toggle (form field `enabled=1`/`0`); returns JSON (`manage_notifications`) |
| `GET /audit` | **Logs** — categorized tabs (All / Clients / Remote sessions / Installers / Groups / Users & auth / Notifications / Email deliveries / Server / Other), per-tab filter; All activity (default) merges `provisioning_events` with `device_sessions`, `device_file_transfers` and (with `manage_notifications`) `notification_log`, newest 200. All activity and Clients hide `device_online`/`device_offline` unless `?onoff=1` (the "Hide online/offline" switch); Remote sessions reads `device_sessions`, Email deliveries reads `notification_log` (tab shown only with `manage_notifications`) |
| `GET /devices/{id}/timeline?range=24h\|7d\|30d&back=N` | Per-device **connection timeline** (ScreenConnect-style): online lane from `device_online`/`device_offline` events, one lane per connecting peer from `device_sessions` with file-transfer markers from `device_file_transfers`, device-event markers, summary and event list (`app/routes/timeline.py`) |
| `GET /timeline?view=device\|user&range=24h\|7d\|30d` or `&from=YYYY-MM-DD&to=YYYY-MM-DD` | Fleet-wide **timeline**: every device's `device_sessions` in the window, one lane per device or per person, `group`/`user` filters, summary and session list (`app/routes/timeline.py`, `templates/timeline.html`) |
| `POST /api/devices/{id}/connect` | Records a Connect/Terminal click (`connect_clicks`); sent with `navigator.sendBeacon` from `base.html` on any `rustdesk://` link |
| `GET /sessions` | Redirects to `/audit?cat=sessions` (kept for old links) |
| `GET /api/devices`, `GET /api/devices/status`, `GET /api/devices/{id}/sessions`, `GET /api/server/health` | JSON for the live tables, per-device session history, and server-health tiles |
| `GET/POST /setup`, `GET/POST /login`, `/login/webauthn/...`, `POST /logout` | First-run setup (needs `SETUP_TOKEN`), password and passkey sign-in, sign out (`GET /logout` only redirects to `/`) |
| `POST /account/sign-out-everywhere`, `POST /users/{id}/sign-out` | End all of your own sessions / another user's (`manage_users`) |

Public, no login:

| Route | Description |
|---|---|
| `GET /d/{token}` | Client-facing download page for a shared link (noindex, no-referrer) |
| `GET /d/{token}/{platform}` | The installer itself; counts toward the link's download limit |
| `GET /guides/{filename}` | Client PDF guides from `client-docs/` (install, getting help), linked from the download page; only the listed files are served |
| `POST /api/sysinfo`, `/api/heartbeat`, `/api/sysinfo_ver`, `/api/audit/conn`, `/api/audit/file` | RustDesk client reporting (audit/conn = per-connection open/authorized/close reports; audit/file = file transfers and clipboard file pastes). Accepted only when id + uuid match the hbbs peer table; the audit endpoints answer 400 otherwise. Also served on port 21114 (see `DEPLOYMENT.md` §7) |

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
  sessions" tab and per-device in the details modal. The client's
  `/api/audit/conn` reports, matched by connection id (`conn_ref`), add who
  connected (`peer_id`, `peer_name` — the controller's RustDesk id and
  reported name), the connecting IP (`peer_ip`) and the connection type
  (`conn_type`: 0 remote, 1 file transfer, 2 port forward, 3 view camera,
  4 terminal). Audit retries within 5 minutes are matched to the same row.
- **File transfer audit**: `/api/audit/file` reports are stored in
  `device_file_transfers` (direction 0 = sent from the device, 1 = sent to
  it; folder path, empty for clipboard pastes; full file count; largest ≤10
  `[name, size]` pairs), linked to the session by connection id and
  deduplicated by the client's `nonce`. Shown on the connection timeline;
  deleting a device clears them with its sessions.
- **Backups** (`subsystems/single-tenant/backup.py`): nightly systemd timer
  (as root) + on-demand, snapshotting the hbbs/hbbr keypair, hbbs peer DB and
  the dashboard DB (consistent SQLite online-backup copies), with rotation,
  optional gpg encryption, restore, and off-site upload to S3-compatible
  storage (AWS S3 / Wasabi / Backblaze B2 / DigitalOcean Spaces / MinIO)
  configured entirely in the GUI via `backup_remote.py`.
- **Installer signing**: Windows installers are Authenticode-signed with Azure
  Trusted Signing via `jsign` on this box — automatically after a build, or
  per installer. See `subsystems/signing/README.md` and `DEPLOYMENT.md` §10.
- **Notifications** (email): device registered / came online / went offline /
  deleted, installer built / deleted / downloaded via link, user created /
  deleted, new server version, new client version, and **server memory / disk
  above a configurable threshold** (debounced, with hysteresis). Device
  registration is detected by the background watcher (below), not by a
  dashboard action, so it fires before the device has a group.
- **Email templates** (`app/email_templates.py`): every email — the 13 alerts,
  the new-user welcome, the passkey reminder and the client download-link
  email — renders from one catalogue of defaults (subject, heading, intro,
  closing, details table, button) overridden per email from the
  `email_templates` table, inside a shared design from the `email_design`
  singleton (brand names, colors, font, footers). Text fields are plain text
  with `{placeholders}`, `**bold**`, auto-linked URLs. A paragraph whose
  placeholders are all empty is dropped. The logo is stored as
  `/opt/rustdesk-fleet/branding/email-logo.png` (uploads normalized to PNG,
  ≤600 px wide) and attached inline (`multipart/related`, `cid:`), so it shows
  without remote-image loading. The layout is table-based with inline styles.
- **Dynamic groups** (`app/dynamic_groups.py`): saved rule sets (`dynamic_groups`
  table: name, match all/any, JSON rules `[{field, op, value}]`) evaluated on
  view against `fleet.fleet_snapshot()` — membership is never stored. The
  `FIELDS`/`OPS` catalogue drives both validation (`normalize_rules`) and the
  JS rule builder. `PRESETS` are the starting templates.
- **Reports** (`app/reports.py`): 12 reports, each a function of a
  `ReportContext` (group filter, period) returning stat tiles + table
  sections. One template renders them all, and any section exports as CSV.
  Sections can carry a `chart` (`share` stacked bar or `columns` time series),
  `row_colors` and `tone_cols`. Categorical colors are assigned by fleet-wide
  rank (stable under filtering; >7 fold into gray "Other"). Status values map
  to tones via `STATUS_TONES` and render as icon + label badges.
- **OS support** (`app/os_lifecycle.py`, `app/fleet.py`): Windows, Windows
  Server and macOS end-of-support dates are downloaded from
  [endoflife.date](https://endoflife.date) (`/api/windows.json`,
  `/api/windows-server.json`, `/api/macos.json`) at most once a day by the update watcher and
  cached in `/opt/rustdesk-fleet/os-lifecycle.json`, so **new releases get
  their dates automatically**. Devices are matched by build and edition:
  LTSC/IoT → LTSC record, Enterprise/Education → "(E)", Home/Pro/Pro
  Education → "(W)". Servers are matched by year, or by build for releases
  without one. Macs are matched by major version (`MacOS 26.6.2` → `26`,
  `10.15.x` → `10.15`). Apple publishes no end dates, so releases still getting
  security updates have `eol: false` (supported, no date) and older ones carry
  the date they stopped. If a download fails, the last cached copy stays in
  use. With no cache or no match, the built-in Windows table in `fleet.py` is
  the fallback (no macOS fallback → "unknown"). If the cache is missing a
  product (e.g. after one is added), it is re-downloaded on the next check.
  "Ending soon" = within 182 days. Linux is "unknown". The OS support
  report shows the source and last update.
- **Themes**: Light / Dark / Auto switch in the sidebar footer, stored in
  `localStorage['rdf-theme']`. An inline head script sets
  `<html data-theme>` before first paint (no white flash) and follows the OS
  setting in Auto. Templates use CSS variables, not hex, for anything that
  should change with the theme. Light values equal the original colors. The
  always-dark sidebar and user modals and the public `/d/` page keep fixed
  colors.
- **Logs**: categorized audit log (clients, remote sessions, installers,
  groups, users & auth, notifications, email deliveries, server) of
  provisioning and admin actions.
- **Notifications page**: event-trigger toggles auto-save individually via
  `fetch`; the delivery log lives on Logs → Email deliveries.

Heartbeat/sysinfo requests are excluded from the uvicorn access log
(`app/main.py`) — devices post every few seconds.

## Code layout

| Path | What |
|---|---|
| `app/main.py` | App, session middleware, router registration, migrations on startup |
| `app/deps.py` | DB access, hbbs peer reader, `get_devices()` (fleet + peers + client-reported details, label order), `run_migrations()` (idempotent `ALTER`/`CREATE IF NOT EXISTS`) |
| `app/fleet.py` | `fleet_snapshot()`: devices plus derived facts (RAM/cores as numbers, OS support, live status, days since seen/registered/last session) — shared by dynamic groups and reports |
| `app/dynamic_groups.py`, `app/reports.py`, `app/email_templates.py` | Rule engine, report catalogue, email catalogue + renderer (see Features) |
| `app/notifications.py` | SMTP send (with inline logo), event registry (`EVENT_LABELS`), per-event toggles, delivery log, `fire_notification()` (non-blocking) |
| `app/permissions.py` | Capability catalogue, roles, `require_perm()` |
| `app/routes/*.py` | One module per area. `api.py` also hosts the background watchers |
| `app/templates/*.html`, `app/static/style.css` | Jinja templates (all extend `base.html` except login/setup/public pages) and the stylesheet |

## Background jobs

Daemon threads started at import (one uvicorn worker, so each runs once):

| Thread | Interval | Does |
|---|---|---|
| `hbbs-ss-poll` (`routes/api.py`) | 1 s | Samples live TCP connections to hbbs ports (an online signal) |
| `device-offline-watch` (`routes/api.py`) | 30 s | **New devices:** any hbbs peer without a `devices` row gets one (unassigned) and fires `device_registered` — the row marks it announced. **Status changes:** online→offline fires `device_offline`, offline→online fires `device_online`. The first pass after start only records a baseline. Also closes sessions for devices that vanished mid-session |
| `health-alert-watch` (`routes/api.py`) | 60 s | Memory/disk thresholds → `memory_high` / `disk_high` (debounced, hysteresis) |
| `server-update-watch` (`routes/home.py`) | 1 h (first 60 s after start) | New RustDesk server/client releases → `*_update_available`, once per version. Also refreshes the Windows end-of-support data (downloads at most daily) |

## Database tables added by the dashboard

Created by `run_migrations()` in `/opt/rustdesk-fleet/fleet.sqlite3`
alongside the single-tenant schema: `notification_settings`,
`notification_events`, `notification_log`, `device_sessions`, `device_file_transfers`, `device_info`,
`download_links`, `backup_config`, `signing_config`, `roles`,
`webauthn_credentials`, `dynamic_groups`, `email_templates`, `email_design`,
`connect_clicks`, `rustdesk_peer_users`.

**Who connected.** RustDesk's connection audit only names the controlling
computer (`peer_id`, `peer_name`). `DASHBOARD_USER_SQL` in
`app/routes/sessions.py` credits a session to a dashboard user: the latest
`connect_clicks` row for that device from 3 min before to 1 min after it
started, else the `rustdesk_peer_users` link for its `peer_id`. Links have
no UI yet; add one with
`INSERT INTO rustdesk_peer_users (peer_id, user_email) VALUES ('<rustdesk id>', '<user email>')`.

## Deploying changes

The service runs straight from the repo checkout. After pulling or editing:

- **Templates and `static/`** are read live — changes show on the next page
  load (bump the `style.css?v=N` query in `base.html`, `login.html`,
  `login_webauthn.html` and `setup.html` so browsers refetch the CSS).
- **Python changes** need `sudo systemctl restart rustdesk-dashboard`. New
  tables/columns are created on startup by `run_migrations()`.
- Check: `systemctl is-active rustdesk-dashboard` and
  `journalctl -u rustdesk-dashboard -n 50`.

## Out of scope (by design)

- In-browser remote control — requires RustDesk Server Pro (paid); evaluated and ruled out
