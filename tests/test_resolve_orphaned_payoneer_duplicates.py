"""Tests for scripts/resolve_orphaned_payoneer_duplicates.py's core
verification logic (``_load_and_verify_one``) — the one-off cleanup script
for the 10 real orphaned "Payment from eBay" review_queue rows found in
production during the Jan-Apr 2026 backfill (see that script's module
docstring and ingestion/payoneer.py's
``_resolve_orphaned_review_queue_duplicate`` for the matching prospective
fix).

Same pattern as tests/test_backfill_jan_apr_2026.py: exercises the script's
real DB-interacting logic against the disposable test database (not a full
subprocess run of ``main()`` — this focuses on the state-verification
business logic, which is the part with real money-correctness implications).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from ingestion.schema import review_queue, source_documents
from ledger.posting import post_inter_account_transfer
from scripts.resolve_orphaned_payoneer_duplicates import _load_and_verify_one, _StateMismatch


def _make_source_document(conn, *, wallet_group_id: int, period_month: _dt.date) -> int:
    result = conn.execute(
        source_documents.insert().values(
            document_type="payoneer_csv",
            period_month=period_month,
            wallet_group_id=wallet_group_id,
            drive_file_name="test-fixture",
        )
    )
    return result.inserted_primary_key[0]


def _make_orphan_row(
    conn,
    *,
    wallet_group_id: int,
    source_document_id: int,
    transaction_date: _dt.date,
    amount_usd_ref: Decimal,
    external_ref: str,
) -> int:
    amount_idr = amount_usd_ref * Decimal("16400")
    result = conn.execute(
        review_queue.insert().values(
            source_type="payoneer_csv",
            source_document_id=source_document_id,
            wallet_group_id=wallet_group_id,
            external_ref=external_ref,
            transaction_date=transaction_date,
            amount_idr=amount_idr,
            amount_usd_ref=amount_usd_ref,
            raw_description=f"Payment from eBay (Additional Description: '', no matching expected payout)",
            match_status="needs_review",
            category=None,
        )
    )
    return result.inserted_primary_key[0]


def _post_real_duplicate_transfer(
    conn, *, ebay_account_id: int, wallet_group_id: int, entry_date: _dt.date, amount_usd_ref: Decimal
) -> int:
    amount_idr = amount_usd_ref * Decimal("16400")
    return post_inter_account_transfer(
        conn,
        entry_date=entry_date,
        from_account_type_code="EBAY_WALLET",
        to_account_type_code="PAYONEER_WALLET",
        amount_idr=amount_idr,
        from_ebay_account_id=ebay_account_id,
        to_wallet_group_id=wallet_group_id,
        amount_usd_ref=amount_usd_ref,
        fx_rate_used=Decimal("16400"),
        memo="eBay payout confirmed arrived in Payoneer",
    )


def test_fresh_orphan_verifies_successfully_against_its_real_duplicate(prototype):
    conn, topo = prototype
    src_id = _make_source_document(conn, wallet_group_id=topo["wallet_group_id"], period_month=_dt.date(2026, 1, 1))
    rq_id = _make_orphan_row(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("2376.64"),
        external_ref="txn-orphan-verify-1",
    )
    je_id = _post_real_duplicate_transfer(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 4, 29),  # 1 day off — within the ±3-day tolerance
        amount_usd_ref=Decimal("2376.64"),
    )

    result = _load_and_verify_one(conn, {"review_queue_id": rq_id, "expected_duplicate_journal_entry_id": je_id})
    assert result == {
        "review_queue_id": rq_id,
        "journal_entry_id": je_id,
        "already_resolved": False,
        "amount_usd_ref": Decimal("2376.64"),
        "transaction_date": _dt.date(2026, 4, 28),
    }


def test_already_resolved_row_is_recognized_as_idempotent(prototype):
    conn, topo = prototype
    src_id = _make_source_document(conn, wallet_group_id=topo["wallet_group_id"], period_month=_dt.date(2026, 1, 1))
    rq_id = _make_orphan_row(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("2376.64"),
        external_ref="txn-orphan-verify-2",
    )
    je_id = _post_real_duplicate_transfer(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("2376.64"),
    )
    # Simulate a prior successful run of the script.
    conn.execute(
        update(review_queue)
        .where(review_queue.c.id == rq_id)
        .values(
            match_status="resolved_duplicate",
            category="revenue_settlement",
            posted_at=_dt.datetime.now(_dt.timezone.utc),
            duplicate_of_journal_entry_id=je_id,
            resolution_note="already resolved by a prior run",
        )
    )

    result = _load_and_verify_one(conn, {"review_queue_id": rq_id, "expected_duplicate_journal_entry_id": je_id})
    assert result["already_resolved"] is True


def test_amount_mismatch_against_expected_duplicate_raises_state_mismatch(prototype):
    conn, topo = prototype
    src_id = _make_source_document(conn, wallet_group_id=topo["wallet_group_id"], period_month=_dt.date(2026, 1, 1))
    rq_id = _make_orphan_row(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("2376.64"),
        external_ref="txn-orphan-verify-3",
    )
    # A real entry exists, but for a DIFFERENT amount — must not be
    # silently accepted as the duplicate.
    je_id = _post_real_duplicate_transfer(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("999.00"),
    )

    with pytest.raises(_StateMismatch, match="do not match"):
        _load_and_verify_one(conn, {"review_queue_id": rq_id, "expected_duplicate_journal_entry_id": je_id})


def test_date_too_far_apart_raises_state_mismatch(prototype):
    conn, topo = prototype
    src_id = _make_source_document(conn, wallet_group_id=topo["wallet_group_id"], period_month=_dt.date(2026, 1, 1))
    rq_id = _make_orphan_row(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("2376.64"),
        external_ref="txn-orphan-verify-4",
    )
    je_id = _post_real_duplicate_transfer(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 5, 10),  # 12 days away — well outside ±3-day tolerance
        amount_usd_ref=Decimal("2376.64"),
    )

    with pytest.raises(_StateMismatch, match="too far apart"):
        _load_and_verify_one(conn, {"review_queue_id": rq_id, "expected_duplicate_journal_entry_id": je_id})


def test_row_already_labeled_by_a_human_is_never_silently_touched(prototype):
    """Neither the fresh-orphan state nor the already-resolved state — a
    human already labeled this row some other way (e.g. 'other') before
    this script ran. Must abort rather than guess."""
    conn, topo = prototype
    src_id = _make_source_document(conn, wallet_group_id=topo["wallet_group_id"], period_month=_dt.date(2026, 1, 1))
    rq_id = _make_orphan_row(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("2376.64"),
        external_ref="txn-orphan-verify-5",
    )
    je_id = _post_real_duplicate_transfer(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("2376.64"),
    )
    conn.execute(update(review_queue).where(review_queue.c.id == rq_id).values(category="other", match_status="matched"))

    with pytest.raises(_StateMismatch, match="neither the expected"):
        _load_and_verify_one(conn, {"review_queue_id": rq_id, "expected_duplicate_journal_entry_id": je_id})


def test_wrong_wallet_group_payoneer_line_raises_state_mismatch(full_topology):
    """The expected journal_entry_id's PAYONEER_WALLET line must be scoped
    to the SAME wallet-group as the orphan row — a real entry for a
    DIFFERENT wallet-group with a coincidentally matching amount/date must
    never be accepted."""
    conn, topo = full_topology
    shared_wallet_group_id = topo["wallet_groups"]["shared"]
    independent_wallet_group_id = topo["wallet_groups"]["independent"]
    account_independent = topo["ebay_accounts"]["3"]  # in the "independent" wallet-group
    assert shared_wallet_group_id != independent_wallet_group_id

    src_id = _make_source_document(conn, wallet_group_id=shared_wallet_group_id, period_month=_dt.date(2026, 1, 1))
    rq_id = _make_orphan_row(
        conn,
        wallet_group_id=shared_wallet_group_id,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("500.00"),
        external_ref="txn-orphan-verify-6",
    )
    # Real entry exists for the SAME amount/date, but under the
    # INDEPENDENT wallet-group, not the orphan row's own "shared" one.
    je_id = _post_real_duplicate_transfer(
        conn,
        ebay_account_id=account_independent,
        wallet_group_id=independent_wallet_group_id,
        entry_date=_dt.date(2026, 4, 28),
        amount_usd_ref=Decimal("500.00"),
    )

    with pytest.raises(_StateMismatch, match="no PAYONEER_WALLET line"):
        _load_and_verify_one(conn, {"review_queue_id": rq_id, "expected_duplicate_journal_entry_id": je_id})
