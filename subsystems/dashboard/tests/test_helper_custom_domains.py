"""rdf-helper and custom domains (step 5): it checks for itself (the
control database, the tenant's status, and DNS) before issuing a
certificate or writing a vhost, and release-domain works without DNS."""
import sqlite3

import pytest

from test_rdf_helper import FakeRun, H, cfg

CUSTOM = "rds.abc.example"
SUB = "abc.rds.example.com"
TOKEN = "rdf-verify-" + "k" * 32


def _db(c, status="active", kind="custom", host=CUSTOM, verified=True):
    c.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(c.data_dir / "fleet.sqlite3")
    conn.executescript("""
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT, status TEXT);
        CREATE TABLE tenant_domains (id INTEGER PRIMARY KEY, tenant_id INTEGER, hostname TEXT UNIQUE, kind TEXT,
                                     verify_token TEXT, status TEXT, verified_at TEXT);""")
    conn.execute("INSERT INTO tenants VALUES (2, 'abc', ?)", (status,))
    conn.execute("INSERT INTO tenants VALUES (1, 'legacy', 'active')")
    conn.execute("INSERT INTO tenant_domains (tenant_id, hostname, kind) VALUES (2, ?, 'provider_subdomain')", (SUB,))
    conn.execute("INSERT INTO tenant_domains (tenant_id, hostname, kind) VALUES (1, 'rds.legacy.example', 'legacy')")
    conn.execute("INSERT INTO tenant_domains (tenant_id, hostname, kind, verify_token, status, verified_at)"
                 " VALUES (2, ?, ?, ?, 'active', ?)", (host, kind, TOKEN, "2026-10-01 00:00:00" if verified else None))
    conn.commit()
    conn.close()


def dns_pointing(target=SUB):
    def q(resolver, name, rtype):
        if name == CUSTOM and rtype == "A":
            return "NOERROR", [(CUSTOM, "CNAME", target + "."), (target, "A", "198.51.100.10")]
        if name == CUSTOM and rtype == "AAAA":
            return "NOERROR", [(CUSTOM, "CNAME", target + ".")]
        return "NOERROR", []
    return q


def no_dns(resolver, name, rtype):
    raise AssertionError("release-domain must not look at DNS")


def _cfg(tmp_path, dns, run=None):
    c = cfg(tmp_path, run)
    c.dns = dns
    return c


def _certbot(run):
    return [a for a in run.calls if a[0] == "certbot"]


def test_issues_when_its_own_dns_check_passes(tmp_path):
    run = FakeRun()
    c = _cfg(tmp_path, dns_pointing(), run)
    _db(c)
    assert H.main(["issue-cert", CUSTOM], c) == 0
    assert _certbot(run)[0][:4] == ["certbot", "certonly", "--webroot", "-w"] and CUSTOM in _certbot(run)[0]


@pytest.mark.parametrize("dns", [dns_pointing("globex.rds.example.com"), dns_pointing("elsewhere.example"),
                                 lambda r, n, t: ("NXDOMAIN", [])])
def test_refuses_when_its_own_dns_check_fails_even_if_the_database_says_verified(tmp_path, dns, capsys):
    run = FakeRun()
    c = _cfg(tmp_path, dns, run)
    _db(c, verified=True)
    (c.le_live / CUSTOM).mkdir(parents=True)
    (c.le_live / CUSTOM / "fullchain.pem").write_text("cert")
    assert H.main(["issue-cert", CUSTOM], c) == 1
    assert H.main(["write-vhost", CUSTOM], c) == 1
    assert "doesn't point at tenant 2" in capsys.readouterr().err
    assert _certbot(run) == [] and not (c.vhost_dir / f"{CUSTOM}.conf").exists()


def test_refuses_domains_that_arent_custom_domains_in_the_database(tmp_path):
    run = FakeRun()
    c = _cfg(tmp_path, dns_pointing(), run)
    _db(c)
    for host in ("other.abc.example", "rds.legacy.example", "evil.example"):
        for cmd in ("issue-cert", "write-vhost", "remove-vhost", "revoke-cert", "release-domain"):
            assert H.main([cmd, host], c) == 1, (cmd, host)
    # A row that says provider_subdomain for an outside name is not a custom domain either.
    c2 = _cfg(tmp_path / "2", dns_pointing(), run)
    _db(c2, kind="provider_subdomain")
    assert H.main(["issue-cert", CUSTOM], c2) == 1
    assert _certbot(run) == []


@pytest.mark.parametrize("status", ["suspended", "cancelled", "purged"])
def test_refuses_a_closed_tenants_domain(tmp_path, status):
    run = FakeRun()
    c = _cfg(tmp_path, dns_pointing(), run)
    _db(c, status=status)
    for cmd in ("issue-cert", "write-vhost", "remove-vhost", "revoke-cert"):
        assert H.main([cmd, CUSTOM], c) == 1, cmd
    assert run.calls == []


