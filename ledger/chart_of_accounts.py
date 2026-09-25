"""The chart of accounts, exactly as listed in CLAUDE.md's "Chart of
accounts" section (including "Shipping Cost", added for the experimental
consignment payout model), shaped to match docs/design/schema-design.md's
``account_types`` table.

Do NOT add an account here that isn't in CLAUDE.md's list. If implementation
reveals a genuine need for one not listed, that's a stop-and-report-back
case, not a silent addition.

Each entry: (code, name, statement_section, normal_balance, scope_kind, is_contra)

- statement_section: 'asset' / 'liability' / 'equity' / 'revenue' / 'cogs' /
  'opex' / 'other_income_expense' — per schema-design.md, Sales Returns &
  Allowances stays in the 'revenue' section but is flagged is_contra=True
  (a natural-debit-balance line that nets against revenue), rather than
  getting its own statement_section.
- scope_kind: how many ``accounts`` rows this type gets instantiated into:
    - per_ebay_account  -> one per eBay account (3 total: eBay Wallet)
    - per_wallet_group  -> one per wallet-group (2 total: Payoneer Wallet,
      BCA Bridging Account) — NOT 1:1 with eBay accounts.
    - consolidated      -> exactly one instance, business-wide.
"""

ACCOUNT_TYPES = [
    # Assets
    ("EBAY_WALLET", "eBay Wallet", "asset", "debit", "per_ebay_account", False),
    ("PAYONEER_WALLET", "Payoneer Wallet", "asset", "debit", "per_wallet_group", False),
    ("BCA_BRIDGING", "BCA Bridging Account", "asset", "debit", "per_wallet_group", False),
    ("BCA_MAIN", "BCA Main Account", "asset", "debit", "consolidated", False),
    # Added 2026-09-10 — the company gives no-interest loans to employees,
    # repaid via a salary deduction over a fixed number of months (e.g. the
    # real Fariz Pradana loan: Rp 27,000,000 disbursed 2026-08-17, repaid
    # Rp 1,500,000/month for 18 months, no interest). Tracked as ONE
    # aggregate asset account (same pattern as CONSIGNOR_PAYABLE's one
    # aggregate liability) — a per-transaction employee-name reference is
    # retained on the posted journal line for traceability (reusing the
    # existing generic consignor_item_ref field, same as how rule (b)
    # already reuses it for "invoice:<id>" on a non-consignor match), not a
    # full per-employee GL sub-ledger. See webapp/settings_bp.py's Employee
    # Loans screen for the admin-editable tracking table (loan amount,
    # installment, start date) that sits alongside this account, and
    # ingestion/matching.py's 'employee_loan_disbursement'/'payroll'
    # categories for how it's posted to.
    ("EMPLOYEE_LOAN_RECEIVABLE", "Employee Loan Receivable", "asset", "debit", "consolidated", False),
    # Liabilities
    ("CONSIGNOR_PAYABLE", "Consignor Payable", "liability", "credit", "consolidated", False),
    # Equity
    ("OWNERS_CAPITAL", "Owner's Capital", "equity", "credit", "consolidated", False),
    ("OWNERS_DRAW", "Owner's Draw", "equity", "debit", "consolidated", False),
    ("RETAINED_EARNINGS", "Retained Earnings", "equity", "credit", "consolidated", False),
    # Revenue
    ("SALES_REVENUE", "Sales Revenue", "revenue", "credit", "consolidated", False),
    (
        "CONSIGNMENT_COMMISSION_INCOME",
        "Consignment Commission Income",
        "revenue",
        "credit",
        "consolidated",
        False,
    ),
    (
        "SALES_RETURNS_ALLOWANCES",
        "Sales Returns & Allowances",
        "revenue",
        "debit",
        "consolidated",
        True,  # is_contra
    ),
    # Cost of Goods Sold
    ("COGS", "Cost of Goods Sold", "cogs", "debit", "consolidated", False),
    # Operating Expenses
    ("EBAY_SELLING_FEES", "eBay Selling Fees", "opex", "debit", "consolidated", False),
    ("PAYOUT_FEE", "Payout Fee", "opex", "debit", "consolidated", False),
    ("PAYROLL", "Payroll", "opex", "debit", "consolidated", False),
    ("GENERAL_OPEX", "General Operating Expenses", "opex", "debit", "consolidated", False),
    ("SHIPPING_COST", "Shipping Cost", "opex", "debit", "consolidated", False),
    # Added 2026-09-05 — the outside IT contractor paid per-listing to
    # create eBay listings is a real, recurring labor/service cost, but paid
    # to a contractor rather than a salaried employee, so it doesn't belong
    # under PAYROLL. Not COGS either (SAK Indonesia / SAK EMKM both treat
    # listing/promotional-type activity as a period expense, never
    # inventory-preparation cost — the same reasoning that already keeps
    # eBay's Promoted Listings fee out of COGS). Given its own line for
    # visibility, same reasoning as SHIPPING_COST/INTEREST_INCOME above.
    ("CONTRACT_LABOR", "Contract Labor", "opex", "debit", "consolidated", False),
    # Added 2026-09-10 — some real Shopee/Tokopedia (and possibly other
    # vendor) purchases are for packaging supplies (boxes, bubble wrap, poly
    # mailers, etc.), not inventory items, and shouldn't dilute either COGS
    # or the generic GENERAL_OPEX catch-all — a real, recurring cost the
    # user wants separately visible, same reasoning already applied to
    # SHIPPING_COST/CONTRACT_LABOR. Deliberately NOT given a keyword
    # auto-match rule: the same Shopee/Tokopedia bank line could be either
    # an item purchase or packaging supplies (or something else) and can't
    # be told apart from the raw description alone — stays human-judgment,
    # per-transaction, in the Review Queue.
    ("PACKAGING_SUPPLIES", "Packaging Supplies", "opex", "debit", "consolidated", False),
    # Added 2026-09-24 — a real, roughly-monthly recurring cost: the business
    # periodically pays for a team meal (e.g. a QRIS/QR-code debit to a local
    # cafe — the real trigger, a -Rp 520,000 "MLINJO CAF" line, is a
    # confirmed team meal, not a one-off). Not PAYROLL (not salary/
    # compensation), not COGS, not CONTRACT_LABOR. Given its own line, same
    # reasoning as SHIPPING_COST/CONTRACT_LABOR (a recurring cost the user
    # wants separately visible on the P&L rather than buried in
    # GENERAL_OPEX). Deliberately NO keyword auto-match rule — same
    # reasoning as PACKAGING_SUPPLIES: a QR/debit line to a cafe or
    # restaurant could plausibly be something else (a business meeting, a
    # different kind of expense) with no way to tell from the raw bank line
    # alone, so this stays a human-judgment, per-transaction category in the
    # Review Queue, never auto-matched.
    ("STAFF_MEALS_WELFARE", "Staff Meals & Welfare", "opex", "debit", "consolidated", False),
    # Other Income / Expense
    ("REALIZED_FX", "Realized FX Gain/Loss", "other_income_expense", "credit", "consolidated", False),
    ("UNREALIZED_FX", "Unrealized FX Gain/Loss", "other_income_expense", "credit", "consolidated", False),
    # Added 2026-08-31 (milestone 3) — bank-credited interest (BUNGA) on the
    # BCA Main Account, found in the real bank statement sample. Booked NET
    # of the small withholding tax deducted at source (PAJAK BUNGA) per
    # Main-agent's decision: both figures are immaterial, and netting avoids
    # a rounding-level opex line for the tax portion. See
    # ledger.posting.post_interest_income.
    ("INTEREST_INCOME", "Interest Income", "other_income_expense", "credit", "consolidated", False),
    # Added 2026-09-05 — the catch-all counterpart to GENERAL_OPEX, but for
    # genuine INFLOWS under the review-queue's 'other' category (a bank line
    # that doesn't confidently fit any of the named categories — see
    # ingestion.matching._DIRECTIONAL_CATEGORY_SIGNS's note on why 'other' is
    # deliberately bidirectional). Concrete real case: journal_entry_id=917 /
    # review_queue.id=321 — a real +Rp 50,000 inflow (the account owner
    # moving his own money from a personal DANA e-wallet into the Bridging
    # Account) that has nowhere correct to post, since every OTHER
    # 'other_income_expense'/revenue account here is either outflow-shaped
    # (GENERAL_OPEX) or scoped to a specific named event (interest, realized/
    # unrealized FX). This is a NEUTRAL pass-through, not an Owner's
    # Contribution — see ledger.posting.post_income_line and
    # ingestion.matching._post_one_row's 'other' branch.
    ("OTHER_INCOME", "Other Income", "other_income_expense", "credit", "consolidated", False),
]

