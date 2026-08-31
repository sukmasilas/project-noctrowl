"""FX handling: booking-date accrual (a reference field on the sale entry),
realized FX at Payoneer withdrawal (fee + spread as two distinct lines),
and month-end unrealized FX revaluation (a separate account, never
conflated with realized).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ledger.posting import post_ebay_sale, post_realized_fx_withdrawal, post_unrealized_fx_revaluation
from tests.helpers import assert_balanced, get_source_type, lines_by_code


def test_sale_books_at_kurs_pajak_booking_rate_as_reference(prototype):
    conn, topo = prototype
    kurs = Decimal("15450")

    entry_id = post_ebay_sale(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=dt.date(2026, 7, 4),
        gross_sale_price_usd=Decimal("60.00"),
        ebay_fee_usd=Decimal("7.00"),
        kurs_pajak_rate=kurs,
    )
    lines = lines_by_code(conn, entry_id)
    # IDR is the ledger's source of truth; USD + the rate used are kept as
    # reference fields only (see CLAUDE.md).
    assert lines["SALES_REVENUE"][0].fx_rate_used == kurs
    assert lines["SALES_REVENUE"][0].debit_amount_idr == Decimal("0")  # it's a credit line
    assert lines["EBAY_WALLET"][0].fx_rate_used == kurs


def test_realized_fx_withdrawal_splits_fee_and_gain(prototype):
    """USD appreciated relative to the booking rate: the pure rate-movement
    effect (isolated from the fee) shows up as a GAIN.
    """
    conn, topo = prototype

    entry_id = post_realized_fx_withdrawal(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        entry_date=dt.date(2026, 7, 25),
        gross_usd=Decimal("1000.00"),
        payoneer_fee_usd=Decimal("10.00"),
        exchange_rate_excl_fee=Decimal("15500"),
        booking_rate_used_idr=Decimal("15400"),  # sales were booked at a lower rate
        memo="Payoneer withdrawal — July batch",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "payoneer_withdrawal"

    lines = lines_by_code(conn, entry_id)
    net_idr_landed = Decimal("990.00") * Decimal("15500")  # (1000-10) * 15500
    payout_fee_idr = Decimal("10.00") * Decimal("15500")
    booking_implied_idr = Decimal("1000.00") * Decimal("15400")

    assert lines["BCA_BRIDGING"][0].debit_amount_idr == net_idr_landed
    assert lines["PAYOUT_FEE"][0].debit_amount_idr == payout_fee_idr
    assert lines["PAYONEER_WALLET"][0].credit_amount_idr == booking_implied_idr

    expected_gain = (net_idr_landed + payout_fee_idr) - booking_implied_idr
    assert expected_gain > 0
    assert lines["REALIZED_FX"][0].credit_amount_idr == expected_gain
    assert lines["REALIZED_FX"][0].debit_amount_idr == Decimal("0")


def test_realized_fx_withdrawal_splits_fee_and_loss(prototype):
    """USD depreciated relative to the booking rate -> a LOSS, debited to
    Realized FX Gain/Loss, kept fully separate from the Payout Fee expense.
    """
    conn, topo = prototype

    entry_id = post_realized_fx_withdrawal(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        entry_date=dt.date(2026, 7, 26),
        gross_usd=Decimal("500.00"),
        payoneer_fee_usd=Decimal("5.00"),
        exchange_rate_excl_fee=Decimal("15200"),
        booking_rate_used_idr=Decimal("15500"),  # booked higher than realized
    )
    assert_balanced(conn, entry_id)

    lines = lines_by_code(conn, entry_id)
    net_idr_landed = Decimal("495.00") * Decimal("15200")
    payout_fee_idr = Decimal("5.00") * Decimal("15200")
    booking_implied_idr = Decimal("500.00") * Decimal("15500")

    expected_loss = booking_implied_idr - (net_idr_landed + payout_fee_idr)
    assert expected_loss > 0
    assert lines["REALIZED_FX"][0].debit_amount_idr == expected_loss
    assert lines["REALIZED_FX"][0].credit_amount_idr == Decimal("0")
    # Payout Fee is untouched by the FX outcome — always the fee's own cost.
    assert lines["PAYOUT_FEE"][0].debit_amount_idr == payout_fee_idr


def test_unrealized_fx_revaluation_month_end_gain(prototype):
    conn, topo = prototype

    entry_id = post_unrealized_fx_revaluation(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        period_month=dt.date(2026, 7, 1),
        usd_balance=Decimal("2000.00"),
        current_book_value_idr=Decimal("30800000"),  # booked at 15400/usd
        kemenkeu_eom_rate_idr=Decimal("15450"),  # month-end rate is higher
    )
    assert entry_id is not None
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "fx_revaluation"

    lines = lines_by_code(conn, entry_id)
    revalued_idr = Decimal("2000.00") * Decimal("15450")
    diff = revalued_idr - Decimal("30800000")
    assert diff > 0
    assert lines["PAYONEER_WALLET"][0].debit_amount_idr == diff
    assert lines["UNREALIZED_FX"][0].credit_amount_idr == diff
    # Unrealized FX is a distinct account from Realized FX — never conflated.
    assert "REALIZED_FX" not in lines


def test_unrealized_fx_revaluation_no_change_posts_nothing(prototype):
    conn, topo = prototype

    entry_id = post_unrealized_fx_revaluation(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        period_month=dt.date(2026, 7, 1),
        usd_balance=Decimal("1000.00"),
        current_book_value_idr=Decimal("15500000"),
        kemenkeu_eom_rate_idr=Decimal("15500"),  # exactly matches the current book value
    )
    assert entry_id is None
