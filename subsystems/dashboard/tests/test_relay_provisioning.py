"""Relay port blocks, compose rendering, provisioning and operations
(app/console/relays.py), with docker faked."""
import pytest

from conftest import add_tenant, db_rows, provider_client
from app.console import jobs, relays
from app.deps import get_db
from app.tenancy import load_tenant


def test_tenant_one_keeps_the_default_block_and_gets_its_real_rendezvous_port():
    t = load_tenant(1)
    assert (t.relay.hbbs_port, t.relay.hbbr_port) == (21116, 21117)
    assert t.relay.ports == {21115, 21116, 21117, 21118, 21119}
    assert [tuple(r) for r in db_rows("SELECT base_port, tenant_id FROM relay_port_blocks")] == [(21115, 1)]


def test_blocks_are_allocated_in_order_and_never_reused(monkeypatch):
    conn = get_db()
    b1 = relays.allocate_block(conn, 2, "acme")
    b2 = relays.allocate_block(conn, 3, "globex")
    conn.commit()
    assert (b1.base, b1.nat, b1.hbbs, b1.hbbr, b1.ws_id, b1.ws_relay) == (21200, 21200, 21201, 21202, 21203, 21204)
    assert b2.base == 21210
    # Even with the newest block's tenant gone, the next block is new.
    conn.execute("UPDATE relay_port_blocks SET tenant_id = NULL WHERE base_port = 21210")
    assert relays.allocate_block(conn, 4, "initech").base == 21220
    monkeypatch.setenv("RELAY_PORT_MAX", "21233")
    with pytest.raises(relays.RelayError, match="No relay ports left"):
        relays.allocate_block(conn, 5, "umbrella")
    conn.close()


def test_compose_follows_the_port_layout():
    text = relays.render_compose(7, "acme", "acme.relay.rds.example.com", relays.Block(21260), "1.1.16")
    assert "name: rdf-tenant-7" in text
    assert "command: hbbs -p 21261 -r acme.relay.rds.example.com:21262" in text
    assert "command: hbbr -p 21262" in text
    assert text.count("network_mode: host") == 2
    assert text.count("image: rustdesk/rustdesk-server:1.1.16") == 2
    assert "container_name: rdf-t7-hbbs" in text and "container_name: rdf-t7-hbbr" in text
    assert "-k" not in text   # same key handling as tenant #1's stack (hbbs enforces its key by default)


def _new_tenant(tid=2, slug="acme"):
    conn = get_db()
    conn.execute("INSERT INTO tenants (id, slug, display_name, status) VALUES (?, ?, ?, 'trial')", (tid, slug, slug.title()))
    block = relays.allocate_block(conn, tid, slug)
    conn.commit()
    return conn, block


def test_provision_records_the_relay_and_its_key(fake_system):
    conn, block = _new_tenant()
    key = relays.provision(conn, 2, "acme", block, "1.1.16")
    conn.close()
    t = load_tenant(2)
    assert key == "PUB-2" and t.relay.pubkey == "PUB-2" and t.relay.status == "active"
    assert (t.relay.host, t.relay.hbbs_port, t.relay.hbbr_port) == ("acme.relay.rds.example.com", 21201, 21202)
    assert t.relay.compose_path.endswith("/tenants/2/relay/docker-compose.yml")
    assert t.relay.hbbs_db_path.endswith("/tenants/2/relay/data/db_v2.sqlite3")
    assert fake_system.commands("docker compose -f") == [f"docker compose -f {t.relay.compose_path} up -d"]
    assert relays.health(t) == {"hbbs": {"running": True, "status": "Up 5 minutes", "name": "rdf-t2-hbbs"},
                                "hbbr": {"running": True, "status": "Up 5 minutes", "name": "rdf-t2-hbbr"}}


def test_stop_start_and_upgrade(fake_system):
    conn, block = _new_tenant()
    relays.provision(conn, 2, "acme", block, "1.1.15")
    conn.close()
    t = load_tenant(2)
    relays.stop(t)
    assert load_tenant(2).relay.status == "stopped" and relays.health(t)["hbbs"]["running"] is False
    relays.start(t)
    relays.upgrade(load_tenant(2), "1.1.16")
    assert relays.running_version(load_tenant(2)) == "1.1.16"
    assert db_rows("SELECT server_version FROM relays WHERE tenant_id = 2")[0][0] == "1.1.16"
    assert f"docker compose -f {t.relay.compose_path} pull" in fake_system.commands("docker compose")


def test_upgrade_all_goes_one_by_one_and_stops_at_the_first_failure(fake_system):
    from pathlib import Path
    for tid, slug in ((2, "acme"), (3, "globex"), (4, "initech")):
        conn, block = _new_tenant(tid, slug)
        relays.provision(conn, tid, slug, block, "1.1.15")
        conn.close()
    t1 = load_tenant(1)
    Path(t1.relay.compose_path).write_text("services:\n  hbbs:\n    image: rustdesk/rustdesk-server:1.1.15\n")
    fake_system.fail["tenants/3/relay/docker-compose.yml pull"] = "pull: network unreachable"
    job_id = jobs.enqueue("relay_upgrade_all", None, {"version": "1.1.16"}, "ops@example.com")
    jobs.run_pending()
    job = jobs.get(job_id)
    assert job["status"] == "failed" and "globex" in job["error"]
    assert relays.running_version(load_tenant(1)) == "1.1.16"
    assert relays.running_version(load_tenant(2)) == "1.1.16"
    assert relays.running_version(load_tenant(4)) == "1.1.15"     # never reached


def test_console_relay_actions_run_as_jobs(fake_system):
    c = provider_client()
    from pathlib import Path
    t1 = load_tenant(1)
    Path(t1.relay.compose_path).write_text("services:\n  hbbs:\n    image: rustdesk/rustdesk-server:1.1.16\n    container_name: hbbs\n")
    assert c.get("/relays").status_code == 200
    r = c.post("/relays/1/restart", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    jobs.run_pending()
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert c.get(f"/jobs/{job_id}.json").json()["status"] == "succeeded"
    assert c.get(f"/jobs/{job_id}").status_code == 200
    assert f"docker compose -f {t1.relay.compose_path} restart" in fake_system.commands("docker compose")
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'relay_restart' AND source = 'provider' AND tenant_id = 1")
    assert c.post("/relays/99/restart", follow_redirects=False).headers["location"] == "/relays"


def test_an_interrupted_job_is_marked_failed():
    job_id = jobs.enqueue("relay_action", 1, {"action": "restart"}, "ops@example.com")
    conn = get_db()
    conn.execute("UPDATE provider_jobs SET status = 'running' WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    jobs.recover_interrupted()
    row = jobs.get(job_id)
    assert row["status"] == "failed" and "restart" in row["error"]
