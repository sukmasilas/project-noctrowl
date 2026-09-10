"""Idempotent, additive-only schema migrations.

Why this file exists (2026-09-02, QA finding): this project's provisioning
story has always been ``metadata.create_all(engine)`` — see
``ledger.schema.create_schema``, ``ingestion.schema.create_ingestion_schema``,
``webapp.schema.create_webapp_schema``. ``create_all`` is checkfirst *per
table*: if a table already exists, SQLAlchemy skips it entirely, including
any column, index, or CHECK constraint added to that table's definition
*after* the table was first created against a given database. This project
has made several such additive changes over its history (a nullable column
appended to ``consignment_sales``, another to ``payoneer_withdrawals``, two
more to ``review_queue``, a unique index each on ``consignment_sales`` and
``invoices``, three widened CHECK constraints, and now
``drive_folder_name`` on both ``ebay_accounts`` and ``wallet_groups`` — see
``MIGRATIONS`` below for the full, audited list). None of these ever had a
real migration mechanism behind them — they worked against the real
``noctrowl`` database only because someone ran the equivalent ALTER TABLE by
hand each time, which is unreproducible, undocumented, and would crash (or
silently under-apply) on a fresh deployment or anyone else's
partially-up-to-date copy of the database. QA proved this concretely: create
a schema, drop just the new ``drive_folder_name`` column to simulate an
older real database, re-run ``create_schema()``, and the column stays
missing.

This module is the fix: a small, ordered, IDEMPOTENT list of raw DDL
statements — deliberately not Alembic-level machinery, since every schema
change this project has ever made has been purely additive (new nullable
column, new index, or a wider CHECK constraint's allowed-value set; never a
column rename, type change, NOT NULL tightening, or drop). Each statement is
safe to run any number of times, against a database at ANY point in this
project's history:

- a completely fresh database (every step is a no-op — ``create_all`` already
  built the current shape from the current Python table definitions)
- a database that only ever ran milestone 2's ``ledger.schema.create_schema``
  and never imported ``ingestion.schema``/``webapp.schema`` (some steps'
  target tables — ``review_queue``, ``invoices``, ``bank_keyword_rules`` —
  won't exist yet in that case; ``run_migrations`` checks each step's table
  exists first and SKIPS it rather than erroring, exactly the same
  "not yet provisioned at this layer" tolerance ``create_schema`` itself
  already has to support standalone milestone-2 use)
- a database that's missing one or more of the specific additive changes
  below, from any point in the project's history
- the real, current ``noctrowl`` database, which (as of this session) already
  has every change below applied by hand — running this module against it is
  expected to be a full no-op confirming that, not a destructive action.

``run_migrations(engine)`` is called automatically at the end of
``ledger.schema.create_schema()`` (see that function) so every existing
provisioning call path — every test fixture, and any future app-startup
provisioning — stays current with zero extra wiring. It is ALSO safely
callable standalone (see ``scripts/run_migrations.py``) for applying it to an
already-running database (like the real droplet's ``noctrowl``) without
dropping/recreating anything.

Action log — what was actually run against the real ``noctrowl`` database
this session, retroactively documented per CLAUDE.md's action-log
requirement (the original ``drive_folder_name`` application on 2026-09-02
was a manual, unreproducible ``ALTER TABLE`` + ``UPDATE`` run directly via
psycopg2/SQLAlchemy in an ad hoc script, before this migrations module
existed):

1. 2026-09-02, manual (superseded): ``ALTER TABLE ebay_accounts ADD COLUMN
   IF NOT EXISTS drive_folder_name TEXT`` and the same for
   ``wallet_groups``, then ``UPDATE ebay_accounts SET drive_folder_name =
   'eBay Account - 1 (ricky-game)' WHERE id = 1`` and ``UPDATE
   wallet_groups SET drive_folder_name = 'Wallet Group for 1 (ricky-game)'
   WHERE id = 1`` — folder names confirmed beforehand by listing the real
   Drive "Finance & Accounting" folder tree, not guessed.
2. 2026-09-02, reproducible (this module, via ``scripts/run_migrations.py``):
   ``run_migrations()`` was run against the real ``noctrowl`` database via
   ``DATABASE_URL``. Every column/index/constraint step reported
   "already_present"/"ensured" (all had already been applied, either by step
   1 above for ``drive_folder_name``, or by earlier undocumented manual
   fixes for everything else in ``MIGRATIONS`` — see the git-history audit
   in each step's own comment below for evidence of when each was
   introduced). The two seed ``UPDATE`` statements in step 1 are NOT part of
   ``MIGRATIONS`` (seed data, not schema) and were re-verified directly by
   ``SELECT`` afterward, not re-run.
"""
from __future__ import annotations

