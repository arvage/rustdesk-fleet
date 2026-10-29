"""Billing plans in the console (step 2): prices checked with Stripe on
save, mismatches refused, public plans in order, archiving."""
import pytest

import fake_stripe
from app import plans
from app.deps import get_db
from conftest import provider_client


@pytest.fixture
def fake(monkeypatch):
    return fake_stripe.install(monkeypatch)


def _form(**kw):
    return {"key": "standard", "name": "Standard", "description": "For small MSPs", "sort_order": "1",
            "is_public": "1", "max_technicians": "", "max_devices": "", "price_flat": "", "price_technician": "",
            "price_device": "", **kw}


def _save(form, plan_id=None):
    conn = get_db()
    try:
        return plans.save(conn, plan_id, form)
    finally:
        conn.close()


def test_a_plan_with_flat_technician_and_device_prices(fake):
    flat = fake.add_price(4900)
    tech = fake.add_price(tiers=[{"up_to": 2, "unit_amount": 0, "flat_amount": None},
                                 {"up_to": None, "unit_amount": 500, "flat_amount": None}])
    dev = fake.add_price(100)
    pid = _save(_form(price_flat=flat, price_technician=tech, price_device=dev, max_devices="200"))
    conn = get_db()
    p = plans.get(conn, pid)
    prices = {r["unit_type"]: dict(r) for r in plans.prices_of(conn, pid)}
    conn.close()
    assert p["currency"] == "usd" and p["interval"] == "month" and p["max_devices"] == 200 and p["max_technicians"] is None
    assert prices["flat"]["summary"] == "$49.00 / month"
    assert prices["technician"]["included_quantity"] == 2
    assert prices["technician"]["summary"] == "2 included, then $5.00 per technician / month"
    assert prices["device"]["billing_scheme"] == "licensed" and prices["device"]["included_quantity"] == 0


def test_a_metered_device_price_with_a_meter(fake):
    meter = fake.add_meter("rdf_devices")
    dev = fake.add_price(5, usage_type="metered", meter=meter)
    pid = _save(_form(price_device=dev))
    conn = get_db()
    row = plans.prices_of(conn, pid)[0]
    conn.close()
    assert row["billing_scheme"] == "metered" and row["meter_event_name"] == "rdf_devices"


@pytest.mark.parametrize("make, message", [
    (lambda f: {"price_flat": f.add_price(currency="usd"), "price_device": f.add_price(currency="eur")}, "same currency"),
    (lambda f: {"price_flat": f.add_price(interval="month"), "price_device": f.add_price(interval="year")}, "same interval"),
    (lambda f: {"price_technician": f.add_price(usage_type="metered", meter=f.add_meter())}, "only the device price"),
    (lambda f: {"price_flat": f.add_price(active=False)}, "archived"),
    (lambda f: {"price_device": f.add_price(usage_type="metered")}, "no Billing Meter"),
    (lambda f: {"price_flat": "price_doesnotexist"}, "couldn't find"),
    (lambda f: {"price_flat": "prod_123"}, "doesn't look like"),
    (lambda f: {}, "at least one"),
])
def test_mismatches_are_refused(fake, make, message):
    with pytest.raises(plans.PlanError, match=message):
        _save(_form(**make(fake)))
    conn = get_db()
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    conn.close()


def test_public_plans_in_order_and_archiving(fake):
    a = _save(_form(key="basic", name="Basic", sort_order="2", price_flat=fake.add_price(1900)))
    b = _save(_form(key="pro", name="Pro", sort_order="1", price_flat=fake.add_price(9900)))
    _save(_form(key="secret", name="Secret", is_public="", price_flat=fake.add_price(1)))
    conn = get_db()
    assert [p["key"] for p in plans.public_plans(conn)] == ["pro", "basic"]
    plans.set_archived(conn, b, True)
    assert [p["key"] for p in plans.public_plans(conn)] == ["basic"]
    assert plans.offered(conn, b) is None and plans.offered(conn, a)["key"] == "basic"
    assert plans.get(conn, b)["archived_at"]          # kept for its subscribers
    conn.close()


def test_console_pages(fake):
    c = provider_client()
    assert c.get("/plans").status_code == 200
    flat = fake.add_price(4900)
    r = c.post("/plans", data=_form(price_flat=flat), follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    page = c.get(f"/plans/{pid}").text
    assert flat in page and "$49.00 / month" in page
    r = c.post(f"/plans/{pid}", data=_form(price_flat="price_nope"))
    assert r.status_code == 400 and "couldn&#39;t find" in r.text
    c.post(f"/plans/{pid}/archive")
    assert "archived" in c.get("/plans").text


def test_console_says_when_billing_is_off(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert "Billing is off" in provider_client().get("/plans").text
