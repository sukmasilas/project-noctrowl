"""Tests for ingestion.payoneer against the real samples: the Payoneer CSV
export and the withdrawal confirmation PDF.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from ingestion.kurs_pajak import lookup_most_recent_rate_as_of, seed_kurs_pajak_rate
from ingestion.payoneer import (
    parse_confirmation_pdf,
    parse_payoneer_csv_rows,
    process_payoneer_rows,
)
from ingestion.schema import ebay_expected_payouts, review_queue
from ledger.schema import journal_entries, journal_lines, payoneer_withdrawals
from tests.ingestion.conftest import make_source_document

CSV_SAMPLE = Path(__file__).resolve().parents[2] / "sample-documents" / "payoneer" / "Payoneer_Transactions_04-2026.csv"
CONFIRMATION_SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "sample-documents"
    / "payoneer"
    / "Payoneer_Confirmation_of_Transfer_4366185623014087.pdf"
)


def test_parse_real_csv_sample():
    rows = parse_payoneer_csv_rows(CSV_SAMPLE.read_text(encoding="utf-8-sig"))
    assert len(rows) == 7
    types = [r["Description"] for r in rows]
    assert types.count("Payment from eBay") == 4
    assert types.count("Withdrawal to BANK MANDIRI (7498)") == 3
    assert all(r["Status"] == "Completed" for r in rows)


def test_parse_real_confirmation_pdf():
    confirmation = parse_confirmation_pdf(CONFIRMATION_SAMPLE)
    assert confirmation.transaction_id == "1016018157"
    assert confirmation.transfer_id == "4366185623014087"
    assert confirmation.amount_withdrawn_usd == Decimal("5000.00")
    assert confirmation.fee_usd == Decimal("200.00")
    assert confirmation.exchange_rate_excl_fee == Decimal("17968.32")
    assert confirmation.amount_sent_idr == Decimal("86247936.00")
    assert confirmation.beneficiary_bank == "BANK MANDIRI (7498)"
    assert confirmation.date_time_utc == _dt.datetime(2026, 7, 28, 1, 5, tzinfo=_dt.timezone.utc)


def test_ebay_payment_row_matches_expected_payout_and_posts_transfer(iprototype):
    conn, topo = iprototype
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 4, 25), rate_idr=Decimal("16400"))
    conn.execute(
        ebay_expected_payouts.insert().values(
            ebay_account_id=topo["ebay_account_id"],
            ebay_payout_id="7474334928",
            payout_date=_dt.date(2026, 4, 28),
            net_amount_usd=Decimal("2376.64"),
        )
    )
    rows = parse_payoneer_csv_rows(CSV_SAMPLE.read_text(encoding="utf-8-sig"))
    payment_row = [r for r in rows if r["Additional Description"] == "P 7474334928"]
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 4, 1), wallet_group_id=topo["wallet_group_id"]
    )

    result = process_payoneer_rows(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        rows=payment_row,
        confirmations=[],
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )

    assert result.revenue_settlements_posted == 1
    assert result.staged_for_review == 0

    entry = conn.execute(select(journal_entries.c.source_type)).scalar_one()
    assert entry == "inter_account_transfer"
    lines = conn.execute(select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr, journal_lines.c.amount_usd_ref)).all()
    assert sum(l.debit_amount_idr for l in lines) == sum(l.credit_amount_idr for l in lines)
    assert all(l.amount_usd_ref == Decimal("2376.64") for l in lines)

    consumed = conn.execute(select(ebay_expected_payouts.c.matched_at)).scalar_one()
    assert consumed is not None


def test_ebay_payment_row_with_no_matching_payout_stages_for_review_not_a_bug(iprototype):
    """Per CLAUDE.md's Prototype scope note: this is expected when the
    wallet-group's export contains settlements for a not-yet-onboarded
    sibling eBay account.
    """
    conn, topo = iprototype
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 4, 1), rate_idr=Decimal("16400"))
    rows = parse_payoneer_csv_rows(CSV_SAMPLE.read_text(encoding="utf-8-sig"))
    payment_rows = [r for r in rows if r["Description"] == "Payment from eBay"]
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 4, 1), wallet_group_id=topo["wallet_group_id"]
    )

    result = process_payoneer_rows(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        rows=payment_rows,
        confirmations=[],
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )
    assert result.revenue_settlements_posted == 0
    assert result.staged_for_review == 4
    rq_rows = conn.execute(select(review_queue.c.match_status, review_queue.c.category)).all()
    assert all(r.match_status == "needs_review" and r.category is None for r in rq_rows)


def test_withdrawal_row_matched_to_confirmation_posts_realized_fx(iprototype):
    conn, topo = iprototype
    confirmation = parse_confirmation_pdf(CONFIRMATION_SAMPLE)
    # Synthetic withdrawal row matching the confirmation's own figures (the
    # real CSV sample is a different month than the real confirmation PDF
    # sample — see sample-documents/README.md — so this pairing is built
    # from the confirmation's own real numbers, not the CSV's).
    withdrawal_row = {
        "Transaction Date": "07/28/2026",
        "Description": "Withdrawal to BANK MANDIRI (7498)",
        "Credit Amount": "",
        "Debit Amount": "5000.00",
        "Status": "Completed",
        "Reference ID": "#4366185623014087",
        "Transaction ID": "1016018157",
    }
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 7, 1), wallet_group_id=topo["wallet_group_id"]
    )

    result = process_payoneer_rows(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        rows=[withdrawal_row],
        confirmations=[confirmation],
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )

    assert result.withdrawals_posted == 1
    wd = conn.execute(
        select(payoneer_withdrawals.c.gross_usd, payoneer_withdrawals.c.payoneer_fee_usd, payoneer_withdrawals.c.net_idr_landed)
    ).one()
    assert wd.gross_usd == Decimal("5000.00")
    assert wd.payoneer_fee_usd == Decimal("200.00")
    assert wd.net_idr_landed == Decimal("4800.00") * Decimal("17968.32")


def test_withdrawal_row_without_confirmation_never_posts_and_warns(iprototype):
    conn, topo = iprototype
    withdrawal_row = {
        "Transaction Date": "07/28/2026",
        "Description": "Withdrawal to BANK MANDIRI (7498)",
        "Credit Amount": "",
        "Debit Amount": "5000.00",
        "Status": "Completed",
        "Reference ID": "#doesnotmatchanything",
        "Transaction ID": "1016018157",
    }
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 7, 1), wallet_group_id=topo["wallet_group_id"]
    )
    result = process_payoneer_rows(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        rows=[withdrawal_row],
        confirmations=[],
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )
    assert result.withdrawals_posted == 0
    assert len(result.parse_warnings) == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []
