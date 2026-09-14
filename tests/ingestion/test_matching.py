"""Tests for ingestion.matching — the shared auto-match priority engine +
posting/idempotency. Synthetic fixtures throughout (this module's logic is
generic across feeds; the real-sample coverage lives in
test_ebay_csv.py/test_bank_statement.py/test_payoneer.py for the parsing
halves that feed it).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from sqlalchemy import select, update

from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.matching import (
    RawLine,
    post_pending_rows,
    run_auto_match,
    stage_raw_lines,
)
from ingestion.schema import bank_keyword_rules, ebay_expected_payouts, invoice_journal_links, invoices, review_queue
from ledger import posting
from ledger.entities import get_account_id
from ledger.schema import consignment_sales, journal_entries, journal_lines
from tests.helpers import assert_balanced, get_lines, lines_by_code
from tests.ingestion.conftest import make_source_document


def _make_bank_source(conn, wallet_group_id=None):
    return make_source_document(
        conn,
        document_type="bank_statement_master" if wallet_group_id is None else "bank_statement_wallet_group",
        period_month=_dt.date(2026, 5, 1),
        **({} if wallet_group_id is None else {"wallet_group_id": wallet_group_id}),
    )


# ---------------------------------------------------------------------------
# stage_raw_lines — row-creation idempotency
# ---------------------------------------------------------------------------


def test_stage_raw_lines_dedupes_by_external_ref(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    lines = [
        RawLine(transaction_date=_dt.date(2026, 5, 3), raw_description="Foo", amount_idr=Decimal("-1000"), external_ref="ext-1"),
    ]
    ids1 = stage_raw_lines(conn, source_type="payoneer_csv", source_document_id=src_id, lines=lines)
    ids2 = stage_raw_lines(conn, source_type="payoneer_csv", source_document_id=src_id, lines=lines)
    assert len(ids1) == 1
    assert ids2 == []  # second pass: already exists, not duplicated
    assert len(conn.execute(select(review_queue.c.id)).all()) == 1


def test_stage_raw_lines_synthesizes_dedup_key_when_no_external_ref(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    lines = [
        RawLine(transaction_date=_dt.date(2026, 5, 3), raw_description="Foo", amount_idr=Decimal("-1000"), occurrence_index=1),
        RawLine(transaction_date=_dt.date(2026, 5, 3), raw_description="Foo", amount_idr=Decimal("-1000"), occurrence_index=2),
    ]
    ids1 = stage_raw_lines(conn, source_type="bank_statement", source_document_id=src_id, lines=lines)
    assert len(ids1) == 2  # two genuinely distinct occurrences, not deduped against each other

    ids2 = stage_raw_lines(conn, source_type="bank_statement", source_document_id=src_id, lines=lines)
    assert ids2 == []  # re-staging the identical file's lines: no new rows
    assert len(conn.execute(select(review_queue.c.id)).all()) == 2


def test_synthetic_external_ref_is_stable_across_separate_processes():
    """Regression test for the 2026-09-02 real live-Drive-run bug: the
    synthetic dedup key used to embed Python's built-in ``hash()`` of the
    raw description, which is randomized per-process (PYTHONHASHSEED) —
    stable within one pytest run (so every OTHER test above, which only
    ever calls stage_raw_lines twice within the SAME process, could never
    catch this), but different across two genuinely separate processes,
    which silently defeated the ON CONFLICT DO NOTHING dedup on a real
    second sync run and double-posted 9 already-posted transactions.

    Proves the fix (hashlib.sha256, not hash()) by actually spawning two
    separate child Python processes with DIFFERENT explicit PYTHONHASHSEED
    values and confirming they compute the identical synthetic
    external_ref for the same input — the exact property a value persisted
    into the database across re-syncs needs, which an in-process-only test
    structurally cannot exercise.
    """
    import os
    import subprocess
    import sys

    snippet = (
        "from ingestion.matching import RawLine, _synthetic_external_ref; "
        "print(_synthetic_external_ref(3, RawLine("
        "transaction_date=__import__('datetime').date(2026, 5, 3), "
        "raw_description='TRSF E-BANKING DB 0608/FTFVA/WS95271 / 80777/TOKOPEDIA', "
        "amount_idr=__import__('decimal').Decimal('-1541400.00'), occurrence_index=1)))"
    )

    def _run_with_seed(seed: str) -> str:
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", snippet], env=env, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    ref_seed_0 = _run_with_seed("0")
    ref_seed_42 = _run_with_seed("42")
    ref_seed_random = _run_with_seed("random")

    assert ref_seed_0 == ref_seed_42 == ref_seed_random
    assert ref_seed_0.startswith("doc3:2026-05-03:")


# ---------------------------------------------------------------------------
# rule (a) — expected eBay payout
# ---------------------------------------------------------------------------


def test_rule_a_matches_expected_payout_and_marks_it_consumed(iprototype):
    conn, topo = iprototype
    conn.execute(
        ebay_expected_payouts.insert().values(
            ebay_account_id=topo["ebay_account_id"],
            ebay_payout_id="PAYOUT-1",
            payout_date=_dt.date(2026, 5, 4),
            net_amount_usd=Decimal("100.00"),
        )
    )
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 5),
                raw_description="Payment from eBay",
                amount_idr=Decimal("1631000"),
                amount_usd_ref=Decimal("100.00"),
                external_ref="txn-a1",
            )
        ],
    )
    result = run_auto_match(conn)
    assert result.matched == 1
    assert result.needs_review == 0

    row = conn.execute(select(review_queue.c.match_status, review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.match_status == "matched"
    assert row.category == "revenue_settlement"
    assert row.match_rule == "a"

    consumed = conn.execute(select(ebay_expected_payouts.c.matched_at)).scalar_one()
    assert consumed is not None


def test_rule_a_matched_row_posts_via_the_generic_review_queue_path(iprototype):
    """QA fix B.3: the common 'Payment from eBay' path posts directly from
    ingestion/payoneer.py and never touches review_queue at all (see
    test_payoneer.py) — so the 'revenue_settlement' branch inside
    _post_one_row (the fallback for when some OTHER feed, e.g. a
    bank-statement line, matches rule (a) generically) had zero test
    coverage of actually being posted through run_auto_match ->
    post_pending_rows. This drives a bank-statement-sourced line through
    exactly that path.
    """
    conn, topo = iprototype
    conn.execute(
        ebay_expected_payouts.insert().values(
            ebay_account_id=topo["ebay_account_id"],
            ebay_payout_id="PAYOUT-GENERIC-1",
            payout_date=_dt.date(2026, 5, 4),
            net_amount_usd=Decimal("250.00"),
        )
    )
    # A bank_statement line (not payoneer_csv), scoped to the wallet-group's
    # own bridging-account statement — rule (a) needs a wallet_group_id (or
    # ebay_account_id) on the row to know which eBay accounts' expected
    # payouts to check against.
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 5),
                raw_description="Incoming eBay-related settlement",
                amount_idr=Decimal("4075000"),
                amount_usd_ref=Decimal("250.00"),
                occurrence_index=1,
            )
        ],
    )

    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(select(review_queue.c.category, review_queue.c.ebay_account_id)).one()
    assert row.category == "revenue_settlement"
    assert row.ebay_account_id == topo["ebay_account_id"]

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry = conn.execute(select(journal_entries.c.source_type)).scalar_one()
    assert entry == "inter_account_transfer"
    lines = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr, journal_lines.c.amount_usd_ref)
    ).all()
    assert sum(l.debit_amount_idr for l in lines) == sum(l.credit_amount_idr for l in lines)
    assert sum(l.debit_amount_idr for l in lines) == Decimal("4075000")
    assert all(l.amount_usd_ref == Decimal("250.00") for l in lines)

    # Never posted twice on a second sync run, same as every other category.
    second_run = post_pending_rows(conn)
    assert second_run.posted == 0


def test_rule_a_no_match_falls_to_needs_review_safely(iprototype):
    """Per CLAUDE.md's Prototype scope: a Payoneer settlement for an
    unknown/not-yet-onboarded eBay account correctly falls to Needs Review —
    expected, not a bug.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 5),
                raw_description="Payment from eBay",
                amount_idr=Decimal("1631000"),
                amount_usd_ref=Decimal("100.00"),
                external_ref="txn-a2",
            )
        ],
    )
    result = run_auto_match(conn)
    assert result.matched == 0
    assert result.needs_review == 1
    row = conn.execute(select(review_queue.c.match_status, review_queue.c.category)).one()
    assert row.match_status == "needs_review"
    assert row.category is None


# ---------------------------------------------------------------------------
# rule (b) — invoice match, including the invoice_journal_links traceability
# write (QA fix A, 2026-09).
# ---------------------------------------------------------------------------


def _make_invoice(conn, *, amount_idr, extracted_date, purpose="cogs_purchase", status="parsed"):
    result = conn.execute(
        invoices.insert().values(
            drive_file_name="test-invoice.pdf",
            period_month=extracted_date.replace(day=1),
            extracted_date=extracted_date,
            vendor_description="Test Vendor",
            amount_idr=amount_idr,
            purpose=purpose,
            status=status,
        )
    )
    return result.inserted_primary_key[0]


