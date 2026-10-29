"""The public host's pages (product page with pricing, sign-up, sign-in)
under the enforced CSP and at phone width (Playwright; skipped like the
other browser tests when it isn't installed)."""
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

import fake_stripe  # noqa: E402
from test_csp_browser import Watch, browser, server  # noqa: E402,F401
from test_mobile_layout import OVERFLOW  # noqa: E402

PUBLIC = "www.rds.example.com"


def test_public_pages(server, browser, monkeypatch):
    port, _ = server
    fake = fake_stripe.install(monkeypatch)
    from app import plans
    from app.deps import get_db
    conn = get_db()
    plans.save(conn, None, {"key": "standard", "name": "Standard", "is_public": "1", "sort_order": "1",
                            "price_flat": fake.add_price(4900), "price_technician": fake.add_price(500)})
    conn.close()
    base = f"https://{PUBLIC}:{port}"
    for width in (1280, 390):
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 900})
        watch = Watch(ctx)
        page = ctx.new_page()
        watch.attach(page)
        for path in ("/", "/signup", "/signin"):
            page.goto(base + path, wait_until="networkidle")
            watch.check(page, path)
            if width == 390:
                assert page.evaluate(OVERFLOW, 390) == [], path
        page.goto(base + "/signup", wait_until="networkidle")
        page.fill("#company", "Acme IT")
        for _ in range(50):                          # the page's CSP forbids eval, so poll from here
            if page.input_value("#slug") == "acme-it" and "acme-it.rds.example.com" in page.inner_text("#slug-hint"):
                break
            page.wait_for_timeout(100)
        assert page.input_value("#slug") == "acme-it"
        assert "acme-it.rds.example.com" in page.inner_text("#slug-hint")
        watch.check(page, "signup js")
        assert not watch.problems, watch.problems
        ctx.close()
