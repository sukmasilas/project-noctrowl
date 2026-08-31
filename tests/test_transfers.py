"""Inter-account transfers: a distinct, non-P&L transaction type that can
never touch revenue or expense — both at the Python and Postgres level.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ledger.errors import InvalidTransferError
from ledger.posting import post_inter_account_transfer
from tests.helpers import assert_balanced, get_source_type, lines_by_code


def test_inter_account_transfer_moves_money_without_touching_pl(prototype):
    conn, topo = prototype

    entry_id = post_inter_account_transfer(
        conn,
        entry_date=dt.date(2026, 7, 18),
        from_account_type_code="BCA_BRIDGING",
        from_wallet_group_id=topo["wallet_group_id"],
        to_account_type_code="BCA_MAIN",
        amount_idr=Decimal("2000000"),
        memo="Monthly consolidation sweep",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "inter_account_transfer"

    lines = lines_by_code(conn, entry_id)
    assert set(lines.keys()) == {"BCA_BRIDGING", "BCA_MAIN"}
    assert lines["BCA_BRIDGING"][0].credit_amount_idr == Decimal("2000000")
    assert lines["BCA_MAIN"][0].debit_amount_idr == Decimal("2000000")


def test_inter_account_transfer_rejects_revenue_account_at_python_level(prototype):
    conn, topo = prototype

    with pytest.raises(InvalidTransferError):
        post_inter_account_transfer(
            conn,
            entry_date=dt.date(2026, 7, 18),
            from_account_type_code="BCA_MAIN",
            to_account_type_code="SALES_REVENUE",  # not a transferable wallet/bank account
            amount_idr=Decimal("1000000"),
        )


def test_inter_account_transfer_rejects_expense_account_at_python_level(prototype):
    conn, topo = prototype

    with pytest.raises(InvalidTransferError):
        post_inter_account_transfer(
            conn,
            entry_date=dt.date(2026, 7, 18),
            from_account_type_code="GENERAL_OPEX",
            to_account_type_code="BCA_MAIN",
            amount_idr=Decimal("1000000"),
        )
