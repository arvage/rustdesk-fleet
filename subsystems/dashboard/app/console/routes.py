"""Provider console pages: overview, the provider audit log, provider admins."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.console import mail
from app.console.auth import add_provider_user, create_enrollment, require_provider
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()


def _flash(request: Request, kind: str, msg: str) -> None:
    request.session["flash"] = {"type": kind, "msg": msg}


def page(request: Request, name: str, ctx: dict, status_code: int = 200):
    return templates.TemplateResponse(request, f"console/{name}", ctx, status_code=status_code)


# ── Audit log ────────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request, event: str = "", tenant: str = "", actor: str = "", q: str = "",
                user: dict = Depends(require_provider)):
    """Provider actions (source = 'provider') and provider-level events
    (no tenant): backups, signing, relay operations, console sign-ins."""
    where, params = ["(e.source = 'provider' OR e.tenant_id IS NULL)"], []
    if event:
        where.append("e.event = ?")
        params.append(event)
    if tenant:
        where.append("t.slug = ?")
        params.append(tenant)
    if actor:
        where.append("e.user_email = ?")
        params.append(actor)
    if q:
        where.append("(e.detail LIKE ? OR e.event LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    conn = get_db()
    rows = conn.execute(
        f"""SELECT e.*, t.slug AS tenant_slug FROM provisioning_events e
            LEFT JOIN tenants t ON t.id = e.tenant_id
            WHERE {' AND '.join(where)} ORDER BY e.created_at DESC, e.id DESC LIMIT 500""",
        params,
    ).fetchall()
    events = [r[0] for r in conn.execute(
        "SELECT DISTINCT event FROM provisioning_events WHERE source = 'provider' OR tenant_id IS NULL ORDER BY 1")]
    tenants = [r[0] for r in conn.execute("SELECT slug FROM tenants ORDER BY slug")]
    actors = [r[0] for r in conn.execute(
        "SELECT DISTINCT user_email FROM provisioning_events WHERE source = 'provider' AND user_email IS NOT NULL ORDER BY 1")]
    conn.close()
    return page(request, "audit.html", {"current_user": user, "rows": rows, "events": events, "tenants": tenants,
                                        "actors": actors, "f": {"event": event, "tenant": tenant, "actor": actor, "q": q}})


# ── Provider admins ──────────────────────────────────────────────────────────

@router.get("/admins", response_class=HTMLResponse)
async def admins(request: Request, user: dict = Depends(require_provider)):
    conn = get_db()
    rows = conn.execute(
        """SELECT u.*, (SELECT COUNT(*) FROM provider_webauthn_credentials c WHERE c.provider_user_id = u.id) AS passkeys
           FROM provider_users u ORDER BY u.created_at""").fetchall()
    conn.close()
    return page(request, "admins.html", {"current_user": user, "admins": rows,
                                         "new_link": request.session.pop("new_enroll_link", None)})


def _send_enrollment(email: str, name: str, link: str, inviter: str) -> tuple[bool, str]:
    return mail.send([email], "Your RustDesk Fleet provider console access", mail.simple_html(
        "Set up your provider console passkey",
        [f"{inviter} gave you access to the RustDesk Fleet provider console.",
         "Open the link below on the device you'll sign in from and create a passkey. "
         "The link works once and expires in one hour."],
        link, "Create my passkey"), event="provider_invite_emailed")


@router.post("/admins")
async def admin_invite(request: Request, email: str = Form(...), display_name: str = Form(""),
                       user: dict = Depends(require_provider)):
    conn = get_db()
    try:
        uid = add_provider_user(conn, email, display_name)
    except ValueError as e:
        conn.close()
        _flash(request, "error", str(e))
        return RedirectResponse("/admins", status_code=303)
    link = create_enrollment(conn, uid, user["email"])
    provider_log(conn, "provider_admin_invited", email.strip().lower(), user["email"])
    conn.close()
    ok, err = _send_enrollment(email.strip().lower(), display_name, link, user["name"])
    request.session["new_enroll_link"] = {"email": email.strip().lower(), "link": link, "emailed": ok}
    _flash(request, "success" if ok else "error",
           "Invitation emailed." if ok else f"Invitation created, but not emailed: {err} Copy the link below.")
    return RedirectResponse("/admins", status_code=303)


def _target(conn, admin_id: int):
    return conn.execute("SELECT * FROM provider_users WHERE id = ?", (admin_id,)).fetchone()


@router.post("/admins/{admin_id}/enroll-link")
async def admin_enroll_link(request: Request, admin_id: int, user: dict = Depends(require_provider)):
    conn = get_db()
    target = _target(conn, admin_id)
    if target is None or target["disabled_at"]:
        conn.close()
        _flash(request, "error", "Provider admin not found.")
        return RedirectResponse("/admins", status_code=303)
    link = create_enrollment(conn, admin_id, user["email"])
    provider_log(conn, "provider_enroll_link_created", target["email"], user["email"])
    conn.close()
    ok, _ = _send_enrollment(target["email"], target["display_name"], link, user["name"])
    request.session["new_enroll_link"] = {"email": target["email"], "link": link, "emailed": ok}
    return RedirectResponse("/admins", status_code=303)


@router.post("/admins/{admin_id}/disable")
async def admin_disable(request: Request, admin_id: int, user: dict = Depends(require_provider)):
    conn = get_db()
    target = _target(conn, admin_id)
    if target is None:
        conn.close()
        _flash(request, "error", "Provider admin not found.")
        return RedirectResponse("/admins", status_code=303)
    if target["id"] == user["id"]:
        conn.close()
        _flash(request, "error", "You can't disable your own account.")
        return RedirectResponse("/admins", status_code=303)
    conn.execute("UPDATE provider_users SET disabled_at = datetime('now'), session_version = session_version + 1"
                 " WHERE id = ?", (admin_id,))
    conn.commit()
    provider_log(conn, "provider_admin_disabled", target["email"], user["email"])
    conn.close()
    _flash(request, "success", f"{target['email']} is disabled and signed out.")
    return RedirectResponse("/admins", status_code=303)


@router.post("/admins/{admin_id}/enable")
async def admin_enable(request: Request, admin_id: int, user: dict = Depends(require_provider)):
    conn = get_db()
    target = _target(conn, admin_id)
    if target is None:
        conn.close()
        _flash(request, "error", "Provider admin not found.")
        return RedirectResponse("/admins", status_code=303)
    conn.execute("UPDATE provider_users SET disabled_at = NULL WHERE id = ?", (admin_id,))
    conn.commit()
    provider_log(conn, "provider_admin_enabled", target["email"], user["email"])
    conn.close()
    _flash(request, "success", f"{target['email']} is enabled again.")
    return RedirectResponse("/admins", status_code=303)


# ── Overview (the tenant list arrives in a later step) ───────────────────────

@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, user: dict = Depends(require_provider)):
    conn = get_db()
    tenants = conn.execute("SELECT id, slug, display_name, status, created_at FROM tenants ORDER BY id").fetchall()
    conn.close()
    return page(request, "overview.html", {"current_user": user, "tenants": tenants})
