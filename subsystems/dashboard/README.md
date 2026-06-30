# Dashboard

FastAPI + Jinja2 web UI served by uvicorn behind nginx (TLS terminated by nginx, Let's Encrypt cert). Runs as a systemd service (`rustdesk-dashboard.service`) on the Lightsail box.

Live at `https://rds.pacificmit.com`.

## Stack

- **Runtime**: Python 3.12 / FastAPI 0.138 / uvicorn
- **Auth**: per-user bcrypt-hashed passwords stored in `users` table; session cookie (HTTPS-only, `SameSite=Lax`); session secret from env
- **DB**: `/opt/rustdesk-fleet/fleet.sqlite3` — same SQLite file as `setup_server.py`
- **Static assets**: `/app/static/style.css` — custom CSS, no framework (Inter font, indigo accent)
- **Reverse proxy**: nginx (`/etc/nginx/sites-enabled/default`) proxies `:443` → `127.0.0.1:8000`; static files served directly by nginx from the repo path

## Service file

`rustdesk-dashboard.service` — managed by systemd, `WorkingDirectory` is the repo checkout, `PYTHONPATH` set to `subsystems/single-tenant/` so `setup_server.py` and `generate_installer.py` are importable.

Env vars loaded from `/etc/rustdesk-fleet/dashboard.env`:
- `SESSION_SECRET` — random hex string, required in production
- `DASHBOARD_PASSWORD` — legacy, no longer consulted (auth now uses the `users` table)

## First-run setup

If the `users` table is empty, all routes redirect to `/setup`. The setup page creates the first admin account (email + bcrypt-hashed password). After that, `/setup` is permanently locked out.

## Routes

| Route | Description |
|---|---|
| `GET /` | Server status (host, ports, pubkey) — reads `server_config` |
| `GET /devices` | Flat device inventory — all devices, filterable by client group |
| `GET /groups` | Client group list with device counts |
| `POST /groups` | Create group — delegates to `setup_server.create_group()` |
| `GET /groups/{slug}` | Group detail: devices + installers |
| `POST /groups/{slug}/build` | Build Windows installer — delegates to `generate_installer.build_installer()` |
| `GET /download/{filename}` | Download installer (path-traversal safe, DB-gated) |
| `GET /audit` | Last 100 provisioning events |
| `GET /setup` | First-run admin account creation (locked after first user exists) |
| `GET /login` | Login form (email + password) |
| `POST /login` | bcrypt auth against `users` table |
| `GET /logout` | Clear session |

## What's built vs deferred

**Built and working:**
- Per-user authentication (bcrypt, first-run setup flow)
- Server status page
- Device inventory (`/devices`) with group filter
- Client group list + create
- Group detail with device list + installer list
- Installer download endpoint
- Audit log
- Flash messages, sidebar with active-nav highlighting

**Deferred (next session):**
- User/tech management UI (CRUD on `users` table, role + group access assignment) — schema exists, no UI yet
- Installer build flow end-to-end testing — route is wired to `generate_installer.build_installer()` but not verified working against makensis
- Visual design review pass with the user

**Out of scope (by design):**
- In-browser remote control — requires RustDesk Server Pro (paid); evaluated and ruled out
- One-click remote launch from dashboard
