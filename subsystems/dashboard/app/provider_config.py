"""Provider-wide settings stored in the database (provider_settings): the
provider's SMTP server for provider emails, host-health alert thresholds,
the rustdesk-server version new relays get. Edited on the console's
Settings page."""
from __future__ import annotations

from app.deps import get_db

DEFAULTS = {
    "smtp_host": "", "smtp_port": "587", "smtp_tls": "starttls", "smtp_user": "", "smtp_pass": "",
    "from_addr": "", "mem_threshold": "90", "disk_threshold": "90", "alerts_enabled": "1",
    "letsencrypt_email": "",
}


def get_all() -> dict[str, str]:
    conn = get_db()
    rows = dict(conn.execute("SELECT key, value FROM provider_settings").fetchall())
    conn.close()
    return {**DEFAULTS, **rows}


def get(key: str, default: str = "") -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM provider_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else DEFAULTS.get(key, default)


def set_many(values: dict[str, str]) -> None:
    conn = get_db()
    for k, v in values.items():
        conn.execute("INSERT INTO provider_settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, str(v)))
    conn.commit()
    conn.close()


def provider_admin_emails() -> list[str]:
    conn = get_db()
    rows = [r[0] for r in conn.execute("SELECT email FROM provider_users WHERE disabled_at IS NULL ORDER BY id")]
    conn.close()
    return rows
