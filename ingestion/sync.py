"""End-to-end sync orchestration.

Per Main-agent's 2026-08-31 resolution to CLAUDE.md's Build milestones
(milestone 3 vs. milestone 5 boundary, closing a real ambiguity QA
surfaced): "all Drive-fed" means milestone 3 must deliver one genuine
end-to-end pipeline — list the expected Drive folder, download, parse,
write ``source_documents``/``review_queue`` rows, auto-match, and post —
callable manually as a plain function. Milestone 5 only adds *when* this
already-proven pipeline runs automatically (the H-15/H+7 window, Sync Now's
cooldown, month-end FX timing); it does not build the pipeline itself.

Deliberately NOT built here (still milestone 5, per the brief's explicit
boundary): automatic month-ahead Drive folder provisioning, any
schedule/cron trigger, the H-15/H+7 active-window gating, Sync Now's
cooldown. ``run_sync_for_period`` is a plain, synchronous, manually-callable
function — nothing wires it to a clock.

Folder-ID resolution is done by walking down from the configured Drive root
by NAME (read-only ``list_files`` calls only — see ``ingestion.drive_client``'s
scope note), matching CLAUDE.md's Architecture section's folder-naming
convention exactly. If a folder in the expected chain doesn't exist yet,
that document type is simply "not yet uploaded" for this period — the same
signal ``source_documents.ingested_at IS NULL`` already represents; no
folder is ever created here (that would need write scope this module
deliberately never requests).

``drive_client`` parameters throughout accept anything satisfying the
minimal duck-typed interface (``list_files(folder_id, mime_types=None)``,
``download_file(file_id)``) that ``ingestion.drive_client.DriveClient``
implements — this keeps every function here testable against a plain fake
object, without needing to mock the Google API client chain again for every
test (that's already covered once, at the DriveClient boundary itself, in
tests/ingestion/test_drive_client.py).
"""
from __future__ import annotations

import datetime as _dt
import io
from dataclasses import dataclass, field
from sqlalchemy import select, text, update
from sqlalchemy.engine import Connection

from ingestion import bank_statement, ebay_csv, invoices as invoices_module, mandiri_statement, matching, payoneer
from ingestion.drive_client import FOLDER_MIME_TYPE, DriveFile
from ingestion.kurs_pajak import lookup_most_recent_rate_as_of
from ingestion.reconciliation import check_account_reconciliation
from ingestion.schema import invoices as invoices_table
from ingestion.schema import source_documents
from ledger.entities import get_account_id
from ledger.errors import UnknownAccountInstanceError

UPLOADS_ROOT_NAME = "01 - Uploads"
EBAY_SALES_SUBFOLDER = "eBay Sales Export (CSV)"
PAYONEER_SUBFOLDER = "Payoneer Exports (CSV)"
BANK_STATEMENTS_SUBFOLDER = "Bank Statements (PDF)"
INVOICES_SUBFOLDER = "Invoices & Proof of Purchase"

_CSV_EXTENSIONS = (".csv",)
_PDF_EXTENSIONS = (".pdf",)
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def _period_segments(period_month: _dt.date) -> tuple[str, str]:
    return str(period_month.year), f"{period_month.year}-{period_month.month:02d}"


def _matches_ext(name: str, extensions: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(ext) for ext in extensions)


def _find_child_folder(drive_client, parent_id: str, name: str) -> DriveFile | None:
    for f in drive_client.list_files(parent_id, mime_types=[FOLDER_MIME_TYPE]):
        if f.name == name:
            return f
    return None


def resolve_folder_path(drive_client, root_folder_id: str, *names: str) -> str | None:
    """Walk down from the Drive root by folder name, read-only. Returns
    None (never fabricates/creates an ID) if any segment doesn't exist yet.
    """
    current_id = root_folder_id
    for name in names:
        folder = _find_child_folder(drive_client, current_id, name)
        if folder is None:
            return None
        current_id = folder.id
    return current_id


