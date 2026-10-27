"""Console pages for relays and jobs."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import settings
from app.console import jobs, relays
from app.console.auth import require_provider
from app.deps import get_db, provider_log
from app.tenancy import load_tenant
from app.templates_config import templates

router = APIRouter()


def _flash(request: Request, kind: str, msg: str) -> None:
    request.session["flash"] = {"type": kind, "msg": msg}


def relay_rows() -> list[dict]:
    from setup_server import get_latest_server_version
    conn = get_db()
    rows = conn.execute(
        """SELECT r.*, t.slug, t.display_name, t.status AS tenant_status FROM relays r
           JOIN tenants t ON t.id = r.tenant_id WHERE t.status <> 'purged' ORDER BY r.tenant_id""").fetchall()
    conn.close()
    out = []
    for r in rows:
        t = load_tenant(r["tenant_id"])
        out.append({**dict(r), "version": relays.running_version(t) if t else None,
                    "containers": relays.health(t) if t else {}, "block": relays.block_of(t.relay) if t else None,
                    "busy": bool(jobs.active_for(r["tenant_id"]))})
    return out


@router.get("/relays", response_class=HTMLResponse)
def relays_page(request: Request, user: dict = Depends(require_provider)):
    from setup_server import get_latest_server_version
    return templates.TemplateResponse(request, "console/relays.html", {
        "current_user": user, "relays": relay_rows(), "latest": get_latest_server_version(),
        "current": relays.current_server_version(),
        "port_range": (settings.relay_port_base(), settings.relay_port_max()),
    })


@router.post("/relays/upgrade-all")
def relays_upgrade_all(request: Request, version: str = Form(""), user: dict = Depends(require_provider)):
    from setup_server import get_latest_server_version
    version = version.strip() or (get_latest_server_version() or "")
    if not relays.IMAGE_TAG_RE.match(f"rustdesk/rustdesk-server:{version}") or not version:
        _flash(request, "error", "Which version? GitHub couldn't be reached; type one in.")
        return RedirectResponse("/relays", status_code=303)
    job_id = jobs.enqueue("relay_upgrade_all", None, {"version": version}, user["email"])
    conn = get_db()
    provider_log(conn, "relay_upgrade_all_queued", version, user["email"])
    conn.close()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/relays/{tenant_id}/{action}")
def relay_action(request: Request, tenant_id: int, action: str, version: str = Form(""),
                 user: dict = Depends(require_provider)):
    t = load_tenant(tenant_id)
    if t is None or t.relay is None or t.status == "purged":
        _flash(request, "error", "Relay not found.")
        return RedirectResponse("/relays", status_code=303)
    if action not in ("start", "stop", "restart", "upgrade"):
        _flash(request, "error", "Unknown action.")
        return RedirectResponse("/relays", status_code=303)
    params = {"action": action}
    if action == "upgrade":
        from setup_server import get_latest_server_version
        params["version"] = version.strip() or (get_latest_server_version() or "")
        if not params["version"]:
            _flash(request, "error", "Which version? GitHub couldn't be reached; type one in.")
            return RedirectResponse("/relays", status_code=303)
    if jobs.active_for(tenant_id):
        _flash(request, "error", f"{t.slug} already has a job running; wait for it to finish.")
        return RedirectResponse("/relays", status_code=303)
    job_id = jobs.enqueue("relay_action", tenant_id, params, user["email"])
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# ── Jobs ─────────────────────────────────────────────────────────────────────

def _job_view(row) -> dict:
    d = dict(row)
    d["steps"] = json.loads(row["steps"] or "[]")
    d["params"] = json.loads(row["params"] or "{}")
    return d


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, user: dict = Depends(require_provider)):
    conn = get_db()
    rows = conn.execute("""SELECT j.*, t.slug FROM provider_jobs j LEFT JOIN tenants t ON t.id = j.tenant_id
                           ORDER BY j.id DESC LIMIT 200""").fetchall()
    conn.close()
    return templates.TemplateResponse(request, "console/jobs.html",
                                      {"current_user": user, "jobs": [_job_view(r) for r in rows]})


@router.get("/jobs/{job_id}.json")
def job_json(job_id: int, user: dict = Depends(require_provider)):
    row = jobs.get(job_id)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    v = _job_view(row)
    return JSONResponse({k: v[k] for k in ("id", "kind", "status", "steps", "error", "tenant_id")})


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: int, user: dict = Depends(require_provider)):
    row = jobs.get(job_id)
    if row is None:
        _flash(request, "error", "Job not found.")
        return RedirectResponse("/jobs", status_code=303)
    return templates.TemplateResponse(request, "console/job.html", {"current_user": user, "job": _job_view(row)})
