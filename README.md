# RustDesk Fleet

Self-hosted RustDesk infrastructure replacing ScreenConnect for
GoVirtual365 (and client-facing use).

**This was previously pushed to GitHub as a private repo
(`arvage/rustdesk-fleet`) but that's no longer the source of truth — this
zip, going forward, is.** No `.git` history is included.

## Current architecture: single-tenant

One `hbbs`/`hbbr` pair for the entire fleet, one shared keypair, one
device table. Client separation (GoVirtual365-internal, individual
external clients, etc.) is a **label** — the `client_groups` table — not
separate infrastructure. See **[`subsystems/single-tenant/README.md`](subsystems/single-tenant/README.md)**
for the live, current system.

### Why single-tenant, after originally building per-tenant isolation

The project initially used one isolated `hbbs`/`hbbr` stack **per client**
specifically for security: a leaked installer or compromised tech account
for one client couldn't reach another client's devices, since each had
its own server key. That was deliberately built, tested, and deployed —
see `subsystems/provisioning/` (now superseded, kept as reference).

It was abandoned because the native RustDesk desktop client can only hold
**one** server/key configuration at a time — confirmed via RustDesk's own
GitHub discussions as a deliberate upstream design choice, repeatedly
requested by other MSP-style users and explicitly rejected by the
maintainers. For a helpdesk team supporting multiple clients day to day,
that meant manually re-entering the ID server and key every time a tech
switched clients — judged impractical enough to outweigh the isolation
benefit. The tradeoff was made deliberately, with the security cost
understood: see `subsystems/single-tenant/README.md`'s "Security note"
section.

(A browser-based web client was briefly considered as a way to keep
isolation *and* avoid the key-switching problem, since a web session
could be handed per-tenant config without touching client-side
settings — but RustDesk's web client turned out to require RustDesk
Server Pro, a paid per-server license, which doesn't actually preserve
isolation without buying one license per client. That ruled it out.)

## Subsystems

| Subsystem | Status | What it does |
|---|---|---|
| [`subsystems/single-tenant`](subsystems/single-tenant) | **Current, built & locally tested** | The one shared `hbbs`/`hbbr` server, plus `client_groups`/`devices`/`users` schema for app-layer separation |
| [`subsystems/provisioning`](subsystems/provisioning) | Superseded, kept as reference | Original per-tenant isolated-stack allocator — real working code, was deployed and verified live on the Lightsail box before the architecture changed |
| [`subsystems/signing`](subsystems/signing) | Not started | Sign installers via Azure Trusted Signing (needs a Windows agent — GitHub Actions `windows-latest` — since Authenticode has no Linux path) |
| [`subsystems/dashboard`](subsystems/dashboard) | Not started | Management API + UI: device inventory filtered by `client_groups`, RBAC via `users`/`user_group_access`, audit log, installer-generation trigger |
| [`subsystems/viewer`](subsystems/viewer) | Ruled out | Browser session viewer — RustDesk's web client requires a paid Pro license per server; not pursuing |

## Shared

[`shared/db/schema.sql`](shared/db/schema.sql) — schema from the
**superseded** multi-tenant phase (`tenants`, `installers`,
`provisioning_events`). The single-tenant phase has its own schema at
`subsystems/single-tenant/schema.sql` (`server_config`, `client_groups`,
`devices`, `installers`, `users`). Use the single-tenant one going
forward.

## Verified live on the Lightsail box (`rds.pacificmit.com`)

During the per-tenant phase: a real test tenant (`test-tenant`) was
provisioned for real, its `hbbs`/`hbbr` containers came up correctly, its
ports (21100-21107 range) were confirmed reachable from the public
internet via `nc`. That tenant should be torn down as part of migrating
to single-tenant — see the migration steps in
`subsystems/single-tenant/README.md`.

The single-tenant `setup_server.py` itself has only been tested against
**mocked** Docker calls so far (built without direct server access in
this session) — real `docker compose up` on the actual box is the next
thing to verify, the same way the per-tenant version was verified
earlier.

## Local dev / testing notes

Python 3, no external dependencies required to run
`subsystems/single-tenant/setup_server.py` (standard library only).
`requirements.txt` at the repo root only matters if you want to validate
YAML locally the way the test suite did.

## Security notes

This repo will eventually have code that *handles* the server's
keypair, signing credentials, and built installer binaries — none of
those artifacts should ever be committed if you put this back under
version control. Keypairs and the fleet DB live on the server under
`/opt/rustdesk-fleet`, outside any repo checkout, always.
