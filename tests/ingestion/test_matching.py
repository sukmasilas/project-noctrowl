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
# rule (c) — internal transfer, including the paired-transfer single-post guard
# ---------------------------------------------------------------------------


def test_rule_c_paired_transfer_never_double_posts(iprototype):
    """A single real Bridging -> Main transfer shows up as an outflow line
    (Bridging statement) AND an inflow line (Master statement). Both should
    match rule (c) and both should end up posted_at-set pointing at the SAME
    journal_entry_id — but only ONE journal entry should ever exist.
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
                amount_idr=Decimal("-86247936.00"),
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
                raw_description="KR OTOMATIS Payoneer HK",
                amount_idr=Decimal("86247936.00"),
                occurrence_index=1,
            )
        ],
    )

    match_result = run_auto_match(conn)
    assert match_result.matched == 2
    rows = conn.execute(select(review_queue.c.category, review_queue.c.match_rule)).all()
    assert all(r.category == "internal_transfer" and r.match_rule == "c" for r in rows)

    post_result = post_pending_rows(conn)
    assert post_result.posted == 2

    posted_entry_ids = conn.execute(select(review_queue.c.posted_journal_entry_id)).scalars().all()
    assert len(posted_entry_ids) == 2
    assert posted_entry_ids[0] == posted_entry_ids[1]  # SAME journal entry, not two

    transfer_entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inter_account_transfer")
    ).all()
    assert len(transfer_entries) == 1  # exactly one transfer posted, never two

    # Debits still equal credits across everything posted in this test.
    lines = conn.execute(select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)).all()
    assert sum(l.debit_amount_idr for l in lines) == sum(l.credit_amount_idr for l in lines)


def test_posted_rows_never_repost_on_a_second_sync_run(iprototype):
    conn, topo = iprototype
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 4, 25), rate_idr=Decimal("17968.32"))
    posting.post_realized_fx_withdrawal(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 4, 28),
        gross_usd=Decimal("5000.00"),
        payoneer_fee_usd=Decimal("200.00"),
        exchange_rate_excl_fee=Decimal("17968.32"),
        booking_rate_used_idr=Decimal("17968.32"),
    )
    src_id = _make_bank_source(conn, wallet_group_id=topo["wallet_group_id"])
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=topo["wallet_group_id"],
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 4, 29),
                raw_description="Transfer out to BCA Main",
                amount_idr=Decimal("-86247936.00"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    r1 = post_pending_rows(conn)
    r2 = post_pending_rows(conn)  # simulate a second sync run finding nothing new
    assert r1.posted == 1
    assert r2.posted == 0
    assert r2.skipped_unclassified == 0  # the row IS classified — just already posted, so not even considered


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
