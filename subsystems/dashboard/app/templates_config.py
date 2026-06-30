from fastapi.templating import Jinja2Templates
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _get_flash(request):
    return request.session.pop("flash", None)


templates.env.globals["get_flash"] = _get_flash
