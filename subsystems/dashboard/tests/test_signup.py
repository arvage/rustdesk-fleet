"""Self-service sign-up on the public host (step 3): the product page and
pricing, isolation from tenant hosts and the console, email verification
before anything is created in Stripe, the abuse limits, and Checkout always
collecting a card."""
import re
from datetime import timedelta

import pytest

import fake_stripe
from app import signup
from app.console import mail
from app.deps import get_db
from conftest import CONSOLE_HOST, HOST, add_tenant, new_client, provider_client

PUBLIC = "www.rds.example.com"


def _sql(sql, *params):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, html, event="": (sent.append((to, subject, html)) or (True, "")))
    return sent


@pytest.fixture
def fake(monkeypatch):
    return fake_stripe.install(monkeypatch)


@pytest.fixture
def plan(fake):
    from app import plans
    conn = get_db()
    pid = plans.save(conn, None, {"key": "standard", "name": "Standard", "is_public": "1", "sort_order": "1",
                                  "price_flat": fake.add_price(4900), "price_technician": fake.add_price(500),
                                  "price_device": fake.add_price(5, usage_type="metered", meter=fake.add_meter())})
    conn.close()
    return pid


def form(plan_id, **kw):
    return {"company": "ABC IT", "slug": "abc", "admin_name": "Ada", "email": "ada@abc.example",
            "password": "a-long-password", "plan": str(plan_id), "accept": "1", **kw}


def _submit(plan_id, ip="203.0.113.5", **kw):
    return new_client(PUBLIC, headers={"X-Real-IP": ip}).post("/signup", data=form(plan_id, **kw))


def _link(emails):
    return re.search(r"https://www\.rds\.example\.com/signup/verify\?token=([\w-]+)", emails[-1][2]).group(1)


# ── The public host ──────────────────────────────────────────────────────────

def test_product_page_with_pricing(plan):
    r = new_client(PUBLIC).get("/")
    assert r.status_code == 200
    assert "Standard" in r.text and "$49.00 / month" in r.text and 'href="/signup?plan=' in r.text
    assert "fonts.googleapis.com" not in r.text and "/static/site-fonts.css" in r.text
    nonce = re.search(r"'nonce-([^']+)'", r.headers.get("content-security-policy-report-only")
                      or r.headers["content-security-policy"]).group(1)
    assert all(tag == f'<script nonce="{nonce}">' for tag in re.findall(r"<script(?: [^>]*)?>", r.text))
    assert "github.com" not in r.text.split("<footer")[0].split("id=\"faq\"")[0]      # a product page, not a project page
    assert new_client(PUBLIC).get("/static/fonts/archivo-latin-standard-normal.woff2").status_code == 200


def test_public_routes_exist_only_on_the_public_host(plan):
    for path in ("/signup", "/signup/slug-check?slug=x", "/signin"):
        assert new_client(PUBLIC).get(path).status_code == 200, path
        assert new_client(HOST).get(path, follow_redirects=False).status_code in (303, 404), path
        assert provider_client().get(path, follow_redirects=False).status_code in (303, 404), path
    # Tenant and console routes aren't there.
    for path in ("/login", "/devices", "/admin", "/setup", "/plans", "/tenants/1"):
        assert new_client(PUBLIC).get(path, follow_redirects=False).status_code == 404, path


