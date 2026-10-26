"""The nginx vhost install.sh writes: the sign-in limit counts only POSTs, and
only the legacy port-21114 listener can tell the app a request came in on it.

The vhost is rendered with install.sh's own functions. If nginx is
installed, it is also run (unprivileged, on a free port, in front of a stub
upstream) to check the behaviour, not just the text.
"""
import http.server
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO / "install.sh"
HOST = "rds.example.com"


def render_http_vhost(tmp_path: Path, client_port: int = 0) -> str:
    """Run install.sh's write_http_vhost into a temp file and return it."""
    lines = INSTALL_SH.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("client_port_block() {"))
    end = next(i for i, l in enumerate(lines) if l == 'case "$TLS_MODE" in')
    fns = tmp_path / "vhost_fns.sh"
    fns.write_text("\n".join(lines[start:end]) + "\n")
    vhost = tmp_path / "vhost.conf"
    script = (f'sudo() {{ "$@"; }}; HOST={HOST}; CLIENT_PORT={client_port}; VHOST="{vhost}"; '
              f'source "{fns}"; write_http_vhost')
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)
    return vhost.read_text()


def test_only_posts_are_keyed(tmp_path):
    conf = render_http_vhost(tmp_path)
    assert "map $request_method $rdf_auth_key {" in conf
    assert "POST    $binary_remote_addr;" in conf
    assert 'default "";' in conf
    assert "limit_req_zone $rdf_auth_key zone=rdf_signin:10m rate=30r/m;" in conf
    assert conf.count("limit_req        zone=rdf_signin burst=20 nodelay;") == 3
    for loc in ("location = /login {", "location ^~ /login/webauthn {", "location = /setup {"):
        assert loc in conf
    # A trailing slash here makes nginx and the app redirect each other.
    assert "location ^~ /login/webauthn/ {" not in conf


def test_only_the_legacy_listener_sets_the_listener_header(tmp_path):
    conf = render_http_vhost(tmp_path, client_port=1)
    main, legacy = conf.split("listen 21114;")
    # Every proxied dashboard location drops a client-sent copy...
    assert main.count("proxy_pass") == main.count('proxy_set_header   X-Fleet-Listener  "";') == 4
    assert "legacy-api" not in main
    # ...and the 21114 block sets its own value, overriding any sent.
    assert legacy.count("proxy_pass") == legacy.count("proxy_set_header   X-Fleet-Listener  legacy-api;") == 1
    assert "21114" not in render_http_vhost(tmp_path, client_port=0)


# ── Running nginx for real ───────────────────────────────────────────────────

class _Upstream(http.server.BaseHTTPRequestHandler):
    """Answers 200 with the X-Fleet-Listener header it received (or "ok")."""
    def _ok(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = (self.headers.get("X-Fleet-Listener") or "ok").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _ok

    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def nginx_urls(tmp_path):
    """(dashboard vhost URL, legacy port-21114 listener URL) on a real nginx."""
    nginx = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists() else None)
    if not nginx:
        pytest.skip("nginx is not installed")

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port, legacy_port = _free_port(), _free_port()

    vhost = render_http_vhost(tmp_path, client_port=1)
    vhost = vhost.replace("listen 80;", f"listen 127.0.0.1:{port};")
    vhost = vhost.replace("listen 21114;", f"listen 127.0.0.1:{legacy_port};").replace("listen [::]:21114;", "")
    vhost = vhost.replace("http://127.0.0.1:8000", f"http://127.0.0.1:{upstream.server_port}")
    (tmp_path / "site.conf").write_text(vhost)
    t = tmp_path
    (t / "nginx.conf").write_text(
        f"daemon off; pid {t}/nginx.pid; error_log {t}/error.log;\n"
        "events {}\n"
        f"http {{ access_log off; client_body_temp_path {t}; proxy_temp_path {t}; "
        f"fastcgi_temp_path {t}; uwsgi_temp_path {t}; scgi_temp_path {t}; include {t}/site.conf; }}\n"
    )
    proc = subprocess.Popen([nginx, "-e", str(t / "error.log"), "-p", str(t), "-c", str(t / "nginx.conf")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for p in (port, legacy_port):
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", p), timeout=0.2).close()
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(f"nginx did not start: {proc.stderr.read().decode()[:500]}")
                time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", f"http://127.0.0.1:{legacy_port}"
    proc.terminate()
    proc.wait(timeout=10)
    upstream.shutdown()


@pytest.fixture
def nginx_url(nginx_urls):
    return nginx_urls[0]


def _request(url: str, method: str = "GET", headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, method=method, data=b"x=1" if method == "POST" else None,
                                 headers={"Host": HOST, **(headers or {})})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        r = opener.open(req, timeout=5)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def _status(url: str, method: str = "GET") -> int:
    return _request(url, method)[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def test_gets_are_never_limited(nginx_url):
    for path in ("/login", "/login/webauthn", "/setup"):
        assert {_status(nginx_url + path) for _ in range(40)} == {200}, path


def test_posts_are_limited_after_the_burst(nginx_url):
    codes = [_status(nginx_url + "/login", "POST") for _ in range(30)]
    allowed = codes.count(200)
    assert 21 <= allowed <= 23, codes          # burst 20 + the first, plus refill while sending
    assert codes[-1] == 429
    # Page loads still work while this IP's POSTs are being refused.
    assert _status(nginx_url + "/login") == 200


def test_device_api_is_not_limited(nginx_url):
    assert {_status(nginx_url + "/api/heartbeat", "POST") for _ in range(40)} == {200}


def test_passkey_page_is_not_redirected(nginx_url):
    # Regression: "location ^~ /login/webauthn/" made nginx 301 this URL.
    assert _status(nginx_url + "/login/webauthn") == 200


def test_listener_header_from_outside_is_stripped(nginx_urls):
    main, legacy = nginx_urls
    spoof = {"X-Fleet-Listener": "legacy-api"}
    # Dashboard vhost: the device API and pages never see a client's copy.
    assert _request(main + "/api/heartbeat", "POST", spoof) == (200, "ok")
    assert _request(main + "/login", "GET", spoof) == (200, "ok")
    assert _request(main + "/login", "POST", spoof) == (200, "ok")


def test_legacy_listener_sets_the_header(nginx_urls):
    _, legacy = nginx_urls
    assert _request(legacy + "/api/heartbeat", "POST") == (200, "legacy-api")
    assert _request(legacy + "/api/heartbeat", "POST", {"X-Fleet-Listener": "something-else"}) == (200, "legacy-api")
    assert _request(legacy + "/api/audit/conn", "POST", {"Host": "203.0.113.9"}) == (200, "legacy-api")
    # Only the device API is exposed there.
    assert _request(legacy + "/login")[0] == 404
