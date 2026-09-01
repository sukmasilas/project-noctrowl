"""BCA bank-statement PDF text extraction/parsing.

Implements docs/design/milestone-3-ingestion-design.md §4. The real sample
(`sample-documents/bank-statements/BCA Bank_1790345891_APR_2026.pdf`) turned
out to be born-digital (a machine-generated statement, not a scan) — its
text layer extracts cleanly via ``pdfplumber``, so this module uses plain
text extraction + line-based parsing rather than image OCR. A scanned
statement would need the ``pytesseract``/``pdf2image`` fallback path
described in the design doc (not exercised here, since it's a different code
path this real sample doesn't need — flagged, not silently assumed to cover
every future bank PDF).

**Real-sample discovery**: ``pdfplumber``'s table-detection heuristics do
NOT reliably reconstruct this statement's 5-column table (TANGGAL /
KETERANGAN / CBG / MUTASI / SALDO) — tried first, produced near-empty
results. Plain per-page ``extract_text()`` + line-based parsing worked
cleanly instead, so that's what this module does. Confirmed against the
statement's OWN printed reconciliation totals (`MUTASI CR : <amount> <count>`
/ `MUTASI DB : <amount> <count>`) — see ``parse_bca_statement``'s
``reconciles`` field, which is a genuine self-check, not just a hope.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

_MONTH_ID = {
    "JANUARI": 1, "FEBRUARI": 2, "MARET": 3, "APRIL": 4, "MEI": 5, "JUNI": 6,
    "JULI": 7, "AGUSTUS": 8, "SEPTEMBER": 9, "OKTOBER": 10, "NOVEMBER": 11, "DESEMBER": 12,
}

_TABLE_HEADER_MARKER = "TANGGAL KETERANGAN CBG MUTASI SALDO"
_PAGE_BREAK_MARKER = "Bersambung ke halaman berikut"
_FOOTER_SALDO_AWAL_MARKER = re.compile(r"^SALDO AWAL\s*:", flags=re.MULTILINE)

_DATE_LINE_RE = re.compile(r"^(\d{2})/(\d{2})\s+(.*)$")
_TAIL_AMOUNT_RE = re.compile(
    r"^(?P<desc>.*?)\s+(?P<mutasi>[\d,]+\.\d{2})\s*(?P<db>DB)?\s*(?P<saldo>[\d,]+\.\d{2})?\s*$"
)
_PERIODE_RE = re.compile(r"PERIODE\s*:\s*([A-Z]+)\s+(\d{4})")
_FOOTER_CR_RE = re.compile(r"MUTASI CR\s*:\s*([\d,]+\.\d{2})\s+(\d+)")
_FOOTER_DB_RE = re.compile(r"MUTASI DB\s*:\s*([\d,]+\.\d{2})\s+(\d+)")


def _parse_idr_amount(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


@dataclass
class BankStatementLine:
    transaction_date: _dt.date
    raw_description: str
    amount_idr: Decimal  # signed: positive = credit/inflow, negative = debit/outflow
    occurrence_index: int  # Nth occurrence of this exact (date, description, amount) tuple in the file


@dataclass
class BcaStatementParseResult:
    period_month: _dt.date | None
    opening_balance_idr: Decimal | None
    lines: list[BankStatementLine] = field(default_factory=list)
    reported_credit_total: Decimal | None = None
    reported_credit_count: int | None = None
    reported_debit_total: Decimal | None = None
    reported_debit_count: int | None = None
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def reconciles(self) -> bool:
        """True only if BOTH the parsed credit/debit totals AND counts match
        the statement's own printed reconciliation summary — the concrete,
        document-level "did extraction actually work" signal per design
        doc §4 (never posted as a per-line Needs Review guess; surfaced as
        a source_documents.parse_warning at the document level instead).
        """
        if self.reported_credit_total is None or self.reported_debit_total is None:
            return False
        credits = [l for l in self.lines if l.amount_idr > 0]
        debits = [l for l in self.lines if l.amount_idr < 0]
        return (
            sum((l.amount_idr for l in credits), Decimal("0")) == self.reported_credit_total
            and len(credits) == self.reported_credit_count
            and -sum((l.amount_idr for l in debits), Decimal("0")) == self.reported_debit_total
            and len(debits) == self.reported_debit_count
        )


def extract_pdf_text_per_page(pdf_path_or_file) -> list[str]:
    """Thin wrapper around pdfplumber, isolated to its own function so
    tests/callers that only care about the parsing LOGIC can pass in
    already-extracted text instead of a real PDF (no pdfplumber/PDF
    dependency needed for those tests).
    """
    import pdfplumber  # imported lazily — a new milestone-3 dependency, not needed by ledger/*

    with pdfplumber.open(pdf_path_or_file) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def _extract_transaction_body_lines(pages_text: list[str]) -> list[str]:
    """Per page, keep only the text between the table header marker and
    either the "continued on next page" footer or the summary footer —
    discards the repeated letterhead/address/notes boilerplate without
    needing to enumerate every possible address line (see module
    docstring).
    """
    body_lines: list[str] = []
    for page_text in pages_text:
        header_idx = page_text.find(_TABLE_HEADER_MARKER)
        if header_idx == -1:
            continue
        body = page_text[header_idx + len(_TABLE_HEADER_MARKER) :]
        page_break_idx = body.find(_PAGE_BREAK_MARKER)
        if page_break_idx != -1:
            body = body[:page_break_idx]
        # Only cut at the *summary* "SALDO AWAL :" footer (colon form),
        # never the in-table "01/04 SALDO AWAL <amount>" opening-balance
        # transaction row (no colon) — that one is handled separately below,
        # as a normal date-prefixed line whose description is "SALDO AWAL".
        m = _FOOTER_SALDO_AWAL_MARKER.search(body)
        if m:
            body = body[: m.start()]
        body_lines.extend(line.strip() for line in body.splitlines() if line.strip())
    return body_lines


def parse_bca_statement_text(pages_text: list[str]) -> BcaStatementParseResult:
    """Parse already-extracted per-page text (see ``extract_pdf_text_per_page``
    for the PDF-reading half) into a structured result.
    """
    period_month = None
    for page_text in pages_text:
        m = _PERIODE_RE.search(page_text)
        if m:
            month_name, year = m.group(1), int(m.group(2))
            month_num = _MONTH_ID.get(month_name)
            if month_num:
                period_month = _dt.date(year, month_num, 1)
            break

    reported_credit_total = reported_credit_count = None
    reported_debit_total = reported_debit_count = None
    for page_text in pages_text:
        m = _FOOTER_CR_RE.search(page_text)
        if m:
            reported_credit_total = _parse_idr_amount(m.group(1))
            reported_credit_count = int(m.group(2))
        m = _FOOTER_DB_RE.search(page_text)
        if m:
            reported_debit_total = _parse_idr_amount(m.group(1))
            reported_debit_count = int(m.group(2))

    body_lines = _extract_transaction_body_lines(pages_text)

    result = BcaStatementParseResult(
        period_month=period_month,
        opening_balance_idr=None,
        reported_credit_total=reported_credit_total,
        reported_credit_count=reported_credit_count,
        reported_debit_total=reported_debit_total,
        reported_debit_count=reported_debit_count,
    )
    if period_month is None:
        result.parse_warnings.append("Could not find 'PERIODE : <MONTH> <YEAR>' anywhere in the statement.")

    current_year = period_month.year if period_month else _dt.date.today().year

    occurrence_counts: dict[tuple, int] = {}
    i = 0
    while i < len(body_lines):
        line = body_lines[i]
        date_match = _DATE_LINE_RE.match(line)
        if not date_match:
            # A continuation line that appears before any transaction has
            # started (shouldn't normally happen post-boilerplate-strip) —
            # skip rather than guess which transaction it belongs to.
            i += 1
            continue

        day, month = int(date_match.group(1)), int(date_match.group(2))
        try:
            txn_date = _dt.date(current_year, month, day)
        except ValueError:
            result.parse_warnings.append(f"Could not build a valid date from day={day} month={month}: {line!r}")
            i += 1
            continue
        rest = date_match.group(3)

        # Gather this transaction's full text block: the first line's
        # remainder, plus every following line until the next date-start
        # line (KETERANGAN commonly wraps across several lines — see
        # module docstring).
        block_lines = [rest]
        j = i + 1
        while j < len(body_lines) and not _DATE_LINE_RE.match(body_lines[j]):
            block_lines.append(body_lines[j])
            j += 1

        first_line_tail = _TAIL_AMOUNT_RE.match(block_lines[0])
        if not first_line_tail:
            result.parse_warnings.append(
                f"Line starting {txn_date.isoformat()!r} has no recognizable trailing MUTASI amount: "
                f"{block_lines[0]!r} — skipped, not parsed as a transaction."
            )
            i = j
            continue

        desc_first = first_line_tail.group("desc")
        mutasi = _parse_idr_amount(first_line_tail.group("mutasi"))
        is_debit = first_line_tail.group("db") is not None

        description_parts = [desc_first] + block_lines[1:]
        raw_description = " / ".join(p for p in description_parts if p)

        if desc_first.strip() == "SALDO AWAL":
            result.opening_balance_idr = mutasi
            i = j
            continue

        signed_amount = -mutasi if is_debit else mutasi

        key = (txn_date, raw_description, signed_amount)
        occurrence_counts[key] = occurrence_counts.get(key, 0) + 1
        result.lines.append(
            BankStatementLine(
                transaction_date=txn_date,
                raw_description=raw_description,
                amount_idr=signed_amount,
                occurrence_index=occurrence_counts[key],
            )
        )
        i = j

    return result


def parse_bca_statement(pdf_path_or_file) -> BcaStatementParseResult:
    """Convenience: extract + parse in one call."""
    pages_text = extract_pdf_text_per_page(pdf_path_or_file)
    return parse_bca_statement_text(pages_text)
