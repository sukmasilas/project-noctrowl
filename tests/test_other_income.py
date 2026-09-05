"""``ledger.posting.post_income_line`` — the generalized, sign-aware
"post a single bank/Payoneer-statement line against an Other-Income/Expense
-section INCOME account" posting (2026-09-05), and its two concrete uses:
the still-hardcoded ``post_interest_income_line`` wrapper (regression —
must behave identically after the refactor) and the new OTHER_INCOME
account backing a positive (inflow) 'other'-labeled review-queue row (see
ingestion/matching.py's ``_post_one_row`` and CLAUDE.md's Chart of accounts,
"Other Income" line — the historical bad entry, journal_entry_id=917 /
review_queue.id=321, is the concrete real case this closes).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ledger.entities import get_account_id
from ledger.posting import post_income_line, post_interest_income_line
from tests.helpers import assert_balanced, get_lines, get_source_type, lines_by_code


def test_post_income_line_inflow_credits_income_account(prototype):
    conn, topo = prototype

    entry_id = post_income_line(
        conn,
        entry_date=dt.date(2026, 8, 11),
        income_account_type_code="OTHER_INCOME",
        amount_idr=Decimal("50000"),
        memo="correction of journal_entry_id=917",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "bank_other"

    lines = lines_by_code(conn, entry_id)
    assert lines["OTHER_INCOME"][0].credit_amount_idr == Decimal("50000")
    assert lines["OTHER_INCOME"][0].debit_amount_idr == Decimal("0")
    assert lines["BCA_MAIN"][0].debit_amount_idr == Decimal("50000")


def test_post_income_line_outflow_debits_income_account(prototype):
    conn, topo = prototype

    entry_id = post_income_line(
        conn,
        entry_date=dt.date(2026, 8, 12),
        income_account_type_code="OTHER_INCOME",
        amount_idr=Decimal("-15000"),
    )
    assert_balanced(conn, entry_id)

    lines = lines_by_code(conn, entry_id)
    assert lines["OTHER_INCOME"][0].debit_amount_idr == Decimal("15000")
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("15000")


def test_post_income_line_rejects_zero_amount(prototype):
    conn, topo = prototype

    with pytest.raises(ValueError):
        post_income_line(
            conn,
            entry_date=dt.date(2026, 8, 11),
            income_account_type_code="OTHER_INCOME",
            amount_idr=Decimal("0"),
        )


def test_post_income_line_respects_paying_wallet_group(prototype):
    conn, topo = prototype

    entry_id = post_income_line(
        conn,
        entry_date=dt.date(2026, 8, 11),
        income_account_type_code="OTHER_INCOME",
        amount_idr=Decimal("50000"),
        paying_account_type_code="BCA_BRIDGING",
        paying_wallet_group_id=topo["wallet_group_id"],
    )
    assert_balanced(conn, entry_id)
    bridging_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    lines = lines_by_code(conn, entry_id)
    assert lines["OTHER_INCOME"][0].credit_amount_idr == Decimal("50000")
    # The debit landed on the wallet-group's own Bridging account, not
    # BCA_MAIN (the default) — confirms paying_account_type_code/
    # paying_wallet_group_id are actually threaded through.
    debit_account_ids = {jl.account_id for jl in get_lines(conn, entry_id) if jl.debit_amount_idr > 0}
    assert bridging_id in debit_account_ids


def test_post_interest_income_line_unchanged_after_generalization(prototype):
    """Regression: post_interest_income_line is now a thin wrapper around
    post_income_line but must behave EXACTLY as before — same account
    (INTEREST_INCOME, never OTHER_INCOME), same sign-aware direction.
    """
    conn, topo = prototype

    inflow_id = post_interest_income_line(
        conn, entry_date=dt.date(2026, 5, 31), amount_idr=Decimal("1186.92")
    )
    assert_balanced(conn, inflow_id)
    lines = lines_by_code(conn, inflow_id)
    assert lines["INTEREST_INCOME"][0].credit_amount_idr == Decimal("1186.92")
    assert "OTHER_INCOME" not in lines

    outflow_id = post_interest_income_line(
        conn, entry_date=dt.date(2026, 5, 31), amount_idr=Decimal("-237.38")
    )
    assert_balanced(conn, outflow_id)
    lines2 = lines_by_code(conn, outflow_id)
    assert lines2["INTEREST_INCOME"][0].debit_amount_idr == Decimal("237.38")
