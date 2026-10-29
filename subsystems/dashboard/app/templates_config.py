from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi.templating import Jinja2Templates
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")


def _get_flash(request):
    return request.session.pop("flash", None)


def _get_new_user_pw(request):
    return request.session.pop("new_user_pw", None)


templates.env.globals["get_flash"] = _get_flash
templates.env.globals["get_new_user_pw"] = _get_new_user_pw


def _tenant_of(request):
    from app.tenancy import request_tenant
    return request_tenant(request)


templates.env.globals["tenant_of"] = _tenant_of


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


def _localtime(dt_str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Convert a UTC datetime string to America/Los_Angeles and reformat."""
    if not dt_str:
        return ""
    try:
        clean = str(dt_str).replace("T", " ").split(".")[0][:19]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(_DISPLAY_TZ).strftime(fmt)
    except (ValueError, AttributeError):
        return str(dt_str)


templates.env.filters["localtime"] = _localtime


def _days_left(dt_str) -> int:
    """Whole days from now until a UTC datetime string (0 once it's past)."""
    try:
        clean = str(dt_str).replace("T", " ").split(".")[0][:19]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return 0
    return max(0, -(-int((dt - datetime.now(timezone.utc)).total_seconds()) // 86400))


templates.env.filters["days_left"] = _days_left


def _passkey_home(request) -> str:
    """On a tenant's custom domain: its own host, where its passkeys belong
    (webauthn_config.rp_id_for) and where a browser without Related Origin
    Requests signs in or adds a passkey. "" on the own host itself."""
    tenant = _tenant_of(request)
    if tenant is None or not tenant.hostname:
        return ""
    from app.webauthn_config import rp_id_for
    home = rp_id_for(tenant.id)
    return home if home and home != tenant.hostname else ""


templates.env.globals["passkey_home"] = _passkey_home


def _money(amount, currency: str = "usd") -> str:
    """Stripe's smallest-unit amounts, for display: 4900, 'usd' -> '$49.00'."""
    if amount is None:
        return "—"
    from app.plans import _money as fmt
    return fmt(amount, (currency or "usd").lower())


templates.env.filters["money"] = _money


def _billing_banners(request, current_user) -> list[dict]:
    """Billing notices for a Stripe tenant's billing admins (phase 2):
    trial ending, payment failed, suspended for non-payment, ending at period
    end, no card on file, over the plan's soft limits."""
    tenant = _tenant_of(request)
    if (tenant is None or tenant.billing_mode != "stripe" or not current_user or current_user.get("is_support")
            or "manage_billing" not in (current_user.get("permissions") or [])):
        return []
    from datetime import timedelta
    from app import billing_usage
    from app.deps import get_db
    conn = get_db()
    bs = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant.id,)).fetchone()
    over = billing_usage.over_limits(conn, tenant.id) if bs else []
    conn.close()
    if bs is None:
        return []
    out = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    if tenant.status == "suspended" and tenant.billing_suspended:
        out.append({"kind": "error", "msg": "This account is suspended for non-payment. Pay the open invoice to "
                    "reopen it at once; your devices, groups and settings are kept.", "link": "Pay now"})
    elif bs["status"] == "past_due":
        out.append({"kind": "error", "msg": "Your last payment failed. Update your card to keep the account open.",
                    "link": "Update card"})
    if bs["status"] == "trialing" and bs["trial_end"] and now < bs["trial_end"] <= soon and not bs["cancel_at_period_end"]:
        out.append({"kind": "warning", "msg": f"Your free trial ends on {_localtime(bs['trial_end'], '%Y-%m-%d')}; "
                    "your card is charged for the first period then.", "link": "Billing"})
    if bs["cancel_at_period_end"] and bs["status"] in ("trialing", "active", "past_due"):
        ends = bs["cancel_at"] or bs["current_period_end"] or bs["trial_end"]
        out.append({"kind": "warning", "msg": f"Your subscription ends on {_localtime(ends, '%Y-%m-%d')}. After that "
                    "the account is cancelled; you can resubscribe until its data is deleted.", "link": "Billing"})
    if bs["has_card"] == 0 and bs["status"] in ("trialing", "active", "past_due"):
        out.append({"kind": "error", "msg": "There's no card on file. Without one the subscription ends when the "
                    "trial does (or at the next renewal).", "link": "Add a card"})
    if over:
        out.append({"kind": "warning", "msg": "You're over your plan's limits: " + "; ".join(over) + ".",
                    "link": "Change plan"})
    return out


templates.env.globals["billing_banners"] = _billing_banners
