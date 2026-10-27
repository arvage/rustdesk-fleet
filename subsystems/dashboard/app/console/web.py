"""Tenant web domains on this box: certificate, nginx vhost, reload, all
through the root-owned helper (subsystems/provisioning/rdf_helper.py,
installed as /usr/local/sbin/rdf-helper). The dashboard user has no other
root access.
"""
from __future__ import annotations

import socket
import ssl
from datetime import datetime, timezone

from app import provider_config
from app.console import system


class WebError(Exception):
    pass


def _helper(*args: str, timeout: int = 300) -> str:
    r = system.SYSTEM.helper(*args, timeout=timeout)
    if not r.ok:
        raise WebError(r.error())
    return r.stdout.strip()


def issue_cert(host: str) -> None:
    email = provider_config.get("letsencrypt_email")
    _helper("issue-cert", host, *([email] if email else []), timeout=600)


def publish(host: str) -> None:
    """Certificate, vhost and a verified nginx reload for one web domain."""
    issue_cert(host)
    _helper("write-vhost", host)
    try:
        _helper("reload-nginx")
    except WebError:
        # Leave nginx as it was: drop the vhost we just wrote and reload again.
        try:
            _helper("remove-vhost", host)
            _helper("reload-nginx")
        except WebError:
            pass
        raise


def unpublish(host: str, revoke: bool = True) -> None:
    """Remove a web domain's vhost (reloading nginx) and, optionally, its
    certificate. Safe to call when parts are already gone."""
    _helper("remove-vhost", host)
    _helper("reload-nginx")
    if revoke:
        _helper("revoke-cert", host)


def purge_relay_data(tenant_id: int) -> None:
    _helper("purge-relay-data", str(int(tenant_id)))


def cert_expiry(host: str, timeout: float = 5.0) -> str | None:
    """notAfter of the certificate nginx serves for host (UTC 'YYYY-MM-DD
    HH:MM:SS'), read over TLS from this box; None if it can't be read."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError:
        return None
    try:
        import tempfile, os
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
        try:
            info = ssl._ssl._test_decode_cert(f.name)  # stdlib; no extra dependency
        finally:
            os.unlink(f.name)
        when = datetime.strptime(info["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return when.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
