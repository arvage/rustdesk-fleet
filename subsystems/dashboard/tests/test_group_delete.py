"""Deleting a client group (routes/groups.py group_delete)."""
from pathlib import Path

import branding
from conftest import HOST_B, db_rows, signed_in, signed_in_at
from app import tenancy
from app.deps import get_db


def _empty_group(tenant_id: int, slug: str = "spare") -> int:
    """A group with an installer file, a link, an access grant, an icon and a
    hidden device, but no visible devices."""
    conn = get_db()
    gid = conn.execute("INSERT INTO client_groups (tenant_id, slug, display_name) VALUES (?, ?, 'Spare')",
                       (tenant_id, slug)).lastrowid
    out = tenancy.tenant_files_dir(tenant_id) / "installers"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"RemoteSupport-{slug}-1.5.0-linux.sh"
    f.write_text("#!/bin/sh\n")
    conn.execute("INSERT INTO installers (tenant_id, group_id, platform, rustdesk_version, status, unsigned_path)"
                 " VALUES (?, ?, 'linux', '1.5.0', 'built', ?)", (tenant_id, gid, str(f)))
    conn.execute("INSERT INTO download_links (tenant_id, token, group_id) VALUES (?, ?, ?)",
                 (tenant_id, f"tok-{tenant_id}-{slug}-" + "y" * 20, gid))
    user_id = conn.execute("SELECT id FROM users WHERE tenant_id = ? LIMIT 1", (tenant_id,)).fetchone()[0]
    conn.execute("INSERT INTO user_group_access (tenant_id, user_id, group_id) VALUES (?, ?, ?)",
                 (tenant_id, user_id, gid))
    conn.execute("INSERT INTO devices (tenant_id, rustdesk_id, group_id, hidden, status) VALUES (?, '999', ?, 1, 'deleted')",
                 (tenant_id, gid))
    conn.commit()
    conn.close()
    for p in branding.icon_paths(tenant_id, gid):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return gid


def test_admin_deletes_an_empty_group_and_everything_it_owns(two_tenants):
    a = two_tenants["a"]
    gid = _empty_group(1)
    installer = Path(db_rows("SELECT unsigned_path FROM installers WHERE group_id = ?", gid)[0][0])
    r = signed_in(a["admin"]).post("/groups/spare/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/groups"
    assert db_rows("SELECT id FROM client_groups WHERE id = ?", gid) == []
    for table in ("installers", "download_links", "user_group_access"):
        assert db_rows(f"SELECT 1 FROM {table} WHERE group_id = ?", gid) == [], table
    assert db_rows("SELECT group_id FROM devices WHERE rustdesk_id = '999'")[0][0] is None
    assert not installer.exists()
    assert not branding.has_icon(1, gid)
    assert [tuple(r) for r in db_rows("SELECT event, detail, user_email FROM provisioning_events"
                                      " WHERE event = 'group_deleted'")] == [("group_deleted", "spare", a["admin"]["email"])]
    # The tenant's other group is untouched.
    assert db_rows("SELECT 1 FROM installers WHERE id = ?", a["installer_id"])


def test_a_group_with_devices_is_not_deleted(two_tenants):
    a = two_tenants["a"]
    r = signed_in(a["admin"]).post(f"/groups/{a['group_slug']}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/groups/{a['group_slug']}"
    assert db_rows("SELECT 1 FROM client_groups WHERE id = ?", a["group_id"])
    assert db_rows("SELECT 1 FROM installers WHERE id = ?", a["installer_id"])


def test_viewer_cannot_delete_a_group(two_tenants):
    from conftest import create_user
    _empty_group(1)
    viewer = create_user("viewer@a.example.com", "viewer")
    signed_in(viewer).post("/groups/spare/delete", follow_redirects=False)
    assert db_rows("SELECT 1 FROM client_groups WHERE tenant_id = 1 AND slug = 'spare'")


def test_another_tenants_group_of_the_same_slug_is_untouched(two_tenants):
    b = two_tenants["b"]
    gid_a = _empty_group(1)
    gid_b = _empty_group(2)
    signed_in_at(b["admin"], HOST_B).post("/groups/spare/delete", follow_redirects=False)
    assert db_rows("SELECT 1 FROM client_groups WHERE id = ?", gid_b) == []
    assert db_rows("SELECT 1 FROM client_groups WHERE id = ?", gid_a)
    assert db_rows("SELECT 1 FROM installers WHERE group_id = ?", gid_a)
    assert branding.has_icon(1, gid_a)


def test_creating_a_group_logs_it_once(two_tenants):
    signed_in(two_tenants["a"]["admin"]).post("/groups", data={"slug": "fresh", "display_name": "Fresh"})
    assert len(db_rows("SELECT 1 FROM provisioning_events WHERE event = 'group_created' AND detail = 'fresh'")) == 1
