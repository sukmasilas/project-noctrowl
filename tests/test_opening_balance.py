"""Opening-balance entries: a one-time entry recording a wallet/bank
account's real balance as of just before ledger-tracking began, booked to
Owner's Capital. See CLAUDE.md's Definition of done (the negative-Payoneer
-balance gap this closes) and ledger/posting.py's post_opening_balance.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from ledger.posting import post_opening_balance
from scheduling.fx_revaluation import compute_payoneer_wallet_balance
from tests.helpers import assert_balanced, get_source_type, lines_by_code


def test_opening_balance_debits_target_account_credits_owners_capital(prototype):
    conn, topo = prototype

    entry_id = post_opening_balance(
        conn,
        account_type_code="PAYONEER_WALLET",
        entry_date=dt.date(2026, 5, 1),
        amount_idr=Decimal("75503150.00"),  # 4703.00 * 16050
        wallet_group_id=topo["wallet_group_id"],
        amount_usd_ref=Decimal("4703.00"),
        fx_rate_used=Decimal("16050"),
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "opening_balance"

    lines = lines_by_code(conn, entry_id)
    assert lines["PAYONEER_WALLET"][0].debit_amount_idr == Decimal("75503150.00")
    assert lines["PAYONEER_WALLET"][0].credit_amount_idr == Decimal("0")
    assert lines["PAYONEER_WALLET"][0].amount_usd_ref == Decimal("4703.00")
    assert lines["PAYONEER_WALLET"][0].fx_rate_used == Decimal("16050")

    assert lines["OWNERS_CAPITAL"][0].credit_amount_idr == Decimal("75503150.00")
    assert lines["OWNERS_CAPITAL"][0].debit_amount_idr == Decimal("0")
    # USD reference is tagged on BOTH lines, matching this module's
    # established convention (post_consignor_reimbursement, etc.).
    assert lines["OWNERS_CAPITAL"][0].amount_usd_ref == Decimal("4703.00")


def test_opening_balance_amount_must_be_positive(prototype):
    conn, topo = prototype

    with pytest.raises(ValueError):
        post_opening_balance(
            conn,
            account_type_code="PAYONEER_WALLET",
            entry_date=dt.date(2026, 5, 1),
            amount_idr=Decimal("0"),
            wallet_group_id=topo["wallet_group_id"],
        )

    with pytest.raises(ValueError):
        post_opening_balance(
            conn,
            account_type_code="PAYONEER_WALLET",
            entry_date=dt.date(2026, 5, 1),
            amount_idr=Decimal("-100"),
            wallet_group_id=topo["wallet_group_id"],
        )


def test_opening_balance_rejects_float_amount(prototype):
    conn, topo = prototype

    with pytest.raises(TypeError):
        post_opening_balance(
            conn,
            account_type_code="PAYONEER_WALLET",
            entry_date=dt.date(2026, 5, 1),
            amount_idr=75503150.00,  # a real float, not a Decimal
            wallet_group_id=topo["wallet_group_id"],
        )


def test_opening_balance_is_idempotent_at_the_db_level(prototype):
    """A second opening_balance entry for the SAME account must be
    impossible, structurally -- not just discouraged by convention. See
    ledger/schema.py's ux_opening_balances_account_id unique index.
    """
    conn, topo = prototype

    post_opening_balance(
        conn,
        account_type_code="PAYONEER_WALLET",
        entry_date=dt.date(2026, 5, 1),
        amount_idr=Decimal("75503150.00"),
        wallet_group_id=topo["wallet_group_id"],
        amount_usd_ref=Decimal("4703.00"),
        fx_rate_used=Decimal("16050"),
    )

    with pytest.raises(IntegrityError):
        post_opening_balance(
            conn,
            account_type_code="PAYONEER_WALLET",
            entry_date=dt.date(2026, 5, 1),
            amount_idr=Decimal("1000000.00"),
            wallet_group_id=topo["wallet_group_id"],
            amount_usd_ref=Decimal("62.00"),
            fx_rate_used=Decimal("16050"),
        )


def test_opening_balance_included_in_usd_and_idr_sums_for_fx_revaluation(prototype):
    """The opening balance must be INCLUDED in compute_payoneer_wallet_
    balance's USD sum (unlike fx_revaluation rows, which are deliberately
    excluded -- see that function's module docstring) since it represents a
    real, actual USD amount sitting in the wallet, not a restatement of an
    existing balance at a new rate.
    """
    conn, topo = prototype
    wallet_group_id = topo["wallet_group_id"]

    post_opening_balance(
        conn,
        account_type_code="PAYONEER_WALLET",
        entry_date=dt.date(2026, 5, 1),
        amount_idr=Decimal("75503150.00"),
        wallet_group_id=wallet_group_id,
        amount_usd_ref=Decimal("4703.00"),
        fx_rate_used=Decimal("16050"),
    )

    usd_balance, idr_book_value = compute_payoneer_wallet_balance(
        conn, wallet_group_id=wallet_group_id, as_of_date=dt.date(2026, 5, 31)
    )
    assert usd_balance == Decimal("4703.00")
    assert idr_book_value == Decimal("75503150.00")


def test_opening_balance_generic_across_account_types(prototype):
    """Not hardcoded to Payoneer Wallet -- works for any account type/
    instance the target business needs (e.g. an eBay Wallet), per the
    brief's "generic/reusable" requirement.
    """
    conn, topo = prototype

    entry_id = post_opening_balance(
        conn,
        account_type_code="EBAY_WALLET",
        entry_date=dt.date(2026, 5, 1),
        amount_idr=Decimal("1000000.00"),
        ebay_account_id=topo["ebay_account_id"],
    )
    lines = lines_by_code(conn, entry_id)
    assert lines["EBAY_WALLET"][0].debit_amount_idr == Decimal("1000000.00")
    assert lines["OWNERS_CAPITAL"][0].credit_amount_idr == Decimal("1000000.00")
