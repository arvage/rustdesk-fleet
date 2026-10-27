# Tenancy

How the dashboard separates tenants (MSPs) inside one control database.
Background and the product plan: [`VISION.md`](../../VISION.md).

- **One control database** (`fleet.sqlite3`) for every tenant. Each
  tenant-owned row carries a `tenant_id`.
- **One relay per tenant.** Each tenant's `hbbs` database (its peer table)
  stays inside that tenant's relay stack and is never shared.
- **The tenant is chosen by the request's `Host`**, looked up in
  `tenant_domains`. Tenant #1 is the original single-tenant deployment.

## Table classification

**Tenant-owned** tables have a direct `tenant_id INTEGER NOT NULL REFERENCES
tenants(id)`, even where a parent row would imply it. That keeps every query's
filter visible in the SQL and lets `tests/test_sql_scoping.py` check for it.
There are no exceptions: child tables such as `webauthn_credentials` and
`user_group_access` carry `tenant_id` too, rather than relying on the parent.

| Table | Class | Ownership | Uniqueness after the migration |
|---|---|---|---|
| `tenants` | provider | — (one row per tenant) | `slug` |
| `relays` | provider | direct `tenant_id` (the relay *of* a tenant; provider-run) | `tenant_id` (one relay per tenant) |
| `tenant_domains` | provider | direct `tenant_id` | `hostname`; one `is_primary` per tenant |
| `server_config` | legacy, unused | — | Kept for one release in case of rollback; nothing reads it |
| `backup_config` | provider | — (singleton) | `id = 1` |
| `signing_config` | provider | — (singleton) | `id = 1` |
| `sqlite_sequence` | SQLite internal | — | — |
| `client_groups` | tenant | direct | `(tenant_id, slug)`; `(tenant_id, id)` for composite foreign keys |
| `devices` | tenant | direct (group via `(tenant_id, group_id)`) | `(tenant_id, rustdesk_id)` |
| `installers` | tenant | direct (group via `(tenant_id, group_id)`) | — |
| `download_links` | tenant | direct (group via `(tenant_id, group_id)`) | `token` (global: random, looked up together with `tenant_id`) |
| `users` | tenant | direct | `(tenant_id, email)`; `(tenant_id, id)` |
| `user_group_access` | tenant | direct (user and group via composite keys) | `(user_id, group_id)` |
| `webauthn_credentials` | tenant | direct (user via `(tenant_id, user_id)`) | `credential_id` (global: authenticator-generated) |
| `roles` | tenant | direct | `(tenant_id, key)` |
| `device_info` | tenant | direct | `(tenant_id, rustdesk_id)` |
| `device_sessions` | tenant | direct | — |
| `device_file_transfers` | tenant | direct | `(tenant_id, nonce)` |
| `connect_clicks` | tenant | direct | — |
| `rustdesk_peer_users` | tenant | direct | `(tenant_id, peer_id)` |
| `dynamic_groups` | tenant | direct | — |
| `notification_settings` | tenant | direct; one row per tenant | `tenant_id` (unique; replaces `id = 1`) |
| `notification_events` | tenant | direct | `(tenant_id, event_type)` |
| `notification_log` | tenant | direct | — |
| `email_templates` | tenant | direct | `(tenant_id, key)` |
| `email_design` | tenant | direct; one row per tenant | `tenant_id` (unique; replaces `id = 1`) |
| `provisioning_events` (audit log) | tenant, **nullable** | direct; `NULL` = a provider event | — |

Composite foreign keys such as `(tenant_id, group_id) REFERENCES
client_groups(tenant_id, id)` mean the database itself refuses a row that
points at another tenant's parent, even if the application gets it wrong.
`devices.group_id` and other nullable links are unaffected when `NULL`.

### Built-in defaults plus tenant rows

