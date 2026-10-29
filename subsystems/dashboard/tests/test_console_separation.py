"""The console host and tenant hosts never share a route: walk both apps'
routers. Every console route, called on a tenant host, is answered by the
tenant app (a 404, or the tenant's own page where the path is the same,
like /login); every tenant route, called on the console host, is answered
by the console (a 404 unless the console has the same path)."""
from fastapi.routing import APIRoute, iter_route_contexts

from conftest import CONSOLE_HOST, HOST, HOST_B, all_routes, new_client, provider_client, signed_in_at
from app.console.main import console_app


def console_routes():
    out = []
    for ctx in iter_route_contexts(console_app.routes):
        if isinstance(ctx.route, APIRoute):
            out += [(m, ctx.path) for m in sorted(ctx.methods or ())]
    return out


def concrete(path: str) -> str:
    import re
    return re.sub(r"{[^}]+}", "1", path)


def test_there_are_console_routes_to_walk():
    assert len(console_routes()) > 30


def test_no_console_route_answers_on_a_tenant_host(two_tenants):
    tenant_paths = {p for _, p in all_routes()}
    for host, user in ((HOST, two_tenants["a"]["admin"]), (HOST_B, two_tenants["b"]["admin"])):
        for client in (new_client(host), signed_in_at(user, host)):
            # Even with a provider cookie in the jar, nothing on a tenant host is the console.
            client.cookies.set("__Host-rdf_provider_session", provider_client().cookies.get("__Host-rdf_provider_session"))
            for method, path in console_routes():
                r = client.request(method, concrete(path), follow_redirects=False)
                assert r.headers.get("X-Fleet-App") != "console", (host, method, path)
                if path not in tenant_paths:
                    assert r.status_code in (404, 405), (host, method, path, r.status_code)


def test_no_tenant_route_answers_on_the_console_host(two_tenants):
    console_paths = {p for _, p in console_routes()}
    for client in (new_client(CONSOLE_HOST), provider_client()):
        client.cookies.set("__Host-rdf_session", signed_in_at(two_tenants["a"]["admin"], HOST).cookies.get("__Host-rdf_session"))
        for method, path in all_routes():
            r = client.request(method, concrete(path), follow_redirects=False)
            assert r.headers.get("X-Fleet-App") == "console", (method, path)
            if path not in console_paths:
                assert r.status_code in (404, 405), (method, path, r.status_code)
