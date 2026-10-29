"""Which custom domains a tenant can add (app/domains.py, step 3)."""
from datetime import timedelta

import pytest

from app import domains, tenancy
from app.deps import get_db
from conftest import HOST, add_tenant


@pytest.fixture
def tenants():
    add_tenant(2, "abc", "abc.rds.example.com")
    add_tenant(3, "globex", "globex.rds.example.com")
    return tenancy.load_tenant(2), tenancy.load_tenant(3)


def _add(tenant, hostname):
    conn = get_db()
    try:
        return domains.add(conn, tenant, hostname, "admin@abc.example")
    finally:
        conn.close()


def _row(hostname):
    conn = get_db()
    row = conn.execute("SELECT * FROM tenant_domains WHERE hostname = ?", (hostname,)).fetchone()
    conn.close()
    return row


def test_a_valid_domain_is_added_pending(tenants):
    abc, _ = tenants
    _add(abc, " RDS.abc.example. ")
    row = _row("rds.abc.example")
    assert row["tenant_id"] == 2 and row["kind"] == "custom" and row["status"] == "verifying"
    assert row["is_primary"] == 0 and row["verified_at"] is None
    assert row["verify_token"].startswith("rdf-verify-") and len(row["verify_token"]) > 30


@pytest.mark.parametrize("bad", [
    "", "abc", "rds.abc_x.example", "*.abc.example", "198.51.100.10", "2001:db8::1", "localhost",
    "rds.localhost", "printer.local", "x." + "a" * 200 + ".example", "rds..abc.example", "-rds.abc.example",
])
def test_invalid_hostnames_are_refused(tenants, bad):
    abc, _ = tenants
    with pytest.raises(domains.DomainError):
        _add(abc, bad)


@pytest.mark.parametrize("ours", ["rds.example.com", "abc.rds.example.com", "x.abc.rds.example.com",
                                  "admin.rds.example.com"])
def test_provider_domain_and_anything_under_it_is_refused(tenants, ours):
    abc, _ = tenants
    with pytest.raises(domains.DomainError):
        _add(abc, ours)


def test_tenant1_legacy_host_is_refused(tenants, monkeypatch):
    abc, _ = tenants
    # Even when PROVIDER_DOMAIN isn't its parent: the hostname is taken.
    monkeypatch.setenv("PROVIDER_DOMAIN", "provider.example.net")
    with pytest.raises(domains.DomainError, match="already in use"):
        _add(abc, HOST)


def test_a_domain_can_only_be_added_once(tenants):
    abc, globex = tenants
    _add(abc, "rds.abc.example")
    with pytest.raises(domains.DomainError, match="already on your list"):
        _add(abc, "rds.abc.example")
    # Pending (not yet verified) claims count too.
    with pytest.raises(domains.DomainError, match="already in use"):
        _add(globex, "rds.abc.example")


def _age(hostname, days):
    conn = get_db()
    conn.execute("UPDATE tenant_domains SET created_at = ? WHERE hostname = ?",
                 (domains.fmt(domains.now() - timedelta(days=days)), hostname))
    conn.commit()
    conn.close()


def test_another_tenants_stale_pending_domain_is_released_after_7_days(tenants):
    abc, globex = tenants
    _add(globex, "rds.abc.example")
    _age("rds.abc.example", 6)
    with pytest.raises(domains.DomainError):
        _add(abc, "rds.abc.example")
    _age("rds.abc.example", 8)
    _add(abc, "rds.abc.example")
    assert _row("rds.abc.example")["tenant_id"] == 2


def test_a_verified_or_active_domain_is_never_released(tenants):
    abc, globex = tenants
    _add(globex, "rds.abc.example")
    conn = get_db()
    conn.execute("UPDATE tenant_domains SET status = 'active', verified_at = datetime('now') WHERE hostname = ?",
                 ("rds.abc.example",))
    conn.commit()
    conn.close()
    _age("rds.abc.example", 30)
    with pytest.raises(domains.DomainError, match="already in use"):
        _add(abc, "rds.abc.example")


def test_a_failed_domain_is_released(tenants):
    abc, globex = tenants
    _add(globex, "rds.abc.example")
    conn = get_db()
    conn.execute("UPDATE tenant_domains SET status = 'failed' WHERE hostname = ?", ("rds.abc.example",))
    conn.commit()
    conn.close()
    _add(abc, "rds.abc.example")
    assert _row("rds.abc.example")["tenant_id"] == 2


def test_max_custom_domains(tenants, monkeypatch):
    abc, _ = tenants
    for i in range(3):
        _add(abc, f"d{i}.abc.example")
    with pytest.raises(domains.DomainError, match="at most 3"):
        _add(abc, "d3.abc.example")
    monkeypatch.setenv("MAX_CUSTOM_DOMAINS", "4")
    _add(abc, "d3.abc.example")


@pytest.mark.parametrize("status", ["suspended", "cancelled"])
def test_only_active_and_trial_tenants(tenants, status):
    abc, _ = tenants
    conn = get_db()
    conn.execute("UPDATE tenants SET status = ? WHERE id = 2", (status,))
    conn.commit()
    conn.close()
    with pytest.raises(domains.DomainError):
        _add(tenancy.load_tenant(2), "rds.abc.example")


def test_a_pending_custom_domain_reaches_no_tenant(tenants):
    abc, _ = tenants
    _add(abc, "rds.abc.example")
    tenancy.invalidate_cache()
    assert tenancy.resolve_host("rds.abc.example") is None
    for status, served in (("active", True), ("dns_broken", True), ("failed", False), ("pending_dns", False)):
        conn = get_db()
        conn.execute("UPDATE tenant_domains SET status = ? WHERE hostname = 'rds.abc.example'", (status,))
        conn.commit()
        conn.close()
        tenancy.invalidate_cache()
        t = tenancy.resolve_host("rds.abc.example")
        assert (t is not None and t.id == 2) == served, status


def test_manage_domains_is_admin_only_and_withheld_from_support():
    from app import permissions, support
    assert "manage_domains" in permissions.CAP_KEYS
    assert "manage_domains" not in permissions.SYSTEM_ROLES["tech"]["perms"]
    assert "manage_domains" in support.SUPPORT_WITHHELD_PERMS
