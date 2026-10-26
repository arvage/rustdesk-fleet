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
marks a provider action that belongs to no tenant: backups and restores,
code-signing settings, and anything the provider console does later.

Until there is a provider console (phase 1b), the provider's own controls
(backups, signing, host health) are on **tenant #1's** dashboard, which is
where they have always been. `tenancy.is_provider_home(tenant)` is true only
for tenant #1. Its Logs page also shows `NULL`-tenant events, so nothing
disappears for tenant #1. Other tenants never see provider events or
provider controls; those routes answer 404 for them.

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
  about our server, so only the provider home tenant sees and gets them.
  Each tenant still sees its own relay's containers and devices.

## Files on disk

Tenant files (installers, group icons, the email logo) live under
`tenancy.tenant_files_dir(tenant_id)`:

- tenant #1: the data directory itself (`/opt/rustdesk-fleet/installers`,
  `/opt/rustdesk-fleet/branding`), exactly where they were before tenancy;
- any other tenant: `/opt/rustdesk-fleet/tenants/<tenant id>/…`.

A relay's own files (keypair, hbbs database, compose file) are wherever its
`relays` row says (`data_dir`, `hbbs_db_path`, `compose_path`).

## Relay host vs. web domains

`relays.host` is what devices use: it is baked into installers as the ID and
relay server. `tenant_domains` are the web hostnames for the dashboard and
download links. They are separate on purpose: installers always use the
relay host, never a custom domain, so a tenant that changes or loses a domain
never strands devices. For tenant #1 both happen to be the same name today.

`tenant_domains.kind` is `legacy` (tenant #1's original host), `provider_subdomain`
(`<slug>.<our domain>`, phase 1b) or `custom` (a tenant's CNAME, phase 1c);
`verified_at` and `tls_status` are there for phase 1c's verification and
certificates.

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
- **Phase 1b:** set `api-server` to `https://<the tenant's relay host>` in
  every new tenant's installers, never to a custom domain, for the same
  reason the ID server is the relay host: a tenant changing domains must not
  strand devices. There will be no per-tenant port-21114 listener.

## How a device request finds its tenant

`tenancy.tenant_for_request()`:

1. The `Host` (lowercased, port stripped) is looked up in `tenant_domains`.
2. Device API paths only (`/api/heartbeat`, `/api/sysinfo`,
   `/api/sysinfo_ver`, `/api/audit/conn`, `/api/audit/file`): a host that is
   a tenant's relay host (`relays.host`) selects that tenant, since installers
   point devices there.
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
