from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.templates_config import templates

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, current_user: dict = Depends(require_auth)):
    from setup_server import get_status, get_server_version, get_latest_server_version
    status = get_status()
    current_version = get_server_version()
    latest_version = get_latest_server_version()
    update_available = bool(
        current_version and latest_version and current_version != latest_version
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "server": status,
            "current_user": current_user,
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": update_available,
        },
    )


@router.post("/server/update")
async def server_update(request: Request, current_user: dict = Depends(require_auth)):
    if current_user["role"] != "admin":
        raise PermissionError("Admin access required.")
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
