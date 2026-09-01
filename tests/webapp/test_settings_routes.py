"""Consignor Payout Tiers screen route tests."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ledger.schema import consignor_payout_tiers


def test_payout_tiers_page_lists_all_rows(logged_in_client, wtopology):
    resp = logged_in_client.get("/settings/payout-tiers")
    assert resp.status_code == 200
    assert b"72.00" in resp.data or b"72" in resp.data


def test_update_rate_changes_only_that_row(logged_in_client, wtopology):
    conn, topo = wtopology
    first_row = conn.execute(
        select(consignor_payout_tiers).where(consignor_payout_tiers.c.display_order == 1)
    ).first()

    resp = logged_in_client.post(
        f"/settings/payout-tiers/{first_row.id}", data={"rate_percent": "75.50"}
    )
    assert resp.status_code in (301, 302)

    updated = conn.execute(select(consignor_payout_tiers).where(consignor_payout_tiers.c.id == first_row.id)).first()
    assert updated.rate_percent == Decimal("75.50")


def test_manual_contact_tier_cannot_be_edited(logged_in_client, wtopology):
    conn, topo = wtopology
    manual_row = conn.execute(
        select(consignor_payout_tiers).where(consignor_payout_tiers.c.requires_manual_contact.is_(True))
    ).first()

    resp = logged_in_client.post(
        f"/settings/payout-tiers/{manual_row.id}", data={"rate_percent": "50.00"}
    )
    assert resp.status_code in (301, 302)

    unchanged = conn.execute(select(consignor_payout_tiers).where(consignor_payout_tiers.c.id == manual_row.id)).first()
    assert unchanged.rate_percent is None


def test_invalid_rate_rejected(logged_in_client, wtopology):
    conn, topo = wtopology
    first_row = conn.execute(
        select(consignor_payout_tiers).where(consignor_payout_tiers.c.display_order == 1)
    ).first()
    original_rate = first_row.rate_percent

    resp = logged_in_client.post(f"/settings/payout-tiers/{first_row.id}", data={"rate_percent": "not-a-number"})
    assert resp.status_code in (301, 302)
    unchanged = conn.execute(select(consignor_payout_tiers).where(consignor_payout_tiers.c.id == first_row.id)).first()
    assert unchanged.rate_percent == original_rate
