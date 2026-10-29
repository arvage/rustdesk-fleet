"""Keeping custom domains healthy (step 6): the daily re-check, the 7 days
a broken domain is still served, then release with the home host primary
again. DNS and rdf-helper are faked."""
from datetime import timedelta

from app import domains, tenancy
from app.deps import get_db
from test_domains_page import CUSTOM, SUB, _activate, _row, abc, dns, emails  # noqa: F401  (fixtures)


def _later(**kw):
    return domains.now() + timedelta(**kw)


def test_daily_recheck_marks_dns_broken_and_keeps_serving(abc):
    _activate(abc)
    abc["client"].post(f"/admin/domains/{_row()['id']}/primary")
    abc["dns"].target = "elsewhere.example"
    domains.run_due(at=_later(hours=1))              # not a day yet
    assert _row()["status"] == "active"
    domains.run_due(at=_later(hours=25))
    row = _row()
    assert row["status"] == "dns_broken" and row["broken_since"] and "elsewhere.example" in row["last_check_message"]
    assert any("no longer points at us" in s for s in abc["emails"])
    tenancy.invalidate_cache()
    assert tenancy.resolve_host(CUSTOM).id == 2      # still served
    assert row["is_primary"] == 1


def test_still_broken_after_7_days_is_released_and_primary_falls_back(abc):
    _activate(abc)
    abc["client"].post(f"/admin/domains/{_row()['id']}/primary")
    abc["dns"].target = None
    domains.run_due(at=_later(hours=25))
    assert _row()["status"] == "dns_broken"
    domains.run_due(at=_later(days=5))
    assert _row()["status"] == "dns_broken"
    domains.run_due(at=_later(days=8, hours=2))
    row = _row()
    assert row["status"] == "failed" and row["is_primary"] == 0 and domains.is_released(row)
    assert _row(SUB)["is_primary"] == 1
    assert abc["system"].commands(f"sudo -n /usr/local/sbin/rdf-helper release-domain {CUSTOM}")
    assert any("stopped serving" in s for s in abc["emails"])
    tenancy.invalidate_cache()
    assert tenancy.resolve_host(CUSTOM) is None
    conn = get_db()
    assert domains.primary_host(conn, 2) == SUB
    conn.close()


def test_dns_coming_back_restores_it(abc):
    _activate(abc)
    abc["dns"].target = None
    domains.run_due(at=_later(hours=25))
    assert _row()["status"] == "dns_broken"
    abc["dns"].target = SUB
    domains.run_due(at=_later(hours=50))
    row = _row()
    assert row["status"] == "active" and row["broken_since"] is None
    assert any("works again" in s for s in abc["emails"])


def test_suspended_tenants_domains_are_left_as_they_are(abc):
    _activate(abc)
    conn = get_db()
    conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = 2")
    conn.commit()
    conn.close()
    abc["dns"].target = None
    domains.run_due(at=_later(days=20))
    assert _row()["status"] == "active"
    assert not abc["system"].commands("sudo -n /usr/local/sbin/rdf-helper release-domain")


def test_cancelled_tenants_domains_show_only_the_export_or_inactive_page(abc):
    _activate(abc)
    conn = get_db()
    conn.execute("UPDATE tenants SET status = 'cancelled' WHERE id = 2")
    conn.commit()
    conn.close()
    tenancy.invalidate_cache()
    from conftest import new_client
    c = new_client(CUSTOM)
    assert c.get("/devices", follow_redirects=False).status_code == 403
    assert c.get("/login").status_code == 200          # sign-in for the export page
