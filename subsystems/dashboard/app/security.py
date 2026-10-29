"""Request hardening shared by the tenant dashboard and the provider console.

Every tenant subdomain and the console are the *same site* to a browser, so
SameSite cookies alone don't stop one of them from submitting forms to
another. SecurityMiddleware wraps the whole app (outside TenantMiddleware,
so it covers the console too) and rejects any state-changing request that
didn't come from a page on its own origin.
"""
from __future__ import annotations

import logging

from app.tenancy import DEVICE_API_PATHS

log = logging.getLogger("rdf.security")

# Session cookies. The __Host- prefix makes browsers refuse any copy with a
# Domain attribute, without Secure or with another Path, so a sibling
# subdomain can't plant one ("cookie tossing").
TENANT_COOKIE = "__Host-rdf_session"
PROVIDER_COOKIE = "__Host-rdf_provider_session"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Non-safe requests allowed without a same-origin signal, with the reason.
# Keep every exemption here.
ORIGIN_EXEMPT_PATHS = {
    # RustDesk clients: no cookies, no browser; each report is checked
    # against the tenant's hbbs peer table (routes/client_api.py).
    **{p: "device API" for p in DEVICE_API_PATHS},
}

# On every dashboard and console response (a route's own value wins, e.g.
# the public download pages' stricter Referrer-Policy). No HSTS
# includeSubDomains: the parent domain isn't ours to commit.
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    # Passkeys need publickey-credentials-*; everything else is off.
    "Permissions-Policy": ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                           "publickey-credentials-get=(self), publickey-credentials-create=(self)"),
    "Cross-Origin-Opener-Policy": "same-origin",
}
_SECURITY_HEADERS_RAW = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]

_REJECTED_HTML = b"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Forbidden</title>
<meta name="robots" content="noindex"></head><body style="font-family:system-ui,sans-serif;text-align:center;
padding:4rem"><h1>Request refused</h1><p>This form was submitted from another site. Go back, reload the page
and try again.</p></body></html>"""


def same_origin(headers: dict[str, str]) -> bool:
    """Whether a browser request came from a page on this exact origin.
    Sec-Fetch-Site wins when present; otherwise Origin must be
    https://<Host>. Neither header: refuse (every supported browser sends
    one on a POST)."""
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() == "same-origin"
    origin = headers.get("origin")
    if origin is not None:
        host = headers.get("host", "").strip().lower()
        return bool(host) and origin.strip().lower() == f"https://{host}"
    return False


class SecurityMiddleware:
    """Pure ASGI: refuse cross-origin state changes before any app code runs,
    and add the security headers to every response except the device API's
    (what RustDesk clients get back stays exactly as it was)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if method not in SAFE_METHODS and path not in ORIGIN_EXEMPT_PATHS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            if not same_origin(headers):
                client = scope.get("client") or ("", 0)
                log.warning("Refused cross-origin %s %s (Origin=%r, Sec-Fetch-Site=%r, IP=%s)",
                            method, path, headers.get("origin"), headers.get("sec-fetch-site"),
                            headers.get("x-real-ip") or client[0])
                await send({"type": "http.response.start", "status": 403, "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(_REJECTED_HTML)).encode()),
                    (b"cache-control", b"no-store"),
                    *_SECURITY_HEADERS_RAW,
                ]})
                await send({"type": "http.response.body", "body": _REJECTED_HTML})
                return
        if path in DEVICE_API_PATHS:
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in _SECURITY_HEADERS_RAW if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_with_headers)
