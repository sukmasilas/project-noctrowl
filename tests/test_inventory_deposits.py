"""``ledger.posting.post_inventory_deposit`` /
``post_inventory_deposit_received`` — the new INVENTORY_DEPOSITS asset
account (2026-09-29). See ledger/chart_of_accounts.py's inline note and the
real trigger: a Master Account bank line, "TRSF E-BANKING DB ... / DP Box
op / FARIZ PRADANA", -Rp 9,840,000, confirmed by the user as a deposit for
goods not yet received.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ledger.entities import get_account_id
from ledger.posting import post_inventory_deposit, post_inventory_deposit_received
from tests.helpers import assert_balanced, get_lines, get_source_type, lines_by_code

DEPOSIT_REF = "DP Box op — Fariz — 2026-08-20"


def test_deposit_debits_inventory_deposits_credits_bca_main(prototype):
    conn, topo = prototype

    entry_id = post_inventory_deposit(
        conn,
        entry_date=dt.date(2026, 8, 20),
        amount_idr=Decimal("9840000"),
        deposit_ref=DEPOSIT_REF,
        memo="DP Box op",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "bank_other"

    lines = lines_by_code(conn, entry_id)
    assert lines["INVENTORY_DEPOSITS"][0].debit_amount_idr == Decimal("9840000")
    assert lines["INVENTORY_DEPOSITS"][0].consignor_item_ref == DEPOSIT_REF
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("9840000")
    assert lines["BCA_MAIN"][0].consignor_item_ref == DEPOSIT_REF


def test_deposit_never_touches_pl_or_equity(prototype):
    """The deposit is a pure asset movement — never COGS, never any P&L
    account — until the goods actually arrive."""
    conn, topo = prototype

    entry_id = post_inventory_deposit(
        conn, entry_date=dt.date(2026, 8, 20), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    lines = lines_by_code(conn, entry_id)
    assert set(lines.keys()) == {"INVENTORY_DEPOSITS", "BCA_MAIN"}
    assert "COGS" not in lines


def test_deposit_requires_deposit_ref(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_inventory_deposit(conn, entry_date=dt.date(2026, 8, 20), amount_idr=Decimal("9840000"), deposit_ref="")


def test_deposit_rejects_non_positive_amount(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_inventory_deposit(
            conn, entry_date=dt.date(2026, 8, 20), amount_idr=Decimal("0"), deposit_ref=DEPOSIT_REF
        )


def test_deposit_received_converts_full_amount_to_cogs(prototype):
    conn, topo = prototype
    post_inventory_deposit(
        conn, entry_date=dt.date(2026, 8, 20), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )

    entry_id = post_inventory_deposit_received(
        conn,
        entry_date=dt.date(2026, 9, 5),
        amount_idr=Decimal("9840000"),
        deposit_ref=DEPOSIT_REF,
        memo="Goods received",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "inventory_deposit_received"

    lines = lines_by_code(conn, entry_id)
    assert lines["COGS"][0].debit_amount_idr == Decimal("9840000")
    assert lines["COGS"][0].consignor_item_ref == DEPOSIT_REF
    assert lines["INVENTORY_DEPOSITS"][0].credit_amount_idr == Decimal("9840000")
    assert lines["INVENTORY_DEPOSITS"][0].consignor_item_ref == DEPOSIT_REF
    # No cash account is touched by the conversion itself.
    assert "BCA_MAIN" not in lines


def test_deposit_received_never_touches_a_cash_account(prototype):
    conn, topo = prototype
    post_inventory_deposit(
        conn, entry_date=dt.date(2026, 8, 20), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    entry_id = post_inventory_deposit_received(
        conn, entry_date=dt.date(2026, 9, 5), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    lines = lines_by_code(conn, entry_id)
    assert set(lines.keys()) == {"COGS", "INVENTORY_DEPOSITS"}


def test_deposit_received_allows_partial_conversion(prototype):
    """A delivery that only partially fulfills the deposit's purchase order
    — converts less than the full deposit, leaving a real remaining
    outstanding balance."""
    conn, topo = prototype
    post_inventory_deposit(
        conn, entry_date=dt.date(2026, 8, 20), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    post_inventory_deposit_received(
        conn, entry_date=dt.date(2026, 9, 5), amount_idr=Decimal("4000000"), deposit_ref=DEPOSIT_REF
    )

    deposits_id = get_account_id(conn, "INVENTORY_DEPOSITS")
    from sqlalchemy import select

    from ledger.schema import journal_lines

    rows = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(
            journal_lines.c.account_id == deposits_id, journal_lines.c.consignor_item_ref == DEPOSIT_REF
        )
    ).all()
    outstanding = sum((r.debit_amount_idr - r.credit_amount_idr for r in rows), Decimal("0"))
    assert outstanding == Decimal("5840000")


def test_deposit_received_refuses_to_exceed_outstanding_balance(prototype):
    """SAFETY CHECK: never let a conversion push a specific deposit
    reference's own outstanding balance negative — e.g. a double-submitted
    conversion or a typo'd amount."""
    conn, topo = prototype
    post_inventory_deposit(
        conn, entry_date=dt.date(2026, 8, 20), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    post_inventory_deposit_received(
        conn, entry_date=dt.date(2026, 9, 5), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    # Already fully converted — a second conversion attempt for the same
    # reference must be refused, not silently push it negative.
    with pytest.raises(ValueError):
        post_inventory_deposit_received(
            conn, entry_date=dt.date(2026, 9, 6), amount_idr=Decimal("1"), deposit_ref=DEPOSIT_REF
        )


def test_deposit_received_requires_deposit_ref(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_inventory_deposit_received(
            conn, entry_date=dt.date(2026, 9, 5), amount_idr=Decimal("9840000"), deposit_ref=""
        )


def test_deposit_received_rejects_non_positive_amount(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_inventory_deposit_received(
            conn, entry_date=dt.date(2026, 9, 5), amount_idr=Decimal("0"), deposit_ref=DEPOSIT_REF
        )


def test_deposit_received_with_no_prior_deposit_refuses(prototype):
    """A deposit_ref with zero (or no) prior deposit activity has nothing
    outstanding to convert — must refuse, not silently create a negative
    balance out of nowhere."""
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_inventory_deposit_received(
            conn, entry_date=dt.date(2026, 9, 5), amount_idr=Decimal("1000000"), deposit_ref="NEVER-DEPOSITED"
        )