- **`roles`.** The built-in roles (`admin`, `tech`, `viewer`, defined in
  `permissions.SYSTEM_ROLES`) are copied into every tenant as ordinary rows
  with `is_system = 1`: by the migration for existing tenants, and by
  `tenancy.seed_tenant(conn, tenant_id)` when a tenant is created. Each
  tenant can then edit its own Technician and Viewer, and add custom roles,
  without touching anyone else's. `admin` always has every capability,
  whatever its row says. Role keys are unique per tenant only, so two
  tenants can both have a `support` role.
- **`email_templates`.** The defaults live in code (`email_templates.TEMPLATES`).
  A row exists only when a tenant has customised that email; reset deletes
  it. Lookups are `WHERE tenant_id = ? AND key = ?`, so a tenant with no row
  gets the code default.
- **`email_design`, `notification_settings`.** One optional row per tenant
  over defaults in code. No row means the defaults (notifications off).
- **`notification_events`.** A row per event a tenant has toggled; missing
  events default to on.

### Provider events in the audit log

`provisioning_events.tenant_id` is the only nullable `tenant_id`. `NULL`
marks a provider event that belongs to no tenant: backups and restores,
code-signing settings, update notices, console sign-ins.

Provider console actions are written with `source = 'provider'`, the
provider user's email, and `tenant_id` NULL or the tenant acted on
(`deps.provider_log`). The console's Audit log shows provider events and
provider actions; a tenant's own Logs page shows neither (its audit scope
is `tenant_id = ? AND source IS NOT 'provider'`).

The provider's controls (backups, signing, host health and its alerts, the
client version, relay restarts and upgrades) are in the provider console
(phase 1b). Until then they were on tenant #1's dashboard; tenant #1's
dashboard now shows only what any tenant sees.

## Provider-level, not per tenant (for now)

- **Code signing** (`signing_config`, `sign_installer.py`): one signing
  identity, run by us for every tenant's installers.
- **Backups** (`backup_config`, `backup.py`): run by us. A backup covers the
  control database and every relay's data directory (keypair and hbbs
  database).
- **OS lifecycle cache** (`os-lifecycle.json`): public data, shared.
- **The RustDesk client version** (`rustdesk_version.txt`,
  `installer-assets/`): one pinned version for every tenant's installers.
- **Host health** (CPU, memory, disk of the box) and its memory/disk alerts:
  about our server, so they are on the console's Host health page and go to
  the provider admins. Each tenant still sees its own relay's containers
  and devices.
- **The rustdesk-server version**: relays are upgraded from the console,
  one at a time.

## Provider console

`admin.<PROVIDER_DOMAIN>` is served by a separate app (`app/console/`), not
by tenant routes: `tenant_for_request()` returns `CONSOLE` for that host and
`TenantMiddleware` hands the request to the console app, which has its own
routes and session cookie (`rdf_provider_session`, signed with a different
key). No tenant route answers there and no console route answers on a
tenant host (`tests/test_console_separation.py`). Optional
`PROVIDER_ALLOWED_IPS` restricts the console by IP.

Provider admins (`provider_users`) are separate from every tenant's users and
sign in with passkeys only (`provider_webauthn_credentials`, RP ID = the
console host), enrolled through one-time links valid for 1 hour
(`provider_enrollments`, from `scripts/provider_admin.py` or a console
invite). Sessions are checked against the database on every request
(`session_version`, `disabled_at`) and last `PROVIDER_SESSION_MAX_AGE`
(8 hours). Support access to a tenant's dashboard (with the tenant's
consent) is phase 1c.

Long operations (tenant creation, relay start/stop/upgrade, cancel,
reactivate, purge) are console jobs (`provider_jobs`), run one at a time by
a worker thread with live progress; a job interrupted by a restart is undone
where needed and marked failed. The only root access is `rdf-helper`
(DEPLOYMENT.md §11).

## Files on disk

Tenant files (installers, group icons, the email logo) live under
`tenancy.tenant_files_dir(tenant_id)`:

- tenant #1: the data directory itself (`/opt/rustdesk-fleet/installers`,
  `/opt/rustdesk-fleet/branding`), exactly where they were before tenancy;
- any other tenant: `/opt/rustdesk-fleet/tenants/<tenant id>/…`.

