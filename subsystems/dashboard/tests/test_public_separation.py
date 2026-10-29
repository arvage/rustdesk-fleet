"""The public host is a provider host like the console (step 10): every one
of its routes 404s on tenant hosts and the console, and every tenant and
console route 404s on it."""
import re

from fastapi.routing import APIRoute, iter_route_contexts

from conftest import CONSOLE_HOST, HOST, all_routes, new_client, provider_client
from app.console.main import console_app
from app.public_site import public_app

PUBLIC = "www.rds.example.com"


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x1", path)


def _routes(app):
    out = []
    for ctx in iter_route_contexts(app.routes):
        if isinstance(ctx.route, APIRoute):
            for m in sorted(ctx.methods or ()):
                out.append((m, ctx.path))
    return out


def test_public_routes_answer_nowhere_else(two_tenants):
    routes = _routes(public_app)
    assert len(routes) >= 9, routes
    tenant = new_client(HOST)
    for method, path in routes:
        r = tenant.request(method, _fill(path), follow_redirects=False)
        # On a tenant host it's either unknown (404) or a tenant route that
        # happens to share the path ("/", "/static"); never the public app.
        assert r.status_code in (303, 404, 405), (method, path, r.status_code)
        assert "Start your" not in r.text and "Check your email" not in r.text, path
        r = provider_client().request(method, _fill(path), follow_redirects=False)
        # The console has its own "/" (the tenant list); nothing else is there.
        assert r.status_code in ((200,) if path == "/" else ()) + (303, 404, 405), (method, path, r.status_code)
        assert "Start your" not in r.text and "Hosted plans" not in r.text, path


def test_tenant_and_console_routes_404_on_the_public_host():
    c = new_client(PUBLIC)
    public_paths = {p for _, p in _routes(public_app)}
    checked = 0
    for method, path in all_routes() + _routes(console_app):
        if path in public_paths or path.startswith("/static"):
            continue
        r = c.request(method, _fill(path), follow_redirects=False)
        assert r.status_code in (404, 405), (method, path, r.status_code)
        checked += 1
    assert checked > 100


def test_unknown_hosts_still_404():
    assert new_client("nowhere.example.org").get("/signup").status_code == 404
