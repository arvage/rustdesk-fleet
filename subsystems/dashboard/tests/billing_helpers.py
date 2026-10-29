"""Shared pieces for the billing tests: a fake Stripe, fake docker/helper,
captured emails, a public plan, and helpers to sign up, finish Checkout and
deliver webhooks the way Stripe would."""
import re

import pytest

import fake_stripe
from app.console import jobs, mail, tenants, web
from app.deps import get_db
from app.tenancy import invalidate_cache
from conftest import new_client

PUBLIC = "www.rds.example.com"


def sql(query, *params):
    conn = get_db()
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


@pytest.fixture
def billing(monkeypatch, fake_system):
    """Everything a sign-up needs, faked."""
    from app import plans
    fake = fake_stripe.install(monkeypatch)
    emails, alerts = [], []
    monkeypatch.setattr(mail, "send", lambda to, subject, html, event="": (emails.append((to, subject, html)) or (True, "")))
    monkeypatch.setattr(mail, "send_to_provider_admins",
                        lambda subject, html, event="": (alerts.append(subject) or (True, "")))

    def guides(tenant_id, name, host):
        d = tenants.guides_dir(tenant_id)
        d.mkdir(parents=True, exist_ok=True)
        return []
    monkeypatch.setattr(tenants, "generate_guides", guides)
    monkeypatch.setattr(web, "cert_expiry", lambda host, timeout=5.0: "2027-01-25 10:00:00")
    flat = fake.add_price(4900)
    tech = fake.add_price(tiers=[{"up_to": 2, "unit_amount": 0, "flat_amount": None},
                                 {"up_to": None, "unit_amount": 500, "flat_amount": None}])
    dev = fake.add_price(100)
    conn = get_db()
    pid = plans.save(conn, None, {"key": "standard", "name": "Standard", "is_public": "1", "sort_order": "1",
                                  "max_technicians": "3", "max_devices": "2",
                                  "price_flat": flat, "price_technician": tech, "price_device": dev})
    conn.close()
    return {"fake": fake, "emails": emails, "alerts": alerts, "plan": pid, "system": fake_system,
            "prices": {"flat": flat, "technician": tech, "device": dev}}


def start_signup(b, slug="abc", email="ada@abc.example", ip="203.0.113.5", plan=None):
    """Form, verification link, Checkout: returns the sign-up row."""
    c = new_client(PUBLIC, headers={"X-Real-IP": ip})
    r = c.post("/signup", data={"company": "ABC IT", "slug": slug, "admin_name": "Ada", "email": email,
                                "password": "a-long-password", "plan": str(plan or b["plan"]), "accept": "1"})
    assert r.status_code == 200, r.text[:300]
    token = re.search(r"/signup/verify\?token=([\w-]+)", b["emails"][-1][2]).group(1)
    r = c.post("/signup/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return sql("SELECT * FROM signups WHERE slug = ?", slug)[0]


def deliver(etype, obj, eid=None):
    """Post a signed event to the webhook and run the queued jobs."""
    body, sig = fake_stripe.signed(fake_stripe.event(etype, obj, eid))
    r = new_client(PUBLIC).post("/stripe/webhook", content=body,
                                headers={"Stripe-Signature": sig, "Content-Type": "application/json"})
    jobs.run_pending()
    invalidate_cache()
    return r


def finish_checkout(b, row, fingerprint="fp_card_1", card=True):
    """The customer pays on Stripe's page; Stripe sends checkout.session.completed."""
    sub = b["fake"].complete_checkout(row["stripe_checkout_id"], fingerprint, card)
    deliver("checkout.session.completed", {"id": row["stripe_checkout_id"], "object": "checkout.session"})
    return sub


def sign_up(b, **kw):
    """All the way: returns (tenant id, subscription id)."""
    row = start_signup(b, **kw)
    sub = finish_checkout(b, row, "fp_" + row["slug"])
    tid = sql("SELECT tenant_id FROM signups WHERE id = ?", row["id"])[0][0]
    return tid, sub