A relay's own files (keypair, hbbs database, compose file) are wherever its
`relays` row says (`data_dir`, `hbbs_db_path`, `compose_path`).

## Relay host vs. web domains

`relays.host` is what devices use for the raw hbbs/hbbr ports: it is baked
into installers as the ID and relay server. For tenants created in the
console it is `<slug>.relay.<PROVIDER_DOMAIN>` and needs no certificate.
`tenant_domains` are the web hostnames, with certificates: the dashboard,
download links and the device API (`api-server`). For console-made tenants
the primary one is `<slug>.<PROVIDER_DOMAIN>` (kind `provider_subdomain`).

They are separate on purpose. A relay can move to another box by changing
one DNS record (`<slug>.relay…`) without touching the web side, and
installers never name a custom domain, so a tenant that changes or loses one
never strands devices. Tenant #1 keeps its single name for both (kind
`legacy`) and gets no relay subdomain.

`tenant_domains.kind` `custom` (a tenant's CNAME) is phase 1d;
`verified_at`, `tls_status` and `cert_expires_at` are already there for it.

## How the RustDesk client finds its API server

Read from the RustDesk client source (`rustdesk/rustdesk` master at
`59f2953`, 2026-10-26: `src/common.rs` `get_api_server()` /
`get_api_server_()` / `get_audit_server()`, `src/hbbs_http/sync.rs`
`heartbeat_url()`, `src/server/connection.rs`; port helpers in
`hbb_common/src/socket_client.rs`). Phase 1b will set `api-server`
explicitly in new tenants' installers based on this.

The client computes one API base URL from two options, `api-server` and
`custom-rendezvous-server` (the ID server), and builds every reporting URL
from it:

| Report | URL |
|---|---|
| heartbeat | `<api>/api/heartbeat` |
| sysinfo | `<api>/api/sysinfo` (the heartbeat URL with `heartbeat` → `sysinfo`) |
| sysinfo_ver | `<api>/api/sysinfo_ver` (only when the API is a `*.rustdesk.com` host) |
| connection audit | `<api>/api/audit/conn` |
| file audit | `<api>/api/audit/file` |

How `<api>` is chosen, in order:

1. **Windows only:** a licence encoded in the installer's file name
   (`get_license_from_exe_name()`) wins over everything: its `api` field if
   set, and its `host` as the ID server. Our installers aren't named that
   way, so this never applies to them.
2. **`api-server` is set:** it is used as is, minus one trailing `/`. One
   exception: if it starts with `https` and ends in `:21114`, the `:21114` is
   dropped (HTTPS on 21114 is assumed to be a mistake) unless the build
   option `allow-https-21114` is `Y`.
3. **`api-server` is empty:** it is derived from the ID server
   (`custom-rendezvous-server`, else the build's built-in server):
   - with an explicit port, `host:PORT` becomes `http://host:(PORT-2)`
     (`increase_port(host, -2)`): `rds.example.com:21116` →
     `http://rds.example.com:21114`;
   - without a port, `http://host:21114` (`RENDEZVOUS_PORT` 21116 − 2); an
     IPv6 literal is bracketed.

   A derived URL is always plain `http`.
4. **Neither is set:** `https://admin.rustdesk.com`. Because that is a
   `rustdesk.com` host, `heartbeat_url()` returns empty and the client
   doesn't report at all. The same happens when the build option
   `register-device` is off (`Config::no_register_device()`).

Reports are HTTP POSTs to that URL, so the `Host` header the dashboard sees is
the URL's `host[:port]`: the relay host for a derived URL (or the IP, if the
ID server was entered as an IP), whatever `api-server` names otherwise. Newer
clients retry a failed or 5xx request over a raw TCP proxy through the ID
server (`with_tcp_proxy_fallback`) when the target is the configured API
host; the community `hbbs` doesn't serve that, so for us it just fails.

What this means here:

- Tenant #1's **older installers** set no `api-server`, so those devices post
  to `http://<relay host>:21114`: the legacy port-21114 nginx vhost.
- Tenant #1's **current installers** set `api-server = "https://<host>"`
  (`installer.nsi.tmpl`, the macOS and Linux templates) and report over 443
  with that `Host`.
- **Tenants created in the provider console (phase 1b)** get installers with
  the ID server `<slug>.relay.<PROVIDER_DOMAIN>:<hbbs port>`, the relay
  `<slug>.relay.<PROVIDER_DOMAIN>:<hbbr port>`, their own key and
  `api-server = "https://<slug>.<PROVIDER_DOMAIN>"`: the provider web domain,
  which has a certificate, and never a custom domain (phase 1d), so a tenant
  changing or losing a custom domain never strands devices. (This replaces
  the earlier plan of pointing `api-server` at the relay host, which has no
  certificate.) There is no per-tenant port-21114 listener.

## How a device request finds its tenant

`tenancy.tenant_for_request()`:

1. The `Host` (lowercased, port stripped) is looked up in `tenant_domains`.
2. Device API paths only (`/api/heartbeat`, `/api/sysinfo`,
   `/api/sysinfo_ver`, `/api/audit/conn`, `/api/audit/file`): a host that is
   a tenant's relay host (`relays.host`) selects that tenant: a fallback for
   clients configured by hand with the relay host as their API server
   (installers use the web domain).
3. Device API paths only: if nginx tagged the request `X-Fleet-Listener:
   legacy-api` (set on the port-21114 vhost, stripped on every vhost), an
   unknown host means tenant #1.
4. Anything else is an unknown host: a 404 page, or for the device API the
   same reply as an unknown device (`{}`, `REJECTED`, 400), because a 404
   means nothing to the client.

The report is then verified against **that tenant's** hbbs peer table
(`relays.hbbs_db_path`): the id must be registered with that tenant's relay
and the uuid must match. A device of tenant A reporting on tenant B's host is
rejected exactly like an unknown id. Each relay has its own peer cache.

Suspended and cancelled tenants: their devices' reports are still accepted
and recorded (it costs nothing and keeps the history intact), but the
heartbeat reply never pushes config to them.

## Port layout

Read from `rustdesk-server` 1.1.16 (`src/main.rs`, `src/hbbr.rs`,
`src/rendezvous_server.rs` `RendezvousServer::start`, `src/relay_server.rs`
`start`) and the RustDesk client (`src/common.rs` `test_nat_type_`,
`src/rendezvous_mediator.rs` `get_relay_server`, `src/client.rs`
`create_relay`, `hbb_common/src/websocket.rs` `check_ws`; defaults in
`hbb_common/src/config.rs`: `RENDEZVOUS_PORT = 21116`, `RELAY_PORT = 21117`).

**What each process listens on**, for `hbbs -p P` (default `P = 21116`) and
`hbbr -p R` (default `R = 21117`):

| Port | Proto | Process | Purpose |
|---|---|---|---|
| `P − 1` | TCP | hbbs | NAT-type test (and online-status queries) |
| `P` | TCP + UDP | hbbs | rendezvous: registration, heartbeats (UDP), punch-hole, ID lookups |
| `P + 2` | TCP | hbbs | websocket rendezvous (web client / websocket mode) |
| `R` | TCP | hbbr | relay |
| `R + 2` | TCP | hbbr | websocket relay |

With the defaults that is TCP 21115–21119 and UDP 21116: the familiar range.

**What a client contacts**, given `custom-rendezvous-server = host[:P]` and
`relay-server = host[:R]` (a missing port means the default):

- `host:P` (TCP and UDP) for everything rendezvous;
- `host:(P − 1)` for the NAT test (`increase_port(server, -1)`);
- the relay: its own `relay-server` option, else the relay hbbs hands out
  (`hbbs -r host:R`), else `rendezvous host:(P + 1)` (`increase_port(host, 1)`);
  then `check_port(relay, 21117)`, so a relay given without a port means 21117;
- in websocket mode: `P + 2` for rendezvous and `R + 2` for the relay
  (`check_ws`), and `P + 2` for online queries (`(P − 1) + 3`).
- reports go to the API server over HTTPS (see above), never to these ports.

**Key enforcement.** `hbbs` with no `-k` (or `-k _`/`-k -`) loads or creates
the keypair in its working directory and *enforces* it: clients with another
key are refused. `hbbr` with no `-k` enforces nothing (any client may use the
relay); `-k _` would make it check the same key. Tenant #1's compose runs
plain `hbbs` / `hbbr`, so new relays do the same; enforcing the key on hbbr
too (`-k _`) is a later change.

**Port blocks.** Each relay gets a block of 10 ports starting at `B`
(`RELAY_PORT_BASE` = 21200, then 21210, … up to `RELAY_PORT_MAX` = 21999,
so 80 tenants), laid out exactly like the defaults:

| Port | Use |
|---|---|
| `B` | NAT test (`P − 1`) |
| `B + 1` | hbbs `-p`: rendezvous `P`, TCP + UDP |
| `B + 2` | hbbr `-p`: relay `R` (= `P + 1`, so even the client's last-resort relay guess is right) |
| `B + 3` | websocket rendezvous (`P + 2`) |
| `B + 4` | websocket relay (`R + 2`) |
| `B + 5`–`B + 9` | unused, left for future needs |

`relays.hbbs_port` is `P` and `relays.hbbr_port` is `R`. (Tenant #1's row
said 21115 for hbbs before phase 1b: the NAT-test port, not the one clients
register on. The phase 1b migration corrects it to 21116; tenant #1's relay
and installers don't change.) Blocks are recorded in `relay_port_blocks` and
**never reused**, even after a tenant is purged: old installers may still be
out there knocking on those ports. Tenant #1 holds the default block
(21115–21119).

The relays run with `network_mode: host`, like tenant #1's, binding their
ports directly (no Docker port mapping, which matters for UDP). The cloud
firewall must allow TCP and UDP on the whole `RELAY_PORT_BASE`–`RELAY_PORT_MAX`
range once, up front; the console shows that range.

## Lifecycle

| Status | Dashboard and download links | Relay | Device reports |
|---|---|---|---|
| `trial`, `active` | open | running | accepted, config pushed |
| `suspended` | "not active" page | **keeps running** (nothing to re-register on reactivation) | accepted, no config pushed |
| `cancelled` | sign-in and a read-only **export page** (CSVs of devices, groups, users, sessions, file transfers) for the tenant's admins; everything else "not active" | **stopped** (`docker compose stop`: containers, keys and data kept) | accepted if they arrive, no config pushed |
| `purged` | gone (its domains are removed: 404) | removed | rejected (unknown host) |

- **Suspend** (console): immediate. For non-payment and the like.
- **Reactivate**: from suspended, or from cancelled while `purge_after` is in
  the future. From cancelled the relay is started again with the same keys
  and ports, so devices reconnect on their own.
- **Cancel**: records `cancelled_at` and `purge_after = cancelled_at + 30
  days` and emails the tenant's admins. The console shows the countdown and
  can extend it.
- **Purge**: the hourly maintenance pass emails the admins 7 days before
  `purge_after` and queues the purge when it passes; "purge now" (typing the
  slug) does it at once. Purge removes the relay containers, compose file,
  keypair and hbbs data (the root-owned part through `rdf-helper
  purge-relay-data`), the tenant's files (installers, guides, branding), its
  vhosts and certificates (revoked), its `relays` and `tenant_domains` rows,
  and every tenant-owned row. It keeps the `tenants` row (status `purged`,
  slug reserved for good), the port block in `relay_port_blocks` (never
  reused), `tenant_usage_daily` and the provider's audit entries about the
  tenant. A purge that stops half-way can be run again.
- **Tenant #1** can't be cancelled or purged.

`tenant_usage_daily` (devices, technicians, session minutes per tenant per
day, written by the hourly maintenance pass) is provider-level and outlives
both deletions and purges; billing will read it later. `tenants.plan` is free
text for now.
