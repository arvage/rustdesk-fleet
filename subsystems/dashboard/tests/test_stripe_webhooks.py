"""The Stripe webhook (step 4) and the "no card, no tenant" rule at
checkout completion (step 3a): signatures, idempotency, state fetched from
Stripe, the tenant created by the job with the chosen password, one trial
per card, and creation failures."""
import json
import time

import pytest

import fake_stripe
from app.console import jobs
from billing_helpers import PUBLIC, billing, deliver, finish_checkout, sign_up, sql, start_signup  # noqa: F401
from conftest import PASSWORD, new_client


def _post(body, sig):
    return new_client(PUBLIC).post("/stripe/webhook", content=body,
                                   headers={"Stripe-Signature": sig, "Content-Type": "application/json"})


# ── The endpoint ─────────────────────────────────────────────────────────────

def test_bad_signature_stale_timestamp_and_oversized_body_are_rejected(billing):
    ev = fake_stripe.event("customer.subscription.updated", {"id": "sub_x"})
    body, _ = fake_stripe.signed(ev)
    _, wrong = fake_stripe.signed(ev, secret="whsec_someone_else")
    assert _post(body, wrong).status_code == 400
    assert _post(body, "").status_code == 400
    stale_body, stale = fake_stripe.signed(ev, at=time.time() - 600)
    assert _post(stale_body, stale).status_code == 400
    big = json.dumps({**ev, "pad": "x" * 300_000}).encode()
    _, sig = fake_stripe.signed(json.loads(big))
    assert _post(big, sig).status_code == 400
    assert sql("SELECT COUNT(*) FROM stripe_events")[0][0] == 0
    ok_body, ok = fake_stripe.signed(ev)
    assert _post(ok_body, ok).status_code == 200


def test_only_on_the_public_host():
    assert new_client("rds.example.com").post("/stripe/webhook", content=b"{}").status_code in (403, 404)


def test_a_replayed_event_is_acknowledged_and_ignored(billing):
    ev = fake_stripe.event("customer.subscription.updated", {"id": "sub_x"})
    body, sig = fake_stripe.signed(ev)
    assert _post(body, sig).json()["result"] == "queued"
    assert _post(body, sig).json()["result"] == "duplicate"
    assert sql("SELECT COUNT(*) FROM stripe_events")[0][0] == 1
    assert len([j for j in sql("SELECT kind FROM provider_jobs") if j[0] == "stripe_event"]) == 1


def test_other_events_are_logged_and_ignored(billing):
    body, sig = fake_stripe.signed(fake_stripe.event("charge.refunded", {"id": "ch_1"}))
    assert _post(body, sig).status_code == 200
    row = sql("SELECT * FROM stripe_events")[0]
    assert row["type"] == "charge.refunded" and row["processed_at"] and not row["error"]
    assert sql("SELECT COUNT(*) FROM provider_jobs")[0][0] == 0


def test_the_webhook_is_the_only_new_origin_exemption():
    from app.security import ORIGIN_EXEMPT_PATHS
    from app.tenancy import DEVICE_API_PATHS
    assert set(ORIGIN_EXEMPT_PATHS) - set(DEVICE_API_PATHS) == {"/stripe/webhook", "/csp-report"}


# ── Checkout completed ───────────────────────────────────────────────────────

