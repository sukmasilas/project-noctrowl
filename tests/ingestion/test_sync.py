"""Tests for ingestion.sync — the QA-required end-to-end orchestration
(list Drive folder -> download -> parse -> write source_documents/
review_queue -> auto-match -> post), per CLAUDE.md's milestone-3-vs-5
boundary clarification.

Everything below the DriveClient boundary uses the SAME real sample
documents already validated in test_ebay_csv.py/test_payoneer.py/
test_bank_statement.py/test_invoices.py — this file is specifically about
proving the ORCHESTRATION wiring (folder resolution, download, dispatch to
the right parser, source_documents bookkeeping, idempotency across two
sync runs), not re-proving parser correctness. Drive itself is faked (a
plain in-memory object satisfying DriveClient's list_files/download_file
shape) — see FakeDriveClient below — except for one live, read-only smoke
test against the real credential/root folder, which is skipped if that
isn't configured in this environment.
"""
from __future__ import annotations

import datetime as _dt
import os
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from ingestion.drive_client import FOLDER_MIME_TYPE, DriveFile
from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.schema import invoices, review_queue, source_documents
from ingestion.sync import (
    BANK_STATEMENTS_SUBFOLDER,
    EBAY_SALES_SUBFOLDER,
    INVOICES_SUBFOLDER,
    PAYONEER_SUBFOLDER,
    UPLOADS_ROOT_NAME,
    resolve_folder_path,
    run_sync_for_period,
    sync_bank_statement,
    sync_ebay_sales_csv,
    sync_invoices,
    sync_payoneer,
)
from ledger.schema import journal_entries

SAMPLES = Path(__file__).resolve().parents[2] / "sample-documents"
EBAY_CSV_BYTES = (SAMPLES / "ebay-sales-export" / "Transaction_report_20260701_20260731.csv").read_bytes()
PAYONEER_CSV_BYTES = (SAMPLES / "payoneer" / "Payoneer_Transactions_04-2026.csv").read_bytes()
PAYONEER_CONFIRMATION_BYTES = (
    SAMPLES / "payoneer" / "Payoneer_Confirmation_of_Transfer_4366185623014087.pdf"
).read_bytes()
BANK_STATEMENT_BYTES = (SAMPLES / "bank-statements" / "BCA Bank_1790345891_APR_2026.pdf").read_bytes()
TOKOPEDIA_INVOICE_BYTES = (SAMPLES / "invoices-proof-of-purchase" / "Invoice Sample _ Tokopedia.pdf").read_bytes()


class FakeDriveClient:
    """A plain in-memory fake satisfying DriveClient's list_files/
    download_file shape — no network, no google-api mocking needed."""

    def __init__(self):
        self._next_id = 0
        self._entries: dict[str, dict] = {}
        self.root_id = self._add(parent=None, name="root", mime_type=FOLDER_MIME_TYPE, content=None)

    def _add(self, *, parent, name, mime_type, content) -> str:
        self._next_id += 1
        fid = f"fake-{self._next_id}"
        self._entries[fid] = {"parent": parent, "name": name, "mime_type": mime_type, "content": content}
        return fid

    def add_folder(self, parent_id: str, name: str) -> str:
        return self._add(parent=parent_id, name=name, mime_type=FOLDER_MIME_TYPE, content=None)

    def add_path(self, parent_id: str, *names: str) -> str:
        current = parent_id
        for name in names:
            existing = next(
                (fid for fid, e in self._entries.items() if e["parent"] == current and e["name"] == name),
                None,
            )
            current = existing or self.add_folder(current, name)
        return current

    def add_file(self, parent_id: str, name: str, content: bytes, mime_type: str = "text/csv") -> str:
        return self._add(parent=parent_id, name=name, mime_type=mime_type, content=content)

    def list_files(self, folder_id: str, mime_types: list[str] | None = None) -> list[DriveFile]:
        results = []
        for fid, e in self._entries.items():
            if e["parent"] != folder_id:
                continue
            if mime_types is not None and e["mime_type"] not in mime_types:
                continue
            results.append(DriveFile(id=fid, name=e["name"], mime_type=e["mime_type"], modified_time=None))
        return results

    def download_file(self, file_id: str) -> bytes:
        return self._entries[file_id]["content"]


