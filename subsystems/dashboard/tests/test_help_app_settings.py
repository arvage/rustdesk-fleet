"""The Help page gives technicians their tenant's server settings, the same
values its installers use (generate_installer.server_settings)."""
from conftest import HOST, HOST_B, signed_in_at
from app.deps import get_db


def _values(html: str) -> list[str]:
    import re
    sec = html[html.index('id="app"'):html.index('id="s2"')]
    return re.findall(r'data-v="([^"]*)"', sec)


def test_each_tenant_sees_its_own_server_settings(two_tenants):
    b = _values(signed_in_at(two_tenants["b"]["tech"], HOST_B).get("/help").text)
    assert b == ["relay-globex.example.com:21125", "relay-globex.example.com:21127",
                 f"https://{HOST_B}", "PUBKEY2"]
    a = _values(signed_in_at(two_tenants["a"]["tech"], HOST).get("/help").text)
    # Tenant #1 is on RustDesk's default ports, so like its installers it leaves them out.
    assert a[0] == HOST and a[1] == HOST and a[3] == "PUBKEY1"
    assert "PUBKEY2" not in signed_in_at(two_tenants["a"]["tech"], HOST).get("/help").text


def test_no_key_yet_shows_a_note_instead(two_tenants):
    conn = get_db()
    conn.execute("UPDATE relays SET pubkey = NULL WHERE tenant_id = 2")
    conn.commit()
    conn.close()
    html = signed_in_at(two_tenants["b"]["tech"], HOST_B).get("/help").text
    assert _values(html) == [] and "aren't available yet" in html
