"""Billing in the console (step 8): the tenant page, billing mode, trial
extension in Stripe, coupons, resync, the overview and sign-ups with retry."""
from datetime import datetime, timedelta, timezone

import pytest

from app import billing as billing_mod
from app.console import jobs
from billing_helpers import billing, deliver, finish_checkout, sign_up, sql, start_signup  # noqa: F401
from conftest import add_tenant, provider_client
from test_billing_status import set_status


@pytest.fixture
def tenant(billing):
    tid, sub = sign_up(billing)
    return billing, tid, sub


def test_tenant_page_billing_section(tenant):
    b, tid, sub = tenant
    text = provider_client().get(f"/tenants/{tid}").text
    cus = sql("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", tid)[0][0]
    for s in (f"https://dashboard.stripe.com/test/customers/{cus}", f"https://dashboard.stripe.com/test/subscriptions/{sub}",
              "trialing", "Standard", "1 technicians · 0 devices", "Resync now", "Apply coupon"):
        assert s in text, s


def test_billing_mode(tenant):
    b, tid, sub = tenant
    c = provider_client()
    c.post("/tenants/1/billing-mode", data={"mode": "stripe"})
    assert sql("SELECT billing_mode FROM tenants WHERE id = 1")[0][0] == "internal"
    add_tenant(5, "handmade", "handmade.rds.example.com")
    c.post("/tenants/5/billing-mode", data={"mode": "stripe"})          # no subscription: refused
    assert sql("SELECT billing_mode FROM tenants WHERE id = 5")[0][0] == "manual"
    c.post("/tenants/5/billing-mode", data={"mode": "internal"})
    assert sql("SELECT billing_mode FROM tenants WHERE id = 5")[0][0] == "internal"
    c.post(f"/tenants/{tid}/billing-mode", data={"mode": "manual"})
    assert sql("SELECT billing_mode FROM tenants WHERE id = ?", tid)[0][0] == "manual"
    assert sql("SELECT 1 FROM provisioning_events WHERE event = 'billing_mode_changed' AND source = 'provider'")


def test_extend_trial_moves_trial_end_in_stripe(tenant):
    b, tid, sub = tenant
    before = b["fake"].sub(sub)["trial_end"]
    provider_client().post(f"/tenants/{tid}/extend-trial", data={"days": "7"})
    after = b["fake"].sub(sub)["trial_end"]
    assert after - before == pytest.approx(7 * 86400, abs=5)
    assert sql("SELECT trial_ends_at FROM tenants WHERE id = ?", tid)[0][0] == \
        datetime.fromtimestamp(after, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_coupon(tenant):
    b, tid, sub = tenant
    c = provider_client()
    c.post(f"/tenants/{tid}/coupon", data={"coupon": "NOPE"})
    assert not b["fake"].sub(sub)["discounts"]
    c.post(f"/tenants/{tid}/coupon", data={"coupon": "FRIENDS"})
    assert b["fake"].sub(sub)["discounts"] == [{"coupon": "FRIENDS"}]
    assert sql("SELECT 1 FROM provisioning_events WHERE event = 'coupon_applied' AND detail = 'FRIENDS'")


def test_resync_now_fixes_drift(tenant):
    b, tid, sub = tenant
    b["fake"].sub(sub).update(status="active", trial_end=None)
    provider_client().post(f"/tenants/{tid}/billing-sync")
    assert sql("SELECT status FROM tenants WHERE id = ?", tid)[0][0] == "active"
    assert "trial → active" in sql("SELECT detail FROM provisioning_events WHERE event = 'billing_resynced'")[0][0]


def test_overview(billing):
    b = billing
    paying, sub1 = sign_up(b, slug="paying", email="p@paying.example")
    set_status(b, sub1, "active", trial_end=None)
    trial, sub2 = sign_up(b, slug="trying", email="t@trying.example", ip="198.51.100.21")
    b["fake"].sub(sub2)["trial_end"] = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
    deliver("customer.subscription.updated", {"id": sub2})
    late, sub3 = sign_up(b, slug="late", email="l@late.example", ip="198.51.100.22")
    set_status(b, sub3, "past_due", trial_end=None)
    row = start_signup(b, slug="nocard", email="n@nocard.example", ip="198.51.100.23")
    finish_checkout(b, row, card=False)
    sql("INSERT INTO stripe_events (event_id, type, error) VALUES ('evt_broken', 'invoice.paid', 'KeyError: x')")
    text = provider_client().get("/billing").text
    # $49.00 flat; 1 technician is inside the 2 free; 0 devices. Paying + past due count.
    assert "$98.00" in text and "Standard" in text
    assert "trying" in text.split("Trials ending this week")[1].split("Past due")[0]
    assert "late" in text.split("Past due and unpaid")[1].split("No card on file")[0]
    assert "nocard" in text and "no payment method" in text
    assert "evt_broken" in text


def test_mrr_from_items():
    price = {"currency": "usd", "recurring": {"interval": "year", "interval_count": 1, "usage_type": "licensed"},
             "billing_scheme": "tiered", "tiers_mode": "graduated",
             "tiers": [{"up_to": 2, "unit_amount": 0}, {"up_to": None, "unit_amount": 1200}]}
    flat = {"currency": "usd", "recurring": {"interval": "month", "interval_count": 1}, "billing_scheme": "per_unit",
            "unit_amount": 4900}
    metered = {"currency": "usd", "recurring": {"interval": "month", "usage_type": "metered"}, "unit_amount": 5}
    sub = {"items": {"data": [{"price": price, "quantity": 5}, {"price": flat, "quantity": 1}, {"price": metered}]}}
    assert billing_mod.mrr(sub) == (4900 + 3 * 1200 // 12, "usd")


def test_signups_list_and_retry(billing):
    billing["system"].fail["issue-cert abc.rds.example.com"] = "rate limited"
    row = start_signup(billing)
    finish_checkout(billing, row)
    c = provider_client()
    page = c.get("/signups").text
    assert "abc" in page and "rate limited" in page and f'/signups/{row["id"]}/retry' in page
    billing["system"].fail.clear()
    r = c.post(f"/signups/{row['id']}/retry", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    jobs.run_pending()
    assert sql("SELECT status FROM signups WHERE id = ?", row["id"])[0][0] == "created"
    # A sign-up without a card can't be retried into a tenant.
    row2 = start_signup(billing, slug="nocard", email="n@nocard.example", ip="198.51.100.30")
    finish_checkout(billing, row2, card=False)
    c.post(f"/signups/{row2['id']}/retry")
    assert sql("SELECT status FROM signups WHERE id = ?", row2["id"])[0][0] == "failed"
