"""Every page and JSON endpoint without a path parameter loads for a signed-in
admin on either tenant: no 500s from a query that lost its tenant."""
from conftest import HOST, HOST_B, all_routes, signed_in_at

# Endpoints that start long or external work rather than render something.
_SKIP = {"/logout"}


def get_routes():
    paths = sorted({p for m, p in all_routes() if m == "GET" and "{" not in p and p not in _SKIP})
    assert len(paths) > 20, paths
    return paths


def test_every_page_renders_for_both_tenants(two_tenants):
    for tag, host in (("a", HOST), ("b", HOST_B)):
        c = signed_in_at(two_tenants[tag]["admin"], host)
        other = two_tenants["b" if tag == "a" else "a"]["secret"]
        for path in get_routes():
            r = c.get(path, follow_redirects=False)
            assert r.status_code < 500, (tag, path, r.text[:500])
            assert other not in r.text, (tag, path)
