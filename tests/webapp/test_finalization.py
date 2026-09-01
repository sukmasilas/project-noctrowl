"""Tests for webapp/finalization.py — new logic with no prior milestone to
lean on (per Main-agent's explicit instruction this needs real coverage).
"""
from __future__ import annotations

import datetime as _dt

from webapp.finalization import expected_by, missing_source_documents, report_status, review_queue_status

from tests.webapp.conftest import make_review_queue_row, make_source_document


def test_expected_by_is_seven_days_after_month_end():
    assert expected_by(_dt.date(2026, 7, 1)) == _dt.date(2026, 8, 7)
    # A 30-day month.
    assert expected_by(_dt.date(2026, 4, 1)) == _dt.date(2026, 5, 7)
    # A leap-relevant Feb (2028 is a leap year).
    assert expected_by(_dt.date(2028, 2, 1)) == _dt.date(2028, 3, 7)


def test_review_queue_status_counts_only_needs_review_in_period_and_scope(wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=period, wallet_group_id=topo["wallet_group_id"])
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 5),
        match_status="needs_review",
        wallet_group_id=topo["wallet_group_id"],
    )
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 10),
        match_status="matched",
        wallet_group_id=topo["wallet_group_id"],
    )
    # Outside the period — must not be counted.
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 8, 1),
        match_status="needs_review",
        wallet_group_id=topo["wallet_group_id"],
    )
    conn.commit()

    count = review_queue_status(conn, period_month=period, ebay_account_id=topo["ebay_account_id"], wallet_group_id=topo["wallet_group_id"])
    assert count == 1


def test_review_queue_status_account_or_shared_wallet_group_row_both_count(wtopology):
    """A per-account report must also count an unresolved row scoped only
    to its wallet-group (a shared-pool Payoneer/bank line) — not just rows
    tagged with that exact ebay_account_id. See finalization.py's
    docstring for the reasoning.
    """
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    src_id = make_source_document(conn, document_type="payoneer_csv", period_month=period, wallet_group_id=topo["wallet_group_id"])
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 5),
        match_status="needs_review",
        wallet_group_id=topo["wallet_group_id"],
        ebay_account_id=None,
    )
    conn.commit()

    count = review_queue_status(
        conn, period_month=period, ebay_account_id=topo["ebay_account_id"], wallet_group_id=topo["wallet_group_id"]
    )
    assert count == 1


def test_missing_source_documents_all_missing_when_nothing_ingested(wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    missing = missing_source_documents(conn, period_month=period, ebay_account_id=topo["ebay_account_id"], wallet_group_id=topo["wallet_group_id"])
    doc_types = {m.document_type for m in missing}
    assert doc_types == {"ebay_sales_csv", "payoneer_csv", "bank_statement_wallet_group"}


def test_missing_source_documents_clears_once_ingested(wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    make_source_document(conn, document_type="ebay_sales_csv", period_month=period, ebay_account_id=topo["ebay_account_id"])
    make_source_document(conn, document_type="payoneer_csv", period_month=period, wallet_group_id=topo["wallet_group_id"])
    make_source_document(conn, document_type="bank_statement_wallet_group", period_month=period, wallet_group_id=topo["wallet_group_id"])
    conn.commit()

    missing = missing_source_documents(conn, period_month=period, ebay_account_id=topo["ebay_account_id"], wallet_group_id=topo["wallet_group_id"])
    assert missing == []


def test_consolidated_missing_documents_inherits_per_account_expectations(wtopology):
    """Main-agent's 2026-09-01 resolution: Consolidated is Provisional if
    ANY per-account/wallet-group document is missing, not just the master
    bank statement's own expectation.
    """
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    # Only the master statement arrives — nothing else.
    make_source_document(conn, document_type="bank_statement_master", period_month=period)
    conn.commit()

    missing = missing_source_documents(conn, period_month=period)  # consolidated: both None
    doc_types = {m.document_type for m in missing}
    assert "ebay_sales_csv" in doc_types
    assert "payoneer_csv" in doc_types
    assert "bank_statement_wallet_group" in doc_types
    assert "bank_statement_master" not in doc_types  # this one WAS ingested


def test_consolidated_final_once_everything_ingested_and_reviewed(wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    for document_type, scope in [
        ("ebay_sales_csv", {"ebay_account_id": topo["ebay_account_id"]}),
        ("payoneer_csv", {"wallet_group_id": topo["wallet_group_id"]}),
        ("bank_statement_wallet_group", {"wallet_group_id": topo["wallet_group_id"]}),
        ("bank_statement_master", {}),
    ]:
        make_source_document(conn, document_type=document_type, period_month=period, **scope)
    conn.commit()

    status = report_status(conn, period_month=period)
    assert status.is_final is True
    assert status.needs_review_count == 0
    assert status.missing_documents == []


def test_report_never_final_with_unresolved_review_row_even_if_docs_all_arrived(wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    src_id = make_source_document(conn, document_type="ebay_sales_csv", period_month=period, ebay_account_id=topo["ebay_account_id"])
    make_source_document(conn, document_type="payoneer_csv", period_month=period, wallet_group_id=topo["wallet_group_id"])
    make_source_document(conn, document_type="bank_statement_wallet_group", period_month=period, wallet_group_id=topo["wallet_group_id"])
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 5),
        match_status="needs_review",
        ebay_account_id=topo["ebay_account_id"],
    )
    conn.commit()

    status = report_status(conn, period_month=period, ebay_account_id=topo["ebay_account_id"])
    assert status.is_final is False
    assert status.needs_review_count == 1
    assert "1 item" in status.reasons[0]


def test_report_provisional_on_zero_uploads_not_indistinguishable_from_clean_period(wtopology):
    """A period with literally nothing uploaded has zero review-queue rows
    too — must still be Provisional (per CLAUDE.md's Report finalization
    status section), not accidentally read as "nothing to review".
    """
    conn, topo = wtopology
    period = _dt.date(2026, 9, 1)  # nothing seeded for this period at all
    status = report_status(conn, period_month=period, ebay_account_id=topo["ebay_account_id"])
    assert status.is_final is False
    assert status.needs_review_count == 0
    assert len(status.missing_documents) > 0
