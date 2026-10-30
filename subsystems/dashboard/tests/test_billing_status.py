"""Stripe status drives the tenant (step 5), card removal (step 3a), and the
daily reconciliation. Every row of the status table."""
from datetime import datetime, timedelta, timezone

import pytest

from app import billing as billing_mod
from app.console import jobs, lifecycle
from billing_helpers import PUBLIC, billing, deliver, sign_up, sql  # noqa: F401
from conftest import new_client, provider_client


def tenant(tid):
    return sql("SELECT * FROM tenants WHERE id = ?", tid)[0]


def bsub(tid):
    return sql("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", tid)[0]


def set_status(b, sub, status, event="customer.subscription.updated", **extra):
    b["fake"].sub(sub).update(status=status, **extra)
    if event.startswith("invoice."):
        import fake_stripe
        iid = fake_stripe.new_id("in")
        b["fake"].invoices[iid] = {"id": iid, "object": "invoice", "customer": b["fake"].sub(sub)["customer"],
                                   "status": "paid" if event == "invoice.paid" else "open",
                                   "parent": {"type": "subscription_details",
                                              "subscription_details": {"subscription": sub}}}
        deliver(event, {"id": iid, "object": "invoice"})
    else:
        deliver(event, {"id": sub})


@pytest.fixture
def signed_up(billing):
    tid, sub = sign_up(billing)
    return billing, tid, sub


