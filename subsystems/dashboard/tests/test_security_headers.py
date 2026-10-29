"""Security headers on every dashboard and console response (app/security.py)."""
import pytest

from conftest import CONSOLE_HOST, HOST, create_user, new_client, provider_client, signed_in
from app.security import SECURITY_HEADERS


def _check(r):
    for name, value in SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, (r.url, name, r.headers.get(name))


@pytest.mark.parametrize("path", ["/login", "/", "/devices", "/no-such-page"])
def test_tenant_responses_carry_the_headers(path):
    _check(signed_in(create_user("admin@example.com", "admin")).get(path, follow_redirects=False))


@pytest.mark.parametrize("path", ["/login", "/", "/health", "/help"])
def test_console_responses_carry_the_headers(path):
    _check(provider_client().get(path, follow_redirects=False))
    _check(new_client(CONSOLE_HOST).get(path, follow_redirects=False))


def test_unknown_hosts_and_refusals_carry_them_too():
    _check(new_client("nowhere.example.org").get("/"))
    c = new_client(HOST)
    c.headers.pop("Sec-Fetch-Site")
    _check(c.post("/login"))


def test_public_pages_keep_their_own_referrer_policy(two_tenants):
    r = new_client(HOST).get("/d/" + two_tenants["a"]["token"])
    assert r.status_code == 200
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-robots-tag"] == "noindex, nofollow"
    assert r.headers["x-frame-options"] == "DENY" and r.headers["strict-transport-security"]
    dl = new_client(HOST).get("/d/" + two_tenants["a"]["token"] + "/linux")
    assert dl.status_code == 200 and dl.content.startswith(b"#!/bin/sh")
    assert dl.headers["x-content-type-options"] == "nosniff"


def test_the_device_api_gets_exactly_what_it_got_before():
    r = new_client(HOST).post("/api/heartbeat", json={})
    for name in SECURITY_HEADERS:
        assert name.lower() not in {k.lower() for k in r.headers}, name
