"""Sessions follow the database: deleted users, role changes and password
changes take effect on the next request."""
from conftest import create_user, db_rows, forge_session, get_status, signed_in

# "/" is the public landing page when signed out, so check a page that
# always needs a session.
SIGNED_IN_PAGE = "/account"

SIGNED_OUT = (303, "/login")
NOT_ALLOWED = (303, "/")          # PermissionError -> flash + redirect home
ADMIN_PAGE = "/users"             # needs manage_users


def test_admin_page_works_when_signed_in(admin):
    assert get_status(signed_in(admin), ADMIN_PAGE)[0] == 200


def test_deleted_user_is_signed_out(admin, tech):
    tech_client = signed_in(tech)
    assert get_status(tech_client, SIGNED_IN_PAGE)[0] == 200
    r = signed_in(admin).post(f"/users/{tech['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert get_status(tech_client, SIGNED_IN_PAGE) == SIGNED_OUT


def test_demoted_admin_loses_admin_pages(admin):
    other = create_user("other-admin@example.com", "admin")
    other_client = signed_in(other)
    assert get_status(other_client, ADMIN_PAGE)[0] == 200
    signed_in(admin).post(f"/users/{other['id']}/role", data={"role": "tech"})
    assert get_status(other_client, ADMIN_PAGE) == SIGNED_OUT
    # Signing back in gives a tech session without the admin page.
    assert get_status(signed_in(other), ADMIN_PAGE) == NOT_ALLOWED


def test_role_is_read_from_database_not_cookie(admin):
    c = signed_in(admin)
    conn_rows = db_rows("SELECT role FROM users WHERE id = ?", admin["id"])
    assert conn_rows[0]["role"] == "admin"
    from app.deps import get_db
    conn = get_db()
    conn.execute("UPDATE users SET role = 'tech' WHERE id = ?", (admin["id"],))  # no version bump
    conn.commit()
    conn.close()
    assert get_status(c, ADMIN_PAGE) == NOT_ALLOWED


def test_admin_password_reset_ends_other_sessions(admin, tech):
    tech_client = signed_in(tech)
    signed_in(admin).post(f"/users/{tech['id']}/reset-password")
    assert get_status(tech_client, SIGNED_IN_PAGE) == SIGNED_OUT


def test_own_password_change_keeps_current_session(admin):
    current = signed_in(admin)
    elsewhere = signed_in(admin)
    r = current.post("/account/password", data={
        "current_password": admin["password"],
        "new_password": "brand-new-password", "new_password2": "brand-new-password",
    })
    assert r.status_code == 200
    assert get_status(current, ADMIN_PAGE)[0] == 200
    assert get_status(elsewhere, SIGNED_IN_PAGE) == SIGNED_OUT


def test_sign_out_everywhere(admin):
    current = signed_in(admin)
    elsewhere = signed_in(admin)
    r = current.post("/account/sign-out-everywhere", follow_redirects=False)
    assert r.status_code == 303
    assert get_status(current, SIGNED_IN_PAGE) == SIGNED_OUT
    assert get_status(elsewhere, SIGNED_IN_PAGE) == SIGNED_OUT
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'signed_out_everywhere'")


def test_admin_can_sign_out_a_user(admin, tech):
    tech_client = signed_in(tech)
    r = signed_in(admin).post(f"/users/{tech['id']}/sign-out", follow_redirects=False)
    assert r.status_code == 303
    assert get_status(tech_client, SIGNED_IN_PAGE) == SIGNED_OUT
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'user_signed_out'")


def test_tech_cannot_sign_out_others(admin, tech):
    admin_client = signed_in(admin)
    signed_in(tech).post(f"/users/{admin['id']}/sign-out")
    assert get_status(admin_client, ADMIN_PAGE)[0] == 200


def test_cookie_without_session_version_is_rejected(admin, client):
    # What a cookie issued before this change looks like.
    old = forge_session({"user_id": admin["id"], "user_role": "admin",
                         "user_name": "admin", "user_email": admin["email"]})
    client.cookies.set("rdf_session", old)
    assert get_status(client, SIGNED_IN_PAGE) == SIGNED_OUT


def test_cookie_with_current_version_is_accepted(admin, client):
    # Positive control for the forged-cookie test above.
    good = forge_session({"user_id": admin["id"], "session_version": 1})
    client.cookies.set("rdf_session", good)
    assert get_status(client, SIGNED_IN_PAGE)[0] == 200
