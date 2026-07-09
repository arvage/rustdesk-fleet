import json
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from app.auth import require_auth
from app.deps import get_db, log_event
from app.templates_config import templates
from app.webauthn_config import RP_NAME, get_origin, get_rp_id

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _credential_descriptors(conn, user_id: int) -> list[PublicKeyCredentialDescriptor]:
    rows = conn.execute(
        "SELECT credential_id FROM webauthn_credentials WHERE user_id = ?", (user_id,)
    ).fetchall()
    return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"])) for r in rows]


# ── Registration (self-service, from /account) ─────────────────────────────

@router.post("/account/passkeys/register/begin")
async def passkey_register_begin(request: Request, current_user: dict = Depends(require_auth)):
    conn = get_db()
    row = conn.execute(
        "SELECT webauthn_user_handle FROM users WHERE id = ?", (current_user["id"],)
    ).fetchone()
    handle = row["webauthn_user_handle"] if row else None
    if not handle:
        handle = bytes_to_base64url(secrets.token_bytes(32))
        conn.execute(
            "UPDATE users SET webauthn_user_handle = ? WHERE id = ?", (handle, current_user["id"])
        )
        conn.commit()

    exclude = _credential_descriptors(conn, current_user["id"])
    conn.close()

    options = generate_registration_options(
        rp_id=get_rp_id(request),
        rp_name=RP_NAME,
        user_name=current_user["email"],
        user_id=base64url_to_bytes(handle),
        user_display_name=current_user["name"] or current_user["email"],
        exclude_credentials=exclude,
    )
    request.session["webauthn_challenge"] = bytes_to_base64url(options.challenge)
    request.session["webauthn_challenge_purpose"] = "register"
    return Response(content=options_to_json(options), media_type="application/json")


