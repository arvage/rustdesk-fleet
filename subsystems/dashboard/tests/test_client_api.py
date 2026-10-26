"""Device reports are only accepted from ids registered with hbbs, with the
matching uuid. Rejected reports get the same empty reply and change nothing."""
import base64
import sqlite3

from conftest import add_peer, db_rows

RID = "123456789"
UUID = b"device-uuid-0001"


def heartbeat(client, rid, uuid: bytes):
    return client.post("/api/heartbeat", json={"id": rid, "uuid": base64.b64encode(uuid).decode(), "conns": []})


def device_rows():
    return db_rows("SELECT rustdesk_id FROM device_info")


def test_registered_device_is_accepted(client):
    add_peer(RID, UUID)
    r = heartbeat(client, RID, UUID)
    assert r.status_code == 200
    assert [row["rustdesk_id"] for row in device_rows()] == [RID]


def test_unknown_id_is_rejected(client):
    add_peer(RID, UUID)
    r = heartbeat(client, "999999999", UUID)
    assert r.status_code == 200 and r.json() == {}
    assert device_rows() == []


def test_wrong_uuid_is_rejected(client):
    add_peer(RID, UUID)
    r = heartbeat(client, RID, b"someone-elses-uuid")
    assert r.status_code == 200 and r.json() == {}
    assert device_rows() == []


def test_unknown_ids_reread_hbbs_at_most_every_5_seconds(client, monkeypatch):
    from app.routes import client_api
    reads = []
    real_connect = sqlite3.connect

    def counting_connect(target, *a, **kw):
        if "db_v2" in str(target):
            reads.append(target)
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(client_api.sqlite3, "connect", counting_connect)
    for i in range(30):
        heartbeat(client, f"55500{i:04d}", UUID)
    assert len(reads) == 1

    # Once 5 seconds have passed, an unknown id may force another read.
    [cache] = client_api._peer_caches.values()
    cache.read_at -= 6
    heartbeat(client, "555009999", UUID)
    assert len(reads) == 2
