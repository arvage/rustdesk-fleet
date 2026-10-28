import dataclasses
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import paths
from app.auth import _users_exist, require_auth
from app.deps import get_db, log_event
from app.notifications import fire_notification
from app.tenancy import Tenant, get_tenant
from app.tenant_migration import LEGACY_TENANT_ID
from app.templates_config import templates

router = APIRouter()


# ── Update watcher (server + client) ─────────────────────────────────────────
# Polls GitHub hourly for the latest rustdesk-server release and the latest
# RustDesk client release. Both are provider-level (we upgrade relays and pin
# the client version for every tenant), so the provider admins are emailed,
# once per new version; each version announced is recorded as a provider
# event, so restarts don't re-send it.

_UPDATE_CHECK_INTERVAL_S = 3600  # matches the 1h caches on both lookups
_UPDATE_CHECK_STARTUP_DELAY_S = 60


def _notify_once(event: str, what: str, current: str, latest: str) -> None:
    conn = get_db()
    try:
        already = conn.execute(
            "SELECT 1 FROM provisioning_events WHERE tenant_id IS NULL AND event = ? AND detail = ?",
            (event, latest),
        ).fetchone()
        if already:
            return
        log_event(conn, event, latest, "", tenant_id=None)
    finally:
        conn.close()
    from app.console import mail
    mail.send_to_provider_admins(
        f"RustDesk {what} {latest} available",
        mail.simple_html(f"RustDesk {what} {latest} is available",
                         [f"Currently in use: {current}.",
                          "Upgrade from the provider console (Relays, or Client version)."]),
        event="provider_update_email")


def _check_server_update() -> None:
    """The relays' version (tenant #1's compose pin) against the latest release."""
    from setup_server import get_relay, get_server_version, get_latest_server_version

    latest = get_latest_server_version()
    current = get_server_version(get_relay(LEGACY_TENANT_ID))
    if latest and current and current != latest:
        _notify_once("server_update_available", "server", current, latest)


def _check_client_update() -> None:
    from generate_installer import get_latest_client_version, get_pinned_version, version_tuple

    current = get_pinned_version()
    latest = get_latest_client_version()
    if current and latest and version_tuple(latest) > version_tuple(current):
        _notify_once("client_update_available", "client", current, latest)


def _refresh_os_lifecycle() -> None:
    """Windows end-of-support dates (app/os_lifecycle.py); downloads at most
    once a day, so calling it every pass is cheap."""
    from app import os_lifecycle
    os_lifecycle.refresh()


def _bg_update_watch_loop() -> None:
    time.sleep(_UPDATE_CHECK_STARTUP_DELAY_S)
    while True:
        for check in (_check_server_update, _check_client_update, _refresh_os_lifecycle):
            try:
                check()
            except Exception:
                pass
        time.sleep(_UPDATE_CHECK_INTERVAL_S)


# ── Client-version update + background rebuild ───────────────────────────────

_rebuild_lock = threading.Lock()


def _rebuild_outdated_installers(tenant_id: int, version: str, user_email: str) -> None:
    """Rebuild each of the tenant's groups' current installers for every
    platform it has, if that installer predates `version`. Runs in a
    background thread — a full rebuild can take longer than a request should."""
    from generate_installer import build_installer, InstallerError
    from app.routes.links import latest_installers

    if not _rebuild_lock.acquire(blocking=False):
        return  # a rebuild is already running
    try:
        conn = get_db()
        groups = conn.execute(
            "SELECT id, slug FROM client_groups WHERE tenant_id = ? AND status = 'active'", (tenant_id,)
        ).fetchall()
        todo = [
            (g["slug"], i["platform"])
            for g in groups
            for i in latest_installers(conn, tenant_id, g["id"])
            if i["rustdesk_version"] != version
        ]
        conn.close()

        built = failed = 0
        for slug, platform in todo:
            try:
                build_installer(tenant_id, slug, platform, user_email)
                built += 1
            except InstallerError:
                failed += 1  # recorded on the installer row + audit log

        conn = get_db()
        log_event(conn, "installers_rebuilt", f"version={version} built={built} failed={failed}", user_email,
                  tenant_id=tenant_id)
        conn.close()
    finally:
        _rebuild_lock.release()


if paths.WATCHERS_ENABLED:
    threading.Thread(target=_bg_update_watch_loop, daemon=True, name="server-update-watch").start()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


# Public product page shown at "/" to visitors who aren't signed in
# (repo-root site/index.html, a static file with no server-side data).
LANDING_PAGE = Path(__file__).resolve().parents[4] / "site" / "index.html"


# Plain `def` (not async): reading the compose file and version pin is
# blocking, so let FastAPI run this in its threadpool.
@router.get("/", response_class=HTMLResponse)
def home(request: Request, tenant: Tenant = Depends(get_tenant)):
    # Unknown hosts never get here (TenantMiddleware answers 404).
    if _users_exist(tenant) and not request.session.get("user_id") and LANDING_PAGE.is_file():
        return FileResponse(LANDING_PAGE, media_type="text/html")
    current_user = require_auth(request)
    from setup_server import get_server_version
    from generate_installer import get_pinned_version
    relay = dataclasses.asdict(tenant.relay) if tenant.relay else None
    conn = get_db()
    subscription = conn.execute(
        "SELECT status, plan, created_at, trial_ends_at FROM tenants WHERE id = ?", (tenant.id,)
    ).fetchone()
    conn.close()
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "server": relay,
            "current_user": current_user,
            "current_version": get_server_version(relay),
            "client_version": get_pinned_version(),
            "subscription": subscription,
        },
    )


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, current_user: dict = Depends(require_auth)):
    return templates.TemplateResponse(request, "help.html", {"current_user": current_user})
