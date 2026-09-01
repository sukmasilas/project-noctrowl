# Milestone 4 — Web App UI Implementation: Design (Phase A)

Status: **proposal, not yet implemented.** Per the two-phase build pattern used in
milestones 2 and 3, this document is Phase A only — framework choice + architecture
plan, for review before any app code is written. Builds exactly what
`docs/design/ui-ux-design.md` and `docs/design/mockup.html` (milestone 1, approved)
specify, against real Postgres data from `ledger/` and `ingestion/` (milestones 2–3,
both QA-approved).

This document re-reads CLAUDE.md as of 2026-09-01, which has moved since milestone 1
was approved (a 4th category "Toys & Collectibles", a second experimental
consignment payout model, invoice `Purpose` expanded to three values, a still-open
Mandiri bridging-account parser gap). None of those changes affect this document's
recommendations directly — they're `ledger`/`ingestion` data-layer facts the web
layer reads as-is — but §3 and §5 call out the specific spots where the UI needs to
reflect them (e.g. the payout-tiers screen's manual-confirmation framing, the
three-state invoice `Purpose` dropdown) so nothing here is a stale read of the
milestone-1 mockup.

## 1. Framework recommendation

**Recommendation: Flask, with Jinja2 server-rendered templates, no separate frontend
build step.**

Alternatives briefly considered:

- **FastAPI.** Excellent for a JSON API + separate frontend (React/Vue), and its
  async support and Pydantic validation are real strengths — but this prototype has
  no JSON API consumer other than its own server-rendered pages, and Pydantic models
  would just duplicate validation the SQLAlchemy Core schema (with its CHECK
  constraints and triggers) already enforces at the DB layer. Async buys nothing
  here: every request does one or two synchronous Postgres queries on a 1-vCPU
  droplet, there's no concurrent I/O-bound workload to overlap. Picking FastAPI would
  mean either building a separate SPA frontend (real added complexity — a build
  toolchain, a second deploy artifact, CORS/session handling across two origins,
  on a 1GB RAM droplet that's already tight) or using it as a template-serving
  framework and getting none of its actual advantages.
- **Django.** Full-featured, but the "full-featured" part is exactly the problem:
  Django's ORM would either sit unused (fighting the existing SQLAlchemy Core
  schema, becoming a second, redundant data-access layer under CLAUDE.md's own
  data-access-layer rule below) or get bridged against the existing tables
  awkwardly via `managed = False` models, which is more integration tax than value
  for 3-4 screens. Django's admin panel, auth system (roles/permissions), and
  migrations framework are all more than this single-shared-login, single-tenant
  prototype needs, and the RAM footprint of a full Django install is heavier than
  this droplet wants to carry alongside Postgres and OCR workloads.
- **Flask** (chosen): a thin routing + templating layer, nothing else opinionated.
  It imposes no ORM (so `ledger.db.get_engine()` + SQLAlchemy Core `select()` calls
  plug in directly, no adapter layer needed — see §2), no async runtime, no bundler.
  Its memory footprint is small (a few MB for the framework itself; the real memory
  cost on this droplet is Postgres + OCR, not the web layer, whichever framework is
  picked). Session-based login (Flask's built-in signed-cookie sessions) is a
  one-line fit for "basic username/password, single shared login, no roles" (§4).
  Jinja2 templates let the existing mockup.html be adapted directly rather than
  rewritten as JSON-consuming components — it's already static HTML with the right
  structure and copy, and it doesn't need to become dynamic client-side rendering to
  satisfy CLAUDE.md's "reports are read-only computed views" scope (no export, no
  live-editing outside the two designated screens).

This is a genuinely close call against plain WSGI-with-no-framework, but Flask's
routing/templating/session conveniences are worth the (small, well-understood,
single-purpose) dependency, and it's the most common "boring, small, well-supported"
choice for exactly this shape of app in Python.

**Dependency additions** (for QA's supply-chain review in Phase B): `Flask` (latest
stable 3.x), `Werkzeug` (Flask's dependency, for `generate_password_hash` /
`check_password_hash` — see §4). No ORM, no template-compiler beyond Jinja2 (ships
with Flask), no separate CSS/JS build tooling — plain CSS + minimal vanilla JS for
the two inline-editor interactions (Review Queue row expand, Documents invoice row
expand), matching what `mockup.html` already sketches statically.

## 2. Integration with `ledger` / `ingestion` — one data-access layer, not two

