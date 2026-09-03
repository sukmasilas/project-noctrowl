"""Documents screen route tests: ingestion status cards, Sync Now cooldown,
invoice editing.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update

from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.schema import invoices as invoices_table, source_documents
from ingestion.sync import EBAY_SALES_SUBFOLDER, UPLOADS_ROOT_NAME
from ledger.schema import ebay_accounts, wallet_groups
from tests.ingestion.test_sync import FakeDriveClient
from tests.webapp.conftest import make_source_document
from webapp import create_app
from webapp.sync_cooldown import record_sync_run

PERIOD = _dt.date(2026, 7, 1)

SAMPLES = Path(__file__).resolve().parents[2] / "sample-documents"
EBAY_CSV_BYTES = (SAMPLES / "eBay account 1_ricky-game" / "Transaction_report_20260701_20260731.csv").read_bytes()


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


def _ebay_sales_csv_ingested_at(conn, *, ebay_account_id: int, period_month: _dt.date):
    row = conn.execute(
        select(source_documents.c.ingested_at).where(
            source_documents.c.document_type == "ebay_sales_csv",
            source_documents.c.ebay_account_id == ebay_account_id,
            source_documents.c.period_month == period_month,
        )
    ).first()
    return row.ingested_at if row is not None else None


def _seed_remaining_july_kurs_pajak_rates(conn):
    """wtopology already seeds one Kurs Pajak rate (effective 2026-07-06).
    The real July 2026 eBay CSV sample (EBAY_CSV_BYTES) has order dates
    spanning the whole month, including before the 6th — same full weekly
    rate set tests/ingestion/conftest.py's ``iprototype`` fixture seeds for
    this exact same real sample file, needed here too so a real order date
    early in July doesn't hit NoKursPajakRateError mid-sync (which would
    roll back the whole transaction, including the ingested_at these tests
    check for — masking the very thing being tested).
    """
    from decimal import Decimal as _Decimal

    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 6, 29), rate_idr=_Decimal("16250.0000"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 7, 13), rate_idr=_Decimal("16280.0000"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 7, 20), rate_idr=_Decimal("16310.0000"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 7, 27), rate_idr=_Decimal("16350.0000"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 8, 3), rate_idr=_Decimal("16400.0000"))


def _app_with_fake_drive(wengine, login_env, drive_client):
    """Same construction as the shared ``app`` fixture (tests/webapp/
    conftest.py), but with an explicit Drive client instead of always None —
    needed here because Sync Now's folder-name resolution can only be
    exercised end-to-end with a real (faked) Drive client behind it.
    """
    flask_app = create_app(engine=wengine, drive_client=drive_client)
    flask_app.config.update(TESTING=True)
    return flask_app


# Regression coverage for the 2026-09-02 fix (QA finding): Sync Now used to
# derive the Drive folder name from ebay_accounts.name/wallet_groups.name
# (generic display labels) instead of the real Drive folder name, so it
# silently looked in the wrong folder. webapp/scoping.py's
# EbayAccountOption now carries an explicit drive_folder_name field (falling
# back to the old derivation only when unset) — these two tests exercise
# THAT resolution through the real Sync Now route with a fake Drive client,
# which neither test above reaches (they only cover the cooldown/
# not-configured short-circuits, before folder resolution ever runs).


def test_sync_now_uses_explicit_drive_folder_name_when_set(wengine, login_env, wtopology, monkeypatch):
    conn, topo = wtopology
    _seed_remaining_july_kurs_pajak_rates(conn)

    # Deliberately DIFFERENT from what the old derivation would produce
    # (f"eBay Account - {name}" / the wallet group's plain `name`) — if the
    # route fell back to deriving instead of using this explicit field, it
    # would look in a folder that doesn't exist in the fake Drive tree below
    # and the eBay CSV would never be found/ingested.
    custom_ebay_folder = "Custom eBay Folder XYZ"
    custom_wallet_group_folder = "Custom Wallet Group Folder XYZ"
    conn.execute(update(ebay_accounts).where(ebay_accounts.c.id == topo["ebay_account_id"]).values(drive_folder_name=custom_ebay_folder))
    conn.execute(update(wallet_groups).where(wallet_groups.c.id == topo["wallet_group_id"]).values(drive_folder_name=custom_wallet_group_folder))
    conn.commit()

    client = FakeDriveClient()
    ebay_folder = client.add_path(client.root_id, UPLOADS_ROOT_NAME, custom_ebay_folder, "2026", "2026-07", EBAY_SALES_SUBFOLDER)
    client.add_file(ebay_folder, "Transaction_report_20260701_20260731.csv", EBAY_CSV_BYTES, "text/csv")
    # The route resolves the Drive root from GOOGLE_DRIVE_ROOT_FOLDER_ID — it
    # must point at THIS fake client's own root id, not an arbitrary string,
    # or resolve_folder_path() finds nothing under it regardless of which
    # folder-name logic (explicit vs. derived) is under test here.
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", client.root_id)

    flask_app = _app_with_fake_drive(wengine, login_env, client)
    username, password = login_env
    test_client = flask_app.test_client()
    login_resp = test_client.post("/login", data={"username": username, "password": password})
    assert login_resp.status_code in (302, 303)

    resp = test_client.post(
        "/documents/sync",
        data={"account_id": str(topo["ebay_account_id"]), "period": PERIOD.isoformat()[:7]},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with wengine.connect() as verify_conn:
        ingested_at = _ebay_sales_csv_ingested_at(verify_conn, ebay_account_id=topo["ebay_account_id"], period_month=PERIOD)
    assert ingested_at is not None, (
        "Sync Now did not find the eBay CSV under the explicit drive_folder_name — "
        "it must have derived the folder name instead of using the explicit field."
    )


def test_sync_now_falls_back_to_derived_folder_name_when_drive_folder_name_is_null(wengine, login_env, wtopology, monkeypatch):
    conn, topo = wtopology
    _seed_remaining_july_kurs_pajak_rates(conn)
    conn.commit()

    # wtopology's seeded rows already have drive_folder_name = NULL (no
    # override set) — confirm that explicitly, since this test's whole point
    # is exercising the NULL-fallback path.
    row = conn.execute(select(ebay_accounts.c.name, ebay_accounts.c.drive_folder_name).where(ebay_accounts.c.id == topo["ebay_account_id"])).first()
    assert row.drive_folder_name is None
    wg_row = conn.execute(select(wallet_groups.c.name, wallet_groups.c.drive_folder_name).where(wallet_groups.c.id == topo["wallet_group_id"])).first()
    assert wg_row.drive_folder_name is None

    # The OLD derivation, exactly as webapp/scoping.py's
    # *_drive_folder_name_resolved fallback still produces today. (The
    # wallet-group side of the fallback — wg_row.name itself, unchanged
    # from before this fix — isn't separately exercised here since only the
    # eBay CSV folder is needed to prove folder-name resolution; asserting
    # wg_row.drive_folder_name is None above is what matters for this test.)
    derived_ebay_folder = f"eBay Account - {row.name}"

    client = FakeDriveClient()
    ebay_folder = client.add_path(client.root_id, UPLOADS_ROOT_NAME, derived_ebay_folder, "2026", "2026-07", EBAY_SALES_SUBFOLDER)
    client.add_file(ebay_folder, "Transaction_report_20260701_20260731.csv", EBAY_CSV_BYTES, "text/csv")
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", client.root_id)

    flask_app = _app_with_fake_drive(wengine, login_env, client)
    username, password = login_env
    test_client = flask_app.test_client()
    login_resp = test_client.post("/login", data={"username": username, "password": password})
    assert login_resp.status_code in (302, 303)

    resp = test_client.post(
        "/documents/sync",
        data={"account_id": str(topo["ebay_account_id"]), "period": PERIOD.isoformat()[:7]},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with wengine.connect() as verify_conn:
        ingested_at = _ebay_sales_csv_ingested_at(verify_conn, ebay_account_id=topo["ebay_account_id"], period_month=PERIOD)
    assert ingested_at is not None, (
        "Sync Now did not find the eBay CSV under the derived (fallback) folder name — "
        "the NULL drive_folder_name fallback no longer matches the old behavior."
    )
