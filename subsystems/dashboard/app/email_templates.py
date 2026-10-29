"""
email_templates.py — every email the dashboard sends, editable from
Admin → Email templates (routes/email_templates.py).

Each email is a TEMPLATES entry with built-in defaults for its subject,
heading, intro and closing text, and a details table. An admin can override
the text per email (email_templates table; only changed emails get a row)
and the shared look (email_design singleton): brand name, logo, colors, font,
footer. render() combines the two into a subject + HTML body.

Text fields are plain text with {placeholders}:
  * a blank line starts a new paragraph, a single newline is a line break
  * **double asterisks** make bold text
  * a paragraph whose placeholders are all empty is left out, so optional
    values (e.g. a link's expiry) don't leave a dangling sentence.
"""

from __future__ import annotations

import base64
import html
import io
import re
import sqlite3
from datetime import datetime, timezone

from app import paths

DB_PATH = paths.FLEET_DB
LOGO_CID = "rdf-email-logo"
MAX_LOGO_BYTES = 2 * 1024 * 1024
TEXT_FIELDS = ("subject", "heading", "intro", "outro", "button_text")

# ── Catalogue ────────────────────────────────────────────────────────────────
# details: (row label, context key). Empty values show as "—".
# sample: realistic values for the preview and test sends.

_DEVICE_SAMPLE = {"rustdesk_id": "123 456 789", "hostname": "RECEPTION-PC", "label": "Front desk",
                  "group_name": "Example Client", "ip": "203.0.113.42"}

