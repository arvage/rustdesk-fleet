"""The root-owned helper (subsystems/provisioning/rdf_helper.py): strict
argument checks, the vhost it writes, and a reload that fails on [emerg]."""
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("rdf_helper", ROOT / "subsystems" / "provisioning" / "rdf_helper.py")
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)


class FakeRun:
    def __init__(self, cfg=None, results=None, on=None):
        self.calls, self.results, self.on = [], results or {}, on or {}

    def __call__(self, args, timeout=300):
        self.calls.append(args)
        key = " ".join(args[:2])
        if key in self.on:
            self.on[key]()
        return self.results.get(key, (0, ""))


def cfg(tmp_path, run=None):
    return H.Config("rds.example.com", tmp_path / "data", vhost_dir=tmp_path / "vhosts", webroot=tmp_path / "www",
                    le_live=tmp_path / "live", error_log=tmp_path / "error.log", run=run or FakeRun(), sleep=lambda s: None)


@pytest.mark.parametrize("host", ["acme.rds.example.com", "admin.rds.example.com", "a1-b2.rds.example.com"])
def test_allowed_hosts(tmp_path, host):
    assert H.check_host(cfg(tmp_path), host) == host


@pytest.mark.parametrize("host", [
    "rds.example.com", "evil.com", "acme.rds.example.com.evil.com", "x.acme.rds.example.com",
    "acme.relay.rds.example.com", "relay.rds.example.com", "ACME.rds.example.com",
    "ac.rds.example.com", "1acme.rds.example.com", "acme-.rds.example.com", "a/b.rds.example.com",
    "acme.rds.example.com;reboot", " acme.rds.example.com", "../etc.rds.example.com", "",
])
def test_rejected_hosts(tmp_path, host):
    with pytest.raises(H.HelperError):
        H.check_host(cfg(tmp_path), host)


def test_main_validates_before_doing_anything(tmp_path):
    run = FakeRun()
    c = cfg(tmp_path, run)
    assert H.main(["issue-cert", "evil.com"], c) == 1
    assert H.main(["issue-cert", "acme.rds.example.com", "not an email"], c) == 1
    assert H.main(["rm", "-rf", "/"], c) == 2
    assert H.main(["write-vhost"], c) == 2
    assert H.main(["reload-nginx", "extra"], c) == 2
    assert run.calls == []


def test_issue_cert_uses_webroot_and_no_shell(tmp_path):
    run = FakeRun()
    assert H.main(["issue-cert", "acme.rds.example.com", "ops@example.com"], cfg(tmp_path, run)) == 0
    [args] = run.calls
    assert args[:4] == ["certbot", "certonly", "--webroot", "-w"]
    assert "-d" in args and args[args.index("-d") + 1] == "acme.rds.example.com"
    assert args[-2:] == ["--email", "ops@example.com"]


def test_write_vhost_needs_the_certificate_and_matches_install_sh(tmp_path):
    c = cfg(tmp_path)
    assert H.main(["write-vhost", "acme.rds.example.com"], c) == 1
    (tmp_path / "live" / "acme.rds.example.com").mkdir(parents=True)
    (tmp_path / "live" / "acme.rds.example.com" / "fullchain.pem").write_text("x")
    assert H.main(["write-vhost", "acme.rds.example.com"], c) == 0
    text = (tmp_path / "vhosts" / "acme.rds.example.com.conf").read_text()
    assert "server_name acme.rds.example.com;" in text and "listen 443 ssl;" in text
    install = (ROOT / "install.sh").read_text()
    locations = install.split("dash_locations() {\n  cat <<'NGX'\n", 1)[1].split("NGX\n}", 1)[0]
    assert H.DASH_LOCATIONS == locations
    assert H.main(["remove-vhost", "acme.rds.example.com"], c) == 0
    assert not (tmp_path / "vhosts" / "acme.rds.example.com.conf").exists()


def test_reserved_slugs_match_the_dashboards():
    from app import settings
    assert H.RESERVED_SLUGS == settings.RESERVED_SLUGS
    assert H.SLUG_RE.pattern == settings.SLUG_RE.pattern


def test_reload_fails_when_the_error_log_gains_an_emerg_line(tmp_path):
    log = tmp_path / "error.log"
    log.write_text("2026/10/01 00:00:00 [emerg] an old one that doesn't count\n")
    run = FakeRun(on={"systemctl reload": lambda: log.open("a").write(
        '2026/10/27 10:00:00 [emerg] 1234#1234: duplicate zone "rdf_signin"\n')})
    c = cfg(tmp_path, run)
    assert H.main(["reload-nginx"], c) == 1
    # Without a new [emerg], the reload passes (old ones are ignored).
    run.on = {"systemctl reload": lambda: log.open("a").write("2026/10/27 10:01:00 [notice] reloaded\n")}
    assert H.main(["reload-nginx"], c) == 0


def test_a_failing_nginx_t_never_reloads(tmp_path):
    run = FakeRun(results={"nginx -t": (1, "nginx: [emerg] unknown directive")})
    assert H.main(["reload-nginx"], cfg(tmp_path, run)) == 1
    assert [a[:2] for a in run.calls] == [["nginx", "-t"]]


def test_purge_relay_data_is_confined(tmp_path):
    c = cfg(tmp_path)
    relay = tmp_path / "data" / "tenants" / "5" / "relay" / "data"
    relay.mkdir(parents=True)
    (relay / "id_ed25519").write_text("k")
    for bad in ("1", "0", "-5", "5/../../etc", "5 6", "abc", "05"):
        assert H.main(["purge-relay-data", bad], c) == 1, bad
    assert relay.exists()
    assert H.main(["purge-relay-data", "5"], c) == 0
    assert not (tmp_path / "data" / "tenants" / "5" / "relay").exists()
    assert (tmp_path / "data" / "tenants" / "5").exists()


def test_the_vhost_passes_nginx_t(tmp_path):
    nginx = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists() else None)
    openssl = shutil.which("openssl")
    if not nginx or not openssl:
        pytest.skip("nginx or openssl not installed")
    live = tmp_path / "live" / "acme.rds.example.com"
    live.mkdir(parents=True)
    subprocess.run([openssl, "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1", "-subj", "/CN=acme",
                    "-keyout", str(live / "privkey.pem"), "-out", str(live / "fullchain.pem")],
                   check=True, capture_output=True)
    c = cfg(tmp_path)
    assert H.main(["write-vhost", "acme.rds.example.com"], c) == 0
    # Unprivileged nginx -t can't bind 443: test the same file on a high port.
    v = tmp_path / "vhosts" / "acme.rds.example.com.conf"
    v.write_text(v.read_text().replace("listen 443 ssl;", "listen 127.0.0.1:18443 ssl;").replace("    listen [::]:443 ssl;\n", "")
                 .replace("/var/log/nginx/access.log", str(tmp_path / "access.log")))
    t = tmp_path
    (t / "nginx.conf").write_text(
        f"pid {t}/nginx.pid; error_log {t}/err.log;\nevents {{}}\n"
        f"http {{ access_log off; client_body_temp_path {t}; proxy_temp_path {t}; fastcgi_temp_path {t}; uwsgi_temp_path {t}; "
        f"scgi_temp_path {t}; limit_req_zone $binary_remote_addr zone=rdf_signin:1m rate=30r/m; "
        f"log_format rdf_noquery '$remote_addr $request_method $uri $status'; "
        f"include {t}/vhosts/*.conf; }}\n")
    r = subprocess.run([nginx, "-t", "-p", str(t), "-c", str(t / "nginx.conf"), "-e", str(t / "err.log")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
