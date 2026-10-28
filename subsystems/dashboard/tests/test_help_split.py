"""Provider topics live on the console's Help page, not the tenants' one."""
from conftest import admin, new_client, provider_client, signed_in

PROVIDER_ONLY = ("Backup &amp; Restore", "Azure Trusted Signing", "Restart relay", "memory or disk high",
                 "Set up off-site backups", "Server health alerts")


def test_tenant_help_has_no_provider_topics(admin):
    html = signed_in(admin).get("/help").text
    for phrase in PROVIDER_ONLY:
        assert phrase not in html, phrase
    assert "run by your provider" in html


def test_console_help_covers_the_provider_topics():
    r = provider_client().get("/help")
    assert r.status_code == 200
    for phrase in ("14-day trial", "Azure Trusted Signing", "nightly at 03:30 UTC", "passkeys only",
                   "Upgrade all relays", 'href="/help"'):
        assert phrase in r.text, phrase


def test_console_help_needs_a_provider_admin():
    from conftest import CONSOLE_HOST
    r = new_client(CONSOLE_HOST).get("/help", follow_redirects=False)
    assert r.status_code in (302, 303, 401) and "14-day trial" not in r.text
