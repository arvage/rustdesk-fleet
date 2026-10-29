"""Every page fits a phone screen (Playwright, Chromium; dev-only, skipped
like test_csp_browser.py when they aren't installed).

Signs in as a tenant admin and as a provider admin with a 390px-wide mobile
viewport, visits the same pages as the CSP browser test and opens the main
dialogs, and fails when anything sticks out past the right edge of the
screen without being inside a horizontally scrolling box. Wide tables don't
count: on phones they become one card per row (app.js, style.css).
"""
from conftest import CONSOLE_HOST, HOST, PASSWORD, SESSION_SECRET, all_routes, forge_session, provider_client
from test_csp_browser import SKIP_PATHS, _fill, browser, server  # noqa: F401  (fixtures)
from test_isolation import CATALOGUE_PARAMS, tenant_param

WIDTH = 390

# Outermost elements whose right edge is past the screen, skipping the
# off-canvas sidebar, fixed overlays and anything inside a scroll box.
OVERFLOW = """W => {
  const scrolls = el => { for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll') return true; } return false; };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || r.right <= W + 1 || el.closest('.sidebar') || scrolls(el)) continue;
    if (getComputedStyle(el).position === 'fixed') continue;
    const p = el.parentElement, pr = p && p.getBoundingClientRect();
    if (pr && pr.right > W + 1 && !scrolls(p)) continue;
    out.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + ' ' + Math.round(r.right) + 'px "'
             + (el.textContent || '').trim().slice(0, 40).replace(/\\s+/g, ' ') + '"');
  }
  return out;
}"""


def test_every_page_fits_a_phone_screen(server, browser):
    port, tt = server
    a = tt["a"]
    base = f"https://{HOST}:{port}"
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": WIDTH, "height": 844},
                              is_mobile=True, has_touch=True)
    page = ctx.new_page()
    problems, seen = [], []

    def check(what):
        seen.append(what)
        problems.extend(f"{what}: {x}" for x in page.evaluate(OVERFLOW, WIDTH))

    def visit(url, what=None):
        page.goto(url, wait_until="networkidle")
        if "html" in (page.evaluate("document.contentType") or ""):
            check(what or url)

    visit(base + "/login")
    page.fill("input[name=email]", a["admin"]["email"])
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(base + "/")

    for method, path in all_routes():
        if method != "GET" or path in SKIP_PATHS or path.startswith(("/api/", "/export/", "/admin/export/")) \
                or path.endswith((".csv", ".json", ".png", ".ico")):
            continue
        visit(base + _fill(path, lambda n: CATALOGUE_PARAMS.get((path, n)) or a[tenant_param(path, n)[0]]))

    for url, opener in [("/groups", "button[data-show=create-modal]"),
                        (f"/groups/{a['group_slug']}", "button[data-show=edit-group-modal]"),
                        ("/devices", "button[data-onclick=openDetailModal]"),
                        ("/devices", "button[data-onclick=openDeviceModal]"),
                        ("/users", "button[data-show^=edit-]")]:
        page.goto(base + url, wait_until="networkidle")
        page.locator(opener).first.click()
        page.wait_for_load_state("networkidle")
        check(f"{url} dialog {opener}")

    # The menu button opens the sidebar.
    page.goto(base + "/", wait_until="networkidle")
    assert not page.locator("#sidebar").is_visible() or page.locator("#sidebar").bounding_box()["x"] < 0
    page.click("button.sidebar-toggle")
    page.wait_for_timeout(400)
    assert page.locator("#sidebar").bounding_box()["x"] >= 0

    console = f"https://{CONSOLE_HOST}:{port}"
    provider_client()   # creates ops@example.com
    from app.deps import get_db
    conn = get_db()
    pid = conn.execute("SELECT id FROM provider_users WHERE email = 'ops@example.com'").fetchone()[0]
    conn.close()
    ctx.add_cookies([{"name": "__Host-rdf_provider_session", "value":
                      forge_session({"provider_user_id": pid, "provider_sv": 1}, "provider-console:" + SESSION_SECRET),
                      "url": console + "/", "secure": True}])
    from fastapi.routing import APIRoute, iter_route_contexts
    from app.console.main import console_app
    for c in iter_route_contexts(console_app.routes):
        if not isinstance(c.route, APIRoute) or "GET" not in (c.methods or ()):
            continue
        path = c.path
        if path in ("/logout", "/enroll/{token}") or path.endswith((".json", ".csv")) or "download" in path:
            continue
        visit(console + _fill(path, {"tenant_id": 2, "job_id": 1}.get), "console " + path)

    assert len(seen) > 40, seen
    assert not problems, problems
