import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router, _AuthRedirect, _SetupRedirect
from app.routes.home import router as home_router
from app.routes.groups import router as groups_router
from app.routes.audit import router as audit_router
from app.routes.sessions import router as sessions_router
from app.routes.admin import router as admin_router
from app.routes.devices import router as devices_router
from app.routes.users import router as users_router
from app.routes.api import router as api_router
from app.routes.notifications import router as notifications_router
from app.routes.webauthn import router as webauthn_router
from app.routes.links import router as links_router
from app.routes.client_api import router as client_api_router
from app.routes.dynamic_groups import router as dynamic_groups_router
from app.routes.reports import router as reports_router
from app.routes.email_templates import router as email_templates_router
from app.routes.timeline import router as timeline_router
from app.routes.invites import router as invites_router
from app.deps import run_migrations
from app.tenancy import TenantMiddleware, UnknownTenant

app = FastAPI(docs_url=None, redoc_url=None)


class _QuietClientReports(logging.Filter):
    """Drop uvicorn access-log lines for RustDesk client reports. Every
    device heartbeats every ~15 s (every 3 s during a session), which would
    otherwise bury everything else in the journal."""

    _PATHS = ("/api/heartbeat", "/api/sysinfo", "/api/sysinfo_ver")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access args: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and str(args[2]).split("?", 1)[0] in self._PATHS:
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietClientReports())


_ENV_FILE = "/etc/rustdesk-fleet/dashboard.env"
_OLD_PLACEHOLDER_SECRET = "change-me-in-production"
_DEFAULT_SESSION_MAX_AGE = 12 * 3600


def _session_secret() -> str:
    """SESSION_SECRET signs every session cookie, so refuse to start with a
    missing, short or well-known one rather than run with a forgeable key."""
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if not secret or len(secret) < 32 or secret == _OLD_PLACEHOLDER_SECRET:
        raise RuntimeError(
            "SESSION_SECRET is missing, shorter than 32 characters, or the old placeholder. "
            f"Set it in {_ENV_FILE}, e.g. SESSION_SECRET=$(python3 -c "
            "'import secrets; print(secrets.token_hex(32))'), then restart the dashboard."
        )
    return secret


def _session_max_age() -> int:
    raw = os.environ.get("SESSION_MAX_AGE", "").strip()
    if not raw:
        return _DEFAULT_SESSION_MAX_AGE
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"SESSION_MAX_AGE must be a number of seconds, got {raw!r} (in {_ENV_FILE}).")
    if value <= 0:
        raise RuntimeError(f"SESSION_MAX_AGE must be positive, got {value} (in {_ENV_FILE}).")
    return value


app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    session_cookie="rdf_session",
    max_age=_session_max_age(),
    https_only=True,
    same_site="lax",
)

# Added last, so it runs first: every request is matched to a tenant by its
# Host before anything else (unknown hosts get a 404 without a session).
app.add_middleware(TenantMiddleware)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    run_migrations()
    # Provider console jobs (tenant creation, relay operations, purges).
    from app.console import jobs, relays, tenants  # noqa: F401  (registers job handlers)
    jobs.start_worker()


@app.exception_handler(_AuthRedirect)
async def auth_redirect_handler(request: Request, _exc: _AuthRedirect):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(_SetupRedirect)
async def setup_redirect_handler(request: Request, _exc: _SetupRedirect):
    return RedirectResponse("/setup", status_code=303)


@app.exception_handler(UnknownTenant)
async def unknown_tenant_handler(request: Request, _exc: UnknownTenant):
    return HTMLResponse("Not found", status_code=404)


@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError):
    request.session["flash"] = {"type": "error", "msg": str(exc)}
    return RedirectResponse("/", status_code=303)


app.include_router(auth_router)
app.include_router(home_router)
app.include_router(devices_router)
app.include_router(groups_router)
app.include_router(audit_router)
app.include_router(sessions_router)
app.include_router(admin_router)
app.include_router(users_router)
app.include_router(api_router)
app.include_router(notifications_router)
app.include_router(webauthn_router)
app.include_router(links_router)
app.include_router(client_api_router)
app.include_router(dynamic_groups_router)
app.include_router(reports_router)
app.include_router(email_templates_router)
app.include_router(timeline_router)
app.include_router(invites_router)
