"""The provider console: a separate app on admin.<PROVIDER_DOMAIN>, reached
only through TenantMiddleware (app/tenancy.py), with its own routes and its
own session cookie. No tenant route exists here, and no console route exists
on a tenant host."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import settings
from app.security import PROVIDER_COOKIE
from app.console.auth import ProviderAuthRedirect, router as auth_router
from app.console.routes import router as console_router
from app.console.server import router as server_router
from app.console.relay_pages import router as relay_router
from app.console.tenant_pages import router as tenant_router

console_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _secret() -> str:
    from app.main import _session_secret
    # A different key from tenant sessions: a tenant cookie can never be
    # replayed as a console one, whatever its name.
    return "provider-console:" + _session_secret()


console_app.add_middleware(
    SessionMiddleware,
    secret_key=_secret(),
    session_cookie=PROVIDER_COOKIE,
    max_age=settings.provider_session_max_age(),
    path="/",
    https_only=True,
    same_site="strict",
)
@console_app.middleware("http")
async def _mark(request: Request, call_next):
    """Mark console responses, so tests (and anyone debugging) can tell which
    app answered: tenant routes never carry this header."""
    response = await call_next(request)
    response.headers["X-Fleet-App"] = "console"
    return response


console_app.mount("/static", StaticFiles(directory=str(Path(__file__).parents[1] / "static")), name="static")


@console_app.exception_handler(ProviderAuthRedirect)
async def _to_login(request: Request, _exc):
    return RedirectResponse("/login", status_code=303)


@console_app.exception_handler(PermissionError)
async def _permission(request: Request, exc: PermissionError):
    request.session["flash"] = {"type": "error", "msg": str(exc)}
    return RedirectResponse("/", status_code=303)


@console_app.exception_handler(404)
async def _not_found(request: Request, _exc):
    return HTMLResponse("Not found", status_code=404)


console_app.include_router(auth_router)
console_app.include_router(console_router)
console_app.include_router(server_router)
console_app.include_router(relay_router)
console_app.include_router(tenant_router)
