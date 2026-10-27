"""Publishing a web domain through the helper (app/console/web.py)."""
import pytest

from app.console import web


def helper_calls(fake):
    return [" ".join(c[3:]) for c in fake.calls if c[:3] == ["sudo", "-n", "/usr/local/sbin/rdf-helper"]]


def test_publish_issues_writes_and_reloads(fake_system):
    web.publish("acme.rds.example.com")
    assert helper_calls(fake_system) == ["issue-cert acme.rds.example.com", "write-vhost acme.rds.example.com", "reload-nginx"]


def test_a_failed_reload_takes_the_vhost_back_out(fake_system):
    fake_system.fail["reload-nginx"] = 'nginx refused the new configuration: [emerg] duplicate zone'
    with pytest.raises(web.WebError, match="emerg"):
        web.publish("acme.rds.example.com")
    assert helper_calls(fake_system) == ["issue-cert acme.rds.example.com", "write-vhost acme.rds.example.com",
                                         "reload-nginx", "remove-vhost acme.rds.example.com", "reload-nginx"]


def test_unpublish(fake_system):
    web.unpublish("acme.rds.example.com")
    assert helper_calls(fake_system) == ["remove-vhost acme.rds.example.com", "reload-nginx",
                                         "revoke-cert acme.rds.example.com"]
