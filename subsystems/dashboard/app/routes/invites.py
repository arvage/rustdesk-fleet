"""Tenant sign-up links: https://<tenant host>/invite/<token>, created by the
provider console for a new tenant's first admin (valid 72 hours, single
use, only on that tenant's own host). The invitee sets a name and password
and is signed in; they can add a passkey from My Account."""
import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import ratelimit
from app.auth import locked_seconds, note_login_failure, sign_in
from app.deps import get_db, log_event
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()


def _page(request: Request, invite, token: str, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, "invite.html",
                                      {"invite": invite, "token": token, "error": error}, status_code=status_code)


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str, tenant: Tenant = Depends(get_tenant)):
    from app.console.tenants import find_invite
    conn = get_db()
    invite = find_invite(conn, tenant.id, token)
    conn.close()
    if invite is None:
        return _page(request, None, "", status_code=404)
    return _page(request, invite, token)


@router.post("/invite/{token}")
async def invite_accept(request: Request, token: str, display_name: str = Form(""), password: str = Form(""),
                        password2: str = Form(""), tenant: Tenant = Depends(get_tenant)):
    from app.console.tenants import find_invite
    wait = locked_seconds(request, "")
    if wait:
        return _page(request, None, "", ratelimit.locked_message(wait), status_code=429)
    conn = get_db()
    invite = find_invite(conn, tenant.id, token)
    if invite is None:
        conn.close()
        note_login_failure(request, "", count_email=False)
        return _page(request, None, "", status_code=404)
    errors = []
    if len(password) < 10:
        errors.append("Password must be at least 10 characters.")
    if password != password2:
        errors.append("Passwords do not match.")
    if conn.execute("SELECT 1 FROM users WHERE tenant_id = ? AND email = ?", (tenant.id, invite["email"])).fetchone():
        errors.append("An account with this email already exists here; sign in instead.")
    if errors:
        conn.close()
        return _page(request, invite, token, " — ".join(errors), status_code=400)
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    cur = conn.execute(
        "INSERT INTO users (tenant_id, email, display_name, role, password_hash) VALUES (?, ?, ?, ?, ?)",
        (tenant.id, invite["email"], display_name.strip() or invite["display_name"], invite["role"], pw_hash))
    conn.execute("UPDATE tenant_invites SET used_at = datetime('now') WHERE id = ? AND tenant_id = ?",
                 (invite["id"], tenant.id))
    conn.commit()
    log_event(conn, "user_created", f"{invite['email']} (invite)", invite["email"], tenant_id=tenant.id)
    user = conn.execute("SELECT * FROM users WHERE id = ? AND tenant_id = ?", (cur.lastrowid, tenant.id)).fetchone()
    conn.close()
    from app.auth import _setup_done
    _setup_done.add(tenant.id)
    sign_in(request, user)
    request.session["flash"] = {"type": "success",
                                "msg": "Welcome! Add a passkey below for a faster, phishing-proof sign-in."}
    return RedirectResponse("/account", status_code=303)