def test_rule_b_matches_invoice_posts_cogs_and_writes_traceability_link(iprototype):
    conn, topo = iprototype
    invoice_id = _make_invoice(conn, amount_idr=Decimal("2617600"), extracted_date=_dt.date(2026, 5, 8))

    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 9),
                raw_description="TRSF E-BANKING DB to Brandon Harvest",
                amount_idr=Decimal("-2617600"),
                occurrence_index=1,
            )
        ],
    )

    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(
        select(review_queue.c.category, review_queue.c.match_rule, review_queue.c.linked_invoice_id)
    ).one()
    assert row.category == "cogs_purchase"
    assert row.match_rule == "b"
    assert row.linked_invoice_id == invoice_id

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry = conn.execute(select(journal_entries.c.id, journal_entries.c.source_type)).one()
    assert entry.source_type == "cogs_purchase"  # master-statement outflow -> post_cogs_purchase directly
    lines = conn.execute(select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)).all()
    assert sum(l.debit_amount_idr for l in lines) == sum(l.credit_amount_idr for l in lines)

    # The fix under test: invoice_journal_links now actually gets written.
    link = conn.execute(
        select(invoice_journal_links.c.journal_entry_id, invoice_journal_links.c.invoice_id)
    ).one()
    assert link.invoice_id == invoice_id
    assert link.journal_entry_id == entry.id


def test_rule_b_needs_confirmation_invoice_still_participates_if_amount_present(iprototype):
    """Per design doc §6: only a non-NULL amount_idr is required to
    participate in matching, regardless of parsed/needs_confirmation status.
    """
    conn, topo = iprototype
    _make_invoice(
        conn, amount_idr=Decimal("500000"), extracted_date=_dt.date(2026, 5, 8), status="needs_confirmation"
    )
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 9),
                raw_description="Unlabeled transfer",
                amount_idr=Decimal("-500000"),
                occurrence_index=1,
            )
        ],
    )
    result = run_auto_match(conn)
    assert result.matched == 1
    row = conn.execute(select(review_queue.c.category)).one()
    assert row.category == "cogs_purchase"


def test_consignment_payout_via_invoice_match_also_writes_traceability_link(iprototype):
    """A 'Consignment Purchase' invoice (per CLAUDE.md's clarified meaning:
    proof-of-transfer for a consignor reimbursement, not a stock purchase)
    should post via post_consignor_reimbursement AND still get its
    invoice_journal_links row written — same fix, the other category.
    """
    conn, topo = iprototype
    invoice_id = _make_invoice(
        conn, amount_idr=Decimal("300000"), extracted_date=_dt.date(2026, 5, 8), purpose="consignment_purchase"
    )
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 9),
                raw_description="Proof of transfer to consignor",
                amount_idr=Decimal("-300000"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.category)).one()
    assert row.category == "consignment_payout"

    post_pending_rows(conn)
    entry = conn.execute(select(journal_entries.c.source_type)).scalar_one()
    assert entry == "consignment_payout"
    link = conn.execute(select(invoice_journal_links.c.invoice_id)).scalar_one()
    assert link == invoice_id


# ---------------------------------------------------------------------------
# rule (c) — internal transfer.
#
# FIX (2026-09-01): CLAUDE.md's Bridging Account correction establishes that
# the amount that LANDS in Bridging (from a Payoneer withdrawal) and the
# amount that later SWEEPS OUT to BCA Main are two genuinely separate events,
# for two genuinely different numbers — never the same net_idr_landed figure
# (the business keeps a buffer). The tests below replace the old single
# "paired-transfer" test (which incorrectly assumed landing amount == sweep
# amount, and so accidentally tested the wrong mechanism) with two, one per
# genuinely distinct sub-case: c-landing (reconciles, posts nothing new) and
# c-sweep (a real, separately-amounted transfer, paired directly against its
# own counterpart line, never against net_idr_landed).
# ---------------------------------------------------------------------------


def test_rule_c_landing_echo_reconciles_without_posting_a_second_transfer(iprototype):
    """The exact real bug this fix addresses: the Bridging Account's own
    bank-statement line showing a Payoneer withdrawal LANDING is just an
    echo of an event ALREADY posted (post_realized_fx_withdrawal, called
    when the Payoneer CSV + confirmation were processed) — it must reconcile
    for traceability, but never post a second, phantom transfer.
    """
    conn, topo = iprototype
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 4, 25), rate_idr=Decimal("17968.32"))
    withdrawal_entry_id = posting.post_realized_fx_withdrawal(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 4, 28),
        gross_usd=Decimal("5000.00"),
        payoneer_fee_usd=Decimal("200.00"),
        exchange_rate_excl_fee=Decimal("17968.32"),
        booking_rate_used_idr=Decimal("17968.32"),
    )
    assert withdrawal_entry_id is not None
    entries_before = conn.execute(select(journal_entries.c.id)).scalars().all()
    assert len(entries_before) == 1  # just the withdrawal itself so far

    bridging_src = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=bridging_src,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 4, 28),
                raw_description="Transfer dari PT. BANK DBS INDONESIA / Payoneer HK",
                amount_idr=Decimal("86247936.00"),  # == net_idr_landed, the landing echo
                occurrence_index=1,
            )
        ],
    )

    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(
        select(review_queue.c.category, review_queue.c.match_rule, review_queue.c.linked_payoneer_withdrawal_id)
    ).one()
    assert row.category == "internal_transfer_landing"
    assert row.match_rule == "c-landing"
    assert row.linked_payoneer_withdrawal_id is not None

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    posted_entry_id = conn.execute(select(review_queue.c.posted_journal_entry_id)).scalar_one()
    assert posted_entry_id == withdrawal_entry_id  # links back to the EXISTING entry, nothing new

    # The real fix under test: still exactly ONE journal entry in the whole
    # ledger — no phantom second inter_account_transfer ever posted.
    all_entries = conn.execute(select(journal_entries.c.id, journal_entries.c.source_type)).all()
    assert len(all_entries) == 1
    assert all_entries[0].source_type == "payoneer_withdrawal"
    transfer_entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inter_account_transfer")
    ).all()
    assert transfer_entries == []

    # A second, DIFFERENT Bridging inflow line claiming the SAME withdrawal
    # must not also reconcile against it (already-consumed guard).
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=bridging_src,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 4, 28),
                raw_description="Duplicate-looking landing line",
                amount_idr=Decimal("86247936.00"),
                occurrence_index=2,
            )
        ],
    )
    run_auto_match(conn)
    statuses = conn.execute(select(review_queue.c.match_status)).all()
    assert any(s.match_status == "needs_review" for s in statuses)


def test_rule_c_sweep_pairs_directly_and_never_double_posts(iprototype):
    """The real, later, genuinely separate Bridging -> Main sweep — a
    rounded amount that does NOT equal net_idr_landed (confirmed against
    real Mandiri data, see ingestion/mandiri_statement.py's module
    docstring) — paired directly against its Master-statement counterpart,
    never against payoneer_withdrawals at all.
    """
    conn, topo = iprototype
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 4, 25), rate_idr=Decimal("17968.32"))
    posting.post_realized_fx_withdrawal(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 4, 28),
        gross_usd=Decimal("5000.00"),
        payoneer_fee_usd=Decimal("200.00"),
        exchange_rate_excl_fee=Decimal("17968.32"),
        booking_rate_used_idr=Decimal("17968.32"),  # net_idr_landed == 86,247,936.00
    )

    bridging_src = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    master_src = _make_bank_source(conn)

    # The real, separate sweep — a rounder, DIFFERENT number than
    # net_idr_landed (86,247,936.00), same as every real sweep in the
    # Mandiri sample data.
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=bridging_src,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 4, 29),
                raw_description="Transfer BI Fast / Ke BCA / DENNY WIJAYA 1790345891",
                amount_idr=Decimal("-86250000.00"),
                occurrence_index=1,
            )
        ],
    )
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=master_src,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 4, 30),
                raw_description="BI-FAST CR BIF TRANSFER DR / 008 / RICO",
                amount_idr=Decimal("86250000.00"),
                occurrence_index=1,
            )
        ],
    )

    match_result = run_auto_match(conn)
    assert match_result.matched == 2
    rows = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).all()
    assert all(r.category == "internal_transfer" and r.match_rule == "c-sweep" for r in rows)

    post_result = post_pending_rows(conn)
    assert post_result.posted == 2
    assert post_result.skipped_pending_pair == 0

    posted_entry_ids = conn.execute(
        select(review_queue.c.posted_journal_entry_id).where(review_queue.c.match_rule == "c-sweep")
    ).scalars().all()
    assert len(posted_entry_ids) == 2
    assert posted_entry_ids[0] == posted_entry_ids[1]  # SAME journal entry, not two

    transfer_entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inter_account_transfer")
    ).all()
    assert len(transfer_entries) == 1  # exactly one sweep transfer posted, never two

    lines = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(
            journal_lines.c.journal_entry_id == posted_entry_ids[0]
        )
    ).all()
    assert sum(l.debit_amount_idr for l in lines) == sum(l.credit_amount_idr for l in lines) == Decimal("86250000.00")

    # Debits still equal credits across EVERYTHING posted in this test
    # (the withdrawal entry too).
    all_lines = conn.execute(select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)).all()
    assert sum(l.debit_amount_idr for l in all_lines) == sum(l.credit_amount_idr for l in all_lines)


