"""Report aggregation queries: Revenue, Cash Flow, P&L, Equity, and the
shared drill-down mechanism.

Nothing here is a second data-access layer — every function is a plain
SQLAlchemy Core query against ``ledger.schema``'s existing tables
(``journal_lines``/``journal_entries``/``accounts``/``account_types``), the
same tables ``ledger/posting.py`` writes to. See
docs/design/milestone-4-web-app-design.md §3 for the screen-by-screen
sketch this implements.

Per-account revenue attribution (design doc open question #1, resolved by
Main-agent 2026-09-01): attribute a journal_entry to the eBay account whose
EBAY_WALLET line it touches — NOT via a join through
``ingestion.ebay_csv_posted_transactions`` (an ingestion-layer idempotency
table, not an attribution source). This holds cleanly for every sale/
consignment-sale entry (post_ebay_sale/post_consignment_sale always include
an EBAY_WALLET line in the same entry) and for eBay-Wallet-stage refunds
(post_refund(stage='ebay_wallet') also touches EBAY_WALLET directly).

VALIDATION FINDING (flagged per the brief's "validate this... flag it back
if something doesn't hold cleanly" instruction): it does NOT hold directly
for Payoneer-stage refund/fee-credit entries
(post_refund(stage='payoneer'), post_refund_fee_credit(stage='payoneer'))
— those touch only the wallet-group-scoped PAYONEER_WALLET account, never a
per-account EBAY_WALLET line, so there is no direct per-account line to
read off. Resolved here with a fallback that still uses only ledger data
(no ingestion-table dependency, preserving the exact principle Main-agent's
resolution was built on): every such entry carries the same
``ebay_order_ref`` the ORIGINAL sale was tagged with (post_refund and
post_refund_fee_credit both accept and pass through ebay_order_ref), and
that original sale's entry DOES have a direct EBAY_WALLET line. So
attribution is two-step: (1) does this entry touch a per-account line
directly? use that. (2) otherwise, does it carry an ebay_order_ref that
some OTHER entry resolves directly via step 1? use that entry's account.
(3) otherwise, unattributable (excluded from every per-account view, never
silently included in the wrong account's numbers).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.balances import account_balance_before as _shared_account_balance_before
from ledger.balances import account_balance_through as _shared_account_balance_through
from ledger.schema import account_types, accounts, ebay_accounts, journal_entries, journal_lines, wallet_groups

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Attribution (per-account Revenue)
# ---------------------------------------------------------------------------


def _entry_direct_account_map(conn: Connection, entry_ids: set[int]) -> dict[int, int]:
    if not entry_ids:
        return {}
    rows = conn.execute(
        select(journal_lines.c.journal_entry_id, accounts.c.ebay_account_id)
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .where(journal_lines.c.journal_entry_id.in_(entry_ids))
        .where(accounts.c.ebay_account_id.isnot(None))
        .distinct()
    ).all()
    return {r.journal_entry_id: r.ebay_account_id for r in rows}


def _order_ref_account_map(conn: Connection, order_refs: set[str]) -> dict[str, int]:
    if not order_refs:
        return {}
    rows = conn.execute(
        select(journal_lines.c.ebay_order_ref, accounts.c.ebay_account_id)
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .where(journal_lines.c.ebay_order_ref.in_(order_refs))
        .where(accounts.c.ebay_account_id.isnot(None))
        .distinct()
    ).all()
    out: dict[str, int] = {}
    for r in rows:
        out.setdefault(r.ebay_order_ref, r.ebay_account_id)
    return out


def attribute_entries_to_ebay_account(conn: Connection, entry_rows) -> dict[int, int | None]:
    """``entry_rows`` is any iterable of objects with ``.journal_entry_id``
    and ``.ebay_order_ref`` attributes (a plain query result works).
    Returns {journal_entry_id: ebay_account_id or None (unattributable)}.
    """
    entry_ids = {r.journal_entry_id for r in entry_rows}
    direct = _entry_direct_account_map(conn, entry_ids)

    unresolved_refs = {
        r.ebay_order_ref for r in entry_rows if r.journal_entry_id not in direct and r.ebay_order_ref
    }
    order_map = _order_ref_account_map(conn, unresolved_refs)

    result: dict[int, int | None] = {}
    for r in entry_rows:
        if r.journal_entry_id in direct:
            result[r.journal_entry_id] = direct[r.journal_entry_id]
        elif r.ebay_order_ref and r.ebay_order_ref in order_map:
            result[r.journal_entry_id] = order_map[r.ebay_order_ref]
        else:
            result[r.journal_entry_id] = None
    return result


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------

REVENUE_CODES = ("SALES_REVENUE", "SALES_RETURNS_ALLOWANCES", "CONSIGNMENT_COMMISSION_INCOME")


@dataclass
class RevenueReport:
    period_month: _dt.date
    ebay_account_id: int | None
    sales_revenue_idr: Decimal
    returns_allowances_idr: Decimal  # positive magnitude (a deduction)
    consignment_commission_idr: Decimal
    net_revenue_idr: Decimal
    unattributed_count: int  # revenue-relevant lines that couldn't be attributed (data-integrity flag)


def _revenue_lines(conn: Connection, period_month: _dt.date):
    return conn.execute(
        select(
            journal_lines.c.journal_entry_id,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            account_types.c.code,
        )
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.code.in_(REVENUE_CODES))
        .where(journal_entries.c.period_month == period_month)
    ).all()


def revenue_report(conn: Connection, *, period_month: _dt.date, ebay_account_id: int | None = None) -> RevenueReport:
    rows = _revenue_lines(conn, period_month)
    attribution = attribute_entries_to_ebay_account(conn, rows) if rows else {}

    totals: dict[str, Decimal] = {code: ZERO for code in REVENUE_CODES}
    unattributed = 0
    for r in rows:
        if ebay_account_id is not None:
            attributed = attribution.get(r.journal_entry_id)
            if attributed is None:
                unattributed += 1
                continue
            if attributed != ebay_account_id:
                continue
        totals[r.code] += r.credit_amount_idr - r.debit_amount_idr

    sales_revenue = totals["SALES_REVENUE"]
    returns_allowances = -totals["SALES_RETURNS_ALLOWANCES"]  # debit-normal -> flip sign to a positive magnitude
    commission = totals["CONSIGNMENT_COMMISSION_INCOME"]
    net_revenue = sales_revenue - returns_allowances + commission

    return RevenueReport(
        period_month=period_month,
        ebay_account_id=ebay_account_id,
        sales_revenue_idr=sales_revenue,
        returns_allowances_idr=returns_allowances,
        consignment_commission_idr=commission,
        net_revenue_idr=net_revenue,
        unattributed_count=unattributed,
    )


# ---------------------------------------------------------------------------
# Statement of Cash Flows (consolidated only, direct method) — replaces the
# old per-wallet-stage movement list that used to live here (superseded per
# Main-agent's brief; that UI shape was already effectively repurposed into
# the Wallet screen in an earlier phase). See module docstring's cash-flow
# section for the standards this follows (PSAK 207 / IAS 7 direct method).
#
# --- The core technique: per-JOURNAL-LINE classification, not per-entry ---
# "Cash" here = every account under statement_section='asset' (EBAY_WALLET,
# PAYONEER_WALLET, BCA_BRIDGING, BCA_MAIN — confirmed to be exactly and only
# those four; see ledger/chart_of_accounts.py). For any journal entry, since
# debits always equal credits (enforced by both posting.py and a Postgres
# trigger — see ledger/schema.py), splitting an entry's lines into a "cash"
# set C and a "non-cash" set N always gives sum_C(debit-credit) =
# -sum_N(debit-credit) = sum_N(credit-debit). That identity is exactly what
# a direct-method statement wants: for every NON-cash line touched by a cash
# -moving entry, (credit-debit) is that line's signed contribution to
# whichever cash-flow category its account represents (positive = an
# inflow-associated bucket, negative = outflow-associated) — and because the
# identity holds per-entry, the sum of every non-cash line's contribution,
# grouped into buckets, is GUARANTEED to equal the total change in cash for
# the period. This is what makes Beginning + Net Change = Ending tie out
# exactly (see cash_flow_statement()'s own difference_idr field, the same
# "surfaced, never papered over" pattern as BalanceSheetReport.difference_idr
# above), and it requires no special-casing for embedded amounts (e.g. the
# eBay Selling Fee debited inside the SAME entry as an ebay_sale's wallet
# credit, never a separate cash movement of its own) — the fee line still
# gets its own correct bucket contribution purely from being a non-cash line.
#
# ONE deliberate exception to "classify every non-cash line by its account
# type alone": CONSIGNOR_PAYABLE (a liability) is credited at the moment a
# consignment sale accrues (real cash — the FULL buyer payment, including
# the portion owed to the consignor — lands in the eBay Wallet at that
# moment) and debited later when the consignor is actually reimbursed (a
# real, separate cash outflow). These are economically two different
# cash-flow events sharing one account type, distinguished by which side of
# the line they're on — not by source_type (see _CONSIGNOR_PAYABLE_BY_SIDE
# below). This is standard treatment for agency/pass-through cash receipts
# in a direct-method statement: gross cash collected on a third party's
# behalf is real "cash received from customers" when it lands, and the
# later pass-through payment is its own operating outflow line when it
# actually happens — not held back until reimbursement, and not shown as a
# strange negative "paid to consignors" figure at accrual time.
#
# TWO deliberate, explicit EXCLUSIONS from Operating/Investing/Financing
# (per Main-agent's brief):
#   - source_type='opening_balance' entries (post_opening_balance) — these
#     represent a pre-existing balance from before ledger-tracking began,
#     not a period Financing activity; excluded from the O/I/F line-item
#     query entirely (the only account type they ever touch besides a cash
#     account is OWNERS_CAPITAL) so they only ever affect Beginning Cash
#     (via account_balance_before(), which already special-cases them — see
#     ledger/balances.py), never appear as a spurious Financing inflow line.
#   - UNREALIZED_FX (post_unrealized_fx_revaluation, source_type=
#     'fx_revaluation') — simply never included in the non-cash code set
#     queried below, so it's structurally excluded from every O/I/F bucket
#     without needing a source_type filter. But its cash-side line (a
#     PAYONEER_WALLET debit/credit) DOES change that account's real book
#     balance, which Beginning/Ending Cash (reused from ledger.balances,
#     unfiltered by source_type, per Main-agent's brief) DOES include. A
#     pure "exclude it and never mention it again" treatment would silently
#     break the Beginning+Change=Ending identity. The standard, sourced
#     resolution (IAS 7 / PSAK 207 §28: "the effect of exchange rate
#     changes on cash... is reported separately... to reconcile cash... at
#     the beginning and end of the period") is a distinct reconciling line,
#     OUTSIDE the Operating/Investing/Financing subtotal, between "Net
#     change from O+I+F" and "Cash at end of period" — see fx_effect_idr
#     below. This is a resolved implementation detail grounded in the same
#     real accounting standard CLAUDE.md already cites for this feature,
#     not an invented workaround; flagged explicitly in the Builder report
#     for QA/Main-agent to double-check.
# ---------------------------------------------------------------------------

CASH_STATEMENT_SECTION = "asset"  # kept for backward-compatible reference only — see CASH_ACCOUNT_TYPE_CODES below.

# BUG FIX (2026-09-10, found while adding EMPLOYEE_LOAN_RECEIVABLE — a new,
# genuine Assets-section account per CLAUDE.md's Chart of accounts, but NOT
# a wallet/bank cash account): this module's "Cash" concept was defined as
# "every account under statement_section='asset'", with an explicit comment
# above claiming that's "confirmed to be exactly and only" the 4 wallet/bank
# accounts. That was only ever true because, until now, those 4 WERE the
# entire Assets section. Adding any other Assets-section account type (like
# EMPLOYEE_LOAN_RECEIVABLE) breaks that assumption silently: it gets swept
# into Beginning/Ending Cash (via _cash_account_rows below, previously
# filtered on statement_section alone) while ALSO being correctly bucketed
# as a non-cash Operating line (_CASH_FLOW_CODE_TO_KEY) — double-counting it
# and breaking the Beginning+NetChange=Ending identity (confirmed by a real
# failing test: a lone employee-loan disbursement produced a
# ``difference_idr`` of the full disbursed amount, not 0).
#
# Fixed by defining "Cash" as an explicit account-TYPE-CODE whitelist
# instead of a broad statement_section match. For every account type that
# existed before this fix, this produces IDENTICAL results (same 4 codes)
# — every existing Balance Sheet/Cash Flow number for EBAY_WALLET/
# PAYONEER_WALLET/BCA_BRIDGING/BCA_MAIN is completely unchanged. The ONLY
# practical effect is that a genuinely non-cash Assets-section account
# (EMPLOYEE_LOAN_RECEIVABLE now, and any future one) is correctly excluded
# from "Cash" instead of silently corrupting it. The Balance Sheet's own
# asset-section listing (webapp.reporting._account_instance_lines) is
# UNAFFECTED — it still correctly lists EMPLOYEE_LOAN_RECEIVABLE as its own
# Assets line, exactly as it should; only THIS module's separate, narrower
# "which accounts count as Cash for the Cash Flow Statement" concept changes.
CASH_ACCOUNT_TYPE_CODES = {"EBAY_WALLET", "PAYONEER_WALLET", "BCA_BRIDGING", "BCA_MAIN"}

# account_type code -> cash-flow line key, for every non-cash account type
# that can appear in Operating or Financing. CONSIGNOR_PAYABLE is handled
# separately (side-dependent) — see _CONSIGNOR_PAYABLE_BY_SIDE below.
_CASH_FLOW_CODE_TO_KEY = {
    "SALES_REVENUE": "cash_from_customers",
    "SALES_RETURNS_ALLOWANCES": "cash_from_customers",
    "CONSIGNMENT_COMMISSION_INCOME": "cash_from_customers",
    "COGS": "cogs_purchases",
    "EBAY_SELLING_FEES": "ebay_selling_fees",
    "PAYOUT_FEE": "payout_fee",
    "PAYROLL": "payroll",
    "GENERAL_OPEX": "general_opex",
    "SHIPPING_COST": "shipping_cost",
    "CONTRACT_LABOR": "contract_labor",
    "INTEREST_INCOME": "interest_income",
    "REALIZED_FX": "realized_fx",
    "OTHER_INCOME": "other_income",
    "OWNERS_CAPITAL": "owners_capital",
    "OWNERS_DRAW": "owners_draw",
    # Added 2026-09-10 alongside the new EMPLOYEE_LOAN_RECEIVABLE asset
    # account (see ledger/chart_of_accounts.py). REQUIRED here, not
    # optional decoration: this dict is a WHITELIST — any cash-touching
    # entry's non-cash counterpart line that ISN'T in this dict silently
    # drops out of the Operating/Financing totals entirely, which would
    # break the Beginning+NetChange=Ending identity (difference_idr) the
    # moment a real employee-loan disbursement or an embedded-in-payroll
    # repayment posts (both touch a cash account on one side and
    # EMPLOYEE_LOAN_RECEIVABLE on the other). Bucketed under Operating (a
    # single net "employee_loans" line — disbursements net outflow,
    # repayments received net inflow) rather than inventing a new Investing
    # section (this module's Investing bucket is explicitly unbuilt/always
    # empty — see the docstring above) — a reasonable, disclosed judgment
    # call for a single small-dollar real loan, flagged for QA/Main-agent
    # to confirm rather than silently assumed as the only valid treatment.
    "EMPLOYEE_LOAN_RECEIVABLE": "employee_loans",
}

# side: 'credit' = the accrual (a consignment sale) -> bucketed with
# customer receipts; 'debit' = the actual reimbursement payout -> its own
# outflow line. See the module-level docstring above.
_CONSIGNOR_PAYABLE_BY_SIDE = {"credit": "cash_from_customers", "debit": "consignor_payouts"}

_CASH_FLOW_LINE_LABELS = {
    "cash_from_customers": "Cash received from customers",
    "cogs_purchases": "Cash paid for COGS purchases",
    "ebay_selling_fees": "Cash paid — eBay Selling Fees",
    "payout_fee": "Cash paid — Payout Fee (Payoneer withdrawal)",
    "payroll": "Cash paid — Payroll",
    "general_opex": "Cash paid — General Operating Expenses",
    "shipping_cost": "Cash paid — Shipping Cost",
    "contract_labor": "Cash paid — Contract Labor",
    "consignor_payouts": "Cash paid to consignors",
    "interest_income": "Interest income received",
    "realized_fx": "Realized FX Gain/Loss (at Payoneer withdrawal)",
    "other_income": "Other Income (owner's e-wallet pass-through)",
    "owners_capital": "Owner's Capital contributions",
    "owners_draw": "Owner's Draw",
    "employee_loans": "Employee Loans, net (disbursed / repaid)",
}

_OPERATING_KEY_ORDER = [
    "cash_from_customers",
    "cogs_purchases",
    "ebay_selling_fees",
    "payout_fee",
    "payroll",
    "general_opex",
    "shipping_cost",
    "contract_labor",
    "consignor_payouts",
    "interest_income",
    "realized_fx",
    "other_income",
    "employee_loans",
]
_FINANCING_KEY_ORDER = ["owners_capital", "owners_draw"]

# For drill-down: key -> [(account_type_code, side_filter)]. side_filter is
# None (either side) except CONSIGNOR_PAYABLE's two lines.
_CASH_FLOW_LINE_SOURCES: dict[str, list[tuple[str, str | None]]] = {}
for _code, _key in _CASH_FLOW_CODE_TO_KEY.items():
    _CASH_FLOW_LINE_SOURCES.setdefault(_key, []).append((_code, None))
for _side, _key in _CONSIGNOR_PAYABLE_BY_SIDE.items():
    _CASH_FLOW_LINE_SOURCES.setdefault(_key, []).append(("CONSIGNOR_PAYABLE", _side))
del _code, _key, _side

CASH_FLOW_LINE_KEYS = set(_CASH_FLOW_LINE_SOURCES.keys())


@dataclass
class CashFlowLine:
    key: str
    label: str
    amount_idr: Decimal  # positive = inflow-associated, negative = outflow-associated


@dataclass
class CashFlowStatement:
    period_month: _dt.date
    beginning_cash_idr: Decimal
    operating_lines: list[CashFlowLine]
    total_operating_idr: Decimal
    investing_lines: list[CashFlowLine]  # always empty for now — see module docstring
    total_investing_idr: Decimal
    financing_lines: list[CashFlowLine]
    total_financing_idr: Decimal
    fx_effect_idr: Decimal  # reconciling line, outside O/I/F — see module docstring
    net_change_idr: Decimal  # total_operating + total_investing + total_financing + fx_effect
    ending_cash_idr: Decimal  # the ledger's own real balance (ledger.balances), independently computed
    # Health-check, same pattern as BalanceSheetReport.difference_idr: should
    # be exactly 0 (beginning + net_change == ending_cash). A nonzero value
    # means a real transaction type doesn't fit the categorization above and
    # needs investigating, never silently trusted.
    difference_idr: Decimal


def _cash_account_rows(conn: Connection):
    return conn.execute(
        select(accounts.c.id, account_types.c.normal_balance)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .where(account_types.c.code.in_(CASH_ACCOUNT_TYPE_CODES))
    ).all()


def _total_cash_before(conn: Connection, period_month: _dt.date) -> Decimal:
    return sum(
        (_shared_account_balance_before(conn, r.id, period_month, r.normal_balance) for r in _cash_account_rows(conn)),
        ZERO,
    )


def _total_cash_through(conn: Connection, period_month: _dt.date) -> Decimal:
    return sum(
        (_shared_account_balance_through(conn, r.id, period_month, r.normal_balance) for r in _cash_account_rows(conn)),
        ZERO,
    )


def _cash_flow_source_lines(conn: Connection, period_month: _dt.date):
    """Every journal_line, this period, touching a non-cash account type
    that appears somewhere in the Operating/Financing categorization
    (``_CASH_FLOW_CODE_TO_KEY`` union CONSIGNOR_PAYABLE) — excludes
    ``source_type='opening_balance'`` (see module docstring; the only
    account type it touches here is OWNERS_CAPITAL).
    """
    codes = list(_CASH_FLOW_CODE_TO_KEY) + ["CONSIGNOR_PAYABLE"]
    return conn.execute(
        select(
            account_types.c.code,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
        )
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.code.in_(codes))
        .where(journal_entries.c.period_month == period_month)
        .where(journal_entries.c.source_type != "opening_balance")
    ).all()


def _fx_revaluation_cash_effect(conn: Connection, period_month: _dt.date) -> Decimal:
    """Net effect on cash-account book balances from
    ``source_type='fx_revaluation'`` entries this period — the standard IAS
    7/PSAK 207 reconciling "effect of exchange rate changes on cash" line,
    kept OUTSIDE Operating/Investing/Financing. See module docstring.
    """
    rows = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.code.in_(CASH_ACCOUNT_TYPE_CODES))
        .where(journal_entries.c.source_type == "fx_revaluation")
        .where(journal_entries.c.period_month == period_month)
    ).all()
    return sum((r.debit_amount_idr - r.credit_amount_idr for r in rows), ZERO)


def cash_flow_statement(conn: Connection, *, period_month: _dt.date) -> CashFlowStatement:
    """The Statement of Cash Flows — consolidated only, direct method. See
    the module docstring above (and CLAUDE.md's Money flow / Core accounting
    rules sections) for the full reasoning.
    """
    totals: dict[str, Decimal] = {}
    for row in _cash_flow_source_lines(conn, period_month):
        contribution = row.credit_amount_idr - row.debit_amount_idr
        if row.code == "CONSIGNOR_PAYABLE":
            side = "credit" if row.credit_amount_idr > 0 else "debit"
            key = _CONSIGNOR_PAYABLE_BY_SIDE[side]
            # Reimbursement (debit side) is an outflow — contribution above
            # is already negative in that case (0 - amount), correct as-is.
        else:
            key = _CASH_FLOW_CODE_TO_KEY[row.code]
        totals[key] = totals.get(key, ZERO) + contribution

    operating_lines = [
        CashFlowLine(key=k, label=_CASH_FLOW_LINE_LABELS[k], amount_idr=totals[k])
        for k in _OPERATING_KEY_ORDER
        if k in totals
    ]
    financing_lines = [
        CashFlowLine(key=k, label=_CASH_FLOW_LINE_LABELS[k], amount_idr=totals[k])
        for k in _FINANCING_KEY_ORDER
        if k in totals
    ]
    total_operating = sum((l.amount_idr for l in operating_lines), ZERO)
    total_investing = ZERO  # No PPE/Capex accounts exist yet — see CLAUDE.md's SAK alignment section.
    total_financing = sum((l.amount_idr for l in financing_lines), ZERO)

    fx_effect = _fx_revaluation_cash_effect(conn, period_month)
    net_change = total_operating + total_investing + total_financing + fx_effect

    beginning_cash = _total_cash_before(conn, period_month)
    ending_cash = _total_cash_through(conn, period_month)

    return CashFlowStatement(
        period_month=period_month,
        beginning_cash_idr=beginning_cash,
        operating_lines=operating_lines,
        total_operating_idr=total_operating,
        investing_lines=[],
        total_investing_idr=total_investing,
        financing_lines=financing_lines,
        total_financing_idr=total_financing,
        fx_effect_idr=fx_effect,
        net_change_idr=net_change,
        ending_cash_idr=ending_cash,
        difference_idr=ending_cash - (beginning_cash + net_change),
    )


def cash_flow_line_drilldown(conn: Connection, *, key: str, period_month: _dt.date) -> list[DrilldownLine]:
    """Source journal lines behind one Operating/Financing line of
    ``cash_flow_statement()`` — same DrilldownLine shape and query pattern
    as ``drilldown()``/``account_instance_drilldown()`` above, filtered by
    (account_type_code, side) pairs so CONSIGNOR_PAYABLE's two different
    lines each show only their own side's lines.
    """
    sources = _CASH_FLOW_LINE_SOURCES.get(key)
    if not sources:
        return []
    codes = sorted({code for code, _side in sources})
    rows = conn.execute(
        select(
            journal_entries.c.entry_date,
            journal_entries.c.source_type,
            journal_entries.c.memo,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
            journal_lines.c.journal_entry_id,
            account_types.c.code,
        )
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.code.in_(codes))
        .where(journal_entries.c.period_month == period_month)
        .where(journal_entries.c.source_type != "opening_balance")
        .order_by(journal_entries.c.entry_date)
    ).all()

    side_by_code = dict(sources)
    out: list[DrilldownLine] = []
    for r in rows:
        side = side_by_code.get(r.code)
        if side == "credit" and not (r.credit_amount_idr > 0):
            continue
        if side == "debit" and not (r.debit_amount_idr > 0):
            continue
        out.append(
            DrilldownLine(
                entry_date=r.entry_date,
                source_type=r.source_type,
                memo=r.memo,
                debit_idr=r.debit_amount_idr,
                credit_idr=r.credit_amount_idr,
                ebay_order_ref=r.ebay_order_ref,
                consignor_item_ref=r.consignor_item_ref,
                journal_entry_id=r.journal_entry_id,
            )
        )
    return out


def cash_flow_fx_effect_drilldown(conn: Connection, *, period_month: _dt.date) -> list[DrilldownLine]:
    """Source journal lines behind the "Effect of unrealized FX revaluation
    on cash" reconciling line — every cash-account line on a
    ``source_type='fx_revaluation'`` entry this period.
    """
    rows = conn.execute(
        select(
            journal_entries.c.entry_date,
            journal_entries.c.source_type,
            journal_entries.c.memo,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
            journal_lines.c.journal_entry_id,
        )
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.code.in_(CASH_ACCOUNT_TYPE_CODES))
        .where(journal_entries.c.source_type == "fx_revaluation")
        .where(journal_entries.c.period_month == period_month)
        .order_by(journal_entries.c.entry_date)
    ).all()
    return [
        DrilldownLine(
            entry_date=r.entry_date,
            source_type=r.source_type,
            memo=r.memo,
            debit_idr=r.debit_amount_idr,
            credit_idr=r.credit_amount_idr,
            ebay_order_ref=r.ebay_order_ref,
            consignor_item_ref=r.consignor_item_ref,
            journal_entry_id=r.journal_entry_id,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# P&L (consolidated only)
# ---------------------------------------------------------------------------


@dataclass
class PnLLine:
    code: str
    name: str
    amount_idr: Decimal  # always a "natural" positive-is-favorable-for-the-line sign


@dataclass
class PnLReport:
    period_month: _dt.date
    revenue_lines: list[PnLLine]
    total_revenue_idr: Decimal
    cogs_idr: Decimal
    gross_profit_idr: Decimal
    opex_lines: list[PnLLine]
    total_opex_idr: Decimal
    operating_income_idr: Decimal
    other_income_expense_lines: list[PnLLine]
    net_income_idr: Decimal


def _section_lines(conn: Connection, period_month: _dt.date, statement_section: str, *, cumulative: bool = False):
    """``cumulative=True`` sums every entry through the end of
    ``period_month`` (period_month <= :month) instead of just entries dated
    within it (period_month == :month) — the same "running balance as of
    period end" shape ``equity_report()`` already uses for
    OWNERS_CAPITAL/OWNERS_DRAW, extended here so the retained-earnings
    computation (a cumulative net-income figure, not a period flow) and the
    Balance Sheet can reuse this same query shape rather than a new pattern.
    """
    query = (
        select(
            account_types.c.code,
            account_types.c.name,
            account_types.c.normal_balance,
            account_types.c.is_contra,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
        )
        .join(accounts, accounts.c.account_type_id == account_types.c.id)
        .join(journal_lines, journal_lines.c.account_id == accounts.c.id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.statement_section == statement_section)
    )
    if cumulative:
        query = query.where(journal_entries.c.period_month <= period_month)
    else:
        query = query.where(journal_entries.c.period_month == period_month)
    return conn.execute(query).all()


def _sum_by_code(rows) -> dict[str, tuple[str, Decimal]]:
    """Returns {code: (name, net_amount)} where net_amount is signed so that
    a credit-normal account nets credit-debit and a debit-normal account
    nets debit-credit — i.e. always "positive means more of what that
    account normally represents" (more revenue, more expense, more FX
    gain), matching how each line should read on a P&L.
    """
    out: dict[str, tuple[str, Decimal]] = {}
    for r in rows:
        name, total = out.get(r.code, (r.name, ZERO))
        if r.normal_balance == "credit":
            total += r.credit_amount_idr - r.debit_amount_idr
        else:
            total += r.debit_amount_idr - r.credit_amount_idr
        out[r.code] = (name, total)
    return out


def pnl_report(conn: Connection, *, period_month: _dt.date) -> PnLReport:
    revenue_rows = _section_lines(conn, period_month, "revenue")
    revenue_by_code = _sum_by_code(revenue_rows)
    # is_contra (Sales Returns & Allowances) already nets as a NEGATIVE
    # contribution here (debit-normal -> debit-credit, positive when it
    # reduces revenue) — flip its sign so total_revenue = sum of all lines
    # as displayed subtracts it correctly.
    revenue_lines = []
    total_revenue = ZERO
    for code, (name, amount) in revenue_by_code.items():
        is_contra = any(r.code == code and r.is_contra for r in revenue_rows)
        display_amount = -amount if is_contra else amount
        revenue_lines.append(PnLLine(code=code, name=name, amount_idr=display_amount))
        total_revenue += display_amount

    cogs_rows = _section_lines(conn, period_month, "cogs")
    cogs_total = sum((r.debit_amount_idr - r.credit_amount_idr for r in cogs_rows), ZERO)
    gross_profit = total_revenue - cogs_total

    opex_rows = _section_lines(conn, period_month, "opex")
    opex_by_code = _sum_by_code(opex_rows)
    opex_lines = [PnLLine(code=c, name=n, amount_idr=a) for c, (n, a) in opex_by_code.items()]
    total_opex = sum((line.amount_idr for line in opex_lines), ZERO)
    operating_income = gross_profit - total_opex

    other_rows = _section_lines(conn, period_month, "other_income_expense")
    other_by_code = _sum_by_code(other_rows)
    other_lines = [PnLLine(code=c, name=n, amount_idr=a) for c, (n, a) in other_by_code.items()]
    total_other = sum((line.amount_idr for line in other_lines), ZERO)
    net_income = operating_income + total_other

    return PnLReport(
        period_month=period_month,
        revenue_lines=revenue_lines,
        total_revenue_idr=total_revenue,
        cogs_idr=cogs_total,
        gross_profit_idr=gross_profit,
        opex_lines=opex_lines,
        total_opex_idr=total_opex,
        operating_income_idr=operating_income,
        other_income_expense_lines=other_lines,
        net_income_idr=net_income,
    )


def _cumulative_net_income(conn: Connection, period_month: _dt.date) -> Decimal:
    """Cumulative net income (Revenue − Sales Returns & Allowances − COGS −
    Operating Expenses ± Other Income/Expense) for every journal_entry with
    ``period_month <= period_month`` — i.e. accumulated profit through the
    end of this period. This is what Retained Earnings actually IS in a
    non-period-closing ledger like this one: no closing entry ever zeroes
    P&L accounts into RETAINED_EARNINGS (see ledger/posting.py — nothing
    posts to that account type), so Retained Earnings must be computed by
    re-summing the P&L accounts cumulatively, not read off a posted balance.

    Mirrors ``pnl_report()``'s exact math (same sign handling for the
    contra Sales Returns & Allowances line, same per-section normal-balance
    netting) but with ``cumulative=True`` instead of a single period, and
    returns only the bottom-line net-income number — callers needing the
    line-item breakdown for a given period should use ``pnl_report()``
    instead; this exists specifically for Equity/Balance Sheet's
    as-of-period-end Retained Earnings figure.
    """
    revenue_rows = _section_lines(conn, period_month, "revenue", cumulative=True)
    revenue_by_code = _sum_by_code(revenue_rows)
    total_revenue = ZERO
    for code, (_name, amount) in revenue_by_code.items():
        is_contra = any(r.code == code and r.is_contra for r in revenue_rows)
        total_revenue += -amount if is_contra else amount

    cogs_rows = _section_lines(conn, period_month, "cogs", cumulative=True)
    cogs_total = sum((r.debit_amount_idr - r.credit_amount_idr for r in cogs_rows), ZERO)

    opex_rows = _section_lines(conn, period_month, "opex", cumulative=True)
    opex_by_code = _sum_by_code(opex_rows)
    total_opex = sum((amount for _name, amount in opex_by_code.values()), ZERO)

    other_rows = _section_lines(conn, period_month, "other_income_expense", cumulative=True)
    other_by_code = _sum_by_code(other_rows)
    total_other = sum((amount for _name, amount in other_by_code.values()), ZERO)

    return total_revenue - cogs_total - total_opex + total_other


# ---------------------------------------------------------------------------
# Equity (consolidated only) — a running balance-sheet-style total, not a
# period flow: uses period_month <= :month, unlike P&L/Cash Flow's
# period_month == :month.
# ---------------------------------------------------------------------------


@dataclass
class EquityReport:
    period_month: _dt.date
    owners_capital_idr: Decimal
    owners_draw_idr: Decimal
    retained_earnings_idr: Decimal
    ending_balance_idr: Decimal


EQUITY_CODES = ("OWNERS_CAPITAL", "OWNERS_DRAW")


def equity_report(conn: Connection, *, period_month: _dt.date) -> EquityReport:
    rows = conn.execute(
        select(
            account_types.c.code,
            account_types.c.normal_balance,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
        )
        .join(accounts, accounts.c.account_type_id == account_types.c.id)
        .join(journal_lines, journal_lines.c.account_id == accounts.c.id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.code.in_(EQUITY_CODES))
        .where(journal_entries.c.period_month <= period_month)
    ).all()

    totals = {code: ZERO for code in EQUITY_CODES}
    for r in rows:
        if r.normal_balance == "credit":
            totals[r.code] += r.credit_amount_idr - r.debit_amount_idr
        else:
            totals[r.code] += r.debit_amount_idr - r.credit_amount_idr

    capital = totals["OWNERS_CAPITAL"]
    draw = totals["OWNERS_DRAW"]  # debit-normal, positive = cumulative draws taken
    # Retained Earnings is NOT read off the RETAINED_EARNINGS account balance
    # (nothing ever posts to it — no closing-entry mechanism exists, and none
    # should: this ledger computes P&L by filtering journal_entries by
    # period, not by zeroing account balances each period-end). It's a pure
    # computation: cumulative net income through this period's end. See
    # _cumulative_net_income()'s docstring for why.
    retained = _cumulative_net_income(conn, period_month)
    ending_balance = capital - draw + retained

    return EquityReport(
        period_month=period_month,
        owners_capital_idr=capital,
        owners_draw_idr=draw,
        retained_earnings_idr=retained,
        ending_balance_idr=ending_balance,
    )


# ---------------------------------------------------------------------------
# Balance Sheet / Statement of Financial Position (consolidated only) — a
# point-in-time snapshot as of period end, same "period_month <= :month"
# cumulative-balance shape as equity_report() above, extended to every
# asset and liability account instance rather than just the two equity
# account types.
#
# Consolidated only, same reasoning as P&L and Equity (see CLAUDE.md's
# Accounting scope): Liabilities (Consignor Payable) and Equity are already
# consolidated-only concepts in the chart of accounts, and Assets=
# Liabilities+Equity only holds as a whole-business identity — a genuine
# per-account balance sheet isn't buildable without inventing an allocation
# for the liability/equity side, the exact thing this project avoids
# elsewhere.
# ---------------------------------------------------------------------------


@dataclass
class BalanceSheetLine:
    account_id: int
    account_type_code: str
    label: str
    balance_idr: Decimal


@dataclass
class BalanceSheetReport:
    period_month: _dt.date
    asset_lines: list[BalanceSheetLine]
    total_assets_idr: Decimal
    liability_lines: list[BalanceSheetLine]
    total_liabilities_idr: Decimal
    owners_capital_idr: Decimal
    owners_draw_idr: Decimal
    retained_earnings_idr: Decimal
    total_equity_idr: Decimal
    total_liabilities_and_equity_idr: Decimal
    # assets - (liabilities + equity). Should be exactly 0 — debits=credits
    # is enforced on every journal entry by a real Postgres trigger (see
    # ledger/schema.py), so once every account is correctly bucketed into
    # Asset/Liability/Equity/Revenue/COGS/Opex/Other, this nets to zero as a
    # mathematical property of double-entry bookkeeping, not something
    # force-reconciled here. A nonzero value means some account somewhere
    # isn't being captured in one of the three buckets — surfaced, never
    # papered over with a plug figure.
    difference_idr: Decimal


def _account_balance_through(conn: Connection, account_id: int, period_month: _dt.date, normal_balance: str) -> Decimal:
    """Thin wrapper kept for every existing call site in this module —
    the actual computation now lives in ``ledger.balances.
    account_balance_through`` (extracted 2026-09 so
    ``ingestion.reconciliation`` can reuse the identical query instead of
    re-implementing it; see that module's docstring)."""
    return _shared_account_balance_through(conn, account_id, period_month, normal_balance)


def _account_instance_lines(conn: Connection, period_month: _dt.date, statement_section: str) -> list[BalanceSheetLine]:
    """Every individual ``accounts`` row (not account TYPE) under a given
    statement_section, with its own as-of-period-end balance — i.e. each of
    the 3 eBay Wallets and 2 Payoneer Wallets/BCA Bridging Accounts shows as
    its own line, not blended into one "eBay Wallet" total, since these are
    genuinely separate ledger accounts (see ledger/schema.py's per-instance
    accounts table + CLAUDE.md's Chart of accounts scoping).
    """
    rows = conn.execute(
        select(
            accounts.c.id.label("account_id"),
            account_types.c.code,
            account_types.c.name.label("type_name"),
            account_types.c.normal_balance,
            ebay_accounts.c.name.label("ebay_account_name"),
            wallet_groups.c.name.label("wallet_group_name"),
        )
        .select_from(accounts)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .outerjoin(ebay_accounts, ebay_accounts.c.id == accounts.c.ebay_account_id)
        .outerjoin(wallet_groups, wallet_groups.c.id == accounts.c.wallet_group_id)
        .where(account_types.c.statement_section == statement_section)
        .order_by(account_types.c.code, accounts.c.id)
    ).all()

    lines: list[BalanceSheetLine] = []
    for r in rows:
        if r.ebay_account_name:
            label = f"{r.type_name} — {r.ebay_account_name}"
        elif r.wallet_group_name:
            label = f"{r.type_name} — {r.wallet_group_name}"
        else:
            label = r.type_name
        balance = _account_balance_through(conn, r.account_id, period_month, r.normal_balance)
        lines.append(
            BalanceSheetLine(account_id=r.account_id, account_type_code=r.code, label=label, balance_idr=balance)
        )
    return lines


def balance_sheet_report(conn: Connection, *, period_month: _dt.date) -> BalanceSheetReport:
    asset_lines = _account_instance_lines(conn, period_month, "asset")
    total_assets = sum((line.balance_idr for line in asset_lines), ZERO)

    liability_lines = _account_instance_lines(conn, period_month, "liability")
    total_liabilities = sum((line.balance_idr for line in liability_lines), ZERO)

    equity = equity_report(conn, period_month=period_month)
    total_equity = equity.ending_balance_idr
    total_liabilities_and_equity = total_liabilities + total_equity

    return BalanceSheetReport(
        period_month=period_month,
        asset_lines=asset_lines,
        total_assets_idr=total_assets,
        liability_lines=liability_lines,
        total_liabilities_idr=total_liabilities,
        owners_capital_idr=equity.owners_capital_idr,
        owners_draw_idr=equity.owners_draw_idr,
        retained_earnings_idr=equity.retained_earnings_idr,
        total_equity_idr=total_equity,
        total_liabilities_and_equity_idr=total_liabilities_and_equity,
        difference_idr=total_assets - total_liabilities_and_equity,
    )


def account_instance_drilldown(
    conn: Connection, *, account_id: int, period_month: _dt.date
) -> list[DrilldownLine]:
    """Every journal line posted to one specific ``accounts`` row (a single
    eBay Wallet, a single wallet-group's Payoneer Wallet, etc.) through the
    end of ``period_month`` — the Balance Sheet's drill-down. Deliberately
    keyed on the literal ``account_id`` rather than reusing ``drilldown()``'s
    account_type_codes + per-eBay-account-attribution approach: that
    attribution logic only resolves to an eBay account, never a wallet-group,
    so it can't correctly key a shared Payoneer Wallet/BCA Bridging line
    (see module docstring on attribution's Payoneer-stage limits). Filtering
    by the exact account_id sidesteps that entirely and is a more literal,
    more obviously-correct traceability mechanism for "what makes up this
    specific ledger account's balance" than re-deriving an attribution.
    """
    rows = conn.execute(
        select(
            journal_entries.c.entry_date,
            journal_entries.c.source_type,
            journal_entries.c.memo,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
            journal_lines.c.journal_entry_id,
        )
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(journal_lines.c.account_id == account_id)
        .where(journal_entries.c.period_month <= period_month)
        .order_by(journal_entries.c.entry_date)
    ).all()

    return [
        DrilldownLine(
            entry_date=r.entry_date,
            source_type=r.source_type,
            memo=r.memo,
            debit_idr=r.debit_amount_idr,
            credit_idr=r.credit_amount_idr,
            ebay_order_ref=r.ebay_order_ref,
            consignor_item_ref=r.consignor_item_ref,
            journal_entry_id=r.journal_entry_id,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Drill-down — the exact source journal_lines behind any figure above.
# Shares the same WHERE-clause shape as the aggregations so a drill-down's
# rows are guaranteed to sum to the headline figure (same query, one
# aggregated and one not).
# ---------------------------------------------------------------------------


@dataclass
class DrilldownLine:
    entry_date: _dt.date
    source_type: str
    memo: str | None
    debit_idr: Decimal
    credit_idr: Decimal
    ebay_order_ref: str | None
    consignor_item_ref: str | None
    journal_entry_id: int


def drilldown(
    conn: Connection,
    *,
    account_type_codes: list[str],
    period_month: _dt.date,
    ebay_account_id: int | None = None,
    period_is_cumulative: bool = False,
) -> list[DrilldownLine]:
    query = (
        select(
            journal_entries.c.entry_date,
            journal_entries.c.source_type,
            journal_entries.c.memo,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
            journal_lines.c.journal_entry_id,
        )
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(account_types.c.code.in_(account_type_codes))
    )
    if period_is_cumulative:
        query = query.where(journal_entries.c.period_month <= period_month)
    else:
        query = query.where(journal_entries.c.period_month == period_month)
    rows = conn.execute(query.order_by(journal_entries.c.entry_date)).all()

    if ebay_account_id is not None:
        attribution = attribute_entries_to_ebay_account(conn, rows) if rows else {}
        rows = [r for r in rows if attribution.get(r.journal_entry_id) == ebay_account_id]

    return [
        DrilldownLine(
            entry_date=r.entry_date,
            source_type=r.source_type,
            memo=r.memo,
            debit_idr=r.debit_amount_idr,
            credit_idr=r.credit_amount_idr,
            ebay_order_ref=r.ebay_order_ref,
            consignor_item_ref=r.consignor_item_ref,
            journal_entry_id=r.journal_entry_id,
        )
        for r in rows
    ]
