from fastapi import Request

from app.tenancy import get_tenant

RP_NAME = "RustDesk Fleet"


def rp_id_for(tenant_id: int, conn=None) -> str:
    """The RP ID a tenant's passkeys belong to: its own web host (its provider
    subdomain, or tenant #1's legacy host), whichever host the user is on.
    A custom domain is a related origin of it (/.well-known/webauthn), so the
    same passkey works there, and losing a custom domain never loses a
    passkey. Never another tenant's: each tenant has its own host."""
    from app import domains
    from app.deps import get_db
    own = conn is None
    conn = conn or get_db()
    try:
        return domains.home_host(conn, tenant_id)
    finally:
        if own:
            conn.close()


def get_rp_id(request: Request) -> str:
    tenant = get_tenant(request)
    rp_id = rp_id_for(tenant.id)
    if not rp_id:
        raise RuntimeError("Could not determine the WebAuthn RP ID: the tenant has no web host")
    return rp_id


def get_origin(request: Request) -> str:
    """Full origin (scheme://host) WebAuthn checks the ceremony was performed on:
    the host this request came in on, which TenantMiddleware matched to the
    tenant (its own host or one of its served custom domains)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def on_home_host(request: Request) -> bool:
    return get_tenant(request).hostname == rp_id_for(get_tenant(request).id)


def ensure_rp_column(conn) -> None:
    """webauthn_credentials.rp_id: the RP ID a passkey was registered for.
    Rows from before phase 1d were all registered on the tenant's own host
    (custom domains didn't exist), so that is their RP ID."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(webauthn_credentials)")}
    if "rp_id" not in cols:
        conn.execute("ALTER TABLE webauthn_credentials ADD COLUMN rp_id TEXT")
    conn.execute("""UPDATE webauthn_credentials SET rp_id = (
                        SELECT hostname FROM tenant_domains d WHERE d.tenant_id = webauthn_credentials.tenant_id
                        AND d.kind <> 'custom' ORDER BY d.kind = 'provider_subdomain' DESC, d.id LIMIT 1)
                    WHERE rp_id IS NULL""")
    conn.commit()
