"""Emails for sign-up and billing (phase 2), sent with the provider's mail
settings (console → Settings)."""
from __future__ import annotations

from app.console import mail


def verify_email(signup_row, link: str) -> tuple[bool, str]:
    return mail.send([signup_row["email"]], "Confirm your email to start your RustDesk Fleet trial", mail.simple_html(
        "Confirm your email",
        [f"Hi {signup_row['admin_name']}, thanks for signing up {signup_row['company']}.",
         "Open the link below within 24 hours to confirm this address and enter payment details for your trial. "
         "If you didn't sign up, ignore this email."],
        link, "Confirm my email"), event="signup_verify_email")


def welcome(tenant_id: int, email: str, name: str, login_url: str) -> tuple[bool, str]:
    return mail.send([email], "Your RustDesk Fleet account is ready", mail.simple_html(
        "Your account is ready",
        [f"Hi {name}, your RustDesk Fleet dashboard is ready. Sign in with the email and password you chose.",
         "Your free trial has started; you can see its end date and manage billing under Admin → Billing."],
        login_url, "Sign in"), event="signup_welcome_email")


LIFECYCLE = {
    "payment_failed": ("Payment failed for your RustDesk Fleet subscription",
                       ["We couldn't take the latest payment for your subscription. Stripe will retry it.",
                        "Please update your card under Admin → Billing → Manage billing so your account isn't suspended."]),
    "suspended": ("Your RustDesk Fleet account is suspended for non-payment",
                  ["Remote support and installer downloads are off because the subscription hasn't been paid.",
                   "Sign in and open Admin → Billing to pay; the account reopens as soon as the payment goes through. "
                   "Your devices, groups and settings are kept."]),
    "reactivated": ("Your RustDesk Fleet account is active again",
                    ["Thanks: the payment went through and your account is open again, with the same relay, keys and "
                     "devices."]),
    "card_missing": ("No card on file for your RustDesk Fleet subscription",
                     ["Your subscription has no card on file. Add one under Admin → Billing → Manage billing.",
                      "Without one, the subscription ends when the trial does (or at the next renewal)."]),
    "trial_ending": ("Your RustDesk Fleet trial ends in 3 days",
                     ["Your free trial ends in 3 days, and your card will be charged for the first period then.",
                      "You can change your plan or cancel under Admin → Billing before it ends."]),
}


def lifecycle(tenant_id: int, kind: str) -> tuple[bool, str]:
    """Email the tenant's admins about a billing event."""
    from app.deps import get_db
    subject, lines = LIFECYCLE[kind]
    conn = get_db()
    to = [r[0] for r in conn.execute("SELECT email FROM users WHERE tenant_id = ? AND role = 'admin'", (tenant_id,))]
    conn.close()
    if not to:
        return False, "no admins"
    return mail.send(to, subject, mail.simple_html(subject, lines), event=f"billing_{kind}_email")
