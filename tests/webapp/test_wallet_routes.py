"""Tests for the Wallet screen (webapp/wallet_bp.py) — the transaction
register per wallet (eBay Wallet / Payoneer Wallet / BCA Bridging / BCA
Main) and the untraceable-invoice flag. See CLAUDE.md's Wallet-screen brief.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from ingestion.ebay_csv import parse_ebay_csv_rows, process_transaction_report
from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.schema import ebay_csv_transactions, invoices as invoices_table
from tests.webapp.conftest import make_review_queue_row, make_source_document
from webapp.documents_bp import list_untraceable_invoices
from webapp.wallet_bp import list_wallet_options, wallet_register

REAL_SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "sample-documents"
    / "eBay account 1_ricky-game"
    / "Transaction_report_20260701_20260731.csv"
)


def test_list_wallet_options_returns_all_four_wallet_types_for_prototype_topology(wtopology):
    conn, topo = wtopology
    options = list_wallet_options(conn)
    codes = sorted(o.account_type_code for o in options)
    # Prototype topology: 1 eBay Wallet + 1 Payoneer Wallet + 1 BCA Bridging
    # + 1 BCA Main (consolidated singleton) = 4 wallet instances.
    assert codes == ["BCA_BRIDGING", "BCA_MAIN", "EBAY_WALLET", "PAYONEER_WALLET"]
    ebay_opt = next(o for o in options if o.account_type_code == "EBAY_WALLET")
    assert ebay_opt.account_id == topo["ebay_wallet_id"]
    assert "eBay Account 1" in ebay_opt.label


def test_ebay_wallet_register_reflects_staged_rows_including_unposted(wtopology):
    conn, topo = wtopology
    # wtopology only seeds one Kurs Pajak rate (effective 2026-07-06); the
    # real sample spans all of July, so cover the whole month the same way
    # tests/ingestion/conftest.py's iprototype fixture does.
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 6, 29), rate_idr=Decimal("16250.0000"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 7, 13), rate_idr=Decimal("16280.0000"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 7, 20), rate_idr=Decimal("16310.0000"))
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 7, 27), rate_idr=Decimal("16350.0000"))
    text = REAL_SAMPLE.read_text(encoding="utf-8-sig")
    _, rows = parse_ebay_csv_rows(text)
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=rows)
    conn.commit()

    options = list_wallet_options(conn)
    ebay_opt = next(o for o in options if o.account_type_code == "EBAY_WALLET")

    txns = wallet_register(conn, ebay_opt, period_month=_dt.date(2026, 7, 1))
    # Every one of the 202 real raw CSV rows shows up, not just the posted
    # ones — this is the whole point of the raw-row staging table.
    assert len(txns) == 202

    posted = [t for t in txns if t.status == "posted"]
    not_posted = [t for t in txns if t.status == "not_posted"]
    assert len(posted) > 0
    assert len(not_posted) > 0  # Hold rows, at minimum
    assert all(t.journal_entry_id is not None for t in posted)


def test_ebay_wallet_register_shows_unconfirmed_consignment_sale_as_awaiting_confirmation(wtopology):
    conn, topo = wtopology
    header, _ = parse_ebay_csv_rows(REAL_SAMPLE.read_text(encoding="utf-8-sig"))
    row = {col: "--" for col in header}
    row.update(
        {
            "Transaction creation date": "Jul 15, 2026",
            "Type": "Order",
            "Order number": "99-99999-99999",
            "Transaction ID": "TXN-CONSIGN-1",
            "Item title": "Seiko SNJ025 Prospex Diver",
            "Custom label": "CONSIGN-SELLER42-001",
            "Item subtotal": "200",
            "Shipping and handling": "20",
            "Gross transaction amount": "220",
            "Net amount": "180",
            "Final Value Fee - fixed": "-0.44",
            "Final Value Fee - variable": "-20",
            "Transaction currency": "USD",
        }
    )
    src_id = make_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=_dt.date(2026, 7, 1),
        ebay_account_id=topo["ebay_account_id"],
    )
    process_transaction_report(conn, ebay_account_id=topo["ebay_account_id"], source_document_id=src_id, rows=[row])
    conn.commit()

    options = list_wallet_options(conn)
    ebay_opt = next(o for o in options if o.account_type_code == "EBAY_WALLET")
    txns = wallet_register(conn, ebay_opt, period_month=_dt.date(2026, 7, 1))
    assert len(txns) == 1
    assert txns[0].status == "awaiting_confirmation"
    assert txns[0].journal_entry_id is None


def test_bca_main_register_reflects_review_queue_needs_review_row(wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=_dt.date(2026, 7, 1))
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 10),
        amount_idr=Decimal("-500000"),
        source_type="bank_statement",
        raw_description="UNKNOWN DEBIT LINE",
        match_status="needs_review",
    )
    conn.commit()

    options = list_wallet_options(conn)
    bca_main_opt = next(o for o in options if o.account_type_code == "BCA_MAIN")
    txns = wallet_register(conn, bca_main_opt, period_month=_dt.date(2026, 7, 1))
    assert len(txns) == 1
    assert txns[0].status == "needs_review"
    assert txns[0].amount_idr == Decimal("-500000")
    assert txns[0].source == "review_queue"


def test_bca_bridging_register_is_scoped_to_its_own_wallet_group_not_bca_main(wtopology):
    conn, topo = wtopology
    src_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=_dt.date(2026, 7, 1),
        wallet_group_id=topo["wallet_group_id"],
    )
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 10),
        amount_idr=Decimal("1000000"),
        source_type="bank_statement",
        raw_description="BRIDGING INFLOW",
        wallet_group_id=topo["wallet_group_id"],
        match_status="matched",
    )
    conn.commit()

    options = list_wallet_options(conn)
    bridging_opt = next(o for o in options if o.account_type_code == "BCA_BRIDGING")
    bca_main_opt = next(o for o in options if o.account_type_code == "BCA_MAIN")

    bridging_txns = wallet_register(conn, bridging_opt, period_month=_dt.date(2026, 7, 1))
    main_txns = wallet_register(conn, bca_main_opt, period_month=_dt.date(2026, 7, 1))

    assert len(bridging_txns) == 1
    assert bridging_txns[0].status == "matched_pending_post"
    assert len(main_txns) == 0  # never leaks into the consolidated BCA Main register


def test_untraceable_invoice_is_flagged_when_no_review_queue_row_links_to_it(wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    inv_id = conn.execute(
        invoices_table.insert().values(
            drive_file_name="invoice-untraceable.pdf",
            period_month=period,
            extracted_date=_dt.date(2026, 7, 5),
            vendor_description="Mystery Vendor",
            amount_idr=Decimal("250000"),
            purpose="cogs_purchase",
            status="parsed",
        )
    ).inserted_primary_key[0]
    conn.commit()

    untraceable = list_untraceable_invoices(conn, period_month=period)
    assert len(untraceable) == 1
    assert untraceable[0].id == inv_id


def test_traceable_invoice_is_not_flagged_once_a_review_queue_row_links_to_it(wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    inv_id = conn.execute(
        invoices_table.insert().values(
            drive_file_name="invoice-traceable.pdf",
            period_month=period,
            extracted_date=_dt.date(2026, 7, 5),
            vendor_description="Real Vendor",
            amount_idr=Decimal("250000"),
            purpose="cogs_purchase",
            status="parsed",
        )
    ).inserted_primary_key[0]
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=period)
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 6),
        amount_idr=Decimal("-250000"),
        source_type="bank_statement",
        raw_description="COGS PAYMENT",
        category="cogs_purchase",
        linked_invoice_id=inv_id,
    )
    conn.commit()

    untraceable = list_untraceable_invoices(conn, period_month=period)
    assert untraceable == []


def test_wallet_route_renders_and_flags_untraceable_invoice(logged_in_client, wtopology):
    conn, topo = wtopology
    period = _dt.date(2026, 7, 1)
    conn.execute(
        invoices_table.insert().values(
            drive_file_name="invoice-untraceable.pdf",
            period_month=period,
            extracted_date=_dt.date(2026, 7, 5),
            vendor_description="Mystery Vendor",
            amount_idr=Decimal("250000"),
            purpose="cogs_purchase",
            status="parsed",
        )
    )
    conn.commit()

    resp = logged_in_client.get(f"/wallet/?period=2026-07&account_id={topo['ebay_wallet_id']}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Mystery Vendor" in body
    assert "no matching transaction" in body.lower()


def test_wallet_route_requires_login(client):
    resp = client.get("/wallet/", follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_wallet_route_handles_no_accounts_set_up_yet_without_crashing(app, login_env, wconn):
    """CLAUDE.md's Definition of done: handle the 'no data yet' case without
    crashing. ``wconn`` seeds catalogs only — no eBay account/wallet-group
    topology exists yet.
    """
    client = app.test_client()
    username, password = login_env
    client.post("/login", data={"username": username, "password": password})
    resp = client.get("/wallet/")
    assert resp.status_code == 200
    assert "No wallet accounts are set up yet" in resp.get_data(as_text=True)
