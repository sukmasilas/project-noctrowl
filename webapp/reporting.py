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
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.entities import get_account_id
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
# Cash Flow
# ---------------------------------------------------------------------------


@dataclass
class Movement:
    entry_date: _dt.date
    source_type: str
    memo: str | None
    debit_idr: Decimal
    credit_idr: Decimal
    journal_entry_id: int

    @property
    def net_idr(self) -> Decimal:
        return self.debit_idr - self.credit_idr


@dataclass
class CashFlowStage:
    label: str
    movements: list[Movement] = field(default_factory=list)

    @property
    def net_idr(self) -> Decimal:
        return sum((m.net_idr for m in self.movements), ZERO)


@dataclass
class CashFlowReport:
    period_month: _dt.date
    scope_label: str
    is_shared_pool: bool
    wallet_group_name: str | None
    stages: list[CashFlowStage]
    transfer_elimination_idr: Decimal | None = None


def _account_movements(conn: Connection, account_id: int, period_month: _dt.date) -> list[Movement]:
    rows = conn.execute(
        select(
            journal_entries.c.entry_date,
            journal_entries.c.source_type,
            journal_entries.c.memo,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.journal_entry_id,
        )
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(journal_lines.c.account_id == account_id)
        .where(journal_entries.c.period_month == period_month)
        .order_by(journal_entries.c.entry_date)
    ).all()
    return [
        Movement(
            entry_date=r.entry_date,
            source_type=r.source_type,
            memo=r.memo,
            debit_idr=r.debit_amount_idr,
            credit_idr=r.credit_amount_idr,
            journal_entry_id=r.journal_entry_id,
        )
        for r in rows
    ]


def _try_account_id(conn: Connection, code: str, **kwargs) -> int | None:
    try:
        return get_account_id(conn, code, **kwargs)
    except Exception:  # UnknownAccountInstanceError — this account_type isn't set up yet
        return None


def cash_flow_report(
    conn: Connection, *, period_month: _dt.date, ebay_account_id: int | None = None
) -> CashFlowReport:
    if ebay_account_id is not None:
        acct_row = conn.execute(
            select(ebay_accounts.c.name, ebay_accounts.c.wallet_group_id, wallet_groups.c.name.label("wg_name"))
            .join(wallet_groups, wallet_groups.c.id == ebay_accounts.c.wallet_group_id)
            .where(ebay_accounts.c.id == ebay_account_id)
        ).first()
        if acct_row is None:
            raise ValueError(f"No ebay_account with id={ebay_account_id}")
        is_shared = (
            len(
                conn.execute(
                    select(ebay_accounts.c.id).where(
                        ebay_accounts.c.wallet_group_id == acct_row.wallet_group_id,
                        ebay_accounts.c.is_active.is_(True),
                    )
                ).all()
            )
            > 1
        )

        stages: list[CashFlowStage] = []
        ebay_wallet_id = _try_account_id(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
        if ebay_wallet_id is not None:
            stages.append(
                CashFlowStage(label="eBay Wallet", movements=_account_movements(conn, ebay_wallet_id, period_month))
            )
        payoneer_id = _try_account_id(conn, "PAYONEER_WALLET", wallet_group_id=acct_row.wallet_group_id)
        if payoneer_id is not None:
            stages.append(
                CashFlowStage(
                    label="Payoneer Wallet" + (" (shared pool)" if is_shared else ""),
                    movements=_account_movements(conn, payoneer_id, period_month),
                )
            )
        bridging_id = _try_account_id(conn, "BCA_BRIDGING", wallet_group_id=acct_row.wallet_group_id)
        if bridging_id is not None:
            stages.append(
                CashFlowStage(
                    label="BCA Bridging Account" + (" (shared pool)" if is_shared else ""),
                    movements=_account_movements(conn, bridging_id, period_month),
                )
            )
        return CashFlowReport(
            period_month=period_month,
            scope_label=acct_row.name,
            is_shared_pool=is_shared,
            wallet_group_name=acct_row.wg_name,
            stages=stages,
        )

    # Consolidated: sum eBay Wallet movements across every active account
    # (1:1, no dedup needed); dedupe Payoneer/BCA Bridging by wallet-group
    # (never sum each paired account's identical shared-pool figure twice —
    # this is the exact double-count bug CLAUDE.md warns about).
    active_accounts = conn.execute(
        select(ebay_accounts.c.id, ebay_accounts.c.name, ebay_accounts.c.wallet_group_id).where(
            ebay_accounts.c.is_active.is_(True)
        )
    ).all()

    ebay_wallet_stage = CashFlowStage(label="eBay Wallet (all accounts)")
    for acc in active_accounts:
        wallet_id = _try_account_id(conn, "EBAY_WALLET", ebay_account_id=acc.id)
        if wallet_id is not None:
            ebay_wallet_stage.movements.extend(_account_movements(conn, wallet_id, period_month))

    seen_wallet_groups: set[int] = set()
    payoneer_stage = CashFlowStage(label="Payoneer Wallet (all wallet-groups, deduped)")
    bridging_stage = CashFlowStage(label="BCA Bridging Account (all wallet-groups, deduped)")
    for acc in active_accounts:
        if acc.wallet_group_id in seen_wallet_groups:
            continue
        seen_wallet_groups.add(acc.wallet_group_id)
        payoneer_id = _try_account_id(conn, "PAYONEER_WALLET", wallet_group_id=acc.wallet_group_id)
        if payoneer_id is not None:
            payoneer_stage.movements.extend(_account_movements(conn, payoneer_id, period_month))
        bridging_id = _try_account_id(conn, "BCA_BRIDGING", wallet_group_id=acc.wallet_group_id)
        if bridging_id is not None:
            bridging_stage.movements.extend(_account_movements(conn, bridging_id, period_month))

    bca_main_id = _try_account_id(conn, "BCA_MAIN")
    bca_main_stage = CashFlowStage(label="BCA Main Account")
    if bca_main_id is not None:
        bca_main_stage.movements = _account_movements(conn, bca_main_id, period_month)

    # Informational only: the total inter-account-transfer inflow landing in
    # BCA Main this period. It's not subtracted from anything — the
    # underlying movement lists above already only count each transfer leg
    # once, on its own account's list — this figure just makes visible that
    # those legs cancel out when you look at total cash movement across the
    # whole business (see module docstring / design doc §3).
    transfer_elimination = sum(
        (m.debit_idr - m.credit_idr for m in bca_main_stage.movements if m.source_type == "inter_account_transfer"),
        ZERO,
    )

    return CashFlowReport(
        period_month=period_month,
        scope_label="Consolidated",
        is_shared_pool=False,
        wallet_group_name=None,
        stages=[ebay_wallet_stage, payoneer_stage, bridging_stage, bca_main_stage],
        transfer_elimination_idr=transfer_elimination,
    )


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
    rows = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(journal_lines.c.account_id == account_id)
        .where(journal_entries.c.period_month <= period_month)
    ).all()
    if normal_balance == "debit":
        return sum((r.debit_amount_idr - r.credit_amount_idr for r in rows), ZERO)
    return sum((r.credit_amount_idr - r.debit_amount_idr for r in rows), ZERO)


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
