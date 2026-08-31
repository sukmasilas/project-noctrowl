"""Consignment sales — both payout models, and the mandatory human
confirmation gate before a payout can ever post.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ledger.consignment import calc_experimental_payout_usd, calc_tier_payout_usd, lookup_tier_rate
from ledger.errors import MissingConfirmedAmountError
from ledger.posting import (
    confirm_consignment_sale,
    create_consignment_sale,
    post_consignor_reimbursement,
    post_consignment_sale,
)
from tests.helpers import assert_balanced, get_source_type, lines_by_code, total_debit


def test_tier_model_calculator_matches_pasal_3_schedule(conn):
    result = lookup_tier_rate(conn, Decimal("80.00"))
    assert result.rate_percent == Decimal("80.00")
    assert result.requires_manual_contact is False
    assert calc_tier_payout_usd(Decimal("80.00"), result.rate_percent) == Decimal("64.00")

    # $7,500+ must never resolve to an auto-applicable rate.
    top = lookup_tier_rate(conn, Decimal("8000.00"))
    assert top.rate_percent is None
    assert top.requires_manual_contact is True


def test_consignment_sale_tier_model_confirmed_and_posted_then_reimbursed(prototype):
    conn, topo = prototype
    kurs = Decimal("15500")

    # Item price $80 (excludes shipping) -> 80% tier -> suggested payout $64.
    suggestion = calc_tier_payout_usd(Decimal("80.00"), Decimal("80.00"))
    assert suggestion == Decimal("64.00")
    confirmed_payout_idr = suggestion * kurs  # a human confirms this exact figure

    cs_id = create_consignment_sale(
        conn,
        item_price_usd=Decimal("80.00"),
        payout_model="tier",
        tier_rate_percent=Decimal("80.00"),
        payout_amount_idr=confirmed_payout_idr,
        consignor_item_ref="CONSIGN-WATCH-001",
    )
    confirm_consignment_sale(conn, cs_id)

    entry_id = post_consignment_sale(
        conn,
        consignment_sale_id=cs_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 8),
        gross_sale_price_usd=Decimal("90.00"),  # $80 item + $10 shipping
        ebay_fee_usd=Decimal("10.00"),
        kurs_pajak_rate=kurs,
        ebay_order_ref="ORDER-CONSIGN-TIER-1",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "consignment_sale"

    lines = lines_by_code(conn, entry_id)
    gross_idr = Decimal("90.00") * kurs
    fee_idr = Decimal("10.00") * kurs
    assert lines["EBAY_WALLET"][0].debit_amount_idr == gross_idr - fee_idr
    assert lines["EBAY_SELLING_FEES"][0].debit_amount_idr == fee_idr
    assert lines["CONSIGNOR_PAYABLE"][0].credit_amount_idr == confirmed_payout_idr
    # Commission = everything not owed to the consignor: gross - payout.
    assert lines["CONSIGNMENT_COMMISSION_INCOME"][0].credit_amount_idr == gross_idr - confirmed_payout_idr
    # Sales Revenue is never touched by a consignment sale — only the
    # commission is revenue (see CLAUDE.md).
    assert "SALES_REVENUE" not in lines
    for row in lines["CONSIGNOR_PAYABLE"]:
        assert row.consignor_item_ref == "CONSIGN-WATCH-001"

    # Reimburse the consignor -> clears the liability, never touches P&L.
    reimb_id = post_consignor_reimbursement(
        conn,
        entry_date=dt.date(2026, 7, 20),
        amount_idr=confirmed_payout_idr,
        consignor_item_ref="CONSIGN-WATCH-001",
    )
    assert_balanced(conn, reimb_id)
    assert get_source_type(conn, reimb_id) == "consignment_payout"
    reimb_lines = lines_by_code(conn, reimb_id)
    assert reimb_lines["CONSIGNOR_PAYABLE"][0].debit_amount_idr == confirmed_payout_idr
    assert reimb_lines["BCA_MAIN"][0].credit_amount_idr == confirmed_payout_idr
    assert "SALES_REVENUE" not in reimb_lines and "COGS" not in reimb_lines


def test_consignment_sale_experimental_model_zero_commission(prototype):
    """net_of_fees_and_shipping model: consignor absorbs fees + shipping,
    seller earns $0 explicit commission — no Commission Income line posts.
    """
    conn, topo = prototype
    kurs = Decimal("15000")

    gross_usd = Decimal("200.00")
    fee_usd = Decimal("20.00")
    shipping_cost_usd = Decimal("15.00")
    suggestion = calc_experimental_payout_usd(gross_usd, fee_usd, shipping_cost_usd)
    assert suggestion == Decimal("165.00")  # 200 - 20 - 15
    confirmed_payout_idr = suggestion * kurs

    cs_id = create_consignment_sale(
        conn,
        item_price_usd=Decimal("185.00"),  # informational — item component of gross
        shipping_cost_usd=shipping_cost_usd,
        payout_model="net_of_fees_and_shipping",
        payout_amount_idr=confirmed_payout_idr,
        consignor_item_ref="CONSIGN-AUTO-777",
    )
    confirm_consignment_sale(conn, cs_id)

    entry_id = post_consignment_sale(
        conn,
        consignment_sale_id=cs_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 15),
        gross_sale_price_usd=gross_usd,
        ebay_fee_usd=fee_usd,
        kurs_pajak_rate=kurs,
    )
    assert_balanced(conn, entry_id)

    lines = lines_by_code(conn, entry_id)
    gross_idr = gross_usd * kurs
    fee_idr = fee_usd * kurs
    net_wallet_idr = gross_idr - fee_idr

    assert "CONSIGNMENT_COMMISSION_INCOME" not in lines  # $0 commission -> no line at all
    assert "SALES_REVENUE" not in lines
    assert lines["EBAY_WALLET"][0].debit_amount_idr == net_wallet_idr
    assert lines["CONSIGNOR_PAYABLE"][0].credit_amount_idr == confirmed_payout_idr

    # eBay Selling Fees: debited (real fee) and credited back (consignor
    # absorbs it) within the same entry -> net P&L effect from the fee on
    # this sale is $0, but the gross fee is still independently traceable.
    fee_debits = sum(r.debit_amount_idr for r in lines["EBAY_SELLING_FEES"])
    fee_credits = sum(r.credit_amount_idr for r in lines["EBAY_SELLING_FEES"])
    assert fee_debits == fee_idr
    assert fee_credits == fee_idr

    # Shipping Cost recovery credit = net_wallet - payout (the consignor's
    # absorbed share of the seller's actual shipping outlay).
    assert lines["SHIPPING_COST"][0].credit_amount_idr == net_wallet_idr - confirmed_payout_idr


def test_consignment_sale_cannot_post_without_confirmation(prototype):
    """The core rule: no consignment payout may ever auto-post. Creating an
    UNCONFIRMED row and attempting to post it must be rejected.
    """
    conn, topo = prototype

    cs_id = create_consignment_sale(
        conn,
        item_price_usd=Decimal("9000.00"),  # a $7,500+ item — no fixed tier rate exists
        payout_model="tier",
        payout_amount_idr=Decimal("999999999"),  # deliberately NOT confirmed
        consignor_item_ref="CONSIGN-BIGTICKET-1",
        confirmed=False,
    )

    with pytest.raises(MissingConfirmedAmountError):
        post_consignment_sale(
            conn,
            consignment_sale_id=cs_id,
            ebay_account_id=topo["ebay_account_id"],
            entry_date=dt.date(2026, 7, 9),
            gross_sale_price_usd=Decimal("9500.00"),
            ebay_fee_usd=Decimal("400.00"),
            kurs_pajak_rate=Decimal("15500"),
        )


def test_consignment_sale_cannot_post_twice(prototype):
    conn, topo = prototype
    kurs = Decimal("15500")

    cs_id = create_consignment_sale(
        conn,
        item_price_usd=Decimal("50.00"),
        payout_model="tier",
        tier_rate_percent=Decimal("80.00"),
        payout_amount_idr=Decimal("40.00") * kurs,
        consignor_item_ref="CONSIGN-DOUBLEPOST-1",
    )
    confirm_consignment_sale(conn, cs_id)

    post_consignment_sale(
        conn,
        consignment_sale_id=cs_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 11),
        gross_sale_price_usd=Decimal("55.00"),
        ebay_fee_usd=Decimal("6.00"),
        kurs_pajak_rate=kurs,
    )

    with pytest.raises(ValueError, match="already been posted"):
        post_consignment_sale(
            conn,
            consignment_sale_id=cs_id,
            ebay_account_id=topo["ebay_account_id"],
            entry_date=dt.date(2026, 7, 11),
            gross_sale_price_usd=Decimal("55.00"),
            ebay_fee_usd=Decimal("6.00"),
            kurs_pajak_rate=kurs,
        )
