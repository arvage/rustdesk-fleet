#!/usr/bin/env python3
"""
backup.py — snapshot the irreplaceable RustDesk Fleet state.

What it protects (everything small and non-reproducible):
  * every tenant relay's keypair         (id_ed25519[.pub] in its data dir)
  * every tenant relay's peer registry   (its db_v2.sqlite3)
  * the dashboard (control) database     (fleet.sqlite3)
  * each relay's docker-compose.yml, rustdesk_version.txt

Backups are provider-level: one archive covers the control database and every
relay listed in its `relays` table. Tenant #1's relay (the one whose data dir
is <fleet dir>/data) keeps the original layout, rustdesk-fleet/data/ and
rustdesk-fleet/docker-compose.yml, so archives from before tenancy restore
the same way; any other relay goes under rustdesk-fleet/relays/<tenant id>/.

Installer binaries and upstream assets are deliberately excluded — they're
large and can be rebuilt/redownloaded.

Both SQLite databases are live (the containers / dashboard hold them open with
a WAL), so they are copied with SQLite's online-backup API, which yields a
consistent single-file snapshot — never a half-written file.

Usage:
  python3 backup.py run       # create a snapshot (+ rotate, + optional offsite)
  python3 backup.py status    # print the last-run status JSON
  python3 backup.py list      # list retained archives

Configuration (env, e.g. from /etc/rustdesk-fleet/backup.env):
  BACKUP_RETENTION     how many archives to keep locally  (default 14)
  BACKUP_PASSPHRASE    if set, encrypt archives with gpg (AES-256) — needed
                       before shipping offsite
  BACKUP_OFFSITE_CMD   shell command to copy an archive offsite; "{path}" is
                       substituted with the archive path, e.g.
                         rclone copy "{path}" fleet-s3:rustdesk-backups
                         aws s3 cp "{path}" s3://my-bucket/rustdesk/
                       (admin-configured, server-side; runs via the shell)

This module is standalone (standard library only) so the systemd timer can run
it without the dashboard's dependencies.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FLEET_DIR = Path(os.environ.get("FLEET_DATA_DIR") or os.environ.get("FLEET_DIR") or "/opt/rustdesk-fleet")
DATA_DIR = FLEET_DIR / "data"
FLEET_DB = FLEET_DIR / "fleet.sqlite3"
HBBS_DB = DATA_DIR / "db_v2.sqlite3"
BACKUP_DIR = FLEET_DIR / "backups"
STATUS_FILE = BACKUP_DIR / "last_backup.json"

_PREFIX = "rustdesk-fleet-"


def _relays(db: Path = FLEET_DB) -> list[dict]:
    """Every relay to back up: {tenant_id, data_dir, hbbs_db, compose, arc},
    where arc is its folder inside the archive ("" = the original layout).
    Falls back to the single original relay if the database has no relays."""
    rows: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relays'").fetchone():
            rows = [dict(r) for r in conn.execute(
                "SELECT tenant_id, data_dir, hbbs_db_path, compose_path FROM relays ORDER BY tenant_id")]
        conn.close()
    except Exception:
        rows = []
    out = []
    for r in rows:
        data_dir = Path(r["data_dir"])
        legacy = data_dir.resolve() == DATA_DIR.resolve()
        out.append({
            "tenant_id": r["tenant_id"], "data_dir": data_dir,
            "hbbs_db": Path(r["hbbs_db_path"] or data_dir / "db_v2.sqlite3"),
            "compose": Path(r["compose_path"]) if r["compose_path"] else None,
            "arc": "" if legacy else f"relays/{int(r['tenant_id'])}",
        })
    if not any(r["arc"] == "" for r in out):
        out.insert(0, {"tenant_id": 1, "data_dir": DATA_DIR, "hbbs_db": HBBS_DB,
                       "compose": FLEET_DIR / "docker-compose.yml", "arc": ""})
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def _copy_into(src: Path, dst: Path, skipped: list) -> None:
    """Copy a file or tree, recording (not raising on) unreadable items — so a
    root-only file like the container's RustDesk.toml doesn't fail an
    unprivileged (dashboard-triggered) backup of everything else."""
    try:
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for child in src.iterdir():
                _copy_into(child, dst / child.name, skipped)
        else:
            shutil.copy2(src, dst)
    except (PermissionError, OSError) as e:
        skipped.append({"path": str(src), "error": str(e)[:200]})


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite DB via the online-backup API."""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    dst_conn = sqlite3.connect(str(dst))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def _log_event(event: str, detail: str) -> None:
    """Best-effort audit entry in the dashboard DB (shows under Logs)."""
    try:
        conn = sqlite3.connect(str(FLEET_DB), timeout=10)
        conn.execute(
            "INSERT INTO provisioning_events (event, detail) VALUES (?, ?)",
            (event, detail),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _write_status(status: dict) -> None:
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(status, indent=2))
    except Exception:
        pass


