"""Stock and pre-order sale posting: COGS timing, gross revenue, fee split."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ledger.posting import post_cogs_purchase, post_ebay_sale
from tests.helpers import assert_balanced, get_lines, get_source_type, lines_by_code, total_debit


def test_stock_sale_cogs_at_purchase_before_sale(prototype):
    """Stock model: purchase (COGS) happens BEFORE the sale. Revenue is
    gross; eBay Selling Fees is its own line; wallet gets the net.
    """
    conn, topo = prototype
    kurs = Decimal("15500")

    # 1) Purchase inventory first (COGS expensed at time of purchase).
    cogs_entry_id = post_cogs_purchase(
        conn,
        entry_date=dt.date(2026, 7, 3),
        amount_idr=Decimal("500000"),
        memo="Stock purchase — TCG cards lot",
    )
    assert_balanced(conn, cogs_entry_id)
    assert get_source_type(conn, cogs_entry_id) == "cogs_purchase"
    cogs_lines = lines_by_code(conn, cogs_entry_id)
    assert cogs_lines["COGS"][0].debit_amount_idr == Decimal("500000")
    assert cogs_lines["BCA_MAIN"][0].credit_amount_idr == Decimal("500000")

    # 2) Later, the item sells.
    sale_entry_id = post_ebay_sale(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 10),
        gross_sale_price_usd=Decimal("100.00"),
        ebay_fee_usd=Decimal("12.50"),
        kurs_pajak_rate=kurs,
        ebay_order_ref="ORDER-STOCK-1",
    )
    assert_balanced(conn, sale_entry_id)
    assert get_source_type(conn, sale_entry_id) == "ebay_sale"

    lines = lines_by_code(conn, sale_entry_id)
    assert lines["SALES_REVENUE"][0].credit_amount_idr == Decimal("1550000")  # 100 * 15500, gross
    assert lines["EBAY_SELLING_FEES"][0].debit_amount_idr == Decimal("193750")  # 12.50 * 15500
    assert lines["EBAY_WALLET"][0].debit_amount_idr == Decimal("1356250")  # net = gross - fee
    assert total_debit(get_lines(conn, sale_entry_id)) == Decimal("1550000")


def test_preorder_sale_cogs_after_sale(prototype):
    """Pre-order model: the sale happens first, COGS (purchase/shipment) is
    posted afterward. Same posting logic as stock — only the timing of the
    COGS call relative to the sale differs.
    """
    conn, topo = prototype
    kurs = Decimal("15600")

    sale_entry_id = post_ebay_sale(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 5),
        gross_sale_price_usd=Decimal("250.00"),
        ebay_fee_usd=Decimal("30.00"),
        kurs_pajak_rate=kurs,
        ebay_order_ref="ORDER-PREORDER-1",
    )
    assert_balanced(conn, sale_entry_id)

    lines = lines_by_code(conn, sale_entry_id)
    assert lines["SALES_REVENUE"][0].credit_amount_idr == Decimal("3900000")  # 250 * 15600
    assert lines["EBAY_WALLET"][0].debit_amount_idr == Decimal("3432000")  # (250-30)*15600

    # Only after the sale does the seller actually buy/ship the item.
    cogs_entry_id = post_cogs_purchase(
        conn,
        entry_date=dt.date(2026, 7, 12),
        amount_idr=Decimal("1200000"),
        memo="Pre-order purchase — dropship watch",
    )
    assert_balanced(conn, cogs_entry_id)
    assert get_source_type(conn, cogs_entry_id) == "cogs_purchase"
    cogs_lines = lines_by_code(conn, cogs_entry_id)
    assert cogs_lines["COGS"][0].debit_amount_idr == Decimal("1200000")


def test_sale_carries_category_tag_on_revenue_line_only(prototype):
    """Category tag is stored on the Sales Revenue line for future phase-2
    analytics — never used to compute this (or any) statement total.
    """
    from sqlalchemy import select

    from ledger.schema import categories

    conn, topo = prototype
    tcg_id = conn.execute(select(categories.c.id).where(categories.c.name == "TCG")).scalar_one()

    sale_entry_id = post_ebay_sale(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 6),
        gross_sale_price_usd=Decimal("40.00"),
        ebay_fee_usd=Decimal("5.00"),
        kurs_pajak_rate=Decimal("15500"),
        category_id=tcg_id,
    )
    lines = lines_by_code(conn, sale_entry_id)
    assert lines["SALES_REVENUE"][0].category_id == tcg_id
    # eBay Wallet / Fee lines are not revenue lines -> no category tag.
    assert lines["EBAY_WALLET"][0].category_id is None
    assert lines["EBAY_SELLING_FEES"][0].category_id is None
