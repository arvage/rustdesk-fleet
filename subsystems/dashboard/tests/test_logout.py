"""Signing out is a POST; GET /logout only redirects."""
from conftest import get_status, signed_in


def test_post_signs_out(admin):
    c = signed_in(admin)
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert get_status(c, "/users") == (303, "/login")


def test_get_does_not_sign_out(admin):
    c = signed_in(admin)
    assert get_status(c, "/logout") == (303, "/")
    assert get_status(c, "/users")[0] == 200


def test_sidebar_uses_a_form(admin):
    r = signed_in(admin).get("/account")
    assert '<form method="post" action="/logout"' in r.text
    assert 'href="/logout"' not in r.text
