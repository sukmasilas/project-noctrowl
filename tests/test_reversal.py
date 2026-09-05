"""``ledger.posting.post_reversal_entry`` — the narrow, one-time,
deliberately-scoped exception to "corrections to posted transactions are out
of scope for the prototype" (see CLAUDE.md's Bank transaction classification
section). Built to close out the real historical bad posting
journal_entry_id=917 / review_queue.id=321 via
scripts/correct_journal_entry_917.py — NOT a general reopen/reverse
workflow (see that function's own docstring in ledger/posting.py).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from ledger.posting import post_cogs_purchase, post_reversal_entry
from ledger.schema import journal_entries
from tests.helpers import assert_balanced, get_lines, lines_by_code


def test_post_reversal_entry_mirrors_and_nets_to_zero(prototype):
    conn, topo = prototype

    original_id = post_cogs_purchase(conn, entry_date=dt.date(2026, 8, 11), amount_idr=Decimal("50000"))
    assert_balanced(conn, original_id)

    reversal_id = post_reversal_entry(
        conn,
        original_journal_entry_id=original_id,
        entry_date=dt.date(2026, 9, 5),
        memo="correction of journal_entry_id=... test reversal",
    )
    assert_balanced(conn, reversal_id)
    assert reversal_id != original_id

    original_lines = lines_by_code(conn, original_id)
    reversal_lines = lines_by_code(conn, reversal_id)
    # Every line's debit/credit is swapped relative to the original.
    assert reversal_lines["COGS"][0].credit_amount_idr == original_lines["COGS"][0].debit_amount_idr
    assert reversal_lines["COGS"][0].debit_amount_idr == Decimal("0")
    assert reversal_lines["BCA_MAIN"][0].debit_amount_idr == original_lines["BCA_MAIN"][0].credit_amount_idr
    assert reversal_lines["BCA_MAIN"][0].credit_amount_idr == Decimal("0")

    # Net effect across both entries, per account, is exactly zero.
    for code in ("COGS", "BCA_MAIN"):
        net = sum(l.debit_amount_idr - l.credit_amount_idr for l in original_lines[code] + reversal_lines[code])
        assert net == Decimal("0")

    # The original entry is now visibly, permanently flagged as reversed.
    reversed_by = conn.execute(
        select(journal_entries.c.reversed_by_id).where(journal_entries.c.id == original_id)
    ).scalar_one()
    assert reversed_by == reversal_id


def test_post_reversal_entry_refuses_to_reverse_twice(prototype):
    conn, topo = prototype
    original_id = post_cogs_purchase(conn, entry_date=dt.date(2026, 8, 11), amount_idr=Decimal("50000"))
    post_reversal_entry(
        conn, original_journal_entry_id=original_id, entry_date=dt.date(2026, 9, 5), memo="first reversal"
    )
    with pytest.raises(ValueError):
        post_reversal_entry(
            conn, original_journal_entry_id=original_id, entry_date=dt.date(2026, 9, 5), memo="second reversal attempt"
        )


def test_post_reversal_entry_requires_a_memo(prototype):
    conn, topo = prototype
    original_id = post_cogs_purchase(conn, entry_date=dt.date(2026, 8, 11), amount_idr=Decimal("50000"))
    with pytest.raises(ValueError):
        post_reversal_entry(conn, original_journal_entry_id=original_id, entry_date=dt.date(2026, 9, 5), memo="")


def test_post_reversal_entry_rejects_unknown_entry(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_reversal_entry(conn, original_journal_entry_id=999999, entry_date=dt.date(2026, 9, 5), memo="x")
