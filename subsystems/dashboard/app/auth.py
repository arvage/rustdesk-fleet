import os
import secrets

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import ratelimit
from app.deps import get_db, log_event
from app.templates_config import templates

router = APIRouter()

# Cached once True, never reset — only tracks whether first-run setup is complete.
_setup_done: bool = False


def _users_exist() -> bool:
    global _setup_done
    if _setup_done:
        return True
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    _setup_done = count > 0
    return _setup_done


class _AuthRedirect(Exception):
    pass


class _SetupRedirect(Exception):
    pass


def note_login_failure(request: Request, email: str) -> None:
    """Count a failed sign-in for this email and IP; log any lockout it starts."""
    ip = ratelimit.client_ip(request)
    if ratelimit.record_failure(email, ip):
        conn = get_db()
        log_event(conn, "login_locked", f"email={email.lower().strip()} ip={ip}", "")
        conn.close()


def locked_seconds(request: Request, email: str) -> int:
    return ratelimit.locked_seconds(email, ratelimit.client_ip(request))


def sign_in(request: Request, user) -> None:
    """Start a session for a users row (password or passkey login). The
    session only carries the id and session_version; everything else is
    read from the database on each request."""
    ratelimit.clear_email(user["email"])
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["session_version"] = user["session_version"]


def bump_session_version(conn, user_id: int) -> int:
    """End every session this user has open. Returns the new version, which
    the caller can pass to sign_in() to keep the current session alive.
    Doesn't commit."""
    conn.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user_id,))
    row = conn.execute("SELECT session_version FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["session_version"] if row else 0


def require_auth(request: Request) -> dict:
    """FastAPI dependency — returns the signed-in user or raises a redirect exception.

    Checks the users table on every request, so deleting a user, changing
    their role or bumping their session_version takes effect immediately.
    """
    if not _users_exist():
        raise _SetupRedirect()
    user_id = request.session.get("user_id")
    if not user_id:
        raise _AuthRedirect()
    conn = get_db()
    user = conn.execute(
        "SELECT id, email, display_name, role, session_version FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if user is None or request.session.get("session_version") != user["session_version"]:
        request.session.clear()
        raise _AuthRedirect()
    from app.permissions import get_role_permissions, all_roles
    role = user["role"]
    return {
        "id": user["id"],
        "role": role,
        "role_name": (all_roles().get(role) or {}).get("name", role),
        "permissions": get_role_permissions(role),
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


@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if _users_exist():
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
):
    global _setup_done
    if _users_exist():
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
        "INSERT INTO users (email, display_name, role, password_hash) VALUES (?, ?, 'admin', ?)",
        (email, display_name, pw_hash),
    )
    conn.commit()
    conn.close()
    _setup_done = True
    return RedirectResponse("/login?setup=1", status_code=303)


# ── Login / logout ────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if not _users_exist():
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
):
    wait = locked_seconds(request, email)
    if wait:
        # Checked before bcrypt, so a locked email or IP costs no hashing.
        return templates.TemplateResponse(
            request, "login.html",
            {"error": ratelimit.locked_message(wait), "just_setup": False},
            status_code=429,
        )

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    conn.close()

    auth_method = user["auth_method"] if user else "password"

    # passkey-only accounts skip the password check entirely — there may be
    # no password_hash worth checking, and the account's security no longer
    # depends on it.
    if auth_method != "passkey":
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
