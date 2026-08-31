"""Structural invariants: an unbalanced posting must be impossible to
persist, not just discouraged — enforced at two independent layers:

1. ledger/posting.py's own fail-fast check (runs before any SQL at all).
2. A Postgres deferred constraint trigger (ledger/schema.py) that holds
   even if something bypasses posting.py entirely and writes raw SQL.

Also covers the inter-account-transfer account whitelist as a genuine DB
trigger, independent of the Python-level check already covered in
test_transfers.py.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from ledger.errors import UnbalancedEntryError
from ledger.posting import _insert_journal_entry, credit, debit
from ledger.schema import journal_entries, journal_lines


def test_posting_engine_rejects_unbalanced_entry_before_touching_db(prototype):
    """Layer 1: the Python-side check runs before any INSERT is issued."""
    conn, topo = prototype

    lines = [
        debit(topo["COGS"], Decimal("100000")),
        credit(topo["BCA_MAIN"], Decimal("99999")),  # deliberately mismatched
    ]
    with pytest.raises(UnbalancedEntryError):
        _insert_journal_entry(conn, entry_date=dt.date(2026, 7, 1), source_type="cogs_purchase", lines=lines)

    # Nothing should have been written — the check ran before any SQL.
    count = conn.execute(select(journal_entries.c.id)).all()
    assert count == []


def test_posting_engine_rejects_a_single_line_entry(prototype):
    conn, topo = prototype
    lines = [debit(topo["COGS"], Decimal("100000"))]
    with pytest.raises(UnbalancedEntryError):
        _insert_journal_entry(conn, entry_date=dt.date(2026, 7, 1), source_type="cogs_purchase", lines=lines)


def test_db_trigger_rejects_unbalanced_raw_insert_bypassing_the_engine(prototype):
    """Layer 2: even a raw SQL insert that skips ledger/posting.py entirely
    cannot commit an unbalanced journal entry — the deferred constraint
    trigger catches it when constraints are checked.
    """
    conn, topo = prototype
    captured_entry_id = {}

    with pytest.raises(DBAPIError):
        with conn.begin_nested():
            result = conn.execute(
                journal_entries.insert().values(
                    entry_date=dt.date(2026, 7, 1),
                    period_month=dt.date(2026, 7, 1),
                    source_type="cogs_purchase",
                )
            )
            entry_id = result.inserted_primary_key[0]
            captured_entry_id["id"] = entry_id
            conn.execute(
                journal_lines.insert(),
                [
                    {
                        "journal_entry_id": entry_id,
                        "account_id": topo["COGS"],
                        "debit_amount_idr": Decimal("100000"),
                        "credit_amount_idr": Decimal("0"),
                    },
                    {
                        "journal_entry_id": entry_id,
                        "account_id": topo["BCA_MAIN"],
                        "debit_amount_idr": Decimal("0"),
                        "credit_amount_idr": Decimal("50000"),  # mismatched on purpose
                    },
                ],
            )
            # Deferred constraint trigger: force it to check right now
            # instead of waiting for an actual COMMIT.
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    # The savepoint rolled back on the exception — nothing persisted.
    leftover = conn.execute(
        select(journal_lines.c.id).where(journal_lines.c.journal_entry_id == captured_entry_id["id"])
    ).all()
    assert leftover == []


def test_db_trigger_rejects_transfer_line_touching_a_revenue_account(prototype):
    """Layer 2 for the transfer whitelist: even bypassing
    post_inter_account_transfer()'s own Python-side check, the Postgres
    trigger blocks a transfer-typed entry from touching Sales Revenue.
    This is a plain (non-deferred) trigger, so it fires immediately on
    INSERT — no SET CONSTRAINTS needed.
    """
    conn, topo = prototype
    captured_entry_id = {}

    with pytest.raises(DBAPIError):
        with conn.begin_nested():
            result = conn.execute(
                journal_entries.insert().values(
                    entry_date=dt.date(2026, 7, 1),
                    period_month=dt.date(2026, 7, 1),
                    source_type="inter_account_transfer",
                )
            )
            entry_id = result.inserted_primary_key[0]
            captured_entry_id["id"] = entry_id
            conn.execute(
                journal_lines.insert(),
                [
                    {
                        "journal_entry_id": entry_id,
                        "account_id": topo["BCA_MAIN"],
                        "debit_amount_idr": Decimal("0"),
                        "credit_amount_idr": Decimal("100000"),
                    },
                    {
                        # Not a transferable wallet/bank account.
                        "journal_entry_id": entry_id,
                        "account_id": topo["SALES_REVENUE"],
                        "debit_amount_idr": Decimal("100000"),
                        "credit_amount_idr": Decimal("0"),
                    },
                ],
            )

    leftover = conn.execute(
        select(journal_lines.c.id).where(journal_lines.c.journal_entry_id == captured_entry_id["id"])
    ).all()
    assert leftover == []