@pytest.fixture()
def drive(iprototype):
    """A fake Drive tree matching CLAUDE.md's Architecture folder structure
    exactly, pre-populated with the real sample documents. Cross-month note:
    the real samples span different real-world months (eBay CSV = July
    2026, Payoneer/BCA statement = April 2026 — see
    sample-documents/README.md) — placed under one shared test period here
    since this file is testing PIPELINE WIRING, not fixture date-realism
    (already covered per-parser in the other test files).
    """
    conn, topo = iprototype
    seed_kurs_pajak_rate(conn, effective_date=_dt.date(2026, 4, 1), rate_idr=Decimal("16400"))

    client = FakeDriveClient()
    period_month = _dt.date(2026, 7, 1)
    year, ym = "2026", "2026-07"

    ebay_sales_folder = client.add_path(
        client.root_id, UPLOADS_ROOT_NAME, "eBay Account 1", year, ym, EBAY_SALES_SUBFOLDER
    )
    client.add_file(ebay_sales_folder, "Transaction_report_20260701_20260731.csv", EBAY_CSV_BYTES, "text/csv")

    payoneer_folder = client.add_path(client.root_id, UPLOADS_ROOT_NAME, "Wallet Group 1", year, ym, PAYONEER_SUBFOLDER)
    client.add_file(payoneer_folder, "Payoneer_Transactions_04-2026.csv", PAYONEER_CSV_BYTES, "text/csv")
    client.add_file(
        payoneer_folder,
        "Payoneer_Confirmation_of_Transfer_4366185623014087.pdf",
        PAYONEER_CONFIRMATION_BYTES,
        "application/pdf",
    )

    master_bank_folder = client.add_path(
        client.root_id, UPLOADS_ROOT_NAME, "Master Account", year, ym, BANK_STATEMENTS_SUBFOLDER
    )
    client.add_file(master_bank_folder, "BCA Bank_1790345891_APR_2026.pdf", BANK_STATEMENT_BYTES, "application/pdf")

    invoices_folder = client.add_path(client.root_id, UPLOADS_ROOT_NAME, "Master Account", year, ym, INVOICES_SUBFOLDER)
    client.add_file(invoices_folder, "Invoice Sample _ Tokopedia.pdf", TOKOPEDIA_INVOICE_BYTES, "application/pdf")

    return conn, topo, client, period_month


# ---------------------------------------------------------------------------
# resolve_folder_path
# ---------------------------------------------------------------------------


def test_resolve_folder_path_walks_by_name(drive):
    conn, topo, client, period_month = drive
    resolved = resolve_folder_path(client, client.root_id, UPLOADS_ROOT_NAME, "eBay Account 1", "2026", "2026-07")
    assert resolved is not None
    files = client.list_files(resolved, mime_types=[FOLDER_MIME_TYPE])
    assert any(f.name == EBAY_SALES_SUBFOLDER for f in files)


def test_resolve_folder_path_returns_none_never_creates_when_missing(drive):
    conn, topo, client, period_month = drive
    resolved = resolve_folder_path(client, client.root_id, UPLOADS_ROOT_NAME, "eBay Account 99 - Nonexistent")
    assert resolved is None
    # Confirm nothing was silently created — the fake's own bookkeeping is
    # the simplest proof: no new folder named "eBay Account 99..." exists.
    assert not any(e["name"] == "eBay Account 99 - Nonexistent" for e in client._entries.values())


# ---------------------------------------------------------------------------
# Per-document-type sync functions
# ---------------------------------------------------------------------------


def test_sync_ebay_sales_csv_missing_folder_marks_not_yet_uploaded(iprototype):
    conn, topo = iprototype
    client = FakeDriveClient()
    step = sync_ebay_sales_csv(
        conn,
        client,
        root_folder_id=client.root_id,
        ebay_account_id=topo["ebay_account_id"],
        ebay_account_folder_name="eBay Account 1",
        period_month=_dt.date(2026, 7, 1),
    )
    assert step.found_file is False
    doc = conn.execute(
        select(source_documents.c.ingested_at, source_documents.c.document_type).where(
            source_documents.c.document_type == "ebay_sales_csv"
        )
    ).one()
    assert doc.ingested_at is None  # "not yet uploaded", not an error


