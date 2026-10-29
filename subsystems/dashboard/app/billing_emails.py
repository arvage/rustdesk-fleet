"""Emails for sign-up and billing (phase 2): the provider-level templates in
email_templates.PROVIDER_TEMPLATES (editable in the console's Emails page),
sent with the provider's mail settings. Stripe's own emails that would
duplicate these are turned off in the Stripe dashboard (DEPLOYMENT.md);
Stripe still sends receipts and invoices."""
from __future__ import annotations

from app import email_templates
from app.console import mail
from app.deps import get_db


def send(key: str, to: list[str], ctx: dict) -> tuple[bool, str]:
    if not to:
        return False, "no recipients"
    subject, html, _ = email_templates.render(0, key, ctx)
    return mail.send(to, subject, html, event=f"{key}_email")


def _tenant(tenant_id: int) -> tuple[list[str], dict]:
    """The tenant's admins and the context every billing email uses."""
    from app import domains
    from app.templates_config import _localtime
    conn = get_db()
    to = [r[0] for r in conn.execute("SELECT email FROM users WHERE tenant_id = ? AND role = 'admin'", (tenant_id,))]
    t = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    bs = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    home = domains.home_host(conn, tenant_id)
    conn.close()
    ctx = {"company": t["display_name"] if t else "", "home": home,
           "trial_end": _localtime(bs["trial_end"], "%b %d, %Y") if bs and bs["trial_end"] else ""}
    return to, ctx


def verify_email(signup_row, link: str) -> tuple[bool, str]:
    from app import signup
    return send("signup_verify", [signup_row["email"]], {"name": signup_row["admin_name"], "company": signup_row["company"],
                                                        "url": link, "trial_days": str(signup.TRIAL_DAYS)})


def welcome(tenant_id: int, email: str, name: str, login_url: str) -> tuple[bool, str]:
    _, ctx = _tenant(tenant_id)
    return send("signup_welcome", [email], {**ctx, "name": name, "url": login_url})


KINDS = {"payment_failed": ("payment_failed", "/admin/billing"), "suspended": ("suspended_nonpayment", "/admin/billing"),
         "reactivated": ("reactivated", "/"), "card_missing": ("card_missing", "/admin/billing"),
         "trial_ending": ("trial_ending", "/admin/billing")}


def lifecycle(tenant_id: int, kind: str) -> tuple[bool, str]:
    """Email the tenant's admins about a billing event."""
    from app import settings
    key, path = KINDS[kind]
    to, ctx = _tenant(tenant_id)
    return send(key, to, {**ctx, "url": f"https://{ctx['home']}{path}", "days": str(settings.past_due_suspend_days())})


def cancelled(tenant_id: int, purge_date: str, stripe: bool) -> tuple[bool, str]:
    to, ctx = _tenant(tenant_id)
    resub = PROVIDER_RESUBSCRIBE if stripe else ""
    return send("subscription_cancelled", to, {**ctx, "url": f"https://{ctx['home']}/export", "purge_date": purge_date,
                                               "resubscribe": resub})


PROVIDER_RESUBSCRIBE = ("Changed your mind? Resubscribe from the same page before then and everything comes back as "
                        "it was.")
