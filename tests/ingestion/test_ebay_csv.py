"""Tests for ingestion.ebay_csv — the real Seller Hub Transaction Report
sample plus synthetic fixtures for paths the real sample has zero coverage
of (CONSIGN- detection, per docs/design/milestone-3-ingestion-design.md §2).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from ingestion.ebay_csv import parse_ebay_csv_rows, process_transaction_report
from ingestion.schema import ebay_csv_transactions, ebay_expected_payouts
from ledger.schema import consignment_sales, journal_entries, journal_lines
from tests.ingestion.conftest import make_source_document

REAL_SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "sample-documents"
    / "eBay account 1_ricky-game"
    / "Transaction_report_20260701_20260731.csv"
)

# Fix 3 validation pass (2026-09-01): 3 more real consecutive months
# (May/Jun/Aug 2026) were collected alongside the original July sample — see
# test_all_real_monthly_samples_parse_and_post_cleanly below.
OTHER_MONTH_SAMPLES = [
    Path(__file__).resolve().parents[2] / "sample-documents" / "eBay account 1_ricky-game" / name
    for name in (
        "Transaction_report_20260501_20260531.csv",
        "Transaction_report_20260601_20260630.csv",
        "Transaction_report_20260801_20260831.csv",
    )
]


def _load_real_sample_rows():
    text = REAL_SAMPLE.read_text(encoding="utf-8-sig")
    return parse_ebay_csv_rows(text)


def test_parse_locates_header_and_returns_all_data_rows():
    header, rows = _load_real_sample_rows()
    assert header[0] == "Transaction creation date"
    assert header[1] == "Type"
    # 202 data rows confirmed against the real file (100 Order + 83 Other fee
    # + 11 Refund + 4 Hold + 4 Payout).
    assert len(rows) == 202
    types = {}
    for row in rows:
        types[row["Type"]] = types.get(row["Type"], 0) + 1
    assert types == {"Order": 100, "Other fee": 83, "Refund": 11, "Hold": 4, "Payout": 4}


def test_real_sample_end_to_end_ingest_no_crash_and_expected_counts(iprototype):
    conn, topo = iprototype
    _, rows = _load_real_sample_rows()
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )

    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=rows
    )

    # 100 'Order'-typed CSV rows collapse to 91 distinct postable orders: 4
    # of them are multi-line-item orders spanning multiple CSV rows each
    # (confirmed against the real file — see ingestion/ebay_csv.py's module
    # docstring "real-sample discovery" note), merging 13 rows into 4 posted
    # sales (87 single-row orders + 4 merged multi-row orders = 91).
    assert result.orders_posted == 91
    assert result.refunds_posted == 11
    assert result.other_fees_posted == 83
    assert result.holds_skipped == 4  # 2 pairs
    assert result.payouts_recorded == 4
    assert result.consignment_sales_created == 0
    assert result.parse_warnings == []

    # Debits always equal credits across everything just posted — the one
    # invariant the whole ledger depends on.
    total_debit = conn.execute(select(journal_lines.c.debit_amount_idr)).scalars().all()
    total_credit = conn.execute(select(journal_lines.c.credit_amount_idr)).scalars().all()
    assert sum(total_debit) == sum(total_credit)

    # Hold rows never created a journal entry of their own (only Order/
    # Refund/Other fee/Payout source_types are ever posted from this file;
    # Payout rows are staged as ebay_expected_payouts, never posted).
    source_types = set(conn.execute(select(journal_entries.c.source_type)).scalars().all())
    assert source_types <= {"ebay_sale", "ebay_refund", "bank_other"}

    payouts = conn.execute(select(ebay_expected_payouts.c.ebay_payout_id, ebay_expected_payouts.c.net_amount_usd)).all()
    assert len(payouts) == 4
    assert all(amt > 0 for _pid, amt in payouts)


def test_real_sample_stages_one_ebay_csv_transactions_row_per_raw_csv_row(iprototype):
    """Every one of the 202 raw CSV rows (see test_parse_locates_header_
    and_returns_all_data_rows) gets its own ebay_csv_transactions staging
    record — including Hold rows (never posted) and every raw row inside a
    merged multi-item Order group (which share one journal_entry_id) — so
    the Wallet screen can browse eBay Wallet activity at the same level of
    row-level detail as the other three wallets.
    """
    conn, topo = iprototype
    _, rows = _load_real_sample_rows()
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )

    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=rows)

    staged = conn.execute(
        select(ebay_csv_transactions).where(ebay_csv_transactions.c.source_document_id == src_id)
    ).all()
    assert len(staged) == 202

    # Every row_index 0..201 appears exactly once — no row silently dropped,
    # none double-staged.
    assert sorted(r.row_index for r in staged) == list(range(202))

    by_type: dict[str, list] = {}
    for r in staged:
        by_type.setdefault(r.row_type, []).append(r)
    assert {t: len(rs) for t, rs in by_type.items()} == {
        "Order": 100,
        "Other fee": 83,
        "Refund": 11,
        "Hold": 4,
        "Payout": 4,
    }

    # Order/Refund/Other fee rows that actually posted carry a
    # journal_entry_id; Hold and Payout rows never do (Holds are always
    # skipped; Payout rows only ever populate ebay_expected_payouts).
    assert all(r.journal_entry_id is not None for r in by_type["Refund"])
    assert all(r.journal_entry_id is not None for r in by_type["Other fee"])
    assert all(r.journal_entry_id is None for r in by_type["Hold"])
    assert all(r.journal_entry_id is None for r in by_type["Payout"])
    # 91 distinct posted orders across 100 raw Order rows (13 rows merge
    # into 4 multi-item orders) -> 91 distinct journal_entry_ids among the
    # 100 staged Order rows.
    order_entry_ids = {r.journal_entry_id for r in by_type["Order"]}
    assert len(order_entry_ids) == 91
    assert None not in order_entry_ids

    # Re-running the same document is a no-op for staging (idempotent on
    # (source_document_id, row_index), same as the rest of this pipeline's
    # idempotency guards) — never doubles the row count.
    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=rows)
    staged_again = conn.execute(
        select(ebay_csv_transactions).where(ebay_csv_transactions.c.source_document_id == src_id)
    ).all()
    assert len(staged_again) == 202


def test_staging_still_populates_when_rows_were_already_posted_on_a_prior_run(iprototype):
    """QA BUG FIX regression test, 2026-09-09: reproduces the exact real-
    world failure mode a freshly-created test schema can never exercise —
    a database that already has months of prior postings (real
    ebay_csv_posted_transactions / ebay_expected_payouts rows) from BEFORE
    ebay_csv_transactions existed, then gets re-synced. Every one of the
    four idempotency early-return paths (Order, Refund, Other fee, Payout)
    used to discard the id ``_already_posted_ebay_csv_row``/the payout
    existence check found and return WITHOUT staging — leaving
    ebay_csv_transactions silently empty for every already-processed month,
    even though the real posted data was completely correct. Confirmed by
    QA against the real ``noctrowl`` database: 0 of 198 already-posted rows
    got staged on a re-run before this fix.
    """
    conn, topo = iprototype
    _, rows = _load_real_sample_rows()
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )

    # First run: posts everything normally (this also happens to populate
    # ebay_csv_transactions, but we deliberately wipe it below to simulate
    # "this table didn't exist yet when these months were first processed"
    # — the real ebay_csv_posted_transactions / ebay_expected_payouts rows
    # are what's left over from that, exactly like the real database.
    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=rows)
    conn.execute(ebay_csv_transactions.delete())
    assert conn.execute(select(ebay_csv_transactions.c.id)).all() == []

    # Second run against the SAME already-posted data (nothing new posts —
    # every row hits an idempotency early-return path) must still stage
    # all 202 rows, correctly linked back to what already posted.
    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=rows
    )
    assert result.orders_posted == 0
    assert result.refunds_posted == 0
    assert result.other_fees_posted == 0
    assert result.payouts_recorded == 0

    staged = conn.execute(select(ebay_csv_transactions)).all()
    assert len(staged) == 202
    by_type: dict[str, list] = {}
    for r in staged:
        by_type.setdefault(r.row_type, []).append(r)
    assert {t: len(rs) for t, rs in by_type.items()} == {
        "Order": 100,
        "Other fee": 83,
        "Refund": 11,
        "Hold": 4,
        "Payout": 4,
    }
    assert all(r.journal_entry_id is not None for r in by_type["Refund"])
    assert all(r.journal_entry_id is not None for r in by_type["Other fee"])
    assert len({r.journal_entry_id for r in by_type["Order"]} - {None}) == 91
    assert all(r.journal_entry_id is None for r in by_type["Hold"])
    assert all(r.journal_entry_id is None for r in by_type["Payout"])


def test_reconciliation_mismatch_stages_existing_review_queue_id_on_rerun(iprototype):
    """Same fix shape, smaller-scope sibling bug QA flagged: when
    ``_make_review_queue_row`` returns None (a row for this external_ref
    already exists), the reconciliation-mismatch branch used to stage
    review_queue_id=None instead of looking up and using the real id.
    """
    conn, topo = iprototype
    header, _ = _load_real_sample_rows()
    row = {col: "--" for col in header}
    row.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "88-88888-88888",
            "Transaction ID": "TXN-MISMATCH-1",
            "Item title": "Mismatched Item",
            "Item subtotal": "50",
            "Shipping and handling": "5",
            "Gross transaction amount": "999",  # doesn't reconcile with subtotal+shipping
            "Net amount": "900",
            "Transaction currency": "USD",
        }
    )
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )

    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=[row])
    first_staged = conn.execute(select(ebay_csv_transactions)).one()
    assert first_staged.review_queue_id is not None
    real_rq_id = first_staged.review_queue_id

    # Wipe staging only (simulating the real-world "table didn't exist yet"
    # gap) and reprocess — the review_queue row from the first run still
    # exists (external_ref conflict), so _make_review_queue_row returns
    # None on this second pass; the fix must look up and use the real id.
    conn.execute(ebay_csv_transactions.delete())
    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=[row])
    second_staged = conn.execute(select(ebay_csv_transactions)).one()
    assert second_staged.review_queue_id == real_rq_id


def test_reprocessing_same_payout_rows_is_idempotent(iprototype):
    """Re-parsing the same CSV (e.g. a re-sync) must never create duplicate
    ebay_expected_payouts rows for the same (ebay_account_id, payout_id).
    """
    conn, topo = iprototype
    _, rows = _load_real_sample_rows()
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    payout_rows = [r for r in rows if r["Type"] == "Payout"]

    from ingestion.ebay_csv import _process_payout_row, EbayIngestResult

    r1 = EbayIngestResult()
    r2 = EbayIngestResult()
    for i, row in enumerate(payout_rows):
        entry_date = _dt.date(2026, 7, 28)  # arbitrary, not exercised in this narrow test
        _process_payout_row(
            conn, row=row, row_index=i, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, entry_date=entry_date, result=r1
        )
    for i, row in enumerate(payout_rows):
        entry_date = _dt.date(2026, 7, 28)
        _process_payout_row(
            conn, row=row, row_index=i, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, entry_date=entry_date, result=r2
        )

    assert r1.payouts_recorded == 4
    assert r2.payouts_recorded == 0  # second pass found all 4 already existing
    count = conn.execute(select(ebay_expected_payouts.c.id)).all()
    assert len(count) == 4


def test_hold_placed_and_released_pair_never_posts_and_never_double_counts(iprototype):
    conn, topo = iprototype
    header, rows = _load_real_sample_rows()
    hold_rows = [r for r in rows if r["Type"] == "Hold"]
    assert len(hold_rows) == 4
    # Confirm the real sample's hold rows are genuinely placed/released
    # pairs netting to zero, which is the fact the skip-logic relies on.
    net_amounts = [Decimal(r["Net amount"].replace(",", "")) for r in hold_rows]
    assert sum(net_amounts) == 0

    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=hold_rows
    )
    assert result.holds_skipped == 4
    assert conn.execute(select(journal_entries.c.id)).all() == []


def test_all_real_monthly_samples_parse_and_post_cleanly(iprototype):
    """Fix 3 validation pass (2026-09-01): re-run the existing eBay CSV
    parser/poster against the 3 other real consecutive months collected
    alongside the original July sample (May/Jun/Aug 2026) — previously only
    tested against a single real month. Also the regression test for the
    real bug this pass found: a real May order (22-14606-65529) has $0 in
    every itemized eBay fee column, which crashed post_ebay_sale's
    unconditional 3-line construction (fixed in ledger/posting.py — only
    posts the fee debit line when there actually is a fee).
    """
    conn, topo = iprototype
    from ingestion.kurs_pajak import seed_kurs_pajak_rate

    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 4, 20), rate_idr=Decimal("16200"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 5, 4), rate_idr=Decimal("16210"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 5, 11), rate_idr=Decimal("16220"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 5, 18), rate_idr=Decimal("16230"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 5, 25), rate_idr=Decimal("16240"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 6, 1), rate_idr=Decimal("16250"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 6, 8), rate_idr=Decimal("16260"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 6, 15), rate_idr=Decimal("16270"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 6, 22), rate_idr=Decimal("16280"))
    # 2026-06-29 through 2026-08-03 are already seeded by the iprototype
    # fixture (tests/ingestion/conftest.py) — not re-seeded here.
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 10), rate_idr=Decimal("16410"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 17), rate_idr=Decimal("16420"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 24), rate_idr=Decimal("16430"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 31), rate_idr=Decimal("16440"))

    for i, sample in enumerate(OTHER_MONTH_SAMPLES):
        text = sample.read_text(encoding="utf-8-sig")
        _header, rows = parse_ebay_csv_rows(text)
        src_id = make_source_document(
            conn,
            document_type="ebay_sales_csv",
            period_month=_dt.date(2026, 5 if i == 0 else (6 if i == 1 else 8), 1),
            ebay_account_id=topo["ebay_account_id"],
        )
        result = process_transaction_report(
            conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=rows
        )
        assert result.parse_warnings == [], f"{sample.name}: {result.parse_warnings}"
        assert result.orders_posted > 0

    total_debit = conn.execute(select(journal_lines.c.debit_amount_idr)).scalars().all()
    total_credit = conn.execute(select(journal_lines.c.credit_amount_idr)).scalars().all()
    assert sum(total_debit) == sum(total_credit)

    # Confirmed 2026-09: no CONSIGN- prefixed row appears in any of the 4
    # real monthly samples (May-Aug 2026) for this account — the CONSIGN-
    # detection path still only has real coverage via the synthetic fixture
    # below, not a real order. Not a bug (defaults to normal-sale treatment
    # either way, per CLAUDE.md) — flagged to Main-agent as still-open, not
    # silently assumed resolved just because more months arrived.
    consign_sales = conn.execute(select(consignment_sales.c.id)).all()
    assert consign_sales == []


def test_consign_prefixed_order_creates_unconfirmed_consignment_sale_not_posted(iprototype):
    """Synthetic fixture — the real sample has zero CONSIGN- rows (see
    sample-documents/README.md and design doc §2's explicitly-flagged
    coverage gap). Built from a real Order row's shape with Custom label set.
    """
    conn, topo = iprototype
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    header, _ = _load_real_sample_rows()
    row = {col: "--" for col in header}
    row.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "99-99999-99999",
            "Transaction ID": "TXN-CONSIGN-1",
            "Item title": "Seiko SNJ025 Prospex Diver",
            "Custom label": "CONSIGN-SELLER42-001",
            "Item subtotal": "200",
            "Shipping and handling": "20",
            "Gross transaction amount": "220",
            "Net amount": "180",
            "Final Value Fee - fixed": "-0.44",
            "Final Value Fee - variable": "-20",
            "Transaction currency": "USD",
        }
    )

    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=[row]
    )

    assert result.consignment_sales_created == 1
    assert result.orders_posted == 0
    # Never auto-posted — milestone 2's two-phase confirm-then-post gate.
    assert conn.execute(select(journal_entries.c.id)).all() == []

    cs = conn.execute(
        select(
            consignment_sales.c.item_price_usd,
            consignment_sales.c.payout_model,
            consignment_sales.c.tier_rate_percent,
            consignment_sales.c.confirmed_at,
            consignment_sales.c.consignor_item_ref,
        )
    ).one()
    assert cs.item_price_usd == Decimal("200.00")
    assert cs.payout_model == "tier"
    assert cs.tier_rate_percent == Decimal("82.00")  # $100-2499.99 tier
    assert cs.confirmed_at is None
    assert cs.consignor_item_ref == "CONSIGN-SELLER42-001:99-99999-99999"

    # The staged row links back to the unconfirmed consignment_sales row
    # (not a journal_entry_id, since nothing posted yet) — see
    # ingestion/schema.py's ebay_csv_transactions docstring.
    staged = conn.execute(select(ebay_csv_transactions)).one()
    assert staged.journal_entry_id is None
    assert staged.review_queue_id is None
    assert staged.consignment_sale_id is not None
    assert staged.row_type == "Order"
    assert staged.order_number == "99-99999-99999"


def test_consign_order_requiring_manual_tier_contact_is_not_staged(iprototype):
    """A $7,500+ item price never gets an auto-suggested payout — per
    CLAUDE.md, that tier requires manual contact and is never auto-applied.
    """
    conn, topo = iprototype
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    header, _ = _load_real_sample_rows()
    row = {col: "--" for col in header}
    row.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "99-99999-88888",
            "Transaction ID": "TXN-CONSIGN-2",
            "Item title": "Rolex Daytona",
            "Custom label": "CONSIGN-SELLER7-002",
            "Item subtotal": "8000",
            "Shipping and handling": "0",
            "Gross transaction amount": "8000",
            "Net amount": "7800",
            "Final Value Fee - fixed": "-0.44",
            "Final Value Fee - variable": "-199.56",
            "Transaction currency": "USD",
        }
    )

    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=[row]
    )

    assert result.consignment_sales_created == 0
    assert len(result.parse_warnings) == 1
    assert "manual tier contact" in result.parse_warnings[0]
    assert conn.execute(select(consignment_sales.c.id)).all() == []


def test_multi_item_order_group_merges_into_one_correctly_valued_sale(iprototype):
    """The real sample's Order 05-14932-33362 (4 CSV rows: 1 order-total row
    + 3 line-item rows) must post as ONE sale using the totals row's gross,
    with fees summed across the line-item rows (fees live there, not on the
    totals row — confirmed against the real file).
    """
    conn, topo = iprototype
    _, rows = _load_real_sample_rows()
    group = [r for r in rows if r.get("Order number") == "05-14932-33362" and r["Type"] == "Order"]
    assert len(group) == 4

    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=group
    )

    assert result.orders_posted == 1
    assert result.parse_warnings == []

    entry = conn.execute(select(journal_entries.c.id, journal_entries.c.source_type)).one()
    assert entry.source_type == "ebay_sale"
    lines = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr, journal_lines.c.amount_usd_ref)
        .where(journal_lines.c.journal_entry_id == entry.id)
    ).all()
    assert sum(l.debit_amount_idr for l in lines) == sum(l.credit_amount_idr for l in lines)
    # Gross 267 (from the totals row) at the seeded 2026-07-20 rate (16310).
    revenue_line = [l for l in lines if l.credit_amount_idr > 0]
    assert len(revenue_line) == 1
    assert revenue_line[0].credit_amount_idr == Decimal("267.00") * Decimal("16310")
    assert revenue_line[0].amount_usd_ref == Decimal("267.00")


def test_reconciliation_mismatch_routes_to_review_queue_not_posted(iprototype):
    conn, topo = iprototype
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    header, _ = _load_real_sample_rows()
    row = {col: "--" for col in header}
    row.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "99-99999-77777",
            "Transaction ID": "TXN-MISMATCH-1",
            "Item title": "Mismatched row",
            "Item subtotal": "100",
            "Shipping and handling": "10",
            "Gross transaction amount": "999",  # doesn't reconcile to 100+10
            "Net amount": "80",
            "Final Value Fee - fixed": "-0.44",
            "Final Value Fee - variable": "-19",
            "Transaction currency": "USD",
        }
    )
    from ingestion.schema import review_queue

    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=[row]
    )
    assert result.orders_posted == 0
    assert result.review_queue_rows_created == 1
    assert conn.execute(select(journal_entries.c.id)).all() == []
    rq = conn.execute(select(review_queue.c.match_status, review_queue.c.category)).one()
    assert rq.match_status == "needs_review"
    assert rq.category is None


def test_mixed_consign_and_normal_line_items_in_one_order_refuses_to_post(iprototype):
    """A multi-item order where one line item is CONSIGN--prefixed and
    another isn't has no supported posting model (see
    ingestion/ebay_csv.py's _merge_order_group) — must refuse to post
    either as a normal sale or as a consignment sale, and must emit a clear
    parse warning rather than silently picking one interpretation.
    """
    conn, topo = iprototype
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    header, _ = _load_real_sample_rows()

    totals_row = {col: "--" for col in header}
    totals_row.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "77-77777-11111",
            "Gross transaction amount": "300",
            "Net amount": "270",
            "Transaction currency": "USD",
        }
    )
    consign_line = {col: "--" for col in header}
    consign_line.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "77-77777-11111",
            "Transaction ID": "TXN-MIX-1",
            "Item title": "Seiko SNJ025 (consignment)",
            "Custom label": "CONSIGN-X-1",
            "Item subtotal": "200",
            "Shipping and handling": "10",
        }
    )
    normal_line = {col: "--" for col in header}
    normal_line.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "77-77777-11111",
            "Transaction ID": "TXN-MIX-2",
            "Item title": "Pokemon card (normal stock)",
            "Item subtotal": "90",
            "Shipping and handling": "0",
        }
    )

    result = process_transaction_report(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        source_document_id=src_id,
        rows=[totals_row, consign_line, normal_line],
    )

    assert result.orders_posted == 0
    assert result.consignment_sales_created == 0
    assert len(result.parse_warnings) == 1
    assert "mixes CONSIGN-" in result.parse_warnings[0]
    assert conn.execute(select(journal_entries.c.id)).all() == []
    assert conn.execute(select(consignment_sales.c.id)).all() == []


def test_nonzero_charity_donation_sale_still_posts_gross_and_flags_donation_separately(iprototype):
    """Per CLAUDE.md's Chart of accounts note (Other eBay Wallet debits): a
    nonzero Charity donation has no obviously-correct account, so it's
    routed to review rather than folded into fees/opex — but the underlying
    sale still posts normally at its full gross amount (the donation is a
    seller-elected deduction, not a reason to withhold revenue recognition).
    """
    conn, topo = iprototype
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    header, _ = _load_real_sample_rows()
    row = {col: "--" for col in header}
    row.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "44-44444-22222",
            "Transaction ID": "TXN-CHARITY-1",
            "Item title": "Item with a charity donation",
            "Item subtotal": "100",
            "Shipping and handling": "8",
            "Gross transaction amount": "108",
            "Net amount": "83",
            "Final Value Fee - fixed": "-0.44",
            "Final Value Fee - variable": "-19.56",
            "Charity donation": "-5",
            "Transaction currency": "USD",
        }
    )

    result = process_transaction_report(
        conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=[row]
    )

    assert result.orders_posted == 1
    assert result.review_queue_rows_created == 1
    assert result.parse_warnings == []

    # The sale itself posted at full gross (108 USD), unaffected by the
    # donation — confirmed via the Sales Revenue credit line.
    sale_entry = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "ebay_sale")
    ).scalar_one()
    sale_lines = conn.execute(
        select(journal_lines.c.credit_amount_idr, journal_lines.c.amount_usd_ref)
        .where(journal_lines.c.journal_entry_id == sale_entry, journal_lines.c.credit_amount_idr > 0)
    ).all()
    assert any(l.amount_usd_ref == Decimal("108.00") for l in sale_lines)

    # The donation itself is flagged separately, never posted, never
    # silently folded into fees/opex.
    from ingestion.schema import review_queue

    donation_row = conn.execute(
        select(review_queue.c.amount_usd_ref, review_queue.c.raw_description, review_queue.c.category)
    ).one()
    assert donation_row.amount_usd_ref == Decimal("5")
    assert donation_row.category is None
    assert "Charity donation" in donation_row.raw_description