def test_rule_c_sweep_ambiguous_candidate_never_double_posts(iprototype):
    """Adversarial self-review finding (2026-09-01): TWO Bridging outflow
    lines that both plausibly match the SAME single Master inflow line
    (same amount, same-day) must never each post their own transfer for
    it — only ONE journal entry may ever exist, and the row that loses the
    race must land safely in 'pending its pair' (never a phantom post, and
    never a crash), regardless of which row post_pending_rows happens to
    process first. Real-data precedent for this shape of input: a
    same-amount same-day double transaction is not implausible in a real
    bank statement (e.g. two failed/retried transfer attempts).
    """
    conn, topo = iprototype
    bridging_src = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    master_src = _make_bank_source(conn)

    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=bridging_src,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 3),
                raw_description="Transfer BI Fast / Ke BCA / DENNY WIJAYA 1790345891 (attempt 1)",
                amount_idr=Decimal("-12000000.00"),
                occurrence_index=1,
            ),
            RawLine(
                transaction_date=_dt.date(2026, 5, 3),
                raw_description="Transfer BI Fast / Ke BCA / DENNY WIJAYA 1790345891 (attempt 2)",
                amount_idr=Decimal("-12000000.00"),
                occurrence_index=2,
            ),
        ],
    )
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=master_src,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 4),
                raw_description="BI-FAST CR BIF TRANSFER DR / 008 / RICO",
                amount_idr=Decimal("12000000.00"),
                occurrence_index=1,
            )
        ],
    )

    match_result = run_auto_match(conn)
    assert match_result.matched == 3  # all three classify — evidence exists for each independently
    post_result = post_pending_rows(conn)

    # Exactly one of the two ambiguous Bridging rows posts (paired with the
    # single real Master inflow); the other is left safely pending.
    assert post_result.posted == 2  # the winning pair
    assert post_result.skipped_pending_pair == 1  # the losing side

    transfer_entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inter_account_transfer")
    ).all()
    assert len(transfer_entries) == 1  # the real proof: never two, no matter the ambiguity

    rows = conn.execute(
        select(review_queue.c.raw_description, review_queue.c.posted_at, review_queue.c.paired_review_queue_id)
    ).all()
    posted_count = sum(1 for r in rows if r.posted_at is not None)
    pending_count = sum(1 for r in rows if r.posted_at is None)
    assert posted_count == 2
    assert pending_count == 1

    # Debits still equal credits across everything actually posted.
    all_lines = conn.execute(select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)).all()
    assert sum(l.debit_amount_idr for l in all_lines) == sum(l.credit_amount_idr for l in all_lines)

    # Idempotent: a second sync run doesn't magically resolve the ambiguity
    # into a phantom second post, and doesn't crash either.
    run_auto_match(conn)
    post_result_2 = post_pending_rows(conn)
    assert post_result_2.posted == 0
    transfer_entries_2 = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inter_account_transfer")
    ).all()
    assert len(transfer_entries_2) == 1


def test_rule_c_sweep_manual_label_before_pair_exists_does_not_crash(iprototype):
    """Requirement 3 of the 2026-09-01 fix: a human manually labels a
    Bridging Account row 'internal_transfer' before its Master-side pair has
    ever been ingested. Must not crash — leaves it pending, and posts once
    the pair does show up in a later sync.
    """
    conn, topo = iprototype
    bridging_src = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=bridging_src,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 3),
                raw_description="Transfer BI Fast / Ke BCA / DENNY WIJAYA 1790345891",
                amount_idr=Decimal("-65950000.00"),
                occurrence_index=1,
            )
        ],
    )
    # A human labels it directly — no counterpart exists anywhere yet.
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .where(review_queue.c.id == row_id)
        .values(category="internal_transfer", labeled_at=_dt.datetime.now(_dt.timezone.utc))
    )

    post_result = post_pending_rows(conn)  # must not raise
    assert post_result.posted == 0
    assert post_result.skipped_pending_pair == 1
    still_unposted = conn.execute(select(review_queue.c.posted_at).where(review_queue.c.id == row_id)).scalar_one()
    assert still_unposted is None

    # A later sync run ingests the real Master-side counterpart.
    master_src = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=master_src,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 4),
                raw_description="BI-FAST CR BIF TRANSFER DR / 008 / RICO",
                amount_idr=Decimal("65950000.00"),
                occurrence_index=1,
            )
        ],
    )
    match_result = run_auto_match(conn)  # finds the still-pending human-labeled orphan as its pair
    assert match_result.matched == 1
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 2  # both rows post now, same journal entry
    assert post_result2.skipped_pending_pair == 0

    entry_ids = conn.execute(select(review_queue.c.posted_journal_entry_id)).scalars().all()
    assert len(entry_ids) == 2
    assert entry_ids[0] == entry_ids[1]

    transfer_entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inter_account_transfer")
    ).all()
    assert len(transfer_entries) == 1


def test_posted_rows_never_repost_on_a_second_sync_run(iprototype):
    conn, topo = iprototype
    bridging_src = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    master_src = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=bridging_src,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 4, 29),
                raw_description="Transfer out to BCA Main",
                amount_idr=Decimal("-86250000.00"),
                occurrence_index=1,
            )
        ],
    )
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=master_src,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 4, 30),
                raw_description="BI-FAST CR BIF TRANSFER DR / 008 / RICO",
                amount_idr=Decimal("86250000.00"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    r1 = post_pending_rows(conn)
    r2 = post_pending_rows(conn)  # simulate a second sync run finding nothing new
    assert r1.posted == 2
    assert r2.posted == 0
    assert r2.skipped_unclassified == 0  # the rows ARE classified — just already posted, so not even considered
    assert r2.skipped_pending_pair == 0


# ---------------------------------------------------------------------------
# rule (d) — consignment reimbursement
# ---------------------------------------------------------------------------


def test_rule_d_matches_and_marks_consignment_sale_reimbursed(iprototype):
    conn, topo = iprototype
    cs_id = posting.create_consignment_sale(
        conn,
        item_price_usd=Decimal("200.00"),
        payout_model="tier",
        payout_amount_idr=Decimal("2686960"),
        consignor_item_ref="CONSIGN-X:order-1",
        tier_rate_percent=Decimal("82.00"),
        confirmed=True,
    )
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 5, 1), rate_idr=Decimal("16400"))
    posting.post_consignment_sale(
        conn,
        consignment_sale_id=cs_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=_dt.date(2026, 5, 5),
        gross_sale_price_usd=Decimal("220.00"),
        ebay_fee_usd=Decimal("20.00"),
        kurs_pajak_rate=Decimal("16400"),
    )

    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 10),
                raw_description="TRSF E-BANKING DB to consignor",
                amount_idr=Decimal("-2686960"),
                occurrence_index=1,
            )
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule, review_queue.c.consignor_item_ref)).one()
    assert row.category == "consignment_payout"
    assert row.match_rule == "d"
    assert row.consignor_item_ref == "CONSIGN-X:order-1"

    post_pending_rows(conn)
    reimbursed = conn.execute(select(consignment_sales.c.reimbursed_journal_entry_id)).scalar_one()
    assert reimbursed is not None

    # A second, identical-amount payout to the SAME consignor_item_ref must
    # NOT match rule (d) again — the liability is already cleared.
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 11),
                raw_description="TRSF E-BANKING DB to consignor again",
                amount_idr=Decimal("-2686960"),
                occurrence_index=2,
            )
        ],
    )
    run_auto_match(conn)
    statuses = conn.execute(select(review_queue.c.match_status)).all()
    assert any(s.match_status == "needs_review" for s in statuses)


def test_rule_d_consignment_reimbursement_paid_from_payoneer_carries_usd_reference(iprototype):
    """QA BUG FIX (2026-09): a consignment reimbursement paid directly out
    of a Payoneer Wallet (not the usual BCA Main/Bridging bank line) used
    to post with NO amount_usd_ref on the Payoneer Wallet line, even
    though the staged payoneer_csv row had a real one. See
    tests/scheduling/test_fx_revaluation.py for why this matters (it
    silently corrupted compute_payoneer_wallet_balance's USD sum).
    """
    conn, topo = iprototype
    cs_id = posting.create_consignment_sale(
        conn,
        item_price_usd=Decimal("200.00"),
        payout_model="tier",
        payout_amount_idr=Decimal("2686960"),
        consignor_item_ref="CONSIGN-Y:order-2",
        tier_rate_percent=Decimal("82.00"),
        confirmed=True,
    )
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 5, 1), rate_idr=Decimal("16400"))
    posting.post_consignment_sale(
        conn,
        consignment_sale_id=cs_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=_dt.date(2026, 5, 5),
        gross_sale_price_usd=Decimal("220.00"),
        ebay_fee_usd=Decimal("20.00"),
        kurs_pajak_rate=Decimal("16400"),
    )

    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 5, 1), wallet_group_id=topo["wallet_group_id"]
    )
    stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 10),
                raw_description="Payment sent to consignor",
                amount_idr=Decimal("-2686960.00"),
                amount_usd_ref=Decimal("-163.84"),
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "consignment_payout"
    assert row.match_rule == "d"

    post_pending_rows(conn)
    payoneer_wallet_id = get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=topo["wallet_group_id"])
    payoneer_line = conn.execute(
        select(journal_lines.c.credit_amount_idr, journal_lines.c.amount_usd_ref).where(
            journal_lines.c.account_id == payoneer_wallet_id
        )
    ).one()
    assert payoneer_line.credit_amount_idr == Decimal("2686960.00")
    # amount_usd_ref is always a positive USD MAGNITUDE (QA bug fix #2,
    # 2026-09) — direction is encoded structurally by debit vs credit, not
    # by the sign of this reference field, matching every other posting
    # function's convention (post_ebay_sale, post_realized_fx_withdrawal,
    # post_unrealized_fx_revaluation). The staged RawLine's amount_usd_ref
    # was -163.84 (an outflow); this must land as +163.84 here.
    assert payoneer_line.amount_usd_ref == Decimal("163.84")


