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
