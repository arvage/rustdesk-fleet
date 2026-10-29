import os
import secrets

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import ratelimit
from app.deps import get_db, log_event
from app.tenancy import Tenant, get_tenant
from app.tenant_migration import LEGACY_TENANT_ID
from app.templates_config import templates

router = APIRouter()

# Tenants whose first-run setup is complete (they have a user). Only ever
# grows; a tenant id in here never goes back to /setup.
_setup_done: set[int] = set()


def _users_exist(tenant: Tenant) -> bool:
    if tenant.id in _setup_done:
        return True
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tenant.id,)).fetchone()[0]
    conn.close()
    if count:
        _setup_done.add(tenant.id)
    return bool(count)


class _AuthRedirect(Exception):
    pass


class _SetupRedirect(Exception):
    pass


def _email_key(tenant: Tenant, email: str) -> str:
    """Rate-limit key for an email: per tenant, since the same address can
    be a different account on each tenant."""
    email = email.lower().strip()
    return f"{tenant.id}/{email}" if email else ""


def note_login_failure(request: Request, email: str, count_email: bool = True) -> None:
    """Count a failed sign-in for this email (within the request's tenant)
    and IP; log any lockout it starts. With count_email=False only the IP is
    counted (the email is still logged)."""
    tenant = get_tenant(request)
    ip = ratelimit.client_ip(request)
    if ratelimit.record_failure(_email_key(tenant, email) if count_email else "", ip):
        conn = get_db()
        log_event(conn, "login_locked", f"email={email.lower().strip()} ip={ip}", "", tenant_id=tenant.id)
        conn.close()


def locked_seconds(request: Request, email: str) -> int:
    """Seconds until this email and IP may try again. Pass email="" to check
    only the IP lock."""
    return ratelimit.locked_seconds(_email_key(get_tenant(request), email), ratelimit.client_ip(request))


def sign_in(request: Request, user) -> None:
    """Start a session for a users row (password or passkey login). The
    session only carries the id and session_version; everything else is
    read from the database on each request. The row must belong to the
    request's tenant."""
    tenant = get_tenant(request)
    if user["tenant_id"] != tenant.id:
        raise _AuthRedirect()
    ratelimit.clear_email(_email_key(tenant, user["email"]))
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["session_version"] = user["session_version"]
    conn = get_db()
    conn.execute("UPDATE users SET last_sign_in_at = datetime('now') WHERE id = ? AND tenant_id = ?",
                 (user["id"], tenant.id))
    conn.commit()
    conn.close()


def bump_session_version(conn, tenant_id: int, user_id: int) -> int:
    """End every session this user has open. Returns the new version, which
    the caller can pass to sign_in() to keep the current session alive.
    Doesn't commit."""
    conn.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ? AND tenant_id = ?",
                 (user_id, tenant_id))
    row = conn.execute("SELECT session_version FROM users WHERE id = ? AND tenant_id = ?",
                       (user_id, tenant_id)).fetchone()
    return row["session_version"] if row else 0


def require_auth(request: Request) -> dict:
    """FastAPI dependency — returns the signed-in user or raises a redirect exception.

    Checks the users table on every request, so deleting a user, changing
    their role or bumping their session_version takes effect immediately.
    The user must belong to the request's tenant: a session from another
    tenant's host is cleared and sent to /login.
    """
    tenant = get_tenant(request)
    if not _users_exist(tenant):
        # Only tenant #1's original install path uses /setup; other tenants
        # get their first admin from an invite link (provider console).
        if tenant.id == LEGACY_TENANT_ID:
            raise _SetupRedirect()
        raise _AuthRedirect()
    if request.session.get("support_session_id"):
        # Provider support, signed in with the tenant's consent (app/support.py).
        from app import support
        user = support.session_user(request.session, tenant)
        if user is None:
            request.session.clear()
            raise _AuthRedirect()
        if support.forbidden_path(request.url.path):
            raise PermissionError("Provider support can't use this page.")
        return user
    user_id = request.session.get("user_id")
    if not user_id:
        raise _AuthRedirect()
    conn = get_db()
    user = conn.execute(
        "SELECT id, tenant_id, email, display_name, role, session_version FROM users"
        " WHERE id = ? AND tenant_id = ?", (user_id, tenant.id)
    ).fetchone()
    conn.close()
    if user is None or request.session.get("session_version") != user["session_version"]:
        request.session.clear()
        raise _AuthRedirect()
    from app.permissions import get_role_permissions, all_roles
    role = user["role"]
    return {
        "id": user["id"],
        "tenant_id": user["tenant_id"],
        "role": role,
        "role_name": (all_roles(tenant.id).get(role) or {}).get("name", role),
        "permissions": get_role_permissions(tenant.id, role),
        "name": user["display_name"] or user["email"],
        "email": user["email"],
    }


# ── First-run setup ───────────────────────────────────────────────────────────

def _setup_token() -> str:
    """SETUP_TOKEN from dashboard.env (install.sh writes one). Only consulted
    while no users exist; ignored once the first admin is created."""
    return os.environ.get("SETUP_TOKEN", "").strip()