# ---------------------------------------------------------------------------
# source_documents upsert — the "expected fixed-expectation upload" row,
# per design doc §1. Created lazily here (on first sync attempt for a
# period) rather than via a separate advance-provisioning step, since
# nothing in this milestone's scope calls for pre-creating them ahead of
# time (that would only matter for a Documents-screen "Not yet uploaded vs.
# Missing-overdue" distinction, which needs the H+7 deadline logic that's
# explicitly milestone 5).
# ---------------------------------------------------------------------------


def _scope_clause(document_type: str, period_month: _dt.date, ebay_account_id, wallet_group_id):
    clause = (source_documents.c.document_type == document_type) & (
        source_documents.c.period_month == period_month
    )
    clause &= (
        source_documents.c.ebay_account_id == ebay_account_id
        if ebay_account_id is not None
        else source_documents.c.ebay_account_id.is_(None)
    )
    clause &= (
        source_documents.c.wallet_group_id == wallet_group_id
        if wallet_group_id is not None
        else source_documents.c.wallet_group_id.is_(None)
    )
    return clause


def _upsert_source_document(
    conn: Connection,
    *,
    document_type: str,
    period_month: _dt.date,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
    ingested: bool = False,
    drive_file_id: str | None = None,
    drive_file_name: str | None = None,
    row_count: int | None = None,
    parse_warning: str | None = None,
) -> int:
    existing = conn.execute(
        select(source_documents.c.id).where(
            _scope_clause(document_type, period_month, ebay_account_id, wallet_group_id)
        )
    ).first()
    values = dict(
        ingested_at=_dt.datetime.now(_dt.timezone.utc) if ingested else None,
        drive_file_id=drive_file_id,
        drive_file_name=drive_file_name,
        row_count=row_count,
        parse_warning=parse_warning,
    )
    if existing is not None:
        conn.execute(update(source_documents).where(source_documents.c.id == existing.id).values(**values))
        return existing.id
    result = conn.execute(
        source_documents.insert().values(
            document_type=document_type,
            period_month=period_month,
            ebay_account_id=ebay_account_id,
            wallet_group_id=wallet_group_id,
            **values,
        )
    )
    return result.inserted_primary_key[0]


def _set_parse_warning(conn: Connection, source_document_id: int, warnings: list[str]) -> None:
    if warnings:
        conn.execute(
            update(source_documents)
            .where(source_documents.c.id == source_document_id)
            .values(parse_warning="; ".join(warnings))
        )


@dataclass
class StepResult:
    document_type: str
    found_file: bool
    row_count: int | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# eBay sales CSV
# ---------------------------------------------------------------------------


def sync_ebay_sales_csv(
    conn: Connection,
    drive_client,
    *,
    root_folder_id: str,
    ebay_account_id: int,
    ebay_account_folder_name: str,
    period_month: _dt.date,
) -> StepResult:
    year, ym = _period_segments(period_month)
    folder_id = resolve_folder_path(
        drive_client, root_folder_id, UPLOADS_ROOT_NAME, ebay_account_folder_name, year, ym, EBAY_SALES_SUBFOLDER
    )
    if folder_id is None:
        _upsert_source_document(
            conn, document_type="ebay_sales_csv", period_month=period_month, ebay_account_id=ebay_account_id
        )
        return StepResult(document_type="ebay_sales_csv", found_file=False)

    files = [f for f in drive_client.list_files(folder_id) if _matches_ext(f.name, _CSV_EXTENSIONS)]
    if not files:
        _upsert_source_document(
            conn, document_type="ebay_sales_csv", period_month=period_month, ebay_account_id=ebay_account_id
        )
        return StepResult(document_type="ebay_sales_csv", found_file=False)

    warnings: list[str] = []
    if len(files) > 1:
        warnings.append(
            f"{len(files)} eBay sales CSV files found for {period_month.isoformat()} (expected exactly 1) — "
            "using the most recently modified one; the rest were ignored, needs manual review."
        )
        files.sort(key=lambda f: f.modified_time or "", reverse=True)
    chosen = files[0]

    raw_bytes = drive_client.download_file(chosen.id)
    text = raw_bytes.decode("utf-8-sig")
    _header, rows = ebay_csv.parse_ebay_csv_rows(text)

    src_id = _upsert_source_document(
        conn,
        document_type="ebay_sales_csv",
        period_month=period_month,
        ebay_account_id=ebay_account_id,
        ingested=True,
        drive_file_id=chosen.id,
        drive_file_name=chosen.name,
        row_count=len(rows),
    )
    result = ebay_csv.process_transaction_report(
        conn, ebay_account_id=ebay_account_id, source_document_id=src_id, rows=rows
    )
    warnings.extend(result.parse_warnings)
    _set_parse_warning(conn, src_id, warnings)
    return StepResult(document_type="ebay_sales_csv", found_file=True, row_count=len(rows), warnings=warnings)


