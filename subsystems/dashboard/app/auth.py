import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import get_db
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


def require_auth(request: Request) -> dict:
    """FastAPI dependency — returns {id, role, name} or raises a redirect exception."""
    if not _users_exist():
        raise _SetupRedirect()
    user_id = request.session.get("user_id")
    if not user_id:
        raise _AuthRedirect()
    return {
        "id": user_id,
        "role": request.session.get("user_role", "tech"),
        "name": request.session.get("user_name", ""),
        "email": request.session.get("user_email", ""),
    }


# ── First-run setup ───────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if _users_exist():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup")
async def setup_post(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    global _setup_done
    if _users_exist():
        return RedirectResponse("/", status_code=303)

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
        return templates.TemplateResponse(
            request, "setup.html", {"error": " — ".join(errors)}, status_code=400
        )

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

    request.session["user_id"] = user["id"]
    request.session["user_role"] = user["role"]
    request.session["user_name"] = user["display_name"] or user["email"]
    request.session["user_email"] = user["email"]
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