def test_sync_ebay_sales_csv_real_sample_downloads_parses_posts_and_records_source_document(drive):
    conn, topo, client, period_month = drive
    step = sync_ebay_sales_csv(
        conn,
        client,
        root_folder_id=client.root_id,
        ebay_account_id=topo["ebay_account_id"],
        ebay_account_folder_name="eBay Account 1",
        period_month=period_month,
    )
    assert step.found_file is True
    assert step.row_count == 202
    assert step.warnings == []

    doc = conn.execute(
        select(
            source_documents.c.ingested_at,
            source_documents.c.row_count,
            source_documents.c.parse_warning,
            source_documents.c.drive_file_name,
        ).where(source_documents.c.document_type == "ebay_sales_csv")
    ).one()
    assert doc.ingested_at is not None
    assert doc.row_count == 202
    assert doc.parse_warning is None
    assert doc.drive_file_name == "Transaction_report_20260701_20260731.csv"

    # 91 distinct sales posted (100 Order rows, 4 multi-item groups merged —
    # see test_ebay_csv.py) — confirms the real data actually flowed through.
    sale_entries = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.source_type == "ebay_sale")
    ).all()
    assert len(sale_entries) == 91


def test_sync_payoneer_real_sample_downloads_both_csv_and_confirmation(drive):
    conn, topo, client, period_month = drive
    step = sync_payoneer(
        conn,
        client,
        root_folder_id=client.root_id,
        wallet_group_id=topo["wallet_group_id"],
        wallet_group_folder_name="Wallet Group 1",
        period_month=period_month,
    )
    assert step.found_file is True
    assert step.row_count == 7  # the real CSV's 7 completed rows

    doc = conn.execute(
        select(source_documents.c.ingested_at, source_documents.c.row_count).where(
            source_documents.c.document_type == "payoneer_csv"
        )
    ).one()
    assert doc.ingested_at is not None
    assert doc.row_count == 7

    # The real confirmation PDF's own figures (Transfer ID 4366185623014087,
    # 5000 USD) don't match any of this real CSV's 3 withdrawal rows'
    # Reference IDs/amounts (different real-world months — see
    # sample-documents/README.md) — expected to warn, not silently drop.
    assert len(step.warnings) == 3
    assert all("no matching withdrawal confirmation" in w for w in step.warnings)
    # The 4 "Payment from eBay" rows have no seeded ebay_expected_payouts to
    # match against in this test, so they stage generically -> needs_review.
    staged = conn.execute(select(review_queue.c.id).where(review_queue.c.source_type == "payoneer_csv")).all()
    assert len(staged) >= 4


def test_sync_bank_statement_master_real_sample_reconciles_no_warning(drive):
    conn, topo, client, period_month = drive
    step = sync_bank_statement(
        conn, client, root_folder_id=client.root_id, period_month=period_month, master_folder_name="Master Account"
    )
    assert step.found_file is True
    assert step.row_count == 79
    assert step.warnings == []  # reconciles cleanly against the statement's own printed totals

    doc = conn.execute(
        select(source_documents.c.row_count, source_documents.c.parse_warning).where(
            source_documents.c.document_type == "bank_statement_master"
        )
    ).one()
    assert doc.row_count == 79
    assert doc.parse_warning is None

    staged = conn.execute(select(review_queue.c.id).where(review_queue.c.source_type == "bank_statement")).all()
    assert len(staged) == 79


def test_sync_bank_statement_missing_folder_records_not_yet_uploaded(iprototype):
    conn, topo = iprototype
    client = FakeDriveClient()
    step = sync_bank_statement(
        conn, client, root_folder_id=client.root_id, period_month=_dt.date(2026, 7, 1), master_folder_name="Master Account"
    )
    assert step.found_file is False
    doc = conn.execute(
        select(source_documents.c.ingested_at).where(source_documents.c.document_type == "bank_statement_master")
    ).one()
    assert doc.ingested_at is None


def test_sync_invoices_real_tokopedia_sample_creates_needs_confirmation_record(drive):
    conn, topo, client, period_month = drive
    step = sync_invoices(
        conn, client, root_folder_id=client.root_id, master_folder_name="Master Account", period_month=period_month
    )
    assert step.found_file is True
    assert step.row_count == 1

    invoice = conn.execute(
        select(invoices.c.amount_idr, invoices.c.vendor_description, invoices.c.status, invoices.c.purpose)
    ).one()
    assert invoice.amount_idr == Decimal("2617600")
    assert invoice.vendor_description == "Brandon Harvest"
    assert invoice.status == "needs_confirmation"  # phone purchase, Purpose correctly stays unset
    assert invoice.purpose is None