# ---------------------------------------------------------------------------
# Payoneer CSV + withdrawal confirmation PDF(s)
# ---------------------------------------------------------------------------


def sync_payoneer(
    conn: Connection,
    drive_client,
    *,
    root_folder_id: str,
    wallet_group_id: int,
    wallet_group_folder_name: str,
    period_month: _dt.date,
) -> StepResult:
    year, ym = _period_segments(period_month)
    folder_id = resolve_folder_path(
        drive_client, root_folder_id, UPLOADS_ROOT_NAME, wallet_group_folder_name, year, ym, PAYONEER_SUBFOLDER
    )
    if folder_id is None:
        _upsert_source_document(
            conn, document_type="payoneer_csv", period_month=period_month, wallet_group_id=wallet_group_id
        )
        return StepResult(document_type="payoneer_csv", found_file=False)

    all_files = drive_client.list_files(folder_id)
    csv_files = [f for f in all_files if _matches_ext(f.name, _CSV_EXTENSIONS)]
    # Withdrawal confirmation PDFs are assumed to live in this same folder
    # (both are Payoneer-sourced documents for the same wallet-group/period)
    # — an explicit, flagged assumption per
    # docs/design/milestone-3-ingestion-design.md §3, not yet confirmed
    # against a real folder (none exist yet).
    pdf_files = [f for f in all_files if _matches_ext(f.name, _PDF_EXTENSIONS)]

    if not csv_files:
        _upsert_source_document(
            conn, document_type="payoneer_csv", period_month=period_month, wallet_group_id=wallet_group_id
        )
        return StepResult(document_type="payoneer_csv", found_file=False)

    warnings: list[str] = []
    if len(csv_files) > 1:
        warnings.append(
            f"{len(csv_files)} Payoneer CSV files found for {period_month.isoformat()} (expected exactly 1) — "
            "processing all of them, flagging for manual review."
        )

    all_rows: list[dict[str, str]] = []
    for f in csv_files:
        text = drive_client.download_file(f.id).decode("utf-8-sig")
        all_rows.extend(payoneer.parse_payoneer_csv_rows(text))

    confirmations = []
    for f in pdf_files:
        raw_bytes = drive_client.download_file(f.id)
        try:
            confirmations.append(payoneer.parse_confirmation_pdf(io.BytesIO(raw_bytes)))
        except ValueError as exc:
            warnings.append(f"Could not parse {f.name!r} as a withdrawal confirmation: {exc}")

    src_id = _upsert_source_document(
        conn,
        document_type="payoneer_csv",
        period_month=period_month,
        wallet_group_id=wallet_group_id,
        ingested=True,
        drive_file_id=csv_files[0].id,
        drive_file_name=csv_files[0].name,
        row_count=len(all_rows),
    )
    result = payoneer.process_payoneer_rows(
        conn,
        wallet_group_id=wallet_group_id,
        source_document_id=src_id,
        rows=all_rows,
        confirmations=confirmations,
        booking_rate_lookup=lookup_most_recent_rate_as_of,
    )
    warnings.extend(result.parse_warnings)
    _set_parse_warning(conn, src_id, warnings)
    return StepResult(document_type="payoneer_csv", found_file=True, row_count=len(all_rows), warnings=warnings)


