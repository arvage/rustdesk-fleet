"""Render every installer type for one of tenant #1's groups, as text, with
makensis faked (it just copies the NSIS script it is given to the output
file), so tests can compare the configuration an installer carries. Paths
under the test data directory are replaced with <DATA>."""
import os
import stat
from pathlib import Path

from conftest import DATA_DIR

# windows-arm64 and macos are left out: the installers table's CHECK doesn't
# allow them (an existing bug, see the phase 1b summary).
PLATFORMS = ("windows-x64", "linux")


def _fake_makensis(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "makensis"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import re, shutil, sys\n"
        "text = open(sys.argv[1]).read()\n"
        "out = re.search(r'OutFile \"([^\"]+)\"', text).group(1)\n"
        "shutil.copy(sys.argv[1], out)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def render_tenant_installers(tenant_id: int, slug: str, tmp: Path) -> dict[str, str]:
    import generate_installer as gi
    _fake_makensis(tmp / "bin")
    os.environ["PATH"] = f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}"
    gi.VERSION_FILE.write_text("1.4.9\n")
    gi.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for arch in ("x86_64", "aarch64"):
        (gi.ASSETS_DIR / f"rustdesk-1.4.9-{arch}.exe").write_bytes(b"MZ")
    out = {}
    for platform in PLATFORMS:
        row = gi.build_installer(tenant_id, slug, platform, "tester@example.com")
        out[platform] = Path(row["unsigned_path"]).read_text().replace(str(DATA_DIR), "<DATA>")
    return out
