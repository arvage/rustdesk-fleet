"""The tenancy migration (app/tenant_migration.py) on a database shaped like
production before tenancy: tests/fixtures/legacy_schema.sql plus sample rows."""
import hashlib
import sqlite3
from pathlib import Path

import pytest

from app import tenant_migration
from app.tenant_migration import NULLABLE_TENANT_TABLES, TENANT_TABLES, migrate

LEGACY_SCHEMA = Path(__file__).parent / "fixtures" / "legacy_schema.sql"
HBBS_PATH = "/srv/relay/data/db_v2.sqlite3"


def legacy_db(path: Path, server_host: str | None = "RDS.example.com") -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA.read_text())
    if server_host:
        conn.execute(
            """INSERT INTO server_config (id, host, hbbs_port, hbbr_port, pubkey, data_dir, compose_path, status)
               VALUES (1, ?, 21115, 21117, 'PUBKEY', '/srv/relay/data', '/srv/relay/docker-compose.yml', 'active')""",
            (server_host,),
        )
    conn.executescript("""
        INSERT INTO client_groups (id, slug, display_name, unattended_password, policy_ts) VALUES
            (1, 'acme', 'Acme', 'pw', 100), (2, 'globex', 'Globex', NULL, NULL);
        INSERT INTO devices (rustdesk_id, group_id, label, hidden) VALUES
            ('111', 1, 'Front desk', 0), ('222', NULL, NULL, 1), ('333', 2, 'Server', 0);
        INSERT INTO installers (id, group_id, platform, rustdesk_version, status) VALUES (1, 1, 'linux', '1.4.9', 'built');
        INSERT INTO users (id, email, display_name, role, password_hash) VALUES
            (1, 'admin@example.com', 'Admin', 'admin', 'x'), (2, 'tech@example.com', 'Tech', 'tech', 'y');
        -- A deleted user: AUTOINCREMENT must not hand id 3 out again.
        INSERT INTO users (id, email, role) VALUES (3, 'gone@example.com', 'tech');
        DELETE FROM users WHERE id = 3;
        INSERT INTO user_group_access (user_id, group_id) VALUES (2, 1);
        INSERT INTO webauthn_credentials (user_id, credential_id, public_key) VALUES (1, 'Y3JlZA', x'00');
        INSERT INTO roles (key, name, permissions, is_system) VALUES
            ('admin', 'Administrator', '*', 1), ('tech', 'Technician', '["manage_devices"]', 1),
            ('custom', 'Custom', '[]', 0);
        INSERT INTO download_links (token, group_id) VALUES ('tok-1', 1);
        INSERT INTO device_info (rustdesk_id, hostname) VALUES ('111', 'PC-1');
        INSERT INTO device_sessions (rustdesk_id, conn_ref) VALUES ('111', 'c1');
        INSERT INTO device_file_transfers (rustdesk_id, direction, file_count, nonce) VALUES ('111', 0, 1, 'n1');
        INSERT INTO connect_clicks (rustdesk_id, user_email) VALUES ('111', 'tech@example.com');
        INSERT INTO rustdesk_peer_users (peer_id, user_email) VALUES ('999', 'tech@example.com');
        INSERT INTO dynamic_groups (name) VALUES ('Old Windows');
        INSERT INTO notification_settings (id, enabled, smtp_host, to_addrs) VALUES (1, 1, 'smtp.example.com', 'ops@example.com');
        INSERT INTO notification_events (event_type, enabled) VALUES ('device_offline', 0);
        INSERT INTO notification_log (event_type, subject, recipients, status) VALUES ('test', 's', 'r', 'sent');
        INSERT INTO email_templates (key, subject) VALUES ('device_offline', 'Gone');
        INSERT INTO email_design (id, brand_name) VALUES (1, 'Example IT');
        INSERT INTO provisioning_events (event, detail) VALUES ('group_created', 'acme'), ('backup_succeeded', '');
        INSERT INTO backup_config (id) VALUES (1);
        INSERT INTO signing_config (id) VALUES (1);
    """)
    conn.commit()
    conn.close()
    return path


def run(path: Path) -> bool:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return migrate(conn, HBBS_PATH)
    finally:
        conn.close()


def counts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    conn.close()
    return out


def fingerprint(path: Path) -> str:
    conn = sqlite3.connect(path)
    h = hashlib.sha256()
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"):
        h.update(sql.encode())
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
            h.update(repr(row).encode())
    conn.close()
    return h.hexdigest()


def query(path: Path, sql: str, *params):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def test_every_row_moves_to_tenant_one_and_counts_are_unchanged(tmp_path):
    db = legacy_db(tmp_path / "fleet.sqlite3")
    before = counts(db)
    assert run(db) is True
    after = counts(db)
    for table, n in before.items():
        if table != "sqlite_sequence":
            assert after[table] == n, table
    for table in TENANT_TABLES:
        assert query(db, f'SELECT COUNT(*) AS n FROM "{table}" WHERE tenant_id IS NOT 1')[0]["n"] == 0, table
        notnull = {r["name"]: r["notnull"] for r in query(db, f'PRAGMA table_info("{table}")')}
        assert notnull["tenant_id"] == (0 if table in NULLABLE_TENANT_TABLES else 1), table
    assert query(db, "PRAGMA foreign_key_check") == []
    assert query(db, "PRAGMA integrity_check")[0][0] == "ok"


