# RustDesk Fleet — Single-Tenant (current architecture)

**This replaces the earlier per-tenant/multi-stack design.** One `hbbs`/
`hbbr` pair for the whole fleet, one shared keypair, one device table.
Client separation (GoVirtual365-internal, Acme Corp, etc.) is now a
**label** on devices and installers (`client_groups` table), not separate
infrastructure.

## Why this changed

The per-tenant design gave real security isolation (a leaked installer or
compromised tech account for one client couldn't reach another's
devices), but it broke day-to-day usability: the native RustDesk desktop
client can only hold one server/key configuration at a time — confirmed
this is a deliberate upstream limitation, not a config gap (RustDesk
maintainers have explicitly rejected multi-profile support as a feature
request). For a helpdesk team jumping between clients all day, that meant
manually re-entering ID server/key every switch. This was judged worse
than the isolation was worth, so: single shared server, isolation traded
for usability, accepted deliberately.

## What's here

```
schema.sql           Single-tenant DB schema: server_config (singleton),
                      client_groups, devices, installers, users, events
docker-compose.yml    One hbbs/hbbr pair, network_mode: host
setup_server.py       Bootstrap script + CLI: init server, manage groups
```

## Migrating from the old per-tenant setup on the Lightsail box

The box currently has a `test-tenant` stack running from the old
`subsystems/provisioning/allocate_tenant.py` system (ports 21100-21107).
To move to single-tenant:

```bash
# 1. Tear down the old per-tenant test stack
cd ~/rustdesk-fleet/subsystems/provisioning
python3 allocate_tenant.py destroy --slug test-tenant --delete-data

# 2. Bring up the new single-tenant server (uses RustDesk's own default
#    ports 21115-21119, since there's now only ever one instance)
cd ~/rustdesk-fleet/subsystems/single-tenant   # or wherever this lands
python3 setup_server.py init --host rds.pacificmit.com

# 3. Create client groups for organizational separation
python3 setup_server.py group create --slug govirtual365-internal --display-name "GoVirtual365 Internal"
python3 setup_server.py group create --slug acme-corp --display-name "Acme Corp"
```

**Firewall changes needed**: the old per-tenant ports (21100-21107) can be
closed once `test-tenant` is torn down. The new single server uses
RustDesk's standard default range — open TCP+UDP 21115-21119 in both the
Lightsail networking tab and the OS firewall (`ufw`) if active.

**The old `subsystems/provisioning/` code is superseded** — leave it in
the repo for now as historical reference (the security tradeoff reasoning
in its README is still valid context for why this isn't a security-first
design), but new work happens here.

## Verified on real hardware (rds.pacificmit.com, 2026-06-30)

Deployed on a fresh AWS Lightsail Ubuntu 24.04 instance (911MB RAM + 2GB
swap already present). Docker 29.1.3, Docker Compose 2.40.3, Python 3.12.3.

`python3 setup_server.py init --host rds.pacificmit.com` ran cleanly first
try. No bugs in the script — worked exactly as designed.

**Actual port bindings** (confirmed via `ss -tlnup`):
| Port  | Proto    | Process |
|-------|----------|---------|
| 21115 | TCP      | hbbs (NAT test) |
| 21116 | TCP+UDP  | hbbs (rendezvous / heartbeat) |
| 21117 | TCP      | hbbr (relay) |
| 21118 | TCP      | hbbs (WebSocket) |
| 21119 | TCP      | hbbr (WebSocket) |

**Pubkey** (share with every client device — not secret):
```
HpZma9OkRoGQEdD4gXC8kXkgMJTdaf8ZVj5KzacLfFM=
```

**Ownership**: `/opt/rustdesk-fleet` is ubuntu-owned (created before
running the script to avoid root ownership from `sudo`). The `ubuntu` user
is in the `docker` group; use `sg docker -c "python3 setup_server.py ..."`
until the next login, then plain `python3 setup_server.py ...` works.

**ufw**: inactive on this box — no OS firewall to configure. Only the
Lightsail networking tab needs the ports opened (see Firewall section below).

