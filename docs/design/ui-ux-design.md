# UI/UX Design — Milestone 1

This is the design-led first milestone: no backend, no database, no integrations. The goal is to sketch every screen and flow the prototype needs so that milestone 2 (the ledger engine) and milestone 4 (the real web app) are built against a spec the user has already seen and approved, instead of a UI improvised after the backend exists.

See `mockup.html` in this folder for a clickable, static version of what's described here (sample data only, no real logic).

## Scope for the prototype

Per `CLAUDE.md`: one eBay account, one month of data, going forward. Concretely that means this design only needs:
- A **Login** screen (added 2026-08-31 — see below)
- A **Documents** screen (ingestion status + invoice records — added 2026-08-31, see below)
- A **Review Queue** screen (the only screen the user edits bank/Payoneer transaction data on)
- A **Consignor Payout Tiers** settings screen (added 2026-08-31 — see below)
- Four **Report views**: Revenue, Cash Flow (both per-account + consolidated), P&L, Equity (both consolidated-only)
- Shared chrome: navigation, account/period selectors, and a Provisional/Final status indicator

Not in scope for this design pass: the other 2 eBay accounts' UI (same pattern, added later), any export/download UI (reports are view-only per the 2026-08-27 decision), any in-app **file** upload UI (files still land in Drive, not uploaded through the app — the Documents screen below is status/visibility, not an uploader), a posted-row correction/reversal flow (deferred 2026-08-31 — a known gap, not built for the prototype), and eBay API sync (deferred 2026-08-31 — eBay sales data is a manual CSV upload for now, same as everything else on the Documents screen).

## Shared chrome

- **Top bar**: app name ("Noctrowl"), account selector (Account 1 / Consolidated — greyed out to "Consolidated" only on P&L and Equity, since those are consolidated-only per Accounting Scope), month/period selector (defaults to the most recent period with data).
- **Left nav**: "Documents", "Review Queue", "Reports" (expands to the four statement types), and "Settings" (added 2026-08-31 — currently just the Consignor Payout Tiers table, more may land here later).
- **Status badge**: shown next to the period selector on every screen. Green "Final", or amber "Provisional" with the specific reason — "N items need review", "Bank Statement not yet received", or both if they both apply. Clicking it jumps to the Review Queue (if items need review) or the Documents screen (if a document is missing). This is the one status signal that must never be wrong — a report can never show Final while a Needs-review row is outstanding, or a fixed-expectation document hasn't arrived, for that account/period (per Definition of Done and Report finalization status).

## Screen -1: Login

**Purpose**: gates everything else behind a basic username/password check (decided 2026-08-31). Financial data shouldn't sit behind nothing but an obscure URL. A single shared login is enough for the prototype — one owner/user, no roles or permissions system.

**Layout**: centered card, app name, username field, password field, "Log In" button, an error message area for a failed attempt. No "forgot password" flow needed yet at this scale — revisit if that becomes a real need.

**Flow**: unauthenticated visit to any URL redirects to Login; a successful login lands on whatever the last-viewed screen was (or Reports by default for a first visit).

## Screen 0: Documents

**Purpose**: answers two questions the earlier design left silent — "did my files actually land?" and "where does an invoice's data live once it's uploaded?" Files still only get *into* the system via Google Drive (no in-app uploader, per the 2026-08-27 decision to keep upload traffic off the small droplet) — this screen is read/confirm visibility on top of that, not a replacement for it.

**Sync Now button** (top-right of screen, mirrored as a small icon+timestamp in the top bar so it's reachable from any screen): triggers the same pipeline (Drive check across all upload types + review-queue matching + report recompute — **no eBay API calls**, eBay sales data is a manual CSV upload for now, see CLAUDE.md's 2026-08-31 scope change) on demand — see "Manual sync trigger" in CLAUDE.md's Scheduling section. While running, the button reads "Syncing…" and is disabled; on completion it briefly shows a result ("Synced just now — 2 new files, 1 new Needs Review item") then settles into "Last synced: 6 min ago". A short cooldown after each run disables the button again, with a tooltip explaining why (protects the droplet from repeated OCR/parsing load across four upload types).

**Ingestion status cards** (top of screen, below Sync Now), one per Drive-fed source type, for the selected account/month. eBay Sales Export, Bank Statements, and Payoneer Exports are **fixed expectations** — exactly one is expected per account per month — so each of those three cards carries one of three states, not just a file count:
- **Uploaded** (green) — file found, with count + last synced timestamp.
- **Not yet uploaded** (neutral blue) — nothing found yet, but the period's H+7 deadline (see Scheduling in CLAUDE.md) hasn't passed. Normal, not urgent — uploads happen in monthly batches.
- **Missing — expected by [date]** (red) — the H+7 deadline for that period has passed and the file still hasn't appeared. This is the state that also keeps the report Provisional (see Report finalization status in CLAUDE.md) — a period with literally nothing uploaded must never look the same as a period that's actually clean, since an empty Review Queue means "nothing to review," not "reviewed."