def test_signed_out_root_on_a_tenant_host_goes_to_login(admin):
    r = new_client(HOST).get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_signin_finds_the_accounts_address():
    add_tenant(2, "abc", "abc.rds.example.com")
    c = new_client(PUBLIC)
    r = c.get("/signin?account=ABC", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://abc.rds.example.com/login"
    assert "couldn" in c.get("/signin?account=nope").text


# ── When sign-up is open ─────────────────────────────────────────────────────

def test_closed_without_billing_or_plans(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert "isn&#39;t available" in new_client(PUBLIC).get("/signup").text
    fake_stripe.install(monkeypatch)
    assert "isn&#39;t available" in new_client(PUBLIC).get("/signup").text      # no public plans


def test_closed_when_relay_ports_run_low(plan, monkeypatch):
    monkeypatch.setenv("RELAY_PORT_MAX", "21239")       # blocks 21200..21230: four
    monkeypatch.setenv("SIGNUP_MIN_FREE_BLOCKS", "5")
    assert "capacity" in new_client(PUBLIC).get("/signup").text
    monkeypatch.setenv("SIGNUP_MIN_FREE_BLOCKS", "4")
    assert new_client(PUBLIC).get("/signup").status_code == 200


def test_closed_after_signups_per_day(plan, monkeypatch, emails):
    monkeypatch.setenv("SIGNUPS_PER_DAY", "1")
    _submit(plan)
    _sql("UPDATE signups SET status = 'created'")
    r = new_client(PUBLIC).get("/signup")
    assert "today" in r.text
    assert _submit(plan, slug="other", email="b@abc.example").status_code == 400


# ── The form ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad, message", [
    ({"slug": "admin"}, "reserved"), ({"slug": "A B"}, "lowercase"), ({"password": "short"}, "10 characters"),
    ({"accept": ""}, "accept"), ({"email": "nope"}, "email"), ({"company": ""}, "company"), ({"plan": "999"}, "plan"),
])
def test_form_rules(plan, emails, bad, message):
    r = _submit(plan, **bad)
    assert r.status_code == 400 and message in r.text.lower() or message in r.text
    assert _sql("SELECT COUNT(*) FROM signups")[0][0] == 0 and not emails


def test_slug_taken_by_a_tenant_or_a_signup_in_progress(plan, emails):
    add_tenant(2, "globex", "globex.rds.example.com")
    assert "taken" in _submit(plan, slug="globex").text
    _submit(plan, slug="abc")
    assert "taken" in _submit(plan, slug="abc", email="other@abc.example").text
    c = new_client(PUBLIC)
    assert c.get("/signup/slug-check?slug=abc").json()["ok"] is False
    assert c.get("/signup/slug-check?slug=fresh").json() == {"slug": "fresh", "ok": True, "problem": "",
                                                            "host": "fresh.rds.example.com"}
    assert c.get("/signup/slug-check?name=Acme IT, Inc.").json()["slug"] == "acme-it-inc"
    # The console can't take a slug held by a sign-up either.
    from app.console.tenants import slug_error
    conn = get_db()
    assert "sign-up in progress" in slug_error(conn, "abc")
    conn.close()


def test_rate_limits_per_ip_and_per_email(plan, emails, monkeypatch):
    monkeypatch.setenv("SIGNUP_IP_LIMIT", "2")
    monkeypatch.setenv("SIGNUP_EMAIL_LIMIT", "2")
    assert _submit(plan, slug="aaa1", email="1@x.example").status_code == 200
    assert _submit(plan, slug="aaa2", email="2@x.example").status_code == 200
    r = _submit(plan, slug="aaa3", email="3@x.example")
    assert r.status_code == 400 and "your network" in r.text
    assert _submit(plan, ip="198.51.100.1", slug="aaa4", email="1@x.example").status_code == 200
    r = _submit(plan, ip="198.51.100.2", slug="aaa5", email="1@x.example")
    assert r.status_code == 400 and "this email address" in r.text


# ── Verification before anything else ───────────────────────────────────────

def test_nothing_is_created_before_verification(plan, emails, fake):
    r = _submit(plan)
    assert r.status_code == 200 and "Check your email" in r.text
    row = _sql("SELECT * FROM signups")[0]
    assert row["status"] == "pending_verification" and row["password_hash"].startswith("$2")
    assert "a-long-password" not in str(dict(row))
    assert row["accepted_ip"] == "203.0.113.5" and row["accepted_at"]
    assert not fake.requests("POST", "/v1/customers") and not fake.requests("POST", "/v1/checkout")
    from app import tenancy
    assert _sql("SELECT COUNT(*) FROM tenants")[0][0] == 1
    assert emails[-1][0] == ["ada@abc.example"]


