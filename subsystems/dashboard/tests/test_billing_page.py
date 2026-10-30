"""The tenant's Billing page (step 7): manage_billing only, 404 for provider
support, the Customer Portal, plan changes, banners, and no Stripe for
manual and internal tenants."""
from datetime import datetime, timedelta, timezone

import pytest

from app import billing as billing_mod
from billing_helpers import PUBLIC, billing, deliver, sign_up, sql  # noqa: F401
from conftest import HOST, add_tenant, create_user, new_client, signed_in_at
from test_billing_status import set_status


def _login(tid, email="ada@abc.example", password="a-long-password", host="abc.rds.example.com"):
    c = new_client(host)
    r = c.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture
def tenant(billing):
    tid, sub = sign_up(billing)
    return billing, tid, sub


def test_the_page(tenant):
    b, tid, sub = tenant
    b["fake"].invoices["in_1"] = {"id": "in_1", "object": "invoice", "customer": b["fake"].sub(sub)["customer"],
                                  "number": "ABC-0001", "created": 1790000000, "amount_paid": 4900, "currency": "usd",
                                  "status": "paid", "hosted_invoice_url": "https://invoice.stripe.example/i/1"}
    text = _login(tid).get("/admin/billing").text
    for s in ("Standard", "Free trial", "1 technicians · 0 devices", "ABC-0001", "$49.00", "https://invoice.stripe.example/i/1",
              "Manage billing", "Change plan"):
        assert s in text, s


def test_only_manage_billing(tenant):
    b, tid, sub = tenant
    tech = create_user("tech@abc.example", "tech", tenant_id=tid)
    c = signed_in_at(tech, "abc.rds.example.com")
    assert c.get("/admin/billing", follow_redirects=False).status_code in (303, 403)
    n = len(b["fake"].portal_sessions)
    c.post("/admin/billing/portal")
    assert len(b["fake"].portal_sessions) == n


def test_support_sessions_get_404(two_tenants, tenant):
    from test_support_access import _support_client
    c = _support_client(two_tenants)
    for method, path in (("GET", "/admin/billing"), ("POST", "/admin/billing/portal"), ("POST", "/admin/billing/plan")):
        r = c.request(method, path, data={"plan": "1"} if path.endswith("plan") else None, follow_redirects=False)
        assert r.status_code == 404, path
    assert "/admin/billing" not in c.get("/").text


def test_manual_and_internal_tenants_see_no_stripe(two_tenants, tenant):
    b, tid, sub = tenant
    n = len(b["fake"].calls)
    a = signed_in_at(two_tenants["a"]["admin"], HOST)
    assert "isn&#39;t billed" in a.get("/admin/billing").text or "isn't billed" in a.get("/admin/billing").text
    bt = signed_in_at(two_tenants["b"]["admin"], "b.rds.example.com")
    assert "bills this account directly" in bt.get("/admin/billing").text
    assert bt.post("/admin/billing/portal").status_code == 404
    assert len(b["fake"].calls) == n


def test_portal(tenant):
    b, tid, sub = tenant
    r = _login(tid).post("/admin/billing/portal", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://billing.stripe.example/")
    sess = b["fake"].portal_sessions[-1]
    assert sess["return_url"] == "https://abc.rds.example.com/admin/billing"
    cfg = b["fake"].portal_configurations[sess["configuration"]]
    f = cfg["features"]
    assert f["payment_method_update"]["enabled"] == "true" and f["invoice_history"]["enabled"] == "true"
    assert f["subscription_cancel"]["mode"] == "at_period_end"
    # The plan has three prices, so plans are switched on our page, not the portal.
    assert f["subscription_update"]["enabled"] == "false"


def test_portal_switches_plans_when_every_public_plan_is_one_price(billing):
    from app import plans
    from app.deps import get_db
    conn = get_db()
    plans.set_archived(conn, billing["plan"], True)
    for key, amount in (("solo", 1900), ("team", 4900)):
        plans.save(conn, None, {"key": key, "name": key.title(), "is_public": "1", "sort_order": "0",
                                "price_flat": billing["fake"].add_price(amount)})
    features = billing_mod.portal_features(conn)
    conn.close()
    upd = features["subscription_update"]
    assert upd["enabled"] and len(upd["products"]) == 2 and upd["default_allowed_updates"] == ["price"]


def test_change_plan(tenant):
    b, tid, sub = tenant
    from app import plans
    from app.deps import get_db
    conn = get_db()
    pro = plans.save(conn, None, {"key": "pro", "name": "Pro", "is_public": "1", "sort_order": "2",
                                  "price_flat": b["fake"].add_price(9900), "price_technician": b["fake"].add_price(800)})
    conn.close()
    _login(tid).post("/admin/billing/plan", data={"plan": str(pro)})
    items = b["fake"].sub(sub)["items"]["data"]
    assert sorted(i["price"]["unit_amount"] for i in items) == [800, 9900]
    assert sql("SELECT plan_id FROM billing_subscriptions WHERE tenant_id = ?", tid)[0][0] == pro


def _banners(tid):
    return _login(tid).get("/devices").text


def test_banners(tenant):
    b, tid, sub = tenant
    soon = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
    set_status(b, sub, "trialing", trial_end=soon)
    assert "free trial ends on" in _banners(tid)
    set_status(b, sub, "past_due", event="invoice.payment_failed")
    assert "last payment failed" in _banners(tid)
    b["fake"].sub(sub).update(status="active", trial_end=None, cancel_at_period_end=True)
    deliver("customer.subscription.updated", {"id": sub})
    assert "Your subscription ends on" in _banners(tid)
    for i in range(3):
        sql("INSERT INTO devices (tenant_id, rustdesk_id, status, hidden) VALUES (?, ?, 'registered', 0)", tid, f"60000000{i}")
    assert "over your plan" in _banners(tid)
    b["fake"].sub(sub)["default_payment_method"] = None
    cus = b["fake"].sub(sub)["customer"]
    b["fake"].customers[cus]["invoice_settings"]["default_payment_method"] = None
    deliver("customer.updated", {"id": cus})
    assert "no card on file" in _banners(tid)
    # A technician doesn't see billing banners.
    tech = create_user("tech@abc.example", "tech", tenant_id=tid)
    assert "no card on file" not in signed_in_at(tech, "abc.rds.example.com").get("/devices").text


def test_portal_cancellation_with_a_date(tenant):
    """The portal cancels with cancel_at, not cancel_at_period_end: the page
    shows the end date and no next invoice, without a Stripe error."""
    b, tid, sub = tenant
    ends = datetime.now(timezone.utc) + timedelta(days=20)
    b["fake"].sub(sub).update(status="active", trial_end=None, cancel_at=int(ends.timestamp()))
    deliver("customer.subscription.updated", {"id": sub})
    c = _login(tid)
    assert "Your subscription ends on" in c.get("/devices").text
    page = c.get("/admin/billing").text
    assert "couldn't be reached" not in page and "ends " in page
    # Before the webhook lands the page still loads.
    sql("UPDATE billing_subscriptions SET cancel_at_period_end = 0 WHERE tenant_id = ?", tid)
    assert "couldn't be reached" not in c.get("/admin/billing").text


def test_suspended_for_billing_admins_reach_the_billing_page(tenant):
    b, tid, sub = tenant
    set_status(b, sub, "unpaid")
    c = _login(tid)
    page = c.get("/admin/billing")
    assert page.status_code == 200 and "suspended for non-payment" in page.text
    assert c.post("/admin/billing/portal", follow_redirects=False).headers["location"].startswith("https://billing.stripe")
    assert c.get("/devices", follow_redirects=False).status_code == 403
