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
