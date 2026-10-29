"""Session cookies are host-only (__Host- prefix): Path=/, Secure, no
Domain. The console's is SameSite=Strict; an old-name cookie is ignored."""
from http.cookies import SimpleCookie

from conftest import CONSOLE_HOST, HOST, create_user, forge_session, login, new_client, SESSION_SECRET


def _set_cookie(response, name):
    for raw in response.headers.get_list("set-cookie"):
        c = SimpleCookie()
        c.load(raw)
        if name in c:
            return c[name], raw
    raise AssertionError(f"no {name} cookie set: {response.headers.get_list('set-cookie')}")


def test_tenant_cookie_is_host_only_and_lax():
    user = create_user("admin@example.com", "admin")
    r = login(new_client(HOST), user)
    morsel, raw = _set_cookie(r, "__Host-rdf_session")
    assert morsel["path"] == "/" and morsel["secure"] and not morsel["domain"]
    assert "samesite=lax" in raw.lower() and "httponly" in raw.lower()


def test_console_cookie_is_host_only_and_strict():
    c = new_client(CONSOLE_HOST)
    # The console sets its cookie once the session is used (sign-in begins a
    # passkey challenge); the login-begin call stores one.
    r = c.post("/login/webauthn/begin", json={})
    morsel, raw = _set_cookie(r, "__Host-rdf_provider_session")
    assert morsel["path"] == "/" and morsel["secure"] and not morsel["domain"]
    assert "samesite=strict" in raw.lower()


def test_an_old_name_cookie_is_ignored():
    user = create_user("admin@example.com", "admin")
    good = new_client(HOST)
    login(good, user)
    value = good.cookies.get("__Host-rdf_session")
    old = new_client(HOST)
    old.cookies.set("rdf_session", value)
    assert old.get("/devices", follow_redirects=False).headers["location"] == "/login"
    provider = new_client(CONSOLE_HOST)
    provider.cookies.set("rdf_provider_session",
                         forge_session({"provider_user_id": 1, "provider_sv": 1}, "provider-console:" + SESSION_SECRET))
    assert provider.get("/", follow_redirects=False).headers["location"] == "/login"