def test_checkout_completed_creates_the_tenant_with_the_chosen_password(billing):
    row = start_signup(billing)
    assert sql("SELECT COUNT(*) FROM tenants")[0][0] == 1          # nothing before the webhook
    sub = finish_checkout(billing, row)
    su = sql("SELECT * FROM signups WHERE id = ?", row["id"])[0]
    assert su["status"] == "created" and su["tenant_id"] and su["stripe_subscription_id"] == sub
    t = sql("SELECT * FROM tenants WHERE id = ?", su["tenant_id"])[0]
    assert t["slug"] == "abc" and t["billing_mode"] == "stripe"
    assert sql("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", t["id"])[0][0] == row["stripe_customer_id"]
    bs = sql("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", t["id"])[0]
    assert bs["stripe_subscription_id"] == sub and bs["status"] == "trialing" and bs["has_card"] == 1
    assert sql("SELECT COUNT(*) FROM tenant_invites WHERE tenant_id = ?", t["id"])[0][0] == 0     # no invite needed
    c = new_client("abc.rds.example.com")
    r = c.post("/login", data={"email": "ada@abc.example", "password": "a-long-password"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert any("account is ready" in e[1] for e in billing["emails"])
    assert billing["fake"].sub(sub)["metadata"]["tenant_id"] == str(t["id"])
    # The progress page says so.
    state = new_client(PUBLIC).get(f"/signup/{row['ref']}/status").json()
    assert state["status"] == "created" and state["login"] == "https://abc.rds.example.com/login"


def test_a_redelivered_checkout_event_creates_one_tenant(billing):
    row = start_signup(billing)
    sub = finish_checkout(billing, row)
    deliver("checkout.session.completed", {"id": row["stripe_checkout_id"]})    # new event id, same session
    assert sql("SELECT COUNT(*) FROM tenants WHERE slug = 'abc'")[0][0] == 1
    assert len([j for j in sql("SELECT kind FROM provider_jobs") if j[0] == "create_tenant"]) == 1


def test_no_card_no_tenant(billing):
    row = start_signup(billing)
    sub = finish_checkout(billing, row, card=False)
    su = sql("SELECT * FROM signups WHERE id = ?", row["id"])[0]
    assert su["status"] == "failed" and su["failure_reason"] == "no payment method" and not su["tenant_id"]
    assert sql("SELECT COUNT(*) FROM tenants WHERE slug = 'abc'")[0][0] == 0
    assert billing["fake"].sub(sub)["status"] == "canceled"
    assert any("no payment method" in a for a in billing["alerts"])
    assert not [j for j in sql("SELECT kind FROM provider_jobs") if j[0] == "create_tenant"]


def test_a_non_card_default_payment_method_is_no_card(billing):
    row = start_signup(billing)
    fake = billing["fake"]
    sub = fake.complete_checkout(row["stripe_checkout_id"], card=False)
    pm = fake.add_card(row["stripe_customer_id"], kind="link")
    fake.sub(sub)["default_payment_method"] = pm
    deliver("checkout.session.completed", {"id": row["stripe_checkout_id"]})
    assert sql("SELECT status FROM signups WHERE id = ?", row["id"])[0][0] == "failed"


def test_one_trial_per_card_surviving_a_purge(billing):
    tid, sub = sign_up(billing, slug="first")
    assert billing["fake"].sub(sub)["status"] == "trialing"
    # Same card again (fingerprint "fp_first"): no trial.
    row = start_signup(billing, slug="second", email="b@second.example", ip="198.51.100.9")
    sub2 = finish_checkout(billing, row, fingerprint="fp_first")
    assert billing["fake"].sub(sub2)["status"] == "active"
    assert {"trial_end": "now"}.items() <= billing["fake"].requests("POST", f"/v1/subscriptions/{sub2}")[0].items()
    su = sql("SELECT * FROM signups WHERE id = ?", row["id"])[0]
    assert su["trial_denied"] == 1 and su["status"] == "created"
    assert new_client(PUBLIC).get(f"/signup/{row['ref']}/status").json()["trial_denied"] is True
    # The fingerprint table outlives the first tenant's purge.
    from app.console import tenants
    from app.deps import get_db
    tenants.delete_tenant_rows(get_db(), tid, keep_provider_events=True)
    sql("UPDATE tenants SET status = 'purged' WHERE id = ?", tid)
    assert sql("SELECT signup_id FROM trial_cards WHERE fingerprint = 'fp_first'")
    row3 = start_signup(billing, slug="third", email="c@third.example", ip="198.51.100.10")
    sub3 = finish_checkout(billing, row3, fingerprint="fp_first")
    assert billing["fake"].sub(sub3)["status"] == "active"


def test_creation_failure_leaves_a_retryable_signup_and_alerts(billing):
    billing["system"].fail["issue-cert abc.rds.example.com"] = "rate limited"
    row = start_signup(billing)
    sub = finish_checkout(billing, row)
    su = sql("SELECT * FROM signups WHERE id = ?", row["id"])[0]
    assert su["status"] == "failed" and "rate limited" in su["failure_reason"] and not su["tenant_id"]
    assert billing["fake"].sub(sub)["status"] == "trialing"                    # Stripe subscription kept
    assert any("tenant creation failed" in a for a in billing["alerts"])
    assert sql("SELECT COUNT(*) FROM tenants WHERE slug = 'abc'")[0][0] == 0
    assert "We're on it" in new_client(PUBLIC).get(f"/signup/{row['ref']}/progress").text
    # Nobody else can take the slug meanwhile, and a retry reuses it.
    from app import billing as billing_mod
    billing["system"].fail.clear()
    billing_mod.start_creation(row["id"], "ops@example.com")
    jobs.run_pending()
    su = sql("SELECT * FROM signups WHERE id = ?", row["id"])[0]
    assert su["status"] == "created" and sql("SELECT slug FROM tenants WHERE id = ?", su["tenant_id"])[0][0] == "abc"


def test_subscription_events_fetch_state_from_stripe(billing):
    tid, sub = sign_up(billing)
    billing["fake"].sub(sub)["cancel_at_period_end"] = True
    # The event's payload says otherwise; the fetched subscription wins.
    deliver("customer.subscription.updated", {"id": sub, "cancel_at_period_end": False, "status": "canceled"})
    bs = sql("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", tid)[0]
    assert bs["cancel_at_period_end"] == 1 and bs["status"] == "trialing"


def test_tenant_creation_has_exactly_two_callers():
    """No card, no tenant: the creation job is queued only by the console's
    manual creation and the paid sign-up flow."""
    import ast
    from pathlib import Path
    app_dir = Path(__file__).resolve().parents[1] / "app"
    callers = []
    for f in app_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == "create_tenant" and getattr(node.func, "attr", "") != "handler":
                callers.append(f.relative_to(app_dir).as_posix())
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "create_tenant":
                callers.append(f.relative_to(app_dir).as_posix() + " (direct)")
    assert sorted(callers) == ["billing.py", "console/tenant_pages.py"], callers


def test_events_out_of_order_end_in_the_right_state(billing):
    tid, sub = sign_up(billing)
    fake = billing["fake"]
    fake.sub(sub).update(status="active", trial_end=None)
    newer = fake_stripe.event("customer.subscription.updated", {"id": sub, "status": "active"})
    older = fake_stripe.event("customer.subscription.created", {"id": sub, "status": "trialing"})
    for ev in (newer, older):                       # the older one arrives last
        body, sig = fake_stripe.signed(ev)
        _post(body, sig)
    jobs.run_pending()
    assert sql("SELECT status FROM tenants WHERE id = ?", tid)[0][0] == "active"
    assert sql("SELECT status FROM billing_subscriptions WHERE tenant_id = ?", tid)[0][0] == "active"


def test_every_checkout_session_goes_through_checkout_params():
    """No card, no tenant: every Checkout Session is built by
    signup.checkout_params (card always collected, a trial without a card
    cancels); the two values are constants, not settings."""
    import ast
    from pathlib import Path
    from app import signup
    assert signup.PAYMENT_METHOD_COLLECTION == "always" and signup.MISSING_PAYMENT_METHOD == "cancel"
    app_dir = Path(__file__).resolve().parents[1] / "app"
    creates = []
    for f in app_dir.rglob("*.py"):
        tree = ast.parse(f.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "create" and "checkout" in ast.unparse(n.func)]
            for call in calls:
                creates.append(f"{f.relative_to(app_dir)}:{fn.name}")
                built = any(isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "checkout_params"
                            for n in ast.walk(fn))
                assert built, f"{f.name}:{fn.name} creates a Checkout Session without checkout_params"
    assert sorted(creates) == ["billing.py:resubscribe_url", "signup.py:create_checkout"], creates
    literals = [n.value for f in app_dir.rglob("*.py") for n in ast.walk(ast.parse(f.read_text()))
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value == "if_required"]
    assert literals == []


def test_paying_with_link_tells_the_customer_and_lets_them_retry_with_a_card(billing):
    """What happened on the live box: Checkout paid with Link (type 'link'),
    which has no card fingerprint, so no tenant. The page must say what to do,
    the slug must not stay held, and the same sign-up can try again."""
    row = start_signup(billing)
    fake = billing["fake"]
    sub = fake.complete_checkout(row["stripe_checkout_id"], card=False)
    fake.sub(sub)["default_payment_method"] = fake.add_card(row["stripe_customer_id"], kind="link", default=False)
    deliver("checkout.session.completed", {"id": row["stripe_checkout_id"]})
    su = sql("SELECT * FROM signups WHERE id = ?", row["id"])[0]
    assert su["status"] == "failed" and su["failure_reason"] == "no payment method"
    page = new_client(PUBLIC).get(f"/signup/{row['ref']}/progress").text
    assert "We need a card" in page and "We're on it" not in page and "Link" in page
    assert new_client(PUBLIC).get(f"/signup/{row['ref']}/status").json()["no_card"] is True
    # The slug is free again (the sign-up isn't going anywhere by itself)...
    from app import signup
    from app.deps import get_db
    conn = get_db()
    assert signup.slug_problem(conn, "abc") is None
    conn.close()
    # ...and "Enter a card" reopens the same sign-up with a new Checkout for the same customer.
    r = new_client(PUBLIC).post(f"/signup/{row['ref']}/checkout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.example/")
    sessions = fake.requests("POST", "/v1/checkout/sessions")
    assert len(sessions) == 2 and sessions[1]["customer"] == sessions[0]["customer"]
    assert sessions[1]["payment_method_collection"] == "always"
    su = sql("SELECT * FROM signups WHERE id = ?", row["id"])[0]
    assert su["status"] == "checkout" and su["stripe_subscription_id"] is None
    finish_checkout(billing, sql("SELECT * FROM signups WHERE id = ?", row["id"])[0])
    assert sql("SELECT status FROM signups WHERE id = ?", row["id"])[0][0] == "created"


def test_retry_with_a_card_refuses_a_slug_taken_meanwhile(billing):
    row = start_signup(billing)
    finish_checkout(billing, row, card=False)
    other = start_signup(billing, slug="abc", email="b@other.example", ip="198.51.100.40")
    r = new_client(PUBLIC).post(f"/signup/{row['ref']}/checkout")
    assert r.status_code == 400 and "taken" in r.text