def test_trialing_is_trial_until_stripes_trial_end(signed_up):
    b, tid, sub = signed_up
    t = tenant(tid)
    end = datetime.fromtimestamp(b["fake"].sub(sub)["trial_end"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert t["status"] == "trial" and t["trial_ends_at"] == end
    # The local trial timer doesn't apply to Stripe tenants.
    lifecycle.daily(now=datetime.now(timezone.utc) + timedelta(days=30))
    assert tenant(tid)["status"] == "trial"


def test_active(signed_up):
    b, tid, sub = signed_up
    set_status(b, sub, "active", trial_end=None)
    t = tenant(tid)
    assert t["status"] == "active" and t["trial_ends_at"] is None


def test_past_due_stays_open_then_suspends_after_the_days(signed_up, monkeypatch):
    b, tid, sub = signed_up
    set_status(b, sub, "past_due", event="invoice.payment_failed")
    assert tenant(tid)["status"] == "trial" and bsub(tid)["past_due_since"]
    assert any("Payment failed" in e[1] for e in b["emails"])
    billing_mod.reconcile(at=datetime.now(timezone.utc) + timedelta(days=6))
    assert tenant(tid)["status"] == "trial"
    billing_mod.reconcile(at=datetime.now(timezone.utc) + timedelta(days=7, minutes=1))
    t = tenant(tid)
    assert t["status"] == "suspended" and t["billing_suspended"] == 1
    assert any("suspended for non-payment" in e[1] for e in b["emails"])
    # The admins can still sign in and reach Billing; nothing else opens.
    c = new_client("abc.rds.example.com")
    assert c.get("/login").status_code == 200
    assert c.get("/devices", follow_redirects=False).status_code == 403
    assert c.get("/", follow_redirects=False).headers["location"] == "/admin/billing"
    # Paying reactivates automatically.
    set_status(b, sub, "active", event="invoice.paid", trial_end=None)
    t = tenant(tid)
    assert t["status"] == "active" and t["billing_suspended"] == 0 and bsub(tid)["past_due_since"] is None
    assert any("active again" in e[1] for e in b["emails"])


def test_past_due_on_a_test_clock_follows_the_clock(signed_up):
    """STRIPE_TEST_CLOCK customers: advancing the clock suspends, real time doesn't."""
    b, tid, sub = signed_up
    now = int(datetime.now(timezone.utc).timestamp())
    b["fake"].test_clocks["clock_1"] = {"id": "clock_1", "object": "test_helpers.test_clock", "frozen_time": now}
    b["fake"].sub(sub)["test_clock"] = "clock_1"
    set_status(b, sub, "past_due", event="invoice.payment_failed")
    assert tenant(tid)["status"] == "trial"
    b["fake"].test_clocks["clock_1"]["frozen_time"] = now + 6 * 86400
    billing_mod.reconcile()
    assert tenant(tid)["status"] == "trial"
    b["fake"].test_clocks["clock_1"]["frozen_time"] = now + 7 * 86400 + 60
    billing_mod.reconcile()
    assert tenant(tid)["status"] == "suspended"


def test_unreadable_test_clock_falls_back_to_real_time(signed_up):
    b, tid, sub = signed_up
    b["fake"].sub(sub)["test_clock"] = "clock_missing"
    set_status(b, sub, "past_due", event="invoice.payment_failed")
    assert tenant(tid)["status"] == "trial" and bsub(tid)["past_due_since"]


def test_past_due_days_setting(signed_up, monkeypatch):
    b, tid, sub = signed_up
    monkeypatch.setenv("PAST_DUE_SUSPEND_DAYS", "2")
    set_status(b, sub, "past_due")
    billing_mod.reconcile(at=datetime.now(timezone.utc) + timedelta(days=2, minutes=1))
    assert tenant(tid)["status"] == "suspended"


def test_unpaid_suspends(signed_up):
    b, tid, sub = signed_up
    set_status(b, sub, "unpaid")
    assert tenant(tid)["status"] == "suspended" and tenant(tid)["billing_suspended"] == 1


def test_a_provider_suspension_is_not_lifted_by_paying(signed_up):
    b, tid, sub = signed_up
    lifecycle.suspend(tid, "ops@example.com")
    set_status(b, sub, "active", event="invoice.paid", trial_end=None)
    assert tenant(tid)["status"] == "suspended"


def test_canceled_cancels_the_tenant_with_grace(signed_up):
    b, tid, sub = signed_up
    set_status(b, sub, "canceled", event="customer.subscription.deleted")
    t = tenant(tid)
    assert t["status"] == "cancelled" and t["purge_after"]
    assert tid not in b["system"].up                     # relay stopped


def test_resubscribing_during_grace_keeps_relay_and_keys(signed_up):
    b, tid, sub = signed_up
    relay_before = sql("SELECT host, hbbs_port, pubkey FROM relays WHERE tenant_id = ?", tid)[0]
    set_status(b, sub, "canceled", event="customer.subscription.deleted")
    from conftest import signed_in_at
    admin = sql("SELECT * FROM users WHERE tenant_id = ?", tid)[0]
    c = new_client("abc.rds.example.com")
    c.post("/login", data={"email": admin["email"], "password": "a-long-password"})
    page = c.get("/export").text
    assert "Resubscribe" in page
    r = c.post("/export/resubscribe", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.example/")
    cs = b["fake"].requests("POST", "/v1/checkout/sessions")[-1]
    assert cs["payment_method_collection"] == "always"
    assert cs["subscription_data"]["trial_settings"]["end_behavior"]["missing_payment_method"] == "cancel"
    assert "trial_period_days" not in cs["subscription_data"]
    assert cs["customer"] == sql("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", tid)[0][0]
    session_id = list(b["fake"].sessions)[-1]
    new_sub = b["fake"].complete_checkout(session_id, "fp_abc")
    b["fake"].sub(new_sub).update(status="active", trial_end=None)
    deliver("checkout.session.completed", {"id": session_id})
    t = tenant(tid)
    assert t["status"] == "active" and t["purge_after"] is None
    assert bsub(tid)["stripe_subscription_id"] == new_sub
    assert sql("SELECT host, hbbs_port, pubkey FROM relays WHERE tenant_id = ?", tid)[0] == relay_before
    assert tid in b["system"].up                         # started again


def test_incomplete_expired_undoes_the_creation(signed_up):
    b, tid, sub = signed_up
    set_status(b, sub, "incomplete_expired")
    assert sql("SELECT COUNT(*) FROM tenants WHERE id = ?", tid)[0][0] == 0
    assert tuple(sql("SELECT status, failure_reason FROM signups")[0]) == ("failed", "payment never completed")


def test_incomplete_changes_nothing(signed_up):
    b, tid, sub = signed_up
    set_status(b, sub, "incomplete")
    assert tenant(tid)["status"] == "trial"


def test_cancel_at_period_end_keeps_the_tenant_open(signed_up):
    b, tid, sub = signed_up
    b["fake"].sub(sub).update(cancel_at_period_end=True, cancel_at=int(datetime.now().timestamp()) + 86400 * 10)
    deliver("customer.subscription.updated", {"id": sub})
    assert tenant(tid)["status"] == "trial" and bsub(tid)["cancel_at_period_end"] == 1 and bsub(tid)["cancel_at"]


def test_tenant1_is_never_touched_by_billing(signed_up):
    b, tid, sub = signed_up
    assert tenant(1)["billing_mode"] == "internal"
    # Even a stray subscription row pointing at tenant #1 changes nothing.
    sql("INSERT INTO billing_subscriptions (tenant_id, stripe_subscription_id, status) VALUES (1, ?, 'trialing')", "sub_t1")
    b["fake"].subscriptions["sub_t1"] = {**b["fake"].sub(sub), "id": "sub_t1", "status": "unpaid"}
    deliver("customer.subscription.updated", {"id": "sub_t1"})
    assert billing_mod.apply_status(1, {"status": "canceled"}) is None
    billing_mod.reconcile()
    assert tenant(1)["status"] == "active"
    with pytest.raises(ValueError):
        billing_mod.cancel_subscription(1, False, "x")


def test_manual_tenants_keep_the_local_trial_timer():
    from conftest import add_tenant
    add_tenant(2, "manual", "manual.rds.example.com", status="trial")
    sql("UPDATE tenants SET trial_ends_at = datetime('now', '-1 hour') WHERE id = 2")
    lifecycle.daily()
    assert tenant(2)["status"] == "suspended" and tenant(2)["billing_mode"] == "manual"


# ── Console actions act on Stripe ────────────────────────────────────────────

def test_console_cancel_now_or_at_period_end(signed_up):
    b, tid, sub = signed_up
    c = provider_client()
    c.post(f"/tenants/{tid}/cancel", data={"confirm": "abc", "when": "period_end"})
    assert b["fake"].sub(sub)["cancel_at_period_end"] is True and tenant(tid)["status"] == "trial"
    r = c.post(f"/tenants/{tid}/cancel", data={"confirm": "abc", "when": "now"}, follow_redirects=False)
    jobs.run_pending()
    assert b["fake"].sub(sub)["status"] == "canceled" and tenant(tid)["status"] == "cancelled"


def test_purge_requires_the_subscription_cancelled(signed_up):
    b, tid, sub = signed_up
    sql("UPDATE tenants SET status = 'cancelled', purge_after = datetime('now', '+1 day') WHERE id = ?", tid)
    with pytest.raises(lifecycle.LifecycleError, match="cancel it first"):
        lifecycle.guard(tid, "purge")
    b["fake"].sub(sub)["status"] = "canceled"
    billing_mod.sync_subscription(sub)
    lifecycle.guard(tid, "purge")


# ── Card removed later (3a) ──────────────────────────────────────────────────

def test_card_removed_banner_email_then_cancelled_at_trial_end(signed_up):
    b, tid, sub = signed_up
    fake = b["fake"]
    pm = fake.sub(sub)["default_payment_method"]
    cus = fake.sub(sub)["customer"]
    fake.sub(sub)["default_payment_method"] = None
    fake.payment_methods[pm]["customer"] = None
    deliver("payment_method.detached", {"id": pm, "object": "payment_method", "customer": None},
            previous={"customer": cus})
    assert bsub(tid)["has_card"] == 0 and bsub(tid)["card_warning_sent_at"]
    assert sum("No card on file" in e[1] for e in b["emails"]) == 1
    deliver("customer.updated", {"id": cus, "object": "customer"})
    assert sum("No card on file" in e[1] for e in b["emails"]) == 1          # once
    # Stripe's missing_payment_method = cancel ends it at the trial's end.
    set_status(b, sub, "canceled", event="customer.subscription.deleted")
    assert tenant(tid)["status"] == "cancelled"


def test_reconciliation_fixes_drift_and_logs_it(signed_up):
    b, tid, sub = signed_up
    b["fake"].sub(sub).update(status="active", trial_end=None)        # a webhook we never got
    changes = billing_mod.reconcile()
    assert tenant(tid)["status"] == "active"
    assert changes and "trialing → active" in changes[0]
    assert sql("SELECT detail FROM provisioning_events WHERE event = 'billing_reconciled'")[0][0].startswith("abc:")
    assert billing_mod.reconcile() == []                                # nothing left to fix
