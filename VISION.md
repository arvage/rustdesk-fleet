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
  optionally a custom domain the tenant points at us with a CNAME (phase 1d).
  The original deployment's host is tenant #1's first (`legacy`) domain.
- **Installers always use the relay host, never a custom domain**, so a
  tenant changing or losing their domain never strands devices. Relay host
  and web domains are separate in the model.
- **Provider-level, not per tenant** (for now): code signing, backups, the
  OS lifecycle cache. Signing and backups are run by us; backups cover every
  relay's keypair and the control database.
- **The current deployment is tenant #1**, with its relay, keys, devices,
  users and URLs exactly as they were.

Support access (phase 1c, built: see
[`subsystems/dashboard/README.md`](subsystems/dashboard/README.md), "Support access"):

- **Consent-based and time-limited.** Provider staff can open a tenant's
  dashboard only while that tenant has granted support access, for a period
  the tenant chooses (up to 12 months), with a reason, for at most 4 hours
  per session. The tenant can revoke at any time, is emailed about every
  grant and session, and sees everything support does in its own audit log.
  Unattended-access passwords stay hidden unless the grant includes them.

Provider console (phase 1b, built: see [`DEPLOYMENT.md`](DEPLOYMENT.md) §11):

- **A provider console** at `admin.<PROVIDER_DOMAIN>`, separate from every
  tenant (own app, own passkey-only accounts, own session cookie, optional IP
  allowlist). We create and manage tenants there, and run the provider's
  controls: backups, signing, host health, relay upgrades, the client version.
- **Each tenant gets** a web domain `<slug>.<PROVIDER_DOMAIN>` (own Let's
  Encrypt certificate) and a relay host `<slug>.relay.<PROVIDER_DOMAIN>` with
  its own keypair and a never-reused block of ports. Two wildcard DNS
  records, set up once by hand; no DNS API. Installers use the relay host and
  `api-server = https://<slug>.<PROVIDER_DOMAIN>`.
- **Tenant lifecycle**: trial or active, suspended (relay keeps running),
  cancelled (relay stopped, **30-day grace period** with a read-only export
  for the tenant's admins, reactivation possible), then purged. Usage is
  recorded daily for billing later.
- **Support access** to a tenant's dashboard needs the tenant's consent and
  is time-limited: built in phase 1c (above).
- **Custom domains** (phase 1d, built: see
  [`subsystems/dashboard/TENANCY.md`](subsystems/dashboard/TENANCY.md), "Custom
  domains"): a tenant points its own domain at us with a CNAME to its
  provider subdomain (or A records plus a TXT record where a CNAME can't be
  used), we verify it from public resolvers and issue its certificate, and it
  can become the primary domain for emails and links. The provider subdomain
  always keeps working. **Devices never use a custom domain**: installers keep
  the relay host and the provider subdomain, so losing a domain can't strand
  a device. Passkeys belong to the provider subdomain and work on custom
  domains through WebAuthn related origins, with a sign-in handoff for older
  browsers.

Provider billing (phase 2, built: see
[`subsystems/dashboard/TENANCY.md`](subsystems/dashboard/TENANCY.md), "Billing",
and [`DEPLOYMENT.md`](DEPLOYMENT.md)):

- **Stripe** bills MSPs (provider billing only; MSPs billing their own clients
  is phase 3). Card details only go to Stripe: **Checkout** for sign-up and
  the **Customer Portal** for card, invoices and cancelling (PCI SAQ A).
- **Plans are configuration**, not code: the console maps each plan to Stripe
  Price IDs (flat, per technician, per device; included quantities as free
  graduated tiers; metered devices through a Billing Meter), with soft limits.
- **Open self-service sign-up** on the public site, email verified first,
  **card required**, a **14-day trial** run by Stripe, one trial per card. No
  card, no tenant, by any path.
- **Stripe drives the lifecycle** of signed-up tenants through webhooks and a
  daily reconciliation: trial, active, past due (suspended after 7 days),
  unpaid, cancelled with the 30-day grace period and resubscribe. Tenant #1
  is never billed; console-made tenants are billed outside Stripe (`manual`).

- **Client PDF guides** ([`client-docs/`](client-docs/README.md)) are
  generated per tenant when the tenant is created (provider console), with
  the tenant's name. They do not include IT support contact details.

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
