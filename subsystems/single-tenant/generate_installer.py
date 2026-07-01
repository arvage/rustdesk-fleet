"""
generate_installer.py — build a pre-configured RustDesk installer for any
supported platform.

Reads server config (host, pubkey) and group settings from the fleet DB,
substitutes placeholders in the appropriate template, and either compiles
with makensis (Windows) or writes a shell script (Linux / macOS).

Usage:
    python3 generate_installer.py build --group govirtual365-internal --platform windows-x64
    python3 generate_installer.py build --group govirtual365-internal --platform linux
    python3 generate_installer.py list
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

FLEET_ROOT  = Path("/opt/rustdesk-fleet")
DB_PATH     = FLEET_ROOT / "fleet.sqlite3"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
ASSETS_DIR  = FLEET_ROOT / "installer-assets"
OUTPUT_DIR  = FLEET_ROOT / "installers"
TMPL_DIR    = Path(__file__).parent

RUSTDESK_VERSION = "1.4.8"

PLATFORMS: dict[str, dict] = {
    "windows-x64": {
        "label":         "Windows x64",
        "type":          "nsis",
        "exe_name":      f"rustdesk-{RUSTDESK_VERSION}-x86_64.exe",
        "output_suffix": f"x64.exe",
    },
    "windows-arm64": {
        "label":         "Windows ARM64",
        "type":          "nsis",
        "exe_name":      f"rustdesk-{RUSTDESK_VERSION}-aarch64.exe",
        "output_suffix": f"arm64.exe",
    },
    "linux": {
        "label":         "Linux",
        "type":          "script",
        "template":      "installer_linux.sh.tmpl",
        "output_suffix": "linux.sh",
    },
    "macos": {
        "label":         "macOS",
        "type":          "script",
        "template":      "installer_macos.command.tmpl",
        "output_suffix": "macos.command",
    },
}


class InstallerError(RuntimeError):
    pass


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def log_event(conn: sqlite3.Connection, event: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO provisioning_events (event, detail) VALUES (?, ?)", (event, detail)
    )
    conn.commit()


def _shell_password_substitutions(pw: str | None) -> dict[str, str]:
    """Return shell password placeholder replacements.

    @@PASSWORD_VAR@@ — declares PW variable at top of script.
    @@PASSWORD_CONFIG_WRITE_SHELL@@ — writes RustDesk.toml with password = "..."
        inside write_config(). Must be the Config struct's top-level "password"
        field (not permanent-password under [options], which is Config2 and
        has no effect on authentication).
    """
    if pw:
        return {
            "@@PASSWORD_VAR@@": f'PW="{pw}"',
            "@@PASSWORD_CONFIG_WRITE_SHELL@@": (
                f'  printf \'password = "%s"\\n\' "$PW" > "$1/RustDesk.toml"\n'
            ),
        }
    return {
        "@@PASSWORD_VAR@@": "",
        "@@PASSWORD_CONFIG_WRITE_SHELL@@": "",
    }


def _build_nsis(
    conn: sqlite3.Connection,
    group: sqlite3.Row,
    server: sqlite3.Row,
    platform: str,
    installer_id: int,
) -> Path:
    cfg = PLATFORMS[platform]

    if not shutil.which("makensis"):
        raise InstallerError("makensis not found — install nsis: sudo apt install nsis")

    rustdesk_exe_src = ASSETS_DIR / cfg["exe_name"]
    if not rustdesk_exe_src.exists():
        raise InstallerError(
            f"RustDesk source binary not found: {rustdesk_exe_src}\n"
            f"Download it from https://github.com/rustdesk/rustdesk/releases/tag/{RUSTDESK_VERSION}"
        )

    output_filename = f"RemoteSupport-{group['slug']}-{RUSTDESK_VERSION}-{cfg['output_suffix']}"
    output_path = OUTPUT_DIR / output_filename

    pw = group["unattended_password"] if "unattended_password" in group.keys() else None

    # PRE-WRITE block: write RustDesk.toml (Config struct "password" field) to all
    # three profile locations before the RustDesk installer runs.  The service reads
    # this at startup and builds its stable identity (id, key_pair, salt) around it.
    # Post-write deliberately omits this file to preserve that generated identity.
    password_pre_write_block = ""
    if pw:
        password_pre_write_block = (
            f'  SetShellVarContext current\n'
            f'  CreateDirectory "$APPDATA\\RustDesk\\config"\n'
            f'  FileOpen $R0 "$APPDATA\\RustDesk\\config\\RustDesk.toml" w\n'
            f'  FileWrite $R0 \'password = "{pw}"$\\r$\\n\'\n'
            f'  FileClose $R0\n'
            f'  SetShellVarContext all\n'
            f'  CreateDirectory "$APPDATA\\RustDesk\\config"\n'
            f'  FileOpen $R0 "$APPDATA\\RustDesk\\config\\RustDesk.toml" w\n'
            f'  FileWrite $R0 \'password = "{pw}"$\\r$\\n\'\n'
            f'  FileClose $R0\n'
            f'  SetShellVarContext current\n'
            f'  ${{DisableX64FSRedirection}}\n'
            f'  CreateDirectory "$WINDIR\\System32\\config\\systemprofile\\AppData\\Roaming\\RustDesk\\config"\n'
            f'  FileOpen $R0 "$WINDIR\\System32\\config\\systemprofile\\AppData\\Roaming\\RustDesk\\config\\RustDesk.toml" w\n'
            f'  FileWrite $R0 \'password = "{pw}"$\\r$\\n\'\n'
            f'  FileClose $R0\n'
            f'  ${{EnableX64FSRedirection}}\n'
        )

    # After service starts, call --password via CLI (Sleep 3000 in template gives
    # the service IPC pipe time to initialize) to upgrade plaintext to hash+salt.
    password_cli_nsis = (
        f'  nsExec::ExecToLog \'"$PROGRAMFILES64\\RustDesk\\rustdesk.exe" --password "{pw}"\'\n  Pop $0\n\n'
        if pw else ""
    )

    nsi_script = (TMPL_DIR / "installer.nsi.tmpl").read_text()
    for marker, value in {
        "@@DISPLAY_NAME@@":             group["display_name"],
        "@@OUTPUT_PATH@@":              str(output_path),
        "@@RUSTDESK_EXE_SRC@@":         str(rustdesk_exe_src),
        "@@RUSTDESK_EXE_NAME@@":        cfg["exe_name"],
        "@@HOST@@":                     server["host"],
        "@@PUBKEY@@":                   server["pubkey"],
        "@@PASSWORD_PRE_WRITE_BLOCK@@": password_pre_write_block,
        "@@PASSWORD_CLI_NSIS@@":        password_cli_nsis,
    }.items():
        nsi_script = nsi_script.replace(marker, value)

    with tempfile.TemporaryDirectory() as tmpdir:
        nsi_path = Path(tmpdir) / "installer.nsi"
        nsi_path.write_text(nsi_script)

        result = subprocess.run(
            ["makensis", str(nsi_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise InstallerError(
                f"makensis failed (exit {result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )

    if not output_path.exists():
        raise InstallerError(f"makensis exited 0 but output file not found: {output_path}")

    return output_path


def _build_script(
    conn: sqlite3.Connection,
    group: sqlite3.Row,
    server: sqlite3.Row,
    platform: str,
    installer_id: int,
) -> Path:
    cfg = PLATFORMS[platform]

    output_filename = f"RemoteSupport-{group['slug']}-{RUSTDESK_VERSION}-{cfg['output_suffix']}"
    output_path = OUTPUT_DIR / output_filename

    pw = group["unattended_password"] if "unattended_password" in group.keys() else None

    template = (TMPL_DIR / cfg["template"]).read_text()
    substitutions = {
        "@@DISPLAY_NAME@@":    group["display_name"],
        "@@GROUP_SLUG@@":      group["slug"],
        "@@RUSTDESK_VERSION@@": RUSTDESK_VERSION,
        "@@HOST@@":            server["host"],
        "@@PUBKEY@@":          server["pubkey"],
        **_shell_password_substitutions(pw),
    }
    for marker, value in substitutions.items():
        template = template.replace(marker, value)

    output_path.write_text(template)
    output_path.chmod(0o755)

    return output_path


def build_installer(group_slug: str, platform: str = "windows-x64") -> dict:
    if platform not in PLATFORMS:
        raise InstallerError(
            f"Unknown platform '{platform}'. Valid options: {', '.join(PLATFORMS)}"
        )

    conn = get_db()
    ensure_schema(conn)

    server = conn.execute("SELECT * FROM server_config WHERE id = 1").fetchone()
    if server is None or server["status"] != "active":
        raise InstallerError("Server not active. Run setup_server.py init first.")

    group = conn.execute(
        "SELECT * FROM client_groups WHERE slug = ?", (group_slug,)
    ).fetchone()
    if group is None:
        raise InstallerError(f"Client group '{group_slug}' not found.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cur = conn.execute(
        """INSERT INTO installers (group_id, platform, rustdesk_version, status)
           VALUES (?, ?, ?, 'pending')""",
        (group["id"], platform, RUSTDESK_VERSION),
    )
    installer_id = cur.lastrowid
    conn.commit()
    log_event(conn, "installer_build_start", f"group={group_slug} platform={platform} installer_id={installer_id}")

    try:
        cfg = PLATFORMS[platform]
        if cfg["type"] == "nsis":
            output_path = _build_nsis(conn, group, server, platform, installer_id)
        else:
            output_path = _build_script(conn, group, server, platform, installer_id)

        sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()

        conn.execute(
            """UPDATE installers
               SET status='built', unsigned_path=?, sha256_unsigned=?
               WHERE id=?""",
            (str(output_path), sha256, installer_id),
        )
        conn.commit()
        log_event(conn, "installer_built", f"installer_id={installer_id} sha256={sha256[:16]}...")

    except Exception as e:
        conn.execute(
            "UPDATE installers SET status='failed', error_message=? WHERE id=?",
            (str(e), installer_id),
        )
        conn.commit()
        log_event(conn, "installer_build_failed", str(e)[:500])
        raise

    return dict(conn.execute("SELECT * FROM installers WHERE id=?", (installer_id,)).fetchone())


def list_installers() -> list:
    conn = get_db()
    ensure_schema(conn)
    return conn.execute(
        """SELECT i.*, cg.slug AS group_slug, cg.display_name
           FROM installers i
           JOIN client_groups cg ON i.group_id = cg.id
           ORDER BY i.created_at DESC"""
    ).fetchall()


def main():
    parser = argparse.ArgumentParser(description="RustDesk installer generator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Build a pre-configured installer for a client group")
    p_build.add_argument("--group", required=True, help="Client group slug")
    p_build.add_argument(
        "--platform",
        default="windows-x64",
        choices=list(PLATFORMS),
        help="Target platform (default: windows-x64)",
    )

    sub.add_parser("list", help="List all generated installers")

    args = parser.parse_args()

    if args.cmd == "build":
        try:
            result = build_installer(args.group, args.platform)
        except InstallerError as e:
            sys.exit(f"Build failed: {e}")
        print(f"Installer {result['status']}.")
        print(f"  group:    {args.group}")
        print(f"  platform: {result['platform']}")
        print(f"  version:  {result['rustdesk_version']}")
        print(f"  path:     {result['unsigned_path']}")
        print(f"  sha256:   {result['sha256_unsigned']}")

    elif args.cmd == "list":
        rows = list_installers()
        if not rows:
            print("No installers built yet.")
        for r in rows:
            print(
                f"{r['group_slug']:<28} {r['platform']:<16} "
                f"v{r['rustdesk_version']:<8} {r['status']:<10} "
                f"{r['unsigned_path'] or '-'}"
            )


if __name__ == "__main__":
    main()