import dataclasses

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclasses.dataclass(frozen=True)
class MigrationStep:
    id: str
    description: str
    # The table this step's DDL targets. Existence is checked before running
    # anything — if it's missing, this step is a no-op SKIP (see module
    # docstring: some tables only exist once ingestion.schema/webapp.schema
    # have been imported and their metadata.create_all() has run).
    table: str
    # Any OTHER table a REFERENCES clause in this step's DDL points at.
    # Checked the same way — e.g. payoneer_withdrawals always exists
    # (ledger-native), but its bridging_landing_reconciled_review_queue_id
    # column references review_queue, which is ingestion-only.
    requires_tables: tuple[str, ...] = ()
    # A SELECT that returns at least one row iff this step's change is
    # already present — used only to report "already_present" vs "applied"
    # in the returned MigrationResult list; the DDL itself always runs
    # regardless (it's written to be a safe no-op when already applied).
    # None for the CHECK-constraint-widening steps, where the "already
    # matches" comparison would require parsing Postgres's own normalized
    # constraint-definition text — not worth the fragility; those steps are
    # always reported as "ensured".
    already_applied_check: str | None = None
    apply_sql: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The full, audited list. Every entry below was found by diffing this
# project's schema files across its git history (and its current
# uncommitted working-tree state) for a column/index/constraint added onto
# a table that ALREADY existed in an earlier state — i.e. exactly the class
# of change ``metadata.create_all()`` cannot retrofit. A change that
# introduced a brand-new table together with all of its own columns/
# indexes/constraints in one commit is NOT here, because ``create_all``
# handles that correctly on its own the first time it sees that table.
# ---------------------------------------------------------------------------

