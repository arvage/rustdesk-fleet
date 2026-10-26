import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_auth
from app.deps import get_db
from app.reports import (
    CATEGORIES, DEFAULT_DAYS, NO_GROUP_FILTER, PERIODS, REPORTS, STATUS_COLUMNS, STATUS_TONES,
    ReportContext, stat_tone,
)
from app.routes.admin import _require_admin
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()


def _context(tenant: Tenant, key: str, group: str, days: str) -> ReportContext:
    valid_days = {d for d, _ in PERIODS}
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 0
    if n not in valid_days:
        n = DEFAULT_DAYS.get(key, 30)
    return ReportContext(tenant=tenant, group="" if key in NO_GROUP_FILTER else group, days=n)


@router.get("/admin/reports", response_class=HTMLResponse)
async def reports_index(request: Request, current_user: dict = Depends(require_auth)):
    _require_admin(current_user)
    by_cat = {c: [] for c in CATEGORIES}
    for key, (title, desc, cat, _uses_period, _fn) in REPORTS.items():
        by_cat[cat].append({"key": key, "title": title, "description": desc})
    # Each category keeps one categorical slot (identity color for its cards).
    cat_slots = {"Fleet": 1, "Maintenance": 2, "Clients": 3, "Administration": 7}
    return templates.TemplateResponse(request, "reports.html", {
        "categories": by_cat, "cat_slots": cat_slots, "current_user": current_user,
    })


@router.get("/admin/reports/{key}", response_class=HTMLResponse)
async def report_view(
    request: Request, key: str, group: str = "", days: str = "",
    current_user: dict = Depends(require_auth),
    tenant: Tenant = Depends(get_tenant),
):
    _require_admin(current_user)
    if key not in REPORTS:
        request.session["flash"] = {"type": "error", "msg": "Report not found."}
        return RedirectResponse("/admin/reports", status_code=303)
    title, desc, _cat, uses_period, fn = REPORTS[key]
    ctx = _context(tenant, key, group, days)
    data = fn(ctx)
    conn = get_db()
    groups = conn.execute("SELECT slug, display_name FROM client_groups WHERE tenant_id = ? ORDER BY display_name",
                          (tenant.id,)).fetchall()
    conn.close()
    return templates.TemplateResponse(request, "report.html", {
        "key": key, "title": title, "description": desc,
        "uses_period": uses_period, "uses_group": key not in NO_GROUP_FILTER,
        "periods": PERIODS, "groups": groups, "ctx": ctx,
        "data": data, "current_user": current_user,
        "stat_tone": stat_tone, "status_tones": STATUS_TONES, "status_columns": STATUS_COLUMNS,
    })


@router.get("/admin/reports/{key}/{section}.csv")
async def report_csv(
    key: str, section: int, group: str = "", days: str = "",
    current_user: dict = Depends(require_auth),
    tenant: Tenant = Depends(get_tenant),
):
    _require_admin(current_user)
    if key not in REPORTS:
        return Response("Not found", status_code=404)
    ctx = _context(tenant, key, group, days)
    sections = REPORTS[key][4](ctx)["sections"]
    if not 0 <= section < len(sections):
        return Response("Not found", status_code=404)
    sec = sections[section]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(sec["columns"])
    w.writerows(sec["rows"])
    slug = "".join(c if c.isalnum() else "-" for c in sec["title"].lower()).strip("-")
    suffix = f"-{ctx.group}" if ctx.group else ""
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={key}-{slug}{suffix}.csv"},
    )
