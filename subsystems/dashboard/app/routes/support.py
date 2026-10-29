"""Support access, tenant side (app/support.py has the rules): the Support
access page, granting and revoking, and the provider's way in."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import support
from app.auth import require_auth
from app.permissions import require_perm
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()


def _flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


@router.get("/admin/support", response_class=HTMLResponse)
def support_page(request: Request, current_user: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    require_perm(current_user, "manage_support_access")
    from app.deps import get_db
    conn = get_db()
    active = support.active_grant(conn, tenant.id)
    conn.close()
    grants, sessions = support.history(tenant.id)
    return templates.TemplateResponse(request, "support_access.html", {
        "current_user": current_user, "active": active, "grants": grants, "sessions": sessions,
        "durations": support.DURATIONS, "max_age_hours": support.SUPPORT_SESSION_MAX_AGE // 3600,
        "now": support.fmt(support._now()),
    })


@router.post("/admin/support/grant")
def support_grant(request: Request, duration: str = Form(""), custom_end: str = Form(""), note: str = Form(""),
                  include_passwords: str = Form(""), current_user: dict = Depends(require_auth),
                  tenant: Tenant = Depends(get_tenant)):
    require_perm(current_user, "manage_support_access")
    try:
        end = support.end_time(duration, custom_end)
        kind, _ = support.grant(tenant.id, current_user, end, note, include_passwords == "1")
    except support.SupportError as e:
        _flash(request, "error", str(e))
        return RedirectResponse("/admin/support", status_code=303)
    _flash(request, "success", f"Support access {'granted' if kind == 'created' else 'changed'}.")
    return RedirectResponse("/admin/support", status_code=303)


@router.post("/admin/support/revoke")
def support_revoke(request: Request, current_user: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    require_perm(current_user, "manage_support_access")
    if support.revoke(tenant.id, current_user):
        _flash(request, "success", "Support access revoked. Any support session has ended.")
    else:
        _flash(request, "error", "Support access isn't granted right now.")
    return RedirectResponse("/admin/support", status_code=303)


@router.get("/support/enter")
def support_enter(request: Request, token: str = "", tenant: Tenant = Depends(get_tenant)):
    """The provider's way in: a single-use token from the console. A GET that
    changes state on purpose (a cross-origin POST from the console would be
    refused); the token is valid for 60 seconds, once, and the redirect to /
    takes it out of the address bar and history."""
    try:
        values = support.enter(tenant.id, token)
    except support.SupportError as e:
        request.session.clear()
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Support link</title>"
            "<body style='font-family:system-ui,sans-serif;text-align:center;padding:4rem'>"
            f"<h1>Can't sign in</h1><p>{_html(str(e))}</p></body>", status_code=403,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    request.session.clear()
    request.session.update(values)
    return RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/support/end")
def support_end(request: Request, current_user: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    if current_user.get("is_support"):
        support.end_session(tenant.id, current_user["support_session_id"], "ended by support", current_user["email"])
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _html(s: str) -> str:
    import html
    return html.escape(s)
