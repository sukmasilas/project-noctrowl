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

### ❌ eBay Sales Export — `ebay-sales-export/` (empty — still needed)
This is the one genuinely missing piece. It was never collected before because the original plan assumed eBay API sync; now that eBay sales data is a manual CSV upload (see CLAUDE.md's 2026-08-31 scope change), Builder can't build or test the parser without a real export.

**What to grab**: an actual monthly sales/orders export from eBay Seller Hub (or whichever report you'd actually use month to month) for the account you're choosing as the prototype's "Account 1." Before handing it over, it's worth opening it yourself and checking:
- Does it show **gross sale price and eBay fees as separate columns** (not just a blended net payout)? Revenue must post gross — if this export only has net figures, that's a real problem to flag, not something to work around silently.
- Does it include **category ID or item specifics** anywhere? This determines whether category tagging is even possible from this data source, or has to wait for API sync.
- Does the export cover a period with **some variety** — at least one normal stock sale, ideally one listing using the `CONSIGN-` SKU prefix if any exist, and a refund/return if one happened that month. A sample with only one transaction type gives Builder much thinner test coverage than a real month usually has.

## Not needed yet
Purchase invoices for Consignment Purchase specifically (as opposed to COGS Purchase) aren't separately required — the existing proof-of-transfer sample already covers the "paying someone via bank transfer" OCR case regardless of which ledger purpose it ends up tagged with.
