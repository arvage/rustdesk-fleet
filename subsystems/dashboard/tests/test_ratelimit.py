"""Failed sign-ins lock the email (and, in bulk, the IP)."""
from conftest import db_rows, login, new_client


def bad_logins(client, email, n, ip=None):
    headers = {"X-Real-IP": ip} if ip else None
    return [client.post("/login", data={"email": email, "password": "wrong"}, headers=headers).status_code
            for _ in range(n)]


def test_sixth_bad_password_is_locked(client, admin):
    assert bad_logins(client, admin["email"], 6) == [401] * 5 + [429]
    r = login(client, admin)  # even the right password, while locked
    assert r.status_code == 429
    assert "Too many attempts" in r.text
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'login_locked'")


def test_nonexistent_email_locks_the_same_way(client):
    assert bad_logins(client, "nobody@example.com", 6) == [401] * 5 + [429]


def test_lock_is_per_email(client, admin, tech):
    bad_logins(client, admin["email"], 5)
    assert login(client, tech).status_code == 303


def test_good_login_clears_the_counter(client, admin):
    assert bad_logins(client, admin["email"], 4) == [401] * 4
    assert login(client, admin).status_code == 303
    assert bad_logins(new_client(), admin["email"], 4) == [401] * 4


def test_ip_is_locked_after_20_failures(client, admin):
    codes = [bad_logins(client, f"user{i}@example.com", 1, ip="203.0.113.7")[0] for i in range(21)]
    assert codes == [401] * 20 + [429]
    other_ip = new_client().post("/login", data={"email": admin["email"], "password": admin["password"]},
                                 headers={"X-Real-IP": "198.51.100.1"}, follow_redirects=False)
    assert other_ip.status_code == 303


def test_passkey_endpoints_respect_the_lock(client, admin):
    from app.deps import get_db
    conn = get_db()
    conn.execute("UPDATE users SET auth_method = 'passkey' WHERE id = ?", (admin["id"],))
    conn.commit()
    conn.close()
    # A passkey-only account skips the password check and goes straight to
    # the passkey step (its failures are counted there). Lock the email
    # while that step is pending.
    r = client.post("/login", data={"email": admin["email"], "password": ""}, follow_redirects=False)
    assert r.headers["location"] == "/login/webauthn"
    from app import ratelimit
    for _ in range(ratelimit.MAX_EMAIL_FAILURES):
        ratelimit.record_failure(admin["email"], "")
    r = client.post("/login/webauthn/begin")
    assert r.status_code == 429
    assert "Too many attempts" in r.json()["error"]
    r = client.post("/login/webauthn/complete", json={"id": "x"})
    assert r.status_code == 429


def test_failed_passkey_attempts_count(client, admin):
    from app.deps import get_db
    conn = get_db()
    conn.execute("UPDATE users SET auth_method = 'passkey' WHERE id = ?", (admin["id"],))
    conn.execute("INSERT INTO webauthn_credentials (user_id, credential_id, public_key) VALUES (?, 'cmVhbC1jcmVk', x'00')",
                 (admin["id"],))
    conn.commit()
    conn.close()
    client.post("/login", data={"email": admin["email"], "password": ""})
    codes = []
    for _ in range(6):
        client.post("/login/webauthn/begin")                     # issues a challenge
        codes.append(client.post("/login/webauthn/complete", json={"id": "wrong-cred"}).status_code)
    assert codes == [400] * 5 + [429]
