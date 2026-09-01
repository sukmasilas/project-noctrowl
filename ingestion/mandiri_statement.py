"""Mandiri Bridging Account e-Statement PDF text extraction/parsing.

**Real-sample discovery (2026-09, follow-up to milestone 3)**: the design doc's
original assumption — "the Bridging Account statement is BCA-issued, same
layout as the Main Account sample, so ``bank_statement.py``'s BCA parser
applies to both" — turned out to be wrong once a real Bridging Account
statement was collected (`sample-documents/Bridging Account (Mandiri)/`).
It's issued by **Bank Mandiri**, not BCA, in an "e-Statement" layout that is
structurally different (a genuine 5-column table — No / Tanggal / Keterangan
/ Nominal / Saldo — vs. BCA's continuous-text KETERANGAN block; explicit
``+``/``-`` signed amounts instead of a trailing ``DB`` suffix; a running
per-line balance column; and header-level Dana Masuk/Dana Keluar/Saldo Awal/
Saldo Akhir summary totals instead of BCA's MUTASI CR/MUTASI DB + count
footer). This is a genuinely different parser, not a reuse of
``bank_statement.py``'s BCA logic — see this module's docstring below for why
a fresh columnar approach was needed instead of forcing it through the BCA
line-based approach.

**Born-digital, confirmed before assuming an OCR path was needed**: like the
BCA sample, ``pdfplumber``'s plain ``extract_text()`` returns clean text —
this is a machine-generated statement, not a scan. However, unlike BCA,
``extract_text()``'s default line-merging turned out to be UNRELIABLE for
this layout specifically: pdfplumber merges same-vertical-position text
across columns into one "line" of output, and Mandiri's Keterangan
(description) column is often 3-4 lines tall while the No/Nominal/Saldo
cells are single-line and vertically centered partway down the row — so
``extract_text()`` interleaves a transaction's OWN multi-line description
with the row-number/amount/balance line and the NEXT transaction's date line
in a way that isn't reliably re-separable after the fact (confirmed by
inspecting the raw ``extract_text()`` output against the real samples).
Word-level extraction (``page.extract_words()``), bucketed by each word's
``x0`` into the table's five known column bands (measured directly from the
real samples' header row coordinates), avoids this entirely — each column's
multi-line content stays column-pure regardless of how pdfplumber orders it
in the plain-text stream. ``page.extract_tables()`` was tried first (same
"check before assuming" discipline as BCA) and only picked up the two header
rows — no gridlines exist for the data rows in this statement's PDF, so table
detection doesn't see them as a table at all.

**Self-validation against the statement's own printed reconciliation
figures** — the same concrete, document-level "did extraction actually work"
discipline as ``bank_statement.py``'s BCA parser, adapted to what THIS
statement prints: Mandiri's e-Statement doesn't print CR/DB row counts the
way BCA does, but it prints richer *balance* data BCA doesn't (a running
Saldo (IDR) column per line, plus Saldo Awal/Saldo Akhir on top of Dana
Masuk/Dana Keluar) — so ``MandiriStatementParseResult.reconciles`` checks
THREE independent things, all of which must hold: (1) parsed credit total ==
printed Dana Masuk, (2) parsed debit total == printed Dana Keluar, (3) the
running balance implied by replaying every parsed line's signed amount,
starting from the printed Saldo Awal, lands exactly on the printed Saldo
Akhir — a strictly stronger check than BCA's, made possible by this
statement's own extra data, not weaker.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

from ingestion.bank_statement import BankStatementLine

_MONTH_EN = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Column x0 boundaries, measured directly from the real samples' header-row
# word coordinates (see module docstring) — each transaction's No / Tanggal /
# Keterangan / Nominal / Saldo cell always starts its words within one of
# these bands, confirmed against all 4 real monthly samples (May-Aug 2026).
_COL_NO = (10.0, 40.0)
_COL_DATE = (40.0, 115.0)
_COL_DESC = (115.0, 365.0)
_COL_AMOUNT = (365.0, 440.0)
_COL_BALANCE = (500.0, 580.0)

_DATE_LINE_RE = re.compile(r"^(\d{1,2}) ([A-Za-z]{3}) (\d{4})$")
_PERIODE_RE = re.compile(r"Periode/Period\s*:\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})")
_SALDO_AWAL_RE = re.compile(r"Saldo Awal/Initial Balance\s*:\s*([\d.]+,\d{2})")
_SALDO_AKHIR_RE = re.compile(r"Saldo Akhir/Closing Balance\s*:\s*([\d.]+,\d{2})")
_DANA_MASUK_RE = re.compile(r"Dana Masuk/Incoming Transactions\s*:\s*\+\s*([\d.]+,\d{2})")
_DANA_KELUAR_RE = re.compile(r"Dana Keluar/Outgoing Transactions\s*:\s*-\s*([\d.]+,\d{2})")

# Distinctive, unambiguous tokens used to bound each page's real transaction
# rows: "Remarks" (the English header's own last word, immediately before
# row 1) never appears in real transaction text; "berizin"/"Disclaimer"/
# "batas" (from the "PT Bank Mandiri... berizin dan diawasi..." legal
# footer, reprinted on every page, and the final "...batas akhir transaksi
# anda"/"Disclaimer" section on the last page) are likewise never part of a
# real Keterangan line. Never a top-coordinate guess/margin — an actual
# marker word's own position, confirmed against all 4 real samples.
_HEADER_END_MARKER = "Remarks"
_FOOTER_START_MARKERS = ("berizin", "Disclaimer", "batas")


def _parse_mandiri_amount(raw: str) -> Decimal:
    """'+66.928.398,00' / '-2.500,00' -> signed Decimal. Dot = thousands
    separator, comma = decimal separator (opposite of BCA's period-as
    -decimal convention) — confirmed against the real samples.
    """
    raw = raw.strip()
    sign = Decimal("1")
    if raw.startswith("-"):
        sign = Decimal("-1")
        raw = raw[1:]
    elif raw.startswith("+"):
        raw = raw[1:]
    raw = raw.replace(".", "").replace(",", ".")
    return sign * Decimal(raw)


@dataclass
class MandiriStatementParseResult:
    period_month: _dt.date | None
    opening_balance_idr: Decimal | None
    closing_balance_idr: Decimal | None
    lines: list[BankStatementLine] = field(default_factory=list)
    reported_dana_masuk: Decimal | None = None
    reported_dana_keluar: Decimal | None = None
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def reconciles(self) -> bool:
        """See module docstring — three independent checks, all required."""
        if (
            self.reported_dana_masuk is None
            or self.reported_dana_keluar is None
            or self.opening_balance_idr is None
            or self.closing_balance_idr is None
        ):
            return False
        credits = sum((l.amount_idr for l in self.lines if l.amount_idr > 0), Decimal("0"))
        debits = sum((-l.amount_idr for l in self.lines if l.amount_idr < 0), Decimal("0"))
        if credits != self.reported_dana_masuk or debits != self.reported_dana_keluar:
            return False
        running = self.opening_balance_idr
        for l in self.lines:
            running += l.amount_idr
        return running == self.closing_balance_idr


def extract_pdf_page_words(pdf_path_or_file) -> list[list[dict]]:
    """Thin wrapper around pdfplumber's word-level extraction, isolated to
    its own function (same rationale as bank_statement.py's
    ``extract_pdf_text_per_page``) — tests/callers that only care about the
    columnar-parsing LOGIC can pass already-extracted word lists instead of
    a real PDF.
    """
    import pdfplumber  # lazy import, same as bank_statement.py

    with pdfplumber.open(pdf_path_or_file) as pdf:
        return [page.extract_words(use_text_flow=False, keep_blank_chars=False) for page in pdf.pages]


def extract_pdf_text_per_page(pdf_path_or_file) -> list[str]:
    """Plain per-page text, used only for the header-level summary figures
    (Periode/Saldo Awal/Dana Masuk/Dana Keluar/Saldo Akhir) — those ARE
    reliably readable via plain extract_text() since they're single-line
    label:value pairs, not a multi-line table; only the transaction TABLE
    itself needs the columnar word-level approach (see module docstring).
    """
    import pdfplumber

    with pdfplumber.open(pdf_path_or_file) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def _page_transaction_lines(words: list[dict]) -> tuple[list[tuple[int, _dt.date | None, str, Decimal, Decimal]], list[str]]:
    """Parse one page's words into a list of
    (row_no, date, raw_description, amount_idr, balance_idr) tuples, bounded
    between the 'Remarks' header marker and the first footer marker (see
    module docstring). Returns (rows, warnings).
    """
    warnings: list[str] = []

    header_words = [w for w in words if w["text"] == _HEADER_END_MARKER]
    if not header_words:
        return [], []  # a page with no transaction table at all (e.g. a pure-footer trailing page)
    start_top = max(w["top"] for w in header_words)

    footer_tops = [w["top"] for w in words if w["text"] in _FOOTER_START_MARKERS and w["top"] > start_top]
    end_top = min(footer_tops) if footer_tops else float("inf")

    body_words = [w for w in words if start_top < w["top"] < end_top]

    anchors = sorted(
        {w["top"] for w in body_words if _COL_NO[0] <= w["x0"] < _COL_NO[1] and w["text"].isdigit()}
    )
    if not anchors:
        return [], []

    rows = []
    for i, anchor_top in enumerate(anchors):
        cluster_start = 0.0 if i == 0 else (anchors[i - 1] + anchor_top) / 2
        cluster_end = float("inf") if i == len(anchors) - 1 else (anchor_top + anchors[i + 1]) / 2
        cluster = [w for w in body_words if cluster_start <= w["top"] < cluster_end]

        no_words = [w for w in cluster if _COL_NO[0] <= w["x0"] < _COL_NO[1] and w["text"].isdigit()]
        row_no = int(no_words[0]["text"]) if no_words else i + 1

        date_lines: dict[float, list[str]] = {}
        for w in cluster:
            if _COL_DATE[0] <= w["x0"] < _COL_DATE[1]:
                date_lines.setdefault(round(w["top"], 1), []).append(w["text"])
        txn_date = None
        for _top, toks in sorted(date_lines.items()):
            m = _DATE_LINE_RE.match(" ".join(toks))
            if m:
                day, month_abbr, year = int(m.group(1)), m.group(2), int(m.group(3))
                month = _MONTH_EN.get(month_abbr)
                if month:
                    txn_date = _dt.date(year, month, day)
                    break
        if txn_date is None:
            warnings.append(f"Row {row_no}: could not find a parseable transaction date.")
            continue

        desc_lines: dict[float, list[tuple[float, str]]] = {}
        for w in cluster:
            if _COL_DESC[0] <= w["x0"] < _COL_DESC[1]:
                desc_lines.setdefault(round(w["top"], 1), []).append((w["x0"], w["text"]))
        raw_description = " / ".join(
            " ".join(tok for _x0, tok in sorted(toks)) for _top, toks in sorted(desc_lines.items())
        )
        if not raw_description:
            warnings.append(f"Row {row_no}: no Keterangan/description text found — skipped.")
            continue

        amount_words = [w for w in cluster if _COL_AMOUNT[0] <= w["x0"] < _COL_AMOUNT[1]]
        balance_words = [w for w in cluster if _COL_BALANCE[0] <= w["x0"] < _COL_BALANCE[1]]
        if not amount_words:
            warnings.append(f"Row {row_no} ({raw_description!r}): no Nominal amount found — skipped.")
            continue

        try:
            amount = _parse_mandiri_amount(amount_words[0]["text"])
            balance = _parse_mandiri_amount(balance_words[0]["text"]) if balance_words else None
        except Exception:  # noqa: BLE001 — a genuinely unparseable amount token
            warnings.append(f"Row {row_no} ({raw_description!r}): could not parse Nominal/Saldo as a number.")
            continue

        rows.append((row_no, txn_date, raw_description, amount, balance))

    return rows, warnings


def parse_mandiri_statement_pages(pages_text: list[str], pages_words: list[list[dict]]) -> MandiriStatementParseResult:
    """Parse already-extracted per-page text+words (see the two extractor
    functions above) into a structured result.
    """
    period_month = None
    opening_balance = closing_balance = None
    dana_masuk = dana_keluar = None
    for page_text in pages_text:
        if period_month is None:
            m = _PERIODE_RE.search(page_text)
            if m:
                day, month_abbr, year = int(m.group(1)), m.group(2), int(m.group(3))
                month = _MONTH_EN.get(month_abbr)
                if month:
                    period_month = _dt.date(year, month, 1)
        if opening_balance is None:
            m = _SALDO_AWAL_RE.search(page_text)
            if m:
                opening_balance = _parse_mandiri_amount(m.group(1))
        if closing_balance is None:
            m = _SALDO_AKHIR_RE.search(page_text)
            if m:
                closing_balance = _parse_mandiri_amount(m.group(1))
        if dana_masuk is None:
            m = _DANA_MASUK_RE.search(page_text)
            if m:
                dana_masuk = _parse_mandiri_amount(m.group(1))
        if dana_keluar is None:
            m = _DANA_KELUAR_RE.search(page_text)
            if m:
                dana_keluar = _parse_mandiri_amount(m.group(1))

    result = MandiriStatementParseResult(
        period_month=period_month,
        opening_balance_idr=opening_balance,
        closing_balance_idr=closing_balance,
        reported_dana_masuk=dana_masuk,
        reported_dana_keluar=dana_keluar,
    )
    if period_month is None:
        result.parse_warnings.append("Could not find 'Periode/Period : <date> - <date>' anywhere in the statement.")
    if opening_balance is None or closing_balance is None or dana_masuk is None or dana_keluar is None:
        result.parse_warnings.append(
            "Could not find one or more of Saldo Awal/Saldo Akhir/Dana Masuk/Dana Keluar in the "
            "statement's summary header — self-reconciliation check cannot run."
        )

    occurrence_counts: dict[tuple, int] = {}
    for words in pages_words:
        rows, warnings = _page_transaction_lines(words)
        result.parse_warnings.extend(warnings)
        for _row_no, txn_date, raw_description, amount, _balance in rows:
            key = (txn_date, raw_description, amount)
            occurrence_counts[key] = occurrence_counts.get(key, 0) + 1
            result.lines.append(
                BankStatementLine(
                    transaction_date=txn_date,
                    raw_description=raw_description,
                    amount_idr=amount,
                    occurrence_index=occurrence_counts[key],
                )
            )

    return result


def parse_mandiri_statement(pdf_path_or_file) -> MandiriStatementParseResult:
    """Convenience: extract + parse in one call. Note this reads the PDF
    TWICE (once for plain text, once for words) — both via pdfplumber's own
    internal caching of the parsed page object when given the same open
    file, this is cheap; kept as two calls rather than one combined
    extractor so ``extract_pdf_text_per_page``/``extract_pdf_page_words`` stay
    independently testable/reusable, same as bank_statement.py's split.
    """
    pages_text = extract_pdf_text_per_page(pdf_path_or_file)
    if hasattr(pdf_path_or_file, "seek"):
        pdf_path_or_file.seek(0)
    pages_words = extract_pdf_page_words(pdf_path_or_file)
    return parse_mandiri_statement_pages(pages_text, pages_words)
