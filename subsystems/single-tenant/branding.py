"""
branding.py — per-group client branding: an app name and an icon.

The icon is uploaded once (PNG or ICO) and stored in two forms, in the
group's tenant's branding directory (tenant_paths.branding_dir), keyed by
group id so renaming a group's slug doesn't orphan it:

  <branding dir>/<group_id>.ico   multi-size, for the Windows installer icon
                                  and the branded Start menu / desktop shortcuts
  <branding dir>/<group_id>.png   256x256, for the Linux menu entry and previews

Only the wrapper installer and the shortcuts are branded; the RustDesk app
itself (window, taskbar, tray) keeps its own icon and name.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from tenant_paths import branding_dir

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MIN_ICON_PX = 32
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

# Used verbatim as a Windows shortcut file name, inside NSIS double-quoted
# strings and a double-quoted shell variable: no path separators, quotes,
# "$", backticks or backslashes.
APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._&'(),+-]{0,58}[A-Za-z0-9.)]$")


class BrandingError(Exception):
    pass


def app_name_problem(name: str) -> str | None:
    """Error message for an invalid app name, or None if it's fine (or blank)."""
    if not name:
        return None
    if not APP_NAME_RE.match(name):
        return ("App name must be 2–60 characters: letters, digits, spaces and . _ & ' ( ) , + - "
                "(no quotes, slashes or $).")
    return None


def icon_paths(tenant_id: int, group_id: int) -> tuple[Path, Path]:
    d = branding_dir(tenant_id)
    return d / f"{int(group_id)}.ico", d / f"{int(group_id)}.png"


def has_icon(tenant_id: int, group_id: int) -> bool:
    ico, png = icon_paths(tenant_id, group_id)
    return ico.exists() and png.exists()


def save_icon(tenant_id: int, group_id: int, data: bytes) -> None:
    """Validate an uploaded PNG/ICO and store it as .ico + .png. Raises BrandingError."""
    from PIL import Image, UnidentifiedImageError

    if not data:
        raise BrandingError("The uploaded icon file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BrandingError("Icon file is too large (2 MB max).")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "ICO"):
            raise BrandingError("Icon must be a PNG or ICO file.")
        if img.format == "ICO":
            # Use the largest image in the .ico.
            img.size = max(img.ico.sizes())
        img.load()
    except UnidentifiedImageError:
        raise BrandingError("Icon must be a PNG or ICO file.")

    img = img.convert("RGBA")
    if min(img.size) < MIN_ICON_PX:
        raise BrandingError(f"Icon is too small — use at least {MIN_ICON_PX}×{MIN_ICON_PX} px (256×256 is best).")

    # Pad to a square on a transparent background, then scale to 256.
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    icon = square.resize((256, 256), Image.LANCZOS)

    branding_dir(tenant_id).mkdir(parents=True, exist_ok=True)
    ico, png = icon_paths(tenant_id, group_id)
    tmp_ico, tmp_png = ico.with_suffix(".ico.part"), png.with_suffix(".png.part")
    # BMP-format entries: the most widely compatible (NSIS, older shell code).
    icon.save(tmp_ico, format="ICO", sizes=ICO_SIZES, bitmap_format="bmp")
    icon.save(tmp_png, format="PNG", optimize=True)
    tmp_ico.replace(ico)
    tmp_png.replace(png)


def delete_icon(tenant_id: int, group_id: int) -> None:
    for p in icon_paths(tenant_id, group_id):
        p.unlink(missing_ok=True)
