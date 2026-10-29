"""Console: the provider-level emails (sign-up and billing), in the same
template system tenants use for theirs (app/email_templates.py)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import email_templates as et
from app.console.auth import require_provider
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()


@router.get("/emails", response_class=HTMLResponse)
def emails(request: Request, user: dict = Depends(require_provider)):
    rows = [{"key": k, "name": v["name"], **et.get_provider_template(k)} for k, v in et.PROVIDER_TEMPLATES.items()]
    return templates.TemplateResponse(request, "console/emails.html", {"current_user": user, "rows": rows})


@router.get("/emails/{key}", response_class=HTMLResponse)
def email_edit(request: Request, key: str, user: dict = Depends(require_provider)):
    if key not in et.PROVIDER_TEMPLATES:
        return RedirectResponse("/emails", status_code=303)
    spec = et.PROVIDER_TEMPLATES[key]
    return templates.TemplateResponse(request, "console/email_edit.html", {
        "current_user": user, "key": key, "spec": spec, "t": et.get_provider_template(key),
        "placeholders": sorted(spec["sample"])})


@router.get("/emails/{key}/preview", response_class=HTMLResponse)
def email_preview(key: str, user: dict = Depends(require_provider)):
    if key not in et.PROVIDER_TEMPLATES:
        return HTMLResponse("Not found", status_code=404)
    subject, html, _ = et.render(0, key, et.PROVIDER_TEMPLATES[key]["sample"], preview=True)
    return HTMLResponse(html)


@router.post("/emails/{key}")
async def email_save(request: Request, key: str, user: dict = Depends(require_provider)):
    if key not in et.PROVIDER_TEMPLATES:
        return RedirectResponse("/emails", status_code=303)
    form = await request.form()
    if form.get("action") == "reset":
        et.reset_provider_template(key)
        msg = "Back to the default text."
    else:
        et.save_provider_template(key, {f: str(form.get(f) or "") for f in et.TEXT_FIELDS}, user["email"])
        msg = "Saved."
    conn = get_db()
    provider_log(conn, "provider_email_saved", key + (" (reset)" if form.get("action") == "reset" else ""), user["email"])
    conn.close()
    request.session["flash"] = {"type": "success", "msg": msg}
    return RedirectResponse(f"/emails/{key}", status_code=303)
