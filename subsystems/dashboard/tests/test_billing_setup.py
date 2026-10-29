"""Provider billing, step 1: settings, the pinned Stripe client, the
billing tables and tenants.billing_mode."""
import pytest

import fake_stripe
from app import stripe_api
from app.deps import get_db
from conftest import add_tenant


def test_billing_modes_after_migration():
    add_tenant(2, "abc", "abc.rds.example.com")
    conn = get_db()
    modes = dict(conn.execute("SELECT id, billing_mode FROM tenants").fetchall())
    conn.close()
    assert modes[1] == "internal"           # tenant #1: never billed
    assert modes[2] == "manual"             # made by hand: as before phase 2


def test_billing_mode_is_checked():
    import sqlite3
    conn = get_db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tenants SET billing_mode = 'free' WHERE id = 1")
    conn.close()


def test_tables_exist():
    conn = get_db()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert {"plans", "plan_prices", "billing_customers", "billing_subscriptions", "stripe_events"} <= names


def test_billing_is_off_without_keys(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert not stripe_api.enabled()
    assert stripe_api.missing() == ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]
    with pytest.raises(stripe_api.BillingUnavailable, match="STRIPE_SECRET_KEY"):
        stripe_api.client()


def test_client_pins_the_api_version_and_goes_through_the_http_client(monkeypatch):
    fake = fake_stripe.install(monkeypatch)
    price = fake.add_price()
    got = stripe_api.client().v1.prices.retrieve(price)
    assert got.id == price
    assert fake.versions == {stripe_api.API_VERSION}
    assert stripe_api.mode() == "test"
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_live_x")
    assert stripe_api.mode() == "live"
    assert "/test/" not in stripe_api.dashboard_url("customers", "cus_1")


def test_api_version_matches_the_pinned_library():
    import re
    from pathlib import Path
    import stripe
    install_sh = (Path(__file__).resolve().parents[3] / "install.sh").read_text()
    pin = re.search(r'STRIPE_PIP="stripe==([\d.]+)"', install_sh).group(1)
    if stripe.VERSION == pin:
        assert stripe.api_version == stripe_api.API_VERSION