# ---------------------------------------------------------------------------
# Bank statement (generalized for the wallet-group's own bridging-account
# statement OR the consolidated Master Account statement).
# ---------------------------------------------------------------------------


_MANDIRI_MARKER = "Bank Mandiri"
_BCA_MARKER = "TANGGAL KETERANGAN CBG MUTASI SALDO"

# Reconciliation-gap detection (added 2026-09) — which ledger account_type
# code (and scoping) each bank-statement document_type's own printed
# opening/closing balance should be checked against. See
# ingestion/reconciliation.py's module docstring for why this is a small,
# explicit mapping rather than something dynamically discovered: there is
# no way to know WHICH ledger account a brand-new document type's balance
# belongs to without a human wiring that up once — the reconciliation
# MECHANISM itself (the table, the computation, the materiality threshold,
# the Provisional gating, the Data Quality screen) is what's generic and
# reusable, not this one-line mapping.
_RECONCILIATION_ACCOUNT_CODE_BY_DOCUMENT_TYPE = {
    "bank_statement_wallet_group": "BCA_BRIDGING",
    "bank_statement_master": "BCA_MAIN",
}


def _detect_bank_statement_format(pages_text: list[str]) -> str:
    """Sniff which bank actually issued this statement from its own
    content, rather than assuming by document_type/folder (see Fix 1's
    finding — the design doc's original assumption that the Bridging
    Account is BCA-formatted "because it's the same bank as Main" was wrong;
    the real Bridging Account statement is Mandiri-issued, in a completely
    different layout — see ingestion/mandiri_statement.py's docstring).
    Content-sniffed, not hardcoded by document_type, so a future
    wallet-group whose bridging bank differs again doesn't need a code
    change here — just another branch in this function once a real sample
    exists for it.
    """
    combined = "\n".join(pages_text)
    if _MANDIRI_MARKER in combined:
        return "mandiri"
    if _BCA_MARKER in combined:
        return "bca"
    return "unknown"


