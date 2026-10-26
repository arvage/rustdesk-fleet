"""First-run setup is locked behind SETUP_TOKEN while no users exist."""
from conftest import SETUP_TOKEN, db_rows

FORM = {"email": "first@example.com", "display_name": "First Admin",
        "password": "first-admin-pw", "password2": "first-admin-pw"}


def users():
    return [r["email"] for r in db_rows("SELECT email FROM users")]


def test_rejected_without_token(client):
    r = client.post("/setup", data=FORM, follow_redirects=False)
    assert r.status_code == 403
    assert users() == []


def test_rejected_with_wrong_token(client):
    r = client.post("/setup", data={**FORM, "setup_token": "not-the-token"}, follow_redirects=False)
    assert r.status_code == 403
    assert users() == []


def test_get_with_wrong_token_in_url(client):
    assert client.get("/setup?token=not-the-token").status_code == 403


def test_get_prefills_token_from_url(client):
    r = client.get(f"/setup?token={SETUP_TOKEN}")
    assert r.status_code == 200
    assert f'value="{SETUP_TOKEN}"' in r.text


def test_works_with_right_token(client):
    r = client.post("/setup", data={**FORM, "setup_token": SETUP_TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?setup=1"
    rows = db_rows("SELECT email, role FROM users")
    assert [(u["email"], u["role"]) for u in rows] == [("first@example.com", "admin")]


def test_closed_once_a_user_exists(client, admin):
    r = client.post("/setup", data={**FORM, "email": "intruder@example.com", "setup_token": SETUP_TOKEN},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert users() == ["admin@example.com"]
    r = client.get(f"/setup?token={SETUP_TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_no_token_configured_accepts_nothing(client, monkeypatch):
    monkeypatch.delenv("SETUP_TOKEN")
    r = client.get("/setup")
    assert r.status_code == 503
    assert "SETUP_TOKEN" in r.text
    r = client.post("/setup", data={**FORM, "setup_token": ""}, follow_redirects=False)
    assert r.status_code == 503
    assert users() == []


def test_wrong_tokens_are_rate_limited(client):
    for _ in range(5):
        assert client.post("/setup", data={**FORM, "setup_token": "guess"}).status_code == 403
    r = client.post("/setup", data={**FORM, "setup_token": SETUP_TOKEN})
    assert r.status_code == 429
    assert users() == []
