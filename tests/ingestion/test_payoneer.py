"""Tests for ingestion.payoneer against the real samples: the Payoneer CSV
export(s) and the withdrawal confirmation PDF(s).

Real sample layout replaced 2026-09-01: the old `payoneer/` folder (a single
"Transactions page"-format CSV export + a single confirmation PDF) is gone,
replaced by `Payoneer/` — 4 real "Reports & Statements"-format
`report_*.csv` exports (May-Aug 2026) plus a `Confirmation of Transfer/
<Month>/` subfolder with 2-3 real confirmations each (11 total). This is a
**second, structurally different real Payoneer CSV export shape**
(confirmed against CLAUDE.md's own text, which names both "Transactions
page" and "Reports & Statements" as valid export sources) — see
ingestion/payoneer.py's `_normalize_reports_statements_row` docstring for
the column-shape differences and how they're normalized away before
anything downstream needs format-awareness.

CSV_SAMPLE below is the real July report (chosen for continuity with the
other ingestion tests' shared July 2026 period). CONFIRMATION_SAMPLE is the
same real confirmation the original milestone-3 sample used
(Transfer ID 4366185623014087) — carried over into the new folder structure
with identical figures, confirmed by inspection.
"""
from __future__ import annotations

import datetime as _dt
import glob
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

PAYONEER_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "Payoneer"
CSV_SAMPLE = PAYONEER_DIR / "report_2026-09-01_01-00-20.csv"  # July 2026
CONFIRMATION_SAMPLE = PAYONEER_DIR / "Confirmation of Transfer" / "Jul" / "Confirmation_of_Transfer_4366185623014087.pdf"

ALL_REPORT_CSVS = sorted(PAYONEER_DIR.glob("report_*.csv"))
ALL_CONFIRMATION_PDFS = sorted(PAYONEER_DIR.glob("Confirmation of Transfer/*/*.pdf"))


def test_parse_real_csv_sample_normalizes_reports_statements_format():
    """The real July report has 6 rows (4 'Payment from eBay' + 2
    'Withdrawal to BANK MANDIRI (7498)') — fewer than the old sample's 7,
    since this is genuinely different real-world data, not the same file
    moved. Confirms the "Reports & Statements" shape (Date/single signed
    Amount/no Reference ID) normalizes into the same canonical
    (Transaction Date MM/DD/YYYY, Credit/Debit Amount, ...) shape
    process_payoneer_rows already expects.
    """
    rows = parse_payoneer_csv_rows(CSV_SAMPLE.read_text(encoding="utf-8-sig"))
    assert len(rows) == 6
    descriptions = [r["Description"] for r in rows]
    assert descriptions.count("Payment from eBay") == 4
    assert descriptions.count("Withdrawal to BANK MANDIRI (7498)") == 2
    assert all(r["Status"] == "Completed" for r in rows)
    # Canonical shape — the OLD "Transactions page" columns, not the raw
    # "Date"/"Amount" columns the real file actually has.
    for r in rows:
        assert set(r.keys()) == {
            "Transaction Date", "Description", "Credit Amount", "Debit Amount",
            "Status", "Reference ID", "Additional Description", "Transaction ID",
        }
        assert r["Transaction Date"].count("/") == 2  # MM/DD/YYYY, not "28 Jul, 2026"


def test_parse_all_four_real_report_csvs_no_crash():
    """Fix 3 validation pass: every real report_*.csv (including the August
    one with 'Card charge (...)' rows — a Description value not seen in any
    other real sample) parses without error.
    """
    assert len(ALL_REPORT_CSVS) == 4
    for p in ALL_REPORT_CSVS:
        rows = parse_payoneer_csv_rows(p.read_text(encoding="utf-8-sig"))
        assert len(rows) > 0
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