# ---------------------------------------------------------------------------
# rule (e) — keyword rules
# ---------------------------------------------------------------------------


def test_rule_e_keyword_match_posts_to_named_expense_account(iprototype):
    conn, topo = iprototype
    conn.execute(
        bank_keyword_rules.insert().values(
            keyword="BIAYA ADM", category="operating_expense", expense_account_type_code="GENERAL_OPEX"
        )
    )
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 17), raw_description="BIAYA ADM", amount_idr=Decimal("-10000"), occurrence_index=1)
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "operating_expense"
    assert row.match_rule == "e"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    entry = conn.execute(select(journal_entries.c.source_type)).scalar_one()
    assert entry == "bank_other"


# ---------------------------------------------------------------------------
# bank_keyword_rules seeding fix (2026-09-02) — the real 5 confirmed
# mappings (ingestion/seed.py's seed_bank_keyword_rules, wired into every
# ingestion test's iprototype/iconn fixture the same way seed_catalogs
# already is) actually auto-match and post correctly. See CLAUDE.md's Chart
# of accounts note on INTEREST_INCOME being booked net of PAJAK BUNGA.
# ---------------------------------------------------------------------------


def test_seeded_bi_fast_transfer_fee_keyword_matches_and_posts(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 3), raw_description="Biaya transfer BI Fast", amount_idr=Decimal("-2500"), occurrence_index=1)
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "operating_expense"
    assert row.match_rule == "e"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    line = conn.execute(select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == general_opex_id)).scalar_one()
    assert line == Decimal("2500.00")


def test_seeded_admin_fee_keyword_matches_and_posts_general_opex(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 31), raw_description="Biaya administrasi rekening", amount_idr=Decimal("-6000"), occurrence_index=1),
            # A textually different real fee ("kartu debit", not "rekening")
            # in the SAME statement — must NOT accidentally match, a
            # genuinely different fee type.
            RawLine(transaction_date=_dt.date(2026, 5, 14), raw_description="Biaya administrasi kartu debit", amount_idr=Decimal("-6000"), occurrence_index=1),
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    assert match_result.needs_review == 1
    rows = conn.execute(select(review_queue.c.raw_description, review_queue.c.category, review_queue.c.match_rule)).all()
    rekening_row = next(r for r in rows if r.raw_description == "Biaya administrasi rekening")
    kartu_debit_row = next(r for r in rows if r.raw_description == "Biaya administrasi kartu debit")
    assert rekening_row.category == "operating_expense"
    assert rekening_row.match_rule == "e"
    assert kartu_debit_row.category is None  # correctly left for Needs Review

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1


def test_seeded_bunga_credit_posts_to_interest_income(iprototype):
    """BUNGA (bank-credited interest, an inflow) must debit BCA_MAIN and
    credit INTEREST_INCOME — NOT the reversed direction 'operating_expense'
    handling would produce if it were forced through that generic path.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 5, 31), raw_description="BUNGA", amount_idr=Decimal("1186.92"), occurrence_index=1)],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "interest_income"
    assert row.match_rule == "e"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    interest_income_id = get_account_id(conn, "INTEREST_INCOME")
    bca_main_id = get_account_id(conn, "BCA_MAIN")
    interest_line = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(journal_lines.c.account_id == interest_income_id)
    ).one()
    assert interest_line.debit_amount_idr == Decimal("0.00")
    assert interest_line.credit_amount_idr == Decimal("1186.92")
    bca_main_line = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == bca_main_id)
    ).scalar_one()
    assert bca_main_line == Decimal("1186.92")


def test_seeded_bunga_and_pajak_bunga_together_net_interest_income_correctly(iprototype):
    """The pair together (two separate real bank lines, two separate
    journal entries — per CLAUDE.md/ledger.posting.post_interest_income_line)
    must leave INTEREST_INCOME's own balance netted to the true amount
    actually received, without a separate tax-expense line anywhere.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 31), raw_description="BUNGA", amount_idr=Decimal("1186.92"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 31), raw_description="PAJAK BUNGA", amount_idr=Decimal("-237.38"), occurrence_index=1),
        ],
    )
    run_auto_match(conn)
    rows = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).all()
    assert all(r.category == "interest_income" and r.match_rule == "e" for r in rows)

    post_result = post_pending_rows(conn)
    assert post_result.posted == 2  # two separate lines, two separate traceable postings
    entries = conn.execute(select(journal_entries.c.id)).scalars().all()
    assert len(entries) == 2  # never combined into one entry

    interest_income_id = get_account_id(conn, "INTEREST_INCOME")
    interest_lines = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(journal_lines.c.account_id == interest_income_id)
    ).all()
    net = sum(l.credit_amount_idr - l.debit_amount_idr for l in interest_lines)
    assert net == Decimal("949.54")  # 1186.92 - 237.38, no separate tax-expense line posted anywhere
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    assert conn.execute(select(journal_lines.c.id).where(journal_lines.c.account_id == general_opex_id)).all() == []


def test_seeded_openai_subscription_keyword_matches_on_payoneer_wallet(iprototype):
    """The recurring Payoneer card charge is staged with source_type
    'payoneer_csv' (see ingestion.payoneer's generic staging branch) — this
    proves the keyword rule fires there too (not just on bank-statement
    lines) and pays out of the Payoneer Wallet, not BCA_MAIN.
    """
    conn, topo = iprototype
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=_dt.date(2026, 8, 1), wallet_group_id=topo["wallet_group_id"]
    )
    stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 8, 28),
                raw_description="Card charge (OPENAI *CHATGPT SUBSCR)",
                amount_idr=Decimal("-334130.00"),
                amount_usd_ref=Decimal("-20.37"),
                external_ref="287852392",
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "operating_expense"
    assert row.match_rule == "e"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    payoneer_wallet_id = get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=topo["wallet_group_id"])
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    payoneer_line = conn.execute(
        select(journal_lines.c.credit_amount_idr, journal_lines.c.amount_usd_ref).where(
            journal_lines.c.account_id == payoneer_wallet_id
        )
    ).one()
    assert payoneer_line.credit_amount_idr == Decimal("334130.00")
    # QA BUG FIX (2026-09): this Payoneer Wallet line used to post with a
    # NULL amount_usd_ref even though the staged row had a real one — it
    # was simply never threaded through post_operating_expense. That
    # silently corrupted scheduling.fx_revaluation.compute_payoneer_wallet_
    # balance's USD-balance sum for this wallet-group going forward (see
    # tests/scheduling/test_fx_revaluation.py's dedicated regression test
    # for the concrete before/after balance numbers).
    #
    # QA BUG FIX #2 (2026-09): amount_usd_ref is always a positive USD
    # MAGNITUDE, never the raw signed value — direction is encoded
    # structurally by debit vs credit, matching every other posting
    # function's convention. The staged RawLine's amount_usd_ref was
    # -20.37 (an outflow); this must land as +20.37 here, not -20.37 (the
    # first version of this fix got this backwards and QA caught it).
    assert payoneer_line.amount_usd_ref == Decimal("20.37")
    opex_line = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.amount_usd_ref).where(
            journal_lines.c.account_id == general_opex_id
        )
    ).one()
    assert opex_line.debit_amount_idr == Decimal("334130.00")
    assert opex_line.amount_usd_ref == Decimal("20.37")


