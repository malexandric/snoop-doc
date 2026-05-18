"""
Stripe live sync.

Pulls Stripe data into `data/tables/stripe_*.csv` so the agent can answer
finance questions (MRR / ARR / churn / cohorts / cash collections) against
real subscription data. Same "materialise then query" pattern as the
Google Sheets sync — synced CSVs are indistinguishable from manually
uploaded ones once they're on disk.

Auth
----
Single restricted API key per Snoop install, stored in `~/.snoop-doc/
settings.json` under `stripe_api_key`. Same model as the Anthropic key —
per-user, never committed. We never write the key to disk anywhere else.
Recommended Stripe permissions (set when creating the restricted key):
read-only on Customers, Subscriptions, Invoices, Charges, Products,
Prices.

Data model — twelve CSVs
------------------------
Core (high-volume, incremental sync):
- `stripe_customers.csv`            — one row per customer
- `stripe_subscriptions.csv`        — one row per subscription
- `stripe_subscription_items.csv`   — one row per (sub, price) pair
- `stripe_invoices.csv`             — one row per invoice
- `stripe_invoice_line_items.csv`   — one row per invoice line
- `stripe_payment_intents.csv`      — one row per payment intent (modern Stripe payment object)
- `stripe_charges.csv`              — one row per charge (legacy payment object; we keep this alongside payment_intents for direct comparison — charges has fields PI doesn't, like `amount_refunded` rolled up)
- `stripe_refunds.csv`              — one row per refund
- `stripe_disputes.csv`             — one row per dispute / chargeback
- `stripe_payouts.csv`              — one row per payout to bank

Reference (small, full re-pull each sync):
- `stripe_products.csv`             — product catalog
- `stripe_prices.csv`               — prices for those products
- `stripe_coupons.csv`              — discount definitions
- `stripe_promotion_codes.csv`      — redeemable codes that map to coupons

These join on obvious keys (`customer_id`, `subscription_id`, `invoice_id`,
`charge_id`, `product_id`, `price_id`, `coupon_id`). Document join patterns
in your context docs.

Sync model
----------
Initial pull is expensive (10–25 min for accounts with ~25k subs / ~250k
invoices). After that we keep an incremental cursor per object type and
only pull what's been created since the last successful sync — typically
seconds.

A couple of object types do **full re-pull** every sync because state
changes (e.g. a subscription cancelling, an invoice transitioning to paid)
aren't captured by a `created[gte]` filter:
  - subscriptions, subscription_items, products, prices, coupons,
    promotion_codes

Two object types are produced **as side effects** of their parent's sync
(no separate API endpoint or button):
  - subscription_items — extracted from subscriptions
  - invoice_line_items — extracted from invoices

The append-with-dedupe-on-id logic in `_write_rows` means re-running a
sync is safe and idempotent: existing rows are replaced by the latest
version, new rows are appended.

The Stripe SDK is imported lazily so the rest of the app still works for
users who haven't run `pip install -r requirements.txt` since this dep
was added.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd


PROJECT_ROOT = Path(__file__).parent
REGISTRY_PATH = PROJECT_ROOT / "data" / "stripe-registry.json"
TABLES_DIR = PROJECT_ROOT / "data" / "tables"

# Sync order matters: products + prices first so other objects can be
# interpreted in context; customers before subs/invoices; subs before
# subscription_items. The order doesn't strictly matter for *fetching*
# (Stripe doesn't enforce it) but it gives the user a coherent progress
# story and lets us skip later objects if an earlier one fails.
SYNC_ORDER = (
    # Reference tables first — they're tiny and other objects reference them.
    "products",
    "prices",
    "coupons",
    "promotion_codes",
    # Core data — customers before what they own.
    "customers",
    "subscriptions",
    "subscription_items",   # side effect of subscriptions sync
    "invoices",
    "invoice_line_items",   # side effect of invoices sync
    "payment_intents",
    "charges",
    "refunds",
    "disputes",
    # Bank-facing — pulled last because it's tied to payments/refunds upstream.
    "payouts",
)

# Object keys that are synced as a side effect of their parent's sync,
# so the UI hides their per-object button and `sync_all` skips them.
SIDE_EFFECT_OBJECTS = ("subscription_items", "invoice_line_items")


# ---------------------------------------------------------------------------
# Errors — typed so the UI can show targeted messages
# ---------------------------------------------------------------------------
class StripeSyncError(Exception):
    """Base for everything this module raises."""


class DepsMissingError(StripeSyncError):
    """The `stripe` package isn't installed."""


class NotConfiguredError(StripeSyncError):
    """No API key in session state."""


class AuthError(StripeSyncError):
    """Stripe rejected the API key."""


