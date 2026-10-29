"""Content-Security-Policy (app/security.py): a fresh nonce per response,
report-only by default, the /csp-report endpoint, and templates that need
no inline handlers."""
import json
import logging
import re
from pathlib import Path

import pytest

from conftest import CONSOLE_HOST, HOST, create_user, new_client, provider_client, signed_in
from app import security

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
REPORT_ONLY = "content-security-policy-report-only"


def _policy(r):
    return r.headers.get("content-security-policy") or r.headers.get(REPORT_ONLY)


def test_html_gets_a_report_only_policy_with_a_fresh_nonce():
    c = signed_in(create_user("admin@example.com", "admin"))
    nonces = []
    for _ in range(2):
        r = c.get("/devices")
        assert "content-security-policy" not in r.headers and REPORT_ONLY in r.headers
        policy = r.headers[REPORT_ONLY]
        nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
        assert set(re.findall(r'<script[^>]*nonce="([^"]+)"', r.text)) == {nonce}
        nonces.append(nonce)
        for part in ("default-src 'self'", "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'",
                     "form-action 'self'", "img-src 'self' data:", "report-uri /csp-report"):
            assert part in policy, part
    assert nonces[0] != nonces[1]


def test_enforce_mode(monkeypatch):
    monkeypatch.setenv("CSP_MODE", "enforce")
    r = new_client(HOST).get("/login")
    assert "'nonce-" in r.headers["content-security-policy"] and REPORT_ONLY not in r.headers
    assert "'nonce-" in provider_client().get("/").headers["content-security-policy"]


def test_console_pages_and_plain_pages_get_it_too():
    assert "'nonce-" in _policy(provider_client().get("/health"))
    assert "'nonce-" in _policy(new_client(CONSOLE_HOST).get("/login"))
    assert _policy(new_client("nowhere.example.org").get("/"))          # the plain 404 page


def test_json_gets_no_policy():
    c = signed_in(create_user("admin@example.com", "admin"))
    r = c.get("/api/devices/status")
    assert r.headers["content-type"].startswith("application/json") and not _policy(r)


def _report(client, body, **kw):
    client.headers.pop("Sec-Fetch-Site", None)
    return client.post("/csp-report", content=body, headers={"Content-Type": "application/csp-report"}, **kw)


def test_reports_are_logged(caplog):
    report = {"csp-report": {"document-uri": "https://rds.example.com/devices", "violated-directive": "script-src-elem",
                             "blocked-uri": "inline"}}
    with caplog.at_level(logging.WARNING, logger="rdf.security"):
        r = _report(new_client(HOST), json.dumps(report))
    assert r.status_code == 204
    assert "CSP violation" in caplog.text and "script-src-elem" in caplog.text
    assert _report(new_client(CONSOLE_HOST), "{}").status_code == 204


def test_reports_are_size_capped_and_rate_limited():
    c = new_client(HOST)
    assert _report(c, "x" * (security.CSP_REPORT_MAX_BYTES + 1)).status_code == 413
    codes = [_report(c, "{}").status_code for _ in range(security.CSP_REPORTS_PER_MINUTE + 3)]
    assert codes[:security.CSP_REPORTS_PER_MINUTE] == [204] * security.CSP_REPORTS_PER_MINUTE
    assert codes[-1] == 429


# ── Templates: nothing the policy would block ────────────────────────────────

INLINE_HANDLER = re.compile(r"(?<![-\w])on[a-z]+\s*=\s*[\"']", re.I)


@pytest.mark.parametrize("path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: p.name)
def test_templates_have_no_inline_handlers_and_every_script_has_the_nonce(path):
    text = path.read_text()
    assert not INLINE_HANDLER.findall(text), "inline event handler: use data-onclick etc. (static/app.js)"
    assert "javascript:" not in text
    for tag in re.findall(r"<script\b[^>]*>", text):
        assert 'nonce="{{ request.state.csp_nonce }}"' in tag or 'src="/static/' in tag, tag
    assert "fonts.googleapis" not in text and "cdn." not in text


def test_static_js_has_no_inline_handlers():
    for js in STATIC.glob("*.js"):
        code = "\n".join(line for line in js.read_text().splitlines() if not line.lstrip().startswith("//"))
        assert not INLINE_HANDLER.findall(code), js.name
