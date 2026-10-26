from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db, log_event
from app.notifications import (
    EVENT_LABELS, get_settings, get_event_states, send_test_email
)
from app.templates_config import templates

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _require_admin(current_user: dict) -> None:
    from app.permissions import require_perm
    require_perm(current_user, "manage_notifications")


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    settings = get_settings(current_user["tenant_id"])
    event_states = get_event_states(current_user["tenant_id"])
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "current_user": current_user,
            "settings": settings,
            "event_states": event_states,
            "event_labels": EVENT_LABELS,
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

    tid = current_user["tenant_id"]
    conn = get_db()
    existing = conn.execute("SELECT tenant_id FROM notification_settings WHERE tenant_id = ?", (tid,)).fetchone()
    if existing:
        # If password field was left blank, preserve the stored one
        if smtp_pass.strip():
            conn.execute(
                """UPDATE notification_settings SET enabled=?, smtp_host=?, smtp_port=?,
                   smtp_tls=?, smtp_user=?, smtp_pass=?, from_addr=?, to_addrs=?,
                   updated_at=datetime('now') WHERE tenant_id=?""",
                (is_enabled, smtp_host.strip(), port, tls, smtp_user.strip(),
                 smtp_pass, from_addr.strip(), to_addrs.strip(), tid),
            )
        else:
            conn.execute(
                """UPDATE notification_settings SET enabled=?, smtp_host=?, smtp_port=?,
                   smtp_tls=?, smtp_user=?, from_addr=?, to_addrs=?,
                   updated_at=datetime('now') WHERE tenant_id=?""",
                (is_enabled, smtp_host.strip(), port, tls, smtp_user.strip(),
                 from_addr.strip(), to_addrs.strip(), tid),
            )
    else:
        conn.execute(
            """INSERT INTO notification_settings
               (tenant_id, enabled, smtp_host, smtp_port, smtp_tls, smtp_user, smtp_pass,
                from_addr, to_addrs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, is_enabled, smtp_host.strip(), port, tls, smtp_user.strip(),
             smtp_pass, from_addr.strip(), to_addrs.strip()),
        )
    log_event(conn, "notification_settings_updated", smtp_host.strip(), current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()

    _set_flash(request, "success", "Notification settings saved.")
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/events/{event_type}")
async def notifications_event_toggle(
    event_type: str,
    request: Request,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    if event_type not in EVENT_LABELS:
        return JSONResponse({"ok": False, "error": "Unknown event."}, status_code=404)
    form = await request.form()
    enabled = 1 if form.get("enabled") == "1" else 0

    conn = get_db()
    tid = current_user["tenant_id"]
    conn.execute(
        "INSERT OR REPLACE INTO notification_events (tenant_id, event_type, enabled) VALUES (?, ?, ?)",
        (tid, event_type, enabled),
    )
    log_event(conn, "notification_triggers_updated",
              f"{event_type}={'on' if enabled else 'off'}", current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()

    return JSONResponse({"ok": True, "enabled": bool(enabled)})


@router.post("/notifications/health-alerts")
async def notifications_health_alerts(
    request: Request,
    mem_threshold: str = Form("90"),
    disk_threshold: str = Form("90"),
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)

    def _clamp(v: str, default: int = 90) -> int:
        try:
            return max(1, min(100, int(float(v))))
        except (ValueError, TypeError):
            return default

    mem = _clamp(mem_threshold)
    disk = _clamp(disk_threshold)

    tid = current_user["tenant_id"]
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO notification_settings (tenant_id) VALUES (?)", (tid,))
    conn.execute(
        "UPDATE notification_settings SET mem_threshold=?, disk_threshold=?,"
        " updated_at=datetime('now') WHERE tenant_id=?",
        (mem, disk, tid),
    )
    log_event(conn, "health_alert_thresholds_updated", f"mem={mem}% disk={disk}%", current_user["email"],
              tenant_id=tid)
    conn.commit()
    conn.close()

    _set_flash(request, "success", f"Health alert thresholds saved (memory {mem}%, disk {disk}%).")
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/test")
async def notifications_test(
    request: Request,
    current_user: dict = Depends(require_auth),
):
    _require_admin(current_user)
    tid = current_user["tenant_id"]
    settings = get_settings(tid)
    ok, err = send_test_email(tid, settings)
    conn = get_db()
    log_event(conn, "notification_test_sent", "" if ok else f"failed: {err}", current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()
    if ok:
        _set_flash(request, "success", "Test email sent successfully.")
    else:
        _set_flash(request, "error", f"Test failed: {err}")
    return RedirectResponse("/notifications", status_code=303)
