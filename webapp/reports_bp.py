"""Report views: Revenue, Cash Flow (per-account + consolidated), P&L and
Equity (consolidated-only), plus the shared drill-down panel.

Read-only, computed live from Postgres — the user only views these (see
CLAUDE.md's Architecture section). See
docs/design/milestone-4-web-app-design.md §3.
"""
from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from webapp import reporting
from webapp.auth import login_required
from webapp.db import get_db
from webapp.finalization import report_status
from webapp.scoping import list_ebay_accounts, parse_period

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
@login_required
def index():
    return redirect(url_for("reports.revenue"))


def _scope_from_request(conn):
    """Returns (accounts, selected_account_or_None, is_consolidated)."""
    accounts = list_ebay_accounts(conn)
    raw = request.args.get("account_id", "consolidated")
    if raw == "consolidated" or not accounts:
        return accounts, None, True
    try:
        account_id = int(raw)
    except ValueError:
        return accounts, None, True
    account = next((a for a in accounts if a.id == account_id), None)
    if account is None:
        return accounts, None, True
    return accounts, account, False


@bp.route("/revenue")
@login_required
def revenue():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    accounts, account, is_consolidated = _scope_from_request(conn)
    ebay_account_id = None if is_consolidated else account.id

    report = reporting.revenue_report(conn, period_month=period_month, ebay_account_id=ebay_account_id)
    status = report_status(conn, period_month=period_month, ebay_account_id=ebay_account_id)

    return render_template(
        "reports/revenue.html",
        accounts=accounts,
        selected_account=account,
        is_consolidated=is_consolidated,
        period_month=period_month,
        report=report,
        status=status,
    )


@bp.route("/cash-flow")
@login_required
def cash_flow():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    accounts, account, is_consolidated = _scope_from_request(conn)
    ebay_account_id = None if is_consolidated else account.id

    report = reporting.cash_flow_report(conn, period_month=period_month, ebay_account_id=ebay_account_id)
    status = report_status(conn, period_month=period_month, ebay_account_id=ebay_account_id)

    return render_template(
        "reports/cash_flow.html",
        accounts=accounts,
        selected_account=account,
        is_consolidated=is_consolidated,
        period_month=period_month,
        report=report,
        status=status,
    )


@bp.route("/pnl")
@login_required
def pnl():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    report = reporting.pnl_report(conn, period_month=period_month)
    status = report_status(conn, period_month=period_month)  # consolidated only
    return render_template("reports/pnl.html", period_month=period_month, report=report, status=status)


@bp.route("/equity")
@login_required
def equity():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    report = reporting.equity_report(conn, period_month=period_month)
    status = report_status(conn, period_month=period_month)  # consolidated only
    return render_template("reports/equity.html", period_month=period_month, report=report, status=status)


@bp.route("/balance-sheet")
@login_required
def balance_sheet():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    report = reporting.balance_sheet_report(conn, period_month=period_month)
    status = report_status(conn, period_month=period_month)  # consolidated only, same gating as P&L/Equity
    return render_template(
        "reports/balance_sheet.html", period_month=period_month, report=report, status=status
    )


@bp.route("/balance-sheet/drilldown/<int:account_id>")
@login_required
def balance_sheet_drilldown(account_id: int):
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    lines = reporting.account_instance_drilldown(conn, account_id=account_id, period_month=period_month)
    return render_template(
        "reports/drilldown.html", figure=f"balance-sheet account #{account_id}", lines=lines, period_month=period_month
    )


_DRILLDOWN_CODES = {
    "sales_revenue": (["SALES_REVENUE"], False),
    "returns_allowances": (["SALES_RETURNS_ALLOWANCES"], False),
    "consignment_commission": (["CONSIGNMENT_COMMISSION_INCOME"], False),
    "cogs": (["COGS"], False),
    "opex": (
        ["EBAY_SELLING_FEES", "PAYOUT_FEE", "PAYROLL", "GENERAL_OPEX", "SHIPPING_COST", "CONTRACT_LABOR"],
        False,
    ),
    "realized_fx": (["REALIZED_FX"], False),
    "unrealized_fx": (["UNREALIZED_FX"], False),
    "interest_income": (["INTEREST_INCOME"], False),
    "owners_capital": (["OWNERS_CAPITAL"], True),
    "owners_draw": (["OWNERS_DRAW"], True),
    # Retained Earnings is a COMPUTED cumulative net-income figure, not a
    # posted account balance (see webapp/reporting.py's
    # _cumulative_net_income — nothing ever posts to the RETAINED_EARNINGS
    # account type). Its drill-down is therefore every P&L account's lines,
    # cumulative through period end — the exact set that nets to the
    # retained-earnings figure — not the (always-empty) RETAINED_EARNINGS
    # account itself.
    "retained_earnings": (
        [
            "SALES_REVENUE",
            "SALES_RETURNS_ALLOWANCES",
            "CONSIGNMENT_COMMISSION_INCOME",
            "COGS",
            "EBAY_SELLING_FEES",
            "PAYOUT_FEE",
            "PAYROLL",
            "GENERAL_OPEX",
            "SHIPPING_COST",
            "CONTRACT_LABOR",
            "REALIZED_FX",
            "UNREALIZED_FX",
            "INTEREST_INCOME",
        ],
        True,
    ),
}


@bp.route("/drilldown/<figure>")
@login_required
def drilldown(figure: str):
    conn = get_db()
    if figure not in _DRILLDOWN_CODES:
        abort(404)
    codes, cumulative = _DRILLDOWN_CODES[figure]
    period_month = parse_period(request.args.get("period"), conn)
    account_id = request.args.get("account_id", type=int)

    lines = reporting.drilldown(
        conn,
        account_type_codes=codes,
        period_month=period_month,
        ebay_account_id=account_id,
        period_is_cumulative=cumulative,
    )
    return render_template("reports/drilldown.html", figure=figure, lines=lines, period_month=period_month)
