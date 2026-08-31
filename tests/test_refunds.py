"""Refund/discount clawbacks post to Sales Returns & Allowances
(contra-revenue) — never netted silently into Sales Revenue."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ledger.posting import post_ebay_sale, post_refund
from tests.helpers import assert_balanced, get_source_type, lines_by_code


def test_refund_at_ebay_wallet_stage_posts_contra_revenue(prototype):
    conn, topo = prototype
    kurs = Decimal("15500")

    sale_id = post_ebay_sale(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 2),
        gross_sale_price_usd=Decimal("120.00"),
        ebay_fee_usd=Decimal("14.00"),
        kurs_pajak_rate=kurs,
        ebay_order_ref="ORDER-REFUND-1",
    )
    assert_balanced(conn, sale_id)

    refund_id = post_refund(
        conn,
        entry_date=dt.date(2026, 7, 6),
        amount_idr=Decimal("120.00") * kurs,
        stage="ebay_wallet",
        ebay_account_id=topo["ebay_account_id"],
        usd_amount=Decimal("120.00"),
        kurs_pajak_rate=kurs,
        ebay_order_ref="ORDER-REFUND-1",
    )
    assert_balanced(conn, refund_id)
    assert get_source_type(conn, refund_id) == "ebay_refund"

    lines = lines_by_code(conn, refund_id)
    assert lines["SALES_RETURNS_ALLOWANCES"][0].debit_amount_idr == Decimal("120.00") * kurs
    assert lines["EBAY_WALLET"][0].credit_amount_idr == Decimal("120.00") * kurs
    # Never posted to Sales Revenue directly — it's a separate contra line.
    assert "SALES_REVENUE" not in lines


def test_refund_at_payoneer_stage_posts_contra_revenue(prototype):
    conn, topo = prototype

    refund_id = post_refund(
        conn,
        entry_date=dt.date(2026, 7, 22),
        amount_idr=Decimal("500000"),
        stage="payoneer",
        wallet_group_id=topo["wallet_group_id"],
    )
    assert_balanced(conn, refund_id)
    lines = lines_by_code(conn, refund_id)
    assert lines["SALES_RETURNS_ALLOWANCES"][0].debit_amount_idr == Decimal("500000")
    assert lines["PAYONEER_WALLET"][0].credit_amount_idr == Decimal("500000")