# ---------------------------------------------------------------------------
# Lazy SDK import
# ---------------------------------------------------------------------------
def _import_stripe():
    try:
        import stripe  # noqa: F401
    except ImportError as e:
        raise DepsMissingError(
            "The `stripe` package isn't installed. Run "
            "`pip install -r requirements.txt` to add it."
        ) from e
    return stripe


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def validate_key(api_key: str) -> tuple[bool, str]:
    """Quick API ping to check the key works. Returns (ok, message)."""
    if not api_key:
        return False, "No API key set."
    try:
        stripe = _import_stripe()
    except DepsMissingError as e:
        return False, str(e)
    stripe.api_key = api_key
    try:
        # The cheapest read-only call we can make. Asking for limit=1 means
        # the response is tiny regardless of account size.
        stripe.Customer.list(limit=1)
    except stripe.error.AuthenticationError as e:
        return False, f"Authentication failed: {e}"
    except stripe.error.PermissionError as e:
        return False, f"Permission denied: {e}"
    except stripe.error.StripeError as e:
        return False, f"Stripe error: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"Unexpected error: {e}"
    return True, "Connection OK."


# ---------------------------------------------------------------------------
# Registry — `data/stripe-registry.json`, committed
# ---------------------------------------------------------------------------
def load_registry() -> dict[str, Any]:
    """Returns `{"objects": {<key>: {"last_synced_at": ISO, "last_sync_error":
    str|None, "row_count": int}, ...}}`. Empty if no file."""
    if not REGISTRY_PATH.exists():
        return {"objects": {}}
    try:
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"objects": {}}
    if "objects" not in raw:
        raw["objects"] = {}
    return raw


