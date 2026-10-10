#!/usr/bin/env python3
"""
backup_remote.py — ship a backup archive to an off-site destination.

Two backends cover the common providers:

  * provider "s3"     — AWS S3, Wasabi, Backblaze B2, DigitalOcean Spaces,
                        MinIO, or any S3-compatible store. Native (boto3),
                        configured entirely from access keys — no OAuth.
  * provider "rclone" — Google Drive, OneDrive, Dropbox, etc. Uses an rclone
                        remote the admin sets up once on the box
                        (`rclone config`), referenced as "remote:path".

Configuration lives in the dashboard DB (backup_config, one row). Standalone
(stdlib + boto3) so the systemd backup timer can use it without the dashboard.
"""

from __future__ import annotations

import os
import subprocess
import sqlite3
from pathlib import Path

FLEET_DIR = Path(os.environ.get("FLEET_DIR", "/opt/rustdesk-fleet"))
FLEET_DB = FLEET_DIR / "fleet.sqlite3"

# Endpoint presets for the S3-compatible providers that use a fixed host.
# "aws" uses region-derived endpoints (left blank); "custom" is whatever the
# admin types. {region} is filled from the configured region.
S3_ENDPOINTS = {
    "aws": "",
    "wasabi": "https://s3.{region}.wasabisys.com",
    "b2": "https://s3.{region}.backblazeb2.com",
    "spaces": "https://{region}.digitaloceanspaces.com",
    "minio": "",
    "custom": "",
}

_DEFAULTS = {
    "enabled": 0, "provider": "s3", "s3_provider": "aws", "s3_endpoint": "",
    "s3_region": "", "s3_bucket": "", "s3_prefix": "", "s3_access_key": "",
    "s3_secret_key": "", "rclone_remote": "", "passphrase": "", "retention": 14,
}


def get_config() -> dict:
    try:
        conn = sqlite3.connect(f"file:{FLEET_DB}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM backup_config WHERE id = 1").fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception:
        pass
    return dict(_DEFAULTS)


def offsite_summary(cfg: dict | None = None) -> dict:
    """What the saved config means for the UI: is off-site on, and is it usable?"""
    cfg = cfg if cfg is not None else get_config()
    provider = cfg.get("provider") or "s3"
    if provider == "rclone":
        configured = ":" in (cfg.get("rclone_remote") or "")
        target = (cfg.get("rclone_remote") or "").strip()
    else:
        configured = bool(cfg.get("s3_bucket") and cfg.get("s3_access_key") and cfg.get("s3_secret_key"))
        target = "s3://" + "/".join(
            p for p in ((cfg.get("s3_bucket") or "").strip(), (cfg.get("s3_prefix") or "").strip().strip("/")) if p)
    return {"enabled": bool(cfg.get("enabled")), "configured": configured, "target": target}


def _resolve_endpoint(cfg: dict) -> str:
    ep = (cfg.get("s3_endpoint") or "").strip()
    if ep:
        return ep
    tmpl = S3_ENDPOINTS.get(cfg.get("s3_provider") or "aws", "")
    if tmpl:
        return tmpl.format(region=(cfg.get("s3_region") or "").strip())
    return ""   # AWS: let boto3 derive from region


def _s3_client(cfg: dict):
    import boto3
    from botocore.config import Config
    kwargs = {
        "aws_access_key_id": cfg["s3_access_key"],
        "aws_secret_access_key": cfg["s3_secret_key"],
        "config": Config(signature_version="s3v4", retries={"max_attempts": 3}),
    }
    region = (cfg.get("s3_region") or "").strip()
    if region:
        kwargs["region_name"] = region
    endpoint = _resolve_endpoint(cfg)
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def _s3_key(cfg: dict, filename: str) -> str:
    prefix = (cfg.get("s3_prefix") or "").strip().strip("/")
    return f"{prefix}/{filename}" if prefix else filename


def _rclone_dest(cfg: dict) -> str:
    return (cfg.get("rclone_remote") or "").strip()


def test(cfg: dict) -> dict:
    """Verify the destination is reachable/writable. Returns {ok, error}."""
    provider = cfg.get("provider") or "s3"
    try:
        if provider == "s3":
            if not cfg.get("s3_bucket"):
                return {"ok": False, "error": "Bucket is required."}
            if not (cfg.get("s3_access_key") and cfg.get("s3_secret_key")):
                return {"ok": False, "error": "Access key and secret are required."}
            client = _s3_client(cfg)
            # list_objects is allowed by more restrictive IAM policies than head_bucket
            client.list_objects_v2(Bucket=cfg["s3_bucket"], MaxKeys=1)
            return {"ok": True, "error": None}
        if provider == "rclone":
            dest = _rclone_dest(cfg)
            if not dest or ":" not in dest:
                return {"ok": False, "error": "rclone remote must look like 'remote:path'."}
            remote = dest.split(":", 1)[0] + ":"
            r = subprocess.run(["rclone", "lsd", remote],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return {"ok": False, "error": (r.stderr or "rclone error").strip()[:400]}
            return {"ok": True, "error": None}
        return {"ok": False, "error": f"Unknown provider '{provider}'."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out reaching the destination."}
    except FileNotFoundError:
        return {"ok": False, "error": "rclone is not installed on the server."}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:360]}"}


def ship(archive: Path, cfg: dict) -> dict:
    """Upload one archive to the configured destination. Returns {attempted, ok, error}."""
    provider = cfg.get("provider") or "s3"
    try:
        if provider == "s3":
            client = _s3_client(cfg)
            client.upload_file(str(archive), cfg["s3_bucket"], _s3_key(cfg, archive.name))
            return {"attempted": True, "ok": True, "error": None}
        if provider == "rclone":
            dest = _rclone_dest(cfg)
            r = subprocess.run(["rclone", "copy", str(archive), dest],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                return {"attempted": True, "ok": False, "error": (r.stderr or "rclone error").strip()[:400]}
            return {"attempted": True, "ok": True, "error": None}
        return {"attempted": True, "ok": False, "error": f"Unknown provider '{provider}'."}
    except FileNotFoundError:
        return {"attempted": True, "ok": False, "error": "rclone is not installed on the server."}
    except Exception as e:
        return {"attempted": True, "ok": False, "error": f"{type(e).__name__}: {str(e)[:360]}"}