def _setup_token_ok(given: str) -> bool:
    expected = _setup_token()
    return bool(expected) and secrets.compare_digest(given.strip().encode(), expected.encode())


def _setup_page(request: Request, error: str | None, token: str = "", status_code: int = 200):
    return templates.TemplateResponse(
        request, "setup.html",
        {"error": error, "token": token, "token_configured": bool(_setup_token())},
        status_code=status_code,
    )


def _setup_allowed(tenant: Tenant) -> None:
    """/setup (with SETUP_TOKEN) is tenant #1's install path only."""
    if tenant.id != LEGACY_TENANT_ID:
        from fastapi import HTTPException
        raise HTTPException(status_code=404)


@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request, tenant: Tenant = Depends(get_tenant)):
    _setup_allowed(tenant)
    if _users_exist(tenant):
        return RedirectResponse("/", status_code=303)
    if not _setup_token():
        return _setup_page(request, None, status_code=503)
    token = request.query_params.get("token", "")
    if token and not _setup_token_ok(token):
        return _setup_page(request, "That setup token isn't valid. Check the URL install.sh printed.",
                           status_code=403)
    return _setup_page(request, None, token=token)


@router.post("/setup")
async def setup_post(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    setup_token: str = Form(""),
    tenant: Tenant = Depends(get_tenant),
):
    """Create the first admin of tenant #1 (other tenants: invite links)."""
    _setup_allowed(tenant)
    if _users_exist(tenant):
        return RedirectResponse("/", status_code=303)
    if not _setup_token():
        return _setup_page(request, None, status_code=503)
    wait = locked_seconds(request, email)
    if wait:
        return _setup_page(request, ratelimit.locked_message(wait), status_code=429)
    if not _setup_token_ok(setup_token):
        note_login_failure(request, email)
        return _setup_page(request, "The setup token is missing or wrong.", status_code=403)

    errors = []
    email = email.lower().strip()
    display_name = display_name.strip()
    if not email or "@" not in email:
        errors.append("Enter a valid email address.")
    if len(password) < 10:
        errors.append("Password must be at least 10 characters.")
    if password != password2:
        errors.append("Passwords do not match.")

    if errors:
        return _setup_page(request, " — ".join(errors), token=setup_token, status_code=400)

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    conn = get_db()
    conn.execute(
        "INSERT INTO users (tenant_id, email, display_name, role, password_hash) VALUES (?, ?, ?, 'admin', ?)",
        (tenant.id, email, display_name, pw_hash),
    )
    conn.commit()
    conn.close()
    _setup_done.add(tenant.id)
    return RedirectResponse("/login?setup=1", status_code=303)


# ── Login / logout ────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, tenant: Tenant = Depends(get_tenant)):
    if not _users_exist(tenant) and tenant.id == LEGACY_TENANT_ID:
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    just_setup = request.query_params.get("setup") == "1"
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "just_setup": just_setup}
    )


@router.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(""),
    tenant: Tenant = Depends(get_tenant),
):
    def locked_page(wait: int):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": ratelimit.locked_message(wait), "just_setup": False},
            status_code=429,
        )

    wait = locked_seconds(request, "")  # IP lock: applies to every sign-in
    if wait:
        return locked_page(wait)

    # Only the request's tenant: the same email may be another tenant's user.
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE tenant_id = ? AND email = ?", (tenant.id, email.lower().strip())
    ).fetchone()
    conn.close()

    auth_method = user["auth_method"] if user else "password"

    # passkey-only accounts skip the password check entirely — there may be
    # no password_hash worth checking, and the account's security no longer
    # depends on it.
    if auth_method != "passkey":
        # The email lock gates only the password check (passkey-only accounts
        # have none). Checked before bcrypt, so a locked email costs no hashing.
        wait = locked_seconds(request, email)
        if wait:
            return locked_page(wait)
        # Constant-time failure path: always run bcrypt even on miss.
        _dummy_hash = b"$2b$12$KIX/wKpGiQHGaL5p6vNyWOeqKKXJvq7oH9M3B5bGxDm7XQZR.vMQi"
        stored_hash = user["password_hash"].encode() if (user and user["password_hash"]) else _dummy_hash
        match = bcrypt.checkpw(password.encode(), stored_hash)

        if not user or not user["password_hash"] or not match:
            note_login_failure(request, email)
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Invalid email or password.", "just_setup": False},
                status_code=401,
            )
    # auth_method can only be "passkey" here if `user` was found above
    # (the no-such-user case always defaults auth_method to "password").

    if auth_method in ("passkey", "both"):
        # Second factor required before the real session is granted —
        # require_auth() never reads this key, so nothing is accessible yet.
        request.session["pending_2fa_user_id"] = user["id"]
        request.session["pending_2fa_tenant_id"] = tenant.id
        return RedirectResponse("/login/webauthn", status_code=303)

    sign_in(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    # Signed out, "/" is the public landing page (falls back to /login if
    # site/index.html is missing).
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout_get():
    # Signing out is a POST (a link or prefetch shouldn't end a session).
    # Old bookmarks land on / instead of a 405, still signed in.
    return RedirectResponse("/", status_code=303)