Invoices & Proof of Purchase is the one card that is **not** a fixed expectation (count varies with actual purchase activity), so it only ever shows a file count + last synced timestamp, never a Missing state. Instead, see the "Unmatched COGS activity" callout below.

Each card has an "Open in Drive ↗" link to the actual folder, since that's still where the user drops new files.

**Invoice & Proof-of-Purchase records** (table below the cards, **consolidated across all accounts — corrected 2026-08-31**, not filtered by the account selector, since these purchases don't trace to one eBay account any more than COGS itself does — see Data sources & inputs in CLAUDE.md): unlike bank/Payoneer lines, invoices were previously only referenced implicitly (as something the auto-match engine checks against) with no actual place to see them. This table makes each invoice a first-class visible record:

| Column | Notes |
|---|---|
| Date | Invoice/receipt date |
| Vendor / Description | From OCR extraction (Tokopedia receipts, handwritten shop receipts, and BCA transfer confirmations all need to go through this — CLAUDE.md's confirmed sample set is intentionally messy) |
| Amount (IDR) | Extracted amount |
| Purpose | COGS Purchase / Consignment Purchase — informs which downstream account it can match against |
| Source File | Link to the underlying document in Drive |
| Status | "Parsed" (green) or "Needs Confirmation" (amber) — deliberately **not** the same label as the Review Queue's "Needs Review" (see below for why) |
| Matched | Whether a bank/Payoneer line has matched against this invoice yet, or "Awaiting payment match" |

**Row interaction**: clicking a Needs Confirmation invoice row expands an inline editor (same interaction pattern as Review Queue) with editable Date, Vendor/Description, Amount, and Purpose fields, plus Save. This is a deliberate parallel to the bank-line editor — same failure mode (unreliable OCR), same fix (human confirms before the data is trusted).

**Why "Needs Confirmation" here, not "Needs Review" (corrected 2026-08-31)**: a Review Queue row marked Needs Review blocks a report from going Final — it's an unclassified transaction. An unconfirmed invoice does **not** block Final (see Report finalization status in CLAUDE.md) — the underlying transaction is already correctly posted from the bank-side label; the invoice only adds traceability. Using the identical badge/wording for two states with different real consequences was a design flaw — someone triaging Needs Review items has no reason to know one type is urgent and the other isn't. Different label, same amber "needs a look" color (it still deserves attention, just not urgently).

**Why this doesn't compromise accounting safety even before it exists**: an invoice that fails to parse doesn't cause a wrong COGS number by itself — the bank line paying for it still falls through to the Review Queue as Needs Review if it can't auto-match, and a human labels it there regardless (per the "never silently guess" rule). What this screen adds is *traceability and confidence*, not a new safety mechanism: without it, a correctly-labeled COGS line has no visible link back to the actual invoice document, which fails the Definition of Done's "numbers are traceable" bar even when the amount itself is right.

**Unmatched COGS activity callout** (below the invoice table): a small panel listing Review Queue transactions labeled COGS Purchase or Consignment Payout that have no linked invoice record — e.g. "2 transactions labeled COGS have no matching invoice — [view in Review Queue]". This is informational, not a Final-status blocker (see Report finalization status in CLAUDE.md — the transaction is already correctly posted from the bank-side label; the invoice only adds traceability, it doesn't change the number). It exists so a documentation gap gets noticed close to when it happens, rather than months later when someone actually needs to trace a number back to its source.

## Screen 1: Review Queue

**Purpose**: the one place the user labels bank transactions the system couldn't confidently auto-match.

**Summary bar (top of screen)**: counts of Matched vs. Needs Review, broken out per account and month — not just per-row status, so the user can tell at a glance which period isn't ready without scanning every row (required by Report finalization status in CLAUDE.md).

**Filters**: Account, Month, Status (All / Matched / Needs Review), Source (Payoneer CSV / Bank PDF).

**Table columns**:
| Column | Notes |
|---|---|
| Date | Transaction date |
| Description | Raw text from OCR (bank PDF) or CSV (Payoneer) |
| Amount (IDR) | Primary figure; USD reference shown as secondary small text where applicable |
| Source | Icon/tag: Payoneer CSV vs. Bank PDF |
| Status | Badge: green "Matched" or amber "Needs Review" (an OCR extraction the system isn't confident it read correctly also counts as Needs Review) |
| Category | Read-only text for Matched rows; editable dropdown for Needs Review rows |
| Consignor/Item Ref | Optional free-text field, only relevant/shown when Category = Consignment Payout |
| Posted | Small checkmark + timestamp once the row has actually posted to the ledger; blank if labeled but not yet picked up by the next sync |

**Row interaction**: clicking a Needs Review row expands an inline editor with:
- Category dropdown: Revenue Settlement / COGS / Internal Transfer / Consignment Payout / Operating Expense / Owner's Draw / Other
- Consignor/Item Reference text field (appears only if Consignment Payout is selected)
- Save button — saving does **not** post immediately; it marks the row labeled and ready, and the next sync run picks it up and posts it (per the "no separate trigger" rule in Scheduling). The row shows a "Queued for next sync" microcopy until Posted flips on.

**Empty state**: "No bank statement uploaded yet for this account/month" when there are zero rows at all for the selected filter — must not look like an error, since a missing upload for a not-yet-closed month is expected, not a failure (per Definition of Done's "no data yet" requirement).

**What this screen must never do**: auto-post an unlabeled row as a guessed category, or let a row post twice. Both are hard rules from CLAUDE.md, not just UI polish — the Save button and Posted column exist specifically to make both visible and impossible to trigger accidentally.

## Screen 1.5: Consignor Payout Tiers (Settings)

**Purpose**: replaces the old Google Sheet home for the "Pasal 3" price-tiered consignor payout schedule (decided 2026-08-31 — see Core accounting rules in CLAUDE.md). A simple editable table, not a config file, so a rate change doesn't require touching code or SSHing into the droplet.

**Layout**: a table with two editable columns — price range (e.g. "$0.99–14.99") and rate (e.g. "72%") — one row per tier, plus a fixed, non-editable note for the $7,500+ tier ("No fixed rate — requires manual contact, never auto-applied," per CLAUDE.md). An "Add tier" / "Remove tier" affordance isn't needed for the prototype since the schedule is fixed at 6 tiers; simple inline edit-and-save on the rate values is enough.

**What this screen must never do**: silently apply an edited rate retroactively to already-posted consignment sales. A rate change here only affects the tier lookup for sales processed *after* the change — consistent with "every consignment sale uses the tier lookup with no override path" in CLAUDE.md, and with corrections to already-posted rows being out of scope for the prototype (see Definition of Done).

## Screens 2–5: Report views

All four share one layout: statement tabs across the top (Revenue / Cash Flow / P&L / Equity), the shared account/period selector and status badge from the global chrome, and a body that renders the statement.

**Traceability requirement**: every number on every report must be clickable, opening a drill-down panel listing the exact posted transactions that sum to it. This isn't a nice-to-have — CLAUDE.md's Definition of Done requires every report figure to be traceable back to source transactions, not a black-box total, so the drill-down is how that rule actually surfaces in the UI.

### Revenue (per account + consolidated)
Sales Revenue, less Sales Returns & Allowances, plus Consignment Commission Income shown as its own line (never blended into Sales Revenue). Account selector active; toggling between Account 1 and Consolidated re-renders the same layout.

### Cash Flow (per account + consolidated)
Per-account view: eBay Wallet → Payoneer Wallet → BCA Bridging → transfer-out to BCA Main, shown as a simple waterfall/list of movements for the period.

**Shared Payoneer wallet (clarified 2026-08-31)**: two of the three eBay accounts share one Payoneer wallet and one downstream BCA bridging account; the third has its own independent pair. This means the per-account Cash Flow view is only genuinely per-account through the eBay Wallet line — for the two accounts that share a wallet, everything from Payoneer onward is identical pooled-total data for both, not a per-account split (there's no non-arbitrary way to divide commingled cash, the same reasoning already applied to shared-shipping category costs). The view surfaces this honestly rather than hiding it: an inline banner appears right where the statement crosses from "genuinely per-account" to "shared pool" — "This Payoneer wallet is shared with Account 2. Everything from here down is the pooled total for both accounts." Switching the account selector between the two paired accounts changes the eBay Wallet line but leaves everything below the banner identical, which is the point — it makes the sharing visible instead of implying a precision that doesn't exist.

Consolidated view: same shape, but the inter-account transfer line is replaced with a visible "Inter-account transfers eliminated" note and the amount nets to zero — this is the one place in the UI that visually confirms the elimination rule is actually being applied, not just trusted. Consolidated also has to **dedupe the shared wallet-group's Payoneer figures** rather than sum each account's displayed number — since the two paired accounts show the identical shared-pool total, naively adding all three accounts' Payoneer-stage figures would double-count that pool. The consolidated elimination note calls this out explicitly, not just the inter-account transfer elimination.

### P&L (consolidated only — account selector disabled/greyed)
Revenue → COGS → Gross Profit → Operating Expenses (eBay Selling Fees, Payout Fee, Payroll, General Operating Expenses) → Operating Income → Other Income/Expense (Realized FX Gain/Loss, Unrealized FX Gain/Loss, kept as two distinct lines, never blended) → Net Income.

### Equity (consolidated only — account selector disabled/greyed)
Owner's Capital, Owner's Draw, Retained Earnings, ending balance for the period.

## Primary user flows

1. **Monthly review flow**: user opens the app → sees an amber "Provisional — 3 items need review" badge → clicks it → lands on Review Queue pre-filtered to those 3 rows → labels each → sees "Queued for next sync" on each → (next scheduled sync runs) → badge flips toward Final as rows clear.
2. **Checking a number**: user is looking at Consolidated P&L → clicks the COGS figure → drill-down panel opens listing the invoices/purchases that sum to it → user closes the panel, confident the number isn't a black box.
3. **Comparing per-account vs. consolidated**: user is on Cash Flow → switches the account selector from Account 1 to Consolidated → sees the same statement shape, but with the inter-account transfer visibly eliminated instead of shown as an outflow.
4. **Confirming an upload landed**: user drops this month's bank PDF and invoices into the usual Drive folders → opens Documents → clicks Sync Now rather than waiting for the next scheduled run → watches the button read "Syncing…" → sees the ingestion cards update (file counts, "last synced" timestamps) → doesn't need to open Drive again to confirm the system actually picked them up.
5. **Fixing a bad invoice OCR read**: user opens Documents → sees a handwritten receipt came in as "Needs Confirmation" with a garbled amount → expands the row → corrects Date/Vendor/Amount/Purpose → Saves → the invoice is now available for the auto-match engine to use against the corresponding bank line.
6. **Noticing a missing document**: it's 8 days after month-end; the user opens the app and sees "Provisional — Bank Statement not yet received" instead of the usual review-item count → goes to Documents → sees the Bank Statement card in red, "Missing — expected by Aug 7" → realizes they forgot to upload it this month, drops it into Drive, hits Sync Now, watches the card flip to Uploaded.

## Open items this design surfaces for the backend (feeds milestone 2)

- Review-queue rows need a `posted_at` marker (nullable) distinct from `labeled_at`, so the UI can show three real states (Needs Review / Labeled-queued / Posted) not just two.
- Every ledger transaction needs enough structure to answer "which transactions sum to this report line" on demand — the drill-down isn't a separate reporting feature, it's a query against the same ledger data.
- The consignor/item reference field needs to exist on the transaction record even though it never rolls up anywhere (already specified in CLAUDE.md; the design confirms where it's actually entered — the Review Queue row editor).
- Invoices need their own table (not just raw files) — the same shape as review-queue rows: extracted fields, a Parsed/Needs-Review status, a human-correctable state, and a link back to the source file in Drive. CLAUDE.md's auto-match rule (b) already assumes this data exists somewhere; this design is what makes that explicit.
- The report drill-down for COGS needs to join through to the specific invoice record(s), not just the bank transaction that paid them, or the traceability chain stops one hop short of the actual source document.
- The backend needs an on-demand sync endpoint (not just the scheduled job) with basic cooldown/rate-limiting state, since the Sync Now button calls the identical pipeline outside the automatic window.
- Final-status logic needs a second input beyond "review-queue rows all resolved": a per-account/period check for whether the fixed-expectation documents (bank statement, Payoneer export) have actually been ingested at all. This is a schema/logic requirement for milestone 2, not just a milestone-5 UI detail — the ledger/review-queue data model needs a way to represent "this document type was expected for this period and never arrived," distinct from "arrived and fully reviewed."
- The "expected by" deadline for a document is derived from the H+7 end of that period's sync window (see Scheduling in CLAUDE.md) — this calculation needs to exist in the backend, not be hardcoded per screen.
- The schema needs a **wallet-group** entity that a Payoneer Wallet and BCA Bridging Account belong to, with eBay accounts referencing a wallet-group many-to-one — not a hardcoded 1:1 eBay-account-to-Payoneer-wallet assumption (see Business model and Chart of accounts, clarified 2026-08-31). Per-account Cash Flow queries need to know which lines are genuinely per-account (eBay Wallet) vs. shared (everything from Payoneer onward for a shared wallet-group), and Consolidated Cash Flow must aggregate by wallet-group, not by summing each eBay account's displayed figures, or a shared pool gets double-counted.
