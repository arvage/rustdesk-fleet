from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db
from app.notifications import (
    EVENT_LABELS, get_settings, get_event_states, get_log, send_test_email
)
from app.templates_config import templates

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _require_admin(current_user: dict) -> None:
    if current_user.get("role") != "admin":
        raise PermissionError("Admin access required.")


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    settings = get_settings()
    event_states = get_event_states()
    log = get_log(50)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "current_user": current_user,
            "settings": settings,
            "event_states": event_states,
            "event_labels": EVENT_LABELS,
            "log": log,
        },
    )


@router.post("/notifications/settings")
async def notifications_save(
    request: Request,
    enabled: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_tls: str = Form("starttls"),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    from_addr: str = Form(""),
    to_addrs: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)

    port = 587
    try:
        port = int(smtp_port)
    except (ValueError, TypeError):
        pass

    tls = smtp_tls if smtp_tls in ("starttls", "ssl", "none") else "starttls"
    is_enabled = 1 if enabled == "1" else 0

    conn = get_db()
    existing = conn.execute("SELECT id FROM notification_settings WHERE id = 1").fetchone()
    if existing:
        # If password field was left blank, preserve the stored one
        if smtp_pass.strip():
            conn.execute(
                """UPDATE notification_settings SET enabled=?, smtp_host=?, smtp_port=?,
                   smtp_tls=?, smtp_user=?, smtp_pass=?, from_addr=?, to_addrs=?,
                   updated_at=datetime('now') WHERE id=1""",
                (is_enabled, smtp_host.strip(), port, tls, smtp_user.strip(),
                 smtp_pass, from_addr.strip(), to_addrs.strip()),
            )
        else:
            conn.execute(
                """UPDATE notification_settings SET enabled=?, smtp_host=?, smtp_port=?,
                   smtp_tls=?, smtp_user=?, from_addr=?, to_addrs=?,
                   updated_at=datetime('now') WHERE id=1""",
                (is_enabled, smtp_host.strip(), port, tls, smtp_user.strip(),
                 from_addr.strip(), to_addrs.strip()),
            )
    else:
        conn.execute(
            """INSERT INTO notification_settings
               (id, enabled, smtp_host, smtp_port, smtp_tls, smtp_user, smtp_pass,
                from_addr, to_addrs)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (is_enabled, smtp_host.strip(), port, tls, smtp_user.strip(),
             smtp_pass, from_addr.strip(), to_addrs.strip()),
        )
    conn.commit()
    conn.close()

    _set_flash(request, "success", "Notification settings saved.")
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/events")
async def notifications_events(
    request: Request,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    form = await request.form()

    conn = get_db()
    for et in EVENT_LABELS:
        enabled = 1 if form.get(et) == "1" else 0
        conn.execute(
            "INSERT OR REPLACE INTO notification_events (event_type, enabled) VALUES (?, ?)",
            (et, enabled),
        )
    conn.commit()
    conn.close()

    _set_flash(request, "success", "Event triggers updated.")
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/test")
async def notifications_test(
    request: Request,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    settings = get_settings()
    ok, err = send_test_email(settings)
    if ok:
        _set_flash(request, "success", "Test email sent successfully.")
    else:
        _set_flash(request, "error", f"Test failed: {err}")
    return RedirectResponse("/notifications", status_code=303)
