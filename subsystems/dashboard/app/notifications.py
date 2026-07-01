"""
notifications.py — email notification engine for RustDesk Fleet.

Call fire_notification(event_type, context) from any route handler to
send a non-blocking email.  The send runs in a daemon thread so it never
delays the HTTP response.

Supported event types (EVENT_LABELS keys):
  device_registered  — a new device appeared in the fleet
  device_deleted     — a device was removed
  installer_built    — an installer was compiled for a group
"""

from __future__ import annotations

import smtplib
import sqlite3
import threading
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

DB_PATH = Path("/opt/rustdesk-fleet/fleet.sqlite3")

# ── Event registry ────────────────────────────────────────────────────────────

EVENT_LABELS: dict[str, str] = {
    "device_registered": "New device registered",
    "device_deleted":    "Device deleted",
    "installer_built":   "Installer built",
}

_SUBJECTS: dict[str, str] = {
    "device_registered": "New device registered — {rustdesk_id}",
    "device_deleted":    "Device removed — {rustdesk_id}",
    "installer_built":   "Installer built — {group_name} ({platform})",
}

# ── Settings helpers ──────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_settings() -> dict:
    conn = _get_db()
    row = conn.execute("SELECT * FROM notification_settings WHERE id = 1").fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "enabled": 0, "smtp_host": "", "smtp_port": 587, "smtp_tls": "starttls",
        "smtp_user": "", "smtp_pass": "", "from_addr": "", "to_addrs": "",
    }


def get_event_states() -> dict[str, bool]:
    """Return {event_type: enabled} for all known events."""
    conn = _get_db()
    rows = conn.execute("SELECT event_type, enabled FROM notification_events").fetchall()
    conn.close()
    stored = {r["event_type"]: bool(r["enabled"]) for r in rows}
    return {et: stored.get(et, True) for et in EVENT_LABELS}


