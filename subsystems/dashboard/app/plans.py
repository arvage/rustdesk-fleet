"""Billing plans as configuration (phase 2; TENANCY.md, "Billing").

Pricing isn't code: a plan names up to three Stripe prices, one per unit
type, created in the Stripe dashboard:

    flat        a recurring price, quantity 1
    technician  per billable technician (licensed quantity)
    device      per registered device: licensed quantity, or usage-based
                through a Stripe Billing Meter

**Included quantities** use Stripe's graduated tiers: a price whose first
tier costs nothing ("up to 2: $0, then $5.00 each") includes that many.
We always report the real count and Stripe charges only the overage, so the
invoice shows exactly what was counted and there is no second copy of the
pricing rules here. A per-unit price includes nothing.

On save every price is fetched from Stripe and checked: it exists and is
active, it is recurring, all prices of a plan share one currency and one
billing interval, and only a device price may be metered (and then it must
be backed by a Billing Meter).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import stripe_api

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
PRICE_RE = re.compile(r"^price_[A-Za-z0-9]{3,100}$")
UNIT_LABELS = {"flat": "per month", "technician": "per technician", "device": "per device"}


class PlanError(Exception):
    pass


@dataclass
class PriceInfo:
    unit_type: str
    price_id: str
    product_id: str
    currency: str
    interval: str
    billing_scheme: str        # licensed or metered
    included: int
    summary: str
    meter_event_name: str | None = None


def _money(amount, currency: str) -> str:
    if amount is None:
        return "?"
    symbol = {"usd": "$", "eur": "€", "gbp": "£", "cad": "CA$", "aud": "A$"}.get(currency, "")
    value = f"{int(amount) / 100:,.2f}" if float(amount).is_integer() else f"{float(amount) / 100:,.4f}"
    return f"{symbol}{value}" if symbol else f"{value} {currency.upper()}"


def _interval(recurring) -> str:
    n = int(recurring.get("interval_count") or 1)
    return recurring["interval"] if n == 1 else f"{n} {recurring['interval']}"


def describe(price, unit_type: str, meter_event: str | None = None) -> PriceInfo:
    """Check one Stripe price for a unit type and summarise it."""
    p = price
    if not p.get("active"):
        raise PlanError(f"{p['id']} is archived in Stripe.")
    recurring = p.get("recurring")
    if not recurring:
        raise PlanError(f"{p['id']} isn't a recurring price.")
    usage = recurring.get("usage_type") or "licensed"
    if usage == "metered" and unit_type != "device":
        raise PlanError(f"{p['id']} is usage-based (metered); only the device price may be, "
                        f"the {unit_type} price must be a licensed (per-unit or tiered) price.")
    if usage == "metered" and not recurring.get("meter"):
        raise PlanError(f"{p['id']} is metered but has no Billing Meter.")
    if unit_type == "flat" and p.get("billing_scheme") == "tiered":
        raise PlanError(f"{p['id']} is tiered; the flat price must be a simple per-unit price.")
    currency = p["currency"]
    every = _interval(recurring)
    included = 0
    unit = UNIT_LABELS[unit_type]
    if p.get("billing_scheme") == "tiered":
        tiers = p.get("tiers") or []
        if p.get("tiers_mode") == "graduated" and tiers and not tiers[0].get("unit_amount") \
                and not tiers[0].get("flat_amount") and tiers[0].get("up_to"):
            included = int(tiers[0]["up_to"])
            rest = tiers[1] if len(tiers) > 1 else {}
            summary = f"{included} included, then {_money(rest.get('unit_amount'), currency)} {unit} / {every}"
        else:
            summary = f"tiered ({p.get('tiers_mode')}) {unit} / {every}"
    else:
        summary = f"{_money(p.get('unit_amount'), currency)} {unit} / {every}" if unit_type != "flat" \
            else f"{_money(p.get('unit_amount'), currency)} / {every}"
    if usage == "metered":
        summary += " (usage-based, daily peak)"
    product = p.get("product")
    return PriceInfo(unit_type, p["id"], product if isinstance(product, str) else (product or {}).get("id", ""),
                     currency, every, "metered" if usage == "metered" else "licensed", included, summary,
                     meter_event)


def check_prices(entries: dict[str, str]) -> list[PriceInfo]:
    """{unit_type: price id} -> checked PriceInfo list, or PlanError. Calls
    Stripe (short timeout)."""
    entries = {k: v.strip() for k, v in entries.items() if v and v.strip()}
    if not entries:
        raise PlanError("A plan needs at least one Stripe price.")
    for unit, pid in entries.items():
        if unit not in UNIT_LABELS:
            raise PlanError(f"Unknown unit type {unit}.")
        if not PRICE_RE.match(pid):
            raise PlanError(f"{pid!r} doesn't look like a Stripe Price ID (price_…).")
    client = stripe_api.client()
    out = []
    for unit, pid in entries.items():
        try:
            price = stripe_api.plain(client.v1.prices.retrieve(pid))
        except Exception as e:
            raise PlanError(f"Stripe couldn't find {pid}: {getattr(e, 'user_message', None) or e}")
        meter_event = None
        meter = (price.get("recurring") or {}).get("meter")
        if meter:
            try:
                meter_event = stripe_api.plain(client.v1.billing.meters.retrieve(meter)).get("event_name")
            except Exception as e:
                raise PlanError(f"Stripe couldn't find the Billing Meter {meter} of {pid}: {e}")
        out.append(describe(price, unit, meter_event))
    if len({p.currency for p in out}) > 1:
        raise PlanError("All prices of a plan must use the same currency: "
                        + ", ".join(f"{p.price_id} {p.currency.upper()}" for p in out))
    if len({p.interval for p in out}) > 1:
        raise PlanError("All prices of a plan must bill on the same interval: "
                        + ", ".join(f"{p.price_id} every {p.interval}" for p in out))
    return out


def _limit(raw) -> int | None:
    raw = (str(raw) if raw is not None else "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise PlanError("Soft limits are whole numbers (or empty for none).")
    return int(raw)


def save(conn, plan_id: int | None, form: dict) -> int:
    """Create or update a plan from the console form. Stripe is asked about
    every price first; nothing is saved if one is wrong."""
    key = (form.get("key") or "").strip().lower()
    name = (form.get("name") or "").strip()
    if not KEY_RE.match(key):
        raise PlanError("The key is 3–32 characters: lowercase letters, digits and hyphens.")
    if not name:
        raise PlanError("Give the plan a name.")
    other = conn.execute("SELECT id FROM plans WHERE key = ?", (key,)).fetchone()
    if other and other[0] != plan_id:
        raise PlanError(f"Another plan already uses the key {key}.")
    prices = check_prices({u: form.get(f"price_{u}", "") for u in UNIT_LABELS})
    values = (key, name, (form.get("description") or "").strip()[:500], 1 if form.get("is_public") else 0,
              int(form.get("sort_order") or 0), _limit(form.get("max_technicians")), _limit(form.get("max_devices")),
              prices[0].currency, prices[0].interval)
    if plan_id is None:
        plan_id = conn.execute(
            "INSERT INTO plans (key, name, description, is_public, sort_order, max_technicians, max_devices,"
            " currency, interval) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values).lastrowid
    else:
        conn.execute("UPDATE plans SET key = ?, name = ?, description = ?, is_public = ?, sort_order = ?,"
                     " max_technicians = ?, max_devices = ?, currency = ?, interval = ?, updated_at = datetime('now')"
                     " WHERE id = ?", (*values, plan_id))
        conn.execute("DELETE FROM plan_prices WHERE plan_id = ?", (plan_id,))
    for p in prices:
        conn.execute("INSERT INTO plan_prices (plan_id, stripe_price_id, stripe_product_id, unit_type,"
                     " included_quantity, billing_scheme, meter_event_name, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (plan_id, p.price_id, p.product_id, p.unit_type, p.included, p.billing_scheme,
                      p.meter_event_name, p.summary))
    conn.commit()
    return plan_id


def set_archived(conn, plan_id: int, archived: bool) -> None:
    conn.execute("UPDATE plans SET archived_at = CASE WHEN ? THEN datetime('now') END, updated_at = datetime('now')"
                 " WHERE id = ?", (1 if archived else 0, plan_id))
    conn.commit()


def get(conn, plan_id: int):
    return conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()


def prices_of(conn, plan_id: int) -> list:
    return conn.execute("SELECT * FROM plan_prices WHERE plan_id = ? ORDER BY CASE unit_type WHEN 'flat' THEN 0"
                        " WHEN 'technician' THEN 1 ELSE 2 END", (plan_id,)).fetchall()


def all_plans(conn) -> list:
    return conn.execute("SELECT * FROM plans ORDER BY archived_at IS NOT NULL, sort_order, id").fetchall()


def public_plans(conn) -> list[dict]:
    """Plans offered on the pricing section and the sign-up form, in order."""
    rows = conn.execute("SELECT * FROM plans WHERE is_public = 1 AND archived_at IS NULL ORDER BY sort_order, id")
    return [{**dict(r), "prices": [dict(p) for p in prices_of(conn, r["id"])]} for r in rows.fetchall()]


def offered(conn, plan_id) -> dict | None:
    """A public, unarchived plan by id (what sign-up may choose), or None."""
    try:
        pid = int(plan_id)
    except (TypeError, ValueError):
        return None
    return next((p for p in public_plans(conn) if p["id"] == pid), None)
