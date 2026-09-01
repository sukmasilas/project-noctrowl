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
