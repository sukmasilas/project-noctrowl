"""Tests for webapp/reporting.py — the report aggregation + per-account
attribution logic. Uses the standard rollback-per-test `conn`/`prototype`/
`full_topology` fixtures from tests/conftest.py (these call
webapp.reporting functions directly against one open Connection — no
Flask/multi-connection concerns here, unlike tests/webapp/test_documents.py
etc., which need the committed wtopology/wengine fixtures instead).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ledger import posting
from webapp import reporting

RATE = Decimal("16300")
PERIOD = _dt.date(2026, 7, 1)
DAY = _dt.date(2026, 7, 10)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_attribution_direct_via_ebay_wallet_line(prototype):
    conn, topo = prototype
    entry_id = posting.post_ebay_sale(
        conn,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=DAY,
        gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"),
        kurs_pajak_rate=RATE,
        ebay_order_ref="ORDER1",
    )
    rows = reporting._revenue_lines(conn, PERIOD)
    attribution = reporting.attribute_entries_to_ebay_account(conn, rows)
    assert attribution[entry_id] == topo["ebay_account_id"]


def test_attribution_fallback_for_payoneer_stage_refund_distinguishes_shared_accounts(full_topology):
    """The core validation the coordinator asked for: two accounts share
    one Payoneer wallet, so a Payoneer-stage refund touches ONLY the
    shared PAYONEER_WALLET line directly — no per-account line at all.
    Attribution must still correctly tell account 1's refund apart from
    account 2's, via the ebay_order_ref fallback to each account's own
    original sale.
    """
    conn, topo = full_topology
    ebay_1 = topo["ebay_accounts"]["1"]
    ebay_2 = topo["ebay_accounts"]["2"]
    shared_wg = topo["wallet_groups"]["shared"]

    posting.post_ebay_sale(
        conn,
        ebay_account_id=ebay_1,
        entry_date=DAY,
        gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"),
        kurs_pajak_rate=RATE,
        ebay_order_ref="ACCOUNT1-ORDER",
    )
    posting.post_ebay_sale(
        conn,
        ebay_account_id=ebay_2,
        entry_date=DAY,
        gross_sale_price_usd=Decimal("50"),
        ebay_fee_usd=Decimal("5"),
        kurs_pajak_rate=RATE,
        ebay_order_ref="ACCOUNT2-ORDER",
    )

    refund_1_entry = posting.post_refund(
        conn,
        entry_date=DAY,
        amount_idr=Decimal("50000"),
        stage="payoneer",
        wallet_group_id=shared_wg,
        ebay_order_ref="ACCOUNT1-ORDER",
    )
    refund_2_entry = posting.post_refund(
        conn,
        entry_date=DAY,
        amount_idr=Decimal("20000"),
        stage="payoneer",
        wallet_group_id=shared_wg,
        ebay_order_ref="ACCOUNT2-ORDER",
    )

    rows = reporting._revenue_lines(conn, PERIOD)
    attribution = reporting.attribute_entries_to_ebay_account(conn, rows)
    assert attribution[refund_1_entry] == ebay_1
    assert attribution[refund_2_entry] == ebay_2


def test_attribution_unresolvable_entry_returns_none_not_a_guess(prototype):
    """A refund with no ebay_order_ref at all (nothing to fall back on)
    must be excluded, never silently attributed to whichever account
    happens to exist.
    """
    conn, topo = prototype
    entry_id = posting.post_refund(
        conn,
        entry_date=DAY,
        amount_idr=Decimal("10000"),
        stage="payoneer",
        wallet_group_id=topo["wallet_group_id"],
        ebay_order_ref=None,
    )
    rows = reporting._revenue_lines(conn, PERIOD)
    attribution = reporting.attribute_entries_to_ebay_account(conn, rows)
    assert attribution[entry_id] is None


# ---------------------------------------------------------------------------
# Revenue report
# ---------------------------------------------------------------------------


def test_revenue_report_per_account_excludes_other_accounts_sale(full_topology):
    conn, topo = full_topology
    ebay_1 = topo["ebay_accounts"]["1"]
    ebay_2 = topo["ebay_accounts"]["2"]

    posting.post_ebay_sale(
        conn, ebay_account_id=ebay_1, entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE, ebay_order_ref="A1",
    )
    posting.post_ebay_sale(
        conn, ebay_account_id=ebay_2, entry_date=DAY, gross_sale_price_usd=Decimal("200"),
        ebay_fee_usd=Decimal("20"), kurs_pajak_rate=RATE, ebay_order_ref="A2",
    )

    report_1 = reporting.revenue_report(conn, period_month=PERIOD, ebay_account_id=ebay_1)
    assert report_1.sales_revenue_idr == Decimal("100") * RATE
    assert report_1.unattributed_count == 0

    report_2 = reporting.revenue_report(conn, period_month=PERIOD, ebay_account_id=ebay_2)
    assert report_2.sales_revenue_idr == Decimal("200") * RATE

    consolidated = reporting.revenue_report(conn, period_month=PERIOD, ebay_account_id=None)
    assert consolidated.sales_revenue_idr == Decimal("300") * RATE


def test_revenue_report_payoneer_stage_refund_attributed_to_correct_account_only(full_topology):
    conn, topo = full_topology
    ebay_1 = topo["ebay_accounts"]["1"]
    ebay_2 = topo["ebay_accounts"]["2"]
    shared_wg = topo["wallet_groups"]["shared"]

    posting.post_ebay_sale(
        conn, ebay_account_id=ebay_1, entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE, ebay_order_ref="A1",
    )
    posting.post_ebay_sale(
        conn, ebay_account_id=ebay_2, entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE, ebay_order_ref="A2",
    )
    posting.post_refund(
        conn, entry_date=DAY, amount_idr=Decimal("30000"), stage="payoneer",
        wallet_group_id=shared_wg, ebay_order_ref="A1",
    )

    report_1 = reporting.revenue_report(conn, period_month=PERIOD, ebay_account_id=ebay_1)
    assert report_1.returns_allowances_idr == Decimal("30000")

    report_2 = reporting.revenue_report(conn, period_month=PERIOD, ebay_account_id=ebay_2)
    assert report_2.returns_allowances_idr == Decimal("0")


def test_revenue_report_includes_consignment_commission(prototype):
    conn, topo = prototype
    sale_id = posting.create_consignment_sale(
        conn,
        item_price_usd=Decimal("100"),
        payout_model="tier",
        payout_amount_idr=Decimal("1300000"),  # ~ 80% tier at rate 16300 (for a simple round number)
        consignor_item_ref="CONSIGN-1",
        tier_rate_percent=Decimal("80.00"),
        confirmed=True,
    )
    posting.post_consignment_sale(
        conn,
        consignment_sale_id=sale_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=DAY,
        gross_sale_price_usd=Decimal("110"),  # item + shipping
        ebay_fee_usd=Decimal("10"),
        kurs_pajak_rate=RATE,
        ebay_order_ref="CONSIGN-ORDER",
    )
    report = reporting.revenue_report(conn, period_month=PERIOD, ebay_account_id=topo["ebay_account_id"])
    assert report.consignment_commission_idr > 0
    # Commission = gross - fee - payout (all in IDR).
    expected_commission = (Decimal("110") * RATE) - Decimal("1300000")
    assert report.consignment_commission_idr == expected_commission


# ---------------------------------------------------------------------------
# Statement of Cash Flows (consolidated, direct method)
# ---------------------------------------------------------------------------


def _bucket(report, key):
    for line in report.operating_lines + report.financing_lines:
        if line.key == key:
            return line.amount_idr
    return None


def test_cash_flow_ebay_sale_splits_into_customers_and_fee_lines(prototype):
    conn, topo = prototype
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE, ebay_order_ref="S1",
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert _bucket(report, "cash_from_customers") == Decimal("100") * RATE
    assert _bucket(report, "ebay_selling_fees") == -(Decimal("10") * RATE)
    # Beginning (0) + net change must equal the real ending cash exactly.
    assert report.difference_idr == Decimal("0")
    assert report.ending_cash_idr == report.beginning_cash_idr + report.net_change_idr
    assert report.ending_cash_idr == Decimal("90") * RATE  # net eBay Wallet inflow


def test_cash_flow_refund_at_ebay_wallet_reduces_customer_receipts(prototype):
    conn, topo = prototype
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE, ebay_order_ref="S2",
    )
    posting.post_refund(
        conn, entry_date=DAY, amount_idr=Decimal("200000"), stage="ebay_wallet",
        ebay_account_id=topo["ebay_account_id"], ebay_order_ref="S2",
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert _bucket(report, "cash_from_customers") == (Decimal("100") * RATE) - Decimal("200000")
    assert report.difference_idr == Decimal("0")


def test_cash_flow_cogs_purchase_is_operating_outflow(prototype):
    conn, topo = prototype
    posting.post_cogs_purchase(conn, entry_date=DAY, amount_idr=Decimal("1000000"))
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert _bucket(report, "cogs_purchases") == Decimal("-1000000")
    assert report.total_operating_idr == Decimal("-1000000")
    assert report.difference_idr == Decimal("0")


def test_cash_flow_employee_loan_disbursement_and_repayment_reconciles(prototype):
    """2026-09-10: EMPLOYEE_LOAN_RECEIVABLE (a new asset account, not a
    revenue/expense) must be classified in the cash-flow non-cash whitelist
    (webapp.reporting._CASH_FLOW_CODE_TO_KEY) — otherwise a real disbursement
    or embedded-in-payroll repayment (both touch a cash account on one side
    and this asset account on the other) would silently break the
    Beginning+NetChange=Ending identity (difference_idr). This is the
    concrete regression test for that fix.
    """
    conn, topo = prototype
    posting.post_employee_loan_disbursement(
        conn, entry_date=DAY, amount_idr=Decimal("27000000"), employee_ref="Fariz Pradana"
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert _bucket(report, "employee_loans") == Decimal("-27000000")
    assert report.total_operating_idr == Decimal("-27000000")
    assert report.difference_idr == Decimal("0")

    posting.post_payroll_with_loan_repayment(
        conn,
        entry_date=DAY,
        net_transfer_idr=Decimal("8500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )
    report2 = reporting.cash_flow_statement(conn, period_month=PERIOD)
    # Net employee_loans bucket = -27,000,000 (disbursement) + 1,500,000
    # (repayment credit) = -25,500,000; payroll bucket carries the FULL
    # gross 10,000,000 payroll expense, not the reduced net transfer.
    assert _bucket(report2, "employee_loans") == Decimal("-25500000")
    assert _bucket(report2, "payroll") == Decimal("-10000000")
    assert report2.difference_idr == Decimal("0")


def test_cash_flow_staff_meals_welfare_is_operating_outflow(prototype):
    """2026-09-24: STAFF_MEALS_WELFARE (new opex account, real team-meal
    cost) must post as its own bucketed Operating outflow and reconcile
    exactly, same shape as test_cash_flow_cogs_purchase_is_operating_outflow
    above.
    """
    conn, topo = prototype
    posting.post_operating_expense(
        conn, entry_date=DAY, expense_account_type_code="STAFF_MEALS_WELFARE", amount_idr=Decimal("520000"),
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert _bucket(report, "staff_meals_welfare") == Decimal("-520000")
    assert report.total_operating_idr == Decimal("-520000")
    assert report.difference_idr == Decimal("0")


def test_cash_flow_staff_meals_welfare_negative_control_breaks_identity_if_whitelist_omits_it(prototype, monkeypatch):
    """Negative-control regression test (per CLAUDE.md's Packaging Supplies
    precedent — see the 2026-09-11 entry under Chart of accounts): proves
    the Beginning+NetChange=Ending identity ACTUALLY DEPENDS on
    STAFF_MEALS_WELFARE being present in webapp.reporting._CASH_FLOW_CODE_TO_KEY,
    not merely that the code doesn't crash without it. Temporarily removes
    the whitelist entry, reposts the same real-shaped transaction, and
    confirms difference_idr breaks by EXACTLY the posted amount — the same
    class of silent corruption a missing whitelist entry caused for real
    once already (see CLAUDE.md's Packaging Supplies note).
    """
    conn, topo = prototype
    patched = dict(reporting._CASH_FLOW_CODE_TO_KEY)
    del patched["STAFF_MEALS_WELFARE"]
    monkeypatch.setattr(reporting, "_CASH_FLOW_CODE_TO_KEY", patched)

    posting.post_operating_expense(
        conn, entry_date=DAY, expense_account_type_code="STAFF_MEALS_WELFARE", amount_idr=Decimal("520000"),
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    # The cash leg (BCA_MAIN credit) still reduces ending cash, but with the
    # whitelist entry gone, the expense's own Operating line silently drops
    # out of the total — so Beginning+NetChange no longer equals Ending,
    # off by exactly the amount that should have been bucketed.
    assert _bucket(report, "staff_meals_welfare") is None  # bucket silently vanished
    assert report.difference_idr == Decimal("-520000")


def test_cash_flow_inter_account_transfer_never_appears_in_any_bucket(prototype):
    """An inter_account_transfer only ever touches two cash accounts (see
    ledger/schema.py's trg_check_transfer_accounts) — it should net to zero
    contribution across every Operating/Investing/Financing bucket, since
    the whole point is it never touches revenue/expense.
    """
    conn, topo = prototype
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("0"), kurs_pajak_rate=RATE, ebay_order_ref="S3",
    )
    posting.post_inter_account_transfer(
        conn, entry_date=DAY, from_account_type_code="EBAY_WALLET", to_account_type_code="PAYONEER_WALLET",
        amount_idr=Decimal("100") * RATE, from_ebay_account_id=topo["ebay_account_id"],
        to_wallet_group_id=topo["wallet_group_id"],
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    # Total cash unaffected by the transfer leg itself (still just the sale).
    assert report.ending_cash_idr == Decimal("100") * RATE
    assert report.total_operating_idr == Decimal("100") * RATE  # only the sale contributes
    assert report.difference_idr == Decimal("0")


def test_cash_flow_consignment_tier_model_customers_includes_consignor_accrual(prototype):
    """The full buyer payment (including the portion owed to the consignor)
    is real cash landing in the eBay Wallet at sale time — see module
    docstring's CONSIGNOR_PAYABLE reasoning. Reimbursement is its own,
    later, separate outflow line.
    """
    conn, topo = prototype
    sale_id = posting.create_consignment_sale(
        conn, item_price_usd=Decimal("100"), payout_model="tier", payout_amount_idr=Decimal("1300000"),
        consignor_item_ref="CONSIGN-1", tier_rate_percent=Decimal("80.00"), confirmed=True,
    )
    posting.post_consignment_sale(
        conn, consignment_sale_id=sale_id, ebay_account_id=topo["ebay_account_id"], entry_date=DAY,
        gross_sale_price_usd=Decimal("110"), ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE,
        ebay_order_ref="CONSIGN-ORDER",
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    gross_idr = Decimal("110") * RATE
    fee_idr = Decimal("10") * RATE
    commission_idr = gross_idr - Decimal("1300000")
    # customers bucket = SALES-side revenue-ish credits: commission + the
    # consignor-payable accrual (payout_idr) — no SALES_REVENUE here (this
    # is a consignment sale, not a stock/pre-order one).
    assert _bucket(report, "cash_from_customers") == commission_idr + Decimal("1300000")
    assert _bucket(report, "ebay_selling_fees") == -fee_idr
    assert _bucket(report, "consignor_payouts") is None  # nothing paid out yet
    assert report.difference_idr == Decimal("0")

    posting.post_consignor_reimbursement(
        conn, entry_date=DAY, amount_idr=Decimal("1300000"), consignor_item_ref="CONSIGN-1",
    )
    report2 = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert _bucket(report2, "consignor_payouts") == Decimal("-1300000")
    assert report2.difference_idr == Decimal("0")


def test_cash_flow_owners_capital_and_draw_reported_gross_not_netted(prototype):
    conn, topo = prototype
    posting.post_owner_contribution(conn, entry_date=DAY, amount_idr=Decimal("5000000"))
    posting.post_owner_draw(conn, entry_date=DAY, amount_idr=Decimal("2000000"))
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert _bucket(report, "owners_capital") == Decimal("5000000")
    assert _bucket(report, "owners_draw") == Decimal("-2000000")
    assert report.total_financing_idr == Decimal("3000000")
    assert report.difference_idr == Decimal("0")


def test_cash_flow_opening_balance_excluded_from_financing_but_counted_in_beginning_cash(prototype):
    conn, topo = prototype
    posting.post_opening_balance(
        conn, account_type_code="PAYONEER_WALLET", entry_date=PERIOD, amount_idr=Decimal("75000000"),
        wallet_group_id=topo["wallet_group_id"],
    )
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    # Never a Financing line — see module docstring.
    assert _bucket(report, "owners_capital") is None
    # But it IS what makes this period's beginning cash correct (booked on
    # the 1st of the period it represents the opening of — see
    # ledger/balances.py's account_balance_before docstring).
    assert report.beginning_cash_idr == Decimal("75000000")
    assert report.ending_cash_idr == Decimal("75000000")
    assert report.difference_idr == Decimal("0")


def test_cash_flow_unrealized_fx_excluded_from_oif_but_reconciles_ending_cash(prototype):
    conn, topo = prototype
    posting.post_opening_balance(
        conn, account_type_code="PAYONEER_WALLET", entry_date=PERIOD, amount_idr=Decimal("16000000"),
        wallet_group_id=topo["wallet_group_id"], amount_usd_ref=Decimal("1000"), fx_rate_used=Decimal("16000"),
    )
    entry_id = posting.post_unrealized_fx_revaluation(
        conn, wallet_group_id=topo["wallet_group_id"], period_month=PERIOD, usd_balance=Decimal("1000"),
        current_book_value_idr=Decimal("16000000"), kemenkeu_eom_rate_idr=Decimal("16500"),
    )
    assert entry_id is not None  # a real Rp 500,000 unrealized gain
    report = reporting.cash_flow_statement(conn, period_month=PERIOD)
    assert report.total_operating_idr == Decimal("0")
    assert report.total_financing_idr == Decimal("0")
    assert report.fx_effect_idr == Decimal("500000")
    assert report.ending_cash_idr == Decimal("16500000")
    # The identity still ties out exactly BECAUSE fx_effect is included in
    # net_change (outside O/I/F, but not outside the total).
    assert report.beginning_cash_idr + report.net_change_idr == report.ending_cash_idr
    assert report.difference_idr == Decimal("0")


def test_cash_flow_beginning_cash_of_next_period_equals_prior_ending(prototype):
    conn, topo = prototype
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("0"), kurs_pajak_rate=RATE, ebay_order_ref="S4",
    )
    july = reporting.cash_flow_statement(conn, period_month=PERIOD)
    august = reporting.cash_flow_statement(conn, period_month=_dt.date(2026, 8, 1))
    assert august.beginning_cash_idr == july.ending_cash_idr
    assert august.difference_idr == Decimal("0")


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------


def test_pnl_report_gross_profit_and_net_income(prototype):
    conn, topo = prototype
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("200"),
        ebay_fee_usd=Decimal("20"), kurs_pajak_rate=RATE, ebay_order_ref="P1",
    )
    posting.post_cogs_purchase(conn, entry_date=DAY, amount_idr=Decimal("1000000"))
    posting.post_operating_expense(
        conn, entry_date=DAY, expense_account_type_code="GENERAL_OPEX", amount_idr=Decimal("300000")
    )

    report = reporting.pnl_report(conn, period_month=PERIOD)
    assert report.total_revenue_idr == Decimal("200") * RATE
    assert report.cogs_idr == Decimal("1000000")
    assert report.gross_profit_idr == report.total_revenue_idr - Decimal("1000000")
    # eBay Selling Fees (20 * RATE) + General Opex (300000) both counted.
    assert report.total_opex_idr == (Decimal("20") * RATE) + Decimal("300000")
    assert report.operating_income_idr == report.gross_profit_idr - report.total_opex_idr
    assert report.net_income_idr == report.operating_income_idr  # no FX/interest posted in this test


def test_pnl_report_keeps_realized_and_unrealized_fx_as_distinct_lines(prototype):
    conn, topo = prototype
    posting.post_realized_fx_withdrawal(
        conn,
        wallet_group_id=topo["wallet_group_id"],
        entry_date=DAY,
        gross_usd=Decimal("100"),
        payoneer_fee_usd=Decimal("2"),
        exchange_rate_excl_fee=Decimal("16400"),
        booking_rate_used_idr=Decimal("16300"),
    )
    report = reporting.pnl_report(conn, period_month=PERIOD)
    codes = {line.code for line in report.other_income_expense_lines}
    assert "REALIZED_FX" in codes
    assert "UNREALIZED_FX" not in codes  # never posted in this test — must not appear as a fabricated $0 line either way, but definitely not blended


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------


def test_equity_report_is_cumulative_across_periods(prototype):
    conn, topo = prototype
    posting.post_owner_contribution(conn, entry_date=_dt.date(2026, 6, 15), amount_idr=Decimal("10000000"))
    posting.post_owner_draw(conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("1000000"))

    report_june = reporting.equity_report(conn, period_month=_dt.date(2026, 6, 1))
    assert report_june.owners_capital_idr == Decimal("10000000")
    assert report_june.owners_draw_idr == Decimal("0")

    report_july = reporting.equity_report(conn, period_month=_dt.date(2026, 7, 1))
    assert report_july.owners_capital_idr == Decimal("10000000")  # still included (cumulative)
    assert report_july.owners_draw_idr == Decimal("1000000")
    assert report_july.ending_balance_idr == Decimal("10000000") - Decimal("1000000")


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------


def test_drilldown_lines_sum_to_the_same_headline_figure(prototype):
    conn, topo = prototype
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("300"),
        ebay_fee_usd=Decimal("30"), kurs_pajak_rate=RATE, ebay_order_ref="D1",
    )
    report = reporting.revenue_report(conn, period_month=PERIOD, ebay_account_id=topo["ebay_account_id"])
    lines = reporting.drilldown(
        conn, account_type_codes=["SALES_REVENUE"], period_month=PERIOD, ebay_account_id=topo["ebay_account_id"]
    )
    summed = sum((l.credit_idr - l.debit_idr for l in lines), Decimal("0"))
    assert summed == report.sales_revenue_idr


# ---------------------------------------------------------------------------
# Retained Earnings fix — must be a computed cumulative net-income figure,
# not the (always-zero, nothing-ever-posts-to-it) RETAINED_EARNINGS account
# balance. See webapp/reporting.py's _cumulative_net_income.
# ---------------------------------------------------------------------------


def test_retained_earnings_is_computed_from_cumulative_net_income_not_the_zero_account(prototype):
    conn, topo = prototype
    # A sale (revenue + COGS-adjacent fee), a COGS purchase, and an opex
    # payment — enough to make net income genuinely nonzero and checkable
    # by hand, not just "some nonzero number".
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("300"),
        ebay_fee_usd=Decimal("30"), kurs_pajak_rate=RATE, ebay_order_ref="RE1",
    )
    posting.post_cogs_purchase(conn, entry_date=DAY, amount_idr=Decimal("1000000"))
    posting.post_operating_expense(
        conn, entry_date=DAY, expense_account_type_code="GENERAL_OPEX", amount_idr=Decimal("500000"),
    )

    # Nothing ever posts to the RETAINED_EARNINGS account type itself — the
    # bug this fixes is exactly that reading its balance always gives 0.
    from sqlalchemy import select as _select

    from ledger.entities import get_account_id
    from ledger.schema import journal_lines as _jl

    re_account_id = get_account_id(conn, "RETAINED_EARNINGS")
    re_lines = conn.execute(_select(_jl.c.id).where(_jl.c.account_id == re_account_id)).all()
    assert re_lines == []  # confirms the account genuinely has zero postings

    pnl = reporting.pnl_report(conn, period_month=PERIOD)
    equity = reporting.equity_report(conn, period_month=PERIOD)

    assert pnl.net_income_idr != Decimal("0")
    assert equity.retained_earnings_idr == pnl.net_income_idr
    assert equity.ending_balance_idr == equity.owners_capital_idr - equity.owners_draw_idr + pnl.net_income_idr


def test_retained_earnings_accumulates_across_periods(prototype):
    conn, topo = prototype
    june = _dt.date(2026, 6, 1)
    july = _dt.date(2026, 7, 1)

    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=_dt.date(2026, 6, 10),
        gross_sale_price_usd=Decimal("100"), ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE, ebay_order_ref="JUN1",
    )
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY,
        gross_sale_price_usd=Decimal("50"), ebay_fee_usd=Decimal("5"), kurs_pajak_rate=RATE, ebay_order_ref="JUL1",
    )

    june_equity = reporting.equity_report(conn, period_month=june)
    july_equity = reporting.equity_report(conn, period_month=july)

    # July's retained earnings includes June's income too (cumulative).
    assert july_equity.retained_earnings_idr > june_equity.retained_earnings_idr
    june_pnl = reporting.pnl_report(conn, period_month=june)
    july_pnl = reporting.pnl_report(conn, period_month=july)
    assert june_equity.retained_earnings_idr == june_pnl.net_income_idr
    assert july_equity.retained_earnings_idr == june_pnl.net_income_idr + july_pnl.net_income_idr


# ---------------------------------------------------------------------------
# Balance Sheet
# ---------------------------------------------------------------------------


def test_balance_sheet_balances_exactly_with_a_realistic_mix_of_transactions(full_topology):
    """Assets = Liabilities + Equity must hold EXACTLY (not approximately)
    once every account type is correctly bucketed — see
    webapp/reporting.py's BalanceSheetReport.difference_idr docstring for
    why this is a mathematical property of double-entry bookkeeping here,
    not something to fudge. Exercises assets (eBay Wallet via a sale, BCA
    Main via COGS/opex/owner transactions), a liability (Consignor Payable,
    via a confirmed consignment sale not yet reimbursed), and equity
    (Capital, Draw, and the fixed Retained Earnings computation) all in one
    scenario.
    """
    from ledger.consignment import calc_tier_payout_usd
    from ledger.posting import confirm_consignment_sale, create_consignment_sale, post_consignment_sale

    conn, topo = full_topology
    ebay_1 = topo["ebay_accounts"]["1"]

    posting.post_owner_contribution(conn, entry_date=_dt.date(2026, 6, 1), amount_idr=Decimal("50000000"))
    posting.post_ebay_sale(
        conn, ebay_account_id=ebay_1, entry_date=DAY, gross_sale_price_usd=Decimal("300"),
        ebay_fee_usd=Decimal("30"), kurs_pajak_rate=RATE, ebay_order_ref="BS1",
    )
    posting.post_cogs_purchase(conn, entry_date=DAY, amount_idr=Decimal("1000000"))
    posting.post_operating_expense(
        conn, entry_date=DAY, expense_account_type_code="PAYROLL", amount_idr=Decimal("2000000"),
    )
    posting.post_owner_draw(conn, entry_date=DAY, amount_idr=Decimal("500000"))

    suggestion = calc_tier_payout_usd(Decimal("80.00"), Decimal("80.00"))
    cs_id = create_consignment_sale(
        conn, item_price_usd=Decimal("80.00"), payout_model="tier", tier_rate_percent=Decimal("80.00"),
        payout_amount_idr=suggestion * RATE, consignor_item_ref="CONSIGN-BS-1",
    )
    confirm_consignment_sale(conn, cs_id)
    post_consignment_sale(
        conn, consignment_sale_id=cs_id, ebay_account_id=ebay_1, entry_date=DAY,
        gross_sale_price_usd=Decimal("90.00"), ebay_fee_usd=Decimal("10.00"), kurs_pajak_rate=RATE,
        ebay_order_ref="BS-CONSIGN-1",
    )

    bs = reporting.balance_sheet_report(conn, period_month=PERIOD)
    assert bs.difference_idr == Decimal("0")
    assert bs.total_assets_idr == bs.total_liabilities_and_equity_idr
    assert bs.total_liabilities_idr > Decimal("0")  # the unreimbursed consignor payable
    assert bs.total_equity_idr == bs.owners_capital_idr - bs.owners_draw_idr + bs.retained_earnings_idr


def test_balance_sheet_lists_one_line_per_account_instance_not_per_type(full_topology):
    """3 eBay Wallets (per-account) and 2 Payoneer Wallets/BCA Bridging
    Accounts (per-wallet-group, one shared by two eBay accounts) should each
    show as their OWN line — never blended into one type-level total (see
    CLAUDE.md's chart-of-accounts scoping).
    """
    conn, topo = full_topology
    bs = reporting.balance_sheet_report(conn, period_month=PERIOD)
    ebay_wallet_lines = [l for l in bs.asset_lines if l.account_type_code == "EBAY_WALLET"]
    payoneer_lines = [l for l in bs.asset_lines if l.account_type_code == "PAYONEER_WALLET"]
    bridging_lines = [l for l in bs.asset_lines if l.account_type_code == "BCA_BRIDGING"]
    bca_main_lines = [l for l in bs.asset_lines if l.account_type_code == "BCA_MAIN"]
    assert len(ebay_wallet_lines) == 3
    assert len(payoneer_lines) == 2
    assert len(bridging_lines) == 2
    assert len(bca_main_lines) == 1


def test_balance_sheet_account_instance_drilldown_sums_to_the_line_balance(prototype):
    conn, topo = prototype
    posting.post_owner_contribution(conn, entry_date=DAY, amount_idr=Decimal("10000000"))
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=RATE, ebay_order_ref="BS-DD1",
    )
    bs = reporting.balance_sheet_report(conn, period_month=PERIOD)
    bca_main_line = next(l for l in bs.asset_lines if l.account_type_code == "BCA_MAIN")
    lines = reporting.account_instance_drilldown(conn, account_id=bca_main_line.account_id, period_month=PERIOD)
    summed = sum((l.debit_idr - l.credit_idr for l in lines), Decimal("0"))
    assert summed == bca_main_line.balance_idr
