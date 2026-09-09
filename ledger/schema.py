"""Postgres schema for the core ledger engine.

Implements docs/design/schema-design.md exactly (table names, column
placement, and the three money-math detail tables) — this doc is the
user's own reviewed design and supersedes any earlier schema draft, per
docs/build-briefs/milestone-2-ledger-engine.md.

Table shapes follow CLAUDE.md's "Chart of accounts" and "Business model"
sections for the underlying accounting facts:

- eBay Wallet is genuinely 1:1 per eBay account (3 total) -> account_types
  row with scope_kind='per_ebay_account'.
- Payoneer Wallet and BCA Bridging Account are per *wallet-group* (2 total)
  -> scope_kind='per_wallet_group', never hardcoded 1:1 with an eBay
  account. A wallet-group can be shared by more than one eBay account.
- BCA Main Account, Consignor Payable, equity/revenue/COGS/opex/other
  accounts are consolidated singletons -> scope_kind='consolidated'.

Two invariants are enforced at the database level (not just in application
code), via Postgres triggers created in ``create_schema()``. schema-design.md
notes plain CHECK constraints can't check a cross-row SUM and says to
enforce the balance invariant in the posting engine (which posting.py
does) — these triggers are an additional, structural backstop beyond that
minimum: they hold even if something bypasses posting.py entirely.

1. ``trg_check_journal_entry_balance`` — a deferred constraint trigger that
   makes it structurally impossible to commit an unbalanced journal entry
   (debits != credits), or one with fewer than 2 lines.
2. ``trg_check_transfer_accounts`` — makes it structurally impossible for a
   journal line on an ``inter_account_transfer`` entry to post to anything
   other than the entity's own wallet/bank accounts (never revenue/expense).
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
    MetaData,
    Numeric,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

# ---------------------------------------------------------------------------
# Structural entities
# ---------------------------------------------------------------------------

wallet_groups = Table(
    "wallet_groups",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    # Explicit real Google Drive folder name for this wallet-group's uploads
    # folder (e.g. "Wallet Group for 1 (ricky-game)"), as it actually exists
    # under "01 - Uploads" in Drive — NOT necessarily the same string as
    # ``name`` above (a generic display label). Added 2026-09-02 to close a
    # known gap flagged during milestone 4's design review (Phase A): Sync
    # Now was deriving the Drive folder name from ``name`` instead of
    # storing the real folder name explicitly, so it silently looked in the
    # wrong folder. Nullable — NULL means "no explicit override recorded
    # yet"; callers fall back to deriving from ``name`` (the old behavior)
    # only in that case, never blindly.
    Column("drive_folder_name", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

ebay_accounts = Table(
    "ebay_accounts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("wallet_group_id", Integer, ForeignKey("wallet_groups.id"), nullable=False),
    Column("ebay_seller_username", Text, nullable=True),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    # Same purpose/rationale as wallet_groups.drive_folder_name above (e.g.
    # "eBay Account - 1 (ricky-game)") — nullable, explicit-field-with
    # -fallback pattern, not a hard requirement on every row.
    Column("drive_folder_name", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# ---------------------------------------------------------------------------
# Chart of accounts: account_types (the 19 GL line items from CLAUDE.md) and
# accounts (the actual postable ledger rows instantiated from them).
# ---------------------------------------------------------------------------

account_types = Table(
    "account_types",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("statement_section", Text, nullable=False),
    Column("normal_balance", Text, nullable=False),
    Column("scope_kind", Text, nullable=False),
    Column("is_contra", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint(
        "statement_section IN ('asset','liability','equity','revenue','cogs','opex','other_income_expense')",
        name="ck_account_types_statement_section",
    ),
    CheckConstraint("normal_balance IN ('debit','credit')", name="ck_account_types_normal_balance"),
    CheckConstraint(
        "scope_kind IN ('per_ebay_account','per_wallet_group','consolidated')",
        name="ck_account_types_scope_kind",
    ),
)

accounts = Table(
    "accounts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("account_type_id", Integer, ForeignKey("account_types.id"), nullable=False),
    Column("ebay_account_id", Integer, ForeignKey("ebay_accounts.id"), nullable=True),
    Column("wallet_group_id", Integer, ForeignKey("wallet_groups.id"), nullable=True),
    Column("currency", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "NOT (ebay_account_id IS NOT NULL AND wallet_group_id IS NOT NULL)",
        name="ck_accounts_single_scope",
    ),
    CheckConstraint("currency IN ('USD','IDR')", name="ck_accounts_currency"),
)

# Partial-unique indexes: this is what makes "2 Payoneer wallets for 3 eBay
# accounts" (or "3 eBay Wallets, 2 Payoneer Wallets") a real, structurally
# guaranteed fact rather than a convention — an account_type scoped
# per_wallet_group can never get two accounts rows for the same
# wallet_group_id (and likewise per_ebay_account / consolidated).
Index(
    "ux_accounts_per_ebay_account",
    accounts.c.account_type_id,
    accounts.c.ebay_account_id,
    unique=True,
    postgresql_where=text("ebay_account_id IS NOT NULL"),
)
Index(
    "ux_accounts_per_wallet_group",
    accounts.c.account_type_id,
    accounts.c.wallet_group_id,
    unique=True,
    postgresql_where=text("wallet_group_id IS NOT NULL"),
)
Index(
    "ux_accounts_consolidated",
    accounts.c.account_type_id,
    unique=True,
    postgresql_where=text("ebay_account_id IS NULL AND wallet_group_id IS NULL"),
)

# ---------------------------------------------------------------------------
# Categories — revenue-analytics tag only (see CLAUDE.md's Category tagging
# section). A lookup table, not a hardcoded enum, since phase 2's sales
# report will query against it.
# ---------------------------------------------------------------------------

categories = Table(
    "categories",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
)

# ---------------------------------------------------------------------------
# Consignor Payout Tiers ("Pasal 3" schedule) — admin-editable table.
# ---------------------------------------------------------------------------

consignor_payout_tiers = Table(
    "consignor_payout_tiers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("min_price_usd", Numeric(14, 2), nullable=False),
    Column("max_price_usd", Numeric(14, 2), nullable=True),  # NULL = open-ended (the $7,500+ row)
    # NULL for the $7,500+ row — "requires manual contact, never auto-applied".
    Column("rate_percent", Numeric(5, 2), nullable=True),
    Column("requires_manual_contact", Boolean, nullable=False, server_default=text("false")),
    Column("display_order", Integer, nullable=False),
)

# ---------------------------------------------------------------------------
# Journal entries (headers) and journal lines (the double-entry detail)
# ---------------------------------------------------------------------------

journal_entries = Table(
    "journal_entries",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("entry_date", Date, nullable=False),
    Column("period_month", Date, nullable=False),  # first-of-month, derived from entry_date
    Column("source_type", Text, nullable=False),
    Column("memo", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Reversal placeholder only — unused in this milestone. Corrections to
    # posted transactions are explicitly out of scope for the prototype
    # (see CLAUDE.md), but the column exists so a future reversal flow is
    # additive, not a migration.
    Column("reversed_by_id", Integer, ForeignKey("journal_entries.id"), nullable=True),
    CheckConstraint(
        "source_type IN ('ebay_sale','ebay_refund','cogs_purchase','consignment_sale',"
        "'consignment_payout','inter_account_transfer','payoneer_withdrawal',"
        "'fx_revaluation','owner_contribution','owner_draw','bank_other',"
        "'opening_balance')",
        name="ck_journal_entries_source_type",
    ),
)

journal_lines = Table(
    "journal_lines",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "journal_entry_id",
        Integer,
        ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("debit_amount_idr", Numeric(20, 2), nullable=False, server_default=text("0")),
    Column("credit_amount_idr", Numeric(20, 2), nullable=False, server_default=text("0")),
    Column("amount_usd_ref", Numeric(14, 2), nullable=True),
    Column("fx_rate_used", Numeric(12, 4), nullable=True),
    # Only meaningful on Sales Revenue lines (see CLAUDE.md's Category
    # tagging section) — never used to allocate any shared cost.
    Column("category_id", Integer, ForeignKey("categories.id"), nullable=True),
    Column("ebay_order_ref", Text, nullable=True),
    # Preserved for traceability even though it never rolls up — the only
    # liability figure that matters for statements is the aggregate
    # Consignor Payable balance.
    Column("consignor_item_ref", Text, nullable=True),
    CheckConstraint("debit_amount_idr >= 0", name="ck_journal_lines_debit_nonneg"),
    CheckConstraint("credit_amount_idr >= 0", name="ck_journal_lines_credit_nonneg"),
    CheckConstraint(
        "(debit_amount_idr > 0 AND credit_amount_idr = 0) OR "
        "(credit_amount_idr > 0 AND debit_amount_idr = 0)",
        name="ck_journal_lines_single_sided",
    ),
)

Index("ix_journal_lines_journal_entry_id", journal_lines.c.journal_entry_id)
Index("ix_journal_entries_entry_date", journal_entries.c.entry_date)
Index("ix_journal_entries_period_month", journal_entries.c.period_month)

# ---------------------------------------------------------------------------
# Money-math detail tables — back the report drill-downs CLAUDE.md requires
# ("every figure traceable to source, not a black-box total").
# ---------------------------------------------------------------------------

consignment_sales = Table(
    "consignment_sales",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("item_price_usd", Numeric(14, 2), nullable=False),  # excludes shipping, per CLAUDE.md
    Column("shipping_cost_usd", Numeric(14, 2), nullable=True),  # only for the experimental model
    Column("payout_model", Text, nullable=False),  # 'tier' | 'net_of_fees_and_shipping'
    Column("tier_rate_percent", Numeric(5, 2), nullable=True),  # null for the experimental model
    Column("payout_amount_idr", Numeric(20, 2), nullable=False),
    Column("consignor_item_ref", Text, nullable=False),
    # NULL = not yet confirmed by a human, which is what blocks posting.
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    # NULL until actually posted — set only inside post_consignment_sale(),
    # and only once confirmed_at is non-null.
    Column("journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "payout_model IN ('tier','net_of_fees_and_shipping')",
        name="ck_consignment_sales_payout_model",
    ),
    # Structural backstop matching the acceptance criterion directly: a row
    # can never have a journal_entry_id while confirmed_at is still NULL.
    CheckConstraint(
        "journal_entry_id IS NULL OR confirmed_at IS NOT NULL",
        name="ck_consignment_sales_no_post_without_confirmation",
    ),
)

payoneer_withdrawals = Table(
    "payoneer_withdrawals",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("wallet_group_id", Integer, ForeignKey("wallet_groups.id"), nullable=False),
    Column("withdrawal_date", Date, nullable=False),
    Column("gross_usd", Numeric(14, 2), nullable=False),
    Column("payoneer_fee_usd", Numeric(14, 2), nullable=False),
    Column("exchange_rate_excl_fee", Numeric(12, 4), nullable=False),
    Column("net_idr_landed", Numeric(20, 2), nullable=False),
    # The Kurs Pajak rate the underlying sale(s) being withdrawn were
    # originally booked at — needed to compute the realized FX spread.
    Column("booking_rate_used_idr", Numeric(12, 4), nullable=False),
    Column("journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=False),
)

fx_revaluations = Table(
    "fx_revaluations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("wallet_group_id", Integer, ForeignKey("wallet_groups.id"), nullable=False),
    Column("period_month", Date, nullable=False),
    Column("usd_balance", Numeric(14, 2), nullable=False),
    Column("kemenkeu_eom_rate_idr", Numeric(12, 4), nullable=False),
    Column("revalued_idr", Numeric(20, 2), nullable=False),
    Column("journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=False),
)

# Real DB-level backstop for the month-end FX revaluation job's idempotency
# (milestone 5, see scheduling/fx_revaluation.py) — makes "one revaluation
# per wallet-group per period" a structural guarantee, not just an
# application-layer SELECT-before-INSERT convention (which this project has
# a real history of races bypassing — see ledger/migrations.py's audit of
# the same class of bug on consignment_sales/invoices). A genuinely
# concurrent second attempt at posting the same wallet-group/period fails
# fast with an IntegrityError that scheduling.fx_revaluation catches inside
# a SAVEPOINT, atomically rolling back that attempt's journal entry too.
Index(
    "ux_fx_revaluations_wallet_group_period",
    fx_revaluations.c.wallet_group_id,
    fx_revaluations.c.period_month,
    unique=True,
)

# One-time opening-balance entries (added 2026-09-03) — records a wallet/
# bank account's real balance as of just before ledger-tracking began
# (2026-05-01, the earliest posted entry in the real database), booked to
# Owner's Capital. See ledger/posting.py's post_opening_balance and
# CLAUDE.md's Definition of done (the negative-Payoneer-balance gap this
# closes). Same "money-math detail table" pattern as consignment_sales /
# payoneer_withdrawals / fx_revaluations above: one row per real event,
# journal_entry_id links it back to the actual posting for traceability.
opening_balances = Table(
    "opening_balances",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("entry_date", Date, nullable=False),
    Column("amount_idr", Numeric(20, 2), nullable=False),
    Column("amount_usd_ref", Numeric(14, 2), nullable=True),
    Column("fx_rate_used", Numeric(12, 4), nullable=True),
    Column("journal_entry_id", Integer, ForeignKey("journal_entries.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Real DB-level backstop (not just an app-layer SELECT-before-INSERT) making
# "at most one opening_balance entry per account" a structural guarantee —
# this project has a real, repeated history of exactly this bug class
# (missing DB-level dedup constraints; see ledger/migrations.py's audit of
# consignment_sales/invoices/fx_revaluations). A second post_opening_balance
# call for an account that already has one fails fast at INSERT time with an
# IntegrityError rather than silently double-posting.
Index(
    "ux_opening_balances_account_id",
    opening_balances.c.account_id,
    unique=True,
)

# Reconciliation-gap detection (added 2026-09) — compares the ledger's own
# computed opening/closing balance for an account against what the SOURCE
# bank statement document itself printed, for the accounts/periods where a
# real parsed statement actually carries both figures (currently: BCA Main
# Account and Mandiri Bridging Account statements — see
# ingestion/reconciliation.py's module docstring for why Payoneer/eBay
# Wallet are never checked here). Detection and surfacing ONLY: nothing in
# this project ever writes a correcting entry from this table — see
# CLAUDE.md's "Correcting a posted review-queue row" deferred item, which
# this feature deliberately does not reopen.
reconciliation_checks = Table(
    "reconciliation_checks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("period_month", Date, nullable=False),
    Column("expected_opening_idr", Numeric(20, 2), nullable=False),
    Column("actual_opening_idr", Numeric(20, 2), nullable=False),
    # actual - expected, signed (positive = ledger shows MORE than the
    # statement states).
    Column("opening_discrepancy_idr", Numeric(20, 2), nullable=False),
    Column("expected_closing_idr", Numeric(20, 2), nullable=False),
    Column("actual_closing_idr", Numeric(20, 2), nullable=False),
    Column("closing_discrepancy_idr", Numeric(20, 2), nullable=False),
    # True if either discrepancy is >= the materiality threshold (see
    # ingestion.reconciliation.MATERIALITY_THRESHOLD_IDR) — the single flag
    # webapp.finalization checks to gate a report to Provisional.
    Column("is_material", Boolean, nullable=False),
    # Plain integer, deliberately WITHOUT a ForeignKey declared inline here:
    # source_documents lives in ingestion/schema.py, which is not
    # guaranteed to be imported (and therefore not guaranteed to exist in
    # this shared MetaData) when ledger.schema.create_schema() runs
    # standalone for milestone-2-only use. The real FK constraint is added
    # by ledger/migrations.py once source_documents actually exists in the
    # target database — same precedent as payoneer_withdrawals.
    # bridging_landing_reconciled_review_queue_id above.
    Column("source_document_id", Integer, nullable=True),
    Column("checked_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Real DB-level backstop making "at most one reconciliation_checks row per
# account/period" a structural guarantee, not just an app-layer SELECT
# -before-write convention — same class of protection already applied to
# fx_revaluations/opening_balances above. A re-run for the same account/
# period UPDATEs the existing row (see ingestion.reconciliation) rather
# than ever accumulating duplicates.
Index(
    "ux_reconciliation_checks_account_period",
    reconciliation_checks.c.account_id,
    reconciliation_checks.c.period_month,
    unique=True,
)


# ---------------------------------------------------------------------------
# Postgres-only structural triggers (defense-in-depth: hold even if a
# caller bypasses ledger/posting.py entirely and inserts raw SQL).
# ---------------------------------------------------------------------------

_BALANCE_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION check_journal_entry_balance() RETURNS TRIGGER AS $$
DECLARE
    entry_id INTEGER;
    total_debit NUMERIC;
    total_credit NUMERIC;
    line_count INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        entry_id := OLD.journal_entry_id;
    ELSE
        entry_id := NEW.journal_entry_id;
    END IF;

    SELECT COALESCE(SUM(debit_amount_idr), 0), COALESCE(SUM(credit_amount_idr), 0), COUNT(*)
        INTO total_debit, total_credit, line_count
        FROM journal_lines
        WHERE journal_entry_id = entry_id;

    IF line_count > 0 AND line_count < 2 THEN
        RAISE EXCEPTION
            'Journal entry % has only % line(s); every posting needs at least 2',
            entry_id, line_count;
    END IF;

    IF total_debit <> total_credit THEN
        RAISE EXCEPTION
            'Unbalanced journal entry %: total debits % != total credits %',
            entry_id, total_debit, total_credit;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_journal_entry_balance ON journal_lines;
CREATE CONSTRAINT TRIGGER trg_check_journal_entry_balance
    AFTER INSERT OR UPDATE OR DELETE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_journal_entry_balance();
"""

