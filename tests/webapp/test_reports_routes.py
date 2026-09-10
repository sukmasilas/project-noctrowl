"""Report view route smoke tests — renders without crashing on both empty
and populated data, P&L/Equity force-consolidated, drill-down reachable
from a report figure.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ledger import posting

PERIOD = _dt.date(2026, 7, 1)
DAY = _dt.date(2026, 7, 10)


def test_revenue_report_renders_empty_period(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/revenue?period=2026-07")
    assert resp.status_code == 200
    assert b"Provisional" in resp.data  # nothing ingested yet for this period


def test_revenue_report_renders_with_data(logged_in_client, wtopology):
    conn, topo = wtopology
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=Decimal("16300"), ebay_order_ref="R1",
    )
    conn.commit()
    resp = logged_in_client.get(f"/reports/revenue?period=2026-07&account_id={topo['ebay_account_id']}")
    assert resp.status_code == 200


def test_cash_flow_report_renders(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/cash-flow?period=2026-07")
    assert resp.status_code == 200
    assert b"Statement of Cash Flows" in resp.data


def test_cash_flow_report_is_consolidated_only(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/cash-flow?period=2026-07")
    assert resp.status_code == 200
    assert b"disabled" in resp.data  # account selector locked out, same as P&L/Equity/Balance Sheet


def test_cash_flow_drilldown_route_renders(logged_in_client, wtopology):
    conn, topo = wtopology
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("50"),
        ebay_fee_usd=Decimal("5"), kurs_pajak_rate=Decimal("16300"), ebay_order_ref="CF-DD",
    )
    conn.commit()
    resp = logged_in_client.get("/reports/cash-flow/drilldown/cash_from_customers?period=2026-07")
    assert resp.status_code == 200
    assert b"CF-DD" in resp.data


def test_cash_flow_drilldown_unknown_key_404s(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/cash-flow/drilldown/not_a_real_key?period=2026-07")
    assert resp.status_code == 404


def test_pnl_report_renders_and_is_consolidated_only(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/pnl?period=2026-07")
    assert resp.status_code == 200
    assert b"disabled" in resp.data  # account selector locked out


def test_equity_report_renders(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/equity?period=2026-07")
    assert resp.status_code == 200


def test_drilldown_route_renders_matching_lines(logged_in_client, wtopology):
    conn, topo = wtopology
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("50"),
        ebay_fee_usd=Decimal("5"), kurs_pajak_rate=Decimal("16300"), ebay_order_ref="R2",
    )
    conn.commit()
    resp = logged_in_client.get(f"/reports/drilldown/sales_revenue?period=2026-07&account_id={topo['ebay_account_id']}")
    assert resp.status_code == 200
    assert b"R2" in resp.data


def test_drilldown_unknown_figure_404s(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/drilldown/not_a_real_figure?period=2026-07")
    assert resp.status_code == 404


def test_balance_sheet_renders_empty_period(logged_in_client, wtopology):
    resp = logged_in_client.get("/reports/balance-sheet?period=2026-07")
    assert resp.status_code == 200
    assert b"Provisional" in resp.data


def test_balance_sheet_renders_with_data_and_balances(logged_in_client, wtopology):
    conn, topo = wtopology
    posting.post_owner_contribution(conn, entry_date=DAY, amount_idr=Decimal("10000000"))
    posting.post_ebay_sale(
        conn, ebay_account_id=topo["ebay_account_id"], entry_date=DAY, gross_sale_price_usd=Decimal("100"),
        ebay_fee_usd=Decimal("10"), kurs_pajak_rate=Decimal("16300"), ebay_order_ref="BSROUTE1",
    )
    conn.commit()
    resp = logged_in_client.get("/reports/balance-sheet?period=2026-07")
    assert resp.status_code == 200
    assert b"disabled" in resp.data  # account selector locked out — consolidated only
    assert b"do not equal" not in resp.data  # no imbalance warning rendered


def test_balance_sheet_account_drilldown_route_renders(logged_in_client, wtopology):
    conn, topo = wtopology
    posting.post_owner_contribution(conn, entry_date=DAY, amount_idr=Decimal("10000000"))
    conn.commit()
    from webapp import reporting

    bs = reporting.balance_sheet_report(conn, period_month=PERIOD)
    bca_main = next(l for l in bs.asset_lines if l.account_type_code == "BCA_MAIN")
    resp = logged_in_client.get(f"/reports/balance-sheet/drilldown/{bca_main.account_id}?period=2026-07")
    assert resp.status_code == 200