def save_registry(reg: dict[str, Any]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def get_object_status(key: str) -> dict | None:
    return load_registry().get("objects", {}).get(key)


def _update_object_status(key: str, **fields) -> None:
    reg = load_registry()
    existing = reg["objects"].get(key, {})
    existing.update(fields)
    reg["objects"][key] = existing
    save_registry(reg)


def filename_for(object_key: str) -> str:
    return f"stripe_{object_key}.csv"


def is_stripe_synced_file(filename: str) -> str | None:
    """Return the object key (e.g. `"customers"`) if `filename` matches a
    registered Stripe sync; else None. Used by the Data view's per-row UI.

    Skips registry entries whose key is no longer in `SPECS` — those are
    stale leftovers from a previous schema version (e.g. `charges` after
    we swapped to `payment_intents`). Returning None for them means the
    file will render as a regular non-synced CSV, and the user can
    delete it via the normal Delete button without the UI crashing on
    lookup."""
    reg = load_registry()
    for key in reg.get("objects", {}):
        if filename_for(key) == filename and key in SPECS:
            return key
    return None


# ---------------------------------------------------------------------------
# Flatteners — each Stripe object → a flat dict ready for CSV. Keep the
# fields tight; we can add more later if the agent asks for them.
# ---------------------------------------------------------------------------
def _ts_to_iso(ts: int | None) -> str | None:
    """Stripe returns Unix timestamps for date fields. ISO-8601 reads
    better in CSV and pandas parses it without a custom format string."""
    if ts is None or ts == 0:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _id_of(ref) -> str | None:
    """Normalise a Stripe reference field to a string id (or None).

    A reference like `pi.invoice` or `c.customer` can take three shapes
    in a Stripe SDK response:
      - a string id ("in_abc123")
      - a nested StripeObject (when expanded)
      - missing entirely (the attribute isn't on the object at all)

    Use this anywhere a flattener pulls a reference id out of a parent
    object. Pair with `getattr(parent, "field", None)` so the
    "missing entirely" case doesn't raise AttributeError.
    """
    if ref is None:
        return None
    if isinstance(ref, str):
        return ref
    return getattr(ref, "id", None)


def _flatten_customer(c) -> dict:
    addr = getattr(c, "address", None) or {}
    return {
        "id": c.id,
        "email": getattr(c, "email", None),
        "name": getattr(c, "name", None),
        "created": _ts_to_iso(getattr(c, "created", None)),
        "currency": getattr(c, "currency", None),
        "country": addr.get("country") if isinstance(addr, dict) else getattr(addr, "country", None),
        "delinquent": getattr(c, "delinquent", None),
        "balance": getattr(c, "balance", None),
        "default_payment_method_id": getattr(getattr(c, "invoice_settings", None), "default_payment_method", None),
    }


def _flatten_subscription(s) -> dict:
    return {
        "id": s.id,
        "customer_id": s.customer if isinstance(s.customer, str) else s.customer.id,
        "status": s.status,
        "created": _ts_to_iso(s.created),
        "start_date": _ts_to_iso(s.start_date),
        "current_period_start": _ts_to_iso(getattr(s, "current_period_start", None)),
        "current_period_end": _ts_to_iso(getattr(s, "current_period_end", None)),
        "cancel_at": _ts_to_iso(getattr(s, "cancel_at", None)),
        "canceled_at": _ts_to_iso(getattr(s, "canceled_at", None)),
        "cancel_at_period_end": getattr(s, "cancel_at_period_end", None),
        "ended_at": _ts_to_iso(getattr(s, "ended_at", None)),
        "trial_start": _ts_to_iso(getattr(s, "trial_start", None)),
        "trial_end": _ts_to_iso(getattr(s, "trial_end", None)),
        "collection_method": getattr(s, "collection_method", None),
        "currency": getattr(s, "currency", None),
        "default_payment_method_id": getattr(s, "default_payment_method", None),
        "discount_id": (getattr(getattr(s, "discount", None), "id", None)
                        if getattr(s, "discount", None) else None),
    }


def _flatten_subscription_items(s) -> list[dict]:
    """Stripe returns sub items nested under `.items.data`. We emit one
    row per item — that's the right grain for joining with products
    and answering multi-product questions."""
    items = getattr(s, "items", None)
    if items is None:
        return []
    out: list[dict] = []
    for it in items.data:
        price = getattr(it, "price", None)
        out.append({
            "id": it.id,
            "subscription_id": s.id,
            "price_id": price.id if price else None,
            "product_id": getattr(price, "product", None) if price else None,
            "quantity": getattr(it, "quantity", None),
            "created": _ts_to_iso(getattr(it, "created", None)),
        })
    return out


def _flatten_invoice(inv) -> dict:
    transitions = getattr(inv, "status_transitions", None) or {}
    def _tr(field):
        if isinstance(transitions, dict):
            return _ts_to_iso(transitions.get(field))
        return _ts_to_iso(getattr(transitions, field, None))
    return {
        "id": inv.id,
        "customer_id": inv.customer if isinstance(inv.customer, str) else (inv.customer.id if inv.customer else None),
        "subscription_id": getattr(inv, "subscription", None),
        "status": inv.status,
        "created": _ts_to_iso(inv.created),
        "period_start": _ts_to_iso(getattr(inv, "period_start", None)),
        "period_end": _ts_to_iso(getattr(inv, "period_end", None)),
        "due_date": _ts_to_iso(getattr(inv, "due_date", None)),
        "paid_at": _tr("paid_at"),
        "finalized_at": _tr("finalized_at"),
        "voided_at": _tr("voided_at"),
        "total": getattr(inv, "total", None),
        "amount_paid": getattr(inv, "amount_paid", None),
        "amount_due": getattr(inv, "amount_due", None),
        "amount_remaining": getattr(inv, "amount_remaining", None),
        "currency": getattr(inv, "currency", None),
        "attempt_count": getattr(inv, "attempt_count", None),
        "next_payment_attempt": _ts_to_iso(getattr(inv, "next_payment_attempt", None)),
        "collection_method": getattr(inv, "collection_method", None),
        "billing_reason": getattr(inv, "billing_reason", None),
    }


def _flatten_payment_intent(pi) -> dict:
    """Flatten a Stripe PaymentIntent object.

    PaymentIntents replaced Charges as the canonical payment object on
    modern Stripe accounts. They carry the same money-movement info
    plus the payment lifecycle state (requires_payment_method,
    requires_confirmation, processing, succeeded, canceled, etc.).

    Lost vs the old Charge flattener: `amount_refunded` and `refunded`
    (PaymentIntents don't carry refund totals directly — derive via
    `stripe_refunds.csv` joined on `payment_intent_id`) and `disputed`
    (derive via `stripe_disputes.csv` joined on `payment_intent_id`).

    Every optional attribute uses `getattr` with a default — the Stripe
    SDK doesn't reliably populate fields like `invoice` or `customer`
    on PaymentIntents that aren't tied to one (one-off payments, direct
    PaymentIntent creation, guest checkouts). Direct attribute access
    raises AttributeError in those cases.
    """
    # last_payment_error is nested; pull out code + message
    err = getattr(pi, "last_payment_error", None) or {}
    if isinstance(err, dict):
        err_code = err.get("code")
        err_message = err.get("message")
    else:
        err_code = getattr(err, "code", None)
        err_message = getattr(err, "message", None)

    # payment_method_types is a list of strings (e.g. ["card", "us_bank_account"]);
    # for analysis the first entry is usually enough.
    pm_types = getattr(pi, "payment_method_types", None) or []
    pm_type = pm_types[0] if pm_types else None

    return {
        "id": pi.id,
        "customer_id": _id_of(getattr(pi, "customer", None)),
        "invoice_id": _id_of(getattr(pi, "invoice", None)),
        "amount": getattr(pi, "amount", None),
        "amount_received": getattr(pi, "amount_received", None),
        "amount_capturable": getattr(pi, "amount_capturable", None),
        "status": getattr(pi, "status", None),
        "created": _ts_to_iso(getattr(pi, "created", None)),
        "canceled_at": _ts_to_iso(getattr(pi, "canceled_at", None)),
        "cancellation_reason": getattr(pi, "cancellation_reason", None),
        "last_payment_error_code": err_code,
        "last_payment_error_message": err_message,
        "payment_method_type": pm_type,
        "payment_method_id": _id_of(getattr(pi, "payment_method", None)),
        "currency": getattr(pi, "currency", None),
        "description": getattr(pi, "description", None),
    }


def _flatten_charge(c) -> dict:
    """Flatten a Stripe Charge object.

    Charge is Stripe's legacy payment object. We keep it alongside
    PaymentIntents (which is what modern accounts use) because:
      - Charges has rolled-up convenience fields PI doesn't expose:
        `amount_refunded` (sum across refunds), `refunded` (bool),
        `disputed` (bool), `paid` (bool), `failure_code` / `_message`.
      - Direct comparison helps verify the two sources agree.
      - For older accounts / certain flows, Charges has rows that
        don't appear in PaymentIntents.

    Includes `payment_intent_id` so the two CSVs join 1:1 where both
    are present.
    """
    pm_details = getattr(c, "payment_method_details", None) or {}
    pm_type = (pm_details.get("type") if isinstance(pm_details, dict)
               else getattr(pm_details, "type", None))
    return {
        "id": c.id,
        "customer_id": _id_of(getattr(c, "customer", None)),
        "invoice_id": _id_of(getattr(c, "invoice", None)),
        "payment_intent_id": _id_of(getattr(c, "payment_intent", None)),
        "amount": getattr(c, "amount", None),
        "amount_captured": getattr(c, "amount_captured", None),
        "amount_refunded": getattr(c, "amount_refunded", None),
        "status": getattr(c, "status", None),
        "paid": getattr(c, "paid", None),
        "refunded": getattr(c, "refunded", None),
        "created": _ts_to_iso(getattr(c, "created", None)),
        "failure_code": getattr(c, "failure_code", None),
        "failure_message": getattr(c, "failure_message", None),
        "payment_method_type": pm_type,
        "currency": getattr(c, "currency", None),
        "disputed": getattr(c, "disputed", None),
    }


def _flatten_product(p) -> dict:
    return {
        "id": p.id,
        "name": getattr(p, "name", None),
        "description": getattr(p, "description", None),
        "active": getattr(p, "active", None),
        "created": _ts_to_iso(p.created),
        "updated": _ts_to_iso(getattr(p, "updated", None)),
    }


def _flatten_refund(r) -> dict:
    return {
        "id": r.id,
        "charge_id": r.charge if isinstance(r.charge, str) else (r.charge.id if r.charge else None),
        "payment_intent_id": getattr(r, "payment_intent", None),
        "amount": r.amount,
        "currency": getattr(r, "currency", None),
        "status": getattr(r, "status", None),
        "reason": getattr(r, "reason", None),
        "created": _ts_to_iso(r.created),
        "receipt_number": getattr(r, "receipt_number", None),
        "balance_transaction": getattr(r, "balance_transaction", None),
        "description": getattr(r, "description", None),
    }


def _flatten_dispute(d) -> dict:
    ev = getattr(d, "evidence_details", None) or {}
    evidence_due_by = (ev.get("due_by") if isinstance(ev, dict)
                       else getattr(ev, "due_by", None))
    return {
        "id": d.id,
        "charge_id": d.charge if isinstance(d.charge, str) else (d.charge.id if d.charge else None),
        "payment_intent_id": getattr(d, "payment_intent", None),
        "amount": d.amount,
        "currency": getattr(d, "currency", None),
        "reason": getattr(d, "reason", None),
        "status": getattr(d, "status", None),
        "created": _ts_to_iso(d.created),
        "evidence_due_by": _ts_to_iso(evidence_due_by),
        "is_charge_refundable": getattr(d, "is_charge_refundable", None),
        "network_reason_code": getattr(d, "network_reason_code", None),
        "balance_transaction": getattr(d, "balance_transaction", None),
    }


def _flatten_coupon(c) -> dict:
    return {
        "id": c.id,
        "name": getattr(c, "name", None),
        "percent_off": getattr(c, "percent_off", None),
        "amount_off": getattr(c, "amount_off", None),
        "currency": getattr(c, "currency", None),
        "duration": getattr(c, "duration", None),
        "duration_in_months": getattr(c, "duration_in_months", None),
        "max_redemptions": getattr(c, "max_redemptions", None),
        "times_redeemed": getattr(c, "times_redeemed", None),
        "valid": getattr(c, "valid", None),
        "created": _ts_to_iso(c.created),
        "redeem_by": _ts_to_iso(getattr(c, "redeem_by", None)),
    }


def _flatten_promotion_code(pc) -> dict:
    coupon = getattr(pc, "coupon", None)
    coupon_id = coupon.id if coupon and not isinstance(coupon, str) else coupon
    restrictions = getattr(pc, "restrictions", None) or {}
    return {
        "id": pc.id,
        "code": getattr(pc, "code", None),
        "coupon_id": coupon_id,
        "customer_id": getattr(pc, "customer", None),
        "active": getattr(pc, "active", None),
        "expires_at": _ts_to_iso(getattr(pc, "expires_at", None)),
        "times_redeemed": getattr(pc, "times_redeemed", None),
        "max_redemptions": getattr(pc, "max_redemptions", None),
        "first_time_transaction": (restrictions.get("first_time_transaction") if isinstance(restrictions, dict)
                                   else getattr(restrictions, "first_time_transaction", None)),
        "minimum_amount": (restrictions.get("minimum_amount") if isinstance(restrictions, dict)
                           else getattr(restrictions, "minimum_amount", None)),
        "created": _ts_to_iso(pc.created),
    }


def _flatten_payout(p) -> dict:
    return {
        "id": p.id,
        "amount": p.amount,
        "currency": getattr(p, "currency", None),
        "status": getattr(p, "status", None),
        "type": getattr(p, "type", None),
        "method": getattr(p, "method", None),
        "source_type": getattr(p, "source_type", None),
        "destination": getattr(p, "destination", None),
        "automatic": getattr(p, "automatic", None),
        "arrival_date": _ts_to_iso(getattr(p, "arrival_date", None)),
        "created": _ts_to_iso(p.created),
        "statement_descriptor": getattr(p, "statement_descriptor", None),
        "balance_transaction": getattr(p, "balance_transaction", None),
        "failure_code": getattr(p, "failure_code", None),
        "failure_message": getattr(p, "failure_message", None),
    }


def _flatten_invoice_line_item(li, invoice_id: str) -> dict:
    price = getattr(li, "price", None)
    price_id = price.id if price and not isinstance(price, str) else price
    product_id = (getattr(price, "product", None) if price and not isinstance(price, str) else None)
    # The `period` field is a small object with start + end Unix timestamps.
    period = getattr(li, "period", None)
    if isinstance(period, dict):
        period_start = _ts_to_iso(period.get("start"))
        period_end = _ts_to_iso(period.get("end"))
    elif period is not None:
        period_start = _ts_to_iso(getattr(period, "start", None))
        period_end = _ts_to_iso(getattr(period, "end", None))
    else:
        period_start = period_end = None
    return {
        "id": li.id,
        "invoice_id": invoice_id,
        # `subscription` field is on lines tied to subscription items; one-off
        # invoice items don't have it.
        "subscription_id": getattr(li, "subscription", None),
        "price_id": price_id,
        "product_id": product_id,
        "amount": getattr(li, "amount", None),
        "currency": getattr(li, "currency", None),
        "description": getattr(li, "description", None),
        "quantity": getattr(li, "quantity", None),
        "period_start": period_start,
        "period_end": period_end,
        "proration": getattr(li, "proration", None),
        "type": getattr(li, "type", None),  # "subscription" or "invoiceitem"
    }


def _flatten_price(pr) -> dict:
    recurring = getattr(pr, "recurring", None) or {}
    return {
        "id": pr.id,
        "product_id": pr.product if isinstance(pr.product, str) else (pr.product.id if pr.product else None),
        "nickname": getattr(pr, "nickname", None),
        "currency": getattr(pr, "currency", None),
        "unit_amount": getattr(pr, "unit_amount", None),
        "recurring_interval": (recurring.get("interval") if isinstance(recurring, dict)
                               else getattr(recurring, "interval", None)),
        "recurring_interval_count": (recurring.get("interval_count") if isinstance(recurring, dict)
                                     else getattr(recurring, "interval_count", None)),
        "active": getattr(pr, "active", None),
        "created": _ts_to_iso(pr.created),
    }


# ---------------------------------------------------------------------------
# Object-type specs — declarative table of what each sync does
# ---------------------------------------------------------------------------
@dataclass
class ObjectSpec:
    key: str
    full_repull: bool  # If False, use `created[gte]=last_synced_at` for incremental
    label: str         # Human-readable for the UI


SPECS: dict[str, ObjectSpec] = {
    "products":           ObjectSpec("products",           full_repull=True,  label="Products"),
    "prices":             ObjectSpec("prices",             full_repull=True,  label="Prices"),
    "coupons":            ObjectSpec("coupons",            full_repull=True,  label="Coupons"),
    "promotion_codes":    ObjectSpec("promotion_codes",    full_repull=True,  label="Promotion codes"),
    "customers":          ObjectSpec("customers",          full_repull=False, label="Customers"),
    "subscriptions":      ObjectSpec("subscriptions",      full_repull=True,  label="Subscriptions"),
    "subscription_items": ObjectSpec("subscription_items", full_repull=True,  label="Subscription items"),
    "invoices":           ObjectSpec("invoices",           full_repull=False, label="Invoices"),
    # Line items inherit incremental from invoices (each invoice line's
    # `id` is stable, so append-with-dedupe gives us correct merging).
    "invoice_line_items": ObjectSpec("invoice_line_items", full_repull=False, label="Invoice line items"),
    "payment_intents":    ObjectSpec("payment_intents",    full_repull=False, label="Payment intents"),
    "charges":            ObjectSpec("charges",            full_repull=False, label="Charges"),
    "refunds":            ObjectSpec("refunds",            full_repull=False, label="Refunds"),
    "disputes":           ObjectSpec("disputes",           full_repull=False, label="Disputes"),
    "payouts":            ObjectSpec("payouts",            full_repull=False, label="Payouts"),
}


# ---------------------------------------------------------------------------
# Per-object fetchers — each yields flat dicts ready for the CSV
# ---------------------------------------------------------------------------
def _list_with_created_filter(stripe_cls, since_ts: int | None, **extra_params):
    """Wrap a Stripe `.list().auto_paging_iter()` call with the standard
    incremental filter. `since_ts` is a Unix timestamp; None pulls everything."""
    params = dict(extra_params)
    if since_ts is not None:
        params["created"] = {"gte": int(since_ts)}
    params["limit"] = 100
    return stripe_cls.list(**params).auto_paging_iter()


def _iso_to_unix(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _fetch_object(
    key: str,
    api_key: str,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch a single object type. Returns `(rows, sibling_rows)` —
    `sibling_rows` is non-empty only for subscriptions (which also produce
    `subscription_items` rows in the same pass). Otherwise empty list.
    """
    stripe = _import_stripe()
    stripe.api_key = api_key

    spec = SPECS[key]
    status = get_object_status(key) or {}
    since_ts = None if spec.full_repull else _iso_to_unix(status.get("last_synced_at"))

    rows: list[dict] = []
    sibling_rows: list[dict] = []

    if key == "customers":
        for i, c in enumerate(_list_with_created_filter(stripe.Customer, since_ts)):
            rows.append(_flatten_customer(c))
            if on_progress and (i + 1) % 200 == 0:
                on_progress(i + 1)

    elif key == "subscriptions":
        # Always status='all' — without this Stripe returns only active
        # subs and we lose canceled / past_due / etc. needed for churn.
        for i, s in enumerate(_list_with_created_filter(stripe.Subscription, since_ts, status="all")):
            rows.append(_flatten_subscription(s))
            sibling_rows.extend(_flatten_subscription_items(s))
            if on_progress and (i + 1) % 200 == 0:
                on_progress(i + 1)

    elif key == "subscription_items":
        # No direct Stripe API for listing items across subs — they only
        # come back nested under subscriptions. Re-fetching them here
        # would duplicate the subscriptions pull. So we treat
        # subscription_items as "synced as a side effect of subscriptions"
        # — only stamp the registry, no fetch.
        # (UI hides the per-object button for this; sync_all skips it too.)
        return [], []

    elif key == "invoices":
        for i, inv in enumerate(_list_with_created_filter(stripe.Invoice, since_ts)):
            rows.append(_flatten_invoice(inv))
            # Line items come back nested on the listed invoice. The
            # default `.lines.data` covers the first 10 lines per invoice;
            # most subscription invoices have 1–3 so this is usually
            # complete. For invoices with more lines, `auto_paging_iter()`
            # transparently fetches additional pages from the same
            # endpoint.
            lines = getattr(inv, "lines", None)
            if lines is not None:
                try:
                    for line in lines.auto_paging_iter():
                        sibling_rows.append(_flatten_invoice_line_item(line, inv.id))
                except AttributeError:
                    # Some SDK shapes don't expose auto_paging_iter on the
                    # nested ListObject — fall back to .data which has the
                    # first page only (still useful for most invoices).
                    for line in lines.data:
                        sibling_rows.append(_flatten_invoice_line_item(line, inv.id))
            if on_progress and (i + 1) % 500 == 0:
                on_progress(i + 1)

    elif key == "invoice_line_items":
        # Synced as a side effect of invoices. The orchestration layer
        # writes the sibling rows; this branch is a no-op so a direct
        # `sync_object("invoice_line_items", ...)` call doesn't refetch
        # invoices (would be 5+ minutes of wasted API work).
        return [], []

    elif key == "payment_intents":
        for i, pi in enumerate(_list_with_created_filter(stripe.PaymentIntent, since_ts)):
            rows.append(_flatten_payment_intent(pi))
            if on_progress and (i + 1) % 500 == 0:
                on_progress(i + 1)

    elif key == "charges":
        for i, c in enumerate(_list_with_created_filter(stripe.Charge, since_ts)):
            rows.append(_flatten_charge(c))
            if on_progress and (i + 1) % 500 == 0:
                on_progress(i + 1)

    elif key == "refunds":
        for i, r in enumerate(_list_with_created_filter(stripe.Refund, since_ts)):
            rows.append(_flatten_refund(r))
            if on_progress and (i + 1) % 500 == 0:
                on_progress(i + 1)

    elif key == "disputes":
        for i, d in enumerate(_list_with_created_filter(stripe.Dispute, since_ts)):
            rows.append(_flatten_dispute(d))
            if on_progress and (i + 1) % 200 == 0:
                on_progress(i + 1)

    elif key == "payouts":
        for i, p in enumerate(_list_with_created_filter(stripe.Payout, since_ts)):
            rows.append(_flatten_payout(p))
            if on_progress and (i + 1) % 200 == 0:
                on_progress(i + 1)

    elif key == "products":
        for p in stripe.Product.list(limit=100).auto_paging_iter():
            rows.append(_flatten_product(p))

    elif key == "prices":
        for pr in stripe.Price.list(limit=100).auto_paging_iter():
            rows.append(_flatten_price(pr))

    elif key == "coupons":
        for c in stripe.Coupon.list(limit=100).auto_paging_iter():
            rows.append(_flatten_coupon(c))

    elif key == "promotion_codes":
        for pc in stripe.PromotionCode.list(limit=100).auto_paging_iter():
            rows.append(_flatten_promotion_code(pc))

    else:
        raise StripeSyncError(f"Unknown object type: {key}")

    return rows, sibling_rows


# ---------------------------------------------------------------------------
# CSV write — append-and-dedupe for incremental, replace for full
# ---------------------------------------------------------------------------
def _write_rows(rows: list[dict], path: Path, full_replace: bool) -> int:
    """Write rows to a CSV. For incremental sync (`full_replace=False`),
    merge with whatever's already on disk and keep the last-seen version
    of each `id`. Returns the total row count after write.
    """
    if not rows and not path.exists():
        # First sync + no data → write an empty CSV with no header. The
        # agent's CSV loader will just report 0 rows.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return 0

    new_df = pd.DataFrame(rows) if rows else pd.DataFrame()

    if full_replace or not path.exists():
        combined = new_df
    else:
        try:
            existing = pd.read_csv(path)
        except Exception:  # noqa: BLE001 — empty file or malformed
            existing = pd.DataFrame()
        if existing.empty:
            combined = new_df
        elif new_df.empty:
            combined = existing
        else:
            combined = pd.concat([existing, new_df], ignore_index=True)
            if "id" in combined.columns:
                combined = combined.drop_duplicates(subset=["id"], keep="last")

    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(path, index=False)
    return len(combined)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def sync_object(
    key: str,
    api_key: str,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Sync one object type. Returns `{"ok": bool, "rows": int,
    "error": str|None}`. Registry is updated regardless of success/failure
    so the UI can show the last error.
    """
    if key not in SPECS:
        return {"ok": False, "rows": 0, "error": f"Unknown object: {key}"}
    if not api_key:
        return {"ok": False, "rows": 0, "error": "No Stripe API key set."}

    spec = SPECS[key]
    try:
        rows, sibling_rows = _fetch_object(key, api_key, on_progress=on_progress)
    except DepsMissingError as e:
        _update_object_status(key, last_synced_at=_now_iso(), last_sync_error=str(e))
        return {"ok": False, "rows": 0, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — Stripe SDK has many error subclasses
        msg = _humanize_stripe_error(e)
        _update_object_status(key, last_synced_at=_now_iso(), last_sync_error=msg)
        return {"ok": False, "rows": 0, "error": msg}

    # Primary CSV.
    path = TABLES_DIR / filename_for(key)
    try:
        row_count = _write_rows(rows, path, full_replace=spec.full_repull)
    except Exception as e:  # noqa: BLE001
        _update_object_status(key, last_synced_at=_now_iso(), last_sync_error=f"CSV write failed: {e}")
        return {"ok": False, "rows": 0, "error": f"CSV write failed: {e}"}

    _update_object_status(
        key,
        last_synced_at=_now_iso(),
        last_sync_error=None,
        row_count=row_count,
    )

    # A handful of parent objects produce a sibling CSV from the same
    # fetch pass — subscriptions → subscription_items, invoices →
    # invoice_line_items. The sibling's `full_repull` setting drives how
    # we write (full replace for state-mutable parents like subs, append-
    # with-dedupe for append-mostly parents like invoices).
    PARENT_TO_SIBLING = {
        "subscriptions": "subscription_items",
        "invoices":      "invoice_line_items",
    }
    sibling_key = PARENT_TO_SIBLING.get(key)
    if sibling_key:
        sibling_spec = SPECS[sibling_key]
        sibling_path = TABLES_DIR / filename_for(sibling_key)
        try:
            sibling_count = _write_rows(
                sibling_rows, sibling_path, full_replace=sibling_spec.full_repull
            )
        except Exception as e:  # noqa: BLE001
            _update_object_status(
                sibling_key,
                last_synced_at=_now_iso(),
                last_sync_error=f"CSV write failed: {e}",
            )
        else:
            _update_object_status(
                sibling_key,
                last_synced_at=_now_iso(),
                last_sync_error=None,
                row_count=sibling_count,
            )

    return {"ok": True, "rows": row_count, "error": None}


def sync_all(
    api_key: str,
    on_progress: Callable[[str, int], None] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Sync every object type in `SYNC_ORDER`. Returns a list of
    `(key, result)` tuples in order. Subsequent objects still run if an
    earlier one fails — independent failures shouldn't block the rest.

    `on_progress(object_key, count)` is called periodically during each
    object's fetch with the running row count.
    """
    results: list[tuple[str, dict[str, Any]]] = []
    for key in SYNC_ORDER:
        if key in SIDE_EFFECT_OBJECTS:
            # These are synced as a side effect of their parent's pull
            # (subs → sub_items, invoices → invoice_line_items). Running
            # them standalone would either re-fetch the parent (wasteful)
            # or be a no-op.
            continue

        def _cb(n: int, k=key):
            if on_progress:
                on_progress(k, n)

        result = sync_object(key, api_key, on_progress=_cb)
        results.append((key, result))
    return results


def reset_incremental_cursors() -> int:
    """Null out `last_synced_at` for every object type whose sync uses an
    incremental `created[gte]` filter (i.e. anything with `full_repull=False`
    in `SPECS`). Returns the number of cursors reset.

    Use this when the registry's timestamps reflect a different upstream
    than the current API key — e.g. switching from a test Stripe account
    to a live one. Without a reset, the next incremental sync filters by
    a date from the previous account's history and misses everything
    older than that timestamp on the new account.

    Full-repull objects (subscriptions, products, prices, coupons,
    promotion_codes, subscription_items, invoice_line_items) aren't
    affected — they re-fetch everything on every sync anyway.
    """
    reg = load_registry()
    incremental_keys = {k for k, spec in SPECS.items() if not spec.full_repull}
    count = 0
    for key, status in reg.get("objects", {}).items():
        if key not in incremental_keys:
            continue
        if status.get("last_synced_at") is not None:
            status["last_synced_at"] = None
            status["row_count"] = 0
            count += 1
    save_registry(reg)
    return count


def sync_all_full(
    api_key: str,
    on_progress: Callable[[str, int], None] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Reset every incremental cursor and then run `sync_all`. Forces a
    full re-pull of every object from Stripe — slow on large accounts,
    but the only way to pick up historical data after an API key swap
    (or any time the local cursors are stale relative to upstream)."""
    reset_incremental_cursors()
    return sync_all(api_key, on_progress=on_progress)


def _humanize_stripe_error(e: Exception) -> str:
    """Translate a Stripe SDK exception into a one-line user-friendly
    message. Keeps `AuthenticationError` distinct so the UI can prompt
    the user to fix their key."""
    try:
        stripe = _import_stripe()
    except DepsMissingError:
        return str(e)
    if isinstance(e, stripe.error.AuthenticationError):
        return "Authentication failed — check your Stripe API key in Settings."
    if isinstance(e, stripe.error.PermissionError):
        return f"Permission denied: your restricted key needs read access to this resource. ({e})"
    if isinstance(e, stripe.error.RateLimitError):
        return f"Stripe rate limit hit — wait a minute and retry. ({e})"
    if isinstance(e, stripe.error.APIConnectionError):
        return f"Network error talking to Stripe. ({e})"
    if isinstance(e, stripe.error.StripeError):
        return f"Stripe error: {e}"
    return f"Unexpected error: {e}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Staleness helpers — same shape as gsheets.staleness_* for UI consistency
# ---------------------------------------------------------------------------
def staleness_seconds(key: str) -> int | None:
    status = get_object_status(key)
    if not status or not status.get("last_synced_at"):
        return None
    try:
        synced = datetime.fromisoformat(status["last_synced_at"])
    except ValueError:
        return None
    if synced.tzinfo is None:
        synced = synced.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - synced).total_seconds())


def staleness_band(key: str) -> str:
    s = staleness_seconds(key)
    if s is None:
        return "never"
    if s < 24 * 3600:
        return "fresh"
    if s < 7 * 24 * 3600:
        return "stale"
    return "old"


def staleness_label(key: str) -> str:
    s = staleness_seconds(key)
    if s is None:
        return "never synced"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 24 * 3600:
        return f"{s // 3600} h ago"
    return f"{s // (24 * 3600)} d ago"
