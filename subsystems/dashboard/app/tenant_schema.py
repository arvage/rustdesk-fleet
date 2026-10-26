"""Schema for tenancy: the provider-level tables that describe tenants, their
relays and their web domains. See TENANCY.md.

Called from deps.run_migrations(); every statement is idempotent.
"""
import sqlite3

TENANT_STATUSES = ("trial", "active", "suspended", "cancelled")
DOMAIN_KINDS = ("provider_subdomain", "custom", "legacy")

TENANCY_TABLES_SQL = """
    CREATE TABLE IF NOT EXISTS tenants (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        slug          TEXT NOT NULL UNIQUE CHECK (slug = lower(slug) AND slug <> ''),
        display_name  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'trial'
                          CHECK (status IN ('trial','active','suspended','cancelled')),
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- One hbbs/hbbr stack per tenant. host is what devices use (installers
    -- bake it in), never one of the tenant's web domains.
    CREATE TABLE IF NOT EXISTS relays (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id     INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
        host          TEXT NOT NULL,
        hbbs_port     INTEGER NOT NULL DEFAULT 21115,
        hbbr_port     INTEGER NOT NULL DEFAULT 21117,
        pubkey        TEXT,
        data_dir      TEXT NOT NULL,
        compose_path  TEXT NOT NULL,
        hbbs_db_path  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'provisioning'
                          CHECK (status IN ('provisioning','active','stopped')),
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Web hostnames that select a tenant (dashboard, download links, device
    -- API). kind: legacy = tenant #1's original host; provider_subdomain =
    -- <slug>.<our domain> (phase 1b); custom = a tenant's CNAME (phase 1c).
    CREATE TABLE IF NOT EXISTS tenant_domains (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
        hostname      TEXT NOT NULL UNIQUE CHECK (hostname = lower(hostname) AND hostname <> ''),
        kind          TEXT NOT NULL CHECK (kind IN ('provider_subdomain','custom','legacy')),
        verified_at   TEXT,
        tls_status    TEXT,
        is_primary    INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_tenant_domains_tenant ON tenant_domains(tenant_id);
    -- At most one primary domain per tenant.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_domains_primary
        ON tenant_domains(tenant_id) WHERE is_primary = 1;
"""


def ensure_tenancy_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(TENANCY_TABLES_SQL)


def normalize_host(raw: str) -> str:
    """'HTTPS://RDS.Example.com:8443/x' -> 'rds.example.com'. Lowercase, no
    scheme, path, port or trailing dot; IPv6 literals lose their brackets."""
    h = (raw or "").strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0]
    if "@" in h:
        h = h.rsplit("@", 1)[1]
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")
