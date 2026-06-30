import os
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templates_config import templates

router = APIRouter()


def require_auth(request: Request) -> None:
    if not request.session.get("ok"):
        raise _AuthRedirect()


class _AuthRedirect(Exception):
    pass


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if request.session.get("ok"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    expected = os.environ.get("DASHBOARD_PASSWORD", "")
    if expected and secrets.compare_digest(password, expected):
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Incorrect password."}, status_code=401
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
