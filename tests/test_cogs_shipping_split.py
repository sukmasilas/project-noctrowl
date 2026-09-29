"""``ledger.posting.post_cogs_purchase_with_shipping_split`` — the new
optional split for a single bundled bank payment that covers both an item
purchase (COGS) and OUTBOUND shipping to a customer (Shipping Cost).

Real trigger: a Master Account bank line, "TRSF E-BANKING DB ... / BANK NEO
COM ...", -Rp 4,140,000, confirmed by the user as Rp 2,140,000 item purchase
+ Rp 2,000,000 outbound shipping bundled into one payment — a recurring
pattern the user confirmed, not a one-off (see CLAUDE.md).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ledger.posting import post_cogs_purchase_with_shipping_split
from tests.helpers import assert_balanced, get_source_type, lines_by_code


def test_split_posts_cogs_and_shipping_lines_credits_full_total(prototype):
    """The real Bank Neo Com case: Rp 4,140,000 total, Rp 2,000,000 shipping
    -> COGS gets the Rp 2,140,000 remainder, Shipping Cost gets Rp 2,000,000,
    and the paying account is credited for the FULL Rp 4,140,000 (the whole
    bundled payment actually left the bank in one real transaction).
    """
    conn, topo = prototype

    entry_id = post_cogs_purchase_with_shipping_split(
        conn,
        entry_date=dt.date(2026, 9, 15),
        amount_idr=Decimal("4140000"),
        shipping_portion_idr=Decimal("2000000"),
        memo="TRSF E-BANKING DB ... / BANK NEO COM ...",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "bank_other"

    lines = lines_by_code(conn, entry_id)
    assert lines["COGS"][0].debit_amount_idr == Decimal("2140000")
    assert lines["SHIPPING_COST"][0].debit_amount_idr == Decimal("2000000")
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("4140000")


def test_split_never_touches_any_other_account(prototype):
    conn, topo = prototype
    entry_id = post_cogs_purchase_with_shipping_split(
        conn,
        entry_date=dt.date(2026, 9, 15),
        amount_idr=Decimal("4140000"),
        shipping_portion_idr=Decimal("2000000"),
    )
    lines = lines_by_code(conn, entry_id)
    assert set(lines.keys()) == {"COGS", "SHIPPING_COST", "BCA_MAIN"}


def test_split_respects_paying_wallet_group(prototype):
    conn, topo = prototype
    entry_id = post_cogs_purchase_with_shipping_split(
        conn,
        entry_date=dt.date(2026, 9, 15),
        amount_idr=Decimal("4140000"),
        shipping_portion_idr=Decimal("2000000"),
        paying_account_type_code="BCA_BRIDGING",
        paying_wallet_group_id=topo["wallet_group_id"],
    )
    lines = lines_by_code(conn, entry_id)
    assert "BCA_MAIN" not in lines
    assert lines["BCA_BRIDGING"][0].credit_amount_idr == Decimal("4140000")


def test_split_threads_usd_reference_on_all_lines(prototype):
    conn, topo = prototype
    entry_id = post_cogs_purchase_with_shipping_split(
        conn,
        entry_date=dt.date(2026, 9, 15),
        amount_idr=Decimal("4140000"),
        shipping_portion_idr=Decimal("2000000"),
        paying_account_type_code="PAYONEER_WALLET",
        paying_wallet_group_id=topo["wallet_group_id"],
        amount_usd_ref=Decimal("270.00"),
        fx_rate_used=Decimal("15333.33"),
    )
    lines = lines_by_code(conn, entry_id)
    for code in ("COGS", "SHIPPING_COST", "PAYONEER_WALLET"):
        assert lines[code][0].amount_usd_ref == Decimal("270.00")
        assert lines[code][0].fx_rate_used == Decimal("15333.33")


def test_split_rejects_non_positive_total(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_cogs_purchase_with_shipping_split(
            conn, entry_date=dt.date(2026, 9, 15), amount_idr=Decimal("0"), shipping_portion_idr=Decimal("100")
        )


def test_split_rejects_non_positive_shipping_portion(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_cogs_purchase_with_shipping_split(
            conn, entry_date=dt.date(2026, 9, 15), amount_idr=Decimal("4140000"), shipping_portion_idr=Decimal("0")
        )
    with pytest.raises(ValueError):
        post_cogs_purchase_with_shipping_split(
            conn,
            entry_date=dt.date(2026, 9, 15),
            amount_idr=Decimal("4140000"),
            shipping_portion_idr=Decimal("-1"),
        )


def test_split_rejects_shipping_portion_equal_to_total(prototype):
    """There must be something left over for COGS — a shipping portion equal
    to the whole amount would leave a zero-amount COGS line."""
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_cogs_purchase_with_shipping_split(
            conn,
            entry_date=dt.date(2026, 9, 15),
            amount_idr=Decimal("4140000"),
            shipping_portion_idr=Decimal("4140000"),
        )


def test_split_rejects_shipping_portion_greater_than_total(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_cogs_purchase_with_shipping_split(
            conn,
            entry_date=dt.date(2026, 9, 15),
            amount_idr=Decimal("4140000"),
            shipping_portion_idr=Decimal("5000000"),
        )