def test_parse_all_eleven_real_confirmation_pdfs_no_crash():
    """Fix 3 validation pass: re-validate the confirmation parser against
    the richer real dataset (11 confirmations across 4 months, vs. the
    original single sample) — every one states amount/fee/rate/amount-sent
    explicitly, per CLAUDE.md's "use those stated figures directly" rule.
    """
    assert len(ALL_CONFIRMATION_PDFS) == 11
    for p in ALL_CONFIRMATION_PDFS:
        c = parse_confirmation_pdf(p)
        assert c.amount_withdrawn_usd > 0
        assert c.fee_usd > 0
        assert c.exchange_rate_excl_fee > 0
        # Sanity range only (not an exact re-derivation) — Payoneer's own
        # printed "Amount sent" may round slightly differently than a naive
        # (withdrawn - fee) * rate recomputation; per CLAUDE.md, the stated
        # figures are used directly, never re-derived, so this only confirms
        # the parsed amount_sent_idr is in the right ballpark, not a strict
        # recomputation.
        naive = (c.amount_withdrawn_usd - c.fee_usd) * c.exchange_rate_excl_fee
        assert abs(c.amount_sent_idr - naive) < Decimal("10")
        assert c.beneficiary_bank == "BANK MANDIRI (7498)"


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
    # Synthetic "Transactions page"-shaped row — real coverage for the
    # Additional-Description/Payout-ID-based match no longer exists (the
    # real samples collected since are all "Reports & Statements" format,
    # which has no Additional Description column at all — see
    # test_ebay_payment_row_with_real_july_data_matches_via_amount_date_
    # fallback below for the now-representative real path). This synthetic
    # row keeps the payout-ID branch itself exercised rather than letting it
    # silently go untested once its real-sample coverage disappeared.
    payment_row = [
        {
            "Transaction Date": "04/28/2026",
            "Description": "Payment from eBay",
            "Credit Amount": "2376.64",
            "Debit Amount": "",
            "Status": "Completed",
            "Reference ID": "",
            "Additional Description": "P 7474334928",
            "Transaction ID": "986000001",
        }
    ]
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


def test_ebay_payment_row_with_real_july_data_matches_via_amount_date_fallback(iprototype):
    """The now-representative real path: the "Reports & Statements" export
    has no Additional Description/Reference ID at all, so every real
    "Payment from eBay" row relies on the amount+date fallback match. Uses
    the real July report CSV against ebay_expected_payouts figures taken
    directly from the real July eBay sales CSV's own Payout rows (see
    tests/ingestion/test_ebay_csv.py) — genuine cross-file real-data
    validation, not synthetic figures.
    """
    conn, topo = iprototype
    # July's weekly rates (2026-07-06 through 2026-07-27) are already seeded
    # by the iprototype fixture (tests/ingestion/conftest.py) — not
    # re-seeded here.
    for payout_id, payout_date, net_usd in [
        ("7647445176", _dt.date(2026, 7, 28), "3470.09"),
        ("7634660616", _dt.date(2026, 7, 21), "2027.90"),
        ("7620433464", _dt.date(2026, 7, 14), "3657.72"),
        ("7606147008", _dt.date(2026, 7, 7), "940.23"),
    ]:
        conn.execute(
            ebay_expected_payouts.insert().values(
                ebay_account_id=topo["ebay_account_id"],
                ebay_payout_id=payout_id,
                payout_date=payout_date,
                net_amount_usd=Decimal(net_usd),
            )
        )

    rows = parse_payoneer_csv_rows(CSV_SAMPLE.read_text(encoding="utf-8-sig"))
    payment_rows = [r for r in rows if r["Description"] == "Payment from eBay"]
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 7, 1), wallet_group_id=topo["wallet_group_id"]
    )

    result = process_payoneer_rows(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        rows=payment_rows,
        confirmations=[],
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )
    assert result.revenue_settlements_posted == 4
    assert result.staged_for_review == 0
    assert all(
        m is not None
        for m in conn.execute(select(ebay_expected_payouts.c.matched_at)).scalars().all()
    )


def test_ebay_payment_row_with_no_matching_payout_stages_for_review_not_a_bug(iprototype):
    """Per CLAUDE.md's Prototype scope note: this is expected when the
    wallet-group's export contains settlements for a not-yet-onboarded
    sibling eBay account.
    """
    conn, topo = iprototype
    # July's weekly rates are already seeded by the iprototype fixture.
    rows = parse_payoneer_csv_rows(CSV_SAMPLE.read_text(encoding="utf-8-sig"))
    payment_rows = [r for r in rows if r["Description"] == "Payment from eBay"]
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 7, 1), wallet_group_id=topo["wallet_group_id"]
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
    # Synthetic withdrawal row matching the confirmation's own figures — kept
    # synthetic (rather than the real July CSV row, which has no Reference
    # ID to match by in the new export format) specifically to exercise the
    # Reference-ID-based match path directly; the amount+date fallback path
    # is exercised for real in test_real_withdrawal_rows_match_via_amount_
    # date_fallback_across_all_months below.
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


