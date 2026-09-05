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
