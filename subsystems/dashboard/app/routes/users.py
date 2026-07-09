import json
import secrets
import string

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db, log_event
from app.notifications import fire_notification, send_credentials_email, send_mfa_reminder_email
from app.templates_config import templates

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _require_admin(current_user: dict) -> None:
    if current_user["role"] != "admin":
        raise PermissionError("Admin access required.")


def _gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _account_extras(user_id: int) -> dict:
    """auth_method + registered passkeys for rendering account.html."""
    conn = get_db()
    auth_row = conn.execute("SELECT auth_method FROM users WHERE id = ?", (user_id,)).fetchone()
    passkeys = conn.execute(
        "SELECT id, nickname, created_at, last_used_at FROM webauthn_credentials "
        "WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ).fetchall()
    conn.close()
    return {
        "auth_method": auth_row["auth_method"] if auth_row else "password",
        "passkeys": passkeys,
    }


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, current_user: dict = Depends(require_auth)):
    _require_admin(current_user)
    conn = get_db()
    users = conn.execute(
        "SELECT id, email, display_name, role, created_at, auth_method FROM users ORDER BY created_at"
    ).fetchall()
    groups = conn.execute(
        "SELECT id, slug, display_name FROM client_groups ORDER BY display_name"
    ).fetchall()
    access_rows = conn.execute(
        "SELECT user_id, group_id FROM user_group_access"
    ).fetchall()
    smtp_row = conn.execute(
        "SELECT enabled, smtp_host FROM notification_settings WHERE id = 1"
    ).fetchone()
    passkey_rows = conn.execute(
        "SELECT id, user_id, nickname, created_at, last_used_at FROM webauthn_credentials ORDER BY created_at"
    ).fetchall()
    conn.close()

    access_by_user: dict[int, set[int]] = {}
    for row in access_rows:
        access_by_user.setdefault(row["user_id"], set()).add(row["group_id"])

    passkeys_by_user: dict[int, list[dict]] = {}
    for row in passkey_rows:
        passkeys_by_user.setdefault(row["user_id"], []).append(dict(row))

    users_with_access = [
        {
            **dict(u),
            "group_ids": access_by_user.get(u["id"], set()),
            "passkeys": passkeys_by_user.get(u["id"], []),
        }
        for u in users
    ]

    smtp_ready = bool(smtp_row and smtp_row["enabled"] and smtp_row["smtp_host"])

    groups_json = json.dumps([
        {"id": g["id"], "slug": g["slug"], "name": g["display_name"]}
        for g in groups
    ])
    access_json = json.dumps({
        str(u["id"]): sorted(u["group_ids"])
        for u in users_with_access
    })
    passkeys_json = json.dumps({
        str(u["id"]): [
            {"id": p["id"], "nickname": p["nickname"], "created_at": p["created_at"], "last_used_at": p["last_used_at"]}
            for p in u["passkeys"]
        ]
        for u in users_with_access
    })

    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "users": users_with_access,
            "groups": groups,
            "current_user": current_user,
            "smtp_ready": smtp_ready,
            "groups_json": groups_json,
            "access_json": access_json,
            "passkeys_json": passkeys_json,
        },
    )


