# Milestone 3 — manual data ingestion + review queue: design doc

**Status: Phase A design only — not implemented.** Per Main-agent's brief this is a two-phase
task; this document is the deliverable for Phase A. No parser/OCR/matching code has been
written yet. Do not start Phase B until Main-agent has reviewed this doc and the open
questions below.

This extends milestone 2's schema (`ledger/schema.py`) additively — nothing in milestone 2's
existing tables, columns, constraints, or `ledger/posting.py` functions is changed. Every new
table below is a new table; every schema "change" to an existing table is a new nullable
column, added via a fresh migration, never an edit to an existing column/constraint.

Grounded in the real files under `sample-documents/` (read in full for this design):
`ebay-sales-export/Transaction_report_20260701_20260731.csv`,
`payoneer/Payoneer_Transactions_04-2026.csv`,
`payoneer/Payoneer_Confirmation_of_Transfer_4366185623014087.pdf`,
`bank-statements/BCA Bank_1790345891_APR_2026.pdf`, and the three invoice samples.

---

## 0. Headline open questions (read this section first)

These came directly out of studying the real samples and cross-checking them against
CLAUDE.md and the milestone-2 posting engine. None of them block writing this design doc,
but several **do** block starting Phase B safely, because they have money-math or
data-model implications CLAUDE.md doesn't fully resolve. Flagging per CLAUDE.md's own rule
("if the brief is ambiguous... stop and report back rather than guessing — especially on
anything touching money math").

1. **Which real account/statement is the sample bank PDF?** `BCA Bank_1790345891_APR_2026.pdf`
   shows heavy, varied operational activity — many individual inventory-purchase transfers
   (named-recipient payments consistent with buying single TCG cards/watches), a payroll-like
   "gaji + bonus" line, owner-draw-looking transfers, a bank admin fee, interest — **and** a
   single large incoming "KR OTOMATIS" credit whose description references Payoneer. That
   activity mix looks like the **BCA Main Account** (where real operations happen, per
   CLAUDE.md), not the **BCA Bridging Account** (described as "pure pass-through, no real
   operational activity"). The README already flagged this as something to confirm before
   milestone 3. I need Main-agent/the user to confirm: is this sample actually the Master
   Account statement, or the wallet-group's bridging account statement? This changes which
   `source_documents`/`accounts` scope it parses into, and affects question 2 below.
2. **Does a physically separate BCA Bridging Account statement exist for this business, or is
   "BCA Bridging Account" only ever an accounting concept** (Payoneer withdraws straight into
   what the user operates as the Main account)? If there's truly no second statement to
   reconcile against, the Bridging→Main "periodic transfer" leg described in CLAUDE.md's Money
   flow section may never appear as two separate bank lines to auto-match — it could instead
   need to post automatically, in the same step as `post_realized_fx_withdrawal`, without going
   through the review queue at all. See §3 and §6 for how this changes the design either way.
3. **Where does the weekly Kurs Pajak rate come from for booking?** CLAUDE.md requires booking
   every sale at "Indonesia's official weekly tax exchange rate (Kurs Pajak)... published at
   fiskal.kemenkeu.go.id" but nothing in the milestone-3 brief's scope list (Drive, CSV/PDF
   parsing, OCR, matching) mentions fetching or storing that rate. Proposal: a small
   `kurs_pajak_rates(effective_date, rate_idr)` reference table, populated **manually** for the
   prototype's one month (an admin/seed entry, not a scraper) — automated fetching from
   Kemenkeu's site is a new external integration I don't think is in scope for milestone 3 and
   would need its own sign-off. Flagging rather than silently building a scraper.
4. **Which "booking rate" values a Payoneer withdrawal that pools sales booked at different
   weekly Kurs Pajak rates?** `post_realized_fx_withdrawal` takes one `booking_rate_used_idr`,
   but a single withdrawal (e.g. USD 5,000) can be drawn from a Payoneer balance that
   accumulated over several weeks, each at a different Kurs Pajak rate. CLAUDE.md doesn't say
   how to collapse that into one number. Options: (a) weighted-average of the booking rates of
   whatever inflows are still "in" the wallet at withdrawal time (a real cost-basis /
   FIFO-or-average-lot approach — accurate, but needs a running per-wallet-group unwithdrawn
   inflow ledger I haven't designed and isn't obviously in scope), or (b) a simpler placeholder
   — most recent week's Kurs Pajak rate as of the withdrawal date, explicitly documented as an
   approximation. I lean toward (b) for the prototype with an explicit "this is an
   approximation" flag on the entry, but this is a real accounting-accuracy tradeoff, not mine
   to decide unilaterally.
5. **`post_inter_account_transfer` doesn't carry `amount_usd_ref`/`fx_rate_used`.** I need to
   reuse this milestone-2 function for the eBay Wallet → Payoneer Wallet leg (both USD
   accounts — see §3), but the function's `Line()` calls don't pass USD reference fields, so
   that transfer's USD amount and the rate used to value it in IDR would be lost. This looks
   like a genuine milestone-2 gap surfaced by milestone-3 usage, not something to work around
   silently. Proposed fix: add **optional** `amount_usd_ref`/`fx_rate_used` kwargs to
   `post_inter_account_transfer` (backward-compatible — existing IDR-only transfer calls, e.g.
   BCA Bridging → BCA Main, keep working with those left `None`). This is additive, not a
   change to any existing call's behavior, but I'm flagging it explicitly since it touches a
   milestone-2 function's signature.
6. **Invoice `Purpose` may need a third value.** CLAUDE.md's invoice design says Purpose is
   "COGS Purchase / Consignment Purchase" — a closed two-value enum. The real Tokopedia sample
   invoice is for a phone, not inventory (TCG/watch/auto-part/toy stock) — it reads like a
   General Operating Expense (a work device), not COGS. It's possible this sample is just
   generic filler and every *real* invoice will genuinely be COGS or Consignment, but I can't
   assume that. Flagging: should Purpose support a third value (e.g. "Other/General Expense"),
   or is this sample non-representative and the two-value enum is correct as specified?
7. **What does "Consignment Purchase" as an invoice purpose actually correspond to?** Re-reading
   the Business model: the consignor already owns the item, so there's nothing the seller
   "purchases" in a consignment deal — the money movement on the seller's side is *paying the
   consignor their share after a sale* (`post_consignor_reimbursement`). I'm designing on the
   assumption that "Consignment Purchase" invoices are actually proof-of-transfer records for
   *consignor reimbursement payouts*, not inbound stock purchases — see §5. Flagging this
   reading explicitly in case it's wrong.
8. **eBay's "Other fee" rows (Promoted Listings ad fee, Store subscription fee) and Charity
   donation have no obviously-correct account.** They're real debits straight from the eBay
   Wallet, but they aren't the per-transaction Final Value Fee that `EBAY_SELLING_FEES` was
   named for, and they aren't COGS/Shipping/Payroll either. Per CLAUDE.md's own rule ("flag to
   Main-agent if implementation reveals a needed account not listed here, rather than inventing
   one silently"), I'm flagging rather than picking one. My lean: `GENERAL_OPEX` for Promoted
   Listings + Store subscription (platform marketing/subscription cost, not a per-sale fee) —
   but this is a call for Main-agent/the user, not me.
9. **No `Interest Income` / bank-interest account exists in the chart of accounts.** The sample
   bank statement has small `BUNGA` (interest credited) and `PAJAK BUNGA` (tax withheld on that
   interest) lines. Both are immaterial in amount but structurally unaccounted for — there's no
   `other_income_expense` or `revenue` line for bank interest, and no expense line for
   withholding tax on interest. Flagging rather than silently mapping either into an unrelated
   existing account (e.g. jamming it into `GENERAL_OPEX` would misstate opex).
10. **Google Drive service-account scope is unverified.** A `secrets/google-service-account.json`
    file does exist at the path `.env`'s `GOOGLE_APPLICATION_CREDENTIALS` points at (confirmed by
    file-existence check only — I did not open or print it, per the standing rule on secrets
    files). Whether it has the Drive scope/permissions needed to *read* the user's existing
    "Finance & Accounting" folder tree (which the service account didn't create itself) is
    unverified and untested. Live Drive connectivity is explicitly not being tested in Phase A;
    if Phase B implementation finds the credential unusable or under-scoped, I'll stop and
    report rather than guessing a workaround.

---

## 1. Schema additions

All new tables. Column shapes deliberately mirror milestone 2's own conventions (money as
`NUMERIC`, never float; a `scope_kind`-style split via nullable `ebay_account_id` /
`wallet_group_id` wherever something can be per-account, per-wallet-group, or consolidated;
detail tables that back a report drill-down rather than a memo string).

### `source_documents`
One row per **expected fixed-expectation upload**, per CLAUDE.md's Report finalization
status condition 2 ("a period with zero uploaded documents also has zero review-queue rows —
without this check [it] would incorrectly flip to Final on empty data"). This table is what
that eventual check queries — computing Provisional/Final itself is milestone 5/backend-report
work, not built here.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PK` | |
| `document_type` | `TEXT` | `ebay_sales_csv` \| `payoneer_csv` \| `bank_statement_wallet_group` \| `bank_statement_master` — each value has a **fixed** scope_kind (see below), so this single column plays the same role `account_types.scope_kind` plays for `accounts`, without needing a second column |
| `ebay_account_id` | `FK → ebay_accounts, NULLABLE` | set only for `ebay_sales_csv` |
| `wallet_group_id` | `FK → wallet_groups, NULLABLE` | set only for `payoneer_csv` / `bank_statement_wallet_group` |
| `period_month` | `DATE` | first-of-month |
| `ingested_at` | `TIMESTAMPTZ, NULLABLE` | NULL = not yet uploaded/found; set once a matching file is located in Drive **and** successfully parsed |
| `drive_file_id` | `TEXT, NULLABLE` | |
| `drive_file_name` | `TEXT, NULLABLE` | |
| `row_count` | `INTEGER, NULLABLE` | rows/transactions parsed out of the file, for the Documents screen's "file found, N rows" display |
| `parse_warning` | `TEXT, NULLABLE` | set when extraction partially failed (e.g. OCR couldn't reliably read some lines) — a **document-level** flag distinct from any individual review-queue row's status, see §4 |
| `created_at` | `TIMESTAMPTZ` | |

Partial unique indexes (same pattern as `accounts` in `ledger/schema.py`): one on
`(document_type, period_month, ebay_account_id) WHERE ebay_account_id IS NOT NULL`, one on
`(document_type, period_month, wallet_group_id) WHERE wallet_group_id IS NOT NULL`, one on
`(document_type, period_month) WHERE ebay_account_id IS NULL AND wallet_group_id IS NULL`
(covers `bank_statement_master`, always consolidated).

Rows are created by an idempotent `ensure_expected_source_documents(period_month, ...)`
helper (upsert-if-absent) called at the start of a sync run for whatever
accounts/wallet-groups/periods are active — **not** wired to any schedule (that's milestone 5;
here it's just a plain function callable manually/from a test, matching "a one-time idempotent
helper is fine to design... but don't wire it to any schedule/cron" in the brief).

Invoices are deliberately **not** in this table — per CLAUDE.md and the UI/UX design, invoice
count is variable, not a fixed expectation, so there's no "expected" row to create. They live
only in the `invoices` table below.

### `review_queue`
Bank/Payoneer (and, see §2, eBay-CSV-row-type-we-didn't-plan-for) lines awaiting or already
carrying a classification.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PK` | |
| `source_type` | `TEXT` | `payoneer_csv` \| `bank_statement` \| `ebay_sales_csv` (this third value is a deliberate defensive extension beyond CLAUDE.md's literal Payoneer/bank scope — see §2) |
| `source_document_id` | `FK → source_documents` | which ingested file this line came from |
| `ebay_account_id` | `FK → ebay_accounts, NULLABLE` | filled in once matched to a specific eBay account (e.g. via rule (a) or (d)); NULL until then |
| `wallet_group_id` | `FK → wallet_groups, NULLABLE` | NULL for `bank_statement_master` lines (consolidated) |
| `external_ref` | `TEXT, NULLABLE` | Payoneer's own `Transaction ID` for `payoneer_csv` rows (globally unique, real dedup key); a synthesized deterministic key for OCR'd bank lines — see the idempotency note in §7 |
| `transaction_date` | `DATE` | |
| `amount_idr` | `NUMERIC(20,2)` | **signed** — positive = inflow, negative = outflow (deliberately not split into separate debit/credit columns like `journal_lines`, since this is a pre-ledger staging row, not a balanced posting) |
| `amount_usd_ref` | `NUMERIC(14,2), NULLABLE` | for Payoneer USD lines |
| `raw_description` | `TEXT` | verbatim CSV field or OCR'd text |
| `match_status` | `TEXT` | `matched` \| `needs_review` |
| `match_rule` | `TEXT, NULLABLE` | which of rules (a)–(e) fired, or NULL — for debugging/traceability, not itself used downstream |
| `category` | `TEXT, NULLABLE` | `revenue_settlement` \| `cogs_purchase` \| `consignment_payout` \| `internal_transfer` \| `operating_expense` \| `owners_draw` \| `owners_contribution` \| `other` — set automatically at match time for `matched` rows, or by the human editor for `needs_review` rows (mirrors the Review Queue screen's dropdown in the UI/UX design doc) |
| `consignor_item_ref` | `TEXT, NULLABLE` | only meaningful when `category = consignment_payout`, entered by the human (or copied from the matched `consignment_sales` row when auto-matched via rule (d)) |
| `linked_invoice_id` | `FK → invoices, NULLABLE` | set when rule (b) matches an invoice |
| `labeled_at` | `TIMESTAMPTZ, NULLABLE` | when a human filled in `category` for a `needs_review` row (distinct from auto-match, which sets `category` immediately without this being meaningfully "labeled" by a human) — this is what the UI/UX design's "Queued for next sync" state reads |
| `posted_at` | `TIMESTAMPTZ, NULLABLE` | **the posted marker** — NULL means never posted; this is the idempotency guard for CLAUDE.md's rule 6 |
| `posted_journal_entry_id` | `FK → journal_entries, NULLABLE` | set once posted; for the paired-transfer case (§3/§6) this can point at a journal entry that a *different* review_queue row actually triggered the posting for |
| `created_at` | `TIMESTAMPTZ` | |

Unique index on `(source_type, external_ref) WHERE external_ref IS NOT NULL` — the
row-creation idempotency guard (distinct from `posted_at`, which is the posting-idempotency
guard). See §7 for why these are two separate concerns.

The sync/posting step only ever touches rows matching `category IS NOT NULL AND posted_at IS
NULL` — this single predicate covers both "auto-matched, post immediately" and "was
needs_review, human just labeled it, post on next sync," per the UI/UX design's "Matched rows
post automatically... Needs-review rows post once labeled, on the next sync" behavior. Rows
with `posted_at IS NOT NULL` are never touched again by posting logic — there is deliberately
no code path that re-posts or reverses a posted row (out of scope, per CLAUDE.md).

### `invoices`
| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PK` | |
| `drive_file_id` | `TEXT` | |
| `drive_file_name` | `TEXT` | |
| `period_month` | `DATE` | which month's Master Account "Invoices & Proof of Purchase" folder it came from |
| `extracted_date` | `DATE, NULLABLE` | NULL if OCR couldn't find one |
| `vendor_description` | `TEXT, NULLABLE` | |
| `amount_idr` | `NUMERIC(20,2), NULLABLE` | |
| `purpose` | `TEXT, NULLABLE` | `cogs_purchase` \| `consignment_purchase` — NULL until confirmed (see §5 for when this can/can't be auto-filled) |
| `status` | `TEXT` | `parsed` \| `needs_confirmation` |
| `ocr_raw_text` | `TEXT, NULLABLE` | full raw extraction, kept for audit/debugging and so a human correcting the row can see what the system actually read |
| `ocr_confidence` | `NUMERIC(5,2), NULLABLE` | if the OCR engine reports one; informational only — see §4/§5 for why this doesn't itself decide Parsed vs Needs Confirmation |
| `confirmed_at` | `TIMESTAMPTZ, NULLABLE` | set at creation time for auto-`parsed` rows, or when a human saves corrections to a `needs_confirmation` row |
| `matched_review_queue_id` | `FK → review_queue, NULLABLE` | set once a bank/Payoneer line matches this invoice (rule (b)) — the "Matched / Awaiting payment match" column in the UI/UX design |
| `created_at` | `TIMESTAMPTZ` | |

No `ebay_account_id` / `wallet_group_id` — invoices are consolidated-only per CLAUDE.md
("filing them per-account would imply an attribution that doesn't exist").

### `invoice_journal_links`
The traceability join CLAUDE.md's "numbers are traceable" rule needs — lets a posted COGS (or
consignment-reimbursement) journal entry point back to the specific invoice(s) that justified
it, without touching `journal_lines`' existing columns.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PK` | |
| `journal_entry_id` | `FK → journal_entries` | |
| `invoice_id` | `FK → invoices` | |

Many-to-many by construction (one bundled invoice could in principle back more than one
posting, or — more realistically for this business, given COGS is expensed in aggregate, not
per-item — one posting could eventually be justified by more than one invoice if a future
change batches purchases; keeping it a join table costs nothing and avoids a future migration).

### `ebay_expected_payouts`
Reference facts extracted from eBay CSV `Payout` rows — **not** posted directly (see §2/§3);
consumed by the Payoneer-CSV auto-match rule (a).

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PK` | |
| `ebay_account_id` | `FK → ebay_accounts` | |
| `ebay_payout_id` | `TEXT` | eBay's own Payout ID (e.g. `7661064504`) |
| `payout_date` | `DATE` | eBay's `Transaction creation date` on the Payout row |
| `net_amount_usd` | `NUMERIC(14,2)` | absolute value of the Payout row's Net amount |
| `source_document_id` | `FK → source_documents` | |
| `matched_review_queue_id` | `FK → review_queue, NULLABLE` | set once a Payoneer line confirms arrival |
| `created_at` | `TIMESTAMPTZ` | |

Unique on `(ebay_account_id, ebay_payout_id)` — re-parsing the same eBay CSV export never
creates a duplicate expectation row.

### `bank_keyword_rules`
Backs auto-match rule (e) ("recurring-description keyword rules... for anything else
confidently recognizable"). Admin-editable-later table, not a hardcoded Python dict — same
precedent as the Consignor Payout Tiers table (a Settings-screen UI for editing this is
milestone 4 work; the table itself belongs here).

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PK` | |
| `keyword` | `TEXT` | matched as a case-insensitive substring against `raw_description` — no fuzzy scoring, see §4 |
| `category` | `TEXT` | one of `review_queue.category`'s values |
| `expense_account_type_code` | `TEXT, NULLABLE` | which `account_types.code` to debit when `category = operating_expense` |
| `is_active` | `BOOLEAN` | |
| `created_at` | `TIMESTAMPTZ` | |

### `kurs_pajak_rates`
Per open question 3 — a plain reference table, manually seeded for now.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PK` | |
| `effective_date` | `DATE` | the week this rate applies from |
| `rate_idr` | `NUMERIC(12,4)` | 1 USD = this many IDR |

Lookup: for a given `entry_date`, use the row with the largest `effective_date <=
entry_date` (Kemenkeu publishes weekly, so this picks "the most recent published rate as of
this date").

### Additive column on `consignment_sales` (existing milestone-2 table)
One new **nullable** column, added via a fresh migration — no existing column, constraint, or
default is touched:

| Column | Type | Notes |
|---|---|---|
| `reimbursed_journal_entry_id` | `FK → journal_entries, NULLABLE` | set once auto-match rule (d) finds and posts the matching consignor reimbursement, so rule (d) can filter to "confirmed, posted-as-a-sale, but not yet reimbursed" rows |

---

## 2. eBay sales CSV parser design

Source: a Seller Hub **Transaction report** (confirmed against the real sample — not an
Orders report). Structural facts confirmed from the real file:

- A fixed-format notes/metadata block precedes the real header row. The parser must locate the
  header by **content** (the row whose first cell is literally `Transaction creation date`),
  not by a hardcoded row index — the notes block's exact line count isn't a format guarantee.
- Observed `Type` values in the real sample: `Order` (100), `Other fee` (83), `Refund` (11),
  `Hold` (4, as one placed/released pair per case), `Payout` (4). CLAUDE.md's own header notes
  text mentions many more possible types (`claim, payment dispute, shipping label, charge,
  transfer, adjustment, purchase, secondary payout, withheld tax, reserve`) that never appear
  in this one month's data.
- `Gross transaction amount` and itemized fee columns (`Final Value Fee - fixed`, `Final Value
  Fee - variable`, `Regulatory operating fee`, `International fee`, `Deposit processing fee`,
  `Below standard performance fee`, `Very high "item not as described" fee`, `Charity
  donation`) are all separate columns — confirms revenue can post gross with fees itemized.
- `Custom label` (the SKU field) is blank on every row in this sample — zero real
  `CONSIGN-`-prefixed rows, confirming the brief's note that this path only gets synthetic test
  coverage this milestone.
- No category/item-specifics column anywhere — category tag stays NULL, as expected.

### Branch on `Type`

- **`Order`** → `post_ebay_sale(...)`. `gross_sale_price_usd = Gross transaction amount`;
  `ebay_fee_usd` = the itemized eBay-take columns only (`Final Value Fee - fixed` + `Final
  Value Fee - variable` + `Regulatory operating fee` + `International fee` + `Deposit
  processing fee` + `Below standard performance fee` + `Very high "item not as described"
  fee`), summed as absolute values (all are stored as negative numbers reducing net amount).
  `Charity donation` is deliberately **excluded** from `ebay_fee_usd` — it's a seller-elected
  donation, not an eBay fee — and is $0 in every sample row; if a real row ever has a nonzero
  Charity donation, that row is routed to `review_queue` (`source_type=ebay_sales_csv`,
  `needs_review`) rather than silently folded into fees or dropped, since neither CLAUDE.md nor
  the chart of accounts says what account it belongs to.
  - **Row-level reconciliation check**: `Item subtotal + Shipping and handling` should equal
    `Gross transaction amount` (confirmed true on every sampled row, e.g. 78 + 30 = 108). Any
    row where this doesn't hold (beyond a documented ±1 cent rounding tolerance) is routed to
    `review_queue` as `needs_review` instead of being posted with a number the parser isn't
    confident about — the eBay CSV is structured data, but that doesn't mean every row is
    guaranteed self-consistent, and CLAUDE.md's "never silently guess" principle should apply
    here too even though this section of CLAUDE.md is framed around bank/Payoneer feeds. This
    is a deliberate extension beyond CLAUDE.md's literal text — flagged for Main-agent, not
    assumed.
  - `Transaction currency` is asserted `== 'USD'`; a non-USD row is routed to `review_queue`
    rather than guessed at, since the sample's `Exchange rate` column is blank/unused
    throughout and CLAUDE.md's ledger design otherwise assumes USD settlement.
  - **`CONSIGN-` detection**: if `Custom label` (stripped, case-insensitive) starts with
    `CONSIGN-`, the row does **not** call `post_ebay_sale`. Instead it calls
    `create_consignment_sale(...)` with a *suggested* payout from `lookup_tier_rate` +
    `calc_tier_payout_usd` (both already in `ledger/consignment.py`), `confirmed=False`. It is
    never auto-posted — milestone 2's two-phase flow (create → confirm → post) already forbids
    that, and this parser doesn't try to work around it. There is currently no UI to actually
    confirm these (that's milestone 4, or an extension of the Review Queue) — for the
    prototype, an unconfirmed row simply sits there, visible only via a direct query, until a
    human confirms it through whatever mechanism Main-agent decides for Phase B. Given the real
    sample has zero such rows, this path is validated only with synthetic test fixtures this
    milestone, as the brief anticipates.
- **`Refund`** → `post_refund(stage='ebay_wallet', ...)` for the Sales Returns & Allowances /
  eBay Wallet contra-revenue leg. **Open sub-question**: refund rows also credit back some of
  the original Final Value Fee (e.g. `+0.44` / `+18.5` in the sample) — `post_refund`'s
  existing two-line shape (debit Returns, credit cash account) has no slot for a fee
  credit-back. `post_refund`'s signature isn't being changed; instead Phase B would add a new,
  separate small function (e.g. `post_refund_fee_credit`, crediting `EBAY_SELLING_FEES` /
  debiting `EBAY_WALLET`) called alongside it for the fee-refund component. Flagging this now
  since it's a real (small) money-math gap in how a refund nets through the books, not
  something to silently absorb into the Returns & Allowances number.
- **`Hold`** → always skipped, never posted, never sent to `review_queue`. A hold is a
  temporary availability restriction on funds already recognized via the underlying `Order`
  row — it doesn't represent a distinct revenue/expense event by itself. This holds whether or
  not a matching "Hold released" row appears in the same file (an unreleased hold at month-end
  still doesn't change what's already been posted). Logged for traceability only, not stored as
  a ledger-adjacent row.
- **`Other fee`** (Promoted Listings ad fee, Store subscription fee, observed) → debits an
  operating-expense account, credits `EBAY_WALLET`, using the row's own (already-USD) `Net
  amount` and the `kurs_pajak_rates` lookup for that date. Needs a **new** small posting
  function (`post_ebay_wallet_expense` or similar) — no existing milestone-2 function has this
  "debit opex, credit eBay Wallet" shape. Which account code to debit is open question 8 above.
- **`Payout`** → never posted directly. Recorded as an `ebay_expected_payouts` row (§1) for the
  Payoneer-CSV side to confirm arrival against (rule (a), §6). Rationale: until a Payoneer line
  confirms the cash actually landed, positing a completed eBay Wallet → Payoneer Wallet
  transfer would be booking a movement we haven't actually observed land anywhere yet — more
  conservative and matches CLAUDE.md's own framing of rule (a) as "match... an *expected* eBay
  payout" (i.e. the CSV Payout row is the expectation, not a completed fact by itself).
- **Any other `Type`** (the ones mentioned in the file's own notes text but never observed —
  `claim`, `payment dispute`, `shipping label`, `charge`, `transfer`, `adjustment`, `purchase`,
  `secondary payout`, `withheld tax`, `reserve`) → routed to `review_queue`
  (`source_type=ebay_sales_csv`, `needs_review`), never silently skipped or guess-posted. This
  is the same deliberate defensive extension noted above.

---

## 3. Payoneer CSV parser design

Structured columns confirmed from the real sample: `Transaction Date`, `Time`, `Time Zone`,
`Transaction ID`, `Description`, `Credit Amount`, `Debit Amount`, `Currency`, `Transfer
Amount`, `Transfer Amount Currency`, `Status`, `Running Balance`, `Additional Description`,
`Store Name`, `Source`, `Target`, `Reference ID`.

Only `Status == 'Completed'` rows participate (the sample has no other status, but a
pending/failed row shouldn't be treated as a real cash event). `Transaction ID` is the
row-creation dedup key (§1/§7).

Two row shapes observed, both need different handling:

### "Payment from eBay" (credit, USD, `Source=eBay`, `Target=USD balance`)
`Additional Description` carries `P <eBay Payout ID>` — this is the match key against
`ebay_expected_payouts` (rule (a), exact ID-text match preferred; fall back to date±3
days/amount-tolerance match if the ID text isn't parseable, which matters more for bank-PDF
lines that won't carry this field at all). On match: `category = revenue_settlement`, and the
posting is `post_inter_account_transfer(from=EBAY_WALLET(matched ebay_account_id),
to=PAYONEER_WALLET(this wallet_group_id), amount_idr=...)` — this is a legitimate reuse of the
existing milestone-2 transfer function (both `EBAY_WALLET` and `PAYONEER_WALLET` are in
`TRANSFERABLE_ACCOUNT_TYPE_CODES` already). Valuation: the USD amount is converted to IDR via
`kurs_pajak_rates` looked up by the Payoneer line's own date (not the original sale's booking
rate — this is a distinct, later internal custodial movement of already-recognized USD, not a
new sale) — see open question 5 for the accompanying `post_inter_account_transfer` signature
gap this surfaces, and note explicitly: **this movement never crystallizes realized FX
gain/loss** — CLAUDE.md is explicit that realized FX only happens at the actual USD→IDR
Payoneer withdrawal step, and no such conversion happens here (both sides are USD accounts).

If no `ebay_expected_payouts` row matches within tolerance, this line falls through rules
(b)–(e) like anything else, and very plausibly lands in `review_queue` as `needs_review` — per
CLAUDE.md's own explicit scope note for the prototype's shared-wallet-group account, this is
**expected, not a bug**, when the wallet-group's export contains settlements for a not-yet
-onboarded sibling eBay account.

### "Withdrawal to [BANK]" (debit, USD, `Target = bank name`, has a `Reference ID`)
This row alone is **not enough to post** — it only gives the gross USD debited and the
Transfer Amount in IDR, not the fee or "exchange rate excluding fee" that
`post_realized_fx_withdrawal` requires. Per CLAUDE.md, those two figures must come from the
matching **withdrawal confirmation PDF**, used directly, never inferred. Design: this CSV row
is a **trigger/pointer**, matched (by amount + date, or by the numeric `Transfer ID` embedded
in the confirmation PDF if the CSV's `Reference ID` carries a comparable value — need to
confirm this once real multi-month samples exist) against a confirmation PDF ingested from the
same wallet-group/period. Once matched, the confirmation PDF's stated `Amount withdrawn`,
`Fee`, and `Exchange rate (excluding fee)` feed `post_realized_fx_withdrawal` directly
(`gross_usd`, `payoneer_fee_usd`, `exchange_rate_excl_fee`); `booking_rate_used_idr` comes from
the `kurs_pajak_rates` lookup per open question 4's chosen approach (flagged, unresolved).

**Where do confirmation PDFs live in Drive?** CLAUDE.md's folder structure names a "Payoneer
Exports (CSV)" subfolder but never a distinct confirmation-PDF folder. Proposal: confirmation
PDFs live in that same "Payoneer Exports (CSV)" folder (mixed file types, both Payoneer
-sourced, same wallet-group/period) — flagging for confirmation before Phase B rather than
assuming.

The resulting Payoneer Wallet → BCA Bridging Account leg is posted directly from this matched
CSV-row + PDF pair — it does **not** go through `review_queue` at all, since both sources are
structured/explicit, not free-text OCR needing human classification. `review_queue` is reserved
for lines the system genuinely can't confidently classify; this pairing is unambiguous once
matched.

### Anything else
Any Payoneer CSV row that isn't one of the two shapes above (e.g. a Payoneer-charged monthly
fee, if one ever appears — none in the sample) goes through the same generic rule (b)–(e)
matching engine as bank-statement lines (§6), landing in `review_queue`.

---

## 4. Bank statement PDF OCR approach

The real sample (`BCA Bank_1790345891_APR_2026.pdf`) is a **born-digital, text-layer PDF**
(machine-generated statement, not a scan) — its text extracted cleanly with high fidelity
when read directly. That's a materially easier case than true image-based OCR, but CLAUDE.md
treats *all* bank-PDF ingestion as noisy/OCR-tier, and a scanned or photographed statement is a
realistic possibility for other banks/future accounts, so the pipeline should support both
without assuming every future bank PDF is this clean.

**Proposed libraries** (none currently in `requirements.txt` — new additions, flagged for
QA's dependency/supply-chain review before Phase B):
- **`pdfplumber`** (pure-Python) as the primary extractor for born-digital PDFs — good
  table-structure extraction, which this statement's layout benefits from (a genuine 5-column
  table: TANGGAL / KETERANGAN / CBG / MUTASI / SALDO).
- **`pytesseract` + `pdf2image`** (needs system packages `tesseract-ocr` and `poppler-utils`
  installed via `apt`, not pip — a droplet provisioning step, not something `requirements.txt`
  alone can satisfy) as the fallback for image-based/scanned PDFs, and as the primary path for
  the invoice **images** (§5), which are genuinely photographs/screenshots, not PDFs.
- Chose Tesseract over a heavier deep-learning OCR stack (e.g. PaddleOCR/EasyOCR) specifically
  because of CLAUDE.md's explicit 1GB-RAM droplet constraint — Tesseract is far lighter, and
  the RAM-pressure warning in CLAUDE.md's Architecture section is exactly the kind of tradeoff
  that should drive this choice rather than picking whatever has the best benchmark numbers.
- `Pillow` as a shared dependency of the above.

### Extraction approach for the BCA layout
1. `pdfplumber.extract_table()` per page, targeting the 5-column table.
2. Forward-fill blank `TANGGAL` cells from the last non-blank date seen (the real layout omits
   the date on continuation rows within the same day — confirmed in the sample).
3. Parse the `MUTASI` cell for a numeric amount + optional `DB` suffix; no suffix (only
   observed on incoming-credit rows, e.g. the `BI-FAST CR` line) means credit/inflow.
4. `KETERANGAN` is kept as one joined raw multi-line string — deliberately **not**
   over-parsed into sub-fields (reference numbers, recipient names) beyond what's needed for
   keyword matching (rule (e)) and human review display. Over-fitting a parser to this one
   bank's internal reference-code format (e.g. `0104/FTSCY/WS95271`) risks silently breaking on
   the next statement's slightly different formatting; the raw text is always shown to a human
   for `needs_review` rows regardless.
5. Skip the header/footer boilerplate (repeated on every page) and the `SALDO AWAL`/`SALDO
   AKHIR`/summary-totals rows — these aren't transactions.

### The "Matched vs Needs Review" rule — stated explicitly, per the brief's requirement
A bank (or Payoneer) line is **Matched** if and only if one of auto-match rules (a)–(e) fires
against it, using these concrete, documented thresholds (chosen deliberately over anything
vaguer, since CLAUDE.md doesn't specify exact numbers and leaving this unstated would be a real
gap):
- **Amount**: exact match required, with a ±Rp 100 tolerance to absorb IDR-rounding noise from
  `round_idr`'s `ROUND_HALF_UP` elsewhere in the pipeline — not an open-ended fuzzy match.
- **Date**: same-day exact for Payoneer-CSV-sourced lines matched against another
  structured-data source (no reason for drift between two systems' own clocks); **±3 calendar
  days** for anything matched against a bank-PDF-sourced line (settlement/posting lag is real
  and expected there) — a concrete, stated window, not "close enough."
- **Rule (e) keyword rules**: case-insensitive **substring** containment against
  `bank_keyword_rules.keyword` — no partial/fuzzy scoring, to avoid guessy false positives on a
  catch-all rule that's explicitly the lowest-confidence tier of the priority order.
- If none of (a)–(e) fire under these thresholds, the row is **Needs Review — full stop —
  regardless of how confident the OCR engine itself was about reading the text correctly.**
  This directly answers the brief's own suggested framing: matched status is earned by a rule
  firing, not by an OCR confidence score. An OCR confidence score (where available) is stored
  (`ocr_confidence` on `invoices`; not modeled on `review_queue` at all, since bank lines don't
  need it under this rule) purely for debugging/audit, never as an input to the match decision.

### When OCR can't even produce usable fields for a line
If extraction can't recover a parseable amount/date for what looks like a transaction row at
all, the pipeline does **not** synthesize a `review_queue` row with a NULL amount (that would
break every downstream sum/report-drilldown that assumes `amount_idr` is always present). Instead
this is a **document-level** signal: `source_documents.parse_warning` gets a note (e.g. "page 3:
1 row could not be parsed, X of Y expected rows recovered"), surfaced on the Documents screen
as a reason the statement might need a closer manual look — distinct from, and in addition to,
any individual `needs_review` rows the lines that *did* parse produce.

---

## 5. Invoice OCR/capture approach

Same OCR stack as §4 (Tesseract for the two image files; the Tokopedia PDF is again
born-digital and extracts cleanly via `pdfplumber`/plain text extraction). The three real
samples are honestly evaluated below rather than assumed uniformly OCR-able:

- **Tokopedia PDF** — clean, labeled, born-digital text (`Tanggal Pembelian`, `Penjual`, `TOTAL
  TAGIHAN`, itemized `INFO PRODUK` line(s)). High-confidence, label-based field extraction
  (search for the known Indonesian field labels, not positional/coordinate guessing) reliably
  gets Date, Vendor (`Penjual`), and Amount. **Amount field choice**: `TOTAL TAGIHAN` (Rp
  2,617,600 in the sample), not `TOTAL BELANJA` (Rp 2,616,600) — they differ by a `Biaya
  Layanan` (service fee) line; `TOTAL TAGIHAN` is "what you actually owe/pay," which is what
  should reconcile against the paying bank transaction. → `status = parsed`.
  - **Purpose**: this sample is literally a phone purchase, not TCG/watch/auto-part/toy stock —
    see open question 6/7. The parser does **not** blanket-assume "every Tokopedia invoice =
    COGS." Proposed heuristic: `purpose = cogs_purchase` is only auto-filled when the extracted
    itemized product line(s) match known inventory vocabulary (a small keyword list per
    category — Pokemon/TCG card terms, watch brand/model patterns like the ones seen in the
    handwritten sample, automotive part terms); otherwise `purpose` stays NULL and
    `status = needs_confirmation` even though Date/Vendor/Amount extraction was itself
    high-confidence. A phone purchase like the actual sample would **not** auto-fill Purpose
    under this rule — it would surface for a human to decide (which per open question 6 might
    reveal the two-value Purpose enum needs a third option).
- **Handwritten shop receipt** ("Toko Arloji" watch shop, two Seiko models, handwritten
  quantities/prices/total, a handwritten spelled-out `TERBILANG` amount as a natural
  cross-check) — realistically low-confidence for the handwritten fields. Design:
  - Vendor/shop name is **printed** (letterpress header) — reliably extractable, high
    confidence on this one field even though the rest of the form is handwritten.
  - Date and Amount are handwritten — attempt Tesseract extraction of both the numeral `Jumlah
    Rp` figure and, where present, the spelled-out `TERBILANG` amount; if both parse, cross
    -check them against each other as a concrete validation signal (agreement doesn't
    guarantee correctness, but disagreement is a strong, cheap "don't trust this" signal).
  - **Any of**: Date unparseable, Amount unparseable, or numeral-vs-terbilang mismatch →
    `status = needs_confirmation`, regardless of how well Vendor extracted. Given this is a
    real, honest limitation of OCR against handwriting, I expect (and CLAUDE.md's own text
    anticipates — "expect this often on handwritten receipts") that this document type lands in
    Needs Confirmation as the **common case**, not an edge case.
  - `Purpose`: the vendor name itself ("Toko Arloji" = watch shop) is a reasonably strong signal
    for `cogs_purchase` (watches are a real product category) — proposed to pre-fill
    `purpose = cogs_purchase` here even while Date/Amount stay unconfirmed, since Purpose and
    Date/Amount are independent judgments (one doesn't need to block the other from being
    pre-filled where the signal genuinely is strong).
- **BCA transfer screenshot** (phone-app UI screenshot, not handwritten) — despite being a JPEG
  like the receipt above, this should OCR **well**, because the reliability driver is
  print-vs-handwriting, not PDF-vs-image format. Clean sans-serif app UI text, high contrast,
  labeled fields (`Total Payment`, a timestamp, `Product Name`, `Details`, `Reference No.`) —
  proposed `status = parsed`.
  - **Purpose is the harder call here, not the OCR itself**: this is a bare proof-of-transfer
    with **no itemized product content** — just a payment amount and a recipient/product-name
    tag (`SHOPEE` in the sample). Per open question 7's reading, a document like this could
    represent either a COGS payment (paying a Shopee-based inventory seller) or a consignor
    reimbursement payout, and nothing in the document itself says which. Design rule: **Purpose
    auto-fills only when the source document contains itemized product/inventory content**
    (Tokopedia-style line items, or a shop receipt's `Nama Barang` rows); a bare
    proof-of-transfer with no itemization always leaves `purpose = NULL` and forces
    `status = needs_confirmation`, no matter how cleanly the rest of it OCR'd. This directly
    reflects that Purpose is a judgment about intent that the document doesn't actually state,
    not something OCR quality can ever resolve.

---

## 6. Auto-match priority logic design

One shared matching engine, run over any staged raw line (Payoneer CSV row or bank-PDF-parsed
line) in CLAUDE.md's exact stated order. Pseudocode-level design (not final code):

```
for line in staged_lines:
    if rule_a_matches(line):       # expected eBay payout (ebay_expected_payouts)
        category = "revenue_settlement"
    elif rule_b_matches(line):     # invoices.amount_idr, ± tolerance/window
        category = "cogs_purchase" if invoice.purpose == "cogs_purchase" else "consignment_payout"
        # (consignment_purchase invoices route to consignment_payout per open question 7's reading)
        link invoice.matched_review_queue_id
    elif rule_c_matches(line):     # payoneer_withdrawals.net_idr_landed (Bridging<->Main leg)
        category = "internal_transfer"
        # see the paired-transfer note below — only ONE side actually calls the posting function
    elif rule_d_matches(line):     # confirmed, posted, not-yet-reimbursed consignment_sales rows
        category = "consignment_payout"
        consignor_item_ref = matched_row.consignor_item_ref
    elif rule_e_matches(line):     # bank_keyword_rules substring match
        category = rule.category
    else:
        match_status = "needs_review"
        category = None
        continue
    match_status = "matched"
```

Each rule is scoped to the line's own `wallet_group_id`/consolidated context (a bank-statement
line never matches against a different wallet-group's expected payouts/transfers).

### The paired-transfer double-posting risk (rule (c))
A single real Bridging→Main transfer produces **two** bank lines to reconcile — an outflow in
the wallet-group's bridging statement and an inflow in the Master statement (see open question
1/2 for whether this even applies to the current sample, vs. the transfer being invisible
because Payoneer already lands straight in what functions as Main). Posting it twice would
double the transfer. Design: whichever line is processed **first** (order depends on which
statement got ingested first in a given sync run) is the one that actually calls
`post_inter_account_transfer(BCA_BRIDGING → BCA_MAIN, ...)`; the **second** line, once matched
against the same `payoneer_withdrawals.net_idr_landed` amount, is marked `matched` /
`posted_at` = now / `posted_journal_entry_id` = **the same journal_entry_id the first line
produced**, without calling the posting function a second time. This requires the matching
engine to check "has this expected-transfer amount already been consumed by another
review_queue row in this sync run" before deciding to post vs. just link.

### Rule (b): which invoice status participates in matching
Only invoices with a **non-NULL `amount_idr`** participate, regardless of `parsed` vs
`needs_confirmation` status — a `needs_confirmation` invoice might still have correctly-read
Amount even if Vendor/Date/Purpose are uncertain (see §5's per-field confidence design).
Matching still requires the tight amount/date tolerances from §4 — a coincidentally-similar
wrong-OCR amount shouldn't false-positive-match just because the invoice's overall status is
uncertain.

### Rule (d): posting mechanics
On match, `post_consignor_reimbursement(consignor_item_ref=matched_row.consignor_item_ref,
amount_idr=matched_row.payout_amount_idr, ...)` (existing milestone-2 function, unchanged), then
set the new `consignment_sales.reimbursed_journal_entry_id` (§1) so the row drops out of future
rule (d) candidate matching.

### Rules needing a new (additive) posting-engine function
- Rule (e) → `operating_expense` category needs a generic "debit expense account, credit paying
  account" poster (proposed `post_operating_expense`, reusing `bank_other` as `source_type` per
  the existing catch-all precedent in `post_shipping_cost_purchase`). Covers Payroll, General
  Opex, and the bank admin-fee (`BIAYA ADM`) keyword-rule case from the real sample.
- `owners_draw` category → `post_owner_draw` already exists and fits directly (no new function
  needed) — e.g. the sample's owner-named outbound transfers, once a human confirms via
  `needs_review` that a given line really is a draw and not, say, an unlabeled COGS purchase to
  an individual seller (see below).
- §2's `Other fee` eBay-CSV posting needs its own new function (not a review-queue concern,
  but noted here for completeness since it's also a new additive posting function this
  milestone surfaces).

### A realistic expectation, not a gap to fix
Many of the sample bank statement's debit lines are transfers to **named individuals** for what
look like single-item inventory purchases (a pattern consistent with informal, non-invoiced
purchases from individual sellers). These will very often **not** match rule (b) (no
corresponding invoice document exists for an informal purchase) and will very often **not** hit
a rule (e) keyword either (recipient names aren't generic keywords). They're expected to land
in `needs_review` at a meaningfully higher rate than a business with fully-invoiced purchasing
would — this is correct, intended behavior per CLAUDE.md's "never silently guess," not a
parser shortfall to chase down.

---

## 7. Posted-row tracking

Two genuinely separate idempotency concerns, deliberately modeled as two separate mechanisms
rather than conflated into one:

1. **Row-creation idempotency** — don't create a duplicate `review_queue` row when the same
   source file (or an overlapping re-export covering the same period) is parsed again.
   - Payoneer CSV: `external_ref = Transaction ID` (a real, stable, globally-unique id from
     Payoneer) — unique index on `(source_type, external_ref)`.
   - Bank-statement PDF lines have no native transaction id. Proposed synthetic key: `hash(
     source_document_id, transaction_date, raw_description, amount_idr, occurrence_index )`,
     where `occurrence_index` counts repeats of an identical `(date, description, amount)`
     tuple **within one file's parse**, in extraction order. This makes re-parsing the exact
     same file deterministic (same rows → same keys → no duplicates), while still keeping
     genuinely-repeated identical-looking transactions within one statement as distinct rows
     (the Nth occurrence stays the Nth occurrence on re-parse, as long as extraction order is
     stable — a documented, honestly-stated limitation, not a false guarantee: two *different*
     real transactions that happen to share date+description+amount exactly, split across two
     *different* file versions with different surrounding content, aren't guaranteed to dedupe
     correctly. Acceptable given bank statements are appended-to/re-issued rarely and the whole
     pipeline already routes ambiguous cases to human review.)
   - eBay CSV `Order`/`Refund` rows: `Transaction ID` column (present, structured) serves the
     same role directly if these ever need a `review_queue` presence (§2's "unexpected Type"
     path); `Payout` rows use `(ebay_account_id, ebay_payout_id)` (already unique on
     `ebay_expected_payouts`).
2. **Posting idempotency** — `review_queue.posted_at` (§1). The sync/posting step's query is
   always `WHERE category IS NOT NULL AND posted_at IS NULL` — a row that already has
   `posted_at` set is never reconsidered, matching CLAUDE.md rule 6 exactly. There is
   deliberately no reopen/reverse code path (out of scope, per CLAUDE.md and the brief's
   explicit boundary) — a `posted_at`-set row is simply inert to every future sync run.

Keeping these separate matters: without (1), re-running ingestion on the same file would create
*new* `needs_review` rows every sync (visible duplication, confusing to the user, and a
correctly-labeled duplicate could even get posted twice via mechanism (2) alone). Without (2), a
row that's already been labeled and posted could be re-posted if some future change caused it to
be reconsidered. Both guards are needed, independently.

---

## 8. Google Drive read layer

Thin interface only — no live connectivity test in Phase A, per the brief.

```
class DriveClient:
    def list_files(self, folder_id: str, mime_types: list[str] | None = None) -> list[DriveFile]
    def download_file(self, file_id: str) -> bytes
    def find_or_create_folder(self, parent_id: str, name: str) -> str  # idempotent get-or-create
```

- `DriveFile`: `id`, `name`, `mime_type`, `modified_time`.
- Auth: `google-auth`'s `service_account.Credentials.from_service_account_file`, path from the
  existing `GOOGLE_APPLICATION_CREDENTIALS` env var (never hardcoded — matches `ledger/db.py`'s
  existing pattern of env-var-only credentials). New proposed dependencies:
  `google-api-python-client`, `google-auth`, `google-auth-httplib2` — flagged for QA's
  dependency review before Phase B, same as the OCR libraries in §4.
- **Scope note (open question 10)**: since the "Finance & Accounting" folder tree is
  user-created, not created by the service account, the account needs at least
  `drive.readonly` (or full `drive`) scope to see it — the more restrictive `drive.file` scope
  (which only sees files/folders the service account itself created) would silently see
  nothing. Whether the actual credential file has adequate scope is unverified — Phase A
  confirmed only that the file *exists* at the expected path (existence check only, contents
  never read), not what it's scoped for.
- **Folder-ID resolution**: proposed a small `drive_folders` table (`scope` [ebay_account_id /
  wallet_group_id / consolidated] + `purpose` [sales_export / payoneer / bank_statement /
  invoices] + `drive_folder_id`), resolved once during setup against the real Drive tree (via
  `find_or_create_folder` traversal from `GOOGLE_DRIVE_ROOT_FOLDER_ID`, or manually seeded with
  real folder IDs) — not fabricated placeholder IDs.
- The month-ahead folder-provisioning feature (`find_or_create_folder` wired to a schedule) is
  explicitly **not** built here — that's milestone 5. `find_or_create_folder` itself is
  designed now only because a one-time idempotent "make sure this month's folder exists"
  helper is useful for testing ingestion against a real (if manually-created) folder during
  Phase B, per the brief's explicit allowance.

---

## Summary of what Phase B would build, pending Main-agent's answers to §0

1. The schema additions in §1 (new tables + one additive column on `consignment_sales`).
2. The eBay CSV parser (§2), including the two new small posting functions it needs
   (`post_ebay_wallet_expense`, `post_refund_fee_credit`) and the `ebay_expected_payouts`
   write path.
3. The Payoneer CSV parser (§3), including the `post_inter_account_transfer` optional-kwargs
   addition from open question 5, and the withdrawal-confirmation-PDF pairing.
4. The bank-statement OCR pipeline (§4) and the shared matching engine (§6), including the new
   `post_operating_expense` function.
5. The invoice OCR/capture pipeline (§5).
6. The Drive read layer (§8), tested against the real service account once its scope is
   confirmed usable.
7. Unit tests for all of the above, per CLAUDE.md's "write tests for anything that touches
   money calculations" — specifically: the paired-transfer single-posting guard (§6), the
   row-creation vs. posting-idempotency separation (§7), the Hold-pair skip logic, the
   CONSIGN- detection path (synthetic fixture, per the brief), and the Matched/Needs-Review
   threshold rules (§4) with deliberately-crafted near-miss cases (amount off by more than the
   tolerance, date outside the window) to prove they correctly fall to Needs Review rather than
   false-matching.
