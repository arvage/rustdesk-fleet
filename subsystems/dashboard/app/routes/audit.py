from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_auth
from app.deps import get_db
from app.templates_config import templates

router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request, current_user: dict = Depends(require_auth)):
    conn = get_db()
    events = conn.execute(
        "SELECT * FROM provisioning_events ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request, "audit.html", {"events": events, "current_user": current_user}
    )
