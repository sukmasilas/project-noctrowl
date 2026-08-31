# Sample documents

Real (anonymize if needed before sharing further) documents for Builder to test CSV/PDF/OCR parsing and matching logic against during milestone 3 — per `CLAUDE.md`'s rule of not building extraction/matching logic from assumptions about format alone. These are **not** production data storage (that's Google Drive, per the Architecture section) — this folder exists purely as local test fixtures for the build process.

## Inventory

### ✅ Bank Statements — `bank-statements/`
- `BCA Bank_1790345891_APR_2026.pdf` — carried over from the old `project-noctrowl` folder.
- **Check before milestone 3**: confirm which real account/wallet-group this statement actually belongs to (the prototype's chosen account, its wallet-group's bridging account, or the consolidated Master Account) — CLAUDE.md now has two distinct bank-statement roles (wallet-group bridging account vs. Master Account) that may end up needing separate samples if their real layouts differ. If both are BCA-issued with the same layout, this one sample is enough to build the OCR/parsing format against; if not, a second sample would help.

### ✅ Payoneer — `payoneer/`
- `Payoneer_Transactions_04-2026.csv` — the structured CSV export (Source/Target/Reference ID/Store Name/Additional Description columns per CLAUDE.md).
- `Payoneer_Confirmation_of_Transfer_4366185623014087.pdf` — a withdrawal confirmation, which is specifically what the realized FX gain/loss rule needs (states the payout fee and "exchange rate excluding fee" explicitly — see Core accounting rules in CLAUDE.md).
- Both carried over from the old `project-noctrowl` folder.

### ✅ Invoices & Proof of Purchase — `invoices-proof-of-purchase/`
- `Invoice Sample _ Tokopedia.pdf` — structured receipt (high-confidence OCR case).
- `Invoice Sample_Direct Invoice from shop.jpeg` — handwritten receipt (low-confidence / Needs Confirmation case).
- `Proof of Transfer Sample_BCA.jpeg` — screenshot-style bank transfer confirmation (a third format entirely).
- All three carried over from the old `project-noctrowl` folder — this is the intentionally mixed-reliability set already referenced in CLAUDE.md.

### ✅ eBay Sales Export — `ebay-sales-export/` (received 2026-08-31, checked)
`Transaction_report_20260701_20260731.csv` — a Seller Hub **Transaction report** (not a simple Orders export) for July 2026. Checked against the three criteria:

- **Gross vs. fees — passes, better than expected.** There's a `Gross transaction amount` column and a `Net amount` column, and fees are broken out into multiple separate columns (`Final Value Fee - fixed`, `Final Value Fee - variable`, `Regulatory operating fee`, `International fee`, `Deposit processing fee`, etc.) rather than one blended fee figure. Revenue can post gross with fees itemized, not just a single lump "eBay Selling Fees" number.
- **Category data — confirmed absent.** No category ID or item specifics column anywhere in the export. Only `Item title` (free text) and `Custom label` (SKU field — see consignment note below). Item titles alone span what look like TCG cards, watches (Seiko), *and* dolls/Hot Wheels — confirmed 2026-08-31 as a real fourth category, "Toys & Collectibles" (smaller volume than the three main categories, per CLAUDE.md). Either way: **category tagging cannot be pulled automatically from this export** — confirms the open question in CLAUDE.md. It'd have to wait for API sync (deferred) or a different report type, if one exists that carries category data.
- **Variety — good.** The file isn't sale-only rows — it's a full transaction ledger with `Type` = Order, Refund, Hold (placed *and* released as a matched pair), Other fee (Promoted Listings + a Store subscription fee), and Payout. Multiple refunds are present. **No `CONSIGN-` prefixed listing appears** — the `Custom label` column is blank on every single row in this file, so the consignment-SKU-detection path has zero real coverage from this sample. Not a blocker (defaults to normal-sale treatment either way, per CLAUDE.md), but Builder will be testing that path against a synthetic row, not a real one, unless a consignment sale shows up in a different month.

**Structural note for Builder**: this being a transaction report rather than an orders report means ingestion has to branch on the `Type` column — `Order` rows carry the sale/fee detail, `Refund` rows carry the contra-revenue detail, `Hold`/`Payout`/`Other fee` rows aren't sales at all and need separate handling (a `Hold placed` + `Hold released` pair nets to zero and shouldn't double-post). The `Payout ID` column also lines up with Payoneer's own transaction/payout references, which may help matching logic in milestone 3.

## Not needed yet
Purchase invoices for Consignment Purchase specifically (as opposed to COGS Purchase) aren't separately required — the existing proof-of-transfer sample already covers the "paying someone via bank transfer" OCR case regardless of which ledger purpose it ends up tagged with.
