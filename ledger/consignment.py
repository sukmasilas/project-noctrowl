"""Consignor payout calculators.

Both functions here are pure *suggestions* — meant to be called (by a test,
or eventually a review-queue UI) to show a human a proposed number before
they confirm it. Per CLAUDE.md's Core accounting rules, the posting engine
(ledger/posting.py) never calls these itself to auto-fill a liability
amount; ledger/schema.py's consignment_sales table structurally enforces
this too (journal_entry_id can never be set while confirmed_at is NULL).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.schema import consignor_payout_tiers

TWO_PLACES = Decimal("0.01")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class TierLookupResult:
    rate_percent: Decimal | None
    requires_manual_contact: bool
    tier_id: int | None


def lookup_tier_rate(conn: Connection, item_price_usd: Decimal) -> TierLookupResult:
    """Look up the Pasal 3 tier rate for an item price (USD, excluding
    shipping) from the admin-editable consignor_payout_tiers table.

    Returns rate_percent=None and requires_manual_contact=True for the
    $7,500+ tier (or any price with no matching tier row) — this must never
    be auto-applied by a caller.
    """
    if item_price_usd < Decimal("0.99"):
        return TierLookupResult(rate_percent=None, requires_manual_contact=True, tier_id=None)

    rows = conn.execute(
        select(
            consignor_payout_tiers.c.id,
            consignor_payout_tiers.c.min_price_usd,
            consignor_payout_tiers.c.max_price_usd,
            consignor_payout_tiers.c.rate_percent,
            consignor_payout_tiers.c.requires_manual_contact,
        ).order_by(consignor_payout_tiers.c.display_order)
    ).all()

    for row in rows:
        if row.min_price_usd <= item_price_usd and (
            row.max_price_usd is None or item_price_usd <= row.max_price_usd
        ):
            if row.requires_manual_contact or row.rate_percent is None:
                return TierLookupResult(rate_percent=None, requires_manual_contact=True, tier_id=row.id)
            return TierLookupResult(rate_percent=row.rate_percent, requires_manual_contact=False, tier_id=row.id)

    return TierLookupResult(rate_percent=None, requires_manual_contact=True, tier_id=None)


def calc_tier_payout_usd(item_price_usd: Decimal, rate_percent: Decimal) -> Decimal:
    """Suggested payout under the Pasal 3 tier model: item price (excluding
    shipping) x tier rate. ``rate_percent`` is a percentage (e.g. 72.00 for
    72%). Rounded to cents.
    """
    return (item_price_usd * (rate_percent / HUNDRED)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def calc_experimental_payout_usd(
    gross_sale_price_usd: Decimal, ebay_fee_usd: Decimal, shipping_cost_usd: Decimal
) -> Decimal:
    """Suggested payout under the experimental ('net_of_fees_and_shipping')
    model: gross sale price minus eBay selling fees minus shipping cost —
    the consignor absorbs both.
    """
    return (gross_sale_price_usd - ebay_fee_usd - shipping_cost_usd).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
