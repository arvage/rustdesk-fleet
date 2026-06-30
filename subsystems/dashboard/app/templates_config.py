from fastapi.templating import Jinja2Templates
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