def sync_bank_statement(
    conn: Connection,
    drive_client,
    *,
    root_folder_id: str,
    period_month: _dt.date,
    wallet_group_id: int | None = None,
    wallet_group_folder_name: str | None = None,
    master_folder_name: str | None = None,
) -> StepResult:
    if wallet_group_id is not None:
        document_type = "bank_statement_wallet_group"
        top_folder_name = wallet_group_folder_name
        if top_folder_name is None:
            raise ValueError("wallet_group_folder_name is required when wallet_group_id is set")
    else:
        document_type = "bank_statement_master"
        top_folder_name = master_folder_name
        if top_folder_name is None:
            raise ValueError("master_folder_name is required when wallet_group_id is None")

    year, ym = _period_segments(period_month)
    folder_id = resolve_folder_path(
        drive_client, root_folder_id, UPLOADS_ROOT_NAME, top_folder_name, year, ym, BANK_STATEMENTS_SUBFOLDER
    )
    if folder_id is None:
        _upsert_source_document(
            conn, document_type=document_type, period_month=period_month, wallet_group_id=wallet_group_id
        )
        return StepResult(document_type=document_type, found_file=False)

    files = [f for f in drive_client.list_files(folder_id) if _matches_ext(f.name, _PDF_EXTENSIONS)]
    if not files:
        _upsert_source_document(
            conn, document_type=document_type, period_month=period_month, wallet_group_id=wallet_group_id
        )
        return StepResult(document_type=document_type, found_file=False)

    warnings: list[str] = []
    if len(files) > 1:
        warnings.append(
            f"{len(files)} bank statement PDFs found for {period_month.isoformat()} (expected exactly 1) — "
            "using the most recently modified one; the rest were ignored, needs manual review."
        )
        files.sort(key=lambda f: f.modified_time or "", reverse=True)
    chosen = files[0]

    raw_bytes = drive_client.download_file(chosen.id)
    pages_text = bank_statement.extract_pdf_text_per_page(io.BytesIO(raw_bytes))
    statement_format = _detect_bank_statement_format(pages_text)

    if statement_format == "mandiri":
        pages_words = mandiri_statement.extract_pdf_page_words(io.BytesIO(raw_bytes))
        parsed = mandiri_statement.parse_mandiri_statement_pages(pages_text, pages_words)
        warnings.extend(parsed.parse_warnings)
        if parsed.reported_dana_masuk is not None and not parsed.reconciles:
            warnings.append(
                "Parsed lines do NOT reconcile against the statement's own printed Dana Masuk/Dana Keluar/"
                "Saldo Awal/Saldo Akhir totals — extraction may have missed or misread a line; needs a "
                "manual look before trusting this period's numbers."
            )
    else:
        # Default to the BCA parser for 'bca' and 'unknown' alike — BCA is
        # the only confirmed real format for bank_statement_master (the
        # consolidated Master Account), and was this whole pipeline's
        # original/only format before Fix 1; an unrecognized format still
        # gets a best-effort BCA-shaped parse attempt (its own parse
        # -warnings/reconciliation check will correctly flag a bad read
        # rather than silently trusting an empty result).
        parsed = bank_statement.parse_bca_statement_text(pages_text)
        warnings.extend(parsed.parse_warnings)
        if parsed.reported_credit_total is not None and not parsed.reconciles:
            warnings.append(
                "Parsed lines do NOT reconcile against the statement's own printed CR/DB totals — "
                "extraction may have missed or misread a line; needs a manual look before trusting this period's numbers."
            )
        if statement_format == "unknown":
            warnings.append(
                "Could not identify this statement as either a known BCA or Mandiri format from its own "
                "content — attempted a best-effort BCA-shaped parse; treat this period's numbers with extra caution."
            )

    src_id = _upsert_source_document(
        conn,
        document_type=document_type,
        period_month=period_month,
        wallet_group_id=wallet_group_id,
        ingested=True,
        drive_file_id=chosen.id,
        drive_file_name=chosen.name,
        row_count=len(parsed.lines),
    )

    raw_lines = [
        matching.RawLine(
            transaction_date=l.transaction_date,
            raw_description=l.raw_description,
            amount_idr=l.amount_idr,
            occurrence_index=l.occurrence_index,
        )
        for l in parsed.lines
    ]
    matching.stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=wallet_group_id,
        lines=raw_lines,
    )

    # Reconciliation-gap detection (added 2026-09), run right after this
    # bank statement has been parsed and its rows staged — per this
    # feature's brief: "right after a bank statement for a period has been
    # parsed and its rows processed". Only runs when the statement actually
    # carries BOTH a printed opening AND closing balance (never forced —
    # see ingestion/reconciliation.py's module docstring) and when this
    # scope already has a ledger account set up to check against.
    if parsed.opening_balance_idr is not None and parsed.closing_balance_idr is not None:
        account_code = _RECONCILIATION_ACCOUNT_CODE_BY_DOCUMENT_TYPE.get(document_type)
        if account_code is not None:
            try:
                reconciliation_account_id = get_account_id(
                    conn, account_code, wallet_group_id=wallet_group_id
                )
            except UnknownAccountInstanceError:
                warnings.append(
                    f"No {account_code} ledger account set up yet for this scope — skipped the automated "
                    "reconciliation check against the statement's own printed opening/closing balance."
                )
            else:
                outcome = check_account_reconciliation(
                    conn,
                    account_id=reconciliation_account_id,
                    period_month=period_month,
                    statement_opening_idr=parsed.opening_balance_idr,
                    statement_closing_idr=parsed.closing_balance_idr,
                    source_document_id=src_id,
                )
                if outcome.is_material:
                    warnings.append(
                        "Reconciliation discrepancy: the ledger's own computed balance for this account "
                        f"does not match the statement's own printed balance (opening off by Rp "
                        f"{outcome.opening_discrepancy_idr}, closing off by Rp {outcome.closing_discrepancy_idr}) "
                        "— see the Data Quality screen. This needs a human to investigate; nothing in this "
                        "pipeline corrects it automatically."
                    )
    _set_parse_warning(conn, src_id, warnings)

    return StepResult(document_type=document_type, found_file=True, row_count=len(parsed.lines), warnings=warnings)