_TRANSFER_ACCOUNT_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION check_transfer_accounts() RETURNS TRIGGER AS $$
DECLARE
    s_type TEXT;
    acc_code TEXT;
BEGIN
    SELECT source_type INTO s_type FROM journal_entries WHERE id = NEW.journal_entry_id;

    IF s_type = 'inter_account_transfer' THEN
        SELECT at.code INTO acc_code
            FROM accounts a
            JOIN account_types at ON at.id = a.account_type_id
            WHERE a.id = NEW.account_id;

        IF acc_code NOT IN ('EBAY_WALLET', 'PAYONEER_WALLET', 'BCA_BRIDGING', 'BCA_MAIN') THEN
            RAISE EXCEPTION
                'inter_account_transfer line % posts to non-transfer account %: '
                'transfers may only move money between the entity''s own wallet/bank '
                'accounts, never a revenue or expense account',
                NEW.id, acc_code;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_transfer_accounts ON journal_lines;
CREATE TRIGGER trg_check_transfer_accounts
    BEFORE INSERT OR UPDATE ON journal_lines
    FOR EACH ROW EXECUTE FUNCTION check_transfer_accounts();
"""


def create_schema(engine: Engine) -> None:
    """Create all tables and (on Postgres) the structural enforcement
    triggers, then bring the schema up to date with every additive change
    ``metadata.create_all()`` can't retrofit onto an already-existing table
    (a later-added nullable column, index, or widened CHECK constraint —
    see ``ledger.migrations`` for the full audited list and why this exists).

    Calling this on a brand-new database is a no-op for the migrations step
    (create_all already built the current shape). Calling it on an older
    database — including one that only ever provisioned milestone 2's
    tables and never imported ingestion.schema/webapp.schema — brings it
    current without erroring; ledger.migrations.run_migrations skips any
    step whose target table isn't provisioned yet at this layer.
    """
    metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(_BALANCE_TRIGGER_SQL))
            conn.execute(text(_TRANSFER_ACCOUNT_TRIGGER_SQL))

        # Imported lazily, inside the function, to avoid a module-import-time
        # circular concern (ledger.migrations has no need to import
        # ledger.schema itself, but keeping the import here rather than at
        # module scope keeps create_schema's own dependency direction
        # obvious: schema definition first, migrations applied after).
        from ledger.migrations import run_migrations

        run_migrations(engine)


class UnsafeSchemaDropError(RuntimeError):
    """Raised when drop_schema() is asked to run against a database that
    doesn't look disposable.

    Added after a 2026-09 incident: a test run's engine fixture resolved to
    the real, persistent `noctrowl` database (via a TEST_DATABASE_URL ->
    DATABASE_URL fallback in tests/conftest.py) and drop_schema() destroyed
    880 real posted journal entries. The fallback itself has since been
    removed, but this check lives here — at the actual destructive call,
    not just in the test fixtures that happen to call it today — so ANY
    caller (a new test file, a script, a REPL session, a future fixture
    someone adds without reading this file) gets the same protection even
    if it forgets to re-implement the guard itself.
    """


def drop_schema(engine: Engine) -> None:
    """Drop all tables (and their triggers, via CASCADE). Test/dev use only.

    Refuses to run unless the target database's name contains "test"
    (case-insensitive) — see UnsafeSchemaDropError above for why. A database
    named plain "noctrowl" must never pass this check, even if some caller
    genuinely means to point at it; rename the database instead.
    """
    db_name = (engine.url.database or "")
    if "test" not in db_name.lower():
        raise UnsafeSchemaDropError(
            f"Refusing to drop_schema() on database {db_name!r} — its name "
            "does not contain 'test'. drop_schema() drops every table and "
            "must only ever run against a disposable test database (e.g. "
            "'noctrowl_test'), never a real/persistent one. If this really "
            "is meant to be a disposable database, rename it to include "
            "'test' rather than bypassing this check."
        )
    metadata.drop_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("DROP FUNCTION IF EXISTS check_journal_entry_balance() CASCADE"))
            conn.execute(text("DROP FUNCTION IF EXISTS check_transfer_accounts() CASCADE"))
