"""nginx's default servers (install.sh default_servers): a hostname with no
server block of its own gets no certificate on 443 (the handshake is
refused) and nothing on 80 except ACME challenges. Tenant #1's host, the
console and provider subdomains still match their own blocks. Runs a real
nginx (skipped when it isn't installed)."""
import http.client
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from conftest import HELPER_MODULE

REPO = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO / "install.sh"


def _bash_fn(name: str) -> str:
    """Run one of install.sh's printing functions and return its output."""
    lines = INSTALL_SH.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    body = next((i for i in range(start, len(lines)) if lines[i] == "NGX"), start)   # past any heredoc
    end = next(i for i in range(body, len(lines)) if lines[i] == "}")
    script = "\n".join(lines[start:end + 1]) + f"\n{name}\n"
    return subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout


def test_default_servers_are_explicit():
    conf = _bash_fn("default_servers")
    assert "listen 80 default_server;" in conf and "listen [::]:80 default_server;" in conf
    assert "listen 443 ssl default_server;" in conf and "listen [::]:443 ssl default_server;" in conf
    assert "ssl_reject_handshake on;" in conf
    assert "location /.well-known/acme-challenge/ { root /var/www/certbot; }" in conf
    assert "return 444;" in conf
    # No certificate of any kind on the default server.
    assert "ssl_certificate" not in conf


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _self_signed(d: Path, host: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1", "-subj", f"/CN={host}",
                    "-keyout", str(d / "privkey.pem"), "-out", str(d / "fullchain.pem")],
                   check=True, capture_output=True)


TENANT1 = "rds.example.com"
CONSOLE = "admin.rds.example.com"
SUBDOMAIN = "abc.rds.example.com"


@pytest.fixture
def nginx(tmp_path):
    binary = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists() else None)
    if not binary or not shutil.which("openssl"):
        pytest.skip("nginx or openssl is not installed")
    http_port, https_port = _free_port(), _free_port()
    live = tmp_path / "live"
    for host in (TENANT1, CONSOLE, SUBDOMAIN):
        _self_signed(live / host, host)
    webroot = tmp_path / "webroot"
    (webroot / ".well-known" / "acme-challenge").mkdir(parents=True)
    (webroot / ".well-known" / "acme-challenge" / "tok123").write_text("challenge-ok")

    def ports(text: str) -> str:
        text = text.replace("listen [::]:80 default_server;", "").replace("listen [::]:443 ssl default_server;", "")
        text = text.replace("listen [::]:443 ssl;", "")
        text = text.replace("listen 80 default_server;", f"listen 127.0.0.1:{http_port} default_server;")
        text = text.replace("listen 443 ssl default_server;", f"listen 127.0.0.1:{https_port} ssl default_server;")
        text = text.replace("listen 443 ssl;", f"listen 127.0.0.1:{https_port} ssl;")
        text = text.replace("listen 80;", f"listen 127.0.0.1:{http_port};")
        return text.replace("/var/www/certbot", str(webroot)).replace("/var/log/nginx/access.log",
                                                                         str(tmp_path / "access.log"))

    cfg = HELPER_MODULE.Config("rds.example.com", tmp_path, le_live=live)
    sites = [ports(_bash_fn("default_servers"))]
    for host in (CONSOLE, SUBDOMAIN):
        sites.append(ports(HELPER_MODULE.vhost_text(cfg, host)))
    # Tenant #1's own vhost (certbot-managed on the live box): 80 -> 443.
    sites.append(ports(f"""server {{ listen 80; server_name {TENANT1}; return 301 https://$host$request_uri; }}
server {{ listen 443 ssl; server_name {TENANT1};
    ssl_certificate {live / TENANT1}/fullchain.pem; ssl_certificate_key {live / TENANT1}/privkey.pem;
    location / {{ return 200 "tenant1"; }} }}"""))
    (tmp_path / "sites.conf").write_text("\n".join(sites).replace("http://127.0.0.1:8000", "http://127.0.0.1:9"))
    t = tmp_path
    (t / "nginx.conf").write_text(
        f"daemon off; pid {t}/nginx.pid; error_log {t}/error.log;\n"
        "events {}\n"
        f"http {{ access_log off; log_format rdf_noquery '$request_method $uri $status'; "
        "map $request_method $rdf_auth_key { POST $binary_remote_addr; default \"\"; } "
        "limit_req_zone $rdf_auth_key zone=rdf_signin:10m rate=30r/m; "
        f"client_body_temp_path {t}; proxy_temp_path {t}; "
        f"fastcgi_temp_path {t}; uwsgi_temp_path {t}; scgi_temp_path {t}; include {t}/sites.conf; }}\n")
    proc = subprocess.Popen([binary, "-e", str(t / "error.log"), "-p", str(t), "-c", str(t / "nginx.conf")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for port in (http_port, https_port):
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(f"nginx did not start: {proc.stderr.read().decode()[:800]}")
                time.sleep(0.1)
    yield http_port, https_port
    proc.terminate()
    proc.wait(timeout=10)


def _cert_cn(port: int, sni: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        with ctx.wrap_socket(sock, server_hostname=sni) as tls:
            der = tls.getpeercert(binary_form=True)
    out = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-subject"], input=der,
                         capture_output=True, check=True).stdout.decode()
    return out.strip().rsplit("CN", 1)[1].strip(" =")


def test_unknown_host_gets_no_certificate(nginx):
    _, https_port = nginx
    with pytest.raises(ssl.SSLError):
        _cert_cn(https_port, "rds.abc.example")


def test_known_hosts_get_their_own_certificate(nginx):
    _, https_port = nginx
    for host in (TENANT1, CONSOLE, SUBDOMAIN):
        assert _cert_cn(https_port, host) == host


def _http(port: int, host: str, path: str = "/") -> int | None:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        c.request("GET", path, headers={"Host": host})
        return c.getresponse().status
    except (http.client.RemoteDisconnected, ConnectionResetError):
        return None     # 444: nginx closed the connection without a reply
    finally:
        c.close()


def test_port_80_answers_unknown_hosts_only_for_acme(nginx):
    http_port, _ = nginx
    assert _http(http_port, "rds.abc.example", "/") is None
    assert _http(http_port, "rds.abc.example", "/login") is None
    assert _http(http_port, "rds.abc.example", "/.well-known/acme-challenge/tok123") == 200
    # Tenant #1's own port-80 block still answers for it.
    assert _http(http_port, TENANT1, "/") == 301