TEMPLATES: dict[str, dict] = {
    "device_registered": {
        "name": "New device registered", "category": "Alerts", "audience": "team",
        "subject": "New device registered — {rustdesk_id}",
        "heading": "New device registered",
        "intro": "A new device has been registered with your RustDesk Fleet server.",
        "outro": "",
        "details": [("RustDesk ID", "rustdesk_id"), ("Computer", "hostname"), ("Label", "label"),
                    ("Group", "group_name"), ("IP address", "ip"), ("Registered at", "registered_at")],
        "sample": {**_DEVICE_SAMPLE, "label": "", "group_name": "unassigned", "registered_at": "2026-10-12 17:40:00"},
    },
    "device_online": {
        "name": "Device came online", "category": "Alerts", "audience": "team",
        "subject": "Device came online — {name}",
        "heading": "Device online",
        "intro": "A device that was offline is back online.",
        "outro": "",
        "details": [("RustDesk ID", "rustdesk_id"), ("Computer", "hostname"), ("Label", "label"),
                    ("Group", "group_name"), ("IP address", "ip"), ("Came online", "online_at")],
        "sample": {**_DEVICE_SAMPLE, "name": "Front desk", "online_at": "2026-10-12 17:51:00 UTC"},
    },
    "device_offline": {
        "name": "Device went offline", "category": "Alerts", "audience": "team",
        "subject": "Device went offline — {rustdesk_id}",
        "heading": "Device offline",
        "intro": "A device has gone offline.",
        "outro": "",
        "details": [("RustDesk ID", "rustdesk_id"), ("Label", "label"), ("Group", "group_name"),
                    ("Last seen", "last_seen")],
        "sample": {**_DEVICE_SAMPLE, "last_seen": "2026-10-12 17:30:12"},
    },
    "device_deleted": {
        "name": "Device deleted", "category": "Alerts", "audience": "team",
        "subject": "Device removed — {rustdesk_id}",
        "heading": "Device removed from fleet",
        "intro": "A device has been removed from your RustDesk Fleet.",
        "outro": "",
        "details": [("RustDesk ID", "rustdesk_id"), ("Label", "label"), ("Group", "group_name"),
                    ("Last seen", "last_seen")],
        "sample": {**_DEVICE_SAMPLE, "last_seen": "2026-09-30 08:12:44"},
    },
    "installer_built": {
        "name": "Installer built", "category": "Alerts", "audience": "team",
        "subject": "Installer built — {group_name} ({platform})",
        "heading": "Installer ready",
        "intro": "A new installer has been generated and is ready to deploy.",
        "outro": "",
        "details": [("Group", "group_name"), ("Platform", "platform"), ("RustDesk version", "version"),
                    ("SHA-256 (first 16)", "sha256_short")],
        "sample": {"group_name": "Example Client", "platform": "Windows", "version": "1.5.0",
                   "sha256": "9f2c4e1a7b3d5c6e8f0a1b2c3d4e5f60"},
    },
    "installer_deleted": {
        "name": "Installer deleted", "category": "Alerts", "audience": "team",
        "subject": "Installer deleted — {group_name} ({platform})",
        "heading": "Installer deleted",
        "intro": "An installer build has been deleted.",
        "outro": "",
        "details": [("Group", "group_name"), ("Platform", "platform"), ("RustDesk version", "version"),
                    ("Deleted by", "deleted_by")],
        "sample": {"group_name": "Example Client", "platform": "Windows", "version": "1.5.0",
                   "deleted_by": "tech@example.com"},
    },
    "installer_downloaded": {
        "name": "Installer downloaded via link", "category": "Alerts", "audience": "team",
        "subject": "Installer downloaded — {group_name} ({platform})",
        "heading": "Installer downloaded",
        "intro": "A client downloaded an installer using a shared download link. "
                 "The device should appear in the dashboard shortly after it's installed.",
        "outro": "",
        "details": [("Group", "group_name"), ("Platform", "platform"), ("RustDesk version", "version"),
                    ("Client IP", "ip"), ("Link note", "note"), ("Downloads on link", "downloads")],
        "sample": {"group_name": "Example Client", "platform": "Windows", "version": "1.5.0",
                   "ip": "198.51.100.7", "note": "New laptop for Sam", "downloads": "1 of 3"},
    },
    "user_created": {
        "name": "User account created", "category": "Alerts", "audience": "team",
        "subject": "New user created — {email}",
        "heading": "New user created",
        "intro": "A new user account has been created in RustDesk Fleet.",
        "outro": "",
        "details": [("Name", "display_name"), ("Email", "email"), ("Role", "role"),
                    ("Group access", "groups"), ("Created by", "created_by")],
        "sample": {"display_name": "Sam Lee", "email": "sam@example.com", "role": "tech",
                   "groups": "Example Client", "created_by": "admin@example.com"},
    },
    "user_deleted": {
        "name": "User account deleted", "category": "Alerts", "audience": "team",
        "subject": "User deleted — {email}",
        "heading": "User deleted",
        "intro": "A user account has been removed from RustDesk Fleet.",
        "outro": "",
        "details": [("Name", "display_name"), ("Email", "email"), ("Role", "role"), ("Deleted by", "deleted_by")],
        "sample": {"display_name": "Sam Lee", "email": "sam@example.com", "role": "tech",
                   "deleted_by": "admin@example.com"},
    },
    "user_credentials": {
        "name": "New user welcome (login details)", "category": "Emails to users", "audience": "team",
        "subject": "Your RustDesk Fleet account credentials",
        "heading": "Welcome to RustDesk Fleet",
        "intro": "Hi {name}, an account has been created for you.",
        "outro": "Please change your password after your first login. This email was sent once and will "
                 "not be resent.\n\nFor faster, more secure sign-in, consider registering a passkey "
                 "(Touch ID, Windows Hello, or a security key) from **My Account → Passkeys** once you're "
                 "logged in.",
        "details": [("Login email", "email"), ("Temporary password", "password")],
        "details_required": True,
        "sample": {"name": "Sam Lee", "email": "sam@example.com", "password": "Xy7-tr9q-Lm2p"},
    },
    "mfa_reminder": {
        "name": "Passkey reminder", "category": "Emails to users", "audience": "team",
        "subject": "Set up a passkey for your RustDesk Fleet account",
        "heading": "Add a passkey to your account",
        "intro": "Hi {name}, you don't have multi-factor sign-in set up yet on your RustDesk Fleet account.",
        "outro": "Registering a passkey (Touch ID, Windows Hello, or a security key) makes signing in faster "
                 "and more secure. You can add one from **My Account → Passkeys** after logging in.",
        "details": [],
        "sample": {"name": "Sam Lee"},
    },
    "download_link": {
        "name": "Installer download link", "category": "Emails to clients", "audience": "client",
        "subject": "Remote support installer{group_suffix}",
        "heading": "Install remote support",
        "intro": "{greeting}\n\n{message}\n\nTo let us help you remotely, please download and install our "
                 "remote support program using the button below. The page will pick the right version for "
                 "your computer and show you the steps.",
        "button_text": "Download installer",
        "button_url": "url",
        "outro": "Or copy this link into your browser:\n{url}\n\nThis link expires {expires_at} (Pacific time)."
                 "\n\nThanks,\n{sender_name}\n\nIf you weren't expecting this email, please don't install "
                 "anything — just reply to let us know.",
        "details": [],
        "sample": {"recipient_name": "Alex", "group_name": "Example Client",
                   "url": "https://rds.example.com/d/abc123", "expires_at": "Oct 19, 2026 at 5:00 PM",
                   "message": "As discussed on the phone — this lets us fix the printer issue.",
                   "sender_name": "Jordan from IT"},
    },
}
CATEGORIES = ["Alerts", "Emails to users", "Emails to clients"]

