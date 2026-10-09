from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi.templating import Jinja2Templates
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")


def _get_flash(request):
    return request.session.pop("flash", None)


def _get_new_user_pw(request):
    return request.session.pop("new_user_pw", None)


templates.env.globals["get_flash"] = _get_flash
templates.env.globals["get_new_user_pw"] = _get_new_user_pw


def _format_rid(rid: str) -> str:
    """Format a RustDesk ID in groups of 3 from the right: '# ### ### ###'."""
    s = str(rid).strip()
    if not s.isdigit():
        return s
    groups = []
    while len(s) > 3:
        groups.insert(0, s[-3:])
        s = s[:-3]
    groups.insert(0, s)
    return " ".join(groups)


templates.env.filters["format_rid"] = _format_rid


def _localtime(dt_str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Convert a UTC datetime string to America/Los_Angeles and reformat."""
    if not dt_str:
        return ""
    try:
        clean = str(dt_str).replace("T", " ").split(".")[0][:19]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(_DISPLAY_TZ).strftime(fmt)
    except (ValueError, AttributeError):
        return str(dt_str)


templates.env.filters["localtime"] = _localtime
