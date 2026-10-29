"""The public site on PUBLIC_SITE_HOST (default www.<PROVIDER_DOMAIN>): the
product page with pricing, self-service sign-up and the Stripe webhook
(phase 2). A provider host like the console: TenantMiddleware hands its
requests here, so no tenant route answers on it and none of these answers
on a tenant host or the console.

No sessions and no sign-in here: a sign-up is carried by its single-use
verification link and then by its random `ref`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import plans, settings, signup, stripe_api
from app.deps import get_db
from app.templates_config import templates

public_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
public_app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@public_app.exception_handler(404)
async def _not_found(request: Request, _exc):
    return HTMLResponse("Not found", status_code=404)


def _ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def _page(request: Request, name: str, ctx: dict, status: int = 200):
    return templates.TemplateResponse(request, f"public/{name}", ctx, status_code=status)


def _message(request: Request, title: str, body: str, status: int = 200, **extra):
    return _page(request, "message.html", {"title": title, "body": body, **extra}, status)


def _offer(conn) -> dict:
    return {"plans": plans.public_plans(conn) if stripe_api.enabled() else [],
            "signup_open": signup.closed_reason(conn) is None, "trial_days": signup.TRIAL_DAYS}


@public_app.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = get_db()
    ctx = _offer(conn)
    conn.close()
    return _page(request, "index.html", ctx)


@public_app.get("/signin", response_class=HTMLResponse)
def signin(request: Request, account: str = ""):
    """Each account signs in on its own address; this finds it."""
    account = account.strip().lower()
    error = ""
    if account:
        conn = get_db()
        row = conn.execute("SELECT t.status, d.hostname FROM tenants t JOIN tenant_domains d ON d.tenant_id = t.id"
                           " AND d.kind = 'provider_subdomain' WHERE t.slug = ?", (account,)).fetchone()
        conn.close()
        if row and row["status"] in ("trial", "active", "suspended", "cancelled"):
            return RedirectResponse(f"https://{row['hostname']}/login", status_code=303)
        error = "We couldn't find that account."
    return _page(request, "signin.html", {"account": account, "error": error,
                                          "example": settings.tenant_web_host("yourcompany")})


@public_app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, plan: str = ""):
    conn = get_db()
    ctx = _offer(conn)
    closed = signup.closed_reason(conn)
    conn.close()
    if closed:
        return _message(request, "Sign-up is closed", closed)
    return _page(request, "signup.html", {**ctx, "form": {"plan": plan}, "error": "", **_legal()})


def _legal() -> dict:
    return {"terms_url": settings.terms_url(), "privacy_url": settings.privacy_url(),
            "suffix": "." + settings.provider_domain()}


@public_app.get("/signup/slug-check")
def slug_check(slug: str = "", name: str = ""):
    """Live check for the sign-up form (read-only)."""
    slug = slug.strip().lower()
    if not slug and name:
        slug = settings.suggest_slug(name)
    conn = get_db()
    problem = signup.slug_problem(conn, slug) if slug else "Choose an address."
    conn.close()
    return JSONResponse({"slug": slug, "ok": problem is None, "problem": problem or "",
                         "host": settings.tenant_web_host(slug) if slug else ""})


@public_app.post("/signup", response_class=HTMLResponse)
async def signup_submit(request: Request):
    form = dict(await request.form())
    conn = get_db()
    try:
        signup_id, token = signup.start(conn, form, _ip(request))
    except signup.SignupError as e:
        ctx = _offer(conn)
        conn.close()
        form.pop("password", None)
        return _page(request, "signup.html", {**ctx, "form": form, "error": str(e), **_legal()}, 400)
    row = signup.get(conn, signup_id)
    conn.close()
    from app import billing_emails
    billing_emails.verify_email(row, f"https://{settings.public_site_host()}/signup/verify?token={token}")
    return _message(request, "Check your email",
                    f"We sent a link to {row['email']}. Open it within 24 hours to confirm your address and "
                    "continue to payment details. Nothing is charged during the "
                    f"{signup.TRIAL_DAYS}-day trial.")


@public_app.get("/signup/verify", response_class=HTMLResponse)
def verify_page(request: Request, token: str = ""):
    """A page with a button, not the verification itself: mail scanners
    open links, and must not spend a single-use token."""
    return _page(request, "verify.html", {"token": token})


@public_app.post("/signup/verify")
async def verify_submit(request: Request):
    form = await request.form()
    conn = get_db()
    try:
        row = signup.verify(conn, str(form.get("token") or ""))
        url = signup.create_checkout(conn, row)
    except signup.SignupError as e:
        conn.close()
        return _message(request, "Can't continue", str(e), 400)
    except stripe_api.BillingUnavailable:
        conn.close()
        return _message(request, "Sign-up is closed", "Sign-up isn't available right now.", 503)
    except Exception:
        conn.close()
        return _message(request, "Something went wrong", "We couldn't reach our payment provider. "
                        "Please try the link again in a few minutes.", 502)
    conn.close()
    return RedirectResponse(url, status_code=303)


@public_app.post("/signup/{ref}/checkout")
def checkout_again(request: Request, ref: str):
    """Back from Checkout without finishing: a fresh session for the same
    verified sign-up."""
    conn = get_db()
    row = signup.by_ref(conn, ref)
    try:
        if row is None:
            raise signup.SignupError("This sign-up wasn't found.")
        url = signup.create_checkout(conn, row)
    except (signup.SignupError, stripe_api.BillingUnavailable) as e:
        conn.close()
        return _message(request, "Can't continue", str(e), 400)
    except Exception:
        conn.close()
        return _message(request, "Something went wrong", "We couldn't reach our payment provider. "
                        "Please try again in a few minutes.", 502)
    conn.close()
    return RedirectResponse(url, status_code=303)


def _state(row) -> dict:
    """What the progress page shows for a sign-up."""
    conn = get_db()
    host = ""
    if row["tenant_id"]:
        r = conn.execute("SELECT hostname FROM tenant_domains WHERE tenant_id = ? AND kind = 'provider_subdomain'",
                         (row["tenant_id"],)).fetchone()
        host = r[0] if r else ""
    conn.close()
    return {"status": row["status"], "host": host, "trial_denied": bool(row["trial_denied"]),
            "login": f"https://{host}/login" if host else ""}


@public_app.get("/signup/{ref}/progress", response_class=HTMLResponse)
def progress(request: Request, ref: str, cancelled: str = ""):
    conn = get_db()
    row = signup.by_ref(conn, ref)
    conn.close()
    if row is None:
        return _message(request, "Not found", "This sign-up wasn't found.", 404)
    return _page(request, "progress.html", {"ref": ref, "row": row, "state": _state(row),
                                            "cancelled": bool(cancelled), "trial_days": signup.TRIAL_DAYS})


@public_app.get("/signup/{ref}/status")
def status(ref: str):
    conn = get_db()
    row = signup.by_ref(conn, ref)
    conn.close()
    if row is None:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return JSONResponse(_state(row))