FONTS = {
    "system": ("System (default)", "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"),
    "arial": ("Arial / Helvetica", "Arial,Helvetica,sans-serif"),
    "verdana": ("Verdana", "Verdana,Geneva,sans-serif"),
    "georgia": ("Georgia (serif)", "Georgia,'Times New Roman',serif"),
    "trebuchet": ("Trebuchet", "'Trebuchet MS',Helvetica,sans-serif"),
}

# Provider-level emails (phase 2): from us to tenants' admins about sign-up
# and billing. Edited in the provider console (Emails), stored in
# provider_email_templates, rendered with the default design; tenants'
# Email templates pages don't list them.
_P = {"category": "Provider: sign-up and billing", "audience": "provider", "details": []}
PROVIDER_TEMPLATES: dict[str, dict] = {
    "signup_verify": {**_P, "name": "Sign-up: confirm your email",
        "subject": "Confirm your email to start your RustDesk Fleet trial",
        "heading": "Confirm your email",
        "intro": "Hi {name}, thanks for signing up {company} for RustDesk Fleet.",
        "button_text": "Confirm my email", "button_url": "url",
        "outro": "Open the link within 24 hours to confirm this address and enter payment details for your "
                 "{trial_days}-day free trial. If you didn't sign up, ignore this email.",
        "sample": {"name": "Ada", "company": "ABC IT", "url": "https://www.rds.example.com/signup/verify?token=x",
                   "trial_days": "14"}},
    "signup_welcome": {**_P, "name": "Sign-up: welcome",
        "subject": "Your RustDesk Fleet account is ready",
        "heading": "Welcome to RustDesk Fleet",
        "intro": "Hi {name}, your dashboard for {company} is ready. Sign in with the email and password you chose.",
        "button_text": "Sign in", "button_url": "url",
        "outro": "Your free trial ends on {trial_end}. You can see your plan, invoices and card under "
                 "**Admin → Billing**.",
        "sample": {"name": "Ada", "company": "ABC IT", "url": "https://abc.rds.example.com/login",
                   "trial_end": "Nov 12, 2026"}},
    "trial_ending": {**_P, "name": "Billing: trial ends in 3 days",
        "subject": "Your RustDesk Fleet trial ends in 3 days",
        "heading": "Your trial ends soon",
        "intro": "The free trial for {company} ends on {trial_end}. Your card is charged for the first period then.",
        "button_text": "Billing", "button_url": "url",
        "outro": "To change your plan or cancel before then, open **Admin → Billing**.",
        "sample": {"company": "ABC IT", "trial_end": "Nov 12, 2026", "url": "https://abc.rds.example.com/admin/billing"}},
    "payment_failed": {**_P, "name": "Billing: payment failed",
        "subject": "Payment failed for your RustDesk Fleet subscription",
        "heading": "Your payment didn't go through",
        "intro": "We couldn't take the latest payment for {company}. Stripe will try again.",
        "button_text": "Update card", "button_url": "url",
        "outro": "Please update your card so the account isn't suspended: it is suspended {days} days after "
                 "a failed payment.",
        "sample": {"company": "ABC IT", "url": "https://abc.rds.example.com/admin/billing", "days": "7"}},
    "suspended_nonpayment": {**_P, "name": "Billing: suspended for non-payment",
        "subject": "Your RustDesk Fleet account is suspended for non-payment",
        "heading": "Your account is suspended",
        "intro": "Remote support and installer downloads for {company} are off because the subscription hasn't "
                 "been paid.",
        "button_text": "Pay now", "button_url": "url",
        "outro": "Sign in and pay under **Admin → Billing**; the account reopens as soon as the payment goes "
                 "through. Your devices, groups and settings are kept.",
        "sample": {"company": "ABC IT", "url": "https://abc.rds.example.com/admin/billing"}},
    "subscription_cancelled": {**_P, "name": "Account cancelled",
        "subject": "Your {company} RustDesk Fleet account was cancelled",
        "heading": "Your account was cancelled",
        "intro": "Remote support for {company} has stopped.",
        "button_text": "Export your data", "button_url": "url",
        "outro": "You can still sign in to download your data (devices, groups, users, session history) until "
                 "{purge_date}. After that, everything is deleted for good.\n\n{resubscribe}",
        "sample": {"company": "ABC IT", "url": "https://abc.rds.example.com/export", "purge_date": "Dec 12, 2026",
                   "resubscribe": "Changed your mind? Resubscribe from the same page before then and everything "
                                  "comes back as it was."}},
    "reactivated": {**_P, "name": "Billing: account active again",
        "subject": "Your RustDesk Fleet account is active again",
        "heading": "You're back",
        "intro": "Thanks: {company} is open again, with the same relay, keys and devices.",
        "button_text": "Open the dashboard", "button_url": "url",
        "outro": "",
        "sample": {"company": "ABC IT", "url": "https://abc.rds.example.com/"}},
    "card_missing": {**_P, "name": "Billing: no card on file",
        "subject": "No card on file for your RustDesk Fleet subscription",
        "heading": "Add a card to keep your account",
        "intro": "The subscription for {company} has no card on file.",
        "button_text": "Add a card", "button_url": "url",
        "outro": "Without one, the subscription ends when the trial does (or at the next renewal).",
        "sample": {"company": "ABC IT", "url": "https://abc.rds.example.com/admin/billing"}},
}
TEMPLATES.update(PROVIDER_TEMPLATES)


