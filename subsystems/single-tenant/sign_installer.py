"""
sign_installer.py — Authenticode-sign a built installer with Azure Trusted
Signing, using jsign on this Linux box (no Windows runner needed).

Config lives in the dashboard DB (signing_config, one row). We mint a short-
lived Azure token with the service-principal client credentials, then hand it
to jsign as the keystore password:

  java -jar jsign.jar --storetype TRUSTEDSIGNING \
       --keystore <region>.codesigning.azure.net --storepass <token> \
       --alias <account>/<profile> --tsaurl http://timestamp.acs.microsoft.com \
       --replace <installer.exe>

jsign 7.x (Jan 2025+) added the TRUSTEDSIGNING store type. Tokens are valid
~1h, so we mint one per sign/test call.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FLEET_DIR = Path("/opt/rustdesk-fleet")
FLEET_DB = FLEET_DIR / "fleet.sqlite3"
JSIGN_JAR = FLEET_DIR / "tools" / "jsign-7.1.jar"
JAVA = "java"
TSA_URL = "http://timestamp.acs.microsoft.com"   # Trusted Signing's RFC3161 TSA
TOKEN_SCOPE = "https://codesigning.azure.net/.default"

_DEFAULTS = {
    "enabled": 0, "auto_sign": 1, "provider": "azure_trusted_signing",
    "azure_tenant_id": "", "azure_client_id": "", "azure_client_secret": "",
    "endpoint": "", "account_name": "", "profile_name": "",
}


def get_config() -> dict:
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{FLEET_DB}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM signing_config WHERE id = 1").fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception:
        pass
    return dict(_DEFAULTS)


def _keystore_host(cfg: dict) -> str:
    """jsign wants the bare region host, e.g. weu.codesigning.azure.net."""
    ep = (cfg.get("endpoint") or "").strip()
    return ep.replace("https://", "").replace("http://", "").strip("/")


def _missing_fields(cfg: dict) -> list[str]:
    need = {
        "azure_tenant_id": "Tenant ID", "azure_client_id": "Client ID",
        "azure_client_secret": "Client secret", "endpoint": "Endpoint",
        "account_name": "Account name", "profile_name": "Certificate profile",
    }
    return [label for key, label in need.items() if not (cfg.get(key) or "").strip()]


def get_token(cfg: dict) -> str:
    """Client-credentials OAuth token for Trusted Signing. Raises on failure."""
    tenant = (cfg.get("azure_tenant_id") or "").strip()
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": (cfg.get("azure_client_id") or "").strip(),
        "client_secret": (cfg.get("azure_client_secret") or "").strip(),
        "scope": TOKEN_SCOPE,
    }).encode()
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            j = json.loads(e.read().decode())
            detail = j.get("error_description", j.get("error", "")).splitlines()[0]
        except Exception:
            pass
        raise RuntimeError(f"Azure token request failed (HTTP {e.code}): {detail or e.reason}")
    except Exception as e:
        raise RuntimeError(f"Azure token request failed: {type(e).__name__}: {str(e)[:200]}")
    token = body.get("access_token")
    if not token:
        raise RuntimeError("Azure returned no access_token.")
    return token


def _run_jsign(path: Path, cfg: dict, token: str) -> None:
    """Invoke jsign; raise RuntimeError with stderr on failure."""
    if not JSIGN_JAR.exists():
        raise RuntimeError(f"jsign jar not found at {JSIGN_JAR}.")
    cmd = [
        JAVA, "-jar", str(JSIGN_JAR),
        "--storetype", "TRUSTEDSIGNING",
        "--keystore", _keystore_host(cfg),
        "--storepass", token,
        "--alias", f"{(cfg.get('account_name') or '').strip()}/{(cfg.get('profile_name') or '').strip()}",
        "--tsaurl", TSA_URL,
        "--replace",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "jsign failed").strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "jsign failed")


def test(cfg: dict) -> dict:
    """Validate config end-to-end short of signing: token + jsign present."""
    if (cfg.get("provider") or "azure_trusted_signing") != "azure_trusted_signing":
        return {"ok": False, "error": f"Unknown signing provider '{cfg.get('provider')}'."}
    missing = _missing_fields(cfg)
    if missing:
        return {"ok": False, "error": "Missing: " + ", ".join(missing) + "."}
    if not JSIGN_JAR.exists():
        return {"ok": False, "error": f"jsign jar not found at {JSIGN_JAR} — install it on the server."}
    try:
        get_token(cfg)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "error": None}


def sign(archive: str | Path, cfg: dict | None = None) -> dict:
    """Sign one installer in place. Returns {ok, error, sha256}."""
    cfg = cfg if cfg is not None else get_config()
    path = Path(archive)
    if not path.exists():
        return {"ok": False, "error": "Installer file not found.", "sha256": None}
    missing = _missing_fields(cfg)
    if missing:
        return {"ok": False, "error": "Signing config incomplete: " + ", ".join(missing) + ".", "sha256": None}
    try:
        token = get_token(cfg)
        _run_jsign(path, cfg, token)
    except Exception as e:
        return {"ok": False, "error": str(e)[:400], "sha256": None}
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"ok": True, "error": None, "sha256": sha}