def _rotate(retention: int) -> int:
    archives = sorted(
        [p for p in BACKUP_DIR.glob(f"{_PREFIX}*") if p.is_file()],
        key=lambda p: p.name,
        reverse=True,
    )
    for old in archives[retention:]:
        try:
            old.unlink()
        except Exception:
            pass
    return min(len(archives), retention)


def _encrypt(archive: Path, passphrase: str) -> Path:
    """gpg symmetric AES-256 → <archive>.gpg; removes the plaintext on success."""
    enc = archive.with_suffix(archive.suffix + ".gpg")
    # Passphrase on stdin, not argv, so it never shows up in `ps` or /proc.
    subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase-fd", "0", "--cipher-algo", "AES256",
         "--symmetric", "--output", str(enc), str(archive)],
        input=passphrase.encode(), check=True, capture_output=True,
    )
    archive.unlink(missing_ok=True)
    return enc


def _offsite(archive: Path, cmd_tmpl: str) -> dict:
    try:
        cmd = cmd_tmpl.replace("{path}", str(archive))
        subprocess.run(cmd, shell=True, check=True, capture_output=True, timeout=600)
        return {"attempted": True, "ok": True, "error": None}
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace")[:400] or f"exit {e.returncode}"
        return {"attempted": True, "ok": False, "error": err}
    except Exception as e:
        return {"attempted": True, "ok": False, "error": str(e)[:400]}


def run(
    retention: int | None = None,
    passphrase: str | None = None,
    offsite_cmd: str | None = None,
    remote: bool = True,
) -> dict:
    """Create one backup archive. Returns the status dict (also persisted)."""
    # Destination/encryption config from the dashboard DB (falls back to env).
    try:
        import backup_remote
        cfg = backup_remote.get_config()
    except Exception:
        backup_remote, cfg = None, {}

    if retention is None:
        retention = int(cfg.get("retention") or os.environ.get("BACKUP_RETENTION", "14"))
    if passphrase is None:
        passphrase = cfg.get("passphrase") or os.environ.get("BACKUP_PASSPHRASE") or ""
    if offsite_cmd is None:
        offsite_cmd = os.environ.get("BACKUP_OFFSITE_CMD") or ""

    use_remote = bool(remote and backup_remote and cfg.get("enabled"))

    started = _now()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # Microsecond suffix keeps filenames unique even for two runs in one second
    # (e.g. the safety snapshot a restore takes right before restoring).
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    archive = BACKUP_DIR / f"{_PREFIX}{ts}.tar.gz"

    skipped: list = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "rustdesk-fleet"
            (stage / "data").mkdir(parents=True)

            # Live control DB → consistent snapshot.
            if FLEET_DB.exists():
                _sqlite_snapshot(FLEET_DB, stage / "fleet.sqlite3")

            for relay in _relays():
                base = stage / relay["arc"] if relay["arc"] else stage
                (base / "data").mkdir(parents=True, exist_ok=True)
                hbbs_db, data_dir = relay["hbbs_db"], relay["data_dir"]
                if hbbs_db.exists():
                    _sqlite_snapshot(hbbs_db, base / "data" / "db_v2.sqlite3")
                # Keypair + anything else in the data dir that isn't the DB/WAL/SHM.
                if data_dir.exists():
                    for item in data_dir.iterdir():
                        if item.name.startswith(hbbs_db.name):
                            continue  # snapshotted above; skip raw file + -wal/-shm
                        _copy_into(item, base / "data" / item.name, skipped)
                if relay["compose"] and relay["compose"].exists():
                    _copy_into(relay["compose"], base / "docker-compose.yml", skipped)
                # The keypair is the one thing that can't be regenerated — never
                # ship a "successful" backup that silently lost one.
                if (data_dir / "id_ed25519").exists() and not (base / "data" / "id_ed25519").exists():
                    raise RuntimeError(
                        f"keypair id_ed25519 of tenant {relay['tenant_id']}'s relay could not be read — backup aborted")

            src = FLEET_DIR / "rustdesk_version.txt"
            if src.exists():
                _copy_into(src, stage / "rustdesk_version.txt", skipped)

            with tarfile.open(archive, "w:gz") as tar:
                tar.add(stage, arcname="rustdesk-fleet")

        encrypted = False
        if passphrase:
            archive = _encrypt(archive, passphrase)
            encrypted = True

        size = archive.stat().st_size
        if use_remote:
            offsite = backup_remote.ship(archive, cfg)
            # Prune off-site copies only after a successful upload, so a failing
            # destination never loses its older good archives.
            if offsite["ok"]:
                pr = backup_remote.prune(cfg)
                offsite.update(kept=pr["kept"], pruned=pr["deleted"], prune_error=pr["error"])
        elif offsite_cmd:
            offsite = _offsite(archive, offsite_cmd)
        else:
            offsite = {"attempted": False, "ok": False, "error": None}
        kept = _rotate(retention)

        status = {
            "ok": True,
            "at": started,
            "file": archive.name,
            "size_bytes": size,
            "size_human": _human(size),
            "encrypted": encrypted,
            "offsite": offsite,
            "retained": kept,
            "skipped": skipped,
            "error": None,
        }
        detail = f"{archive.name} ({status['size_human']}" + (
            ", encrypted" if encrypted else "") + (
            ", offsite ok" if offsite["ok"] else ", offsite FAILED" if offsite["attempted"] else "") + (
            f", {offsite['pruned']} old offsite removed" if offsite.get("pruned") else "") + (
            ", offsite prune FAILED" if offsite.get("prune_error") else "") + (
            f", {len(skipped)} unreadable skipped" if skipped else "") + ")"
        _log_event("backup_succeeded", detail)
        _write_status(status)
        return status

    except Exception as e:
        archive.unlink(missing_ok=True)
        status = {
            "ok": False, "at": started, "file": None, "size_bytes": 0,
            "size_human": "—", "encrypted": False,
            "offsite": {"attempted": False, "ok": False, "error": None},
            "retained": None, "error": str(e)[:400],
        }
        _log_event("backup_failed", str(e)[:400])
        _write_status(status)
        return status


