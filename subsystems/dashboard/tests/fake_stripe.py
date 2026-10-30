"""An in-memory Stripe for the tests, plugged in at the HTTP boundary
(app.stripe_api.HTTP_CLIENT): the real stripe-python client builds and sends
real requests, and this answers them. No network.

Only what the dashboard uses is modelled. Tests read and change the store
directly (fake.subscriptions[sid]["status"] = "past_due") and can count the
requests (fake.calls).
"""
from __future__ import annotations

import itertools
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit

import stripe
from stripe._http_client import HTTPClient

_ids = itertools.count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}_test{next(_ids):06d}"


def decode(form: str) -> dict:
    """Stripe's form encoding (a[b][0][c]=x) back into dicts and lists."""
    out: dict = {}
    for key, value in parse_qsl(form or "", keep_blank_values=True):
        parts = re.findall(r"[^\[\]]+", key)
        node = out
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            nxt = None if last else parts[i + 1]
            if isinstance(node, list):
                idx = int(part)
                while len(node) <= idx:
                    node.append({} if nxt is not None and not nxt.isdigit() else [] if nxt is not None else None)
                if last:
                    node[idx] = value
                else:
                    if node[idx] is None:
                        node[idx] = [] if nxt.isdigit() else {}
                    node = node[idx]
            else:
                if last:
                    node[part] = value
                else:
                    node = node.setdefault(part, [] if nxt.isdigit() else {})
    return out


def _bool(v) -> bool:
    return str(v).lower() in ("true", "1")


