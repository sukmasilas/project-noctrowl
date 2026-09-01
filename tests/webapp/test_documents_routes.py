"""Documents screen route tests: ingestion status cards, Sync Now cooldown,
invoice editing.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from sqlalchemy import select

from ingestion.schema import invoices as invoices_table
from tests.webapp.conftest import make_source_document
from webapp.sync_cooldown import record_sync_run

PERIOD = _dt.date(2026, 7, 1)


def test_documents_page_renders_not_yet_uploaded_state(logged_in_client, wtopology):
    # Use the CURRENT month, not the fixed 2026-07 PERIOD below — the H+7
    # deadline for July 2026 has already passed relative to the real
    # system clock, which would make this period show "Missing" instead.
    current_month = _dt.date.today().replace(day=1)
    resp = logged_in_client.get(f"/documents/?period={current_month.isoformat()[:7]}")
    assert resp.status_code == 200
    assert b"Not yet uploaded" in resp.data


def test_documents_page_shows_uploaded_card(logged_in_client, wtopology):
    conn, topo = wtopology
    make_source_document(conn, document_type="ebay_sales_csv", period_month=PERIOD, ebay_account_id=topo["ebay_account_id"], ingested=True)
    conn.commit()
    resp = logged_in_client.get(f"/documents/?period={PERIOD.isoformat()[:7]}")
    assert resp.status_code == 200
    assert b"Uploaded" in resp.data


def test_documents_page_shows_missing_when_deadline_passed(logged_in_client, wtopology):
    old_period = _dt.date(2020, 1, 1)  # deadline long since passed
    resp = logged_in_client.get(f"/documents/?period={old_period.isoformat()[:7]}")
    assert resp.status_code == 200
    assert b"Missing" in resp.data


def test_sync_now_without_drive_configured_flashes_error_not_crash(logged_in_client, wtopology, monkeypatch):
    monkeypatch.delenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", raising=False)
    conn, topo = wtopology
    resp = logged_in_client.post(
        "/documents/sync",
        data={"account_id": str(topo["ebay_account_id"]), "period": PERIOD.isoformat()},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"not configured" in resp.data or b"GOOGLE_DRIVE_ROOT_FOLDER_ID" in resp.data


def test_sync_now_respects_cooldown(logged_in_client, wtopology, monkeypatch):
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "fake-root")
    conn, topo = wtopology
    record_sync_run(conn, triggered_by="tester", ebay_account_id=topo["ebay_account_id"], period_month=PERIOD, result_summary="test")
    conn.commit()

    resp = logged_in_client.post(
        "/documents/sync",
        data={"account_id": str(topo["ebay_account_id"]), "period": PERIOD.isoformat()},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"cooldown" in resp.data


def test_update_invoice_saves_fields_and_marks_confirmed(logged_in_client, wtopology):
    conn, topo = wtopology
    result = conn.execute(
        invoices_table.insert().values(
            drive_file_id="f1",
            drive_file_name="receipt.pdf",
            period_month=PERIOD,
            status="needs_confirmation",
        )
    )
    invoice_id = result.inserted_primary_key[0]
    conn.commit()

    resp = logged_in_client.post(
        f"/documents/invoices/{invoice_id}",
        data={
            "extracted_date": "2026-07-10",
            "vendor_description": "Toko Sederhana",
            "amount_idr": "150000",
            "purpose": "cogs_purchase",
            "period": PERIOD.isoformat(),
            "account_id": str(topo["ebay_account_id"]),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(invoices_table).where(invoices_table.c.id == invoice_id)).first()
    assert row.vendor_description == "Toko Sederhana"
    assert row.amount_idr == Decimal("150000")
    assert row.purpose == "cogs_purchase"
    assert row.status == "parsed"
    assert row.confirmed_at is not None