@router.post("/users")
async def user_create(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(...),
    role: str = Form("tech"),
    password_mode: str = Form("auto"),
    password: str = Form(""),
    send_email: str = Form(""),
    group_ids: list[int] = Form(default=[]),
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    email = email.lower().strip()
    display_name = display_name.strip()

    if role not in ("tech", "admin"):
        _set_flash(request, "error", "Invalid role.")
        return RedirectResponse("/users", status_code=303)

    if password_mode == "manual":
        chosen = password.strip()
        if len(chosen) < 10:
            _set_flash(request, "error", "Password must be at least 10 characters.")
            return RedirectResponse("/users", status_code=303)
        final_password = chosen
    else:
        final_password = _gen_password()

    pw_hash = bcrypt.hashpw(final_password.encode(), bcrypt.gensalt(rounds=12)).decode()

    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        _set_flash(request, "error", f"A user with email {email} already exists.")
        return RedirectResponse("/users", status_code=303)

    conn.execute(
        "INSERT INTO users (email, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
        (email, display_name, role, pw_hash),
    )
    conn.commit()
    user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if role == "tech" and group_ids:
        for gid in group_ids:
            conn.execute(
                "INSERT OR IGNORE INTO user_group_access (user_id, group_id) VALUES (?, ?)",
                (user_id, gid),
            )
        conn.commit()

    # Resolve group names for the notification context
    group_names = []
    if group_ids:
        placeholders = ",".join("?" * len(group_ids))
        rows = conn.execute(
            f"SELECT display_name FROM client_groups WHERE id IN ({placeholders})",
            group_ids,
        ).fetchall()
        group_names = [r["display_name"] for r in rows]

    log_event(conn, "user_created", email, current_user["email"])
    conn.commit()
    conn.close()

    email_sent = False
    if send_email == "1":
        ok, _ = send_credentials_email(email, display_name, final_password)
        email_sent = ok

    fire_notification("user_created", {
        "email": email,
        "display_name": display_name or email,
        "role": role,
        "groups": ", ".join(group_names) if group_names else "None",
        "created_by": current_user["email"],
    })

    request.session["new_user_pw"] = {
        "email": email,
        "password": final_password,
        "email_sent": email_sent,
    }
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role")
async def user_set_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    if role not in ("tech", "admin"):
        _set_flash(request, "error", "Invalid role.")
        return RedirectResponse("/users", status_code=303)
    if user_id == current_user["id"]:
        _set_flash(request, "error", "You cannot change your own role.")
        return RedirectResponse("/users", status_code=303)

    conn = get_db()
    target = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    log_event(conn, "user_role_changed", f"{target['email'] if target else user_id} -> {role}", current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Role updated.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/access")
async def user_set_access(
    request: Request,
    user_id: int,
    group_ids: list[int] = Form(default=[]),
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    conn = get_db()
    target = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.execute("DELETE FROM user_group_access WHERE user_id = ?", (user_id,))
    for gid in group_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_group_access (user_id, group_id) VALUES (?, ?)",
            (user_id, gid),
        )
    log_event(
        conn, "user_access_updated",
        f"{target['email'] if target else user_id} groups={sorted(group_ids)}",
        current_user["email"],
    )
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Group access updated.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/reset-password")
async def user_reset_password(
    request: Request,
    user_id: int,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    temp_password = _gen_password()
    pw_hash = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt(rounds=12)).decode()

    conn = get_db()
    user = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        _set_flash(request, "error", "User not found.")
        return RedirectResponse("/users", status_code=303)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
    log_event(conn, "user_password_reset", user["email"], current_user["email"])
    conn.commit()
    conn.close()

    request.session["new_user_pw"] = {"email": user["email"], "password": temp_password}
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/edit")
async def user_edit(
    request: Request,
    user_id: int,
    email: str = Form(...),
    display_name: str = Form(...),
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    email = email.lower().strip()
    display_name = display_name.strip()

    if not email or "@" not in email:
        _set_flash(request, "error", "Enter a valid email address.")
        return RedirectResponse("/users", status_code=303)

    conn = get_db()
    conflict = conn.execute(
        "SELECT id FROM users WHERE email = ? AND id != ?", (email, user_id)
    ).fetchone()
    if conflict:
        conn.close()
        _set_flash(request, "error", f"Email {email} is already in use by another account.")
        return RedirectResponse("/users", status_code=303)

    conn.execute(
        "UPDATE users SET email = ?, display_name = ? WHERE id = ?",
        (email, display_name, user_id),
    )
    log_event(conn, "user_updated", email, current_user["email"])
    conn.commit()
    conn.close()

    if user_id == current_user["id"]:
        request.session["user_name"] = display_name or email

    _set_flash(request, "success", "User updated.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def user_delete(
    request: Request,
    user_id: int,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    if user_id == current_user["id"]:
        _set_flash(request, "error", "You cannot delete your own account.")
        return RedirectResponse("/users", status_code=303)

    conn = get_db()
    deleted = conn.execute(
        "SELECT email, display_name, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.execute("DELETE FROM user_group_access WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if deleted:
        log_event(conn, "user_deleted", deleted["email"], current_user["email"])
    conn.commit()
    conn.close()

    if deleted:
        fire_notification("user_deleted", {
            "email": deleted["email"],
            "display_name": deleted["display_name"] or deleted["email"],
            "role": deleted["role"],
            "deleted_by": current_user["email"],
        })

    _set_flash(request, "success", "User deleted.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/passkeys/{cred_id}/revoke")
async def user_revoke_passkey(
    request: Request,
    user_id: int,
    cred_id: int,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    conn = get_db()
    target = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    cred = conn.execute(
        "SELECT nickname FROM webauthn_credentials WHERE id = ? AND user_id = ?", (cred_id, user_id)
    ).fetchone()
    if not target or not cred:
        conn.close()
        _set_flash(request, "error", "Passkey not found.")
        return RedirectResponse("/users", status_code=303)

    conn.execute("DELETE FROM webauthn_credentials WHERE id = ? AND user_id = ?", (cred_id, user_id))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM webauthn_credentials WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    if remaining == 0:
        # Never leave an account requiring a passkey with none registered.
        conn.execute("UPDATE users SET auth_method = 'password' WHERE id = ?", (user_id,))
    log_event(
        conn, "passkey_revoked_by_admin",
        f"{target['email']} cred={cred['nickname'] or cred_id}",
        current_user["email"],
    )
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Passkey revoked.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/passkeys/reset")
async def user_reset_passkeys(
    request: Request,
    user_id: int,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    conn = get_db()
    target = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        _set_flash(request, "error", "User not found.")
        return RedirectResponse("/users", status_code=303)

    conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (user_id,))
    conn.execute("UPDATE users SET auth_method = 'password' WHERE id = ?", (user_id,))
    log_event(conn, "auth_method_force_reset", target["email"], current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Passkeys reset — this account now signs in with a password.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/mfa-reminder")
async def user_mfa_reminder(
    request: Request,
    user_id: int,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    conn = get_db()
    target = conn.execute(
        "SELECT email, display_name FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if not target:
        _set_flash(request, "error", "User not found.")
        return RedirectResponse("/users", status_code=303)

    ok, err = send_mfa_reminder_email(target["email"], target["display_name"])

    conn = get_db()
    log_event(
        conn, "mfa_reminder_sent" if ok else "mfa_reminder_failed",
        target["email"], current_user["email"],
    )
    conn.commit()
    conn.close()

    if ok:
        _set_flash(request, "success", f"Reminder email sent to {target['email']}.")
    else:
        _set_flash(request, "error", f"Could not send reminder: {err}")
    return RedirectResponse("/users", status_code=303)


@router.get("/account", response_class=HTMLResponse)
async def account_get(request: Request, current_user: dict = Depends(require_auth)):
    conn = get_db()
    user = conn.execute(
        "SELECT email, display_name FROM users WHERE id = ?", (current_user["id"],)
    ).fetchone()
    conn.close()
    return templates.TemplateResponse(
        request, "account.html",
        {
            "current_user": current_user, "user": user, "error": None,
            "pw_success": False, "profile_success": False,
            **_account_extras(current_user["id"]),
        },
    )


@router.post("/account/profile")
async def account_update_profile(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(...),
    current_user: dict = Depends(require_auth),
):
    email = email.lower().strip()
    display_name = display_name.strip()

    if not email or "@" not in email:
        conn = get_db()
        user = conn.execute("SELECT email, display_name FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        conn.close()
        return templates.TemplateResponse(
            request, "account.html",
            {
                "current_user": current_user, "user": user, "error": "Enter a valid email address.",
                "pw_success": False, "profile_success": False,
                **_account_extras(current_user["id"]),
            },
            status_code=400,
        )

    conn = get_db()
    conflict = conn.execute(
        "SELECT id FROM users WHERE email = ? AND id != ?", (email, current_user["id"])
    ).fetchone()
    if conflict:
        user = conn.execute("SELECT email, display_name FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        conn.close()
        return templates.TemplateResponse(
            request, "account.html",
            {
                "current_user": current_user, "user": user, "error": "That email is already in use.",
                "pw_success": False, "profile_success": False,
                **_account_extras(current_user["id"]),
            },
            status_code=400,
        )

    conn.execute(
        "UPDATE users SET email = ?, display_name = ? WHERE id = ?",
        (email, display_name, current_user["id"]),
    )
    log_event(conn, "profile_updated", email, current_user["email"])
    conn.commit()
    user = conn.execute("SELECT email, display_name FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    conn.close()

    request.session["user_name"] = display_name or email
    return templates.TemplateResponse(
        request, "account.html",
        {
            "current_user": {**current_user, "name": display_name or email}, "user": user,
            "error": None, "pw_success": False, "profile_success": True,
            **_account_extras(current_user["id"]),
        },
    )


@router.post("/account/password")
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password2: str = Form(...),
    current_user: dict = Depends(require_auth),
):
    conn = get_db()
    db_user = conn.execute(
        "SELECT email, display_name, password_hash FROM users WHERE id = ?", (current_user["id"],)
    ).fetchone()
    conn.close()

    def _pw_error(msg: str):
        return templates.TemplateResponse(
            request, "account.html",
            {
                "current_user": current_user, "user": db_user, "error": msg,
                "pw_success": False, "profile_success": False,
                **_account_extras(current_user["id"]),
            },
            status_code=400,
        )

    if not db_user or not bcrypt.checkpw(current_password.encode(), db_user["password_hash"].encode()):
        return _pw_error("Current password is incorrect.")
    if len(new_password) < 10:
        return _pw_error("New password must be at least 10 characters.")
    if new_password != new_password2:
        return _pw_error("New passwords do not match.")

    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, current_user["id"]))
    log_event(conn, "password_changed", current_user["email"], current_user["email"])
    conn.commit()
    conn.close()

    return templates.TemplateResponse(
        request, "account.html",
        {
            "current_user": current_user, "user": db_user, "error": None,
            "pw_success": True, "profile_success": False,
            **_account_extras(current_user["id"]),
        },
    )