def _decrypt(enc: Path, passphrase: str, out: Path) -> None:
    subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase-fd", "0", "--decrypt", "--output", str(out), str(enc)],
        input=passphrase.encode(), check=True, capture_output=True,
    )


def _replace_file(src: Path, dst: Path) -> None:
    """Replace dst with src even if dst is a root-owned file in a dir we own
    (unlink in the owned dir, then write a fresh copy)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst.exists():
            dst.unlink()
    except Exception:
        pass
    shutil.copy2(src, dst)


def restore(archive: str | Path, passphrase: str | None = None) -> dict:
    """Restore the keypairs + databases from an archive.

    Takes a safety backup first, then replaces each relay's keypair and hbbs
    peer DB on disk (the caller should restart the relays afterwards so they
    reopen them) and restores the dashboard DB in place via SQLite. Returns a
    status dict.
    """
    archive = Path(archive)
    if not archive.exists():
        return {"ok": False, "error": "Archive not found.", "applied": [], "safety": None}
    if passphrase is None:
        try:
            import backup_remote
            passphrase = backup_remote.get_config().get("passphrase") or os.environ.get("BACKUP_PASSPHRASE") or ""
        except Exception:
            passphrase = os.environ.get("BACKUP_PASSPHRASE") or ""

    # Stage a private copy first: the safety snapshot below rotates local
    # archives and could otherwise delete the very archive being restored.
    stage_dir = Path(tempfile.mkdtemp(prefix="rdf-restore-"))
    staged = stage_dir / archive.name
    shutil.copy2(archive, staged)

    # Always snapshot current state before overwriting anything. Local only:
    # never upload (or prune off-site copies) in the middle of a restore.
    safety = run(offsite_cmd="", remote=False)
    applied: list[str] = []

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            tarball = staged
            if staged.name.endswith(".gpg"):
                if not passphrase:
                    return {"ok": False, "error": "Archive is encrypted but no passphrase is configured.",
                            "applied": [], "safety": safety.get("file")}
                tarball = tmpd / "decrypted.tar.gz"
                _decrypt(staged, passphrase, tarball)

            extract = tmpd / "x"
            extract.mkdir()
            with tarfile.open(tarball, "r:gz") as tar:
                tar.extractall(extract)   # our own archives, created by this tool
            root = extract / "rustdesk-fleet"
            if not root.is_dir():
                return {"ok": False, "error": "Archive does not contain a rustdesk-fleet/ root.",
                        "applied": [], "safety": safety.get("file")}

            has_key = (root / "data" / "id_ed25519").exists() or any((root / "relays").glob("*/data/id_ed25519"))
            has_fleet = (root / "fleet.sqlite3").exists()
            if not (has_key or has_fleet):
                return {"ok": False, "error": "Archive has neither the keypair nor the dashboard DB.",
                        "applied": [], "safety": safety.get("file")}

            # Where each relay's files go back: the relays as the archive's own
            # database describes them (the state being restored).
            targets = {r["arc"]: r for r in _relays(root / "fleet.sqlite3" if has_fleet else FLEET_DB)}
            arcs = [""] + sorted(f"relays/{d.name}" for d in (root / "relays").glob("*") if d.is_dir())
            for arc in arcs:
                src_base = root / arc if arc else root
                relay = targets.get(arc)
                if relay is None or not (src_base / "data").is_dir():
                    continue
                _restore_relay(src_base, relay, applied)

            # Original single-relay archives have docker-compose.yml at the root.
            src_compose = root / "docker-compose.yml"
            legacy = targets.get("")
            if src_compose.exists() and legacy and legacy["compose"]:
                _replace_file(src_compose, legacy["compose"])
                applied.append("docker-compose.yml")

            # Dashboard DB: restore in place via SQLite so open connections stay valid.
            src_fleet = root / "fleet.sqlite3"
            if src_fleet.exists():
                src_conn = sqlite3.connect(f"file:{src_fleet}?mode=ro", uri=True, timeout=15)
                dst_conn = sqlite3.connect(str(FLEET_DB), timeout=15)
                try:
                    with dst_conn:
                        src_conn.backup(dst_conn)
                finally:
                    src_conn.close()
                    dst_conn.close()
                applied.append("fleet.sqlite3")

            src = root / "rustdesk_version.txt"
            if src.exists():
                _replace_file(src, FLEET_DIR / "rustdesk_version.txt")
                applied.append("rustdesk_version.txt")

        _log_event("backup_restored", f"from {archive.name}: {', '.join(applied)}")
        return {"ok": True, "error": None, "applied": applied, "safety": safety.get("file")}

    except Exception as e:
        _log_event("backup_restore_failed", f"{archive.name}: {str(e)[:300]}")
        return {"ok": False, "error": str(e)[:400], "applied": applied, "safety": safety.get("file")}
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _restore_relay(src_base: Path, relay: dict, applied: list[str]) -> None:
    """Put one relay's keypair, other data files and hbbs peer DB back."""
    src_data = src_base / "data"
    data_dir, hbbs_db = relay["data_dir"], relay["hbbs_db"]
    label = f"{relay['arc']}/" if relay["arc"] else ""
    data_dir.mkdir(parents=True, exist_ok=True)
    # Keypair + any other data files (not the hbbs DB, handled below).
    for item in src_data.rglob("*"):
        if item.is_dir() or item.name.startswith("db_v2.sqlite3"):
            continue
        rel = item.relative_to(src_data)
        _replace_file(item, data_dir / rel)
        applied.append(f"{label}data/{rel}")
    # hbbs peer DB: drop the live file + WAL/SHM, write the restored copy.
    src_hbbs = src_data / "db_v2.sqlite3"
    if src_hbbs.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(hbbs_db) + suffix)
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        shutil.copy2(src_hbbs, hbbs_db)
        applied.append(f"{label}data/db_v2.sqlite3")
    if relay["arc"] and (src_base / "docker-compose.yml").exists() and relay["compose"]:
        _replace_file(src_base / "docker-compose.yml", relay["compose"])
        applied.append(f"{label}docker-compose.yml")


def list_archives() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob(f"{_PREFIX}*"), key=lambda p: p.name, reverse=True):
        if p.is_file():
            st = p.stat()
            out.append({
                "file": p.name,
                "size_bytes": st.st_size,
                "size_human": _human(st.st_size),
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "encrypted": p.name.endswith(".gpg"),
            })
    return out


def status() -> dict | None:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return None


def latest_archive() -> Path | None:
    archives = sorted(
        [p for p in BACKUP_DIR.glob(f"{_PREFIX}*") if p.is_file()],
        key=lambda p: p.name, reverse=True,
    ) if BACKUP_DIR.exists() else []
    return archives[0] if archives else None


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        st = run()
        print(json.dumps(st, indent=2))
        return 0 if st["ok"] else 1
    if cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if cmd == "list":
        for a in list_archives():
            print(f"{a['modified']}  {a['size_human']:>8}  {a['file']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
