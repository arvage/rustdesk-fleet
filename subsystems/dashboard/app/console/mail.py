"""Emails from the provider (to provider admins, or to a tenant's admins
about their account), sent with the provider's SMTP settings, not any
tenant's. Logged to the provider audit log."""
from __future__ import annotations

import html as _html

from app import provider_config
from app.deps import get_db, provider_log


def smtp_settings() -> dict | None:
    s = provider_config.get_all()
    if not s["smtp_host"] or not s["from_addr"]:
        return None
    return {"tenant_id": None, "smtp_host": s["smtp_host"], "smtp_port": int(s["smtp_port"] or 587),
            "smtp_tls": s["smtp_tls"], "smtp_user": s["smtp_user"], "smtp_pass": s["smtp_pass"],
            "from_addr": s["from_addr"]}


def simple_html(heading: str, paragraphs: list[str], link: str = "", link_text: str = "") -> str:
    esc = _html.escape
    body = "".join(f'<p style="margin:0 0 14px">{esc(p)}</p>' for p in paragraphs)
    if link:
        body += (f'<p style="margin:0 0 14px"><a href="{esc(link)}" style="display:inline-block;background:#6366F1;'
                 f'color:#fff;text-decoration:none;font-weight:600;padding:10px 20px;border-radius:8px">'
                 f'{esc(link_text or link)}</a></p><p style="margin:0;font-size:12px;color:#64748B">{esc(link)}</p>')
    return (f'<!doctype html><html><body style="font-family:system-ui,sans-serif;background:#F1F5F9;padding:24px">'
            f'<div style="max-width:540px;margin:auto;background:#fff;border-radius:8px;padding:24px 28px;color:#334155">'
            f'<h2 style="margin:0 0 16px;color:#0F172A">{esc(heading)}</h2>{body}</div></body></html>')


def send(to: list[str], subject: str, html: str, event: str = "provider_email") -> tuple[bool, str]:
    """Send one email. Returns (ok, error). Never raises."""
    to = [t for t in to if t and "@" in t]
    settings = smtp_settings()
    if not settings:
        return False, "Provider email isn't set up (console → Settings)."
    if not to:
        return False, "No recipients."
    from app.notifications import _send_sync
    try:
        _send_sync(settings, to, subject, html)
        ok, err = True, ""
    except Exception as e:
        ok, err = False, str(e)[:300]
    conn = get_db()
    provider_log(conn, event if ok else event + "_failed", f"{subject} → {', '.join(to)}" + (f" ({err})" if err else ""), "")
    conn.close()
    return ok, err


def send_to_provider_admins(subject: str, html: str, event: str = "provider_alert") -> tuple[bool, str]:
    return send(provider_config.provider_admin_emails(), subject, html, event)