@pytest.mark.parametrize("status", ["active", "cancelled"])
def test_release_domain_works_without_dns(tmp_path, status):
    run = FakeRun()
    c = _cfg(tmp_path, no_dns, run)
    _db(c, status=status)
    c.vhost_dir.mkdir(parents=True)
    (c.vhost_dir / f"{CUSTOM}.conf").write_text("server {}")
    (c.le_live / CUSTOM).mkdir(parents=True)
    assert H.main(["release-domain", CUSTOM], c) == 0
    assert not (c.vhost_dir / f"{CUSTOM}.conf").exists()
    assert ["nginx", "-t"] in run.calls and ["systemctl", "reload", "nginx"] in run.calls
    assert any(a[:2] == ["certbot", "revoke"] and CUSTOM in a for a in run.calls)


def test_release_domain_never_touches_provider_or_legacy_hosts(tmp_path):
    run = FakeRun()
    c = _cfg(tmp_path, no_dns, run)
    _db(c)
    for host in (SUB, "admin.rds.example.com", "rds.legacy.example"):
        assert H.main(["release-domain", host], c) == 1
    assert run.calls == []


def test_custom_vhost_has_its_own_port_80_block(tmp_path):
    c = _cfg(tmp_path, dns_pointing())
    _db(c)
    (c.le_live / CUSTOM).mkdir(parents=True)
    (c.le_live / CUSTOM / "fullchain.pem").write_text("cert")
    assert H.main(["write-vhost", CUSTOM], c) == 0
    text = (c.vhost_dir / f"{CUSTOM}.conf").read_text()
    assert "listen 80;" in text and f"server_name {CUSTOM};" in text
    assert f"location /.well-known/acme-challenge/ {{ root {c.webroot}; }}" in text
    assert "return 301 https://$host$request_uri;" in text
    # Provider subdomains are covered by the wildcard port-80 block already.
    assert "listen 80;" not in H.vhost_text(c, SUB)


def test_env_file_dns_settings(tmp_path):
    env = tmp_path / "dashboard.env"
    env.write_text("PROVIDER_DOMAIN=rds.example.com\nPUBLIC_IPV4=198.51.100.10\nPUBLIC_IPV6=2001:db8::10\n"
                   "DNS_CHECK_RESOLVERS=9.9.9.9,149.112.112.112\n")
    c = H.Config.from_env_file(env)
    assert (c.public_ipv4, c.public_ipv6, c.resolvers) == ("198.51.100.10", "2001:db8::10", ("9.9.9.9", "149.112.112.112"))
    env.write_text("PROVIDER_DOMAIN=rds.example.com\nPUBLIC_IPV4=2001:db8::10\n")
    with pytest.raises(H.HelperError):
        H.Config.from_env_file(env)
    env.write_text("PROVIDER_DOMAIN=rds.example.com\nDNS_CHECK_RESOLVERS=1.1.1.1 ; touch /tmp/x\n")
    with pytest.raises(H.HelperError):
        H.Config.from_env_file(env)
    env.write_text("PROVIDER_DOMAIN=rds.example.com\n")
    assert H.Config.from_env_file(env).resolvers == ("1.1.1.1", "8.8.8.8")


def test_the_dashboard_and_the_helper_apply_the_same_rules():
    # One implementation: the dashboard loads the helper's own file, so the
    # Domains page and the helper can never disagree about a domain.
    from pathlib import Path

    from app import domain_dns
    assert domain_dns.HELPER_FILE.resolve() == Path(H.__file__).resolve()
    for domain in (CUSTOM, "abc.rds.example.com", "localhost", "*.abc.example", "198.51.100.10"):
        assert domain_dns.domain_problem(domain) == H.custom_domain_problem(domain, "rds.example.com")


def test_a_custom_vhost_passes_nginx_t(tmp_path):
    import shutil
    import subprocess
    nginx = shutil.which("nginx") or "/usr/sbin/nginx"
    if not shutil.which("openssl") or not __import__("pathlib").Path(nginx).exists():
        pytest.skip("nginx or openssl not installed")
    c = _cfg(tmp_path, dns_pointing())
    _db(c)
    live = c.le_live / CUSTOM
    live.mkdir(parents=True)
    subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1", "-subj", "/CN=x",
                    "-keyout", str(live / "privkey.pem"), "-out", str(live / "fullchain.pem")], check=True,
                   capture_output=True)
    assert H.main(["write-vhost", CUSTOM], c) == 0
    v = c.vhost_dir / f"{CUSTOM}.conf"
    v.write_text(v.read_text().replace("listen 443 ssl;", "listen 127.0.0.1:18443 ssl;")
                 .replace("listen 80;", "listen 127.0.0.1:18080;").replace("    listen [::]:443 ssl;\n", "")
                 .replace("    listen [::]:80;\n", "").replace("/var/log/nginx/access.log", str(tmp_path / "access.log")))
    t = tmp_path
    (t / "nginx.conf").write_text(
        f"pid {t}/nginx.pid; error_log {t}/err.log;\nevents {{}}\n"
        f"http {{ access_log off; client_body_temp_path {t}; proxy_temp_path {t}; fastcgi_temp_path {t}; "
        f"uwsgi_temp_path {t}; scgi_temp_path {t}; limit_req_zone $binary_remote_addr zone=rdf_signin:1m rate=30r/m; "
        f"log_format rdf_noquery '$remote_addr $request_method $uri $status'; include {c.vhost_dir}/*.conf; }}\n")
    r = subprocess.run([nginx, "-t", "-p", str(t), "-c", str(t / "nginx.conf"), "-e", str(t / "err.log")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
