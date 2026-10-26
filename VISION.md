# Product vision

Recorded 2026-10-26. This is where RustDesk Fleet is headed. The code today
is still the single-tenant system described in [`README.md`](README.md);
nothing below is built yet unless it says so.

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

- **Client PDF guides** ([`client-docs/`](client-docs/README.md)) are
  generated per tenant when the tenant is created. They do not include IT
  support contact details. `generate_guides()` in `client-docs/build_client_docs.py`
  is ready for tenant creation to call; the hook itself waits for the tenant
  model.

## Questions to settle before building

- **Relay isolation per tenant.** The project moved to one shared relay and
  key because a technician's RustDesk client holds one server and key at a
  time, and switching between *clients* was impractical
  ([`README.md`](README.md), single-tenant security note). With tenants as
  MSPs, a technician works inside one tenant, so a relay and key **per
  tenant** may be workable again (see `subsystems/provisioning/`, kept as
  reference). Sharing one relay across tenants means one MSP's leaked key
  could reach another MSP's devices, which is much harder to accept between
  separate paying companies than between clients of one MSP.
- **Data separation.** Today everything lives in one SQLite file. Decide on a
  `tenant_id` on every table, a database per tenant, or a move to a server
  database.
- **Tenant onboarding.** Sign-up, trial, and what is set up when a tenant is
  created (relay, keys, branding, guides).
- **Tenant branding and domains.** Whether each tenant gets its own
  subdomain or custom domain, logo, emails and installer branding.
- **Provider admin.** A provider-level console above tenants: tenant list,
  plans, usage, billing, support access.
- **Time tracking for tenant billing.** Whether session time alone is
  billable, or technicians also log manual time, and how rates, rounding and
  minimums are set per contract.
- **Compliance.** Taking payments and storing customer data for other
  companies raises the bar (for example PCI scope through Stripe/Square,
  data-processing terms, audit expectations).