**Image tag note**: `rustdesk/rustdesk-server:1.1.14` pulled and ran fine.
hbbs logged "new version is available: 1.1.15" at startup — the tag in
`docker-compose.yml` is pinned to 1.1.14 deliberately; upgrade when ready.

- `init` is idempotent — re-running returns same pubkey, no duplicate row
- `status` reflects `active` with pubkey after successful init
- `group create`/`group list` work; `govirtual365-internal` created and confirmed
- `docker-compose.yml` valid with `network_mode: host` on both services

## Firewall — Lightsail networking tab (open these manually in AWS console)

ufw is inactive, so only the Lightsail tab needs updating. Open:

| Port  | Protocol | Service |
|-------|----------|---------|
| 21115 | TCP      | hbbs NAT test |
| 21116 | TCP      | hbbs rendezvous |
| 21116 | UDP      | hbbs heartbeat |
| 21117 | TCP      | hbbr relay |
| 21118 | TCP      | hbbs WebSocket |
| 21119 | TCP      | hbbr WebSocket |

To test reachability from outside the box:
```bash
nc -zv rds.pacificmit.com 21115
nc -zv rds.pacificmit.com 21116
nc -zv rds.pacificmit.com 21117
```

## Installer generation — built and verified (2026-06-30)

**Approach**: NSIS wrapper installer (built with `makensis` on Linux) that
bundles the official RustDesk binary and drops a pre-configured
`RustDesk2.toml` after install, pointing at our server.

**Why not rdgen-cli**: investigated — it's a remote-build service client
(POSTs to rdgen.crayoneater.org, 30-45 min build time, external
dependency). The config-bundling approach is fully local, ~seconds to
build, no external services.

```bash
cd ~/rustdesk-fleet/subsystems/single-tenant
python3 generate_installer.py build --group govirtual365-internal
python3 generate_installer.py list
```

Output goes to `/opt/rustdesk-fleet/installers/`. Each build is recorded
in the `installers` table with `sha256_unsigned` for later signing
verification.

**First real build** (`govirtual365-internal`, windows-x64, v1.4.8):
- Output: `RemoteSupport-govirtual365-internal-1.4.8-x64.exe` (24MB)
- SHA256: `1b43687cd72969fa267c537b040678fe94d8c67c39359190cd14ca1dae4780e7`
- Verified as valid PE32 / NSIS installer via `file`

**What the installer does on the end-user's Windows machine:**
1. Runs `rustdesk-1.4.8-x86_64.exe --silent-install` (no UI)
2. Kills any RustDesk process that auto-started during install
3. Writes `%APPDATA%\RustDesk\config\RustDesk2.toml` with our server/key
4. On first launch, RustDesk reads our config and connects to `rds.pacificmit.com`

**Config written to client machine:**
```toml
rendezvous_server = "rds.pacificmit.com"

[options]
custom-rendezvous-server = "rds.pacificmit.com"
relay-server = "rds.pacificmit.com"
key = "HpZma9OkRoGQEdD4gXC8kXkgMJTdaf8ZVj5KzacLfFM="
```

**Known limitation**: config writes to the *running user's* `%APPDATA%`.
If an admin deploys this remotely under a different account, the config
lands in the admin's profile, not the end-user's. Direct user-run
installs (the expected flow) are fine.

**Prerequisites on the build box** (all already installed):
- `nsis` (makensis) — `sudo apt install nsis`
- `rustdesk-1.4.8-x86_64.exe` in `/opt/rustdesk-fleet/installer-assets/`

## Not yet built

- Signing subsystem (unchanged by this architecture shift) — needs Azure
  Trusted Signing + GitHub Actions Windows runner
- Dashboard reading `client_groups`/`devices`/`users` for filtering and RBAC

## Security note, stated plainly

There is no infrastructure-level isolation between client groups in this
design. Every device, every installer, every tech with access to the
shared key can in principle reach every other group's devices via the
native RustDesk client. `client_groups`/`user_group_access` give the
**dashboard** a way to filter/restrict what's *shown* and what dashboard
*actions* are permitted, but a leaked installer, a leaked key, or a
compromised tech credential has fleet-wide reach. This was a deliberate,
informed tradeoff for operability — worth re-reading if requirements
change later (e.g. a client requires contractual proof of infrastructure
isolation).