def test_verification_then_checkout(plan, emails, fake):
    _submit(plan)
    token = _link(emails)
    c = new_client(PUBLIC)
    # Opening the link (or a mail scanner opening it) spends nothing.
    assert c.get(f"/signup/verify?token={token}").status_code == 200
    assert not fake.requests("POST", "/v1/customers")
    r = c.post("/signup/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.example/")
    (cus,) = fake.requests("POST", "/v1/customers")
    assert cus["email"] == "ada@abc.example" and cus["metadata"]["slug"] == "abc"
    (cs,) = fake.requests("POST", "/v1/checkout/sessions")
    assert cs["mode"] == "subscription" and cs["payment_method_collection"] == "always"
    assert cs["subscription_data"]["trial_period_days"] == "14"
    assert cs["subscription_data"]["trial_settings"]["end_behavior"]["missing_payment_method"] == "cancel"
    assert cs["billing_address_collection"] == "required"
    assert len(cs["line_items"]) == 3 and "quantity" not in cs["line_items"][2]     # metered device price
    row = _sql("SELECT * FROM signups")[0]
    assert row["status"] == "checkout" and row["verified_at"] and row["stripe_checkout_id"].startswith("cs_")
    # Single use.
    r = c.post("/signup/verify", data={"token": token})
    assert r.status_code == 400 and "already used" in r.text
    assert len(fake.requests("POST", "/v1/customers")) == 1


def test_link_expires_after_24_hours(plan, emails, fake):
    _submit(plan)
    token = _link(emails)
    _sql("UPDATE signups SET verify_expires_at = ?", signup.fmt(signup.now() - timedelta(seconds=1)))
    r = new_client(PUBLIC).post("/signup/verify", data={"token": token})
    assert r.status_code == 400 and "expired" in r.text and not fake.requests("POST", "/v1/customers")


def test_stripe_tax_switch(plan, emails, fake, monkeypatch):
    monkeypatch.setenv("STRIPE_TAX", "on")
    _submit(plan)
    new_client(PUBLIC).post("/signup/verify", data={"token": _link(emails)})
    assert fake.requests("POST", "/v1/checkout/sessions")[0]["automatic_tax"] == {"enabled": "true"}


def test_back_from_checkout_gets_a_new_session_for_the_same_customer(plan, emails, fake):
    _submit(plan)
    c = new_client(PUBLIC)
    c.post("/signup/verify", data={"token": _link(emails)})
    ref = _sql("SELECT ref FROM signups")[0][0]
    assert "Enter payment details" in c.get(f"/signup/{ref}/progress?cancelled=1").text
    r = c.post(f"/signup/{ref}/checkout", follow_redirects=False)
    assert r.status_code == 303
    sessions = fake.requests("POST", "/v1/checkout/sessions")
    assert len(sessions) == 2 and sessions[0]["customer"] == sessions[1]["customer"]
    assert all(s["payment_method_collection"] == "always" for s in sessions)
    assert len(fake.requests("POST", "/v1/customers")) == 1


def test_unverified_signups_are_deleted_after_48_hours(plan, emails):
    _submit(plan)
    from app.console import maintenance
    maintenance._signups()
    assert _sql("SELECT COUNT(*) FROM signups")[0][0] == 1
    _sql("UPDATE signups SET created_at = ?", signup.fmt(signup.now() - timedelta(hours=49)))
    maintenance._signups()
    assert _sql("SELECT COUNT(*) FROM signups")[0][0] == 0


def test_helper_accepts_the_public_host(tmp_path):
    from test_rdf_helper import H, cfg
    c = cfg(tmp_path)
    assert H.check_host(c, PUBLIC) == PUBLIC and H.check_web_host(c, PUBLIC) == PUBLIC
    assert not H.is_custom(c, PUBLIC)
    c2 = H.Config("rds.example.com", tmp_path, public_site_host="example.org")
    assert H.check_web_host(c2, "example.org") == "example.org"
    with pytest.raises(H.HelperError):
        H.check_host(c2, PUBLIC)       # www.<domain> only by default


def test_test_clock_only_in_test_mode(plan, emails, fake, monkeypatch):
    monkeypatch.setenv("STRIPE_TEST_CLOCK", "clock_123")
    _submit(plan)
    new_client(PUBLIC).post("/signup/verify", data={"token": _link(emails)})
    assert fake.requests("POST", "/v1/customers")[0]["test_clock"] == "clock_123"
    from app import settings
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_live_x")
    assert settings.stripe_test_clock() == ""
