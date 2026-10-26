from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import email_templates as et
from app.auth import require_auth
from app.deps import get_db, log_event
from app.notifications import _log, _send_sync, get_settings
from app.permissions import require_perm
from app.templates_config import templates

router = APIRouter()

_CAP = "manage_notifications"


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _audit(event: str, detail: str, user: dict) -> None:
    conn = get_db()
    log_event(conn, event, detail, user["email"], tenant_id=user["tenant_id"])
    conn.close()


@router.get("/admin/emails", response_class=HTMLResponse)
async def emails_index(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _CAP)
    groups = {c: [] for c in et.CATEGORIES}
    for key, spec in et.TEMPLATES.items():
        t = et.get_template(current_user["tenant_id"], key)
        groups[spec["category"]].append({
            "key": key, "name": spec["name"], "audience": spec["audience"],
            "subject": t["subject"], "customized": t["customized"],
        })
    return templates.TemplateResponse(request, "email_templates.html", {
        "groups": groups, "design": et.get_design(current_user["tenant_id"]),
        "has_logo": et.has_logo(current_user["tenant_id"]),
        "current_user": current_user,
    })


# ── Design ───────────────────────────────────────────────────────────────────

@router.get("/admin/emails/design", response_class=HTMLResponse)
async def design_page(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _CAP)
    tid = current_user["tenant_id"]
    return templates.TemplateResponse(request, "email_design.html", {
        "design": et.get_design(tid), "defaults": et.DESIGN_DEFAULTS, "fonts": et.FONTS,
        "has_logo": et.has_logo(tid), "logo_v": int(et.logo_path(tid).stat().st_mtime) if et.has_logo(tid) else 0,
        "current_user": current_user,
    })


@router.post("/admin/emails/design")
async def design_save(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _CAP)
    form = await request.form()
    tid = current_user["tenant_id"]
    if form.get("action") == "reset":
        et.reset_design(tid)
        _audit("email_design_updated", "reset to defaults", current_user)
        _set_flash(request, "success", "Email design reset to the defaults (the logo was kept).")
        return RedirectResponse("/admin/emails/design", status_code=303)

    logo = form.get("logo")
    if hasattr(logo, "read") and logo.filename:
        try:
            et.save_logo(tid, await logo.read(et.MAX_LOGO_BYTES + 1))
        except et.LogoError as e:
            _set_flash(request, "error", str(e))
            return RedirectResponse("/admin/emails/design", status_code=303)
    elif form.get("remove_logo") == "1":
        et.remove_logo(tid)
    et.save_design(tid, dict(form))
    _audit("email_design_updated", "", current_user)
    _set_flash(request, "success", "Email design saved — it applies to every email from now on.")
    return RedirectResponse("/admin/emails/design", status_code=303)


@router.get("/admin/emails/logo.png")
async def logo_file(current_user: dict = Depends(require_auth)):
    require_perm(current_user, _CAP)
    data = et.logo_bytes(current_user["tenant_id"])
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})


# ── Preview & test ───────────────────────────────────────────────────────────

def _draft_template(tenant_id: int, key: str, body: dict) -> dict:
    t = et.get_template(tenant_id, key)
    for f in et.TEXT_FIELDS:
        if f in body:
            t[f] = str(body[f])
    if "show_details" in body:
        t["show_details"] = bool(body["show_details"]) or bool(et.TEMPLATES[key].get("details_required"))
    return t


def _draft_design(tenant_id: int, body: dict) -> dict:
    d = et.get_design(tenant_id)
    for k in et.DESIGN_DEFAULTS:
        if k in body:
            d[k] = body[k]
    # same validation as saving, without writing anything
    for k in ("header_bg", "header_text", "accent", "page_bg"):
        if not et._COLOR_RE.match(str(d[k])):
            d[k] = et.DESIGN_DEFAULTS[k]
    try:
        d["logo_height"] = max(20, min(120, int(d["logo_height"])))
    except (TypeError, ValueError):
        d["logo_height"] = 40
    for k in ("show_logo_team", "show_logo_client"):
        d[k] = d[k] in ("1", 1, True)
    return d


@router.post("/admin/emails/preview")
async def preview(request: Request, current_user: dict = Depends(require_auth)):
    """Render an unsaved draft (template text and/or design) with sample data."""
    require_perm(current_user, _CAP)
    body = await request.json()
    key = body.get("key") if body.get("key") in et.TEMPLATES else "device_registered"
    tid = current_user["tenant_id"]
    subject, html, _ = et.render(
        tid, key, et.TEMPLATES[key]["sample"], preview=True,
        template=_draft_template(tid, key, body.get("template") or {}),
        design=_draft_design(tid, body.get("design") or {}),
    )
    return JSONResponse({"subject": subject, "html": html})


@router.post("/admin/emails/{key}/test")
async def send_test(request: Request, key: str, current_user: dict = Depends(require_auth)):
    """Send the saved version of one email, with sample data, to the signed-in user."""
    require_perm(current_user, _CAP)
    if key not in et.TEMPLATES:
        return JSONResponse({"ok": False, "error": "Unknown email."})
    tid = current_user["tenant_id"]
    settings = get_settings(tid)
    if not settings.get("smtp_host") or not settings.get("from_addr"):
        return JSONResponse({"ok": False, "error": "Set up SMTP on the Notifications page first."})
    to = current_user.get("email") or ""
    if "@" not in to:
        return JSONResponse({"ok": False, "error": "Your account has no email address."})
    subject, html, _ = et.render(tid, key, et.TEMPLATES[key]["sample"])
    subject = "[Test] " + subject
    try:
        _send_sync(settings, [to], subject, html)
        _log(tid, "test", subject, to, "sent")
        return JSONResponse({"ok": True, "to": to})
    except Exception as exc:
        _log(tid, "test", subject, to, "failed", str(exc)[:500])
        return JSONResponse({"ok": False, "error": str(exc)[:300]})


# ── Per-email editor ─────────────────────────────────────────────────────────

@router.get("/admin/emails/{key}", response_class=HTMLResponse)
async def template_page(request: Request, key: str, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _CAP)
    if key not in et.TEMPLATES:
        _set_flash(request, "error", "Unknown email template.")
        return RedirectResponse("/admin/emails", status_code=303)
    spec = et.TEMPLATES[key]
    return templates.TemplateResponse(request, "email_template_edit.html", {
        "key": key, "spec": spec, "t": et.get_template(current_user["tenant_id"], key),
        "placeholders": et.placeholders(key),
        "current_user": current_user,
    })


@router.post("/admin/emails/{key}")
async def template_save(request: Request, key: str, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _CAP)
    if key not in et.TEMPLATES:
        return RedirectResponse("/admin/emails", status_code=303)
    form = await request.form()
    name = et.TEMPLATES[key]["name"]
    if form.get("action") == "reset":
        et.reset_template(current_user["tenant_id"], key)
        _audit("email_template_updated", f"{key}: reset to default", current_user)
        _set_flash(request, "success", f"“{name}” reset to the default wording.")
    else:
        et.save_template(current_user["tenant_id"], key, dict(form), current_user["email"])
        _audit("email_template_updated", key, current_user)
        _set_flash(request, "success", f"“{name}” saved.")
    return RedirectResponse(f"/admin/emails/{key}", status_code=303)