def test_seeded_kurasi_keyword_matches_and_posts_to_shipping_cost(iprototype):
    """Added 2026-09-09 (confirmed directly by the user): Kurasi is a real
    shipping vendor — every bank line whose raw description contains
    "KURASI" is a shipping cost, no exceptions. Must auto-match straight to
    the dedicated 'shipping_cost' review-queue category and post to the
    SHIPPING_COST account — NOT 'operating_expense'/GENERAL_OPEX, which is
    what a plain keyword-rule-with-no-dedicated-category would have
    produced (see ingestion/matching.py's _post_one_row shipping_cost
    branch and ingestion/seed.py's BANK_KEYWORD_RULES).
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 8),
                raw_description="TRSF E-BANKING DB 0805/FTFVA/WS95271 / 15810/KURASI / - / - / 02485376996",
                amount_idr=Decimal("-948000.00"),
                occurrence_index=1,
            )
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "shipping_cost"
    assert row.match_rule == "e"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    shipping_cost_id = get_account_id(conn, "SHIPPING_COST")
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    line = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == shipping_cost_id)
    ).scalar_one()
    assert line == Decimal("948000.00")
    # Confirms it did NOT fall through to the GENERAL_OPEX default path.
    assert conn.execute(select(journal_lines.c.id).where(journal_lines.c.account_id == general_opex_id)).all() == []


def test_kurasi_keyword_is_case_insensitive_substring_match(iprototype):
    """Rule (e) matching is case-insensitive substring containment (see
    _try_rule_e_keyword) — a lowercase, embedded "kurasi" must match too,
    not just the exact uppercase "KURASI" seeded keyword.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 9),
                raw_description="transfer to jasa kurasi pengiriman",
                amount_idr=Decimal("-512000.00"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "shipping_cost"
    assert row.match_rule == "e"


def test_other_seeded_keyword_rules_still_behave_unchanged_alongside_kurasi(iprototype):
    """Regression test: adding the KURASI rule must not disturb the other
    real seeded keyword rules (BI Fast, admin fee, BUNGA/PAJAK BUNGA,
    OpenAI) — each of several DIFFERENT real bank/Payoneer lines, staged
    together in one pass, must still resolve to its own correct category,
    not bleed into 'shipping_cost' or each other.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 3), raw_description="Biaya transfer BI Fast", amount_idr=Decimal("-2500"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 31), raw_description="Biaya administrasi rekening", amount_idr=Decimal("-6000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 20), raw_description="BUNGA", amount_idr=Decimal("1186.92"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 21), raw_description="PAJAK BUNGA", amount_idr=Decimal("-237.38"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 8), raw_description="TRSF DB / KURASI / shipping", amount_idr=Decimal("-948000.00"), occurrence_index=1),
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 5
    rows = conn.execute(select(review_queue.c.raw_description, review_queue.c.category, review_queue.c.match_rule)).all()
    by_desc = {r.raw_description: r for r in rows}
    assert by_desc["Biaya transfer BI Fast"].category == "operating_expense"
    assert by_desc["Biaya administrasi rekening"].category == "operating_expense"
    assert by_desc["BUNGA"].category == "interest_income"
    assert by_desc["PAJAK BUNGA"].category == "interest_income"
    assert by_desc["TRSF DB / KURASI / shipping"].category == "shipping_cost"
    for r in rows:
        assert r.match_rule == "e"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 5
    shipping_cost_id = get_account_id(conn, "SHIPPING_COST")
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    interest_income_id = get_account_id(conn, "INTEREST_INCOME")
    shipping_line = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == shipping_cost_id)
    ).scalar_one()
    assert shipping_line == Decimal("948000.00")
    opex_debits = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == general_opex_id)
    ).all()
    assert sorted(d.debit_amount_idr for d in opex_debits) == [Decimal("2500.00"), Decimal("6000.00")]
    assert conn.execute(select(journal_lines.c.id).where(journal_lines.c.account_id == interest_income_id)).all() != []


def test_amount_just_outside_tolerance_does_not_false_match(iprototype):
    """A near-miss (amount off by more than AMOUNT_TOLERANCE_IDR) must fall
    to Needs Review, not fuzzy-match — proves the threshold is a real gate,
    not decorative.
    """
    conn, topo = iprototype
    conn.execute(
        ebay_expected_payouts.insert().values(
            ebay_account_id=topo["ebay_account_id"],
            ebay_payout_id="PAYOUT-2",
            payout_date=_dt.date(2026, 5, 4),
            net_amount_usd=Decimal("100.00"),
        )
    )
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 5),
                raw_description="Payment from eBay",
                amount_idr=Decimal("1631000"),
                amount_usd_ref=Decimal("100.50"),  # 0.50 off — outside the 0.01 USD tolerance
                external_ref="txn-tol-1",
            )
        ],
    )
    result = run_auto_match(conn)
    assert result.matched == 0
    assert result.needs_review == 1


def test_never_posts_unlabeled_needs_review_row(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 20), raw_description="Unrecognized transfer", amount_idr=Decimal("-999999"), occurrence_index=1)
        ],
    )
    run_auto_match(conn)
    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_unclassified == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []

    # Human labels it — next sync run picks it up.
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(update(review_queue).values(category="operating_expense", labeled_at=_dt.datetime.now(_dt.timezone.utc)).where(review_queue.c.id == row_id))
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 1


def test_post_pending_rows_flips_match_status_to_matched_on_success(iprototype):
    """Regression test for the 2026-09-14 bug fix: post_pending_rows was
    setting posted_at/posted_journal_entry_id on a successful post but never
    flipping match_status from 'needs_review' to 'matched', so a row that
    had genuinely posted kept showing the amber 'Needs Review' badge in the
    UI forever (see CLAUDE.md's "New low-priority gap found by QA
    2026-09-05" note). This drives a row through the exact human-labels-a-
    needs_review-row path (the normal case this bug affected) and asserts
    match_status is 'matched' afterward, not just posted_at being set.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 20),
                raw_description="Unrecognized transfer",
                amount_idr=Decimal("-500000"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)

    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    row_before = conn.execute(
        select(review_queue.c.match_status, review_queue.c.posted_at).where(review_queue.c.id == row_id)
    ).one()
    assert row_before.match_status == "needs_review"
    assert row_before.posted_at is None

    # Human labels it — next sync run picks it up (rule 4).
    conn.execute(
        update(review_queue)
        .values(category="operating_expense", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    row_after = conn.execute(
        select(
            review_queue.c.match_status,
            review_queue.c.posted_at,
            review_queue.c.posted_journal_entry_id,
        ).where(review_queue.c.id == row_id)
    ).one()
    assert row_after.match_status == "matched"
    assert row_after.posted_at is not None
    assert row_after.posted_journal_entry_id is not None


# ---------------------------------------------------------------------------
# 'contract_labor' — a human-selected review-queue category (2026-09-05,
# added alongside the new CONTRACT_LABOR operating-expense account for the
# outside IT contractor paid per-listing to create eBay listings — see
# ledger/chart_of_accounts.py and CLAUDE.md). No real sample invoice/bank
# line exists yet for this cost (forward-looking capacity only, per the
# brief), so there's no keyword-rule/rule-(e) auto-match coverage here — only
# the manual-label -> post path a human actually uses, mirroring
# test_never_posts_unlabeled_needs_review_row's pattern above.
# ---------------------------------------------------------------------------


def test_manually_labeled_contract_labor_row_posts_to_its_own_account_not_general_opex(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 15),
                raw_description="Transfer to IT contractor - listing work",
                amount_idr=Decimal("-4500000"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.id, review_queue.c.category)).one()
    assert row.category is None  # no keyword rule for this yet — correctly Needs Review

    conn.execute(
        update(review_queue)
        .values(category="contract_labor", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row.id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry_id = conn.execute(
        select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row.id)
    ).scalar_one()
    lines = lines_by_code(conn, entry_id)

    contract_labor_id = get_account_id(conn, "CONTRACT_LABOR")
    assert contract_labor_id is not None  # the account instance genuinely exists (not just the catalog row)
    assert "CONTRACT_LABOR" in lines
    assert lines["CONTRACT_LABOR"][0].debit_amount_idr == Decimal("4500000")
    assert "GENERAL_OPEX" not in lines  # must NOT fall back to the generic default
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("4500000")
    assert_balanced(conn, entry_id)

    # Idempotent: a second sync run must not double-post the same row.
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 0


# ---------------------------------------------------------------------------
# 'packaging_supplies' — a human-selected review-queue category (2026-09-10,
# added alongside the new PACKAGING_SUPPLIES operating-expense account for
# real Shopee/Tokopedia (and possibly other vendor) purchases that are
# packaging supplies rather than inventory items — see
# ledger/chart_of_accounts.py and CLAUDE.md). Deliberately NO keyword
# auto-match rule (the user confirmed a Shopee/Tokopedia bank line could be
# EITHER an item purchase OR packaging supplies, with no way to tell from
# the raw description alone) — only the manual-label -> post path a human
# actually uses, mirroring
# test_manually_labeled_contract_labor_row_posts_to_its_own_account_not_
# general_opex's pattern above.
# ---------------------------------------------------------------------------


def test_manually_labeled_packaging_supplies_row_posts_to_its_own_account_not_general_opex(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 15),
                raw_description="Transfer to Tokopedia - bubble wrap and boxes",
                amount_idr=Decimal("-850000"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.id, review_queue.c.category)).one()
    assert row.category is None  # no keyword rule for this — correctly Needs Review

    conn.execute(
        update(review_queue)
        .values(category="packaging_supplies", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row.id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry_id = conn.execute(
        select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row.id)
    ).scalar_one()
    lines = lines_by_code(conn, entry_id)

    packaging_supplies_id = get_account_id(conn, "PACKAGING_SUPPLIES")
    assert packaging_supplies_id is not None  # the account instance genuinely exists (not just the catalog row)
    assert "PACKAGING_SUPPLIES" in lines
    assert lines["PACKAGING_SUPPLIES"][0].debit_amount_idr == Decimal("850000")
    assert "GENERAL_OPEX" not in lines  # must NOT fall back to the generic default
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("850000")
    assert_balanced(conn, entry_id)

    # Idempotent: a second sync run must not double-post the same row.
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 0


# ---------------------------------------------------------------------------
# 'other' — bidirectional by design (2026-09-05 fix). CLAUDE.md's Chart of
# accounts adds OTHER_INCOME as the inflow-side counterpart to GENERAL_OPEX.
# Found against a real historical bad entry: a real +Rp 50,000 inflow (the
# account owner moving his own money from a personal DANA e-wallet into the
# Bridging Account) was wrongly labeled 'cogs_purchase' and posted with its
# direction flipped (journal_entry_id=917 / review_queue.id=321) — and even
# a CORRECTLY-labeled 'other' inflow had nowhere to post but the
# outflow-shaped path (post_operating_expense), which would have silently
# flipped it the same way. No auto-match rule ever produces 'other' — like
# 'contract_labor' above, this is always a human-selected review-queue
# label, so these tests manually set the category (mirroring
# test_manually_labeled_contract_labor_row_posts_to_its_own_account_not_
# general_opex's pattern).
# ---------------------------------------------------------------------------


def test_manually_labeled_other_outflow_still_posts_to_general_opex(iprototype):
    """Regression: an 'other'-labeled OUTFLOW must behave EXACTLY as it did
    before this fix — no behavior change for the existing, working case.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 8, 24),
                raw_description="Transfer antar Mandiri / DARI ESPAY DEBIT INDONESI",
                amount_idr=Decimal("-30000"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.id, review_queue.c.category)).one()
    assert row.category is None  # no auto-match rule produces 'other' — correctly Needs Review

    conn.execute(
        update(review_queue).values(category="other", labeled_at=_dt.datetime.now(_dt.timezone.utc)).where(review_queue.c.id == row.id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry_id = conn.execute(select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row.id)).scalar_one()
    lines = lines_by_code(conn, entry_id)
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    assert general_opex_id is not None
    assert lines["GENERAL_OPEX"][0].debit_amount_idr == Decimal("30000")
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("30000")
    assert "OTHER_INCOME" not in lines
    assert_balanced(conn, entry_id)


def test_manually_labeled_other_inflow_posts_to_other_income_not_flipped(iprototype):
    """The fix's core case: a real +Rp 50,000 inflow labeled 'other' must
    post as an inflow to OTHER_INCOME, never silently flipped into an
    outflow against GENERAL_OPEX the way the old unconditional
    post_operating_expense call would have.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 8, 11),
                raw_description="Transfer BI Fast / Dari / RICO 6283874841900 / DANA",
                amount_idr=Decimal("50000"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.id, review_queue.c.category)).one()
    assert row.category is None  # correctly Needs Review — no rule confidently matches this

    conn.execute(
        update(review_queue).values(category="other", labeled_at=_dt.datetime.now(_dt.timezone.utc)).where(review_queue.c.id == row.id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    assert post_result.skipped_sign_mismatch == 0  # 'other' is bidirectional — never flagged as a mismatch

    entry_id = conn.execute(select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row.id)).scalar_one()
    lines = lines_by_code(conn, entry_id)
    other_income_id = get_account_id(conn, "OTHER_INCOME")
    assert other_income_id is not None
    assert lines["OTHER_INCOME"][0].credit_amount_idr == Decimal("50000")
    assert lines["OTHER_INCOME"][0].debit_amount_idr == Decimal("0")
    bridging_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    assert lines["BCA_BRIDGING"][0].debit_amount_idr == Decimal("50000")
    assert "GENERAL_OPEX" not in lines
    assert "COGS" not in lines  # the historical bug's wrong destination
    assert_balanced(conn, entry_id)

    # Idempotent: a second sync run must not double-post the same row.
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 0


def test_other_inflow_and_outflow_same_period_both_post_correctly(iprototype):
    """The real Aug 24 sibling pair (review_queue.id=329/330 in the real
    database): an inflow and an outflow, both labeled 'other', in the SAME
    statement — each must resolve to its own correct destination
    independently, not accidentally share one posting decision.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(transaction_date=_dt.date(2026, 8, 24), raw_description="inflow sibling", amount_idr=Decimal("30000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 8, 24), raw_description="outflow sibling", amount_idr=Decimal("-30000"), occurrence_index=2),
        ],
    )
    run_auto_match(conn)
    conn.execute(update(review_queue).values(category="other", labeled_at=_dt.datetime.now(_dt.timezone.utc)))
    post_result = post_pending_rows(conn)
    assert post_result.posted == 2

    other_income_id = get_account_id(conn, "OTHER_INCOME")
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    other_income_credit = conn.execute(
        select(journal_lines.c.credit_amount_idr).where(journal_lines.c.account_id == other_income_id)
    ).scalar_one()
    general_opex_debit = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == general_opex_id)
    ).scalar_one()
    assert other_income_credit == Decimal("30000")
    assert general_opex_debit == Decimal("30000")


# ---------------------------------------------------------------------------
# Requirement 4 of the 2026-09-01 Bridging Account fix: a Bridging Account
# line that ISN'T a landing echo or a sweep leg is just a normal bank line —
# rules (a)/(b)/(d)/(e) must still fire normally on a wallet_group-scoped
# row, not just fall through to Needs Review by omission. Every other test
# in this module that exercises rules (a)/(b)/(d)/(e) uses a Master-scoped
# (wallet_group_id=None) source; these three prove the SAME rules fire when
# wallet_group_id IS set, i.e. rule (c)'s redesign didn't accidentally wall
# off the Bridging Account from the rest of the priority chain.
# ---------------------------------------------------------------------------


def test_rule_b_invoice_match_fires_normally_on_a_bridging_scoped_line(iprototype):
    conn, topo = iprototype
    invoice_id = _make_invoice(conn, amount_idr=Decimal("1200000"), extracted_date=_dt.date(2026, 5, 8))
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 9),
                raw_description="Transfer BI Fast / some vendor",
                amount_idr=Decimal("-1200000"),
                occurrence_index=1,
            )
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule, review_queue.c.linked_invoice_id)).one()
    assert row.category == "cogs_purchase"
    assert row.match_rule == "b"
    assert row.linked_invoice_id == invoice_id

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    entry = conn.execute(select(journal_entries.c.source_type)).scalar_one()
    assert entry == "bank_other"  # post_operating_expense's paying-account resolves BCA_BRIDGING, not BCA_MAIN


def test_rule_d_consignment_reimbursement_fires_normally_on_a_bridging_scoped_line(iprototype):
    conn, topo = iprototype
    cs_id = posting.create_consignment_sale(
        conn,
        item_price_usd=Decimal("100.00"),
        payout_model="tier",
        payout_amount_idr=Decimal("1300000"),
        consignor_item_ref="CONSIGN-Y:order-9",
        tier_rate_percent=Decimal("78.00"),
        confirmed=True,
    )
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 5, 1), rate_idr=Decimal("16400"))
    posting.post_consignment_sale(
        conn,
        consignment_sale_id=cs_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=_dt.date(2026, 5, 5),
        gross_sale_price_usd=Decimal("110.00"),
        ebay_fee_usd=Decimal("10.00"),
        kurs_pajak_rate=Decimal("16400"),
    )
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 10),
                raw_description="Transfer BI Fast / to consignor",
                amount_idr=Decimal("-1300000"),
                occurrence_index=1,
            )
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "consignment_payout"
    assert row.match_rule == "d"

    post_pending_rows(conn)
    reimbursed = conn.execute(select(consignment_sales.c.reimbursed_journal_entry_id)).scalar_one()
    assert reimbursed is not None


def test_rule_e_keyword_fires_normally_on_a_bridging_scoped_line(iprototype):
    conn, topo = iprototype
    conn.execute(
        bank_keyword_rules.insert().values(
            keyword="BIAYA ADMINISTRASI", category="operating_expense", expense_account_type_code="GENERAL_OPEX"
        )
    )
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 5, 31),
                raw_description="Biaya administrasi rekening",
                amount_idr=Decimal("-6000"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).one()
    assert row.category == "operating_expense"
    assert row.match_rule == "e"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1


# ---------------------------------------------------------------------------
# Sign-vs-category directional guard (2026-09-05 Fix) — QA found that
# cogs_purchase/operating_expense/contract_labor (and, on audit, several
# other directional categories) applied abs(row.amount_idr) unconditionally
# when posting, with no check that the category's inherent real-world
# direction (an expense/purchase/payout/draw is always an outflow; a
# contribution/revenue settlement is always an inflow) actually agreed with
# the raw signed amount. A real +Rp 50,000 INFLOW manually labeled
# 'cogs_purchase' posted with its direction silently flipped to look like an
# outflow — journal_entry_id=917 / review_queue.id=321, the real historical
# case this fix was found from (see CLAUDE.md's Definition of done).
# ---------------------------------------------------------------------------


def test_regression_correctly_signed_cogs_purchase_still_posts_normally(iprototype):
    """(a) No regression: a genuine outflow labeled cogs_purchase posts
    exactly as before — same amount, same accounts, no flag.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 12), raw_description="Transfer to supplier", amount_idr=Decimal("-500000"), occurrence_index=1)
        ],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="cogs_purchase", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )

    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    assert post_result.skipped_sign_mismatch == 0

    row = conn.execute(
        select(review_queue.c.posted_at, review_queue.c.posted_journal_entry_id, review_queue.c.sign_mismatch_reason)
        .where(review_queue.c.id == row_id)
    ).one()
    assert row.posted_at is not None
    assert row.sign_mismatch_reason is None
    lines = lines_by_code(conn, row.posted_journal_entry_id)
    assert lines["COGS"][0].debit_amount_idr == Decimal("500000")
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("500000")
    assert_balanced(conn, row.posted_journal_entry_id)


