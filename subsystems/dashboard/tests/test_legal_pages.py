"""The provider's Terms and Privacy pages: <data dir>/legal/*.html on the
public site, and the default TERMS_URL / PRIVACY_URL that point at them."""
import pytest

from app import settings
from app.paths import LEGAL_DIR
from conftest import HOST, new_client

PUBLIC = "www.rds.example.com"


@pytest.fixture
def legal_files(monkeypatch):
    monkeypatch.delenv("TERMS_URL", raising=False)
    monkeypatch.delenv("PRIVACY_URL", raising=False)
    LEGAL_DIR.mkdir(parents=True, exist_ok=True)
    (LEGAL_DIR / "terms.html").write_text("<h2>1. The service</h2><p>Example Co LLC provides it.</p>")
    (LEGAL_DIR / "privacy.html").write_text("<h2>What we collect</h2><p>Very little.</p>")
    yield
    for name in ("terms.html", "privacy.html"):
        (LEGAL_DIR / name).unlink(missing_ok=True)


def test_pages_404_until_written(monkeypatch):
    monkeypatch.delenv("TERMS_URL", raising=False)
    c = new_client(PUBLIC)
    assert c.get("/terms").status_code == 404 and c.get("/privacy").status_code == 404
    assert settings.terms_url() == ""


def test_pages_show_the_providers_text(legal_files):
    c = new_client(PUBLIC)
    r = c.get("/terms")
    assert r.status_code == 200 and "Terms of Service" in r.text and "Example Co LLC provides it." in r.text
    assert "Last updated" in r.text
    r = c.get("/privacy")
    assert r.status_code == 200 and "Privacy Policy" in r.text and "Very little." in r.text
    # Only on the public site.
    assert "Example Co LLC" not in new_client(HOST).get("/terms").text


def test_urls_default_to_the_pages_and_reach_the_signup_form(legal_files, monkeypatch):
    assert settings.terms_url() == f"https://{PUBLIC}/terms"
    assert settings.privacy_url() == f"https://{PUBLIC}/privacy"
    page = new_client(PUBLIC).get("/").text
    assert f'href="https://{PUBLIC}/terms"' in page and f'href="https://{PUBLIC}/privacy"' in page
    monkeypatch.setenv("TERMS_URL", "https://legal.example.com/terms")
    assert settings.terms_url() == "https://legal.example.com/terms"
