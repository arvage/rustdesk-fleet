"""
generate_installer.py — build a pre-configured RustDesk installer for any
supported platform.

Reads the tenant's relay (host, pubkey) and group settings from the fleet DB,
substitutes placeholders in the appropriate template, and either compiles
with makensis (Windows) or writes a shell script (Linux / macOS).

The RustDesk binary version is pinned in `rustdesk_version.txt` (next to
fleet.sqlite3) rather than hardcoded, so builds stay reproducible even as
new upstream releases come out. Run `update-version` to check GitHub for a
newer release and pull down its binaries — nothing changes until you do.

Installers always point at the relay host (relays.host), never at one of the
tenant's web domains, so a tenant changing its domain never strands devices.

Usage:
    python3 generate_installer.py build --group acme-internal --platform windows-x64
    python3 generate_installer.py build --tenant 2 --group acme --platform linux
    python3 generate_installer.py list
    python3 generate_installer.py update-version
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

FLEET_ROOT   = Path(os.environ.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet")
DB_PATH      = FLEET_ROOT / "fleet.sqlite3"
SCHEMA_PATH  = Path(__file__).parent / "schema.sql"
ASSETS_DIR   = FLEET_ROOT / "installer-assets"   # provider-level: one client version for everyone
TMPL_DIR     = Path(__file__).parent
VERSION_FILE = FLEET_ROOT / "rustdesk_version.txt"

# Only used to seed VERSION_FILE the first time it's read — after that the
# file on disk is the source of truth.
DEFAULT_VERSION = "1.4.9"

GITHUB_REPO = "rustdesk/rustdesk"
USER_AGENT = "rustdesk-fleet-installer-generator"


def get_pinned_version() -> str:
    if VERSION_FILE.exists():
        pinned = VERSION_FILE.read_text().strip()
        if pinned:
            return pinned
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(DEFAULT_VERSION + "\n")
    return DEFAULT_VERSION


# Read the pin at build time (not import time): the dashboard imports this
# module once and stays running, so an import-time constant would keep
# building the old version after `update_version()` moves the pin.

PLATFORMS: dict[str, dict] = {
    "windows-x64": {
        "label":         "Windows x64",
        "type":          "nsis",
        "arch":          "x86_64",
        "output_suffix": f"x64.exe",
    },
    "windows-arm64": {
        "label":         "Windows ARM64",
        "type":          "nsis",
        "arch":          "aarch64",
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


def fetch_latest_release_tag() -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return data["tag_name"].lstrip("v")


_latest_client_cache: dict = {"version": None, "checked_at": 0.0}
_LATEST_CLIENT_TTL_S = 3600


def get_latest_client_version() -> str | None:
    """Latest RustDesk client release from GitHub, cached for an hour.

    Best-effort: returns the last known value (or None) on any failure
    instead of raising, so it can't break the dashboard's status page.
    """
    now = time.time()
    if _latest_client_cache["version"] and now - _latest_client_cache["checked_at"] < _LATEST_CLIENT_TTL_S:
        return _latest_client_cache["version"]
    try:
        tag = fetch_latest_release_tag()
    except Exception:
        return _latest_client_cache["version"]
    _latest_client_cache.update(version=tag, checked_at=now)
    return tag


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def download_asset(version: str, arch: str) -> Path:
    """Download rustdesk-{version}-{arch}.exe to a temp file inside ASSETS_DIR
    and return its path, leaving the final filename untouched until the
    caller commits it. Raises on any HTTP/network failure."""
    filename = f"rustdesk-{version}-{arch}.exe"
    url = f"https://github.com/{GITHUB_REPO}/releases/download/{version}/{filename}"
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = ASSETS_DIR / f".{filename}.part"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    return tmp_path


def update_version() -> dict:
    """Check GitHub for the latest RustDesk release. If it's newer than the
    pinned version, download binaries for whichever architectures are
    already present locally (so it never fetches a platform you don't use),
    then move the pin forward. Returns a summary dict; raises InstallerError
    on any failure, leaving the existing pin/assets untouched."""
    current = get_pinned_version()

    try:
        latest = fetch_latest_release_tag()
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
        raise InstallerError(f"Could not check latest RustDesk release: {e}")

    if latest == current:
        return {"current": current, "latest": latest, "updated": False, "archs": []}

    # Fetch every architecture we've ever staged (any version), not just the
    # current one's — otherwise an arch that lagged a release behind (e.g.
    # aarch64 still on an older version) would never be updated again.
    known = {a for a in (p.get("arch") for p in PLATFORMS.values()) if a}
    archs = sorted({
        arch for p in ASSETS_DIR.glob("rustdesk-*-*.exe")
        for arch in known if p.name.endswith(f"-{arch}.exe")
    })
    if not archs:
        raise InstallerError(
            f"No existing rustdesk-*.exe assets found in {ASSETS_DIR} "
            "to know which architectures to fetch."
        )

    downloaded: list[tuple[str, Path, Path]] = []
    try:
        for arch in archs:
            tmp_path = download_asset(latest, arch)
            final_path = ASSETS_DIR / f"rustdesk-{latest}-{arch}.exe"
            downloaded.append((arch, tmp_path, final_path))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        for _, tmp_path, _ in downloaded:
            tmp_path.unlink(missing_ok=True)
        raise InstallerError(f"Download failed partway through ({e}); pinned version unchanged.")

    for _, tmp_path, final_path in downloaded:
        tmp_path.replace(final_path)
    VERSION_FILE.write_text(latest + "\n")

    return {
        "current": current,
        "latest": latest,
        "updated": True,
        "archs": [
            {"arch": arch, "path": str(final_path), "sha256": hashlib.sha256(final_path.read_bytes()).hexdigest()}
            for arch, _, final_path in downloaded
        ],
    }


def _group_option_value(group: sqlite3.Row, column: str) -> str:
    """Value for a managed client option in new installers: 'N' only if the
    group explicitly set it off, 'Y' otherwise (incl. not managed).
    Columns: auto_update -> allow-auto-update,
             remote_config -> allow-remote-config-modification."""
    return "N" if column in group.keys() and group[column] == "N" else "Y"


def _strip_port(host: str) -> str:
    """Return the hostname portion of host[:port], stripping any port number."""
    # Handle IPv6 bracket notation [addr]:port
    if host.startswith("["):
        return host.split("]")[0].lstrip("[")
    return host.split(":")[0]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def log_event(conn: sqlite3.Connection, event: str, detail: str = "", user_email: str = "",
              *, tenant_id: int | None) -> None:
    conn.execute(
        "INSERT INTO provisioning_events (tenant_id, event, detail, user_email) VALUES (?, ?, ?, ?)",
        (tenant_id, event, detail, user_email or None),
    )
    conn.commit()


def output_dir(tenant_id: int) -> Path:
    """Where a tenant's built installers go (tenant #1: <data dir>/installers)."""
    from tenant_paths import installers_dir
    return installers_dir(tenant_id)


def get_relay(conn: sqlite3.Connection, tenant_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM relays WHERE tenant_id = ?", (tenant_id,)).fetchone()


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


def _group_branding(group: sqlite3.Row) -> tuple[str | None, Path | None, Path | None]:
    """(app name, .ico, .png) for a group; each None when not set."""
    import branding
    name = (group["brand_app_name"] or "").strip() if "brand_app_name" in group.keys() else ""
    ico, png = branding.icon_paths(group["tenant_id"], group["id"])
    if not branding.has_icon(group["tenant_id"], group["id"]):
        ico = png = None
    return name or None, ico, png


def _vi_version(version: str) -> str:
    """NSIS VIProductVersion needs exactly four numeric parts: 1.5.0 -> 1.5.0.0."""
    parts = [int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", version)[:4]]
    return ".".join(str(p) for p in (parts + [0, 0, 0, 0])[:4])


def _nsis_brand_shortcuts(app_name: str | None, has_icon: bool) -> str:
    """NSIS block replacing RustDesk's desktop + Start menu shortcuts with
    branded ones. Same locations RustDesk uses (src/platform/windows.rs):
    %PUBLIC%\\Desktop and %ProgramData%\\...\\Start Menu\\Programs\\RustDesk.
    Only replaces shortcuts the RustDesk installer actually created.

    The icon lives outside RustDesk's install dir: RustDesk's own updater
    wipes that dir, which would leave a renamed desktop shortcut iconless.
    """
    if not app_name and not has_icon:
        return ""
    name = app_name or "RustDesk"
    if has_icon:
        icon_file = "$PROGRAMFILES64\\RustDesk Branding\\app.ico"
        install_icon = (
            '  CreateDirectory "$PROGRAMFILES64\\RustDesk Branding"\n'
            '  SetOutPath "$PROGRAMFILES64\\RustDesk Branding"\n'
            '  File "/oname=app.ico" "@@BRAND_ICO_SRC@@"\n'
        )
    else:
        icon_file = "$PROGRAMFILES64\\RustDesk\\rustdesk.exe"
        install_icon = ""
    shortcut = (f'"$PROGRAMFILES64\\RustDesk\\rustdesk.exe" "" "{icon_file}" 0 '
                f'SW_SHOWNORMAL "" "{name}"')
    return (
        "  ; ── Step 5: branded shortcuts ────────────────────────────────────────────\n"
        "  SetShellVarContext all\n"
        + install_icon +
        '  ${If} ${FileExists} "$DESKTOP\\RustDesk.lnk"\n'
        '    Delete "$DESKTOP\\RustDesk.lnk"\n'
        f'    CreateShortCut "$DESKTOP\\{name}.lnk" {shortcut}\n'
        "  ${EndIf}\n"
        '  ${If} ${FileExists} "$SMPROGRAMS\\RustDesk\\RustDesk.lnk"\n'
        '    Delete "$SMPROGRAMS\\RustDesk\\RustDesk.lnk"\n'
        f'    CreateShortCut "$SMPROGRAMS\\RustDesk\\{name}.lnk" {shortcut}\n'
        "  ${EndIf}\n"
        "  SetShellVarContext current\n"
    )


def _shell_brand_block(app_name: str | None, png: Path | None) -> str:
    """Shell block installing a branded copy of RustDesk's menu entry.

    Same desktop-file ID (rustdesk.desktop) in /usr/local/share/applications,
    which precedes /usr/share in XDG_DATA_DIRS — so it shadows the package's
    entry and survives package updates. The icon is embedded as base64.
    """
    if not app_name and not png:
        return ""
    import base64
    lines = [
        "# ── Branded menu entry ───────────────────────────────────────────────────",
        "# Shadows the package's rustdesk.desktop (/usr/local/share comes first in",
        "# XDG_DATA_DIRS), so it survives RustDesk package updates.",
        "if [ -f /usr/share/applications/rustdesk.desktop ]; then",
        "  echo \"Applying branding…\"",
        "  mkdir -p /usr/local/share/applications",
    ]
    sed = []
    if app_name:
        # "&" means "the match" in a sed replacement.
        lines.append(f'  BRAND_NAME="{app_name.replace("&", chr(92) + "&")}"')
        sed.append('-e "s|^Name=RustDesk$|Name=${BRAND_NAME}|"')
    if png:
        b64 = base64.encodebytes(png.read_bytes()).decode().rstrip("\n")
        lines += [
            "  mkdir -p /usr/local/share/rustdesk-branding",
            "  base64 -d > /usr/local/share/rustdesk-branding/app.png <<'ICON'",
            b64,
            "ICON",
        ]
        sed.append('-e "s|^Icon=rustdesk$|Icon=/usr/local/share/rustdesk-branding/app.png|"')
    lines += [
        f"  sed {' '.join(sed)} /usr/share/applications/rustdesk.desktop \\",
        "    > /usr/local/share/applications/rustdesk.desktop",
        "  update-desktop-database /usr/local/share/applications 2>/dev/null || true",
        "fi",
        "",
    ]
    return "\n".join(lines) + "\n"


def _build_nsis(
    conn: sqlite3.Connection,
    group: sqlite3.Row,
    server: sqlite3.Row,
    platform: str,
    installer_id: int,
    version: str,
) -> Path:
    cfg = PLATFORMS[platform]
    exe_name = f"rustdesk-{version}-{cfg['arch']}.exe"

    if not shutil.which("makensis"):
        raise InstallerError("makensis not found — install nsis: sudo apt install nsis")

    rustdesk_exe_src = ASSETS_DIR / exe_name
    if not rustdesk_exe_src.exists():
        raise InstallerError(
            f"RustDesk source binary not found: {rustdesk_exe_src}\n"
            f"Download it from https://github.com/rustdesk/rustdesk/releases/tag/{version}"
        )

    output_filename = f"RemoteSupport-{group['slug']}-{version}-{cfg['output_suffix']}"
    output_path = output_dir(group["tenant_id"]) / output_filename

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
            # Service (SYSTEM) profile: RustDesk maps it to ServiceProfiles\LocalService.
            # Fresh installs only — on an upgrade this file holds the device's
            # id + key_pair, and overwriting it would change the device's ID.
            # The --password CLI call after start sets the password either way.
            f'  ${{IfNot}} ${{FileExists}} "${{SVC_CONFIG}}\\RustDesk.toml"\n'
            f'    CreateDirectory "${{SVC_CONFIG}}"\n'
            f'    FileOpen $R0 "${{SVC_CONFIG}}\\RustDesk.toml" w\n'
            f'    FileWrite $R0 \'password = "{pw}"$\\r$\\n\'\n'
            f'    FileClose $R0\n'
            f'  ${{EndIf}}\n'
        )

    # After service starts, call --password via CLI (Sleep 3000 in template gives
    # the service IPC pipe time to initialize) to upgrade plaintext to hash+salt.
    password_cli_nsis = (
        f'  nsExec::ExecToLog \'"$PROGRAMFILES64\\RustDesk\\rustdesk.exe" --password "{pw}"\'\n  Pop $0\n\n'
        if pw else ""
    )

    app_name, brand_ico, _ = _group_branding(group)

    nsi_script = (TMPL_DIR / "installer.nsi.tmpl").read_text()
    for marker, value in {
        # Inserted blocks first: they contain markers of their own.
        "@@BRAND_SHORTCUTS@@":          _nsis_brand_shortcuts(app_name, brand_ico is not None),
        "@@INSTALLER_ICON@@":           f'Icon "{brand_ico}"' if brand_ico else "",
        "@@BRAND_ICO_SRC@@":            str(brand_ico or ""),
        "@@PRODUCT_NAME@@":             app_name or f"{group['display_name']} Remote Support",
        "@@VI_VERSION@@":               _vi_version(version),
        "@@DISPLAY_NAME@@":             group["display_name"],
        "@@OUTPUT_PATH@@":              str(output_path),
        "@@RUSTDESK_EXE_SRC@@":         str(rustdesk_exe_src),
        "@@RUSTDESK_EXE_NAME@@":        exe_name,
        "@@HOST@@":                     _strip_port(server["host"]),
        "@@PUBKEY@@":                   server["pubkey"],
        "@@PASSWORD_PRE_WRITE_BLOCK@@": password_pre_write_block,
        "@@PASSWORD_CLI_NSIS@@":        password_cli_nsis,
        "@@AUTO_UPDATE@@":              _group_option_value(group, "auto_update"),
        "@@REMOTE_CONFIG@@":            _group_option_value(group, "remote_config"),
        "@@RUSTDESK_VERSION@@":         version,
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
    version: str,
) -> Path:
    cfg = PLATFORMS[platform]

    output_filename = f"RemoteSupport-{group['slug']}-{version}-{cfg['output_suffix']}"
    output_path = output_dir(group["tenant_id"]) / output_filename

    pw = group["unattended_password"] if "unattended_password" in group.keys() else None

    # Branding: Linux menu entry only. macOS is left alone — changing the app
    # bundle's icon would break RustDesk's code signature.
    brand_block = ""
    if platform == "linux":
        app_name, _, brand_png = _group_branding(group)
        brand_block = _shell_brand_block(app_name, brand_png)

    template = (TMPL_DIR / cfg["template"]).read_text()
    substitutions = {
        "@@BRAND_DESKTOP_BLOCK@@": brand_block,
        "@@DISPLAY_NAME@@":    group["display_name"],
        "@@GROUP_SLUG@@":      group["slug"],
        "@@RUSTDESK_VERSION@@": version,
        "@@HOST@@":            _strip_port(server["host"]),
        "@@PUBKEY@@":          server["pubkey"],
        "@@AUTO_UPDATE@@":     _group_option_value(group, "auto_update"),
        "@@REMOTE_CONFIG@@":   _group_option_value(group, "remote_config"),
        **_shell_password_substitutions(pw),
    }
    for marker, value in substitutions.items():
        template = template.replace(marker, value)

    output_path.write_text(template)
    output_path.chmod(0o755)

    return output_path


def sign_installer_row(conn, tenant_id: int, installer_id: int, path, scfg: dict, user_email: str = "") -> dict:
    """Sign one installer in place and update its row. Raises on failure.

    jsign signs with --replace, so the signed file keeps the same path; links
    prefer signed_path, so serving picks up the signed build automatically.
    """
    import sign_installer
    conn.execute("UPDATE installers SET status='signing' WHERE id=? AND tenant_id=?", (installer_id, tenant_id))
    conn.commit()
    res = sign_installer.sign(path, scfg)
    if not res["ok"]:
        conn.execute("UPDATE installers SET status='built', error_message=? WHERE id=? AND tenant_id=?",
                     (res["error"], installer_id, tenant_id))
        conn.commit()
        log_event(conn, "installer_sign_failed", f"installer_id={installer_id} {res['error'][:300]}", user_email,
                  tenant_id=tenant_id)
        raise InstallerError(res["error"])
    conn.execute(
        """UPDATE installers
           SET status='signed', signed_path=?, sha256_signed=?, signed_at=datetime('now'),
               error_message=NULL
           WHERE id=? AND tenant_id=?""",
        (str(path), res["sha256"], installer_id, tenant_id),
    )
    conn.commit()
    log_event(conn, "installer_signed", f"installer_id={installer_id} sha256={res['sha256'][:16]}...", user_email,
              tenant_id=tenant_id)
    return dict(conn.execute("SELECT * FROM installers WHERE id=? AND tenant_id=?",
                             (installer_id, tenant_id)).fetchone())


def sign_existing(tenant_id: int, group_slug: str, installer_id: int, user_email: str = "") -> dict:
    """Manual sign/re-sign of one of a group's built installers. Used by the
    dashboard. An installer of another group or tenant is "not found"."""
    import sign_installer
    conn = get_db()
    row = conn.execute(
        """SELECT i.* FROM installers i
           JOIN client_groups cg ON cg.id = i.group_id AND cg.tenant_id = i.tenant_id
           WHERE i.id = ? AND i.tenant_id = ? AND cg.slug = ?""",
        (installer_id, tenant_id, group_slug),
    ).fetchone()
    if row is None:
        raise InstallerError("Installer not found.")
    path = row["signed_path"] or row["unsigned_path"]
    if not path or not Path(path).exists():
        raise InstallerError("Installer file is missing on disk — rebuild it first.")
    scfg = sign_installer.get_config()
    if not scfg.get("enabled"):
        raise InstallerError("Code signing is not enabled. Configure it under Admin → Signing.")
    return sign_installer_row(conn, tenant_id, installer_id, path, scfg, user_email)


def build_installer(tenant_id: int, group_slug: str, platform: str = "windows-x64", user_email: str = "") -> dict:
    if platform not in PLATFORMS:
        raise InstallerError(
            f"Unknown platform '{platform}'. Valid options: {', '.join(PLATFORMS)}"
        )

    conn = get_db()
    ensure_schema(conn)

    server = get_relay(conn, tenant_id)
    if server is None or server["status"] != "active":
        raise InstallerError("Relay not active. Run setup_server.py init first.")

    group = conn.execute(
        "SELECT * FROM client_groups WHERE tenant_id = ? AND slug = ?", (tenant_id, group_slug)
    ).fetchone()
    if group is None:
        raise InstallerError(f"Client group '{group_slug}' not found.")
    # Groups saved before the dashboard validated passwords could still hold one
    # that breaks the generated script — refuse rather than build a bad installer.
    from setup_server import password_problem
    problem = password_problem(group["unattended_password"] if "unattended_password" in group.keys() else None)
    if problem:
        raise InstallerError(f"{problem} Change it on the group page, then rebuild.")

    output_dir(tenant_id).mkdir(parents=True, exist_ok=True)
    version = get_pinned_version()

    cur = conn.execute(
        """INSERT INTO installers (tenant_id, group_id, platform, rustdesk_version, status)
           VALUES (?, ?, ?, ?, 'pending')""",
        (tenant_id, group["id"], platform, version),
    )
    installer_id = cur.lastrowid
    conn.commit()
    log_event(conn, "installer_build_start", f"group={group_slug} platform={platform} installer_id={installer_id}",
              user_email, tenant_id=tenant_id)

    try:
        cfg = PLATFORMS[platform]
        if cfg["type"] == "nsis":
            output_path = _build_nsis(conn, group, server, platform, installer_id, version)
        else:
            output_path = _build_script(conn, group, server, platform, installer_id, version)

        sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()

        conn.execute(
            """UPDATE installers
               SET status='built', unsigned_path=?, sha256_unsigned=?
               WHERE id=? AND tenant_id=?""",
            (str(output_path), sha256, installer_id, tenant_id),
        )
        conn.commit()
        log_event(conn, "installer_built", f"installer_id={installer_id} sha256={sha256[:16]}...", user_email,
                  tenant_id=tenant_id)

        # Auto-sign when signing is enabled with auto_sign on. A signing failure
        # must not fail the build — the installer is still usable unsigned — so
        # we log it and leave status='built' for a manual retry.
        try:
            import sign_installer
            scfg = sign_installer.get_config()
            if scfg.get("enabled") and scfg.get("auto_sign"):
                sign_installer_row(conn, tenant_id, installer_id, output_path, scfg, user_email)
        except Exception as e:
            log_event(conn, "installer_sign_failed", f"installer_id={installer_id} {str(e)[:300]}", user_email,
                      tenant_id=tenant_id)

    except Exception as e:
        conn.execute(
            "UPDATE installers SET status='failed', error_message=? WHERE id=? AND tenant_id=?",
            (str(e), installer_id, tenant_id),
        )
        conn.commit()
        log_event(conn, "installer_build_failed", str(e)[:500], user_email, tenant_id=tenant_id)
        raise

    return dict(conn.execute("SELECT * FROM installers WHERE id=? AND tenant_id=?",
                             (installer_id, tenant_id)).fetchone())


def list_installers(tenant_id: int) -> list:
    conn = get_db()
    ensure_schema(conn)
    return conn.execute(
        """SELECT i.*, cg.slug AS group_slug, cg.display_name
           FROM installers i
           JOIN client_groups cg ON i.group_id = cg.id AND cg.tenant_id = i.tenant_id
           WHERE i.tenant_id = ?
           ORDER BY i.created_at DESC""",
        (tenant_id,),
    ).fetchall()


def main():
    parser = argparse.ArgumentParser(description="RustDesk installer generator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Build a pre-configured installer for a client group")
    p_build.add_argument("--tenant", type=int, default=1, help="Tenant id (default: 1)")
    p_build.add_argument("--group", required=True, help="Client group slug")
    p_build.add_argument(
        "--platform",
        default="windows-x64",
        choices=list(PLATFORMS),
        help="Target platform (default: windows-x64)",
    )

    p_list = sub.add_parser("list", help="List a tenant's generated installers")
    p_list.add_argument("--tenant", type=int, default=1, help="Tenant id (default: 1)")

    sub.add_parser(
        "update-version",
        help="Check GitHub for a newer RustDesk release and download it if found",
    )

    args = parser.parse_args()

    if args.cmd == "build":
        try:
            result = build_installer(args.tenant, args.group, args.platform)
        except InstallerError as e:
            sys.exit(f"Build failed: {e}")
        print(f"Installer {result['status']}.")
        print(f"  group:    {args.group}")
        print(f"  platform: {result['platform']}")
        print(f"  version:  {result['rustdesk_version']}")
        print(f"  path:     {result['unsigned_path']}")
        print(f"  sha256:   {result['sha256_unsigned']}")

    elif args.cmd == "list":
        rows = list_installers(args.tenant)
        if not rows:
            print("No installers built yet.")
        for r in rows:
            print(
                f"{r['group_slug']:<28} {r['platform']:<16} "
                f"v{r['rustdesk_version']:<8} {r['status']:<10} "
                f"{r['unsigned_path'] or '-'}"
            )

    elif args.cmd == "update-version":
        try:
            result = update_version()
        except InstallerError as e:
            sys.exit(f"Update check failed: {e}")
        if not result["updated"]:
            print(f"Already up to date (pinned: {result['current']}, latest: {result['latest']}).")
        else:
            print(f"Updated pin: {result['current']} -> {result['latest']}")
            for a in result["archs"]:
                print(f"  {a['arch']:<10} {a['path']}")
                print(f"  {'':<10} sha256={a['sha256']}")
            print("Future builds will use the new version. Old binaries were left in place.")


if __name__ == "__main__":
    main()
