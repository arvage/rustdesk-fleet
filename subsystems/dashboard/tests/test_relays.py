"""Everything that used to assume one relay reads the tenant's relays row:
installer builds, relay status and backups (provider-level, every relay)."""
import tarfile

from conftest import DATA_DIR, HOST, HOST_B, db_rows, signed_in_at


def build(tenant_id, slug):
    from generate_installer import build_installer
    return build_installer(tenant_id, slug, "linux", "tester@example.com")


def test_installers_use_the_tenants_own_relay(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    ia, ib = build(1, a["group_slug"]), build(2, b["group_slug"])
    text_a = open(ia["unsigned_path"]).read()
    text_b = open(ib["unsigned_path"]).read()
    assert HOST in text_a and "PUBKEY1" in text_a
    assert "relay-globex.example.com" in text_b and "PUBKEY2" in text_b
    assert "PUBKEY1" not in text_b and HOST not in text_b.replace("relay-globex.example.com", "")
    # Each tenant's files stay in its own directory.
    assert ia["unsigned_path"].startswith(str(DATA_DIR / "installers"))
    assert ib["unsigned_path"].startswith(str(DATA_DIR / "tenants" / "2" / "installers"))
    assert [r["tenant_id"] for r in db_rows("SELECT tenant_id FROM installers WHERE id = ?", ib["id"])] == [2]


def test_a_group_slug_of_another_tenant_cant_be_built(two_tenants):
    import pytest
    from generate_installer import InstallerError
    with pytest.raises(InstallerError):
        build(2, two_tenants["a"]["group_slug"])


def test_status_page_shows_the_tenants_relay(two_tenants):
    page_b = signed_in_at(two_tenants["b"]["admin"], HOST_B).get("/").text
    assert "relay-globex.example.com" in page_b and "PUBKEY2" in page_b
    assert "PUBKEY1" not in page_b
    page_a = signed_in_at(two_tenants["a"]["admin"], HOST).get("/").text
    assert "PUBKEY1" in page_a and "relay-globex.example.com" not in page_a


def _keypair(data_dir, tag):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "id_ed25519").write_text(f"secret-{tag}")
    (data_dir / "id_ed25519.pub").write_text(f"pub-{tag}")


def test_backup_covers_every_relay_and_restores_them(two_tenants):
    import backup
    a_dir, b_dir = DATA_DIR / "data", two_tenants["b"]["hbbs"].parent
    _keypair(a_dir, "a")
    _keypair(b_dir, "b")
    st = backup.run(remote=False)
    assert st["ok"], st
    with tarfile.open(backup.BACKUP_DIR / st["file"]) as tar:
        names = set(tar.getnames())
    # Tenant #1's relay keeps the original layout; others go under relays/<id>/.
    for n in ("rustdesk-fleet/fleet.sqlite3", "rustdesk-fleet/data/id_ed25519",
              "rustdesk-fleet/data/db_v2.sqlite3", "rustdesk-fleet/relays/2/data/id_ed25519",
              "rustdesk-fleet/relays/2/data/db_v2.sqlite3"):
        assert n in names, n
    # Backups are provider events: no tenant.
    assert db_rows("SELECT tenant_id FROM provisioning_events WHERE event = 'backup_succeeded'")[0][0] is None

    (a_dir / "id_ed25519").write_text("lost")
    (b_dir / "id_ed25519").write_text("lost")
    res = backup.restore(backup.BACKUP_DIR / st["file"])
    assert res["ok"], res
    assert (a_dir / "id_ed25519").read_text() == "secret-a"
    assert (b_dir / "id_ed25519").read_text() == "secret-b"
    assert "relays/2/data/id_ed25519" in res["applied"] and "data/id_ed25519" in res["applied"]
