"""Creating a tenant from the provider console (app/console/tenants.py),
with docker, the helper and certbot faked: the happy path, failure at each
step leaving nothing behind but the reserved port block, and invites."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import DATA_DIR, HOST, db_rows, new_client, provider_client
from app.console import jobs, tenants, web
from app.deps import get_db
from app.tenancy import invalidate_cache

WEB = "acme.rds.example.com"


@pytest.fixture
def fakes(fake_system, monkeypatch):
    """Fake docker/helper (fake_system), guides and certificate expiry."""
    def guides(tenant_id, name, host):
        d = tenants.guides_dir(tenant_id)
        d.mkdir(parents=True, exist_ok=True)
        out = [d / "install-remote-support.pdf", d / "getting-help.pdf"]
        for f in out:
            f.write_bytes(b"%PDF " + name.encode())
        return out
    monkeypatch.setattr(tenants, "generate_guides", guides)
    monkeypatch.setattr(web, "cert_expiry", lambda host, timeout=5.0: "2027-01-25 10:00:00")
    sent = []
    monkeypatch.setattr(tenants, "email_invite", lambda email, name, link: (sent.append((email, link)) or (True, "")))
    fake_system.sent = sent
    return fake_system


def create(c=None, slug="acme", name="Acme IT", email="boss@acme.example.com"):
    c = c or provider_client()
    r = c.post("/tenants", data={"name": name, "slug": slug, "admin_email": email, "admin_name": "Boss"},
               follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    jobs.run_pending()
    invalidate_cache()
    return jobs.get(job_id)


def helper(fake):
    return [" ".join(c[3:]) for c in fake.calls if c[:3] == ["sudo", "-n", "/usr/local/sbin/rdf-helper"]]


def nothing_left(slug="acme"):
    assert db_rows("SELECT * FROM tenants WHERE slug = ?", slug) == []
    assert db_rows("SELECT * FROM relays WHERE host LIKE ?", f"{slug}.%") == []
    assert db_rows("SELECT * FROM tenant_domains WHERE hostname = ?", f"{slug}.rds.example.com") == []
    assert db_rows("SELECT * FROM tenant_invites") == []
    assert db_rows("SELECT * FROM roles WHERE tenant_id <> 1") == []
    assert not (DATA_DIR / "tenants" / "2").exists()


def test_create_a_tenant(fakes):
    job = create()
    assert job["status"] == "succeeded", (job["error"], job["steps"])
    [t] = db_rows("SELECT * FROM tenants WHERE slug = 'acme'")
    assert (t["display_name"], t["status"]) == ("Acme IT", "trial")
    assert {r["key"] for r in db_rows("SELECT key FROM roles WHERE tenant_id = ?", t["id"])} == {"admin", "tech", "viewer"}
    [r] = db_rows("SELECT * FROM relays WHERE tenant_id = ?", t["id"])
    assert (r["host"], r["hbbs_port"], r["hbbr_port"], r["pubkey"], r["status"]) == (
        "acme.relay.rds.example.com", 21201, 21202, f"PUB-{t['id']}", "active")
    [d] = db_rows("SELECT * FROM tenant_domains WHERE tenant_id = ?", t["id"])
    assert (d["hostname"], d["kind"], d["is_primary"], d["tls_status"], d["cert_expires_at"]) == (
        WEB, "provider_subdomain", 1, "active", "2027-01-25 10:00:00")
    assert helper(fakes) == [f"issue-cert {WEB}", f"write-vhost {WEB}", "reload-nginx"]
    assert (DATA_DIR / "tenants" / str(t["id"]) / "guides" / "getting-help.pdf").exists()
    [(email, link)] = fakes.sent
    assert email == "boss@acme.example.com" and link.startswith(f"https://{WEB}/invite/")
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'tenant_created' AND source = 'provider' AND tenant_id = ?", t["id"])
    # The new tenant's own host answers, with its own guides and no /setup.
    c = new_client(WEB)
    assert c.get("/login").status_code == 200
    assert c.get("/setup").status_code == 404
    assert c.get("/devices", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/guides/getting-help.pdf").content == b"%PDF Acme IT"


@pytest.mark.parametrize("step,breaker", [
    ("Provision the relay", lambda f, mp: f.fail.update({"up -d": "docker: image not found"})),
    ("Publish", lambda f, mp: f.fail.update({"issue-cert": "certbot: DNS problem"})),
    ("Publish", lambda f, mp: f.fail.update({"reload-nginx": "[emerg] duplicate zone"})),
    ("Generate the client guides", lambda f, mp: mp.setattr(tenants, "generate_guides",
                                                            lambda *a: (_ for _ in ()).throw(ImportError("No module named 'reportlab'")))),
    ("Invite", lambda f, mp: mp.setattr(tenants, "create_invite", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")))),
])
def test_a_failed_step_undoes_everything_but_the_port_block(fakes, monkeypatch, step, breaker):
    breaker(fakes, monkeypatch)
    job = create()
    assert job["status"] == "failed"
    import json
    steps = json.loads(job["steps"])
    failed = [s["name"] for s in steps if s["status"] == "failed"]
    assert failed and failed[0].startswith(step), steps
    assert steps[-1]["name"] == "Undo what was done", steps[-1]
    # With nginx refusing every reload, the undo says so; everything else is still cleaned up.
    assert steps[-1]["status"] == ("failed" if "reload-nginx" in fakes.fail else "done"), steps[-1]
    nothing_left()
    blocks = [tuple(r) for r in db_rows("SELECT base_port FROM relay_port_blocks WHERE base_port >= 21200")]
    if step != "Check the slug":
        assert blocks == [(21200,)]
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'tenant_create_failed'")
    # The relay and the domain were taken down again.
    if any(c[:2] == ["docker", "compose"] and "up" in c for c in fakes.calls):
        assert any(c[:2] == ["docker", "compose"] and "down" in c for c in fakes.calls)
    if f"write-vhost {WEB}" in helper(fakes):
        assert f"remove-vhost {WEB}" in helper(fakes)
    # Retrying is safe: the slug is free, and a new block is used.
    fakes.fail.clear()
    monkeypatch.undo()
    monkeypatch.setattr(tenants, "email_invite", lambda *a: (True, ""))
    monkeypatch.setattr(tenants, "generate_guides", lambda *a: [])
    monkeypatch.setattr(web, "cert_expiry", lambda *a, **k: None)
    from app.console import system
    monkeypatch.setattr(system, "SYSTEM", fakes)
    job = create()
    assert job["status"] == "succeeded", job["error"]
    assert db_rows("SELECT hbbs_port FROM relays r JOIN tenants t ON t.id = r.tenant_id WHERE t.slug = 'acme'")[0][0] == 21211


def test_bad_slugs_are_refused_up_front(fakes):
    c = provider_client()
    for slug in ("admin", "relay", "www", "ab", "1acme", "acme-", "a" * 33, "tenant1"):
        r = c.post("/tenants", data={"name": "X", "slug": slug, "admin_email": "a@example.com"})
        assert r.status_code == 400, slug
    assert db_rows("SELECT * FROM provider_jobs") == []


def test_purged_slugs_stay_taken(fakes):
    assert create()["status"] == "succeeded"
    conn = get_db()
    conn.execute("UPDATE tenants SET status = 'purged' WHERE slug = 'acme'")
    conn.commit()
    conn.close()
    r = provider_client().post("/tenants", data={"name": "Again", "slug": "acme", "admin_email": "a@example.com"})
    assert r.status_code == 400 and "taken" in r.text


def test_an_interrupted_creation_is_undone_after_a_restart(fakes, monkeypatch):
    # Stop the job right after the relay step, as a restart would.
    real = tenants.web.publish
    monkeypatch.setattr(tenants.web, "publish", lambda host: (_ for _ in ()).throw(KeyboardInterrupt()))
    c = provider_client()
    c.post("/tenants", data={"name": "Acme IT", "slug": "acme", "admin_email": "b@example.com"})
    [row] = db_rows("SELECT * FROM provider_jobs")
    with pytest.raises(KeyboardInterrupt):
        # run the handler directly, without run_one's bookkeeping, then pretend we died
        conn = get_db()
        conn.execute("UPDATE provider_jobs SET status = 'running' WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        monkeypatch.setattr(tenants, "undo_creation", lambda *a: [])   # the dying process never got to undo
        tenants.create_tenant(jobs.Job(jobs.get(row["id"])))
    monkeypatch.undo()
    from app.console import system
    monkeypatch.setattr(system, "SYSTEM", fakes)
    assert db_rows("SELECT 1 FROM tenants WHERE slug = 'acme'")      # half-created
    # A real crash never reaches the handler's own cleanup: the job row still
    # says 'running' and names the tenant it was creating.
    conn = get_db()
    conn.execute("UPDATE provider_jobs SET status = 'running', tenant_id = (SELECT id FROM tenants WHERE slug = 'acme')"
                 " WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    jobs.recover_interrupted()
    job = jobs.get(row["id"])
    assert job["status"] == "failed" and "undone" in job["error"]
    nothing_left()


def test_invite_links(fakes):
    assert create()["status"] == "succeeded"
    [(_, link)] = fakes.sent
    token = link.rsplit("/", 1)[1]
    # Only on the tenant's own host.
    assert new_client(HOST).get(f"/invite/{token}").status_code == 404
    assert provider_client().get(f"/invite/{token}").status_code == 404
    c = new_client(WEB)
    assert c.get(f"/invite/{token}").status_code == 200
    r = c.post(f"/invite/{token}", data={"display_name": "Boss", "password": "long-password-1",
                                         "password2": "long-password-1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert c.get("/devices").status_code == 200
    [u] = db_rows("SELECT u.*, t.slug FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE u.email = 'boss@acme.example.com'")
    assert (u["slug"], u["role"], u["display_name"]) == ("acme", "admin", "Boss")
    # Single use.
    assert new_client(WEB).get(f"/invite/{token}").status_code == 404
    assert new_client(WEB).post(f"/invite/{token}", data={"password": "x" * 12, "password2": "x" * 12}).status_code == 404


def test_invites_expire_after_72_hours_and_can_be_resent(fakes):
    assert create()["status"] == "succeeded"
    [(_, link)] = fakes.sent
    token = link.rsplit("/", 1)[1]
    [inv] = db_rows("SELECT * FROM tenant_invites")
    made = datetime.strptime(inv["created_at"], "%Y-%m-%d %H:%M:%S")
    exp = datetime.strptime(inv["expires_at"], "%Y-%m-%d %H:%M:%S")
    assert timedelta(hours=71, minutes=59) < exp - made <= timedelta(hours=72, minutes=1)
    conn = get_db()
    conn.execute("UPDATE tenant_invites SET expires_at = datetime('now', '-1 minute')")
    conn.commit()
    conn.close()
    assert new_client(WEB).get(f"/invite/{token}").status_code == 404
    c = provider_client()
    tid = db_rows("SELECT id FROM tenants WHERE slug = 'acme'")[0][0]
    c.post(f"/tenants/{tid}/invites/{inv['id']}/resend")
    new_token = fakes.sent[-1][1].rsplit("/", 1)[1]
    assert new_token != token and new_client(WEB).get(f"/invite/{new_token}").status_code == 200
