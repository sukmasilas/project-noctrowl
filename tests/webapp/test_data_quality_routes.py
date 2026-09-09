"""Data Quality screen route smoke tests — renders without crashing on both
empty and populated (matching / discrepancy) reconciliation data, and never
exposes any correction/reopen action (pure detection and surfacing, per
CLAUDE.md's guardrail on this feature).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ledger.entities import get_account_id

from tests.webapp.conftest import make_reconciliation_check

PERIOD = _dt.date(2026, 7, 1)


def test_data_quality_renders_empty_state_when_nothing_checked_yet(logged_in_client, wtopology):
    resp = logged_in_client.get("/data-quality/?period=2026-07")
    assert resp.status_code == 200
    assert b"No reconciliation check has run yet" in resp.data


def test_data_quality_renders_a_clean_match(logged_in_client, wtopology):
    conn, topo = wtopology
    bridging_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    make_reconciliation_check(conn, account_id=bridging_id, period_month=PERIOD, is_material=False)
    conn.commit()

    resp = logged_in_client.get("/data-quality/?period=2026-07")
    assert resp.status_code == 200
    assert b"Matches" in resp.data
    assert b"Discrepancy found" not in resp.data


def test_data_quality_renders_a_material_discrepancy_without_any_fix_action(logged_in_client, wtopology):
    conn, topo = wtopology
    bridging_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    make_reconciliation_check(
        conn,
        account_id=bridging_id,
        period_month=PERIOD,
        is_material=True,
        expected_closing_idr=Decimal("1000000"),
        actual_closing_idr=Decimal("1500000"),
    )
    conn.commit()

    resp = logged_in_client.get("/data-quality/?period=2026-07")
    assert resp.status_code == 200
    assert b"Discrepancy found" in resp.data
    # This is a detection-only screen — never a correction/reopen action.
    assert b"Fix" not in resp.data
    assert b"Correct" not in resp.data
    assert b"Reopen" not in resp.data
    assert b"already labeled and posted" in resp.data  # no outstanding review rows for this scope