# ---------------------------------------------------------------------------
# Invoices & proof-of-purchase — NOT a fixed expectation (no source_documents
# row; variable count, per CLAUDE.md's Documents screen design). Best-effort
# extraction only — a full per-format invoice classifier beyond the three
# known real sample shapes (Tokopedia-style PDF, or "needs a human either
# way" for anything else) is out of scope here; see ingestion/invoices.py
# for the actual honest reliability assessment per format.
# ---------------------------------------------------------------------------


def sync_invoices(
    conn: Connection,
    drive_client,
    *,
    root_folder_id: str,
    master_folder_name: str,
    period_month: _dt.date,
) -> StepResult:
    year, ym = _period_segments(period_month)
    folder_id = resolve_folder_path(
        drive_client, root_folder_id, UPLOADS_ROOT_NAME, master_folder_name, year, ym, INVOICES_SUBFOLDER
    )
    if folder_id is None:
        return StepResult(document_type="invoices", found_file=False)

    files = drive_client.list_files(folder_id)
    created = 0
    warnings: list[str] = []
    for f in files:
        already = conn.execute(select(invoices_table.c.id).where(invoices_table.c.drive_file_id == f.id)).first()
        if already is not None:
            continue  # row-creation idempotency, same principle as review_queue's external_ref

        raw_bytes = drive_client.download_file(f.id)
        extraction = _extract_invoice(f, raw_bytes)
        conn.execute(
            invoices_table.insert().values(
                drive_file_id=f.id,
                drive_file_name=f.name,
                period_month=period_month,
                extracted_date=extraction.extracted_date,
                vendor_description=extraction.vendor_description,
                amount_idr=extraction.amount_idr,
                purpose=extraction.purpose,
                status=extraction.status,
                ocr_raw_text=extraction.ocr_raw_text,
            )
        )
        created += 1
        warnings.extend(f"{f.name}: {w}" for w in extraction.warnings)

    return StepResult(document_type="invoices", found_file=len(files) > 0, row_count=created, warnings=warnings)


