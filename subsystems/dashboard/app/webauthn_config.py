from urllib.parse import urlparse

from fastapi import Request

RP_NAME = "RustDesk Fleet"


def get_rp_id(request: Request) -> str:
    """Bare hostname WebAuthn scopes credentials to. Derived fresh from the
    incoming request on every call — never cached, never hardcoded — so a
    credential registered against one host is never silently valid on another."""
    host = request.headers.get("host") or request.url.hostname or ""
    hostname = host.split(":")[0]
    if hostname:
        return hostname

    from setup_server import get_status
    status = get_status()
    if status and status.get("host"):
        return urlparse(status["host"]).hostname or status["host"]
    raise RuntimeError("Could not determine WebAuthn RP ID: no Host header and no server_config.host")


def get_origin(request: Request) -> str:
    """Full origin (scheme://host) WebAuthn checks the ceremony was performed on."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"