def test_real_withdrawal_rows_match_via_amount_date_fallback_across_all_months(iprototype):
    """The real "Reports & Statements" export has no Reference ID at all, so
    every real withdrawal row relies on the amount+date fallback against the
    confirmation PDFs — validated here across all 4 real months' CSVs and
    all 11 real confirmations together (richer real-data validation than
    the original single-CSV/single-confirmation design could exercise).
    """
    conn, topo = iprototype
    # 2026-06-29 through 2026-08-03 are already seeded by the iprototype
    # fixture — not re-seeded here.
    for d in (_dt.date(2026, 4, 27), _dt.date(2026, 5, 4), _dt.date(2026, 5, 11), _dt.date(2026, 5, 18),
              _dt.date(2026, 5, 25), _dt.date(2026, 6, 1), _dt.date(2026, 6, 8), _dt.date(2026, 6, 15),
              _dt.date(2026, 6, 22), _dt.date(2026, 8, 10),
              _dt.date(2026, 8, 17), _dt.date(2026, 8, 24), _dt.date(2026, 8, 31)):
        seed_kurs_pajak_rate(conn, effective_date=d, rate_idr=Decimal("16300"))

    confirmations = [parse_confirmation_pdf(p) for p in ALL_CONFIRMATION_PDFS]
    total_withdrawals_posted = 0
    for i, csv_path in enumerate(ALL_REPORT_CSVS):
        rows = parse_payoneer_csv_rows(csv_path.read_text(encoding="utf-8-sig"))
        withdrawal_rows = [r for r in rows if r["Description"].startswith("Withdrawal to")]
        src_id = make_source_document(
            conn,
            document_type="payoneer_csv",
            period_month=_dt.date(2026, 5 + i, 1),
            wallet_group_id=topo["wallet_group_id"],
        )
        result = process_payoneer_rows(
            conn,
            wallet_group_id=topo["wallet_group_id"],
            source_document_id=src_id,
            rows=withdrawal_rows,
            confirmations=confirmations,
            booking_rate_lookup=lookup_most_recent_rate_as_of,
        )
        assert result.parse_warnings == [], f"{csv_path.name}: {result.parse_warnings}"
        total_withdrawals_posted += result.withdrawals_posted

    # 11 real confirmations, all matched to their corresponding CSV
    # withdrawal row across the 4 monthly files (12 withdrawal CSV rows
    # total across the 4 files; 11 have a matching confirmation — see
    # ingestion/mandiri_statement.py cross-check notes / Builder's report).
    assert total_withdrawals_posted == 11
    wd_count = conn.execute(select(payoneer_withdrawals.c.id)).all()
    assert len(wd_count) == 11


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


def test_real_august_card_charge_rows_stage_for_review_not_a_crash(iprototype):
    """The real August report introduces a new real Description shape never
    seen before ('Card charge (OPENAI *CHATGPT SUBSCR)', etc. — the
    Payoneer card being used directly for subscriptions). Confirms these
    fall through to the generic auto-match engine (Needs Review by default)
    rather than crashing or being silently mis-posted as a withdrawal/
    eBay-payment.
    """
    conn, topo = iprototype
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 24), rate_idr=Decimal("16400"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 26), rate_idr=Decimal("16400"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 28), rate_idr=Decimal("16400"))
    august_csv = PAYONEER_DIR / "report_2026-09-01_01-12-48.csv"
    rows = parse_payoneer_csv_rows(august_csv.read_text(encoding="utf-8-sig"))
    card_rows = [r for r in rows if r["Description"].startswith("Card charge")]
    assert len(card_rows) == 5

    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 8, 1), wallet_group_id=topo["wallet_group_id"]
    )
    result = process_payoneer_rows(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_id,
        rows=card_rows,
        confirmations=[],
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )
    assert result.staged_for_review == 5
    assert conn.execute(select(journal_entries.c.id)).all() == []
