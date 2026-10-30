"""Provider-level sign-up and billing emails (step 9): sent from the
templates, editable in the console, not on tenants' Email templates page."""
from app import billing_emails
from app import email_templates as et
from billing_helpers import billing, sign_up, sql  # noqa: F401
from conftest import provider_client, signed_in_at
from test_billing_status import set_status


def test_every_billing_email_comes_from_a_provider_template(billing):
    b = billing
    tid, sub = sign_up(b)
    subjects = [e[1] for e in b["emails"]]
    assert "Confirm your email to start your RustDesk Fleet trial" in subjects
    assert "Your RustDesk Fleet account is ready" in subjects
    welcome = next(e for e in b["emails"] if e[1] == "Your RustDesk Fleet account is ready")
    assert "https://abc.rds.example.com/login" in welcome[2] and "Admin → Billing" in welcome[2]
    set_status(b, sub, "past_due", event="invoice.payment_failed")
    set_status(b, sub, "unpaid")
    set_status(b, sub, "active", event="invoice.paid", trial_end=None)
    set_status(b, sub, "canceled", event="customer.subscription.deleted")
    subjects = [e[1] for e in b["emails"]]
    for key in ("payment_failed", "suspended_nonpayment", "reactivated"):
        assert et.PROVIDER_TEMPLATES[key]["subject"] in subjects, key
    cancelled = next(e for e in b["emails"] if e[1] == "Your ABC IT RustDesk Fleet account was cancelled")
    assert "https://abc.rds.example.com/export" in cancelled[2] and "Resubscribe" in cancelled[2]


def test_trial_will_end_emails_once(billing):
    b = billing
    tid, sub = sign_up(b)
    from billing_helpers import deliver
    deliver("customer.subscription.trial_will_end", {"id": sub})
    deliver("customer.subscription.trial_will_end", {"id": sub})
    assert sum(e[1] == et.PROVIDER_TEMPLATES["trial_ending"]["subject"] for e in b["emails"]) == 1


def test_trial_will_end_after_the_trial_was_ended_sends_nothing(billing):
    """One trial per card ends the trial at once; Stripe still sends trial_will_end."""
    b = billing
    tid, sub = sign_up(b)
    from billing_helpers import deliver
    b["fake"].sub(sub).update(status="active")
    deliver("customer.subscription.trial_will_end", {"id": sub})
    assert not any(e[1] == et.PROVIDER_TEMPLATES["trial_ending"]["subject"] for e in b["emails"])


def test_editing_in_the_console(billing):
    c = provider_client()
    assert "Sign-up: confirm your email" in c.get("/emails").text
    c.post("/emails/signup_verify", data={"subject": "Hello {name}, confirm", "heading": "H", "intro": "I",
                                         "outro": "O", "button_text": "Go"})
    ok, _ = billing_emails.send("signup_verify", ["x@example.com"], {"name": "Ada", "url": "https://x.example"})
    assert billing["emails"][-1][1] == "Hello Ada, confirm"
    assert "Hello" in c.get("/emails/signup_verify/preview").text
    c.post("/emails/signup_verify", data={"action": "reset"})
    assert et.get_provider_template("signup_verify")["customized"] is False


def test_tenants_dont_see_provider_emails(two_tenants):
    c = signed_in_at(two_tenants["a"]["admin"], "rds.example.com")
    page = c.get("/admin/emails").text
    assert "Sign-up: confirm your email" not in page
    assert c.get("/admin/emails/signup_verify", follow_redirects=False).status_code in (303, 404)
