from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_auth
from app.templates_config import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, current_user: dict = Depends(require_auth)):
    from setup_server import get_status
    status = get_status()
    return templates.TemplateResponse(
        request, "home.html", {"server": status, "current_user": current_user}
    )
