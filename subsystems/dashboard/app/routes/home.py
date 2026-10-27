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
from app.permissions import require_perm
from app.tenancy import Tenant, active_tenants, get_tenant, is_provider_home
from app.templates_config import templates

router = APIRouter()


# ── Update watcher (server + client) ─────────────────────────────────────────
# Polls GitHub hourly for the latest rustdesk-server release and the latest
# RustDesk client release, firing "server_update_available" /
# "client_update_available" once per new version, per tenant (each tenant's
# own recipients). Each version notified is recorded in that tenant's
# provisioning_events, so restarts don't re-send it.

_UPDATE_CHECK_INTERVAL_S = 3600  # matches the 1h caches on both lookups
_UPDATE_CHECK_STARTUP_DELAY_S = 60


def _notify_once(tenant_id: int, event: str, current: str, latest: str) -> None:
    conn = get_db()
    try:
        already = conn.execute(
            "SELECT 1 FROM provisioning_events WHERE tenant_id = ? AND event = ? AND detail = ?",
            (tenant_id, event, latest),
        ).fetchone()
        if already:
            return
        log_event(conn, event, latest, "", tenant_id=tenant_id)
    finally:
        conn.close()

    fire_notification(tenant_id, event, {"current_version": current, "latest_version": latest})


def _check_server_update() -> None:
    """Each tenant's own relay version against the latest release."""
    from setup_server import get_server_version, get_latest_server_version

    latest = get_latest_server_version()
    if not latest:
        return
    for tenant in active_tenants():
        if tenant.relay is None:
            continue
        current = get_server_version(dataclasses.asdict(tenant.relay))
        if current and current != latest:
            _notify_once(tenant.id, "server_update_available", current, latest)


def _check_client_update() -> None:
    """The client version is provider-level (one pin for every tenant's
    installers); every tenant that builds installers hears about updates."""
    from generate_installer import get_latest_client_version, get_pinned_version, version_tuple

    current = get_pinned_version()
    latest = get_latest_client_version()
    if current and latest and version_tuple(latest) > version_tuple(current):
        for tenant in active_tenants():
            _notify_once(tenant.id, "client_update_available", current, latest)


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


# Plain `def` (not async): the GitHub lookups below are blocking, so let
# FastAPI run this in its threadpool instead of stalling the event loop.
@router.get("/", response_class=HTMLResponse)
def home(request: Request, tenant: Tenant = Depends(get_tenant)):
    # Unknown hosts never get here (TenantMiddleware answers 404).
    if _users_exist(tenant) and not request.session.get("user_id") and LANDING_PAGE.is_file():
        return FileResponse(LANDING_PAGE, media_type="text/html")
    current_user = require_auth(request)
    from setup_server import get_server_version, get_latest_server_version
    from generate_installer import get_latest_client_version, get_pinned_version, version_tuple
    relay = dataclasses.asdict(tenant.relay) if tenant.relay else None
    status = relay
    current_version = get_server_version(relay)
    latest_version = get_latest_server_version()
    update_available = bool(
        current_version and latest_version and current_version != latest_version
    )
    client_version = get_pinned_version()
    latest_client = get_latest_client_version()
    client_update_available = bool(
        client_version and latest_client and version_tuple(latest_client) > version_tuple(client_version)
    )
    # Backups are the provider's (TENANCY.md): shown on the provider home only.
    backup_status = offsite = None
    if is_provider_home(tenant):
        try:
            import backup
            backup_status = backup.status()
        except Exception:
            backup_status = None
        try:
            import backup_remote
            offsite = backup_remote.offsite_summary()
        except Exception:
            offsite = None
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "server": status,
            "current_user": current_user,
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": update_available,
            "client_version": client_version,
            "latest_client": latest_client,
            "client_update_available": client_update_available,
            "rebuild_running": _rebuild_lock.locked(),
            "backup_status": backup_status,
            "offsite": offsite,
            "provider_home": is_provider_home(tenant),
        },
    )


def require_provider_home(tenant: Tenant = Depends(get_tenant)) -> Tenant:
    """Provider-level pages (backups, signing) exist only on the provider
    home tenant for now; anywhere else they are simply not there (404)."""
    if not is_provider_home(tenant):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return tenant


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, current_user: dict = Depends(require_auth)):
    return templates.TemplateResponse(request, "help.html", {"current_user": current_user})


@router.post("/server/backup", dependencies=[Depends(require_provider_home)])
def server_backup(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_backups")
    import backup
    st = backup.run()
    if st.get("ok"):
        msg = f"Backup created — {st['file']} ({st['size_human']})."
        if st.get("skipped"):
            msg += f" {len(st['skipped'])} root-only file(s) skipped (captured by the nightly root backup)."
        if st.get("offsite", {}).get("attempted") and not st["offsite"]["ok"]:
            _set_flash(request, "error", msg + f" Offsite copy FAILED: {st['offsite']['error']}")
            return RedirectResponse("/", status_code=303)
        _set_flash(request, "success", msg)
    else:
        _set_flash(request, "error", f"Backup failed: {st.get('error')}")
    return RedirectResponse("/", status_code=303)


@router.get("/server/backup/download/latest", dependencies=[Depends(require_provider_home)])
def server_backup_download(current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_backups")
    import backup
    latest = backup.latest_archive()
    if not latest:
        raise PermissionError("No backup archive available yet.")
    return FileResponse(str(latest), filename=latest.name, media_type="application/octet-stream")


@router.post("/server/update")
async def server_update(request: Request, current_user: dict = Depends(require_auth)):
    """Update the request's tenant's own relay."""
    require_perm(current_user, "update_server")
    from setup_server import get_latest_server_version, update_server, ProvisioningError

    latest = get_latest_server_version()
    if not latest:
        _set_flash(request, "error", "Could not reach GitHub to determine the latest version.")
        return RedirectResponse("/", status_code=303)

    try:
        update_server(current_user["tenant_id"], latest, current_user["email"])
        _set_flash(request, "success", f"Server updated to {latest}.")
    except ProvisioningError as e:
        _set_flash(request, "error", f"Update failed: {e}")
    return RedirectResponse("/", status_code=303)


@router.post("/installers/update-version")
def installers_update_version(
    request: Request,
    rebuild: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "update_server")
    from generate_installer import update_version, InstallerError

    try:
        result = update_version()
    except InstallerError as e:
        _set_flash(request, "error", f"Client update failed: {e}")
        return RedirectResponse("/", status_code=303)

    conn = get_db()
    if result["updated"]:
        archs = ", ".join(a["arch"] for a in result["archs"])
        log_event(conn, "client_version_updated", f"{result['current']} -> {result['latest']} ({archs})",
                  current_user["email"], tenant_id=current_user["tenant_id"])
    conn.close()

    version = result["latest"]
    msg = (f"Installers now use RustDesk {version}." if result["updated"]
           else f"Installers already use RustDesk {version}.")
    if rebuild == "1":
        threading.Thread(
            target=_rebuild_outdated_installers,
            args=(current_user["tenant_id"], version, current_user["email"]),
            daemon=True, name="installer-rebuild",
        ).start()
        msg += " Rebuilding every group's installers in the background — check the group pages in a minute or two."
    else:
        msg += " Rebuild each group's installer to use it."
    _set_flash(request, "success", msg)
    return RedirectResponse("/", status_code=303)
