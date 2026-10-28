"""A tenant sees its subscription on the home page, and a trial tenant sees a
caution on every page; the provider activates a trial from the console."""
from conftest import HOST_B, add_tenant, create_user, db_rows, provider_client, signed_in, signed_in_at
from app.deps import get_db

NOTICE = "is on a trial"
PAGES = ("/devices", "/groups", "/help", "/sessions")


def _trial_tenant() -> dict:
    add_tenant(2, "globex", HOST_B, status="trial")
    conn = get_db()
    conn.execute("UPDATE tenants SET plan = 'Pilot, 25 devices' WHERE id = 2")
    conn.commit()
    conn.close()
    return create_user("admin@globex.example.com", "admin", tenant_id=2)


def test_trial_tenant_sees_its_subscription_and_the_caution_everywhere():
    c = signed_in_at(_trial_tenant(), HOST_B)
    home = c.get("/").text
    assert "Subscription" in home and "Trial" in home and "Pilot, 25 devices" in home
    assert NOTICE in home
    for path in PAGES:
        r = c.get(path)
        assert r.status_code == 200, path
        assert NOTICE in r.text, path


def test_active_tenant_sees_its_status_but_no_caution():
    c = signed_in(create_user("admin@example.com", "admin"))
    home = c.get("/").text
    assert "Subscription" in home and "Active" in home
    for path in ("/",) + PAGES:
        assert NOTICE not in c.get(path).text, path


def test_provider_activates_a_trial_and_the_caution_goes():
    user = _trial_tenant()
    tenant_c = signed_in_at(user, HOST_B)
    assert NOTICE in tenant_c.get("/devices").text
    r = provider_client().post("/tenants/2/activate", follow_redirects=False)
    assert r.headers["location"] == "/tenants/2"
    assert db_rows("SELECT status FROM tenants WHERE id = 2")[0][0] == "active"
    assert db_rows("SELECT 1 FROM provisioning_events WHERE tenant_id = 2 AND event = 'tenant_activated'"
                   " AND source = 'provider'")
    assert NOTICE not in tenant_c.get("/devices").text


def test_only_a_trial_can_be_activated():
    add_tenant(2, "globex", HOST_B, status="suspended")
    provider_client().post("/tenants/2/activate", follow_redirects=False)
    assert db_rows("SELECT status FROM tenants WHERE id = 2")[0][0] == "suspended"