class FakeStripe(HTTPClient):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str, dict]] = []
        self.customers: dict[str, dict] = {}
        self.prices: dict[str, dict] = {}
        self.products: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.payment_methods: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.portal_sessions: list[dict] = []
        self.portal_configurations: dict[str, dict] = {}
        self.meter_events: dict[str, dict] = {}
        self.meters: dict[str, dict] = {}
        self.test_clocks: dict[str, dict] = {}
        self.coupons: dict[str, dict] = {"FRIENDS": {"id": "FRIENDS", "object": "coupon", "valid": True}}
        self.fail: dict[str, tuple[int, str]] = {}      # path prefix -> (status, message)
        self.versions: set[str] = set()

    # ── Helpers for tests ───────────────────────────────────────────────────
    def add_price(self, unit_amount=1000, currency="usd", interval="month", interval_count=1, usage_type="licensed",
                  active=True, tiers=None, meter=None, product_name="RustDesk Fleet") -> str:
        prod = new_id("prod")
        self.products[prod] = {"id": prod, "object": "product", "name": product_name, "active": True}
        pid = new_id("price")
        self.prices[pid] = {
            "id": pid, "object": "price", "active": active, "currency": currency, "product": prod,
            "unit_amount": None if tiers else unit_amount, "type": "recurring",
            "billing_scheme": "tiered" if tiers else "per_unit", "tiers_mode": "graduated" if tiers else None,
            "tiers": tiers, "recurring": {"interval": interval, "interval_count": interval_count,
                                          "usage_type": usage_type, "meter": meter},
            "nickname": None,
        }
        return pid

    def add_meter(self, event_name: str = "rdf_devices") -> str:
        mid = new_id("mtr")
        self.meters[mid] = {"id": mid, "object": "billing.meter", "event_name": event_name,
                            "default_aggregation": {"formula": "sum"}}
        return mid

    def add_card(self, customer: str, fingerprint: str = "fp_card_1", default: bool = True, kind: str = "card") -> str:
        pm = new_id("pm")
        self.payment_methods[pm] = {"id": pm, "object": "payment_method", "type": kind, "customer": customer,
                                    "card": {"fingerprint": fingerprint, "brand": "visa", "last4": "4242"}
                                    if kind == "card" else None}
        if default:
            self.customers[customer].setdefault("invoice_settings", {})["default_payment_method"] = pm
        return pm

    def complete_checkout(self, session_id: str, fingerprint: str = "fp_card_1", card: bool = True) -> str:
        """What Stripe does when the customer pays: a subscription (trialing),
        the card attached and made the subscription's default."""
        s = self.sessions[session_id]
        cus = s["customer"]
        pm = self.add_card(cus, fingerprint, default=False) if card else None
        sd = s.get("subscription_data") or {}
        trial_days = int(sd.get("trial_period_days") or 0)
        items = []
        for li in s["line_items"]:
            price = self.prices[li["price"]]
            item = {"id": new_id("si"), "object": "subscription_item", "price": price,
                    "current_period_end": int(time.time()) + 30 * 86400}
            if price["recurring"]["usage_type"] != "metered":
                item["quantity"] = int(li.get("quantity") or 1)
            items.append(item)
        sid = new_id("sub")
        now = int(time.time())
        self.subscriptions[sid] = {
            "id": sid, "object": "subscription", "customer": cus,
            "status": "trialing" if trial_days else "active",
            "trial_end": now + trial_days * 86400 if trial_days else None,
            "trial_settings": sd.get("trial_settings"),
            "default_payment_method": pm, "cancel_at_period_end": False, "cancel_at": None, "canceled_at": None,
            "items": {"object": "list", "data": items}, "metadata": dict(sd.get("metadata") or {}),
            "latest_invoice": None, "discounts": [],
        }
        s.update(status="complete", subscription=sid, payment_status="no_payment_required")
        return sid

    def sub(self, sid: str) -> dict:
        return self.subscriptions[sid]

    def requests(self, method: str, prefix: str) -> list[dict]:
        return [p for m, path, p in self.calls if m == method and path.startswith(prefix)]

    # ── The HTTP boundary ───────────────────────────────────────────────────
    def request(self, method, url, headers, post_data=None, *, _usage=None):
        parts = urlsplit(url)
        path = parts.path
        params = decode(post_data if isinstance(post_data, str) else (post_data or b"").decode()
                        if post_data else "")
        params.update(decode(parts.query))
        self.calls.append((method.upper(), path, params))
        self.versions.add((headers or {}).get("Stripe-Version", ""))
        for prefix, (code, msg) in self.fail.items():
            if path.startswith(prefix):
                return self._error(code, msg)
        try:
            body = self._route(method.upper(), path, params)
        except KeyError as e:
            return self._error(404, f"No such object: {e}")
        except ValueError as e:
            return self._error(400, str(e))
        return json.dumps(body), 200, {"Request-Id": "req_fake"}

    def request_stream(self, *a, **k):
        raise NotImplementedError

    def close(self):
        pass

    def _error(self, code, msg):
        return json.dumps({"error": {"message": msg, "type": "invalid_request_error"}}), code, {}

    def _expand(self, obj: dict, expand: list) -> dict:
        obj = json.loads(json.dumps(obj))
        # Like Stripe: a price's tiers are only included when expanded.
        if obj.get("object") == "price" and "tiers" not in (expand or []):
            obj["tiers"] = None
        if obj.get("object") == "subscription" and "items.data.price.tiers" not in (expand or []):
            for item in (obj.get("items") or {}).get("data", []):
                if isinstance(item.get("price"), dict):
                    item["price"]["tiers"] = None
        for path in expand or []:
            keys = path.split(".")
            self._expand_path(obj, keys)
        return obj

    def _expand_path(self, node, keys):
        if not keys or node is None:
            return
        k, rest = keys[0], keys[1:]
        if k == "data" and isinstance(node, dict) and isinstance(node.get("data"), list):
            for item in node["data"]:
                self._expand_path(item, rest)
            return
        if not isinstance(node, dict) or k not in node:
            return
        v = node[k]
        if isinstance(v, str) and not rest:
            node[k] = self._lookup(v)
        elif rest:
            if isinstance(v, str):
                node[k] = v = self._lookup(v)
            self._expand_path(v, rest)

    def _lookup(self, oid: str):
        for store in (self.payment_methods, self.customers, self.subscriptions, self.prices, self.products,
                      self.invoices):
            if oid in store:
                return store[oid]
        return oid

    def _route(self, method, path, p):
        seg = path.strip("/").split("/")[1:]       # drop "v1"
        expand = p.get("expand") or []
        if seg[0] == "prices" and method == "GET":
            return self._expand(self.prices[seg[1]], expand)
        if seg[0] == "customers":
            if method == "POST" and len(seg) == 1:
                cid = new_id("cus")
                self.customers[cid] = {"id": cid, "object": "customer", "email": p.get("email"),
                                       "name": p.get("name"), "metadata": p.get("metadata") or {},
                                       "invoice_settings": {"default_payment_method": None}}
                return self.customers[cid]
            if method == "POST":
                c = self.customers[seg[1]]
                for k, v in p.items():
                    if k == "invoice_settings":
                        c.setdefault("invoice_settings", {}).update(v)
                    elif k != "expand":
                        c[k] = v
                return self._expand(c, expand)
            return self._expand(self.customers[seg[1]], expand)
        if seg[0] == "checkout" and seg[1] == "sessions":
            if method == "POST" and len(seg) == 2:
                if p["customer"] not in self.customers:
                    raise KeyError(p["customer"])
                for li in p.get("line_items", []):
                    if li["price"] not in self.prices:
                        raise KeyError(li["price"])
                sid = new_id("cs")
                self.sessions[sid] = {"id": sid, "object": "checkout.session", "status": "open",
                                      "url": f"https://checkout.stripe.example/c/{sid}", "subscription": None,
                                      **p}
                return self.sessions[sid]
            return self._expand(self.sessions[seg[2]], expand)
        if seg[0] == "subscriptions":
            if method == "GET" and len(seg) == 1:
                data = [s for s in self.subscriptions.values() if s["customer"] == p.get("customer")]
                return {"object": "list", "data": data, "has_more": False}
            s = self.subscriptions[seg[1]]
            if method == "DELETE":
                s.update(status="canceled", canceled_at=int(time.time()))
                return s
            if method == "POST":
                if "trial_end" in p:
                    s["trial_end"] = int(time.time()) if p["trial_end"] == "now" else int(p["trial_end"])
                    if p["trial_end"] == "now" and s["status"] == "trialing":
                        s["status"] = "active"
                if "cancel_at_period_end" in p:
                    s["cancel_at_period_end"] = _bool(p["cancel_at_period_end"])
                if "discounts" in p:
                    for d in p["discounts"]:
                        if d.get("coupon") not in self.coupons:
                            raise ValueError(f"No such coupon: {d.get('coupon')}")
                    s["discounts"] = p["discounts"]
                for item in p.get("items", []):
                    if _bool(item.get("deleted")):
                        s["items"]["data"] = [i for i in s["items"]["data"] if i["id"] != item["id"]]
                    elif item.get("id"):
                        for i in s["items"]["data"]:
                            if i["id"] == item["id"] and "quantity" in item:
                                i["quantity"] = int(item["quantity"])
                    else:
                        price = self.prices[item["price"]]
                        new = {"id": new_id("si"), "object": "subscription_item", "price": price,
                               "current_period_end": int(time.time()) + 30 * 86400}
                        if price["recurring"]["usage_type"] != "metered":
                            new["quantity"] = int(item.get("quantity") or 1)
                        s["items"]["data"].append(new)
                if "metadata" in p:
                    s["metadata"].update(p["metadata"])
                s["last_update"] = p
                return self._expand(s, expand)
            return self._expand(s, expand)
        if seg[0] == "payment_methods":
            return self._expand(self.payment_methods[seg[1]], expand)
        if seg[0] == "billing_portal" and seg[1] == "sessions":
            sess = {"id": new_id("bps"), "object": "billing_portal.session", "customer": p["customer"],
                    "url": f"https://billing.stripe.example/p/{p['customer']}", "return_url": p.get("return_url"),
                    "configuration": p.get("configuration")}
            self.portal_sessions.append(sess)
            return sess
        if seg[0] == "billing_portal" and seg[1] == "configurations":
            if method == "POST" and len(seg) == 2:
                cid = new_id("bpc")
                self.portal_configurations[cid] = {"id": cid, "object": "billing_portal.configuration", **p}
                return self.portal_configurations[cid]
            if method == "POST":
                self.portal_configurations[seg[2]].update(p)
                return self.portal_configurations[seg[2]]
            return self.portal_configurations[seg[2]]
        if seg[0] == "invoices":
            if seg[-1] == "create_preview":
                sub = self.subscriptions.get(p.get("subscription", ""))
                if sub and (sub.get("cancel_at") or sub.get("cancel_at_period_end")):
                    raise ValueError(f"No upcoming invoices for customer: {p.get('customer')}")
                total = sum((i["price"].get("unit_amount") or 0) * i.get("quantity", 0)
                            for i in (sub["items"]["data"] if sub else []))
                return {"object": "invoice", "amount_due": total, "total": total, "currency": "usd",
                        "next_payment_attempt": int(time.time()) + 86400, "period_end": int(time.time()) + 86400}
            if method == "GET" and len(seg) == 1:
                data = [i for i in self.invoices.values() if i["customer"] == p.get("customer")]
                return {"object": "list", "data": data, "has_more": False}
            return self.invoices[seg[1]]
        if seg[0] == "billing" and seg[1] == "meters":
            return self.meters[seg[2]]
        if seg[0] == "billing" and seg[1] == "meter_events":
            ident = p.get("identifier") or new_id("mev")
            if ident in self.meter_events:
                raise ValueError("An event with this identifier already exists")
            self.meter_events[ident] = p
            return {"object": "billing.meter_event", **p}
        if seg[0] == "test_helpers" and seg[1] == "test_clocks":
            return self.test_clocks[seg[2]]
        if seg[0] == "coupons":
            return self.coupons[seg[1]]
        raise KeyError(path)


def install(monkeypatch) -> FakeStripe:
    """Point the app at a fresh fake Stripe, with billing configured."""
    from app import stripe_api
    fake = FakeStripe()
    monkeypatch.setattr(stripe_api, "HTTP_CLIENT", fake)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_fake")
    return fake


def signed(payload: dict, secret: str = "whsec_test_fake", at: int | None = None) -> tuple[bytes, str]:
    """A webhook body and its Stripe-Signature header."""
    body = json.dumps(payload).encode()
    ts = int(at if at is not None else time.time())
    sig = stripe.WebhookSignature._compute_signature(f"{ts}.{body.decode()}", secret)
    return body, f"t={ts},v1={sig}"


def event(etype: str, obj: dict, eid: str | None = None, previous: dict | None = None) -> dict:
    data = {"object": obj}
    if previous:
        data["previous_attributes"] = previous
    return {"id": eid or new_id("evt"), "object": "event", "type": etype, "api_version": "2026-09-30.endive",
            "created": int(time.time()), "data": data}
