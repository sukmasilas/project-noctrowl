# Builder Brief — Milestone 2: Core Ledger Engine

From Main-agent, per the Agent workflow in `CLAUDE.md`. This supersedes any schema/model decisions made before `docs/design/schema-design.md` existed — if you've already written models or migrations that conflict with that doc, stop and report the conflict rather than reconciling by guessing.

## Goal
Implement the Postgres schema, chart of accounts, and double-entry posting engine exactly as specified in `docs/design/schema-design.md`, plus the money-math business rules below that the schema's detail tables (`consignment_sales`, `payoneer_withdrawals`, `fx_revaluations`) exist to support.

## Scope boundaries — do NOT build yet
- No eBay API integration of any kind (deferred indefinitely — see CLAUDE.md's Data sources & inputs and Build milestones). No `ebay/api_client.py`-style module, no live sync.
- **No Google Sheets client or Sheets API usage anywhere.** This architecture was scrapped 2026-08-27. If you're carrying forward any code from the old `project-noctrowl` folder, do not bring over `sheets/` — it must not exist in this project.
- No Google Drive integration, no OCR, no file ingestion.
- No web app / HTTP framework / routes.
- No Review Queue, Invoices, or Documents-tracking tables — those are milestone 3 (previewed at the bottom of `schema-design.md` for context only).
- No login/auth (milestone 4).
- No reversal/correction logic beyond the schema's placeholder field (`journal_entries.reversed_by_id`) — corrections to posted transactions are explicitly out of scope for the prototype.
- No per-account P&L, no category-level P&L, no allocation logic for shared shipping costs across categories.

## Schema — implement exactly as specified in `docs/design/schema-design.md`
Tables: `wallet_groups`, `ebay_accounts`, `account_types`, `accounts`, `categories`, `journal_entries`, `journal_lines`, `consignment_sales`, `payoneer_withdrawals`, `fx_revaluations`, `consignor_payout_tiers`.

Key constraints to actually enforce, not just document:
- `accounts`: partial unique indexes so an `account_type` with `scope_kind = per_wallet_group` can't get two rows for the same `wallet_group_id` (and same for `per_ebay_account`) — this is what makes "2 Payoneer wallets for 3 eBay accounts" a real guarantee, not a convention.
- `journal_lines`: for every `journal_entry_id`, `SUM(debit_amount_idr) = SUM(credit_amount_idr)`. Enforce this in the posting engine (reject an unbalanced entry before it's committed), and cover it with a unit test that deliberately tries to post an unbalanced entry and expects a rejection.
- `consignment_sales.journal_entry_id` must stay NULL until `confirmed_at` is set — a consignment payout must never auto-post from the tier lookup alone.
- Money columns are `NUMERIC`, never float.

## Business rules the posting engine must implement
Reference: Core accounting rules and Chart of accounts in `CLAUDE.md`.
- **COGS timing**: stock and pre-order models both expense COGS at time of purchase/shipment — never held as an inventory asset. Same posting logic for both; the only difference is timing relative to the sale.
- **Consignment**: liability accrual at time of sale (credit Consignor Payable), cleared on reimbursement. Two payout models coexist: the Pasal 3 tier lookup (`consignor_payout_tiers`, item price excludes shipping) and the experimental gross-minus-fees-minus-shipping model (seller earns $0 explicit commission on these). Which model applies is a manual, per-transaction decision — do not build any auto-selection logic. No consignment payout may post without `confirmed_at` set on `consignment_sales`.
- **Inter-account transfers**: post as `journal_entries.source_type = 'inter_account_transfer'` — never as income or expense.
- **FX**: sales book at the Kurs Pajak rate on the booking date (reference field only, `journal_lines.fx_rate_used`). Realized FX gain/loss splits into two distinct lines at Payoneer withdrawal — Payout Fee (opex) and FX spread (Realized FX Gain/Loss) — using Payoneer's own stated fee and exchange-rate-excluding-fee figures (`payoneer_withdrawals` table), never inferred. Month-end unrealized FX revaluation on unwithdrawn Payoneer balances uses Kemenkeu's end-of-month rate (`fx_revaluations` table) and must never be conflated with the realized figure.
- **Revenue**: gross of eBay fees. eBay Selling Fees post as their own opex line, never netted against revenue.
- **Category tagging**: `journal_lines.category_id` from `{TCG, Watches, Auto Parts, Toys & Collectibles}` on revenue lines only — analytics reference field, never used to allocate any shared cost.
- **Wallet-group structure**: eBay Wallet accounts are per `ebay_account`; Payoneer Wallet and BCA Bridging Account are per `wallet_group`, which one or more eBay accounts reference many-to-one. Do not hardcode a 1:1 assumption anywhere in the posting logic.

## Acceptance criteria
Hand-crafted test transactions (per CLAUDE.md milestone 2: "verified against hand-crafted test transactions and unit tests only") covering at minimum:
1. A stock-model sale (COGS at purchase, separate transaction from the sale).
2. A pre-order sale (COGS posted after the sale, at purchase/shipment).
3. A consignment sale under the Pasal 3 tier model, confirmed and posted.
4. A consignment sale under the experimental net-of-fees-and-shipping model, confirmed and posted, showing $0 Consignment Commission Income.
5. An attempted consignment payout with `confirmed_at` still NULL — must be rejected/blocked from posting.
6. A refund posting to Sales Returns & Allowances (contra-revenue), not netted into Sales Revenue.
7. An inter-account transfer between two `BCA Main Account`-adjacent accounts, confirmed as non-P&L.
8. A Payoneer withdrawal with realized FX gain/loss correctly split into Payout Fee (opex) and FX spread (Realized FX Gain/Loss), using stated fee/rate figures.
9. A month-end unrealized FX revaluation on an unwithdrawn balance, correctly separate from #8.
10. A deliberately unbalanced journal entry — must be rejected by the posting engine.
11. At least one test exercising the wallet-group structure directly: two eBay accounts sharing a wallet-group cannot each get their own Payoneer Wallet account row.

Unit test coverage for all of the above, not just a manual script. No hardcoded credentials — `DATABASE_URL` via env var, per `.env.example` already in the repo root.

## Report to QA before calling anything done
Per the Agent workflow — send this to QA once implemented; do not mark milestone 2 complete unilaterally. QA will check debits-equal-credits enforcement, wallet-group non-duplication, consignment confirmation gating, FX split correctness, and basic test coverage, per `.claude/agents/qa.md`.

## Flag rather than guess
- If anything about the schema doesn't hold once you're actually writing the posting logic (e.g., a rule that's ambiguous in practice), stop and report back to Main-agent rather than silently deviating from `schema-design.md`.
- `journal_lines.category_id` is modeled at the line level, not the journal-entry header, on the assumption a single order could in principle mix categories across its items. If implementing this turns out to be awkward or the assumption seems wrong, flag it — don't silently move it to the header.