Both `ledger/db.py` (`get_engine()`, reads `DATABASE_URL`, Postgres-only, no
fallback) and the full SQLAlchemy Core table definitions in `ledger/schema.py` +
`ingestion/schema.py` already exist and are the single source of truth for both
schema and querying. The web app does **not** get its own engine/connection helper,
its own model layer, or a duplicate `DATABASE_URL` reader:

- The Flask app factory calls `ledger.db.get_engine()` once at startup (same
  no-hardcoded-connection-string rule as everywhere else — QA already blocks on
  this).
- Every request that needs data opens a connection via `engine.connect()` (Flask's
  request-scoped `g` object holds it per-request, closed in a `teardown_appcontext`
  hook — the standard Flask pattern for a bare SQLAlchemy Core setup, no
  Flask-SQLAlchemy extension needed since there's no ORM to wire up).
- All queries are plain SQLAlchemy Core `select()`/`join()` statements against the
  existing `Table` objects imported from `ledger.schema` / `ingestion.schema` —
  the same objects `ledger/posting.py`, `ledger/entities.py`, and
  `ingestion/matching.py` already use. A new `webapp/queries.py` (or per-screen
  query modules — see §3) module holds these `select()` builders; it imports table
  objects, it does not redefine them.
- Any write path exposed through the UI (Review Queue row labeling, Consignor
  Payout Tiers edits) calls into existing `ledger`/`ingestion` functions or does a
  plain, narrowly-scoped `UPDATE` against the specific table/columns the milestone-1
  design specifies (e.g. `review_queue.category`, `.consignor_item_ref`,
  `.labeled_at`) — never a new posting code path. Actually posting a labeled row to
  the ledger stays exactly where it is: `ingestion.matching.post_pending_rows()`,
  called by `ingestion.sync.run_sync_for_period()` (via the Sync Now trigger, §6),
  never inline in a Flask route. This preserves the "Save doesn't post immediately"
  design rule (`ui-ux-design.md` Screen 1) structurally, not just as UI copy.

No second data-access layer, no repository/service classes wrapping the existing
functions unnecessarily — Flask routes call `ledger`/`ingestion` functions and
`select()` queries directly. This is a deliberate minimalism call for a
single-developer, 3–4-screen prototype; revisit only if route logic actually starts
duplicating itself across screens.

## 3. Screen-by-screen plan

For each screen: what it reads, and (where nothing computes it yet) the actual
query/aggregation shape needed.

### Login (Screen -1)
Reads: nothing from the DB. Compares submitted username/password against
`APP_LOGIN_USERNAME`/`APP_LOGIN_PASSWORD` env vars (see §4). No `users` table for
this prototype — a single shared credential pair, per CLAUDE.md.

### Documents (Screen 0)
- **Ingestion status cards** (eBay Sales Export / Bank Statements / Payoneer
  Exports, per selected account+wallet-group+month): read
  `ingestion.schema.source_documents`, one row per `(document_type, period_month,
  ebay_account_id | wallet_group_id)` per the existing partial-unique indexes.
  `ingested_at IS NOT NULL` → Uploaded (green); `ingested_at IS NULL` and today ≤
  that period's H+7 deadline → Not yet uploaded (blue); `ingested_at IS NULL` and
  today > H+7 → Missing (red). The H+7 deadline computation
  (`period_month`'s last day + 7 days) is a small pure function in the new
  `webapp/finalization.py` module (see the Provisional/Final section below) —
  reused by both this screen and the status badge, not duplicated.
- **Sync Now button**: POSTs to a route that calls
  `ingestion.sync.run_sync_for_period()` for the selected account/wallet-group/month
  (see §6 for cooldown handling), then re-renders the same page with fresh card
  state.
- **Invoice table** (consolidated, not account-filtered — per the design doc):
  `SELECT * FROM invoices WHERE period_month = :month ORDER BY extracted_date`
  against `ingestion.schema.invoices`. Status column maps directly from
  `invoices.status` (`'parsed'` / `'needs_confirmation'`). Row expand/edit posts an
  `UPDATE invoices SET ... , confirmed_at = now()` for the four editable fields —
  **`Purpose` is a three-value dropdown** (`cogs_purchase` / `consignment_purchase`
  / `general_operating_expense`), matching CLAUDE.md's 2026-09-01 update, not the
  two-value version the original milestone-1 mockup copy describes — the mockup's
  copy is stale on this one specific point and Phase B implementation should follow
  CLAUDE.md over the older mockup text.
- **"Matched" column** (has a bank/Payoneer line matched this invoice yet):
  `EXISTS (SELECT 1 FROM review_queue WHERE linked_invoice_id = invoices.id)` — a
  correlated subquery/`LEFT JOIN`, per `ingestion/schema.py`'s documented one-direction
  linkage (`review_queue.linked_invoice_id`, no reverse column).
- **Unmatched COGS activity callout**: `review_queue` rows where
  `category IN ('cogs_purchase','consignment_payout')` and `linked_invoice_id IS
  NULL`, count + link to Review Queue filtered the same way.

### Review Queue (Screen 1)
- **Summary bar**: `SELECT match_status, ebay_account_id, wallet_group_id,
  date_trunc('month', transaction_date) AS period, COUNT(*) FROM review_queue GROUP
  BY ...` — per-account/month Matched vs. Needs Review counts. (Note: `review_queue`
  rows carry either `ebay_account_id` or `wallet_group_id`, not always both — the
  summary bar groups by whichever is populated, same partial-scoping pattern as
  `source_documents`.)
- **Table**: `SELECT * FROM review_queue WHERE ... ORDER BY transaction_date` with
  the Account/Month/Status/Source filters as `WHERE` clauses. `Posted` column reads
  `posted_at`/`posted_journal_entry_id` directly — three real states (Needs Review /
  labeled-not-yet-posted / Posted) already representable from `labeled_at` +
  `posted_at`, exactly as `ui-ux-design.md`'s "Open items" section anticipated.
- **Row save**: `UPDATE review_queue SET category = :cat, consignor_item_ref = :ref,
  labeled_at = now() WHERE id = :id AND posted_at IS NULL`. The `posted_at IS NULL`
  guard in the `WHERE` clause is a deliberate belt-and-suspenders match to
  `ck_review_queue_no_post_without_category`/CLAUDE.md's "corrections to a posted row
  are out of scope" rule — an already-posted row's category is not editable through
  this route at all (the route returns a 409/flash-error rather than silently
  no-op'ing, so a user doesn't wonder why their edit didn't seem to save). It does
  **not** post — posting only happens via the next `run_sync_for_period()` call
  (Sync Now or, later, milestone 5's scheduler), per the "no separate trigger" rule.

### Consignor Payout Tiers (Screen 1.5, Settings)
- Reads/writes `ledger.schema.consignor_payout_tiers` directly (already seeded with
  the Pasal 3 schedule by `ledger.seed.seed_consignor_payout_tiers`). Editable
  columns: `rate_percent` per row (price range and the `$7,500+` row's
  "manual contact" note are display-only, matching the mockup's "6 fixed rows, no
  add/remove" scope). A plain `UPDATE consignor_payout_tiers SET rate_percent = :r
  WHERE id = :id`.
  - **Framing note (updated for the 2026-08-31/09-01 CLAUDE.md changes):** the tier
    table is a calculation *aid* now, not an auto-apply source — CLAUDE.md's Core
    accounting rules now require **every** consignment payout (tier model or the
    experimental net-of-fees-and-shipping model) to go through manual confirmation
    before posting (`ledger.posting.confirm_consignment_sale` /
    `post_consignment_sale`'s `MissingConfirmedAmountError` guard already enforces
    this at the posting-engine level). This settings screen's own "must never
    silently apply an edited rate retroactively" rule from `ui-ux-design.md` still
    holds and is even more clearly satisfied now: an edited rate only changes what
    number the *lookup* suggests to a human on the next unconfirmed sale, never an
    already-posted `consignment_sales` row's stored `payout_amount_idr`.

### Report views (Screens 2–5)
All four share one Flask blueprint/route family (`/reports/<statement>`) and one
base template with statement tabs, per the mockup. None of these aggregations exist
yet — sketched here as the actual `select()`/aggregation shape each needs, built
against `ledger.schema.journal_lines` joined to `accounts`/`account_types`:

- **Revenue** (per account + consolidated): sum `journal_lines.credit_amount_idr -
  debit_amount_idr` grouped by `account_types.code` for
  `SALES_REVENUE`/`SALES_RETURNS_ALLOWANCES`/`CONSIGNMENT_COMMISSION_INCOME`, joined
  through `journal_entries.period_month = :month`, filtered per-account by joining
  `journal_lines.account_id -> accounts.ebay_account_id = :account` **only for the
  `EBAY_WALLET`-adjacent revenue lines** — in practice, revenue lines don't carry an
  `ebay_account_id` themselves (Sales Revenue is a consolidated account_type), so
  the per-account filter is actually `journal_lines.ebay_order_ref` correlated back
  to which `ebay_account_id` posted that sale. **This is the one open question this
  design surfaces** (see §7) — flagging rather than guessing, since it determines
  whether per-account Revenue needs a join through `ebay_csv_posted_transactions`
  (which does carry `ebay_account_id`) or a denormalized `ebay_account_id` column
  added to `journal_lines` itself.
- **Cash Flow** (per account + consolidated): eBay Wallet line = journal_lines
  joined to the specific `accounts` row for that `ebay_account_id`'s `EBAY_WALLET`
  account_type, summed by movement type (`journal_entries.source_type`). Payoneer
  Wallet/BCA Bridging lines = same shape but joined via `accounts.wallet_group_id`
  for that account's wallet-group, **shown identically for both eBay accounts in a
  shared wallet-group** per the "shared pool" banner rule — the query is literally
  the same wallet-group-scoped query regardless of which of the two paired accounts
  is selected, which is what makes the banner logic (`wallet_groups` row has >1
  active `ebay_accounts`) trivial: `SELECT COUNT(*) FROM ebay_accounts WHERE
  wallet_group_id = :wg AND is_active` — count `>1` shows the banner.
- **Consolidated Cash Flow elimination**: sum `inter_account_transfer`-sourced
  journal lines per wallet-group-pair and net them to zero for display (the ledger
  data itself already balances to zero at the entry level — the "eliminated" note is
  a presentation label on top of a real zero, not a subtraction). Dedup rule: when
  aggregating Payoneer/BCA-Bridging figures across all `ebay_accounts` for
  Consolidated, **group by `wallet_group_id` first, then sum distinct wallet-groups**
  — never `SUM(...) GROUP BY ebay_account_id` then add the per-account rows, which
  is exactly the double-count bug CLAUDE.md warns about.
- **P&L** (consolidated only): standard multi-step aggregation — `SUM` by
  `account_types.statement_section` (`revenue` minus `is_contra` rows, `cogs`,
  `opex`, `other_income_expense`), all `WHERE journal_entries.period_month = :month`,
  no `ebay_account_id`/`wallet_group_id` filter at all (consolidated accounts don't
  carry one). Realized vs. Unrealized FX stay two separate `SUM`s keyed off
  `account_types.code IN ('REALIZED_FX','UNREALIZED_FX')` — never blended into one
  number, matching CLAUDE.md.
- **Equity**: `SUM` by `account_types.code IN
  ('OWNERS_CAPITAL','OWNERS_DRAW','RETAINED_EARNINGS')` for the period, plus a
  running/cumulative ending balance (needs a `period_month <= :month` cumulative sum,
  not just `= :month`, since equity is a balance-sheet-style running total, not a
  period flow like P&L/Cash Flow — flagging this distinction explicitly since it's
  easy to get wrong by reusing the P&L query shape unchanged).
- **Drill-down** (all four reports): every rendered figure links to
  `/reports/drilldown?account_type=...&period=...&scope=...`, which reruns the same
  `WHERE` clause without the aggregation, returning the individual `journal_lines`
  rows (joined to `journal_entries` for date/memo/source_type, and to
  `invoice_journal_links`/`invoices` for COGS/consignment lines specifically — per
  `ui-ux-design.md`'s "drill-down needs to join through to the specific invoice
  record" requirement). One shared query-building helper parameterized by
  account-type-code(s) + scope, called both by the report aggregation and its
  drill-down, so the two can never drift out of sync (the drill-down rows are
  guaranteed to actually sum to the headline figure because they're the same query,
  one aggregated and one not).

## 4. Login gate

Single shared username/password from `APP_LOGIN_USERNAME`/`APP_LOGIN_PASSWORD`
(already scaffolded, blank, in `.env.example`) — no `users` table, no per-user
anything, per CLAUDE.md's explicit "no roles/permissions system needed yet."

Mechanism:
- `APP_LOGIN_PASSWORD` is stored in `.env` as a **pre-hashed** value (Werkzeug's
  `generate_password_hash`, e.g. `scrypt` or `pbkdf2:sha256`), not plaintext — the
  app never compares a plaintext env var directly, it runs
  `check_password_hash(stored_hash, submitted_password)`. A short one-off setup
  script (`scripts/hash_login_password.py`, prints a hash for the user to paste into
  `.env`) covers generating this once — never done inline in the Flask app or logged
  anywhere.
- On successful login, Flask's built-in signed-cookie session
  (`app.secret_key` — a **new** env var, `APP_SECRET_KEY`, random, also never
  hardcoded; add it to `.env.example` in Phase B) sets `session['logged_in'] =
  True`. A `before_request` hook redirects any request without that session flag to
  `/login`, except `/login` itself and static assets.
- No "remember me," no password reset flow, no lockout/rate-limiting on login
  attempts for this prototype scale (single owner, not internet-scanned at any real
  volume yet) — flagging this as a deliberate minimalism call, not an oversight;
  revisit if the droplet's URL ever needs to tolerate genuine hostile traffic.
- Logout: a route that calls `session.clear()`.

This satisfies "gates everything else behind a basic username/password check" with
the smallest mechanism that actually works — no new dependency beyond
Flask/Werkzeug (both already recommended in §1).

## 5. Consignor Payout Tiers screen — CRUD detail

Covered in §3 above. Restating the scope boundary explicitly since it's easy to
over-build: **update-only** on `rate_percent` for the 6 fixed rows (no add/remove
row UI — matches `ui-ux-design.md`'s explicit "not needed for the prototype since
the schedule is fixed at 6 tiers"). No versioning/audit-log table for rate-change
history in this milestone — if that's wanted later (e.g. "what rate was in effect
when sale X was confirmed"), it's answerable in principle from
`consignment_sales.tier_rate_percent`, which already freezes the rate actually used
at confirmation time per-transaction, so a settings-table history log would be
redundant with data already captured. Not building it speculatively.

## 6. Manual Sync Now trigger

- A POST route (`/documents/sync`) calls
  `ingestion.sync.run_sync_for_period(conn, drive_client, ...)` for the currently
  selected account/wallet-group/period, wrapped in `engine.begin()` for one atomic
  transaction (matches how `run_sync_for_period` is already designed to be called —
  see its docstring: "a plain, synchronous, manually-callable function").
- **Cooldown mechanism**: a new tiny table, `webapp_sync_runs` (id, `triggered_at`
  timestamp, `triggered_by` — always the shared login username, for the audit trail
  CLAUDE.md's action-log expectations imply), written once per completed run (start
  timestamp is enough; no need to track "in progress" state across requests for a
  single-user prototype — a second concurrent click while a sync is running is
  already effectively impossible for one person operating one browser tab, and if it
  somehow happened, `run_sync_for_period`'s own idempotent posting guards make a
  double-click harmless rather than a duplicate-post risk). Table lives in a new,
  small `webapp/schema.py` (own `Table`, registered on the *same* shared
  `sqlalchemy.MetaData` object from `ledger.schema`, exactly the additive pattern
  `ingestion/schema.py` already established for milestone 3's tables — not a third,
  disconnected metadata object).
- The button route checks `now() - last_run.triggered_at < COOLDOWN` (a plain
  Python `timedelta`, e.g. 5 minutes — matches CLAUDE.md's "e.g. a few minutes"
  language) before allowing a new run; if within cooldown, returns the disabled
  state + remaining-time message instead of re-running. This is genuinely all
  milestone 4 needs to build here — the **H-15/H+7 automatic window** stays
  explicitly out of scope (milestone 5), this only gates the manual button itself.
- `drive_client`: constructed once at app startup from
  `GOOGLE_APPLICATION_CREDENTIALS`/`GOOGLE_DRIVE_ROOT_FOLDER_ID` env vars (already
  scaffolded), reusing `ingestion.drive_client.DriveClient` exactly as `ingestion`'s
  own tests do — no new Drive-auth code path in the web layer.

## 7. Provisional/Final status computation

This is new logic — CLAUDE.md's Report finalization status section names the two
conditions but nothing computes or renders them yet. Proposed shape, in a new
`webapp/finalization.py` module (pure functions over a `Connection`, no Flask
dependency, so it's independently testable):

```
def review_queue_status(conn, *, ebay_account_id=None, wallet_group_id=None, period_month) -> int:
    # COUNT(*) FROM review_queue WHERE match_status = 'needs_review'
    #   AND transaction_date is within period_month
    #   AND (ebay_account_id = :id OR wallet_group_id = :id, matching the report's scope)
    # Returns the outstanding count (0 = condition 1 satisfied).

def missing_source_documents(conn, *, ebay_account_id=None, wallet_group_id=None, period_month) -> list[str]:
    # For the account/period's SCOPED fixed expectations only:
    #   - per-account report -> ebay_sales_csv (that account) +
    #     payoneer_csv/bank_statement_wallet_group (that account's wallet-group)
    #   - consolidated report -> bank_statement_master, PLUS every active
    #     ebay_account's ebay_sales_csv and every wallet-group's payoneer_csv/
    #     bank_statement_wallet_group (consolidated depends on ALL of them,
    #     not just the master statement — a consolidated report built while an
    #     underlying per-account document is still missing is exactly the
    #     "may be missing transactions entirely" gap CLAUDE.md's condition 2
    #     exists to catch)
    # For each expected (document_type, scope) pair: source_documents row with
    # ingested_at IS NOT NULL? -> satisfied. Otherwise -> included in the
    # returned list (used both for "which document is missing" messaging and
    # as a boolean: non-empty list = condition 2 unsatisfied).

def report_status(conn, *, scope, period_month) -> ReportStatus:
    # scope: {"ebay_account_id": N} | {"wallet_group_id": N} | {"consolidated": True}
    # Combines both checks above into a dataclass:
    #   ReportStatus(final: bool, needs_review_count: int, missing_documents: list[str])
    # final = (needs_review_count == 0) and (missing_documents == [])
```

- The status badge (shared chrome, every screen) calls `report_status()` for
  whatever account/period is currently selected and renders green "Final" or amber
  "Provisional — {needs_review_count} items need review" / "Provisional — {doc}
  not yet received" / both messages joined, per the mockup's copy.
- **Consolidated's dependency on per-account/per-wallet-group documents** (the bullet
  above) is the one place this design goes slightly beyond a literal reading of
  CLAUDE.md's wording ("the master/consolidated bank statement is the equivalent
  fixed expectation at the consolidated level") — CLAUDE.md doesn't explicitly say
  whether Consolidated *also* inherits the per-account/wallet-group expectations or
  only has its own master-statement expectation. I'm proposing the stricter reading
  (inherits both) because the stated *reason* for condition 2 — "the ledger may be
  missing transactions entirely" — applies just as much to a missing per-account eBay
  CSV feeding into Consolidated P&L as to a missing master statement. **Flagging this
  explicitly as a decision Main-agent/the user should confirm before Phase B**,
  rather than silently picking the interpretation — this is exactly the kind of
  money-reporting-correctness ambiguity CLAUDE.md asks Builder to surface rather than
  guess on.
- The H+7 "expected by" deadline function used by both the Documents screen (§3) and
  implicitly by "how overdue is missing" messaging: `expected_by(period_month) =
  (period_month + 1 month).replace(day=1) + relativedelta(day=31, months=0) ...`
  i.e., simply: last calendar day of `period_month`, plus 7 days. A small pure
  function, no external dependency needed beyond `datetime`/`calendar.monthrange`
  (already in the stdlib — no new `python-dateutil` dependency required for this).

## Open questions for Main-agent (flagging rather than guessing, per Builder's brief)

1. **Per-account Revenue attribution join** (§3, Revenue report): `journal_lines`
   for `SALES_REVENUE` don't carry `ebay_account_id` directly (it's a consolidated
   account_type). The only path back to "which eBay account sold this" is via
   `ebay_order_ref` → `ebay_csv_posted_transactions.ebay_account_id`. Confirming this
   is the intended join (rather than, say, adding a denormalized `ebay_account_id`
   column directly to `journal_lines` for convenience) before Phase B, since it's a
   schema-touching decision if the answer is "add the column."
2. **Consolidated report's document-completeness scope** (§7): does Consolidated's
   Provisional/Final gate inherit every per-account/wallet-group document
   expectation, or only the master bank statement's own expectation? Proposed the
   stricter (inherits-all) reading above; want explicit confirmation before building
   it, since it changes what "Consolidated is Final" actually promises the user.
3. **`APP_SECRET_KEY`** (§4): a new env var for Flask's session-signing key, not
   currently in `.env.example`. Confirming it's fine to add (it's not itself a
   credential to an external system, just a locally-generated random signing key,
   but flagging since CLAUDE.md is strict about anything credential-shaped going
   through env vars / secrets file only — this already follows that rule, just
   noting the new var name for visibility).

None of the above block starting Phase B on the rest of the plan (routing structure,
templates adapted from the mockup, the Review Queue/Documents/Payout-Tiers CRUD
routes, the login gate, the Sync Now button) — only the Revenue per-account query
and the Consolidated finalization scope need an answer before those two specific
pieces are implemented.
