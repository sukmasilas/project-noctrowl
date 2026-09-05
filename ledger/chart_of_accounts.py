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
