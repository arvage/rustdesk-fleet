> **⚠️ SUPERSEDED.** This was the original per-tenant/multi-stack
> architecture (one `hbbs`/`hbbr` pair per client, isolated by design).
> It was abandoned because the native RustDesk desktop client can't hold
> multiple server profiles at once — confirmed as a deliberate upstream
> limitation, not a config gap — which made switching between clients
> impractical for helpdesk use. The project moved to
> **`subsystems/single-tenant/`** (one shared server, clients separated
> by a `client_groups` label instead of separate infrastructure).
>
> Left in place as working, tested reference code and as a record of the
> isolation-vs-usability tradeoff that was deliberately made — see
> `subsystems/single-tenant/README.md` for the full reasoning. Do not
> build new work here.

# Provisioning (superseded — see notice above)

Stands up a new tenant's isolated `hbbs`/`hbbr` Docker stack on the Lightsail
box: allocates a permanent port block, lays out the data directory, renders
the compose file, brings it up, captures the generated keypair, and persists
everything to `shared/db/schema.sql`'s `tenants` table.

## Files

```
allocate_tenant.py              Core library + CLI (create / list / destroy)
compose-templates/tenant-stack.yml.j2   Jinja2 template, one hbbs+hbbr pair per tenant
requirements.txt
```

## Port scheme

Tenant index `N` (0, 1, 2, ...) occupies a 10-port block starting at
`21100 + N*10`:

- `hbbs` binds to `base`, `base+1` (TCP+UDP heartbeat), `base+3` (web client)
- `hbbr` binds to `base+5`, `base+7`

Indices are **never reused**, even after a tenant is decommissioned (see
`next_port_index()`) — reusing a port block could let a stale cached client
config, or a leaked old installer, register against whatever tenant now
occupies that block.

Each tenant's `hbbs` and `hbbr` share one Docker volume so they share one
keypair — that's how a tenant's clients trust both halves of that tenant's
stack.

Deliberately **not** using `network_mode: host`, since this box runs
multiple tenants — host networking would mean two tenants' containers
fighting over the same default ports.

## Setup on the actual Lightsail box

```bash
pip install -r requirements.txt --break-system-packages

# Edit allocate_tenant.py and set:
#   PUBLIC_HOST          -> your real Lightsail host/domain
#   RUSTDESK_SERVER_TAG  -> pin to a specific rustdesk/rustdesk-server image tag
```

Then open the relevant port range in **two places**, both required:
1. Lightsail's networking tab (cloud firewall)
2. The box's own firewall (`ufw`/`iptables`) if you run one

## Usage

```bash
# Provision a new tenant
python3 allocate_tenant.py create --slug acme-corp --display-name "Acme Corp"

# List all tenants and their connection info
python3 allocate_tenant.py list

# Tear down a tenant (keeps data by default; add --delete-data to wipe)
python3 allocate_tenant.py destroy --slug acme-corp
```

`create` prints the host/port/pubkey a client needs — this is exactly the
data the (not-yet-built) installer-generation step will consume
automatically instead of anyone copy-pasting it by hand.

## Tested so far (mocked Docker, no real infra access)

- Port math: no collisions within or across tenants, confirmed for several
  indices
- Compose template renders to valid YAML with correct port mappings
- Full create flow against a mocked `docker compose up`: DB rows, event
  log, pubkey capture all behave correctly
- Duplicate slug rejected; invalid slug format rejected
- Failure mid-provisioning: tenant row stays visible with
  `status=provisioning` rather than disappearing, and its port index is
  permanently retired — confirmed the next successful tenant does not
  reuse it

**Not yet tested**: actual `docker compose up` against a real Docker
daemon. Verify this first thing on the real box before trusting the happy
path end-to-end.

## Not yet built

- Calling `rdgen-cli` to actually produce a tenant's installer using the
  host/port/pubkey this module already returns
- Hooking into `subsystems/signing` once an installer exists
- "Status of all tenant stacks" / "patch all tenants" fleet-wide operations
