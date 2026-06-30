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

## Tested so far (mocked Docker, no real infra access in this sandbox)

- `init` brings up the server and captures its pubkey
- `init` is idempotent — re-running it doesn't fail or duplicate the
  `server_config` row, returns the same pubkey
- `status` reflects current state
- `group create`/`group list` work; duplicate and invalid slugs rejected
- `docker-compose.yml` is valid YAML with `network_mode: host` on both
  services

**Not yet tested**: real `docker compose up` against actual Docker on the
Lightsail box (same caveat as the original provisioning subsystem before
it). Run `init` for real and confirm against `docker ps` / `docker logs
hbbs` before trusting the happy path.

## Not yet built

- Installer generation tagging a build with a `group_id` (was scoped for
  the old per-tenant `rdgen-cli` work; needs adapting — same tool, just
  every installer now points at the same single server, differentiated
  only by which group it's tagged under in the DB)
- Dashboard reading `client_groups`/`devices`/`users` for filtering and
  RBAC
- Signing subsystem (unchanged by this architecture shift)

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
