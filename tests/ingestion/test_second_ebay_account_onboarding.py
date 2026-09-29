"""Real, first-ever exercise of onboarding a genuinely SECOND eBay account
sharing an EXISTING wallet-group with real data (CLAUDE.md's Business
model/Prototype scope sections anticipated exactly this shape since the
project began, but it had never been tested against real August 2026 data
for both accounts until now).

Covers three things the onboarding brief specifically asked to verify:
  1. The second account's real eBay CSV ingests independently, correctly
     scoped (its own EBAY_WALLET, its own ebay_expected_payouts rows),
     without touching or double-counting eBay Account 1's existing data.
  2. The GENERAL "orphaned review_queue row resolves once the sibling
     account's expected-payout data exists" mechanism (``ingestion.matching
     .run_auto_match``'s rule (a), wallet-group-scoped) genuinely works —
     demonstrated in isolation with amounts that align, as 17 of the real
     dataset's 18 real "Payment from eBay" Payoneer credits actually do.
  3. The ONE specific real orphaned row this onboarding was expected to
     resolve (the real Aug 24, 2026 $14.79 Payoneer credit) does NOT
     actually auto-resolve, because eBay's own CSV states that payout as
     -$15.79 — a real, unexplained $1.00 gap, outside the existing $0.01
     matching tolerance. This is a genuine finding, not a bug in the
     matching engine (which is correctly wallet-group-scoped — see test
     #2) and not something this task fixes by loosening the tolerance
     (CLAUDE.md: never guess on money math). The row correctly stays
     Needs Review for human judgment — flagged to Main-agent/QA rather
     than silently forced to match or silently left unexplained.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from ingestion.ebay_csv import parse_ebay_csv_rows, process_transaction_report
from ingestion.kurs_pajak import lookup_most_recent_rate_as_of, seed_kurs_pajak_rate
from ingestion.matching import RawLine, run_auto_match, stage_raw_lines
from ingestion.payoneer import parse_confirmation_pdf, parse_payoneer_csv_rows, process_payoneer_rows
from ingestion.schema import ebay_expected_payouts, review_queue
from ledger.balances import account_balance_through
from ledger.entities import get_account_id
from ledger.schema import journal_entries, journal_lines
from scripts.onboard_ebay_account import onboard_ebay_account
from tests.ingestion.conftest import make_source_document

ACCOUNT_1_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "eBay account 1_ricky-game"
ACCOUNT_2_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "eBay account 2_ricky-garage"
PAYONEER_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "Payoneer"

ACCOUNT_1_AUGUST_CSV = ACCOUNT_1_DIR / "Transaction_report_20260801_20260831.csv"
ACCOUNT_2_AUGUST_CSV = ACCOUNT_2_DIR / "Transaction_report_20260801_20260831.csv"
AUGUST_PAYONEER_CSV = PAYONEER_DIR / "report_2026-09-01_01-12-48.csv"
AUGUST_CONFIRMATION_DIR = PAYONEER_DIR / "Confirmation of Transfer" / "Aug"


def _seed_august_kurs_pajak_rates(conn) -> None:
    # iprototype already seeds through 2026-08-03 — see tests/ingestion/
    # conftest.py. These cover the rest of August (same rates
    # test_all_real_monthly_samples_parse_and_post_cleanly uses).
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 10), rate_idr=Decimal("16410"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 17), rate_idr=Decimal("16420"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 24), rate_idr=Decimal("16430"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 31), rate_idr=Decimal("16440"))


def test_second_account_csv_ingests_independently_without_touching_account_1(iprototype):
    conn, topo = iprototype
    _seed_august_kurs_pajak_rates(conn)

    # --- Ingest Account 1's real August CSV first (its normal, pre-existing
    # activity) and record its EBAY_WALLET balance. ---
    _, account_1_rows = parse_ebay_csv_rows(ACCOUNT_1_AUGUST_CSV.read_text(encoding="utf-8-sig"))
    src_1 = make_source_document(
        conn, document_type="ebay_sales_csv", period_month=_dt.date(2026, 8, 1), ebay_account_id=topo["ebay_account_id"]
    )
    result_1 = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_1, rows=account_1_rows
    )
    assert result_1.parse_warnings == []
    assert result_1.orders_posted > 0
    assert result_1.payouts_recorded == 4

    account_1_balance_before = account_balance_through(
        conn, topo["ebay_wallet_id"], _dt.date(2026, 8, 1), normal_balance="debit"
    )

    # --- Onboard Account 2 into the SAME (now shared) wallet-group. ---
    outcome = onboard_ebay_account(
        conn,
        name="eBay Account 2",
        wallet_group_id=topo["wallet_group_id"],
        ebay_seller_username="ricky.garage",
        drive_folder_name="eBay Account - 2 (ricky-garage)",
    )
    account_2_id = outcome["ebay_account_id"]
    account_2_ebay_wallet_id = outcome["ebay_wallet_account_id"]

    # --- Ingest Account 2's real August CSV. ---
    _, account_2_rows = parse_ebay_csv_rows(ACCOUNT_2_AUGUST_CSV.read_text(encoding="utf-8-sig"))
    src_2 = make_source_document(
        conn, document_type="ebay_sales_csv", period_month=_dt.date(2026, 8, 1), ebay_account_id=account_2_id
    )
    result_2 = process_transaction_report(
        conn, ebay_account_id=account_2_id, source_document_id=src_2, rows=account_2_rows
    )

    # Real, confirmed counts for this file (see task investigation): 237
    # data rows -> 6 Order, 229 Other fee, 1 Refund, 1 Payout, 0 Hold.
    assert result_2.orders_posted == 6
    assert result_2.refunds_posted == 1
    assert result_2.other_fees_posted == 229
    assert result_2.holds_skipped == 0
    assert result_2.payouts_recorded == 1
    assert result_2.consignment_sales_created == 0
    assert result_2.parse_warnings == []

    # --- Account 1's own topology/balance is completely untouched. ---
    assert get_account_id(conn, "EBAY_WALLET", ebay_account_id=topo["ebay_account_id"]) == topo["ebay_wallet_id"]
    account_1_balance_after = account_balance_through(
        conn, topo["ebay_wallet_id"], _dt.date(2026, 8, 1), normal_balance="debit"
    )
    assert account_1_balance_after == account_1_balance_before

    # --- Account 2's activity landed in its OWN EBAY_WALLET, independently. ---
    account_2_balance = account_balance_through(conn, account_2_ebay_wallet_id, _dt.date(2026, 8, 1), normal_balance="debit")
    assert account_2_balance != Decimal("0")

    # --- Global double-entry invariant across BOTH accounts' postings. ---
    total_debit = conn.execute(select(journal_lines.c.debit_amount_idr)).scalars().all()
    total_credit = conn.execute(select(journal_lines.c.credit_amount_idr)).scalars().all()
    assert sum(total_debit) == sum(total_credit)

    # --- ebay_expected_payouts correctly scoped per account. ---
    account_1_payouts = conn.execute(
        select(ebay_expected_payouts.c.net_amount_usd).where(
            ebay_expected_payouts.c.ebay_account_id == topo["ebay_account_id"]
        )
    ).scalars().all()
    assert len(account_1_payouts) == 4  # unaffected by account 2 onboarding

    account_2_payouts = conn.execute(
        select(
            ebay_expected_payouts.c.net_amount_usd,
            ebay_expected_payouts.c.payout_date,
            ebay_expected_payouts.c.ebay_payout_id,
        ).where(ebay_expected_payouts.c.ebay_account_id == account_2_id)
    ).all()
    assert len(account_2_payouts) == 1
    assert account_2_payouts[0].net_amount_usd == Decimal("15.79")
    assert account_2_payouts[0].payout_date == _dt.date(2026, 8, 24)
    assert account_2_payouts[0].ebay_payout_id == "7689718266"

    # --- Journal entries correctly attribute to the right EBAY_WALLET —
    # no leakage of account 2's lines onto account 1's wallet or vice versa.
    account_1_line_count = conn.execute(
        select(journal_lines.c.id).where(journal_lines.c.account_id == topo["ebay_wallet_id"])
    ).all()
    account_2_line_count = conn.execute(
        select(journal_lines.c.id).where(journal_lines.c.account_id == account_2_ebay_wallet_id)
    ).all()
    assert len(account_1_line_count) > 0
    assert len(account_2_line_count) > 0
    # Sanity: the two EBAY_WALLET accounts really are different instances.
    assert topo["ebay_wallet_id"] != account_2_ebay_wallet_id


def test_orphaned_review_queue_row_resolves_once_sibling_accounts_payout_exists(prototype):
    """The GENERAL mechanism, isolated from the real data's $1.00 gap (see
    the module docstring and the next test) — proves rule (a) genuinely is
    wallet-group-scoped and DOES resolve an orphaned "no matching expected
    payout" row once the sibling account's own expected-payout data shows
    up, for amounts that align exactly (as 17 of the real dataset's 18 real
    pairs do — see ingestion/payoneer.py real-sample cross-check in the
    onboarding investigation).
    """
    conn, topo = prototype  # ledger-only prototype fixture — no CSV parsing needed here
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 1), rate_idr=Decimal("16400"))

    outcome = onboard_ebay_account(
        conn,
        name="eBay Account 2",
        wallet_group_id=topo["wallet_group_id"],
        ebay_seller_username="ricky.garage",
        drive_folder_name="eBay Account - 2 (ricky-garage)",
    )
    account_2_id = outcome["ebay_account_id"]

    src_doc_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 8, 1), wallet_group_id=topo["wallet_group_id"]
    )

    # Stage the orphan EXACTLY as ingestion.payoneer._process_ebay_payment_row
    # does for a real "no matching expected payout" credit — BEFORE account
    # 2's own expected-payout data exists anywhere in the database.
    rate = lookup_most_recent_rate_as_of(conn, _dt.date(2026, 8, 24))
    orphan_usd = Decimal("15.79")
    staged_ids = stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_doc_id,
        wallet_group_id=topo["wallet_group_id"],
        ebay_account_id=None,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 8, 24),
                raw_description="Payment from eBay (Additional Description: '', no matching expected payout)",
                amount_idr=orphan_usd * rate,
                amount_usd_ref=orphan_usd,
                external_ref="1028048527",
            )
        ],
    )
    assert len(staged_ids) == 1
    row_id = staged_ids[0]

    # Confirms it stays unmatched — no ebay_expected_payouts row exists at
    # all yet for EITHER account in this isolated scenario.
    result_before = run_auto_match(conn)
    assert result_before.matched == 0
    assert result_before.needs_review == 1
    row = conn.execute(
        select(review_queue.c.category, review_queue.c.match_status).where(review_queue.c.id == row_id)
    ).one()
    assert row.category is None
    assert row.match_status == "needs_review"

    # Now Account 2's own expected payout "arrives" (the real effect of
    # onboarding + ingesting its eBay CSV — see
    # ingestion.ebay_csv._process_payout_row, exercised directly by the
    # previous test; inserted directly here to isolate this mechanism).
    conn.execute(
        ebay_expected_payouts.insert().values(
            ebay_account_id=account_2_id,
            ebay_payout_id="7689718266",
            payout_date=_dt.date(2026, 8, 24),
            net_amount_usd=orphan_usd,
        )
    )

    # A plain re-run of run_auto_match (exactly what the next sync run
    # does — no special "resync this one row" mechanism needed) resolves it.
    result_after = run_auto_match(conn)
    assert result_after.matched == 1
    assert result_after.needs_review == 0

    row_after = conn.execute(
        select(review_queue.c.category, review_queue.c.match_status, review_queue.c.match_rule, review_queue.c.ebay_account_id)
        .where(review_queue.c.id == row_id)
    ).one()
    assert row_after.category == "revenue_settlement"
    assert row_after.match_status == "matched"
    assert row_after.match_rule == "a"
    assert row_after.ebay_account_id == account_2_id

    matched_payout = conn.execute(
        select(ebay_expected_payouts.c.matched_at).where(ebay_expected_payouts.c.ebay_account_id == account_2_id)
    ).one()
    assert matched_payout.matched_at is not None


def test_real_august_14_79_orphan_stays_needs_review_due_to_real_1_dollar_gap(iprototype):
    """THE FINDING: the one real "no matching expected payout" row in the
    available real sample data (the 24 Aug 2026 $14.79 Payoneer credit —
    see the module docstring) does NOT resolve after Account 2 is onboarded
    and its real August CSV is ingested, because eBay's own CSV states that
    same day's Payout as -$15.79 (Payout ID 7689718266), not -$14.79 — an
    exact $1.00 gap, confirmed by direct inspection of both real files, with
    no other nearby real transaction explaining it (see task report). This
    exceeds ingestion.matching's $0.01 amount tolerance, so the row
    correctly, deliberately stays Needs Review — this is the review queue
    working as designed (CLAUDE.md: "never guess on money math" / never
    force a match outside tolerance), not a pipeline bug. Flagged to
    Main-agent/QA as an open question for the user: is this the same event
    with an unexplained $1 difference (e.g. a batching/reserve artifact), or
    genuinely two different things? This test intentionally does NOT loosen
    the matching tolerance to force a resolution.
    """
    conn, topo = iprototype
    _seed_august_kurs_pajak_rates(conn)

    # Account 1's real August CSV — creates its 4 real expected payouts,
    # which the other 4 (of 5) real "Payment from eBay" Payoneer credits
    # correctly match via amount+date fallback (no Additional Description
    # in this real export format — see ingestion/payoneer.py).
    _, account_1_rows = parse_ebay_csv_rows(ACCOUNT_1_AUGUST_CSV.read_text(encoding="utf-8-sig"))
    src_1 = make_source_document(
        conn, document_type="ebay_sales_csv", period_month=_dt.date(2026, 8, 1), ebay_account_id=topo["ebay_account_id"]
    )
    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_1, rows=account_1_rows)

    # Onboard Account 2 + ingest its real August CSV — creates its one real
    # expected payout: net_amount_usd=15.79, payout_date=2026-08-24 (see the
    # previous test for the direct assertion of this).
    outcome = onboard_ebay_account(
        conn,
        name="eBay Account 2",
        wallet_group_id=topo["wallet_group_id"],
        ebay_seller_username="ricky.garage",
        drive_folder_name="eBay Account - 2 (ricky-garage)",
    )
    account_2_id = outcome["ebay_account_id"]
    _, account_2_rows = parse_ebay_csv_rows(ACCOUNT_2_AUGUST_CSV.read_text(encoding="utf-8-sig"))
    src_2 = make_source_document(
        conn, document_type="ebay_sales_csv", period_month=_dt.date(2026, 8, 1), ebay_account_id=account_2_id
    )
    process_transaction_report(conn, ebay_account_id=account_2_id, source_document_id=src_2, rows=account_2_rows)

    # Now process the REAL August Payoneer CSV for this (shared) wallet
    # -group, with the real August withdrawal confirmations available too
    # (so the unrelated withdrawal rows don't generate noise warnings).
    payoneer_rows = parse_payoneer_csv_rows(AUGUST_PAYONEER_CSV.read_text(encoding="utf-8-sig"))
    confirmations = [parse_confirmation_pdf(p) for p in sorted(AUGUST_CONFIRMATION_DIR.glob("*.pdf"))]
    src_payoneer = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 8, 1), wallet_group_id=topo["wallet_group_id"]
    )
    payoneer_result = process_payoneer_rows(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        source_document_id=src_payoneer,
        rows=payoneer_rows,
        confirmations=confirmations,
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )

    # 4 of 5 "Payment from eBay" credits match Account 1's real payouts
    # directly at ingestion time (ingestion.payoneer's own fallback
    # amount+date match, before run_auto_match ever runs); the $14.79 one
    # does not (no candidate — Account 2's expected payout is $15.79, a
    # $1.00 gap outside the $0.01 tolerance) and stages generically instead.
    assert payoneer_result.revenue_settlements_posted == 4
    assert payoneer_result.withdrawals_posted == 3

    orphan_row = conn.execute(
        select(review_queue.c.id, review_queue.c.category, review_queue.c.match_status, review_queue.c.wallet_group_id)
        .where(review_queue.c.amount_usd_ref == Decimal("14.79"))
    ).one()
    assert orphan_row.category is None
    assert orphan_row.match_status == "needs_review"
    assert orphan_row.wallet_group_id == topo["wallet_group_id"]

    # The general auto-match engine ALSO cannot resolve it — confirming
    # this isn't merely a gap in ingestion.payoneer's own inline fallback,
    # but genuinely outside tolerance in the shared rule (a) engine too,
    # even with account 2's real expected payout now fully present.
    auto_match_result = run_auto_match(conn)
    orphan_row_after = conn.execute(
        select(review_queue.c.category, review_queue.c.match_status).where(review_queue.c.id == orphan_row.id)
    ).one()
    assert orphan_row_after.category is None
    assert orphan_row_after.match_status == "needs_review"
    assert auto_match_result.needs_review >= 1

    # And it was never silently posted as a guess.
    posted_check = conn.execute(
        select(review_queue.c.posted_at).where(review_queue.c.id == orphan_row.id)
    ).one()
    assert posted_check.posted_at is None

    # Confirm Account 2's own real expected payout is genuinely still
    # sitting unmatched too (net_amount_usd=15.79) — not consumed by
    # anything else, and not the source of the discrepancy on its own side.
    account_2_payout = conn.execute(
        select(ebay_expected_payouts.c.net_amount_usd, ebay_expected_payouts.c.matched_at).where(
            ebay_expected_payouts.c.ebay_account_id == account_2_id
        )
    ).one()
    assert account_2_payout.net_amount_usd == Decimal("15.79")
    assert account_2_payout.matched_at is None
