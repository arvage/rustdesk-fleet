"""
notifications.py — email notification engine for RustDesk Fleet.

Call fire_notification(event_type, context) from any route handler to
send a non-blocking email.  The send runs in a daemon thread so it never
delays the HTTP response.

Supported event types (EVENT_LABELS keys):
  device_registered  — a new device appeared in the fleet
  device_deleted     — a device was removed
  device_offline     — a previously-online device stopped responding
  device_online      — a previously-offline device came back online
  installer_built    — an installer was compiled for a group
  installer_deleted  — an installer build was deleted
  server_update_available — a newer rustdesk-server release is on GitHub
  installer_downloaded — a client downloaded an installer via a shared link
  client_update_available — a newer RustDesk client release is on GitHub
"""

from __future__ import annotations

import smtplib
import sqlite3
import threading
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app import paths

DB_PATH = paths.FLEET_DB

# ── Event registry ────────────────────────────────────────────────────────────

EVENT_LABELS: dict[str, str] = {
    "device_registered": "New device registered",
    "device_deleted":    "Device deleted",
    "device_offline":    "Device went offline",
    "device_online":     "Device came online",
    "installer_built":   "Installer built",
    "installer_deleted": "Installer deleted",
    "user_created":      "User account created",
    "user_deleted":      "User account deleted",
    "server_update_available": "New server version available",
    "installer_downloaded": "Client downloaded installer via link",
    "client_update_available": "New client version available",
    "memory_high":       "Server memory above threshold",
    "disk_high":         "Server disk above threshold",
}

_SUBJECTS: dict[str, str] = {
    "device_registered": "New device registered — {rustdesk_id}",
    "device_deleted":    "Device removed — {rustdesk_id}",
    "device_offline":    "Device went offline — {rustdesk_id}",
    "device_online":     "Device came online — {name}",
    "installer_built":   "Installer built — {group_name} ({platform})",
    "installer_deleted": "Installer deleted — {group_name} ({platform})",
    "user_created":      "New user created — {email}",
    "user_deleted":      "User deleted — {email}",
    "server_update_available": "RustDesk server {latest_version} available",
    "installer_downloaded": "Installer downloaded — {group_name} ({platform})",
    "client_update_available": "RustDesk client {latest_version} available",
    "memory_high":       "Server memory high — {pct}% used",
    "disk_high":         "Server disk high — {pct}% used",
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
        "mem_threshold": 90, "disk_threshold": 90,
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

# ── Email rendering ───────────────────────────────────────────────────────────
# Wording and look are editable under Admin → Email templates; see
# app/email_templates.py for the catalogue and defaults.

def _now_la() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).strftime(
        "%Y-%m-%d %H:%M"
    )


def _render(event_type: str, context: dict) -> tuple[str, str]:
    """Return (subject, html_body) for the given event and context."""
    from app.email_templates import render
    subject, html, _ = render(event_type, context)
    return subject, html


# ── SMTP send ─────────────────────────────────────────────────────────────────

def _send_sync(
    settings: dict, to_list: list[str], subject: str, html: str, reply_to: str = ""
) -> None:
    """Blocking send — must be called from a thread, not the async event loop."""
    from app.email_templates import LOGO_CID, logo_bytes
    logo = logo_bytes() if f"cid:{LOGO_CID}" in html else None
    if logo:
        # multipart/related so the logo travels inline with the message
        # (shows without the recipient having to allow remote images).
        msg = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)
        img = MIMEImage(logo, "png")
        img.add_header("Content-ID", f"<{LOGO_CID}>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        msg.attach(img)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html, "html", "utf-8"))
    msg["Subject"] = subject
    msg["From"] = settings["from_addr"]
    msg["To"] = ", ".join(to_list)
    if reply_to:
        msg["Reply-To"] = reply_to

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


def send_credentials_email(to_email: str, display_name: str, password: str) -> tuple[bool, str]:
    """Send a welcome/credentials email to a new user. Returns (ok, error_msg)."""
    settings = get_settings()
    if not settings.get("enabled") or not settings.get("smtp_host"):
        return False, "Notifications not configured or disabled."
    if not settings.get("from_addr"):
        return False, "No from address configured."
    try:
        subject, html = _render("user_credentials", {
            "name": display_name or to_email, "email": to_email, "password": password,
        })
        _send_sync(settings, [to_email], subject, html)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def send_mfa_reminder_email(to_email: str, display_name: str) -> tuple[bool, str]:
    """Nudge an existing user who hasn't set up a passkey yet. Returns (ok, error_msg)."""
    settings = get_settings()
    if not settings.get("enabled") or not settings.get("smtp_host"):
        return False, "Notifications not configured or disabled."
    if not settings.get("from_addr"):
        return False, "No from address configured."
    try:
        subject, html = _render("mfa_reminder", {"name": display_name or to_email, "email": to_email})
        _send_sync(settings, [to_email], subject, html)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def send_download_link_email(
    to_email: str,
    recipient_name: str,
    group_name: str,
    url: str,
    expires_at_local: str,
    message: str,
    sender_name: str,
    reply_to: str,
) -> tuple[bool, str]:
    """Email a client their installer download link. Returns (ok, error_msg).

    Client-facing: uses the client email design (Admin → Email templates),
    which has no internal fleet branding unless one is set there.
    Replies go to the staff member who sent it.
    """
    settings = get_settings()
    if not settings.get("enabled") or not settings.get("smtp_host"):
        return False, "Email isn't configured — set up SMTP on the Notifications page first."
    if not settings.get("from_addr"):
        return False, "No from address configured on the Notifications page."

    subject, html = _render("download_link", {
        "recipient_name": recipient_name, "group_name": group_name, "url": url,
        "expires_at": expires_at_local, "message": message, "sender_name": sender_name,
    })
    try:
        _send_sync(settings, [to_email], subject, html, reply_to=reply_to)
        _log("download_link_email", subject, to_email, "sent")
        return True, ""
    except Exception as exc:
        _log("download_link_email", subject, to_email, "failed", str(exc)[:500])
        return False, str(exc)


def send_test_email(settings: dict) -> tuple[bool, str]:
    """Send a test email with the given settings dict.  Returns (ok, error_msg)."""
    to_list = [a.strip() for a in settings.get("to_addrs", "").split(",") if a.strip()]
    if not to_list:
        return False, "No recipient addresses configured."
    if not settings.get("smtp_host"):
        return False, "SMTP host is required."
    if not settings.get("from_addr"):
        return False, "From address is required."

    from app.email_templates import TEMPLATES
    _, html = _render("device_registered", TEMPLATES["device_registered"]["sample"])
    subject = "RustDesk Fleet — test notification"

    try:
        _send_sync(settings, to_list, subject, html)
        _log("test", subject, ", ".join(to_list), "sent")
        return True, ""
    except Exception as exc:
        _log("test", subject, ", ".join(to_list), "failed", str(exc)[:500])
        return False, str(exc)