def tenant_keys() -> list[str]:
    """The emails a tenant edits (Admin → Email templates)."""
    return [k for k, v in TEMPLATES.items() if v["audience"] != "provider"]


DESIGN_DEFAULTS = {
    "brand_name": "RustDesk Fleet",        # header title on team emails
    "client_brand_name": "",               # header title on client emails ("" = no header)
    "show_logo_team": 1,
    "show_logo_client": 1,
    "logo_height": 40,
    "header_bg": "#1E293B",
    "header_text": "#FFFFFF",
    "accent": "#6366F1",
    "page_bg": "#F1F5F9",
    "font": "system",
    "footer_text": "RustDesk Fleet Management · {timestamp} (Los Angeles)",
    "client_footer_text": "",
}
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ── Storage ──────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS email_templates (
            key          TEXT PRIMARY KEY,
            subject      TEXT,
            heading      TEXT,
            intro        TEXT,
            outro        TEXT,
            button_text  TEXT,
            show_details INTEGER NOT NULL DEFAULT 1,
            updated_by   TEXT,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS email_design (
            id                 INTEGER PRIMARY KEY CHECK (id = 1),
            brand_name         TEXT,
            client_brand_name  TEXT,
            show_logo_team     INTEGER,
            show_logo_client   INTEGER,
            logo_height        INTEGER,
            header_bg          TEXT,
            header_text        TEXT,
            accent             TEXT,
            page_bg            TEXT,
            font               TEXT,
            footer_text        TEXT,
            client_footer_text TEXT,
            updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


def get_design(tenant_id: int) -> dict:
    d = dict(DESIGN_DEFAULTS)
    try:
        conn = _db()
        row = conn.execute("SELECT * FROM email_design WHERE tenant_id = ?", (tenant_id,)).fetchone()
        conn.close()
    except sqlite3.Error:
        row = None
    if row:
        for k in DESIGN_DEFAULTS:
            if row[k] is not None:
                d[k] = row[k]
    return d


def save_design(tenant_id: int, values: dict) -> dict:
    """Validate and store design settings. Returns the cleaned values."""
    clean = dict(DESIGN_DEFAULTS)
    for k in ("brand_name", "client_brand_name"):
        clean[k] = str(values.get(k, "")).strip()[:80]
    for k in ("footer_text", "client_footer_text"):
        clean[k] = str(values.get(k, "")).strip()[:300]
    for k in ("header_bg", "header_text", "accent", "page_bg"):
        v = str(values.get(k, "")).strip()
        clean[k] = v.upper() if _COLOR_RE.match(v) else DESIGN_DEFAULTS[k]
    for k in ("show_logo_team", "show_logo_client"):
        clean[k] = 1 if values.get(k) in ("1", 1, True, "on") else 0
    try:
        clean["logo_height"] = max(20, min(120, int(values.get("logo_height") or 40)))
    except (TypeError, ValueError):
        clean["logo_height"] = 40
    clean["font"] = values.get("font") if values.get("font") in FONTS else "system"
    cols = list(DESIGN_DEFAULTS)
    conn = _db()
    conn.execute(
        f"INSERT OR REPLACE INTO email_design (tenant_id, {', '.join(cols)}, updated_at)"
        f" VALUES (?, {', '.join('?' * len(cols))}, datetime('now'))",
        [tenant_id, *[clean[c] for c in cols]],
    )
    conn.commit()
    conn.close()
    return clean


def reset_design(tenant_id: int) -> None:
    conn = _db()
    conn.execute("DELETE FROM email_design WHERE tenant_id = ?", (tenant_id,))
    conn.commit()
    conn.close()


def get_template(tenant_id: int, key: str) -> dict:
    """Defaults merged with the tenant's saved override, if any; adds 'customized'."""
    spec = TEMPLATES[key]
    t = {f: spec.get(f, "") for f in TEXT_FIELDS}
    t["show_details"] = True
    t["customized"] = False
    try:
        conn = _db()
        row = conn.execute("SELECT * FROM email_templates WHERE tenant_id = ? AND key = ?",
                           (tenant_id, key)).fetchone()
        conn.close()
    except sqlite3.Error:
        row = None
    if row:
        for f in TEXT_FIELDS:
            if row[f] is not None:
                t[f] = row[f]
        t["show_details"] = bool(row["show_details"])
        t["customized"] = True
        t["updated_at"], t["updated_by"] = row["updated_at"], row["updated_by"]
    if spec.get("details_required"):
        t["show_details"] = True
    return t


PROVIDER_SQL = """
    CREATE TABLE IF NOT EXISTS provider_email_templates (
        key           TEXT PRIMARY KEY,
        subject       TEXT, heading TEXT, intro TEXT, outro TEXT, button_text TEXT,
        updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_by    TEXT
    );
"""


def get_provider_template(key: str) -> dict:
    spec = PROVIDER_TEMPLATES[key]
    t = {f: spec.get(f, "") for f in TEXT_FIELDS}
    t["show_details"], t["customized"] = True, False
    try:
        conn = _db()
        conn.executescript(PROVIDER_SQL)
        row = conn.execute("SELECT * FROM provider_email_templates WHERE key = ?", (key,)).fetchone()
        conn.close()
    except sqlite3.Error:
        row = None
    if row:
        for f in TEXT_FIELDS:
            if row[f] is not None:
                t[f] = row[f]
        t["customized"], t["updated_at"], t["updated_by"] = True, row["updated_at"], row["updated_by"]
    return t


def save_provider_template(key: str, values: dict, user_email: str) -> None:
    clean = {f: (values.get(f) or "").strip()[:5000] for f in TEXT_FIELDS}
    conn = _db()
    conn.executescript(PROVIDER_SQL)
    conn.execute("INSERT OR REPLACE INTO provider_email_templates (key, subject, heading, intro, outro, button_text,"
                 " updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)",
                 (key, *[clean[f] for f in TEXT_FIELDS], user_email))
    conn.commit()
    conn.close()


def reset_provider_template(key: str) -> None:
    conn = _db()
    conn.executescript(PROVIDER_SQL)
    conn.execute("DELETE FROM provider_email_templates WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def save_template(tenant_id: int, key: str, values: dict, user_email: str) -> None:
    spec = TEMPLATES[key]
    clean = {f: str(values.get(f, "")).replace("\r\n", "\n").strip()[:4000] for f in TEXT_FIELDS}
    clean["subject"] = " ".join(clean["subject"].split())[:200] or spec["subject"]
    if not spec.get("button_url"):
        clean["button_text"] = ""
    show = 1 if spec.get("details_required") or values.get("show_details") in ("1", 1, True, "on") else 0
    conn = _db()
    conn.execute(
        """INSERT OR REPLACE INTO email_templates
               (tenant_id, key, subject, heading, intro, outro, button_text, show_details, updated_by, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (tenant_id, key, clean["subject"], clean["heading"], clean["intro"], clean["outro"], clean["button_text"],
         show, user_email),
    )
    conn.commit()
    conn.close()


def reset_template(tenant_id: int, key: str) -> None:
    conn = _db()
    conn.execute("DELETE FROM email_templates WHERE tenant_id = ? AND key = ?", (tenant_id, key))
    conn.commit()
    conn.close()


# ── Logo ─────────────────────────────────────────────────────────────────────

class LogoError(ValueError):
    pass


def logo_path(tenant_id: int):
    """The tenant's email logo (tenant #1: <data dir>/branding/email-logo.png)."""
    from app.tenancy import tenant_files_dir
    return tenant_files_dir(tenant_id) / "branding" / "email-logo.png"


def save_logo(tenant_id: int, data: bytes) -> None:
    """Validate an uploaded image and store it as a PNG (max 600 px wide)."""
    if len(data) > MAX_LOGO_BYTES:
        raise LogoError("The logo must be under 2 MB.")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise LogoError("That file isn't an image we can read — use PNG, JPG or GIF.")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    if img.width > 600:
        img = img.resize((600, max(1, round(img.height * 600 / img.width))))
    path = logo_path(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    img.save(tmp, "PNG", optimize=True)
    tmp.replace(path)


def remove_logo(tenant_id: int) -> None:
    logo_path(tenant_id).unlink(missing_ok=True)


def has_logo(tenant_id: int) -> bool:
    return logo_path(tenant_id).exists()


def logo_bytes(tenant_id: int) -> bytes | None:
    path = logo_path(tenant_id)
    return path.read_bytes() if path.exists() else None


# ── Rendering ────────────────────────────────────────────────────────────────

def _now_la() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M")


def _prepare(key: str, ctx: dict) -> dict:
    """Fill defaults and derived values so placeholders always resolve."""
    c = {k: ("" if v is None else str(v)) for k, v in ctx.items()}
    # Every placeholder the template knows about resolves, even if the caller
    # didn't pass it — empty, so its paragraph drops out instead of showing "{x}".
    for k in TEMPLATES.get(key, {}).get("sample", {}):
        c.setdefault(k, "")
    c["timestamp"] = _now_la()
    if key in ("device_registered", "device_online"):
        c["group_name"] = c.get("group_name") or "unassigned"
    if key == "device_registered":
        c["registered_at"] = c.get("registered_at") or c["timestamp"]
    if key == "device_offline":
        c["last_seen"] = c.get("last_seen") or c["timestamp"]
    if key == "device_online":
        c["name"] = c.get("name") or c.get("label") or c.get("hostname") or c.get("rustdesk_id", "")
        c["online_at"] = c.get("online_at") or c["timestamp"]
    if key == "installer_built":
        c["sha256_short"] = (c.get("sha256") or "")[:16] + "…" if c.get("sha256") else ""
    if key in ("user_credentials", "mfa_reminder"):
        c["name"] = c.get("name") or c.get("display_name") or c.get("email", "")
    if key == "download_link":
        c["greeting"] = f"Hi {c['recipient_name']}," if c.get("recipient_name") else "Hello,"
        c["sender_name"] = c.get("sender_name") or "Your IT support team"
        c["group_suffix"] = f" for {c['group_name']}" if c.get("group_name") else ""
    return c


_PH_RE = re.compile(r"\{([a-z_]+)\}")


def placeholders(key: str) -> list[str]:
    """Placeholder names available to a template (for the editor)."""
    return sorted(set(_prepare(key, TEMPLATES[key]["sample"])) - {"sha256", "pct", "threshold"}
                  | {"timestamp"})


def _fill(text: str, ctx: dict) -> str:
    return _PH_RE.sub(lambda m: ctx.get(m.group(1), m.group(0)), text)


def _paragraphs(text: str, ctx: dict, style: str, accent: str) -> str:
    """Plain text -> escaped HTML paragraphs (see module docstring)."""
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        names = _PH_RE.findall(para)
        if names and all(not ctx.get(n) for n in names if n in ctx) and any(n in ctx for n in names):
            continue
        filled = html.escape(_fill(para, ctx))
        filled = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", filled)
        filled = re.sub(r"(https?://[^\s<]+)",
                        lambda m: f'<a href="{m.group(1)}" style="color:{accent};word-break:break-all">{m.group(1)}</a>',
                        filled)
        out.append(f'<p style="{style}">{filled.replace(chr(10), "<br>")}</p>')
    return "".join(out)


def render(tenant_id: int, key: str, ctx: dict, *, preview: bool = False,
           template: dict | None = None, design: dict | None = None) -> tuple[str, str, bool]:
    """Return (subject, html, uses_logo). With preview=True the logo is inlined
    as a data URI (for the browser) instead of a cid: reference (for email).
    template/design override the saved ones (editor preview)."""
    spec = TEMPLATES.get(key)
    if spec is None:
        return key, f"<p>Event: <strong>{html.escape(key)}</strong></p>", False
    provider = spec["audience"] == "provider"
    t = template or (get_provider_template(key) if provider else get_template(tenant_id, key))
    d = design or get_design(0 if provider else tenant_id)     # provider emails: the default design
    c = _prepare(key, ctx)
    client = spec["audience"] == "client"
    font = FONTS.get(d["font"], FONTS["system"])[1]
    accent = d["accent"]
    esc = html.escape

    subject = " ".join(_fill(t["subject"] or spec["subject"], c).split())

    # Header: brand title and/or logo; team emails also put the heading here.
    title = d["client_brand_name"] if client else d["brand_name"]
    show_logo = has_logo(tenant_id) and bool(d["show_logo_client"] if client else d["show_logo_team"])
    logo_html = ""
    if show_logo:
        src = ("data:image/png;base64," + base64.b64encode(logo_bytes(tenant_id)).decode()) if preview else f"cid:{LOGO_CID}"
        logo_html = (f'<img src="{src}" alt="{esc(title or "Logo")}" height="{int(d["logo_height"])}" '
                     f'style="display:block;height:{int(d["logo_height"])}px;width:auto;border:0;margin:0 0 10px">')
    heading = _fill(t["heading"], c)
    header = ""
    if logo_html or title or not client:
        sub = (f'<p style="color:{d["header_text"]};opacity:.7;margin:4px 0 0;font-size:12px">{esc(heading)}</p>'
               if heading and not client else "")
        h1 = (f'<h1 style="color:{d["header_text"]};margin:0;font-size:16px;font-weight:600">{esc(title)}</h1>'
              if title else "")
        header = (f'<tr><td style="background:{d["header_bg"]};padding:22px 28px">'
                  f'{logo_html}{h1}{sub}</td></tr>')
    p_style = "color:#334155;font-size:14px;line-height:1.6;margin:0 0 16px"

    body = ""
    if client and heading:
        body += f'<h2 style="font-size:19px;font-weight:700;color:#0F172A;margin:0 0 16px">{esc(heading)}</h2>'
    body += _paragraphs(t["intro"], c, p_style, accent)
    if spec.get("details") and t["show_details"]:
        rows = "".join(
            f'<tr><td style="padding:9px 12px 9px 0;border-bottom:1px solid #F1F5F9;color:#64748B;'
            f'width:150px;white-space:nowrap;vertical-align:top">{esc(label)}</td>'
            f'<td style="padding:9px 0;border-bottom:1px solid #F1F5F9;color:#0F172A;font-weight:500;'
            f'word-break:break-all">{esc(c.get(k) or "—")}</td></tr>'
            for label, k in spec["details"])
        body += f'<table role="presentation" width="100%" style="border-collapse:collapse;font-size:13px;margin:0 0 16px">{rows}</table>'
    if spec.get("button_url") and t["button_text"] and c.get(spec["button_url"]):
        body += (f'<p style="margin:0 0 20px"><a href="{esc(c[spec["button_url"]])}" '
                 f'style="display:inline-block;background:{accent};color:#ffffff;text-decoration:none;'
                 f'font-weight:600;font-size:14px;padding:11px 22px;border-radius:8px">{esc(_fill(t["button_text"], c))}</a></p>')
    body += _paragraphs(t["outro"], c, p_style, accent)

    footer_text = _fill(d["client_footer_text"] if client else d["footer_text"], c)
    footer = (f'<tr><td style="background:#F8FAFC;padding:14px 28px;border-top:1px solid #E2E8F0">'
              f'<p style="color:#94A3B8;font-size:11px;margin:0">{esc(footer_text)}</p></td></tr>'
              if footer_text else "")

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(subject)}</title></head>
<body style="margin:0;padding:24px 0;background:{d['page_bg']};font-family:{font}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{d['page_bg']}"><tr><td align="center" style="padding:0 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:540px;background:#ffffff;border-radius:8px;overflow:hidden;font-family:{font}">
{header}
<tr><td style="padding:24px 28px">{body}</td></tr>
{footer}
</table>
</td></tr></table>
</body></html>"""
    return subject, doc, show_logo and not preview