MIGRATIONS: tuple[MigrationStep, ...] = (
    # --- ledger/schema.py-native tables: always exist once create_schema's
    # own metadata.create_all() has run, regardless of whether ingestion/
    # webapp were ever imported. ---
    MigrationStep(
        id="consignment_sales_reimbursed_journal_entry_id",
        description=(
            "consignment_sales.reimbursed_journal_entry_id (nullable FK to "
            "journal_entries) — appended in milestone 3 (ingestion/schema.py) "
            "onto milestone 2's already-existing consignment_sales table; "
            "lets auto-match rule (d) tell 'confirmed+posted but not yet "
            "reimbursed' apart from 'already reimbursed'."
        ),
        table="consignment_sales",
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'consignment_sales' AND column_name='reimbursed_journal_entry_id'"
        ),
        apply_sql=(
            "ALTER TABLE consignment_sales ADD COLUMN IF NOT EXISTS "
            "reimbursed_journal_entry_id INTEGER REFERENCES journal_entries(id)",
        ),
    ),
    MigrationStep(
        id="payoneer_withdrawals_bridging_landing_reconciled_review_queue_id",
        description=(
            "payoneer_withdrawals.bridging_landing_reconciled_review_queue_id "
            "(nullable FK to review_queue) — replaces the original, retired "
            "bridging_to_main_journal_entry_id column (2026-09-01 Fix, see "
            "ingestion/schema.py's SUPERSEDED note on the Bridging Account's "
            "real, non-pass-through business model). The retired column is "
            "deliberately left in place on any database that still has it — "
            "dropping columns is out of this module's purely-additive scope."
        ),
        table="payoneer_withdrawals",
        requires_tables=("review_queue",),
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'payoneer_withdrawals' AND column_name="
            "'bridging_landing_reconciled_review_queue_id'"
        ),
        apply_sql=(
            # Explicit constraint name, matching the SQLAlchemy Table
            # definition's ForeignKey(..., use_alter=True,
            # name="fk_payoneer_withdrawals_landing_review_queue") in
            # ingestion/schema.py EXACTLY. Required, not cosmetic: SQLAlchemy's
            # own drop_all() (used by the test suite's drop_schema()) issues
            # an explicit "ALTER TABLE ... DROP CONSTRAINT
            # fk_payoneer_withdrawals_landing_review_queue" for this specific
            # FK (it has to — this is one half of a genuine two-table FK
            # cycle with review_queue, which use_alter exists to break). An
            # auto-generated Postgres constraint name here would leave a
            # database migrated by this module unable to have its schema
            # dropped/recreated by SQLAlchemy's own tooling — confirmed by a
            # real failure during this fix's own test-suite verification.
            "ALTER TABLE payoneer_withdrawals ADD COLUMN IF NOT EXISTS "
            "bridging_landing_reconciled_review_queue_id INTEGER "
            "CONSTRAINT fk_payoneer_withdrawals_landing_review_queue "
            "REFERENCES review_queue(id)",
        ),
    ),
    MigrationStep(
        id="consignment_sales_consignor_item_ref_unique_index",
        description=(
            "ux_consignment_sales_consignor_item_ref — a real DB-level unique "
            "index (QA BUG FIX, 2026-09) closing a gap where the only guard "
            "against a duplicate consignment_sales row was an app-layer "
            "SELECT-before-insert, unsafe against two concurrent syncs."
        ),
        table="consignment_sales",
        already_applied_check=(
            "SELECT 1 FROM pg_indexes WHERE indexname="
            "'ux_consignment_sales_consignor_item_ref'"
        ),
        apply_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ux_consignment_sales_consignor_item_ref "
            "ON consignment_sales (consignor_item_ref)",
        ),
    ),
    MigrationStep(
        id="ebay_accounts_drive_folder_name",
        description=(
            "ebay_accounts.drive_folder_name (nullable) — added 2026-09-02 to "
            "close the Sync Now 'looks in the wrong Drive folder' gap: the "
            "route was deriving the folder name from the display name "
            "instead of storing the real Drive folder name explicitly."
        ),
        table="ebay_accounts",
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'ebay_accounts' AND column_name='drive_folder_name'"
        ),
        apply_sql=(
            "ALTER TABLE ebay_accounts ADD COLUMN IF NOT EXISTS "
            "drive_folder_name TEXT",
        ),
    ),
    MigrationStep(
        id="wallet_groups_drive_folder_name",
        description=(
            "wallet_groups.drive_folder_name (nullable) — same change/reason "
            "as ebay_accounts.drive_folder_name above, for the wallet-group's "
            "own upload folder."
        ),
        table="wallet_groups",
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'wallet_groups' AND column_name='drive_folder_name'"
        ),
        apply_sql=(
            "ALTER TABLE wallet_groups ADD COLUMN IF NOT EXISTS "
            "drive_folder_name TEXT",
        ),
    ),
    MigrationStep(
        id="fx_revaluations_wallet_group_period_unique_index",
        description=(
            "ux_fx_revaluations_wallet_group_period — a real DB-level unique "
            "index (milestone 5, see scheduling/fx_revaluation.py) closing "
            "the same class of gap already fixed for consignment_sales/"
            "invoices above: the only guard against double-posting a "
            "wallet-group's month-end unrealized FX revaluation was an "
            "app-layer SELECT-before-INSERT, unsafe against two concurrent "
            "job runs racing each other."
        ),
        table="fx_revaluations",
        already_applied_check=(
            "SELECT 1 FROM pg_indexes WHERE indexname="
            "'ux_fx_revaluations_wallet_group_period'"
        ),
        apply_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ux_fx_revaluations_wallet_group_period "
            "ON fx_revaluations (wallet_group_id, period_month)",
        ),
    ),
    # --- ingestion/schema.py-owned tables: only exist once ingestion.schema
    # has been imported and provisioned in this database. Each step below
    # is skipped (not an error) on a milestone-2-only database. ---
    MigrationStep(
        id="review_queue_linked_payoneer_withdrawal_id",
        description=(
            "review_queue.linked_payoneer_withdrawal_id (nullable FK to "
            "payoneer_withdrawals) — 2026-09-01 Fix, traceability link for an "
            "'internal_transfer_landing' row back to the withdrawal it "
            "reconciles against; added onto the already-existing review_queue "
            "table from milestone 3's original commit."
        ),
        table="review_queue",
        requires_tables=("payoneer_withdrawals",),
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'review_queue' AND column_name='linked_payoneer_withdrawal_id'"
        ),
        apply_sql=(
            "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS "
            "linked_payoneer_withdrawal_id INTEGER "
            "REFERENCES payoneer_withdrawals(id)",
        ),
    ),
    MigrationStep(
        id="review_queue_paired_review_queue_id",
        description=(
            "review_queue.paired_review_queue_id (nullable, self-referencing "
            "FK) — 2026-09-01 Fix, a 'claimed' guard preventing two candidate "
            "rows from both posting a transfer for the same real counterpart."
        ),
        table="review_queue",
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'review_queue' AND column_name='paired_review_queue_id'"
        ),
        apply_sql=(
            # Same explicit-constraint-name requirement as the
            # payoneer_withdrawals step above, for the same reason: this is a
            # self-referencing FK (use_alter=True,
            # name="fk_review_queue_paired_review_queue_id" in
            # ingestion/schema.py) — SQLAlchemy's drop_all() explicitly drops
            # it by that exact name.
            "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS "
            "paired_review_queue_id INTEGER "
            "CONSTRAINT fk_review_queue_paired_review_queue_id "
            "REFERENCES review_queue(id)",
        ),
    ),
    MigrationStep(
        id="invoices_drive_file_id_unique_index",
        description=(
            "ux_invoices_drive_file_id (partial unique index, WHERE "
            "drive_file_id IS NOT NULL) — QA BUG FIX, 2026-09, same "
            "double-insert-guard gap/fix as consignment_sales' index above, "
            "added onto the already-existing invoices table."
        ),
        table="invoices",
        already_applied_check=(
            "SELECT 1 FROM pg_indexes WHERE indexname='ux_invoices_drive_file_id'"
        ),
        apply_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_drive_file_id "
            "ON invoices (drive_file_id) WHERE drive_file_id IS NOT NULL",
        ),
    ),
    MigrationStep(
        id="invoices_purpose_check_widen",
        description=(
            "ck_invoices_purpose widened to add 'general_operating_expense' "
            "(CLAUDE.md, 2026-09-01) — the invoices table's purpose CHECK "
            "originally allowed only 'cogs_purchase'/'consignment_purchase'. "
            "DROP+ADD is unconditionally re-run (idempotent by construction, "
            "not gated on an already-applied check — see MigrationStep's "
            "docstring on why constraint text-comparison is skipped)."
        ),
        table="invoices",
        apply_sql=(
            "ALTER TABLE invoices DROP CONSTRAINT IF EXISTS ck_invoices_purpose",
            "ALTER TABLE invoices ADD CONSTRAINT ck_invoices_purpose CHECK "
            "(purpose IS NULL OR purpose IN ('cogs_purchase',"
            "'consignment_purchase','general_operating_expense'))",
        ),
    ),
    MigrationStep(
        id="review_queue_category_check_widen",
        description=(
            "ck_review_queue_category widened to its current full value set "
            "(originally missing 'internal_transfer_landing', added "
            "2026-09-01; 'interest_income', added 2026-09-02; "
            "'contract_labor', added 2026-09-05 for the new CONTRACT_LABOR "
            "expense account; 'shipping_cost', added 2026-09-09 for the "
            "confirmed Kurasi shipping-vendor keyword rule and dedicated "
            "SHIPPING_COST posting path; 'payroll' / "
            "'employee_loan_disbursement', added 2026-09-10 for the new "
            "PAYROLL posting path and the new EMPLOYEE_LOAN_RECEIVABLE asset "
            "account; and 'item_purchase' / 'inbound_shipping' / "
            "'item_purchase_and_inbound_shipping', added 2026-09-10 as more "
            "specific COGS sub-labels (all three still post to the existing "
            "COGS account — a labeling/traceability improvement, not a new "
            "expense type; 'cogs_purchase' itself is kept, unchanged) — see "
            "ingestion/matching.py's _post_one_row) — brings the constraint "
            "to whatever the LATEST code defines in one step, regardless of "
            "which of those historical widenings a given database happens "
            "to be missing."
        ),
        table="review_queue",
        apply_sql=(
            "ALTER TABLE review_queue DROP CONSTRAINT IF EXISTS ck_review_queue_category",
            "ALTER TABLE review_queue ADD CONSTRAINT ck_review_queue_category CHECK "
            "(category IS NULL OR category IN ('revenue_settlement','cogs_purchase',"
            "'consignment_payout','internal_transfer','internal_transfer_landing',"
            "'operating_expense','owners_draw','owners_contribution',"
            "'interest_income','contract_labor','shipping_cost','payroll',"
            "'employee_loan_disbursement','item_purchase','inbound_shipping',"
            "'item_purchase_and_inbound_shipping','other'))",
        ),
    ),
    MigrationStep(
        id="journal_entries_source_type_check_widen",
        description=(
            "ck_journal_entries_source_type widened to add 'opening_balance' "
            "(2026-09-03, see ledger/posting.py's post_opening_balance — a "
            "one-time entry recording a wallet/bank account's real balance "
            "as of just before ledger-tracking began, booked to Owner's "
            "Capital; closes the negative-Payoneer-balance gap described in "
            "CLAUDE.md's Definition of done). DROP+ADD unconditionally "
            "re-run, same pattern as the invoices/review_queue CHECK "
            "-widening steps above."
        ),
        table="journal_entries",
        apply_sql=(
            "ALTER TABLE journal_entries DROP CONSTRAINT IF EXISTS ck_journal_entries_source_type",
            "ALTER TABLE journal_entries ADD CONSTRAINT ck_journal_entries_source_type CHECK "
            "(source_type IN ('ebay_sale','ebay_refund','cogs_purchase','consignment_sale',"
            "'consignment_payout','inter_account_transfer','payoneer_withdrawal',"
            "'fx_revaluation','owner_contribution','owner_draw','bank_other',"
            "'opening_balance'))",
        ),
    ),
    MigrationStep(
        id="account_types_contract_labor",
        description=(
            "account_types row for CONTRACT_LABOR (2026-09-05) — a new "
            "Operating Expenses line for the outside IT contractor paid "
            "per-listing to create eBay listings (see ledger/chart_of_"
            "accounts.py's inline note). account_types.code has a UNIQUE "
            "constraint and ledger.seed.seed_account_types is a plain "
            "INSERT with no upsert guard, so a brand-new account_type added "
            "to the Python catalog after a database was already seeded "
            "needs an explicit, idempotent INSERT here — the same class of "
            "gap this whole module exists to close for columns/indexes/"
            "CHECK constraints, just for one seed-catalog row instead. Uses "
            "INSERT ... WHERE NOT EXISTS (rather than ON CONFLICT DO "
            "NOTHING) so it works even if a target database's account_types "
            "table predates the UNIQUE constraint on code for some reason."
        ),
        table="account_types",
        already_applied_check=(
            "SELECT 1 FROM account_types WHERE code = 'CONTRACT_LABOR'"
        ),
        apply_sql=(
            "INSERT INTO account_types (code, name, statement_section, "
            "normal_balance, scope_kind, is_contra) "
            "SELECT 'CONTRACT_LABOR', 'Contract Labor', 'opex', 'debit', "
            "'consolidated', false "
            "WHERE NOT EXISTS (SELECT 1 FROM account_types WHERE code = "
            "'CONTRACT_LABOR')",
        ),
    ),
    # NOTE: this step only creates the account_types CATALOG row (the GL
    # line item concept). The actual postable `accounts` row (the
    # consolidated singleton instance CONTRACT_LABOR needs before anything
    # can post to it) is deliberately NOT a MIGRATIONS step — unlike
    # account_types/CHECK-constraint/column changes, `accounts` rows are
    # normal SEED DATA (ledger.seed._consolidated_singletons's job, via
    # ledger.entities.create_account, which is a plain INSERT with no
    # ON CONFLICT guard). Inserting it here too would fire automatically
    # inside ledger.schema.create_schema()'s own run_migrations() call on
    # EVERY fresh test schema, colliding with that plain INSERT the moment
    # a test's seed_prototype_topology/seed_full_topology tries to create
    # the exact same consolidated singleton. See scripts/
    # ensure_contract_labor_account.py for the one-off, idempotent
    # real-database equivalent instead.
    MigrationStep(
        id="opening_balances_account_id_unique_index",
        description=(
            "ux_opening_balances_account_id — real DB-level unique index "
            "enforcing at most one opening_balance entry per account (see "
            "ledger/posting.py's post_opening_balance and ledger/schema.py's "
            "opening_balances table). opening_balances is a brand-new table "
            "introduced in this same change with this index already built "
            "in, so per this module's own stated policy (see module "
            "docstring) create_all() alone is sufficient on any database "
            "that doesn't have the table yet — this step is additional "
            "defense-in-depth only, for a database that somehow already has "
            "the table without the index (same caution already applied to "
            "fx_revaluations' own unique index above)."
        ),
        table="opening_balances",
        already_applied_check=(
            "SELECT 1 FROM pg_indexes WHERE indexname='ux_opening_balances_account_id'"
        ),
        apply_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_opening_balances_account_id "
            "ON opening_balances (account_id)",
        ),
    ),
    MigrationStep(
        id="account_types_other_income",
        description=(
            "account_types row for OTHER_INCOME (2026-09-05) — a new Other "
            "Income/Expense line, the inflow-side counterpart to GENERAL_OPEX, "
            "backing a positive (inflow) 'other'-labeled review_queue row (see "
            "ledger/chart_of_accounts.py's inline note and "
            "ingestion.matching._post_one_row's 'other' branch — the historical "
            "bad entry, journal_entry_id=917 / review_queue.id=321, is the "
            "concrete real case this closes). Same exact pattern/reasoning as "
            "'account_types_contract_labor' above: account_types.code has a "
            "UNIQUE constraint and ledger.seed.seed_account_types is a plain "
            "INSERT with no upsert guard, so a brand-new account_type added to "
            "the Python catalog after a database was already seeded needs an "
            "explicit, idempotent INSERT here."
        ),
        table="account_types",
        already_applied_check=(
            "SELECT 1 FROM account_types WHERE code = 'OTHER_INCOME'"
        ),
        apply_sql=(
            "INSERT INTO account_types (code, name, statement_section, "
            "normal_balance, scope_kind, is_contra) "
            "SELECT 'OTHER_INCOME', 'Other Income', 'other_income_expense', "
            "'credit', 'consolidated', false "
            "WHERE NOT EXISTS (SELECT 1 FROM account_types WHERE code = "
            "'OTHER_INCOME')",
        ),
    ),
    # NOTE: same as the account_types_contract_labor step above — this only
    # creates the account_types CATALOG row. The actual postable `accounts`
    # row (the consolidated singleton instance OTHER_INCOME needs before
    # anything can post to it) is deliberately NOT a MIGRATIONS step, for the
    # identical reason documented there (colliding with
    # ledger.seed._consolidated_singletons's plain INSERT inside every fresh
    # test schema's create_schema() call). See scripts/
    # ensure_other_income_account.py for the one-off, idempotent real
    # -database equivalent instead.
    MigrationStep(
        id="account_types_employee_loan_receivable",
        description=(
            "account_types row for EMPLOYEE_LOAN_RECEIVABLE (2026-09-10) — a "
            "new Assets line for no-interest loans the company gives "
            "employees, repaid via salary deduction (see ledger/chart_of_"
            "accounts.py's inline note; the real Fariz Pradana loan, Rp "
            "27,000,000 disbursed 2026-08-17, is the concrete real case this "
            "backs). Same exact pattern/reasoning as "
            "'account_types_contract_labor'/'account_types_other_income' "
            "above: account_types.code has a UNIQUE constraint and "
            "ledger.seed.seed_account_types is a plain INSERT with no "
            "upsert guard, so a brand-new account_type added to the Python "
            "catalog after a database was already seeded needs an explicit, "
            "idempotent INSERT here."
        ),
        table="account_types",
        already_applied_check=(
            "SELECT 1 FROM account_types WHERE code = 'EMPLOYEE_LOAN_RECEIVABLE'"
        ),
        apply_sql=(
            "INSERT INTO account_types (code, name, statement_section, "
            "normal_balance, scope_kind, is_contra) "
            "SELECT 'EMPLOYEE_LOAN_RECEIVABLE', 'Employee Loan Receivable', "
            "'asset', 'debit', 'consolidated', false "
            "WHERE NOT EXISTS (SELECT 1 FROM account_types WHERE code = "
            "'EMPLOYEE_LOAN_RECEIVABLE')",
        ),
    ),
    # NOTE: same as the account_types_contract_labor/account_types_other_income
    # steps above — this only creates the account_types CATALOG row. The
    # actual postable `accounts` row (the consolidated singleton instance
    # EMPLOYEE_LOAN_RECEIVABLE needs before anything can post to it) is
    # deliberately NOT a MIGRATIONS step, for the identical reason documented
    # there. See scripts/ensure_employee_loan_receivable_account.py for the
    # one-off, idempotent real-database equivalent instead.
    MigrationStep(
        id="review_queue_loan_repayment_amount_idr",
        description=(
            "review_queue.loan_repayment_amount_idr (nullable) — 2026-09-10, "
            "backs the 'payroll' category's optional embedded employee-loan "
            "-repayment split (see ingestion.matching._post_one_row and "
            "ledger.posting.post_payroll_with_loan_repayment). When a human "
            "reviewing a Payroll bank line also specifies a repayment amount "
            "here, the row posts as a 3-line entry (gross Payroll expense / "
            "credit EMPLOYEE_LOAN_RECEIVABLE for the repayment / credit the "
            "paying account for the actual net transfer) instead of a flat "
            "2-line expense. NULL (the overwhelming default) means a plain "
            "Payroll line with no embedded loan deduction. The employee this "
            "repayment applies to is identified via the row's existing, "
            "already-generic ``consignor_item_ref`` field (same field reused "
            "for the employee-loan-disbursement category's employee "
            "reference) — no separate employee-name column needed."
        ),
        table="review_queue",
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'review_queue' AND column_name='loan_repayment_amount_idr'"
        ),
        apply_sql=(
            "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS "
            "loan_repayment_amount_idr NUMERIC(20,2)",
        ),
    ),
    MigrationStep(
        id="review_queue_missing_reference_reason",
        description=(
            "review_queue.missing_reference_reason (nullable) — 2026-09-10, "
            "QA-found gap fix: backs ingestion.matching.post_pending_rows' "
            "new employee-reference guard (see _missing_employee_ref_reason) "
            "— an 'employee_loan_disbursement' row (always) or a 'payroll' "
            "row with a loan_repayment_amount_idr set (only then) with a "
            "blank consignor_item_ref is never posted with a silently "
            "substituted placeholder reference; the reason is recorded here "
            "instead, for a human to see and fill in the reference. Same "
            "pattern as sign_mismatch_reason above."
        ),
        table="review_queue",
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'review_queue' AND column_name='missing_reference_reason'"
        ),
        apply_sql=(
            "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS "
            "missing_reference_reason TEXT",
        ),
    ),
    MigrationStep(
        id="review_queue_sign_mismatch_reason",
        description=(
            "review_queue.sign_mismatch_reason (nullable) — 2026-09-05 Fix, "
            "backs ingestion.matching.post_pending_rows' new sign-vs-category "
            "directional guard (a directional category — cogs_purchase/"
            "operating_expense/contract_labor/consignment_payout/owners_draw/"
            "owners_contribution/revenue_settlement — paired with a raw "
            "amount_idr of the wrong sign is never posted; the reason is "
            "recorded here instead, for a human to see and re-classify). "
            "Found against a real historical bad entry, journal_entry_id=917 "
            "/ review_queue.id=321 — see ingestion/schema.py's inline note."
        ),
        table="review_queue",
        already_applied_check=(
            "SELECT 1 FROM information_schema.columns WHERE table_name="
            "'review_queue' AND column_name='sign_mismatch_reason'"
        ),
        apply_sql=(
            "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS "
            "sign_mismatch_reason TEXT",
        ),
    ),
    MigrationStep(
        id="reconciliation_checks_source_document_fk",
        description=(
            "reconciliation_checks.source_document_id gets its real FK to "
            "source_documents (ingestion/schema.py) — deliberately NOT "
            "declared inline on the ledger.schema.py table definition (see "
            "that table's own comment) so ledger.schema.create_schema() "
            "stays safe to call standalone (milestone-2-only use, before "
            "ingestion.schema has ever been imported) — same cross-schema "
            "-FK-via-migration precedent as payoneer_withdrawals."
            "bridging_landing_reconciled_review_queue_id above. DROP+ADD "
            "unconditionally re-run (Postgres has no 'ADD CONSTRAINT IF "
            "NOT EXISTS'), same idempotency pattern as the CHECK "
            "-constraint-widening steps above."
        ),
        table="reconciliation_checks",
        requires_tables=("source_documents",),
        apply_sql=(
            "ALTER TABLE reconciliation_checks DROP CONSTRAINT IF EXISTS "
            "fk_reconciliation_checks_source_document",
            "ALTER TABLE reconciliation_checks ADD CONSTRAINT "
            "fk_reconciliation_checks_source_document FOREIGN KEY "
            "(source_document_id) REFERENCES source_documents(id)",
        ),
    ),
    MigrationStep(
        id="bank_keyword_rules_category_check_widen",
        description=(
            "ck_bank_keyword_rules_category widened to add 'interest_income' "
            "(2026-09-02, backs the BUNGA/PAJAK BUNGA auto-match keyword "
            "rules) and 'shipping_cost' (2026-09-09, backs the new KURASI "
            "auto-match keyword rule — see ingestion/seed.py's "
            "BANK_KEYWORD_RULES and CLAUDE.md's confirmed Kurasi "
            "shipping-vendor fact) — bank_keyword_rules already existed "
            "(milestone 3 follow-up commit) before either value was added."
        ),
        table="bank_keyword_rules",
        apply_sql=(
            "ALTER TABLE bank_keyword_rules DROP CONSTRAINT IF EXISTS "
            "ck_bank_keyword_rules_category",
            "ALTER TABLE bank_keyword_rules ADD CONSTRAINT "
            "ck_bank_keyword_rules_category CHECK (category IN "
            "('revenue_settlement','cogs_purchase','consignment_payout',"
            "'internal_transfer','operating_expense','owners_draw',"
            "'owners_contribution','interest_income','shipping_cost','other'))",
        ),
    ),
)


