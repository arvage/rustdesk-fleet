"""Installers are built from the tenant's relays row (step 8 of phase 1b).
Tenant #1's must stay exactly what they were: fixtures/installers_tenant1
was rendered by the code before this change."""
from pathlib import Path

from conftest import add_tenant, get_db
from installer_render import PLATFORMS, render_tenant_installers

FIXTURES = Path(__file__).parent / "fixtures" / "installers_tenant1"


def _group(tenant_id: int, slug: str) -> None:
    conn = get_db()
    conn.execute("INSERT INTO client_groups (tenant_id, slug, display_name, unattended_password, auto_update,"
                 " remote_config, brand_app_name) VALUES (?, ?, 'Acme Corp', 'Secret pw 1', 'N', 'Y', NULL)",
                 (tenant_id, slug))
    conn.commit()
    conn.close()


def test_tenant_one_installers_are_byte_for_byte_unchanged(tmp_path):
    _group(1, "acme")
    out = render_tenant_installers(1, "acme", tmp_path)
    for platform in PLATFORMS:
        assert out[platform] == (FIXTURES / platform).read_text(), platform


def test_new_tenants_get_their_relay_ports_key_and_web_domain(tmp_path):
    add_tenant(2, "globex", "globex.rds.example.com")
    _group(2, "acme")
    out = render_tenant_installers(2, "acme", tmp_path)
    nsis, linux = out["windows-x64"], out["linux"]
    for line in ('rendezvous_server = "relay-globex.example.com:21125"',
                 'custom-rendezvous-server = "relay-globex.example.com:21125"',
                 'relay-server = "relay-globex.example.com:21127"',
                 'api-server = "https://globex.rds.example.com"',
                 'key = "PUBKEY2"'):
        assert line in nsis, line
    assert '--option custom-rendezvous-server "relay-globex.example.com:21125"' in nsis
    assert '--option relay-server "relay-globex.example.com:21127"' in nsis
    assert '--option api-server "https://globex.rds.example.com"' in nsis
    assert 'HOST="relay-globex.example.com:21125"' in linux and 'KEY="PUBKEY2"' in linux
    assert """printf 'relay-server = "%s"\\n' "relay-globex.example.com:21127\"""" in linux
    assert """printf 'api-server = "https://%s"\\n' "globex.rds.example.com\"""" in linux
    assert "rds.example.com\"" not in nsis.replace("globex.rds.example.com", "")
    assert "PUBKEY1" not in nsis + linux