def _extract_invoice(drive_file: DriveFile, raw_bytes: bytes) -> invoices_module.InvoiceExtraction:
    if _matches_ext(drive_file.name, _PDF_EXTENSIONS):
        try:
            # extract_pdf_invoice() sniffs the actual PDF content (Tokopedia
            # / Shopee / a known Operations vendor) rather than assuming
            # every PDF invoice is Tokopedia-shaped — see
            # ingestion/invoices.py's dispatcher docstring. Replaces the
            # milestone-3 original's hardcoded extract_tokopedia_pdf() call,
            # which was correct when Tokopedia was the only real sample but
            # broke once real Shopee/Operations-vendor invoices arrived.
            return invoices_module.extract_pdf_invoice(io.BytesIO(raw_bytes))
        except Exception as exc:  # noqa: BLE001 - any unrecognized PDF format
            return invoices_module.InvoiceExtraction(
                extracted_date=None,
                vendor_description=None,
                amount_idr=None,
                purpose=None,
                status="needs_confirmation",
                ocr_raw_text=None,
                warnings=[f"Unrecognized/unparseable PDF invoice format ({exc})"],
            )

    if _matches_ext(drive_file.name, _IMAGE_EXTENSIONS):
        raw_text = invoices_module.extract_image_ocr(io.BytesIO(raw_bytes))
        if raw_text is None:
            return invoices_module.InvoiceExtraction(
                extracted_date=None,
                vendor_description=None,
                amount_idr=None,
                purpose=None,
                status="needs_confirmation",
                ocr_raw_text=None,
                warnings=["OCR backend unavailable or could not read this image — needs manual entry"],
            )
        # No structured label-based format is known for an arbitrary
        # receipt image (per ingestion/invoices.py's honest assessment,
        # this is expected to be the common case for the handwritten
        # -receipt document type) — always needs_confirmation, raw OCR text
        # preserved for a human to read and fill in the fields from.
        return invoices_module.InvoiceExtraction(
            extracted_date=None,
            vendor_description=None,
            amount_idr=None,
            purpose=None,
            status="needs_confirmation",
            ocr_raw_text=raw_text,
            warnings=["Image invoice — extracted raw OCR text only, fields need manual entry"],
        )

    return invoices_module.InvoiceExtraction(
        extracted_date=None,
        vendor_description=None,
        amount_idr=None,
        purpose=None,
        status="needs_confirmation",
        ocr_raw_text=None,
        warnings=[f"Unrecognized file type for an invoice ({drive_file.mime_type})"],
    )


# ---------------------------------------------------------------------------
# The orchestration entrypoint.
# ---------------------------------------------------------------------------


class SyncAlreadyRunningError(RuntimeError):
    """Raised when another sync pipeline run already holds the advisory
    lock below — a concurrent trigger correctly backing off rather than
    racing it. Never a bug/crash signal; the caller (e.g. webapp's Sync Now
    handler) should surface this as a plain "a sync is already running, try
    again shortly" message, same spirit as the Sync Now cooldown.
    """


# Fix (2026-09-01, QA-found concurrency bug): ingestion.matching's
# review-queue matching/posting (specifically _post_internal_transfer's
# paired_review_queue_id claim, but also several OTHER SELECT-then-UPDATE
# "claim" patterns in this pipeline — ebay_expected_payouts.matched_at,
# consignment_sales.reimbursed_journal_entry_id/journal_entry_id, invoices'
# drive_file_id dedup) is only safe under SEQUENTIAL execution. QA proved
# this concretely: a two-thread test against a real staged Bridging/Main
# pair, forced to maximal overlap with a threading.Barrier, produced TWO
# separate journal entries for what should be one real transfer. QA also
# found a real trigger path: webapp's Sync Now cooldown
# (webapp/sync_cooldown.py) only records a run AFTER run_sync_for_period()
# completes, so two near-simultaneous clicks (double-click, two tabs, a
# client retry) can both pass the cooldown check and run the pipeline on
# two separate DB connections at once.
#
# Fixed with a single, GLOBAL (not per-wallet-group — several of the races
# above aren't wallet-group-scoped either, e.g. rule (b)'s invoice matching
# and rule (d)'s consignment matching both search across ALL wallet groups)
# Postgres advisory lock, acquired before ANY work starts in
# run_sync_for_period below. Two deliberate choices on the exact primitive:
#
# - ``pg_try_advisory_xact_lock`` (the non-blocking, TRANSACTION-scoped
#   variant), not ``pg_advisory_lock``: non-blocking means a second
#   concurrent caller fails fast with SyncAlreadyRunningError instead of
#   silently queueing/hanging the HTTP request (bad UX, and this pipeline
#   does real work — OCR, Drive downloads — that a 1GB droplet shouldn't
#   have two of piling up regardless of correctness, per CLAUDE.md's
#   Scheduling section). Transaction-scoped (not session-scoped) means the
#   lock is automatically released at COMMIT or ROLLBACK of the caller's
#   own transaction (webapp commits once, at the end, after
#   run_sync_for_period returns — see webapp/documents_bp.py) with NO
#   manual unlock call needed anywhere — a session-scoped lock would risk
#   leaking forever on a pooled connection if some future code path ever
#   skipped a manual unlock (e.g. an unusual exception path), silently
#   bricking every future sync until the app restarted. A transaction-scoped
#   lock cannot leak that way: the DB itself guarantees release at
#   transaction end no matter how the Python side exits.
# - Re-acquiring the SAME lock from the SAME transaction is a safe no-op
#   (Postgres advisory locks are reentrant per session/xact) — so a test (or
#   any future caller) that calls run_sync_for_period more than once within
#   one open transaction/connection, sequentially, is unaffected; only a
#   GENUINELY concurrent second transaction is rejected.
SYNC_PIPELINE_ADVISORY_LOCK_KEY = 872341987


