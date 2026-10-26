# Product vision

Recorded 2026-10-26. This is where RustDesk Fleet is headed. Nothing below is
built yet unless it says so; the tenant model (phase 1a) is.

## The product

RustDesk Fleet becomes a hosted, multi-tenant remote-support platform that
is sold to MSPs as a commercial product.

- **The provider (us)** hosts the whole system on our own infrastructure and
  sells it. MSPs never run their own server.
- **Tenants are MSPs.** Each MSP that buys the product is a tenant, with its
  own technicians, settings and branding.
- **Each tenant has its own clients.** The businesses an MSP supports are
  that tenant's clients. Today's *client groups* become a tenant's clients.
- **Clients have devices.** This is the device inventory, sessions and audit
  the dashboard already has, scoped to one tenant.

```
Provider (us)
└── Tenant (an MSP)            billed by us
    ├── Technicians / users    roles and permissions, as today
    └── Client (a business)    billed by the tenant at contract rates
        └── Devices            inventory, sessions, audit, as today
```

The goal is a product that makes a profit.

## Billing, two layers

1. **Provider billing.** We bill tenants for using the platform. The pricing
   model is not decided yet (for example per technician, per device, a flat
   plan, or a mix).
2. **Tenant billing.** Each tenant bills its own clients, based on each
   client's contract and hourly rates. The session audit already records who
   connected, to which device and for how long, so remote sessions can feed
   billable time.

These are separate: the provider's billing is about tenants, and a tenant's
billing is about its clients. A tenant must never see another tenant's
billing data, and we act as the platform, not as the party billing the end
client.

## Integrations

An **Integrations** section where tenants (and we, for provider billing)
connect outside services. The aim is to support as many as possible.
Named so far:

- **Stripe** and **Square**: payments and invoicing
- **Microsoft** (for example Microsoft 365 / Entra ID sign-in, Outlook, Teams)
- **Google** (for example Google Workspace sign-in, Gmail, Calendar)
- More to follow (etc.)

## Decided so far

Tenancy (phase 1a, built: see
[`subsystems/dashboard/TENANCY.md`](subsystems/dashboard/TENANCY.md)):

- **One shared control database** (`fleet.sqlite3`) with a `tenant_id` on
  every tenant-owned table. Each tenant's hbbs database stays inside that
  tenant's own relay stack.
- **One relay per tenant**, with its own keypair. A relay is a row in
  `relays` (host, ports, public key, data dir, compose file, hbbs database,
  status), not server-wide config. This answers the relay-isolation question
  below: a technician works inside one tenant, so one server and key per
  tenant is workable, and one MSP's leaked key can't reach another's devices.
- **The tenant is chosen by the request's `Host`.** Each tenant has one or
  more domains in `tenant_domains`: a provider subdomain (phase 1b) and
  optionally a custom domain the tenant points at us with a CNAME (phase 1c).
  The original deployment's host is tenant #1's first (`legacy`) domain.
- **Installers always use the relay host, never a custom domain**, so a
  tenant changing or losing their domain never strands devices. Relay host
  and web domains are separate in the model.
- **Provider-level, not per tenant** (for now): code signing, backups, the
  OS lifecycle cache. Signing and backups are run by us; backups cover every
  relay's keypair and the control database.
- **The current deployment is tenant #1**, with its relay, keys, devices,
  users and URLs exactly as they were.
- Next: creating tenants and their relay stacks, the provider console and
  provider subdomains (phase 1b); custom domains with CNAME verification and
  per-domain certificates (phase 1c).

- **Client PDF guides** ([`client-docs/`](client-docs/README.md)) are
  generated per tenant when the tenant is created. They do not include IT
  support contact details. `generate_guides()` in `client-docs/build_client_docs.py`
  is ready for tenant creation to call; the hook itself waits for the tenant
  model.

## Questions to settle before building

- **Tenant onboarding.** Sign-up, trial, and what is set up when a tenant is
  created (relay, keys, branding, guides).
- **Tenant branding.** Logo, emails and installer branding per tenant
  (domains are decided above).
- **Provider admin.** A provider-level console above tenants: tenant list,
  plans, usage, billing, support access.
- **Time tracking for tenant billing.** Whether session time alone is
  billable, or technicians also log manual time, and how rates, rounding and
  minimums are set per contract.
- **Compliance.** Taking payments and storing customer data for other
  companies raises the bar (for example PCI scope through Stripe/Square,
  data-processing terms, audit expectations).
