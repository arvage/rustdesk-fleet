import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
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
from app.deps import run_migrations

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

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "change-me-in-production"),
    session_cookie="rdf_session",
    https_only=True,
    same_site="lax",
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    run_migrations()


@app.exception_handler(_AuthRedirect)
async def auth_redirect_handler(request: Request, _exc: _AuthRedirect):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(_SetupRedirect)
async def setup_redirect_handler(request: Request, _exc: _SetupRedirect):
    return RedirectResponse("/setup", status_code=303)


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