def test_sign_mismatch_inflow_labeled_cogs_purchase_never_posts(iprototype):
    """(b) The real journal_entry_id=917 / review_queue.id=321 case: a
    +Rp 50,000 inflow manually labeled 'cogs_purchase' (inherently an
    outflow) must NOT post — no journal entry created, no direction
    flipped — and must stay flagged, visible, in needs_review with a reason.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 8, 12), raw_description="Setoran tunai", amount_idr=Decimal("50000"), occurrence_index=1)
        ],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="cogs_purchase", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )

    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_sign_mismatch == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []  # nothing posted, no flipped-direction entry

    row = conn.execute(
        select(review_queue.c.posted_at, review_queue.c.match_status, review_queue.c.category, review_queue.c.sign_mismatch_reason)
        .where(review_queue.c.id == row_id)
    ).one()
    assert row.posted_at is None
    assert row.match_status == "needs_review"
    assert row.category == "cogs_purchase"  # left as-is for the human to see/fix, not silently cleared
    assert row.sign_mismatch_reason is not None
    assert "cogs_purchase" in row.sign_mismatch_reason

    # A human re-checks and re-classifies it as its actual real direction
    # (a genuine inflow — 'owners_contribution') — the next sync then posts
    # it correctly, and the stale mismatch flag is cleared.
    conn.execute(
        update(review_queue)
        .values(category="owners_contribution")
        .where(review_queue.c.id == row_id)
    )
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 1
    row2 = conn.execute(
        select(review_queue.c.posted_at, review_queue.c.sign_mismatch_reason).where(review_queue.c.id == row_id)
    ).one()
    assert row2.posted_at is not None
    assert row2.sign_mismatch_reason is None


def test_sign_mismatch_outflow_labeled_owners_contribution_never_posts(iprototype):
    """Same guard, opposite direction: an outflow can never be a genuine
    owner's contribution (money coming IN from the owner).
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 8, 15), raw_description="Ambiguous outflow", amount_idr=Decimal("-750000"), occurrence_index=1)
        ],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="owners_contribution", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )

    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_sign_mismatch == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []


def test_sign_mismatch_auto_matched_row_reverts_to_needs_review(iprototype):
    """An auto-matched row (rule e, which is direction-blind) whose category
    disagrees with the line's actual sign must not silently post — and must
    be pulled OUT of 'matched' back into 'needs_review' so a human actually
    sees it, not left showing as a false 'Matched' with a phantom problem.
    """
    conn, topo = iprototype
    conn.execute(
        bank_keyword_rules.insert().values(
            keyword="BIAYA ADM", category="operating_expense", expense_account_type_code="GENERAL_OPEX"
        )
    )
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            # A genuine inflow whose description happens to contain the
            # 'BIAYA ADM' keyword (e.g. a reversal/refund of a prior fee) —
            # rule (e) is a pure substring match with no sign awareness, so
            # it fires and marks this 'matched'/'operating_expense'.
            RawLine(transaction_date=_dt.date(2026, 8, 20), raw_description="BIAYA ADM reversal credit", amount_idr=Decimal("10000"), occurrence_index=1)
        ],
    )
    run_auto_match(conn)
    row = conn.execute(select(review_queue.c.match_status, review_queue.c.category)).one()
    assert row.match_status == "matched"
    assert row.category == "operating_expense"

    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_sign_mismatch == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []

    row2 = conn.execute(
        select(review_queue.c.match_status, review_queue.c.sign_mismatch_reason, review_queue.c.posted_at)
    ).one()
    assert row2.match_status == "needs_review"  # pulled out of 'matched'
    assert row2.posted_at is None
    assert row2.sign_mismatch_reason is not None


def test_regression_correctly_signed_owners_draw_still_posts(iprototype):
    """(a) No regression, another directional category: a genuine outflow
    labeled owners_draw still posts exactly as before.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 3), raw_description="Owner withdrawal", amount_idr=Decimal("-2000000"), occurrence_index=1)
        ],
    )
    draw_row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="owners_draw", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == draw_row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    assert post_result.skipped_sign_mismatch == 0

    row = conn.execute(
        select(review_queue.c.posted_at, review_queue.c.sign_mismatch_reason).where(review_queue.c.id == draw_row_id)
    ).one()
    assert row.posted_at is not None
    assert row.sign_mismatch_reason is None


def test_sign_mismatch_consignment_payout_and_contract_labor_never_post(iprototype):
    """Regression coverage for the two other categories the brief explicitly
    named (consignment_payout, contract_labor) — both outflow-only, both
    must reject a mislabeled inflow the same way cogs_purchase does above.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 8, 4), raw_description="Inbound credit A", amount_idr=Decimal("120000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 8, 5), raw_description="Inbound credit B", amount_idr=Decimal("340000"), occurrence_index=1),
        ],
    )
    rows = conn.execute(select(review_queue.c.id, review_queue.c.raw_description).order_by(review_queue.c.id)).all()
    consignment_row_id = rows[0].id
    contract_labor_row_id = rows[1].id
    conn.execute(
        update(review_queue)
        .values(category="consignment_payout", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == consignment_row_id)
    )
    conn.execute(
        update(review_queue)
        .values(category="contract_labor", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == contract_labor_row_id)
    )

    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_sign_mismatch == 2
    assert conn.execute(select(journal_entries.c.id)).all() == []


# ---------------------------------------------------------------------------
# Expanded COGS sub-categories (2026-09-10) — 'item_purchase' /
# 'inbound_shipping' / 'item_purchase_and_inbound_shipping' all post
# IDENTICALLY to the existing COGS account as plain 'cogs_purchase' — a
# labeling/traceability improvement only, never a new expense type.
# ---------------------------------------------------------------------------


def test_item_purchase_and_inbound_shipping_sub_categories_all_post_to_cogs(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 3), raw_description="Item purchase A", amount_idr=Decimal("-1000000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 4), raw_description="Freight-in B", amount_idr=Decimal("-200000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 5), raw_description="Item + shipping C", amount_idr=Decimal("-500000"), occurrence_index=1),
        ],
    )
    rows = conn.execute(select(review_queue.c.id).order_by(review_queue.c.id)).scalars().all()
    categories = ["item_purchase", "inbound_shipping", "item_purchase_and_inbound_shipping"]
    for row_id, category in zip(rows, categories):
        conn.execute(
            update(review_queue)
            .values(category=category, labeled_at=_dt.datetime.now(_dt.timezone.utc))
            .where(review_queue.c.id == row_id)
        )

    post_result = post_pending_rows(conn)
    assert post_result.posted == 3

    cogs_id = get_account_id(conn, "COGS")
    cogs_debits = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == cogs_id)
    ).scalars().all()
    assert sorted(cogs_debits) == [Decimal("200000"), Decimal("500000"), Decimal("1000000")]

    # None of these leaked into GENERAL_OPEX.
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    assert conn.execute(select(journal_lines.c.id).where(journal_lines.c.account_id == general_opex_id)).all() == []

    # Categories themselves are preserved on the (now-posted) rows — the
    # actual traceability improvement this feature exists for.
    posted_categories = conn.execute(select(review_queue.c.category).order_by(review_queue.c.id)).scalars().all()
    assert posted_categories == categories

    # Plain 'cogs_purchase' still works unchanged, side by side (same
    # source document — a consolidated bank_statement_master source is a
    # fixed once-per-period expectation, see ux_source_documents_consolidated).
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 5, 6), raw_description="Plain COGS", amount_idr=Decimal("-300000"), occurrence_index=1)],
    )
    plain_row_id = conn.execute(
        select(review_queue.c.id).where(review_queue.c.raw_description == "Plain COGS")
    ).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="cogs_purchase", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == plain_row_id)
    )
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 1
    cogs_debits2 = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == cogs_id)
    ).scalars().all()
    assert Decimal("300000") in cogs_debits2


# ---------------------------------------------------------------------------
# Payroll + optional embedded employee-loan repayment (2026-09-10). See
# CLAUDE.md's Core accounting rules and ledger.posting.
# post_payroll_with_loan_repayment. The real motivating case: Fariz
# Pradana's Rp 27,000,000 loan disbursed 2026-08-17, repaid Rp 1,500,000/
# month via a reduced payroll transfer.
# ---------------------------------------------------------------------------


def test_plain_payroll_row_posts_flat_expense_to_payroll_account(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 5, 25), raw_description="gaji / DENNY WIJAYA", amount_idr=Decimal("-10000000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="payroll", consignor_item_ref="Denny Wijaya", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry_id = conn.execute(select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row_id)).scalar_one()
    lines = lines_by_code(conn, entry_id)
    assert lines["PAYROLL"][0].debit_amount_idr == Decimal("10000000")
    assert "EMPLOYEE_LOAN_RECEIVABLE" not in lines
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("10000000")
    assert_balanced(conn, entry_id)


def test_payroll_row_with_loan_repayment_posts_three_line_entry(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 9, 25),
                raw_description="gaji / FARIZ PRADANA",
                amount_idr=Decimal("-8500000"),
                occurrence_index=1,
            )
        ],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(
            category="payroll",
            consignor_item_ref="Fariz Pradana",
            loan_repayment_amount_idr=Decimal("1500000"),
            labeled_at=_dt.datetime.now(_dt.timezone.utc),
        )
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry_id = conn.execute(select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row_id)).scalar_one()
    lines = lines_by_code(conn, entry_id)
    assert lines["PAYROLL"][0].debit_amount_idr == Decimal("10000000")  # gross, not the reduced net transfer
    assert lines["PAYROLL"][0].consignor_item_ref == "Fariz Pradana"
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].credit_amount_idr == Decimal("1500000")
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].consignor_item_ref == "Fariz Pradana"
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("8500000")  # the real amount transferred
    assert_balanced(conn, entry_id)

    # Idempotent: a second sync run must not double-post.
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 0


def test_payroll_sign_mismatch_still_caught(iprototype):
    """'payroll' is directional (always an outflow) — a wrongly-signed
    inflow must never silently post, same guard as every other directional
    category."""
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 5, 25), raw_description="gaji misclassified", amount_idr=Decimal("10000000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="payroll", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_sign_mismatch == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []


# ---------------------------------------------------------------------------
# Employee loan disbursement (2026-09-10) — the real Fariz Pradana loan.
# ---------------------------------------------------------------------------


def test_employee_loan_disbursement_posts_to_receivable_account(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 8, 17),
                raw_description="TRSF E-BANKING DB 1708/FTSCY/WS95271 / 27000000.00 / rab Angel / FARIZ PRADANA",
                amount_idr=Decimal("-27000000"),
                occurrence_index=1,
            )
        ],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(
            category="employee_loan_disbursement",
            consignor_item_ref="Fariz Pradana",
            labeled_at=_dt.datetime.now(_dt.timezone.utc),
        )
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry_id = conn.execute(select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row_id)).scalar_one()
    lines = lines_by_code(conn, entry_id)
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].debit_amount_idr == Decimal("27000000")
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].consignor_item_ref == "Fariz Pradana"
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("27000000")
    assert_balanced(conn, entry_id)

    # Idempotent: a second sync run must not double-post.
    post_result2 = post_pending_rows(conn)
    assert post_result2.posted == 0