# Account types whose currency is USD (eBay/Payoneer wallets); everything
# else (BCA accounts and all consolidated P&L/equity accounts) is IDR.
USD_ACCOUNT_TYPE_CODES = {"EBAY_WALLET", "PAYONEER_WALLET"}

# Category tag values — revenue-analytics reference only (see CLAUDE.md's
# Category tagging section). "Toys & Collectibles" confirmed 2026-08-31 as
# a real fourth product line.
CATEGORIES = ["TCG", "Watches", "Auto Parts", "Toys & Collectibles"]

# The "Pasal 3" consignor payout tier schedule from CLAUDE.md's Core
# accounting rules. rate_percent is a percentage (e.g. 72.00 = 72%), and is
# NULL with requires_manual_contact=True for the $7,500+ tier — "no fixed
# rate, requires manual contact — never auto-applied".
CONSIGNOR_PAYOUT_TIERS = [
    # (min_price_usd, max_price_usd, rate_percent, requires_manual_contact, display_order)
    ("0.99", "14.99", "72.00", False, 1),
    ("15.00", "49.99", "78.00", False, 2),
    ("50.00", "99.99", "80.00", False, 3),
    ("100.00", "2499.99", "82.00", False, 4),
    ("2500.00", "4999.99", "83.00", False, 5),
    ("5000.00", "7499.99", "85.00", False, 6),
    ("7500.00", None, None, True, 7),
]