@router.post("/account/passkeys/register/complete")
async def passkey_register_complete(request: Request, current_user: dict = Depends(require_auth)):
    body = await request.json()
    nickname = (body.pop("nickname", "") or "").strip()[:100]

    challenge = request.session.pop("webauthn_challenge", None)
    purpose = request.session.pop("webauthn_challenge_purpose", None)
    if not challenge or purpose != "register":
        return JSONResponse({"error": "Registration session expired — try again."}, status_code=400)

    try:
        verification = verify_registration_response(
            credential=body,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=get_rp_id(request),
            expected_origin=get_origin(request),
        )
    except Exception as e:
        return JSONResponse({"error": f"Could not register passkey: {e}"}, status_code=400)

    transports = (body.get("response") or {}).get("transports") or []

    conn = get_db()
    conn.execute(
        "INSERT INTO webauthn_credentials "
        "(user_id, credential_id, public_key, sign_count, transports, nickname) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            current_user["id"],
            bytes_to_base64url(verification.credential_id),
            verification.credential_public_key,
            verification.sign_count,
            json.dumps(transports),
            nickname or "Passkey",
        ),
    )
    log_event(conn, "passkey_registered", nickname or "Passkey", current_user["email"])
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@router.post("/account/passkeys/{cred_id}/delete")
async def passkey_delete(request: Request, cred_id: int, current_user: dict = Depends(require_auth)):
    conn = get_db()
    cred = conn.execute(
        "SELECT nickname FROM webauthn_credentials WHERE id = ? AND user_id = ?",
        (cred_id, current_user["id"]),
    ).fetchone()
    if not cred:
        conn.close()
        _set_flash(request, "error", "Passkey not found.")
        return RedirectResponse("/account", status_code=303)

    auth_row = conn.execute(
        "SELECT auth_method FROM users WHERE id = ?", (current_user["id"],)
    ).fetchone()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM webauthn_credentials WHERE user_id = ?", (current_user["id"],)
    ).fetchone()[0]
    if remaining <= 1 and auth_row["auth_method"] in ("passkey", "both"):
        conn.close()
        _set_flash(request, "error", "Switch to password-only before removing your last passkey.")
        return RedirectResponse("/account", status_code=303)

    conn.execute(
        "DELETE FROM webauthn_credentials WHERE id = ? AND user_id = ?", (cred_id, current_user["id"])
    )
    log_event(conn, "passkey_deleted", cred["nickname"], current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Passkey removed.")
    return RedirectResponse("/account", status_code=303)


@router.post("/account/auth-method")
async def account_set_auth_method(
    request: Request,
    auth_method: str = Form(...),
    current_user: dict = Depends(require_auth),
):
    if auth_method not in ("password", "passkey", "both"):
        _set_flash(request, "error", "Invalid sign-in mode.")
        return RedirectResponse("/account", status_code=303)

    conn = get_db()
    row = conn.execute(
        "SELECT auth_method FROM users WHERE id = ?", (current_user["id"],)
    ).fetchone()
    old = row["auth_method"] if row else "password"

    if auth_method in ("passkey", "both"):
        count = conn.execute(
            "SELECT COUNT(*) FROM webauthn_credentials WHERE user_id = ?", (current_user["id"],)
        ).fetchone()[0]
        if count == 0:
            conn.close()
            _set_flash(request, "error", "Register a passkey before switching to this sign-in mode.")
            return RedirectResponse("/account", status_code=303)

    conn.execute("UPDATE users SET auth_method = ? WHERE id = ?", (auth_method, current_user["id"]))
    log_event(conn, "auth_method_changed", f"{old} -> {auth_method}", current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Sign-in method updated.")
    return RedirectResponse("/account", status_code=303)


# ── Login-time assertion (pre-auth, gated by pending_2fa_user_id) ──────────

@router.get("/login/webauthn", response_class=HTMLResponse)
async def login_webauthn_get(request: Request):
    if not request.session.get("pending_2fa_user_id"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login_webauthn.html", {"error": None})


@router.post("/login/webauthn/begin")
async def login_webauthn_begin(request: Request):
    user_id = request.session.get("pending_2fa_user_id")
    if not user_id:
        return JSONResponse({"error": "No pending sign-in."}, status_code=400)

    conn = get_db()
    allow = _credential_descriptors(conn, user_id)
    conn.close()
    if not allow:
        return JSONResponse({"error": "No passkeys registered for this account."}, status_code=400)

    options = generate_authentication_options(rp_id=get_rp_id(request), allow_credentials=allow)
    request.session["webauthn_challenge"] = bytes_to_base64url(options.challenge)
    request.session["webauthn_challenge_purpose"] = "login"
    return Response(content=options_to_json(options), media_type="application/json")


@router.post("/login/webauthn/complete")
async def login_webauthn_complete(request: Request):
    user_id = request.session.get("pending_2fa_user_id")
    challenge = request.session.pop("webauthn_challenge", None)
    purpose = request.session.pop("webauthn_challenge_purpose", None)
    if not user_id or not challenge or purpose != "login":
        return JSONResponse({"error": "Sign-in session expired — start over."}, status_code=400)

    body = await request.json()
    posted_cred_id = body.get("id")

    conn = get_db()
    cred_row = conn.execute(
        "SELECT id, public_key, sign_count FROM webauthn_credentials "
        "WHERE credential_id = ? AND user_id = ?",
        (posted_cred_id, user_id),
    ).fetchone()
    if not cred_row:
        conn.close()
        return JSONResponse({"error": "Passkey not recognized for this account."}, status_code=400)

    try:
        verification = verify_authentication_response(
            credential=body,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=get_rp_id(request),
            expected_origin=get_origin(request),
            credential_public_key=cred_row["public_key"],
            credential_current_sign_count=cred_row["sign_count"],
        )
    except Exception as e:
        conn.close()
        return JSONResponse({"error": f"Could not verify passkey: {e}"}, status_code=400)

    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.execute(
        "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = datetime('now') WHERE id = ?",
        (verification.new_sign_count, cred_row["id"]),
    )
    log_event(conn, "passkey_login", user["email"], user["email"])
    conn.commit()
    conn.close()

    request.session.pop("pending_2fa_user_id", None)
    request.session["user_id"] = user["id"]
    request.session["user_role"] = user["role"]
    request.session["user_name"] = user["display_name"] or user["email"]
    request.session["user_email"] = user["email"]
    return JSONResponse({"ok": True})
