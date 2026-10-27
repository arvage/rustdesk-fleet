"""Provider controls live in the console now: backups, signing, host health
and its alerts, the client version, settings. Tenant #1 no longer has them."""
from conftest import CONSOLE_HOST, db_rows, provider_client, signed_in
from app import provider_config


def test_console_server_pages_render():
    c = provider_client()
    for path in ("/backup", "/signing", "/health", "/health.json", "/settings", "/audit", "/admins", "/"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    h = c.get("/health.json").json()
    assert {"mem", "disk", "cpu", "relays"} <= set(h)
    assert [r["tenant"] for r in h["relays"]] == ["tenant1"]
    r = c.post("/backup/run", follow_redirects=False)
    assert r.headers["location"] == "/backup"
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'backup_succeeded' AND tenant_id IS NULL")


def test_tenant_one_no_longer_has_provider_controls(admin):
    c = signed_in(admin)
    for method, path in (("GET", "/admin/backup"), ("GET", "/admin/signing"), ("POST", "/admin/backup/run"),
                         ("POST", "/server/backup"), ("POST", "/server/update"), ("POST", "/installers/update-version"),
                         ("POST", "/admin/restart-relay"), ("POST", "/notifications/health-alerts")):
        r = c.request(method, path, follow_redirects=False)
        assert r.status_code in (404, 405), (method, path, r.status_code)
    home = c.get("/").text
    assert "Back up now" not in home and "Update to" not in home and 'id="h-cpu"' not in home
    assert "Backup" not in c.get("/admin").text
    assert "cpu" not in c.get("/api/server/health").json()
    # Tenant roles no longer offer provider capabilities.
    assert "update_server" not in c.get("/admin/roles").text and "manage_backups" not in c.get("/admin/roles").text


def test_settings_save_and_keep_the_smtp_password():
    c = provider_client()
    c.post("/settings", data={"smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_tls": "starttls",
                              "smtp_user": "u", "smtp_pass": "secret", "from_addr": "fleet@example.com",
                              "mem_threshold": "80", "disk_threshold": "150", "alerts_enabled": "1"})
    s = provider_config.get_all()
    assert (s["smtp_host"], s["smtp_pass"], s["mem_threshold"], s["disk_threshold"]) == ("smtp.example.com", "secret", "80", "100")
    c.post("/settings", data={"smtp_host": "smtp.example.com", "from_addr": "fleet@example.com", "smtp_tls": "ssl"})
    assert provider_config.get_all()["smtp_pass"] == "secret"
    assert "secret" not in c.get("/settings").text
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'provider_settings_updated' AND source = 'provider'")


def test_host_alerts_go_to_provider_admins(monkeypatch):
    from app.console import mail
    from app.routes import api
    provider_client("ops@example.com")
    provider_client("night@example.com")
    provider_config.set_many({"smtp_host": "smtp.example.com", "from_addr": "fleet@example.com",
                              "alerts_enabled": "1", "mem_threshold": "50"})
    sent = []
    monkeypatch.setattr("app.notifications._send_sync", lambda settings, to, subject, html, reply_to="": sent.append((to, subject)))
    monkeypatch.setattr(api, "_host_health", lambda: {"mem": {"pct": 90.0, "used_kb": 9, "total_kb": 10}, "disk": {"pct": 10.0}})
    for st in api._health_alert_state.values():
        st.update(high=False, count=0)
    for _ in range(api._HEALTH_ALERT_DEBOUNCE + 1):
        api._check_health_thresholds()
    assert sent == [(["ops@example.com", "night@example.com"], "Server memory high — 90.0% used")]
    assert mail.smtp_settings()["tenant_id"] is None