@dataclasses.dataclass(frozen=True)
class MigrationResult:
    id: str
    status: str  # 'already_present' | 'applied' | 'ensured' | 'skipped_table_missing'
    detail: str


def _table_exists(conn, table_name: str) -> bool:
    return conn.execute(text("SELECT to_regclass(:t)"), {"t": table_name}).scalar() is not None


def run_migrations(engine: Engine) -> list[MigrationResult]:
    """Bring an existing database's schema up to date with every additive
    change audited in ``MIGRATIONS`` above, regardless of what state it's
    currently in. Postgres only (same guard ``ledger.schema.create_schema``
    already applies to its own trigger installation — this project has never
    targeted another database engine for anything beyond dialect detection).

    Safe to call on a fresh database (every step no-ops against the schema
    ``metadata.create_all()`` already built from the CURRENT Python table
    definitions), a partially-provisioned one (milestone-2-only, or missing
    one or more of the specific historical changes below), or a fully
    up-to-date one (the expected outcome against the real ``noctrowl``
    database as of this session — see this module's docstring's action log).
    """
    if engine.dialect.name != "postgresql":
        return []

    results: list[MigrationResult] = []
    with engine.begin() as conn:
        for step in MIGRATIONS:
            tables_needed = (step.table, *step.requires_tables)
            missing = [t for t in tables_needed if not _table_exists(conn, t)]
            if missing:
                results.append(
                    MigrationResult(
                        id=step.id,
                        status="skipped_table_missing",
                        detail=f"table(s) not yet provisioned: {', '.join(missing)}",
                    )
                )
                continue

            already_applied = False
            if step.already_applied_check is not None:
                already_applied = conn.execute(text(step.already_applied_check)).first() is not None

            for stmt in step.apply_sql:
                conn.execute(text(stmt))

            if step.already_applied_check is None:
                results.append(MigrationResult(id=step.id, status="ensured", detail=step.description))
            elif already_applied:
                results.append(
                    MigrationResult(id=step.id, status="already_present", detail=step.description)
                )
            else:
                results.append(MigrationResult(id=step.id, status="applied", detail=step.description))

    return results