def test_tenant_one_inherits_the_relay_and_host(tmp_path):
    db = legacy_db(tmp_path / "fleet.sqlite3")
    run(db)
    [t] = query(db, "SELECT * FROM tenants")
    assert (t["id"], t["slug"], t["display_name"], t["status"]) == (1, "pacificmit", "Pacific MIT", "active")
    [r] = query(db, "SELECT * FROM relays")
    assert (r["tenant_id"], r["host"], r["hbbs_port"], r["hbbr_port"], r["pubkey"]) == (1, "RDS.example.com", 21115, 21117, "PUBKEY")
    assert (r["data_dir"], r["compose_path"], r["hbbs_db_path"], r["status"]) == (
        "/srv/relay/data", "/srv/relay/docker-compose.yml", HBBS_PATH, "active")
    [d] = query(db, "SELECT * FROM tenant_domains")
    assert (d["tenant_id"], d["hostname"], d["kind"], d["is_primary"]) == (1, "rds.example.com", "legacy", 1)
    assert d["verified_at"]
    # Kept for rollback, untouched.
    assert query(db, "SELECT host FROM server_config")[0]["host"] == "RDS.example.com"


def test_running_twice_is_a_no_op(tmp_path):
    db = legacy_db(tmp_path / "fleet.sqlite3")
    run(db)
    first = fingerprint(db)
    assert run(db) is False
    assert fingerprint(db) == first


def test_deleted_ids_are_not_reused(tmp_path):
    db = legacy_db(tmp_path / "fleet.sqlite3")
    run(db)
    conn = sqlite3.connect(db)
    cur = conn.execute("INSERT INTO users (tenant_id, email) VALUES (1, 'new@example.com')")
    assert cur.lastrowid == 4
    conn.close()


def test_uniqueness_is_per_tenant_now(tmp_path):
    db = legacy_db(tmp_path / "fleet.sqlite3")
    run(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO tenants (id, slug, display_name) VALUES (2, 'other', 'Other')")
    conn.execute("INSERT INTO users (tenant_id, email) VALUES (2, 'admin@example.com')")
    conn.execute("INSERT INTO client_groups (tenant_id, slug, display_name) VALUES (2, 'acme', 'Acme 2')")
    conn.execute("INSERT INTO devices (tenant_id, rustdesk_id) VALUES (2, '111')")
    conn.execute("INSERT INTO roles (tenant_id, key, name) VALUES (2, 'custom', 'Custom')")
    conn.execute("INSERT INTO device_info (tenant_id, rustdesk_id) VALUES (2, '111')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (tenant_id, email) VALUES (1, 'admin@example.com')")
    # A device can't point at another tenant's group.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO devices (tenant_id, rustdesk_id, group_id) VALUES (2, '444', 1)")
    # Forgetting tenant_id is an error, not a silent default.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO devices (rustdesk_id) VALUES ('555')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO notification_settings (enabled) VALUES (1)")
    conn.close()


def test_an_interrupted_migration_leaves_the_old_schema(tmp_path, monkeypatch):
    db = legacy_db(tmp_path / "fleet.sqlite3")
    before = fingerprint(db)
    real = tenant_migration._rebuild

    def failing(conn, table, ddl):
        if table == "device_info":
            raise RuntimeError("power cut")
        real(conn, table, ddl)

    monkeypatch.setattr(tenant_migration, "_rebuild", failing)
    with pytest.raises(RuntimeError):
        run(db)
    # Only the (empty) tenancy tables exist on top of the old schema.
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("PRAGMA table_info(users)")].count("tenant_id") == 0
    assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0
    conn.close()
    # Apart from those, the database is byte-for-byte what it was.
    conn = sqlite3.connect(db)
    for t in ("tenants", "relays", "tenant_domains"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('tenants','relays','tenant_domains')")
    conn.commit()
    conn.close()
    assert fingerprint(db) == before

    monkeypatch.setattr(tenant_migration, "_rebuild", real)
    assert run(db) is True
    assert query(db, "SELECT COUNT(*) AS n FROM devices WHERE tenant_id = 1")[0]["n"] == 3


def test_fresh_install_gets_tenant_one_without_a_relay(tmp_path):
    db = legacy_db(tmp_path / "fleet.sqlite3", server_host=None)
    run(db)
    assert [r["id"] for r in query(db, "SELECT id FROM tenants")] == [1]
    assert query(db, "SELECT * FROM relays") == []
    assert query(db, "SELECT * FROM tenant_domains") == []


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_migration.py"


def _script(*args, data_dir: Path):
    import os
    import subprocess
    import sys
    env = {**os.environ, "FLEET_DATA_DIR": str(data_dir)}
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, env=env, timeout=60)


def test_check_migration_script_passes_on_a_copy(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    copy = legacy_db(tmp_path / "copy.sqlite3")
    r = _script(copy, data_dir=data)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "second run: no changes" in r.stdout
    assert "All checks passed." in r.stdout


def test_check_migration_script_refuses_the_live_database(tmp_path):
    live = legacy_db(tmp_path / "fleet.sqlite3")
    before = fingerprint(live)
    r = _script(live, data_dir=tmp_path)
    assert r.returncode == 1 and "Refusing" in r.stdout
    assert fingerprint(live) == before
