"""Provider console sign-in: passkeys only, no passwords.

Provider accounts (provider_users) are separate from every tenant's users.
Sessions use their own cookie (rdf_provider_session, set by the console
app's SessionMiddleware), hold only the provider user id and
session_version, and are checked against the database on every request,
like tenant sessions (phase 0). The WebAuthn RP ID is the console host.

Passkeys are discoverable (resident) credentials, so signing in needs no
email: the browser offers the provider's passkey for the console host.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app import ratelimit, settings
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()

RP_NAME = "RustDesk Fleet provider console"
ENROLL_TTL = timedelta(hours=1)


class ProviderAuthRedirect(Exception):
    """Not signed in to the console (handled by redirecting to /login)."""


# ── Accounts and enrollment links (also used by scripts/provider_admin.py) ───

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add_provider_user(conn, email: str, display_name: str) -> int:
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("Enter a valid email address.")
    if conn.execute("SELECT 1 FROM provider_users WHERE email = ?", (email,)).fetchone():
        raise ValueError(f"A provider admin with email {email} already exists.")
    cur = conn.execute("INSERT INTO provider_users (email, display_name) VALUES (?, ?)",
                       (email, display_name.strip()))
    conn.commit()
    return cur.lastrowid


def create_enrollment(conn, provider_user_id: int, created_by: str) -> str:
    """A one-time passkey enrollment URL for a provider user, valid for 1 hour.
    Any earlier unused link of theirs stops working."""
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE provider_enrollments SET used_at = ? WHERE provider_user_id = ? AND used_at IS NULL",
                 (_now(), provider_user_id))
    conn.execute(
        "INSERT INTO provider_enrollments (provider_user_id, token_hash, expires_at, created_by) VALUES (?, ?, ?, ?)",
        (provider_user_id, _hash(token),
         (datetime.now(timezone.utc) + ENROLL_TTL).strftime("%Y-%m-%d %H:%M:%S"), created_by),
    )
    conn.commit()
    return f"https://{settings.console_host()}/enroll/{token}"


def _enrollment(conn, token: str):
    """The live enrollment row and its user for a token, or None."""
    return conn.execute(
        """SELECT e.id AS enrollment_id, u.* FROM provider_enrollments e
           JOIN provider_users u ON u.id = e.provider_user_id
           WHERE e.token_hash = ? AND e.used_at IS NULL AND e.expires_at > ? AND u.disabled_at IS NULL""",
        (_hash(token), _now()),
    ).fetchone()


# ── Sessions ─────────────────────────────────────────────────────────────────

def sign_in_provider(request: Request, user) -> None:
    ratelimit.clear_email(f"provider/{user['email']}")
    request.session.clear()
    request.session["provider_user_id"] = user["id"]
    request.session["provider_sv"] = user["session_version"]
    conn = get_db()
    conn.execute("UPDATE provider_users SET last_sign_in_at = datetime('now') WHERE id = ?", (user["id"],))
    conn.commit()
    conn.close()


def require_provider(request: Request) -> dict:
    """FastAPI dependency for console pages: the signed-in, enabled provider
    user, or a redirect to /login. Checked against the database each time,
    so disabling a user or bumping session_version ends their sessions."""
    uid = request.session.get("provider_user_id")
    if not uid:
        raise ProviderAuthRedirect()
    conn = get_db()
    user = conn.execute("SELECT * FROM provider_users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    if user is None or user["disabled_at"] or request.session.get("provider_sv") != user["session_version"]:
        request.session.clear()
        raise ProviderAuthRedirect()
    return {"id": user["id"], "email": user["email"], "name": user["display_name"] or user["email"]}


def _ip(request: Request) -> str:
    return ratelimit.client_ip(request)


def _locked(request: Request) -> int:
    return ratelimit.locked_seconds("", _ip(request))


def _fail(request: Request, email: str = "") -> None:
    if ratelimit.record_failure(f"provider/{email}" if email else "", _ip(request)):
        conn = get_db()
        provider_log(conn, "provider_login_locked", f"email={email} ip={_ip(request)}", email)
        conn.close()


def _rp_id() -> str:
    return settings.console_host()


async def _json(request: Request) -> dict | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{request.headers.get('host') or request.url.netloc}"


# ── Sign-in ──────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("provider_user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "console/login.html", {})


@router.post("/login/webauthn/begin")
async def login_begin(request: Request):
    wait = _locked(request)
    if wait:
        return JSONResponse({"error": ratelimit.locked_message(wait)}, status_code=429)
    options = generate_authentication_options(
        rp_id=_rp_id(), allow_credentials=[], user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session["console_challenge"] = bytes_to_base64url(options.challenge)
    return Response(content=options_to_json(options), media_type="application/json")


@router.post("/login/webauthn/complete")
async def login_complete(request: Request):
    challenge = request.session.pop("console_challenge", None)
    wait = _locked(request)
    if wait:
        return JSONResponse({"error": ratelimit.locked_message(wait)}, status_code=429)
    if not challenge:
        return JSONResponse({"error": "Sign-in session expired — try again."}, status_code=400)
    body = await _json(request)
    if body is None:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    conn = get_db()
    row = conn.execute(
        """SELECT c.id AS cred_row, c.public_key, c.sign_count, u.*
           FROM provider_webauthn_credentials c JOIN provider_users u ON u.id = c.provider_user_id
           WHERE c.credential_id = ?""",
        (str(body.get("id") or ""),),
    ).fetchone()
    if row is None or row["disabled_at"]:
        conn.close()
        _fail(request)
        return JSONResponse({"error": "That passkey isn't registered for the provider console."}, status_code=400)
    try:
        v = verify_authentication_response(
            credential=body, expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_rp_id(), expected_origin=_origin(request),
            credential_public_key=row["public_key"], credential_current_sign_count=row["sign_count"],
        )
    except Exception as e:
        conn.close()
        _fail(request, row["email"])
        return JSONResponse({"error": f"Could not verify passkey: {e}"}, status_code=400)
    conn.execute("UPDATE provider_webauthn_credentials SET sign_count = ?, last_used_at = datetime('now') WHERE id = ?",
                 (v.new_sign_count, row["cred_row"]))
    conn.commit()
    provider_log(conn, "provider_login", row["email"], row["email"])
    conn.close()
    sign_in_provider(request, row)
    return JSONResponse({"ok": True})


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── Passkey enrollment (one-time link) ───────────────────────────────────────

@router.get("/enroll/{token}", response_class=HTMLResponse)
async def enroll_page(request: Request, token: str):
    conn = get_db()
    row = _enrollment(conn, token)
    conn.close()
    if row is None:
        return templates.TemplateResponse(request, "console/enroll.html", {"user": None}, status_code=404)
    return templates.TemplateResponse(request, "console/enroll.html", {"user": row, "token": token})


@router.post("/enroll/{token}/begin")
async def enroll_begin(request: Request, token: str):
    conn = get_db()
    row = _enrollment(conn, token)
    if row is None:
        conn.close()
        return JSONResponse({"error": "This enrollment link is invalid, used or expired."}, status_code=404)
    handle = row["webauthn_user_handle"] or bytes_to_base64url(secrets.token_bytes(32))
    conn.execute("UPDATE provider_users SET webauthn_user_handle = ? WHERE id = ?", (handle, row["id"]))
    conn.commit()
    existing = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"])) for r in conn.execute(
        "SELECT credential_id FROM provider_webauthn_credentials WHERE provider_user_id = ?", (row["id"],))]
    conn.close()
    options = generate_registration_options(
        rp_id=_rp_id(), rp_name=RP_NAME, user_name=row["email"], user_id=base64url_to_bytes(handle),
        user_display_name=row["display_name"] or row["email"], exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.PREFERRED),
    )
    request.session["console_enroll"] = {"token": _hash(token), "challenge": bytes_to_base64url(options.challenge)}
    return Response(content=options_to_json(options), media_type="application/json")


@router.post("/enroll/{token}/complete")
async def enroll_complete(request: Request, token: str):
    pending = request.session.pop("console_enroll", None) or {}
    conn = get_db()
    row = _enrollment(conn, token)
    if row is None or pending.get("token") != _hash(token):
        conn.close()
        return JSONResponse({"error": "This enrollment link is invalid, used or expired."}, status_code=404)
    body = await _json(request)
    if body is None:
        conn.close()
        return JSONResponse({"error": "Bad request."}, status_code=400)
    nickname = (body.pop("nickname", "") or "").strip()[:100]
    try:
        v = verify_registration_response(
            credential=body, expected_challenge=base64url_to_bytes(pending["challenge"]),
            expected_rp_id=_rp_id(), expected_origin=_origin(request),
        )
    except Exception as e:
        conn.close()
        return JSONResponse({"error": f"Could not register passkey: {e}"}, status_code=400)
    conn.execute(
        "INSERT INTO provider_webauthn_credentials (provider_user_id, credential_id, public_key, sign_count,"
        " transports, nickname) VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], bytes_to_base64url(v.credential_id), v.credential_public_key, v.sign_count,
         json.dumps((body.get("response") or {}).get("transports") or []), nickname or "Passkey"),
    )
    conn.execute("UPDATE provider_enrollments SET used_at = ? WHERE id = ?", (_now(), row["enrollment_id"]))
    conn.commit()
    provider_log(conn, "provider_passkey_enrolled", nickname or "Passkey", row["email"])
    user = conn.execute("SELECT * FROM provider_users WHERE id = ?", (row["id"],)).fetchone()
    conn.close()
    sign_in_provider(request, user)
    return JSONResponse({"ok": True})