def test_sync_invoices_is_idempotent_on_drive_file_id(drive):
    conn, topo, client, period_month = drive
    sync_invoices(conn, client, root_folder_id=client.root_id, master_folder_name="Master Account", period_month=period_month)
    step2 = sync_invoices(
        conn, client, root_folder_id=client.root_id, master_folder_name="Master Account", period_month=period_month
    )
    assert step2.row_count == 0  # already ingested by drive_file_id, not duplicated
    assert len(conn.execute(select(invoices.c.id)).all()) == 1


# ---------------------------------------------------------------------------
# Full orchestration
# ---------------------------------------------------------------------------


def test_run_sync_for_period_wires_everything_and_runs_auto_match_and_post(drive):
    conn, topo, client, period_month = drive
    result = run_sync_for_period(
        conn,
        client,
        root_folder_id=client.root_id,
        period_month=period_month,
        ebay_account_id=topo["ebay_account_id"],
        ebay_account_folder_name="eBay Account 1",
        wallet_group_id=topo["wallet_group_id"],
        wallet_group_folder_name="Wallet Group 1",
        sync_wallet_group_bank_statement=False,  # no synthetic Bridging folder in this fixture
    )

    step_types = {s.document_type for s in result.steps}
    assert step_types == {"ebay_sales_csv", "payoneer_csv", "bank_statement_master", "invoices"}
    assert all(s.found_file for s in result.steps)

    assert result.auto_match is not None
    assert result.posted is not None
    # The real bank statement's 79 lines are all unlabeled recipient
    # transfers with no matching invoice/payout/keyword rule in this test's
    # minimal fixture — expected to mostly need review, not a bug (same
    # "never guess" principle as everywhere else).
    assert result.auto_match.needs_review > 0

    # Debits still equal credits across the ENTIRE pipeline's output.
    from ledger.schema import journal_lines

    lines = conn.execute(select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)).all()
    assert sum(l.debit_amount_idr for l in lines) == sum(l.credit_amount_idr for l in lines)


def test_run_sync_for_period_is_idempotent_across_two_runs(drive):
    conn, topo, client, period_month = drive
    kwargs = dict(
        root_folder_id=client.root_id,
        period_month=period_month,
        ebay_account_id=topo["ebay_account_id"],
        ebay_account_folder_name="eBay Account 1",
        wallet_group_id=topo["wallet_group_id"],
        wallet_group_folder_name="Wallet Group 1",
        sync_wallet_group_bank_statement=False,
    )
    run_sync_for_period(conn, client, **kwargs)

    source_doc_count_1 = len(conn.execute(select(source_documents.c.id)).all())
    review_queue_count_1 = len(conn.execute(select(review_queue.c.id)).all())
    journal_entry_count_1 = len(conn.execute(select(journal_entries.c.id)).all())

    result2 = run_sync_for_period(conn, client, **kwargs)

    source_doc_count_2 = len(conn.execute(select(source_documents.c.id)).all())
    review_queue_count_2 = len(conn.execute(select(review_queue.c.id)).all())
    journal_entry_count_2 = len(conn.execute(select(journal_entries.c.id)).all())

    assert source_doc_count_2 == source_doc_count_1  # updated in place, not duplicated
    assert review_queue_count_2 == review_queue_count_1  # row-creation idempotency held
    assert journal_entry_count_2 == journal_entry_count_1  # posting idempotency held
    assert result2.posted.posted == 0  # nothing new to post on the second run


# ---------------------------------------------------------------------------
# Live smoke test — real credential, real (currently empty) root folder.
# Skipped if this environment isn't configured for it, per the same pattern
# tests/conftest.py already uses for TEST_DATABASE_URL.
# ---------------------------------------------------------------------------


def test_live_list_files_against_real_configured_root_folder():
    root_id = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID")
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not root_id or not cred_path or not os.path.isfile(cred_path):
        pytest.skip("Live Drive credentials/root folder not configured in this environment.")

    from ingestion.drive_client import DriveClient

    client = DriveClient(readwrite=False)
    # Must not raise (proves the credential + scope + folder ID all work
    # together) — this is a real, if currently empty, Drive folder, so we
    # only assert it comes back as a list, not on its exact contents.
    files = client.list_files(root_id)
    assert isinstance(files, list)
