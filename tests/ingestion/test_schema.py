"""Structural (DB-level) idempotency invariants for milestone 3's schema
additions — same spirit as tests/test_schema.py's milestone-2 coverage:
prove these are real unique-index-backed constraints that hold even when
something bypasses the application layer entirely (raw SQL), not just an
app-layer SELECT-before-insert convention that a second, overlapping
caller could race past.

Specifically covers the QA-found gap (2026-09): consignment_sales.
consignor_item_ref and invoices.drive_file_id previously had ONLY an
app-layer guard (safe against sequential re-runs, not against two
overlapping sync calls) — both now have a real unique index.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from ingestion.schema import invoices
from ledger.schema import consignment_sales


def test_consignment_sales_consignor_item_ref_has_a_real_db_level_unique_index(iprototype):
    """Bypasses ingestion/ebay_csv.py entirely (raw SQL) — the exact
    scenario QA used to originally find this gap: two overlapping callers
    racing past the app-layer SELECT-before-insert check.
    """
    conn, topo = iprototype

    conn.execute(
        consignment_sales.insert().values(
            item_price_usd=Decimal("100.00"),
            payout_model="tier",
            tier_rate_percent=Decimal("82.00"),
            payout_amount_idr=Decimal("1348000"),
            consignor_item_ref="CONSIGN-DUPLICATE-TEST:order-1",
        )
    )

    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(
                consignment_sales.insert().values(
                    item_price_usd=Decimal("100.00"),
                    payout_model="tier",
                    tier_rate_percent=Decimal("82.00"),
                    payout_amount_idr=Decimal("1348000"),
                    consignor_item_ref="CONSIGN-DUPLICATE-TEST:order-1",  # identical ref, second row
                )
            )


def test_consignment_sales_different_refs_insert_fine(iprototype):
    """Confirms the fix isn't over-broad — legitimate, genuinely distinct
    consignment sales still insert without collision.
    """
    conn, topo = iprototype
    conn.execute(
        consignment_sales.insert().values(
            item_price_usd=Decimal("100.00"),
            payout_model="tier",
            tier_rate_percent=Decimal("82.00"),
            payout_amount_idr=Decimal("1348000"),
            consignor_item_ref="CONSIGN-A:order-1",
        )
    )
    conn.execute(
        consignment_sales.insert().values(
            item_price_usd=Decimal("50.00"),
            payout_model="tier",
            tier_rate_percent=Decimal("78.00"),
            payout_amount_idr=Decimal("634050"),
            consignor_item_ref="CONSIGN-B:order-2",
        )
    )  # no exception


def test_invoices_drive_file_id_has_a_real_db_level_unique_index(iprototype):
    conn, topo = iprototype
    conn.execute(
        invoices.insert().values(
            drive_file_id="drive-file-dup-test",
            drive_file_name="a.pdf",
            period_month=_dt.date(2026, 7, 1),
            status="parsed",
        )
    )
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(
                invoices.insert().values(
                    drive_file_id="drive-file-dup-test",  # identical, second row
                    drive_file_name="a-again.pdf",
                    period_month=_dt.date(2026, 7, 1),
                    status="parsed",
                )
            )


def test_invoices_multiple_null_drive_file_ids_allowed(iprototype):
    """The unique index is partial (WHERE drive_file_id IS NOT NULL) —
    multiple NULLs (an invoice inserted without a Drive reference, however
    that might happen) must never collide with each other.
    """
    conn, topo = iprototype
    for _ in range(2):
        conn.execute(
            invoices.insert().values(
                drive_file_id=None,
                drive_file_name="no-drive-ref.pdf",
                period_month=_dt.date(2026, 7, 1),
                status="needs_confirmation",
            )
        )  # no exception on either insert
