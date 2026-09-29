"""Tests for the new Inventory Deposits screen (webapp/inventory_deposits_bp.py)
— the small, write-capable screen where a human confirms goods have arrived
and converts an outstanding inventory deposit to COGS. See that module's
docstring for why this is deliberately NOT a review-queue category (the
conversion event has no cash movement/bank line of its own).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from sqlalchemy import select

from ledger import posting
from ledger.entities import get_account_id
from ledger.schema import journal_entries, journal_lines

DEPOSIT_REF = "DP Box op — Fariz — 2026-08-20"


def test_index_shows_empty_state_with_no_outstanding_deposits(client, wtopology):
    resp = client.get("/inventory-deposits/")
    assert resp.status_code == 200
    assert b"No outstanding inventory deposits" in resp.data


def test_index_lists_an_outstanding_deposit(client, wtopology):
    conn, topo = wtopology
    posting.post_inventory_deposit(
        conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    conn.commit()

    resp = client.get("/inventory-deposits/")
    assert resp.status_code == 200
    assert DEPOSIT_REF.encode() in resp.data
    assert b"9,840,000" in resp.data or b"9840000" in resp.data


def test_index_excludes_a_fully_converted_deposit(client, wtopology):
    conn, topo = wtopology
    posting.post_inventory_deposit(
        conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    posting.post_inventory_deposit_received(
        conn, entry_date=_dt.date(2026, 7, 20), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    conn.commit()

    resp = client.get("/inventory-deposits/")
    assert resp.status_code == 200
    assert b"No outstanding inventory deposits" in resp.data
    assert DEPOSIT_REF.encode() not in resp.data


def test_convert_posts_a_real_journal_entry_debiting_cogs(client, wtopology):
    conn, topo = wtopology
    posting.post_inventory_deposit(
        conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    conn.commit()

    resp = client.post(
        "/inventory-deposits/convert",
        data={"deposit_ref": DEPOSIT_REF, "amount_idr": "9840000", "entry_date": "2026-07-20"},
    )
    assert resp.status_code in (301, 302)

    cogs_id = get_account_id(conn, "INVENTORY_DEPOSITS")  # sanity: account exists
    assert cogs_id is not None

    entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inventory_deposit_received")
    ).all()
    assert len(entries) == 1

    rows = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(
            journal_lines.c.journal_entry_id == entries[0].id,
            journal_lines.c.consignor_item_ref == DEPOSIT_REF,
        )
    ).all()
    assert any(r.debit_amount_idr == Decimal("9840000") for r in rows)


def test_convert_refuses_amount_exceeding_outstanding_balance(client, wtopology):
    conn, topo = wtopology
    posting.post_inventory_deposit(
        conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    conn.commit()

    resp = client.post(
        "/inventory-deposits/convert",
        data={"deposit_ref": DEPOSIT_REF, "amount_idr": "99999999", "entry_date": "2026-07-20"},
    )
    assert resp.status_code in (301, 302)

    entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inventory_deposit_received")
    ).all()
    assert entries == []  # refused, nothing posted


def test_convert_requires_a_deposit_reference(client, wtopology):
    resp = client.post(
        "/inventory-deposits/convert",
        data={"deposit_ref": "", "amount_idr": "9840000", "entry_date": "2026-07-20"},
    )
    assert resp.status_code in (301, 302)


def test_convert_rejects_non_positive_amount(client, wtopology):
    conn, topo = wtopology
    posting.post_inventory_deposit(
        conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("9840000"), deposit_ref=DEPOSIT_REF
    )
    conn.commit()

    resp = client.post(
        "/inventory-deposits/convert",
        data={"deposit_ref": DEPOSIT_REF, "amount_idr": "0", "entry_date": "2026-07-20"},
    )
    assert resp.status_code in (301, 302)

    entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "inventory_deposit_received")
    ).all()
    assert entries == []
