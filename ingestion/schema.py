"""Milestone 3 schema additions.

Implements docs/design/milestone-3-ingestion-design.md §1, as amended by
Main-agent's 2026-08-31 resolutions. Every table here is NEW — nothing in
``ledger/schema.py`` is edited. New tables are registered against the SAME
SQLAlchemy ``MetaData`` object milestone 2 already uses (imported, not
recreated), so ``create_ingestion_schema(engine)`` below fully provisions
both milestone 2's and milestone 3's tables together in one call.

The one exception to "every table here is new" is
``consignment_sales.reimbursed_journal_entry_id`` — a single NEW NULLABLE
column appended (via SQLAlchemy's ``Table.append_column``, not by editing
``ledger/schema.py``'s source) to the existing milestone-2
``consignment_sales`` table, per the design doc's §1. It lets auto-match
rule (d) tell "confirmed + posted-as-a-sale but not yet reimbursed" apart
from "already reimbursed" without touching any existing column.

Design note on ``invoices`` <-> ``review_queue``: the design doc originally
sketched a matched-pointer column on both sides. That's a genuine circular
foreign key and (worse) two places to keep the same fact in sync. Simplified
here to a single direction of truth: ``review_queue.linked_invoice_id``
points at the invoice a bank/Payoneer line matched (rule (b)); "which
review_queue row matched a given invoice" is answered by a reverse query
(``SELECT * FROM review_queue WHERE linked_invoice_id = :invoice_id``)
rather than a redundant back-reference column.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.engine import Engine

from ledger.schema import consignment_sales, metadata, payoneer_withdrawals

# ---------------------------------------------------------------------------
# Additive column on the existing milestone-2 consignment_sales table.
# ---------------------------------------------------------------------------

if "reimbursed_journal_entry_id" not in consignment_sales.c:
    consignment_sales.append_column(
        Column(
            "reimbursed_journal_entry_id",
            Integer,
            ForeignKey("journal_entries.id"),
            nullable=True,
        )
    )

# A second additive column, same pattern, on the existing milestone-2
# payoneer_withdrawals table: solves the "paired-transfer double-posting
# risk" from design doc §6 (a single real BCA Bridging -> BCA Main transfer
# can show up as TWO bank lines to reconcile — an outflow in the
# wallet-group's bridging statement, an inflow in the Master statement).
# Whichever review_queue row is processed FIRST posts the transfer and sets
# this; the second row that matches the same withdrawal's net_idr_landed
# amount sees it already set and just links to the existing journal entry
# instead of posting a second time. See ingestion/matching.py.
if "bridging_to_main_journal_entry_id" not in payoneer_withdrawals.c:
    payoneer_withdrawals.append_column(
        Column(
            "bridging_to_main_journal_entry_id",
            Integer,
            ForeignKey("journal_entries.id"),
            nullable=True,
        )
    )

# BUG FIX (QA, 2026-09): a real DB-level unique index on
# consignment_sales.consignor_item_ref — the same additive pattern as the
# two appended columns above, just an index instead of a column. Before
# this, the ONLY thing stopping a duplicate consignment_sales row (same
# consignor_item_ref) was the app-layer SELECT-before-insert guard in
# ingestion/ebay_csv.py's CONSIGN- branch — safe against sequential re-runs
# of the same sync, but not against two overlapping/concurrent sync calls,
# unlike every other idempotency-critical table this milestone touched
# (review_queue.external_ref, ebay_expected_payouts'
# (ebay_account_id, ebay_payout_id), both *_posted_transactions tables,
# invoice_journal_links — all have a real unique index). QA confirmed via
# raw SQL (bypassing ingestion/ebay_csv.py entirely) that two rows with an
# identical consignor_item_ref inserted silently before this fix. The key
# as built (f"{custom_label}:{order_number}") is genuinely globally unique
# — order_number is unique per eBay order — so a plain unique index is the
# correct minimal fix, no compound key needed.
Index(
    "ux_consignment_sales_consignor_item_ref",
    consignment_sales.c.consignor_item_ref,
    unique=True,
)

# ---------------------------------------------------------------------------
# source_documents — one row per EXPECTED fixed-expectation upload (eBay
# sales CSV, Payoneer CSV, wallet-group bank statement, master bank
# statement). Invoices are deliberately NOT here (variable count, not a
# fixed expectation — see CLAUDE.md's Report finalization status / Documents
# screen design). This table is what a future Provisional/Final status
# computation (not built in this milestone) would query against.
# ---------------------------------------------------------------------------

source_documents = Table(
    "source_documents",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("document_type", Text, nullable=False),
    Column("ebay_account_id", Integer, ForeignKey("ebay_accounts.id"), nullable=True),
    Column("wallet_group_id", Integer, ForeignKey("wallet_groups.id"), nullable=True),
    Column("period_month", Date, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=True),
    Column("drive_file_id", Text, nullable=True),
    Column("drive_file_name", Text, nullable=True),
    Column("row_count", Integer, nullable=True),
    Column("parse_warning", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "document_type IN ('ebay_sales_csv','payoneer_csv','bank_statement_wallet_group',"
        "'bank_statement_master')",
        name="ck_source_documents_document_type",
    ),
    CheckConstraint(
        "NOT (ebay_account_id IS NOT NULL AND wallet_group_id IS NOT NULL)",
        name="ck_source_documents_single_scope",
    ),
    # Each document_type has a FIXED scope_kind (mirrors account_types):
    # ebay_sales_csv -> per_ebay_account, payoneer_csv /
    # bank_statement_wallet_group -> per_wallet_group, bank_statement_master
    # -> consolidated. Enforced here rather than a second scope_kind column.
    CheckConstraint(
        "(document_type = 'ebay_sales_csv' AND ebay_account_id IS NOT NULL AND wallet_group_id IS NULL) OR "
        "(document_type IN ('payoneer_csv','bank_statement_wallet_group') "
        " AND wallet_group_id IS NOT NULL AND ebay_account_id IS NULL) OR "
        "(document_type = 'bank_statement_master' AND ebay_account_id IS NULL AND wallet_group_id IS NULL)",
        name="ck_source_documents_scope_matches_type",
    ),
)

Index(
    "ux_source_documents_per_ebay_account",
    source_documents.c.document_type,
    source_documents.c.period_month,
    source_documents.c.ebay_account_id,
    unique=True,
    postgresql_where=text("ebay_account_id IS NOT NULL"),
)
Index(
    "ux_source_documents_per_wallet_group",
    source_documents.c.document_type,
    source_documents.c.period_month,
    source_documents.c.wallet_group_id,
    unique=True,
    postgresql_where=text("wallet_group_id IS NOT NULL"),
)
Index(
    "ux_source_documents_consolidated",
    source_documents.c.document_type,
    source_documents.c.period_month,
    unique=True,
    postgresql_where=text("ebay_account_id IS NULL AND wallet_group_id IS NULL"),
)

# ---------------------------------------------------------------------------
# invoices — OCR-extracted purchase / proof-of-transfer records, consolidated
# level only (no ebay_account_id / wallet_group_id, per CLAUDE.md).
# ---------------------------------------------------------------------------

invoices = Table(
    "invoices",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("drive_file_id", Text, nullable=True),
    Column("drive_file_name", Text, nullable=False),
    Column("period_month", Date, nullable=False),
    Column("extracted_date", Date, nullable=True),
    Column("vendor_description", Text, nullable=True),
    Column("amount_idr", Numeric(20, 2), nullable=True),
    Column("purpose", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("ocr_raw_text", Text, nullable=True),
    Column("ocr_confidence", Numeric(5, 2), nullable=True),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "purpose IS NULL OR purpose IN ('cogs_purchase','consignment_purchase')",
        name="ck_invoices_purpose",
    ),
    CheckConstraint("status IN ('parsed','needs_confirmation')", name="ck_invoices_status"),
    CheckConstraint("amount_idr IS NULL OR amount_idr > 0", name="ck_invoices_amount_positive"),
)

# BUG FIX (QA, 2026-09 — secondary/lower-priority, same review pass as the
# consignment_sales index above): ingestion/sync.py's sync_invoices() only
# had an app-layer SELECT-before-insert guard on drive_file_id, no DB-level
# backstop — same inconsistent pattern, lower severity here since a
# duplicate invoice record doesn't itself double-post money (only a
# matched review_queue row does, and that path is already guarded).
# Partial (WHERE NOT NULL) since drive_file_id is nullable in principle.
Index(
    "ux_invoices_drive_file_id",
    invoices.c.drive_file_id,
    unique=True,
    postgresql_where=text("drive_file_id IS NOT NULL"),
)

# ---------------------------------------------------------------------------
# invoice_journal_links — traceability join: which posted journal entry a
# given invoice justified (COGS or consignment reimbursement).
# ---------------------------------------------------------------------------

invoice_journal_links = Table(
    "invoice_journal_links",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=False),
    Column("invoice_id", Integer, ForeignKey("invoices.id"), nullable=False),
)

Index(
    "ux_invoice_journal_links_pair",
    invoice_journal_links.c.journal_entry_id,
    invoice_journal_links.c.invoice_id,
    unique=True,
)

# ---------------------------------------------------------------------------
# review_queue — bank/Payoneer (and defensively, unexpected eBay-CSV-row)
# lines awaiting or already carrying a classification.
# ---------------------------------------------------------------------------

review_queue = Table(
    "review_queue",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_type", Text, nullable=False),
    Column("source_document_id", Integer, ForeignKey("source_documents.id"), nullable=False),
    Column("ebay_account_id", Integer, ForeignKey("ebay_accounts.id"), nullable=True),
    Column("wallet_group_id", Integer, ForeignKey("wallet_groups.id"), nullable=True),
    Column("external_ref", Text, nullable=True),
    Column("transaction_date", Date, nullable=False),
    Column("amount_idr", Numeric(20, 2), nullable=False),
    Column("amount_usd_ref", Numeric(14, 2), nullable=True),
    Column("raw_description", Text, nullable=False),
    Column("match_status", Text, nullable=False),
    Column("match_rule", Text, nullable=True),
    Column("category", Text, nullable=True),
    Column("consignor_item_ref", Text, nullable=True),
    Column("linked_invoice_id", Integer, ForeignKey("invoices.id"), nullable=True),
    Column("labeled_at", DateTime(timezone=True), nullable=True),
    Column("posted_at", DateTime(timezone=True), nullable=True),
    Column("posted_journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "source_type IN ('payoneer_csv','bank_statement','ebay_sales_csv')",
        name="ck_review_queue_source_type",
    ),
    CheckConstraint("match_status IN ('matched','needs_review')", name="ck_review_queue_match_status"),
    CheckConstraint(
        "category IS NULL OR category IN ('revenue_settlement','cogs_purchase','consignment_payout',"
        "'internal_transfer','operating_expense','owners_draw','owners_contribution','other')",
        name="ck_review_queue_category",
    ),
    # The idempotency invariant from CLAUDE.md rule 6, structural: a row can
    # never be "posted" without a category (never a silent best-guess post),
    # and never have a journal_entry_id while posted_at is still NULL.
    CheckConstraint(
        "posted_at IS NULL OR category IS NOT NULL", name="ck_review_queue_no_post_without_category"
    ),
    CheckConstraint(
        "posted_journal_entry_id IS NULL OR posted_at IS NOT NULL",
        name="ck_review_queue_no_journal_without_posted_at",
    ),
)

Index(
    "ux_review_queue_source_external_ref",
    review_queue.c.source_type,
    review_queue.c.external_ref,
    unique=True,
    postgresql_where=text("external_ref IS NOT NULL"),
)
Index("ix_review_queue_posted_at", review_queue.c.posted_at)
Index("ix_review_queue_match_status", review_queue.c.match_status)

# ---------------------------------------------------------------------------
# ebay_expected_payouts — reference facts from eBay CSV `Payout` rows. NOT
# posted directly (see design doc §2/§3) — consumed by the Payoneer-CSV
# auto-match rule (a). Defined after review_queue so its FK to it needs no
# string-forward-reference trickery.
# ---------------------------------------------------------------------------

ebay_expected_payouts = Table(
    "ebay_expected_payouts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ebay_account_id", Integer, ForeignKey("ebay_accounts.id"), nullable=False),
    Column("ebay_payout_id", Text, nullable=False),
    Column("payout_date", Date, nullable=False),
    Column("net_amount_usd", Numeric(14, 2), nullable=False),
    Column("source_document_id", Integer, ForeignKey("source_documents.id"), nullable=True),
    # NULL = not yet confirmed as arrived. Set either directly (a Payoneer
    # CSV credit row matched it, no review_queue row needed — see
    # ingestion.payoneer) or, in principle, via a review_queue linkage in a
    # future extension. A plain timestamp rather than a review_queue FK
    # specifically because the common path posts directly without ever
    # creating a review_queue row (the Payoneer CSV is structured/trusted
    # data, not something needing human classification — see design doc §3).
    Column("matched_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("net_amount_usd > 0", name="ck_ebay_expected_payouts_amount_positive"),
)

Index(
    "ux_ebay_expected_payouts_account_payout_id",
    ebay_expected_payouts.c.ebay_account_id,
    ebay_expected_payouts.c.ebay_payout_id,
    unique=True,
)

# ---------------------------------------------------------------------------
# ebay_csv_posted_transactions — posting idempotency for the eBay CSV's
# DIRECT-posting rows (Order/Refund/Other fee — Payout rows already have
# their own idempotency via ebay_expected_payouts' unique index above).
#
# BUG FIX (QA, 2026-09): unlike review_queue rows (which have a
# posted_at/posted_journal_entry_id guard from day one), the eBay CSV's
# Order/Refund/Other-fee rows call ledger.posting functions DIRECTLY from
# ingestion.ebay_csv, with no equivalent guard — re-running the sync for a
# period whose file hasn't changed re-posted every sale/refund/fee a second
# time. Caught by tests/ingestion/test_sync.py's two-runs-are-idempotent
# test once the orchestration pipeline (ingestion/sync.py) made re-running
# ingestion an actual realistic scenario, not just a hypothetical.
#
# ``row_key`` encodes what's being posted (Order number for a merged Order
# group; the row's own ``Reference ID`` for Refund/Other fee — confirmed
# against the real sample that Reference ID is unique and non-blank for
# both those types, whereas Transaction ID is blank on them).
# ---------------------------------------------------------------------------

ebay_csv_posted_transactions = Table(
    "ebay_csv_posted_transactions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ebay_account_id", Integer, ForeignKey("ebay_accounts.id"), nullable=False),
    Column("row_key", Text, nullable=False),
    Column("journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

Index(
    "ux_ebay_csv_posted_transactions_account_row_key",
    ebay_csv_posted_transactions.c.ebay_account_id,
    ebay_csv_posted_transactions.c.row_key,
    unique=True,
)

# ---------------------------------------------------------------------------
# payoneer_csv_posted_transactions — the same posting-idempotency fix as
# ebay_csv_posted_transactions above, for ingestion.payoneer's two DIRECT
# -posting branches (a "Payment from eBay" row confirmed against an
# expected payout -> post_inter_account_transfer; a "Withdrawal to..." row
# matched to its confirmation PDF -> post_realized_fx_withdrawal). Found
# during the same QA-driven idempotency review as the eBay CSV bug — the
# withdrawal branch in particular had NO guard at all (would re-post the
# same realized-FX withdrawal event every time the same CSV+confirmation
# pair was re-ingested), which is the more serious of the two since it's a
# straightforward double-post of a real money-moving event, not just
# duplicate review-queue noise.
#
# Keyed on the Payoneer CSV's own ``Transaction ID`` (confirmed globally
# unique, non-blank, on every real sample row — see
# docs/design/milestone-3-ingestion-design.md §3), scoped per wallet_group.
# ---------------------------------------------------------------------------

payoneer_csv_posted_transactions = Table(
    "payoneer_csv_posted_transactions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("wallet_group_id", Integer, ForeignKey("wallet_groups.id"), nullable=False),
    Column("payoneer_transaction_id", Text, nullable=False),
    Column("journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

Index(
    "ux_payoneer_csv_posted_transactions_wg_txn_id",
    payoneer_csv_posted_transactions.c.wallet_group_id,
    payoneer_csv_posted_transactions.c.payoneer_transaction_id,
    unique=True,
)

# ---------------------------------------------------------------------------
# bank_keyword_rules — admin-editable-later lookup for auto-match rule (e).
# ---------------------------------------------------------------------------

bank_keyword_rules = Table(
    "bank_keyword_rules",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("keyword", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("expense_account_type_code", Text, nullable=True),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "category IN ('revenue_settlement','cogs_purchase','consignment_payout',"
        "'internal_transfer','operating_expense','owners_draw','owners_contribution','other')",
        name="ck_bank_keyword_rules_category",
    ),
)

# ---------------------------------------------------------------------------
# kurs_pajak_rates — manually-seeded weekly reference rate table.
# ---------------------------------------------------------------------------

kurs_pajak_rates = Table(
    "kurs_pajak_rates",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("effective_date", Date, nullable=False, unique=True),
    Column("rate_idr", Numeric(12, 4), nullable=False),
    CheckConstraint("rate_idr > 0", name="ck_kurs_pajak_rates_rate_positive"),
)


def create_ingestion_schema(engine: Engine) -> None:
    """Create all milestone-3 tables (and the additive consignment_sales
    column) on top of an already-created milestone-2 schema.

    Safe to call standalone too: by the time this module is imported, the
    new tables (and the appended consignment_sales column) are already
    registered on the shared ``metadata`` object milestone 2 uses, so this
    single ``metadata.create_all(engine)`` call picks up milestone 2's AND
    milestone 3's tables together, in FK-dependency order. Calling
    ``ledger.schema.create_schema(engine)`` first (for its Postgres
    triggers) then this is the intended sequence — see
    ``ingestion.seed.provision_test_schema`` for the combined helper tests
    use.
    """
    metadata.create_all(engine)


def drop_ingestion_schema(engine: Engine) -> None:
    """Drop only the milestone-3 tables (test/dev use only). Does not touch
    milestone-2 tables — use ``ledger.schema.drop_schema`` for those, or just
    call ``ledger.schema.drop_schema`` alone since it CASCADEs and will also
    remove these (they FK into milestone-2 tables).
    """
    for table in (
        bank_keyword_rules,
        kurs_pajak_rates,
        ebay_expected_payouts,
        invoice_journal_links,
        review_queue,
        invoices,
        source_documents,
    ):
        table.drop(engine, checkfirst=True)
