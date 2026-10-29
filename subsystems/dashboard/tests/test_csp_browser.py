"""Every page works with the Content-Security-Policy enforced (Playwright,
Chromium). Dev-only: skipped when Playwright or its browser isn't installed
(python3 -m pip install -r requirements-dev.txt; python3 -m playwright
install chromium).

Starts the app over HTTPS (self-signed) on a free port, with Chromium
resolving *.example.com to it, signs in as a tenant admin (the real form)
and as a provider admin, visits every navigation page, every route without
path parameters and one example of each parameterised page, opens the main
dialogs, and fails on any securitypolicyviolation, page error or CSP report.
"""
import logging
import re
import shutil
import socket
import subprocess
import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from conftest import (CONSOLE_HOST, DATA_DIR, HOST, PASSWORD, SESSION_SECRET, all_routes, forge_session,  # noqa: E402
                      provider_client)
from test_isolation import CATALOGUE_PARAMS, tenant_param  # noqa: E402

CAPTURE = """
window.__rdfViolations = [];
document.addEventListener('securitypolicyviolation', e =>
  window.__rdfViolations.push(e.violatedDirective + ' ' + e.blockedURI + ' ' + (e.sourceFile || '') + ':' + e.lineNumber));
"""

# Pages that legitimately aren't plain HTML pages or need a fresh token.
SKIP_PATHS = {"/logout", "/support/enter", "/setup", "/d/{token}/{platform}", "/download/{filename}",
              "/guides/{filename}", "/groups/{slug}/branding/icon.png", "/admin/emails/logo.png"}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(two_tenants, monkeypatch, tmp_path):
    import uvicorn
    from app.main import app
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    key, crt = tmp_path / "key.pem", tmp_path / "crt.pem"
    subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1", "-subj", "/CN=example.com",
                    "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    monkeypatch.setenv("CSP_MODE", "enforce")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, ssl_keyfile=str(key),
                                        ssl_certfile=str(crt), log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield port, two_tenants
    srv.should_exit = True
    t.join(5)


@pytest.fixture
def browser(server):
    try:
        pw = playwright_api.sync_playwright().start()
        b = pw.chromium.launch(args=["--host-resolver-rules=MAP *.example.com 127.0.0.1"])
    except Exception as e:      # browser not downloaded
        pytest.skip(f"Chromium for Playwright isn't available: {e}")
    yield b
    b.close()
    pw.stop()


class Watch:
    def __init__(self, context):
        self.problems = []
        context.add_init_script(CAPTURE)

    def attach(self, page):
        page.on("pageerror", lambda e: self.problems.append(f"{page.url}: JS error {e}"))
        page.on("console", lambda m: m.type == "error" and "Content Security Policy" in m.text
                and self.problems.append(f"{page.url}: {m.text}"))

    def check(self, page, what):
        try:
            v = page.evaluate("window.__rdfViolations || []")
        except Exception:
            v = []
        self.problems += [f"{what}: {x}" for x in v]


def _visit(page, watch, url):
    page.goto(url, wait_until="networkidle")
    watch.check(page, url)


def _fill(path, values):
    for name in re.findall(r"{(\w+)}", path):
        path = path.replace("{" + name + "}", str(values(name)))
    return path


def test_every_page_and_dialog_runs_under_the_enforced_policy(server, browser, caplog):
    port, tt = server
    a = tt["a"]
    base = f"https://{HOST}:{port}"
    ctx = browser.new_context(ignore_https_errors=True)
    watch = Watch(ctx)
    page = ctx.new_page()
    watch.attach(page)

    # Tenant admin: the real sign-in form.
    page.goto(base + "/login")
    page.fill("input[name=email]", a["admin"]["email"])
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(base + "/")

    with caplog.at_level(logging.WARNING, logger="rdf.security"):
        visited = []
        for method, path in all_routes():
            if method != "GET" or path in SKIP_PATHS or path.startswith(("/api/", "/export/", "/admin/export/")) \
                    or path.endswith((".csv", ".json", ".png", ".ico")):
                continue
            url = _fill(path, lambda n: CATALOGUE_PARAMS.get((path, n)) or a[tenant_param(path, n)[0]])
            _visit(page, watch, base + url)
            visited.append(url)

        # Dialogs.
        _visit(page, watch, base + "/groups")
        page.click("button[data-show=create-modal]")
        assert page.is_visible("#create-modal")
        page.click("#create-modal button[data-hide=create-modal]")
        assert not page.is_visible("#create-modal")
        page.click("button[data-onclick=toggleColumnMenu]")
        assert page.is_visible("#column-menu")
        page.evaluate("document.body.click()")   # an outside click closes it
        assert not page.is_visible("#column-menu")
        _visit(page, watch, base + f"/groups/{a['group_slug']}")
        page.click("button[data-show=edit-group-modal]")
        assert page.is_visible("#edit-group-modal")
        _visit(page, watch, base + "/devices")
        page.click("button[data-onclick=toggleColumnMenu]")
        assert page.is_visible("#column-menu")
        page.evaluate("document.body.click()")   # an outside click closes it
        assert not page.is_visible("#column-menu")
        page.locator("button[data-onclick=openDetailModal]").first.click()
        assert page.is_visible("#detail-modal")
        page.locator("#detail-modal button[data-onclick=closeDetailModal]").first.click()
        page.locator("button[data-onclick=openDeviceModal]").first.click()
        assert page.is_visible("#device-modal")
        _visit(page, watch, base + "/users")
        page.locator("button[data-show^=edit-]").first.click()
        watch.check(page, "dialogs")

        # Provider admin: the console.
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
        for ctx_route in iter_route_contexts(console_app.routes):
            if not isinstance(ctx_route.route, APIRoute) or "GET" not in (ctx_route.methods or ()):
                continue
            path = ctx_route.path
            if path in ("/logout", "/enroll/{token}") or path.endswith((".json", ".csv")) or "download" in path:
                continue
            _visit(page, watch, console + _fill(path, {"tenant_id": 2, "job_id": 1}.get))
            visited.append("console " + path)

    reports = [r.getMessage() for r in caplog.records if "CSP violation" in r.getMessage()]
    assert len(visited) > 40, visited
    assert not watch.problems and not reports, watch.problems + reports
