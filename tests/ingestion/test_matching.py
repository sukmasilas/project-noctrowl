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
from ledger.schema import consignment_sales, journal_entries, journal_lines
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
