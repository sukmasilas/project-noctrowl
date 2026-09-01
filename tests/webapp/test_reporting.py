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
# Cash Flow
# ---------------------------------------------------------------------------


def test_cash_flow_shared_wallet_group_shown_identically_per_account_and_deduped_consolidated(full_topology):
    conn, topo = full_topology
    ebay_1 = topo["ebay_accounts"]["1"]
    ebay_2 = topo["ebay_accounts"]["2"]
    shared_wg = topo["wallet_groups"]["shared"]

    posting.post_inter_account_transfer(
        conn,
        entry_date=DAY,
        from_account_type_code="PAYONEER_WALLET",
        to_account_type_code="BCA_BRIDGING",
        amount_idr=Decimal("5000000"),
        from_wallet_group_id=shared_wg,
        to_wallet_group_id=shared_wg,
    )

    cf_1 = reporting.cash_flow_report(conn, period_month=PERIOD, ebay_account_id=ebay_1)
    cf_2 = reporting.cash_flow_report(conn, period_month=PERIOD, ebay_account_id=ebay_2)
    assert cf_1.is_shared_pool is True
    assert cf_2.is_shared_pool is True

    payoneer_stage_1 = next(s for s in cf_1.stages if "Payoneer" in s.label)
    payoneer_stage_2 = next(s for s in cf_2.stages if "Payoneer" in s.label)
    assert payoneer_stage_1.net_idr == payoneer_stage_2.net_idr == Decimal("-5000000")

    consolidated = reporting.cash_flow_report(conn, period_month=PERIOD, ebay_account_id=None)
    consolidated_payoneer = next(s for s in consolidated.stages if s.label.startswith("Payoneer"))
    # Must NOT be double-counted (-5,000,000 * 2) just because two accounts
    # share this wallet-group — the whole point of deduping by wallet_group.
    assert consolidated_payoneer.net_idr == Decimal("-5000000")


def test_cash_flow_independent_account_not_marked_shared(full_topology):
    conn, topo = full_topology
    ebay_3 = topo["ebay_accounts"]["3"]
    cf_3 = reporting.cash_flow_report(conn, period_month=PERIOD, ebay_account_id=ebay_3)
    assert cf_3.is_shared_pool is False


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
