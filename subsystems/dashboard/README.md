# Dashboard

**Not started.**

Management API + UI: tenant-scoped device inventory, RBAC (techs/admins,
scoped per-tenant), audit log of sessions, and the "create tenant" /
"generate installer" actions that call into `subsystems/provisioning` and
`subsystems/signing` respectively.

Reads/writes `shared/db/schema.sql` — same fleet DB the provisioning CLI
already writes to.

## Not yet built

- Everything. This is a placeholder so the monorepo structure reflects the
  full architecture from day one.