@dataclass
class SyncResult:
    steps: list[StepResult] = field(default_factory=list)
    auto_match: matching.AutoMatchResult | None = None
    posted: matching.PostResult | None = None


def run_sync_for_period(
    conn: Connection,
    drive_client,
    *,
    root_folder_id: str,
    period_month: _dt.date,
    ebay_account_id: int,
    ebay_account_folder_name: str,
    wallet_group_id: int,
    wallet_group_folder_name: str,
    master_folder_name: str = "Master Account",
    sync_wallet_group_bank_statement: bool = True,
) -> SyncResult:
    """The one callable pipeline: list each expected Drive folder for this
    account/wallet-group/period, download whatever's there, parse it, post
    what can post directly, stage what needs review, then run the matching
    engine and post everything ready. A plain function — no trigger, no
    schedule, no cooldown (all explicitly milestone 5, see module docstring).

    ``sync_wallet_group_bank_statement`` defaults True but can be set False
    for a wallet-group with no genuinely separate Bridging Account statement
    (see CLAUDE.md's Prototype scope note on this) — set False rather than
    silently treating an absent Bridging folder as "not yet uploaded" when
    it may not exist as a concept for this business's real setup at all.

    Raises SyncAlreadyRunningError immediately (before any Drive/parsing
    work starts) if another sync run is concurrently in progress on a
    different DB connection — see SYNC_PIPELINE_ADVISORY_LOCK_KEY above.
    """
    acquired = conn.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SYNC_PIPELINE_ADVISORY_LOCK_KEY}
    ).scalar()
    if not acquired:
        raise SyncAlreadyRunningError(
            "Another sync is already running — its review-queue matching/posting isn't safe to run "
            "concurrently with a second one. Try again once it finishes."
        )

    result = SyncResult()

    result.steps.append(
        sync_ebay_sales_csv(
            conn,
            drive_client,
            root_folder_id=root_folder_id,
            ebay_account_id=ebay_account_id,
            ebay_account_folder_name=ebay_account_folder_name,
            period_month=period_month,
        )
    )
    result.steps.append(
        sync_payoneer(
            conn,
            drive_client,
            root_folder_id=root_folder_id,
            wallet_group_id=wallet_group_id,
            wallet_group_folder_name=wallet_group_folder_name,
            period_month=period_month,
        )
    )
    if sync_wallet_group_bank_statement:
        result.steps.append(
            sync_bank_statement(
                conn,
                drive_client,
                root_folder_id=root_folder_id,
                period_month=period_month,
                wallet_group_id=wallet_group_id,
                wallet_group_folder_name=wallet_group_folder_name,
            )
        )
    result.steps.append(
        sync_bank_statement(
            conn,
            drive_client,
            root_folder_id=root_folder_id,
            period_month=period_month,
            master_folder_name=master_folder_name,
        )
    )
    result.steps.append(
        sync_invoices(
            conn,
            drive_client,
            root_folder_id=root_folder_id,
            master_folder_name=master_folder_name,
            period_month=period_month,
        )
    )

    result.auto_match = matching.run_auto_match(conn)
    result.posted = matching.post_pending_rows(conn)
    return result