def test_employee_loan_disbursement_sign_mismatch_still_caught(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 8, 17), raw_description="misclassified inflow", amount_idr=Decimal("27000000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="employee_loan_disbursement", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_sign_mismatch == 1


# ---------------------------------------------------------------------------
# QA-found gap fix (2026-09-10): a blank employee reference must NEVER
# silently post as a placeholder "unspecified" employee_ref — the sole
# reason a per-transaction reference is retained on an aggregate account at
# all (same reasoning CLAUDE.md already applies to Consignor Payable) is
# defeated if it can be blank. This is the defense-in-depth backstop for
# webapp/review_queue_bp.py::label_row's own equivalent validation — these
# tests bypass that route entirely (direct SQL, like every other matching
# test) to prove ingestion.matching itself refuses to post, independent of
# the UI layer.
# ---------------------------------------------------------------------------


def test_employee_loan_disbursement_with_blank_employee_ref_never_posts(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 8, 17), raw_description="no employee ref given", amount_idr=Decimal("-27000000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="employee_loan_disbursement", consignor_item_ref=None, labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_missing_employee_ref == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []  # nothing posted, definitely not to "unspecified"

    row = conn.execute(select(review_queue.c.match_status, review_queue.c.missing_reference_reason).where(review_queue.c.id == row_id)).first()
    assert row.match_status == "needs_review"
    assert row.missing_reference_reason is not None


def test_employee_loan_disbursement_with_blank_string_employee_ref_never_posts(iprototype):
    """A blank/whitespace-only string (not just SQL NULL) must be treated
    the same as no reference at all."""
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 8, 17), raw_description="blank string ref", amount_idr=Decimal("-27000000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="employee_loan_disbursement", consignor_item_ref="   ", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_missing_employee_ref == 1


def test_payroll_with_loan_repayment_and_blank_employee_ref_never_posts(iprototype):
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 9, 25), raw_description="gaji no ref", amount_idr=Decimal("-8500000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(
            category="payroll",
            consignor_item_ref=None,
            loan_repayment_amount_idr=Decimal("1500000"),
            labeled_at=_dt.datetime.now(_dt.timezone.utc),
        )
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 0
    assert post_result.skipped_missing_employee_ref == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []


def test_plain_payroll_with_no_loan_repayment_and_blank_ref_still_posts(iprototype):
    """A plain payroll line with NO embedded loan repayment never touches
    EMPLOYEE_LOAN_RECEIVABLE at all, so it has nothing to need a reference
    for — the missing-ref guard must not over-reach and block it."""
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 5, 25), raw_description="gaji / RICO", amount_idr=Decimal("-6000000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="payroll", consignor_item_ref=None, labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    assert post_result.skipped_missing_employee_ref == 0


def test_missing_employee_ref_flag_clears_once_reference_is_filled_in_and_posted(iprototype):
    """Once a human fills in the reference and the row posts successfully,
    the stale missing_reference_reason must not linger."""
    conn, topo = iprototype
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[RawLine(transaction_date=_dt.date(2026, 8, 17), raw_description="fix me", amount_idr=Decimal("-27000000"), occurrence_index=1)],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(category="employee_loan_disbursement", consignor_item_ref=None, labeled_at=_dt.datetime.now(_dt.timezone.utc))
        .where(review_queue.c.id == row_id)
    )
    first_pass = post_pending_rows(conn)
    assert first_pass.skipped_missing_employee_ref == 1

    conn.execute(
        update(review_queue).values(consignor_item_ref="Fariz Pradana").where(review_queue.c.id == row_id)
    )
    second_pass = post_pending_rows(conn)
    assert second_pass.posted == 1

    row = conn.execute(select(review_queue.c.missing_reference_reason).where(review_queue.c.id == row_id)).first()
    assert row.missing_reference_reason is None


def test_payroll_with_loan_repayment_carries_usd_reference_when_paid_from_payoneer(iprototype):
    """QA-found gap fix (2026-09-10): post_payroll_with_loan_repayment must
    thread amount_usd_ref/fx_rate_used through, same as every other posting
    function a Payoneer-wallet-sourced review-queue row can reach — see
    scheduling.fx_revaluation.compute_payoneer_wallet_balance's dependence
    on every real line touching a Payoneer Wallet having a USD reference.
    """
    conn, topo = iprototype
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 9, 25),
                raw_description="Payroll paid from Payoneer",
                amount_idr=Decimal("-8500000"),
                amount_usd_ref=Decimal("-535.32"),
                occurrence_index=1,
            )
        ],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .values(
            category="payroll",
            consignor_item_ref="Fariz Pradana",
            loan_repayment_amount_idr=Decimal("1500000"),
            labeled_at=_dt.datetime.now(_dt.timezone.utc),
        )
        .where(review_queue.c.id == row_id)
    )
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1

    entry_id = conn.execute(select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == row_id)).scalar_one()
    lines = get_lines(conn, entry_id)
    payoneer_wallet_id = get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=topo["wallet_group_id"])
    payoneer_line = next(l for l in lines if l.account_id == payoneer_wallet_id)
    # Always a positive USD magnitude, direction encoded structurally by
    # debit/credit side — same convention as every other posting function.
    assert payoneer_line.amount_usd_ref == Decimal("535.32")


# ---------------------------------------------------------------------------
# Keyword-matching word-boundary fix (2026-09-10) — "BIAYA ADM" must match
# the real, shorter BCA Main Account admin-fee line WITHOUT also matching
# the textually similar but genuinely different Bridging "Biaya
# administrasi rekening"/"Biaya administrasi kartu debit" lines. See
# ingestion/matching.py's _keyword_matches docstring.
# ---------------------------------------------------------------------------


def test_biaya_adm_keyword_matches_real_master_statement_forms(iprototype):
    conn, topo = iprototype
    conn.execute(
        bank_keyword_rules.insert().values(
            keyword="BIAYA ADM", category="operating_expense", expense_account_type_code="GENERAL_OPEX"
        )
    )
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 15), raw_description="BIAYA ADM", amount_idr=Decimal("-10000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 6, 1), raw_description="BIAYA ADM 0998", amount_idr=Decimal("-10000"), occurrence_index=1),
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 2
    rows = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).all()
    assert all(r.category == "operating_expense" and r.match_rule == "e" for r in rows)


def test_biaya_adm_keyword_does_not_match_bridging_administrasi_lines(iprototype):
    """The exact regression this fix must never introduce: the pre-existing,
    deliberate decision that 'Biaya administrasi kartu debit' stays Needs
    Review (see ingestion/seed.py's own note) must hold even with the new
    bare 'BIAYA ADM' keyword seeded alongside it.
    """
    conn, topo = iprototype
    conn.execute(
        bank_keyword_rules.insert().values(
            keyword="BIAYA ADM", category="operating_expense", expense_account_type_code="GENERAL_OPEX"
        )
    )
    conn.execute(
        bank_keyword_rules.insert().values(
            keyword="Biaya administrasi rekening", category="operating_expense", expense_account_type_code="GENERAL_OPEX"
        )
    )
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 31), raw_description="Biaya administrasi rekening", amount_idr=Decimal("-6000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 5, 14), raw_description="Biaya administrasi kartu debit", amount_idr=Decimal("-6000"), occurrence_index=1),
        ],
    )
    match_result = run_auto_match(conn)
    assert match_result.matched == 1
    assert match_result.needs_review == 1
    rows = conn.execute(select(review_queue.c.raw_description, review_queue.c.category)).all()
    rekening_row = next(r for r in rows if r.raw_description == "Biaya administrasi rekening")
    kartu_debit_row = next(r for r in rows if r.raw_description == "Biaya administrasi kartu debit")
    assert rekening_row.category == "operating_expense"
    assert kartu_debit_row.category is None  # still correctly left for Needs Review


def test_real_seeded_biaya_adm_keyword_matches_real_master_data(iprototype):
    """End-to-end against ingestion.seed's actual seeded BANK_KEYWORD_RULES
    (not a test-local ad hoc rule) — proves the real fix, not just the
    matching primitive."""
    from ingestion.seed import seed_bank_keyword_rules

    conn, topo = iprototype
    seed_bank_keyword_rules(conn)
    src_id = _make_bank_source(conn)
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        lines=[
            RawLine(transaction_date=_dt.date(2026, 5, 15), raw_description="BIAYA ADM", amount_idr=Decimal("-10000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 6, 1), raw_description="BIAYA ADM 0998", amount_idr=Decimal("-10000"), occurrence_index=1),
            RawLine(transaction_date=_dt.date(2026, 7, 1), raw_description="BIAYA ADM 0998", amount_idr=Decimal("-10000"), occurrence_index=1),
        ],
    )
    run_auto_match(conn)
    post_result = post_pending_rows(conn)
    assert post_result.posted == 3
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    debits = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(journal_lines.c.account_id == general_opex_id)
    ).scalars().all()
    assert debits == [Decimal("10000"), Decimal("10000"), Decimal("10000")]