def get_log(limit: int = 50) -> list[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM notification_log ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Email templates ───────────────────────────────────────────────────────────

_BASE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{margin:0;padding:24px 0;background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
  .wrap{{max-width:540px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)}}
  .hdr{{background:#1E293B;padding:22px 28px}}
  .hdr h1{{color:#fff;margin:0;font-size:16px;font-weight:600;letter-spacing:.01em}}
  .hdr p{{color:#94A3B8;margin:4px 0 0;font-size:12px}}
  .body{{padding:24px 28px}}
  .body p{{color:#334155;font-size:14px;line-height:1.6;margin:0 0 16px}}
  table.kv{{width:100%;border-collapse:collapse;font-size:13px;margin:16px 0}}
  table.kv td{{padding:9px 0;border-bottom:1px solid #F1F5F9;vertical-align:top}}
  table.kv td:first-child{{color:#64748B;width:150px;padding-right:12px;white-space:nowrap}}
  table.kv td:last-child{{color:#0F172A;font-weight:500;word-break:break-all}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}}
  .badge-ok{{background:#D1FAE5;color:#065F46}}
  .badge-warn{{background:#FEF3C7;color:#92400E}}
  .badge-err{{background:#FEE2E2;color:#7F1D1D}}
  .badge-info{{background:#DBEAFE;color:#1E3A8A}}
  .ftr{{background:#F8FAFC;padding:14px 28px;border-top:1px solid #E2E8F0}}
  .ftr p{{color:#94A3B8;font-size:11px;margin:0}}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>RustDesk Fleet</h1>
    <p>{subtitle}</p>
  </div>
  <div class="body">
    {body}
  </div>
  <div class="ftr">
    <p>RustDesk Fleet Management &nbsp;·&nbsp; {ts} (Los Angeles)</p>
  </div>
</div>
</body>
</html>"""


def _now_la() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).strftime(
        "%Y-%m-%d %H:%M"
    )


def _kv(*pairs: tuple[str, str]) -> str:
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in pairs)
    return f'<table class="kv">{rows}</table>'


def _render(event_type: str, context: dict) -> tuple[str, str]:
    """Return (subject, html_body) for the given event and context."""
    ts = _now_la()
    subj = _SUBJECTS.get(event_type, event_type).format_map(context)

    if event_type == "device_registered":
        body = (
            "<p>A new device has been registered with your RustDesk Fleet server.</p>"
            + _kv(
                ("RustDesk ID", context.get("rustdesk_id", "—")),
                ("Label", context.get("label") or "—"),
                ("Group", context.get("group_name") or "—"),
                ("IP address", context.get("ip") or "—"),
                ("Registered at", context.get("registered_at") or ts),
            )
        )
        subtitle = "New device registered"

    elif event_type == "device_deleted":
        body = (
            "<p>A device has been removed from your RustDesk Fleet.</p>"
            + _kv(
                ("RustDesk ID", context.get("rustdesk_id", "—")),
                ("Label", context.get("label") or "—"),
                ("Group", context.get("group_name") or "—"),
                ("Last seen", context.get("last_seen") or "—"),
            )
        )
        subtitle = "Device removed from fleet"

    elif event_type == "installer_built":
        body = (
            "<p>A new installer has been generated and is ready to deploy.</p>"
            + _kv(
                ("Group", context.get("group_name", "—")),
                ("Platform", context.get("platform", "—")),
                ("RustDesk version", context.get("version", "—")),
                ("SHA-256 (first 16)", (context.get("sha256") or "")[:16] + "…"),
            )
        )
        subtitle = "Installer ready"

    else:
        body = f"<p>Event: <strong>{event_type}</strong></p>"
        subtitle = event_type

    html = _BASE.format(subtitle=subtitle, body=body, ts=ts)
    return subj, html


# ── SMTP send ─────────────────────────────────────────────────────────────────

def _send_sync(settings: dict, to_list: list[str], subject: str, html: str) -> None:
    """Blocking send — must be called from a thread, not the async event loop."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings["from_addr"]
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(html, "html", "utf-8"))

    host = settings["smtp_host"]
    port = int(settings["smtp_port"])
    tls  = settings["smtp_tls"]
    user = settings.get("smtp_user", "")
    pw   = settings.get("smtp_pass", "")

    if tls == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=10)
    else:
        smtp = smtplib.SMTP(host, port, timeout=10)
        if tls == "starttls":
            smtp.starttls()

    if user:
        smtp.login(user, pw)

    smtp.sendmail(settings["from_addr"], to_list, msg.as_string())
    smtp.quit()


def _log(event_type: str, subject: str, recipients: str, status: str, error: str = "") -> None:
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO notification_log (event_type, subject, recipients, status, error)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_type, subject, recipients, status, error or None),
        )
        # Cap log to 200 rows
        conn.execute(
            "DELETE FROM notification_log WHERE id NOT IN"
            " (SELECT id FROM notification_log ORDER BY created_at DESC LIMIT 200)"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def fire_notification(event_type: str, context: dict) -> None:
    """Fire an email notification in a background daemon thread.

    Returns immediately; never raises.
    """
    def _worker():
        try:
            settings = get_settings()
            if not settings.get("enabled"):
                return
            states = get_event_states()
            if not states.get(event_type, True):
                return

            to_list = [a.strip() for a in settings.get("to_addrs", "").split(",") if a.strip()]
            if not to_list or not settings.get("smtp_host"):
                return

            subject, html = _render(event_type, context)
            _send_sync(settings, to_list, subject, html)
            _log(event_type, subject, ", ".join(to_list), "sent")
        except Exception as exc:
            try:
                subject = _SUBJECTS.get(event_type, event_type)
                _log(event_type, subject, "", "failed", str(exc)[:500])
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def send_test_email(settings: dict) -> tuple[bool, str]:
    """Send a test email with the given settings dict.  Returns (ok, error_msg)."""
    to_list = [a.strip() for a in settings.get("to_addrs", "").split(",") if a.strip()]
    if not to_list:
        return False, "No recipient addresses configured."
    if not settings.get("smtp_host"):
        return False, "SMTP host is required."
    if not settings.get("from_addr"):
        return False, "From address is required."

    _, html = _render("device_registered", {
        "rustdesk_id": "123 456 789",
        "label": "Test Device",
        "group_name": "Example Group",
        "ip": "203.0.113.42",
        "registered_at": _now_la(),
    })
    subject = "RustDesk Fleet — test notification"

    try:
        _send_sync(settings, to_list, subject, html)
        _log("test", subject, ", ".join(to_list), "sent")
        return True, ""
    except Exception as exc:
        _log("test", subject, ", ".join(to_list), "failed", str(exc)[:500])
        return False, str(exc)
