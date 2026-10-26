from fastapi import Request

from app.tenancy import get_tenant

RP_NAME = "RustDesk Fleet"


def get_rp_id(request: Request) -> str:
    """Bare hostname WebAuthn scopes credentials to: the tenant domain this
    request was matched on (TenantMiddleware), so a passkey registered on one
    tenant's domain is never valid on another's. Never cached or hardcoded."""
    hostname = get_tenant(request).hostname
    if not hostname:
        raise RuntimeError("Could not determine the WebAuthn RP ID: the request has no tenant domain")
    return hostname


def get_origin(request: Request) -> str:
    """Full origin (scheme://host) WebAuthn checks the ceremony was performed on."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"
