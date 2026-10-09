import threading
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db, log_event
from app.notifications import fire_notification
from app.permissions import require_perm
from app.templates_config import templates

router = APIRouter()


# ── Update watcher (server + client) ─────────────────────────────────────────
# Polls GitHub hourly for the latest rustdesk-server release and the latest
# RustDesk client release, firing "server_update_available" /
# "client_update_available" once per new version. Each version notified is
# recorded in provisioning_events, so restarts don't re-send it.

_UPDATE_CHECK_INTERVAL_S = 3600  # matches the 1h caches on both lookups
_UPDATE_CHECK_STARTUP_DELAY_S = 60


def _notify_once(event: str, current: str, latest: str) -> None:
    conn = get_db()
    try:
        already = conn.execute(
            "SELECT 1 FROM provisioning_events WHERE event = ? AND detail = ?",
            (event, latest),
        ).fetchone()
        if already:
            return
        log_event(conn, event, latest, "")
    finally:
        conn.close()

    fire_notification(event, {"current_version": current, "latest_version": latest})


def _check_server_update() -> None:
    from setup_server import get_server_version, get_latest_server_version

    current = get_server_version()
    latest = get_latest_server_version()
    if current and latest and current != latest:
        _notify_once("server_update_available", current, latest)


def _check_client_update() -> None:
    from generate_installer import get_latest_client_version, get_pinned_version, version_tuple

    current = get_pinned_version()
    latest = get_latest_client_version()
    if current and latest and version_tuple(latest) > version_tuple(current):
        _notify_once("client_update_available", current, latest)


def _bg_update_watch_loop() -> None:
    time.sleep(_UPDATE_CHECK_STARTUP_DELAY_S)
    while True:
        for check in (_check_server_update, _check_client_update):
            try:
                check()
            except Exception:
                pass
        time.sleep(_UPDATE_CHECK_INTERVAL_S)


# ── Client-version update + background rebuild ───────────────────────────────

_rebuild_lock = threading.Lock()


def _rebuild_outdated_installers(version: str, user_email: str) -> None:
    """Rebuild each group's current installer for every platform it has, if
    that installer predates `version`. Runs in a background thread — a full
    rebuild can take longer than a request should."""
    from generate_installer import build_installer, InstallerError
    from app.routes.links import latest_installers

    if not _rebuild_lock.acquire(blocking=False):
        return  # a rebuild is already running
    try:
        conn = get_db()
        groups = conn.execute(
            "SELECT id, slug FROM client_groups WHERE status = 'active'"
        ).fetchall()
        todo = [
            (g["slug"], i["platform"])
            for g in groups
            for i in latest_installers(conn, g["id"])
            if i["rustdesk_version"] != version
        ]
        conn.close()

        built = failed = 0
        for slug, platform in todo:
            try:
                build_installer(slug, platform, user_email)
                built += 1
            except InstallerError:
                failed += 1  # recorded on the installer row + audit log

        conn = get_db()
        log_event(conn, "installers_rebuilt", f"version={version} built={built} failed={failed}", user_email)
        conn.close()
    finally:
        _rebuild_lock.release()


threading.Thread(target=_bg_update_watch_loop, daemon=True, name="server-update-watch").start()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


# Plain `def` (not async): the GitHub lookups below are blocking, so let
# FastAPI run this in its threadpool instead of stalling the event loop.
@router.get("/", response_class=HTMLResponse)
def home(request: Request, current_user: dict = Depends(require_auth)):
    from setup_server import get_status, get_server_version, get_latest_server_version
    from generate_installer import get_latest_client_version, get_pinned_version, version_tuple
    status = get_status()
    current_version = get_server_version()
    latest_version = get_latest_server_version()
    update_available = bool(
        current_version and latest_version and current_version != latest_version
    )
    client_version = get_pinned_version()
    latest_client = get_latest_client_version()
    client_update_available = bool(
        client_version and latest_client and version_tuple(latest_client) > version_tuple(client_version)
    )
    try:
        import backup
        backup_status = backup.status()
    except Exception:
        backup_status = None
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
        },
    )


@router.post("/server/backup")
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


@router.get("/server/backup/download/latest")
def server_backup_download(current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_backups")
    import backup
    latest = backup.latest_archive()
    if not latest:
        raise PermissionError("No backup archive available yet.")
    return FileResponse(str(latest), filename=latest.name, media_type="application/octet-stream")


@router.post("/server/update")
async def server_update(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "update_server")
    from setup_server import get_latest_server_version, update_server, ProvisioningError

    latest = get_latest_server_version()
    if not latest:
        _set_flash(request, "error", "Could not reach GitHub to determine the latest version.")
        return RedirectResponse("/", status_code=303)

    try:
        update_server(latest, current_user["email"])
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
        log_event(conn, "client_version_updated", f"{result['current']} -> {result['latest']} ({archs})", current_user["email"])
    conn.close()

    version = result["latest"]
    msg = (f"Installers now use RustDesk {version}." if result["updated"]
           else f"Installers already use RustDesk {version}.")
    if rebuild == "1":
        threading.Thread(
            target=_rebuild_outdated_installers, args=(version, current_user["email"]),
            daemon=True, name="installer-rebuild",
        ).start()
        msg += " Rebuilding every group's installers in the background — check the group pages in a minute or two."
    else:
        msg += " Rebuild each group's installer to use it."
    _set_flash(request, "success", msg)
    return RedirectResponse("/", status_code=303)
