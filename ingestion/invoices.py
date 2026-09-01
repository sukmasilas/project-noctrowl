"""Invoice / proof-of-purchase extraction.

Implements docs/design/milestone-3-ingestion-design.md §5. Three real
samples, honestly different reliability:
- Tokopedia PDF: born-digital, label-based extraction — high confidence.
- Handwritten shop receipt (JPEG): needs real OCR (pytesseract); expect
  Needs Confirmation as the COMMON case per CLAUDE.md's own text.
- BCA transfer screenshot (JPEG): also needs OCR, but clean printed app-UI
  text — should OCR well despite being an image (the reliability driver is
  print-vs-handwriting, not PDF-vs-image format — see design doc §5).

This module never blocks on tesseract being installed: if the OCR backend
is unavailable (confirmed to be the case in this dev sandbox — no
apt/system-package access here), extraction degrades to
status='needs_confirmation' with a clear warning rather than crashing the
whole ingestion run. The text-parsing/classification LOGIC below (date/
amount extraction, the terbilang cross-check, the Purpose heuristic) is
built and tested independently of whether OCR itself succeeded, by testing
it against synthetic already-extracted text — see tests/ingestion/
test_invoices.py.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

_MONTH_ID = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
}

_TANGGAL_RE = re.compile(r"Tanggal Pembelian\s*:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_PENJUAL_RE = re.compile(r"Penjual\s*:\s*(.+?)\s+Pembeli")
_TOTAL_TAGIHAN_RE = re.compile(r"TOTAL TAGIHAN\s*Rp([\d.]+)")
_PRODUCT_SECTION_RE = re.compile(r"INFO PRODUK.*?TOTAL HARGA\n(.*?)\nSUBTOTAL", re.DOTALL)

# Known-inventory-vocabulary keyword lists — deliberately narrow (see design
# doc §5: Purpose only auto-fills when the document's own content actually
# looks like one of this business's real product categories, never assumed
# by default just because a document landed in the invoices folder).
_INVENTORY_KEYWORDS = {
    "TCG": ["pokemon", "tcg", "sar", "sir", "card", "kartu", "one piece", "op15", "weiss schwarz"],
    "Watches": ["seiko", "rolex", "arloji", "watch", "jam tangan", "prospex", "diver"],
    "Auto Parts": ["sparepart", "spare part", "onderdil", "aki", "ban", "oli"],
    "Toys & Collectibles": ["barbie", "hot wheels", "doll", "mainan", "action figure"],
}


def _parse_idr_amount(raw: str) -> Decimal:
    return Decimal(raw.replace(".", ""))


@dataclass
class InvoiceExtraction:
    extracted_date: _dt.date | None
    vendor_description: str | None
    amount_idr: Decimal | None
    purpose: str | None
    status: str  # 'parsed' | 'needs_confirmation'
    ocr_raw_text: str | None
    ocr_confidence: Decimal | None = None
    warnings: list[str] = field(default_factory=list)


def classify_purpose(item_descriptions: list[str]) -> str | None:
    """Purpose auto-fills to 'cogs_purchase' ONLY when the extracted
    itemized content matches known inventory vocabulary — never a blanket
    "every invoice = COGS" assumption (see design doc §5's Tokopedia
    phone-purchase counter-example). Returns None (forcing
    needs_confirmation) otherwise, including for a bare proof-of-transfer
    with no itemized content at all.
    """
    combined = " ".join(item_descriptions).lower()
    for keywords in _INVENTORY_KEYWORDS.values():
        if any(kw in combined for kw in keywords):
            return "cogs_purchase"
    return None


# ---------------------------------------------------------------------------
# Tokopedia-style born-digital PDF — label-based, high confidence.
# ---------------------------------------------------------------------------


def parse_tokopedia_text(text: str) -> InvoiceExtraction:
    warnings: list[str] = []

    date_m = _TANGGAL_RE.search(text)
    extracted_date = None
    if date_m:
        day, month_name, year = int(date_m.group(1)), date_m.group(2).lower(), int(date_m.group(3))
        month = _MONTH_ID.get(month_name)
        if month:
            extracted_date = _dt.date(year, month, day)
        else:
            warnings.append(f"Unrecognized Indonesian month name {month_name!r}")
    else:
        warnings.append("Could not find 'Tanggal Pembelian' label")

    vendor_m = _PENJUAL_RE.search(text)
    vendor = vendor_m.group(1).strip() if vendor_m else None
    if vendor is None:
        warnings.append("Could not find 'Penjual' label")

    # TOTAL TAGIHAN ("what you actually owe/pay") — not TOTAL BELANJA, which
    # excludes a small "Biaya Layanan" service-fee line; TOTAL TAGIHAN is
    # what should reconcile against the actual paying bank transaction. See
    # design doc §5.
    amount_m = _TOTAL_TAGIHAN_RE.search(text)
    amount = _parse_idr_amount(amount_m.group(1)) if amount_m else None
    if amount is None:
        warnings.append("Could not find 'TOTAL TAGIHAN' label")

    product_m = _PRODUCT_SECTION_RE.search(text)
    item_descriptions = [product_m.group(1).strip()] if product_m else []
    purpose = classify_purpose(item_descriptions) if item_descriptions else None
    if purpose is None:
        warnings.append(
            "Could not confidently classify Purpose from item content — staying unset "
            "(see design doc §5: never assumed COGS just because it's in the invoices folder)."
        )

    status = "parsed" if (extracted_date and vendor and amount and not warnings) else "needs_confirmation"
    # Purpose being unset alone doesn't force needs_confirmation for the
    # OTHER fields' confidence — but if warnings exist for any reason
    # (including the purpose one above), we're conservative and mark the
    # whole record for a human look, per design doc §5's "any required
    # field low-confidence -> needs_confirmation" rule (extended here to
    # include a fully-unclassifiable purpose, which needs a human decision
    # regardless of how well Date/Vendor/Amount extracted).

    return InvoiceExtraction(
        extracted_date=extracted_date,
        vendor_description=vendor,
        amount_idr=amount,
        purpose=purpose,
        status=status,
        ocr_raw_text=text,
        warnings=warnings,
    )


def extract_tokopedia_pdf(pdf_path_or_file) -> InvoiceExtraction:
    import pdfplumber  # lazy import, milestone-3-only dependency

    with pdfplumber.open(pdf_path_or_file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_tokopedia_text(text)


# ---------------------------------------------------------------------------
# Indonesian spelled-out amount ("terbilang") parser — a cross-check for the
# handwritten receipt's numeral vs. spelled-out total. Deliberately scoped
# to what the real sample actually needs (up to hundreds of millions), not a
# fully general Indonesian numeral-word parser.
# ---------------------------------------------------------------------------

_ONES = {
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7, "delapan": 8, "sembilan": 9,
}
_TEENS = {
    "sepuluh": 10, "sebelas": 11, "dua belas": 12, "tiga belas": 13, "empat belas": 14, "lima belas": 15,
    "enam belas": 16, "tujuh belas": 17, "delapan belas": 18, "sembilan belas": 19,
}


def parse_terbilang_idr(text: str) -> Decimal | None:
    """Parse an Indonesian spelled-out Rupiah amount, e.g. 'sembilan juta
    lima ratus delapan puluh lima ribu rupiah' -> Decimal('9585000'). Returns
    None if the text doesn't parse cleanly — never a best-guess partial
    number (a cross-check that silently gets it wrong is worse than no
    cross-check at all).
    """
    cleaned = text.lower().strip()
    cleaned = re.sub(r"[^a-z\s]", " ", cleaned)
    cleaned = re.sub(r"\brupiah\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    def parse_hundreds_group(words: list[str]) -> int | None:
        """Parse a 1-999 group (e.g. 'sembilan ratus' or 'delapan puluh
        lima' or 'seratus dua puluh tiga')."""
        if not words:
            return 0
        total = 0
        i = 0
        if words[i] == "seratus":
            total += 100
            i += 1
        elif i + 1 < len(words) and words[i + 1] == "ratus":
            if words[i] not in _ONES:
                return None
            total += _ONES[words[i]] * 100
            i += 2
        remaining = " ".join(words[i:])
        if not remaining:
            return total
        if remaining == "sepuluh" or remaining in _TEENS:
            total += _TEENS.get(remaining, 10)
            return total
        rem_words = remaining.split()
        if rem_words[0] == "sepuluh":
            total += 10
            rem_words = rem_words[1:]
            if rem_words:
                return None
            return total
        if len(rem_words) >= 2 and rem_words[1] == "puluh":
            if rem_words[0] not in _ONES:
                return None
            total += _ONES[rem_words[0]] * 10
            rem_words = rem_words[2:]
        if rem_words:
            if len(rem_words) == 1 and rem_words[0] in _ONES:
                total += _ONES[rem_words[0]]
                rem_words = []
            else:
                return None
        return total

    words = cleaned.split()
    # Split on 'juta' and 'ribu' scale words, left-to-right.
    remainder = words
    juta_value = 0
    if "juta" in remainder:
        idx = remainder.index("juta")
        group = parse_hundreds_group(remainder[:idx])
        if group is None:
            return None
        juta_value = group
        remainder = remainder[idx + 1 :]

    ribu_value = 0
    if "ribu" in remainder:
        idx = remainder.index("ribu")
        group_words = remainder[:idx]
        if group_words == ["se"] or group_words == []:
            ribu_value = 1
        else:
            group = parse_hundreds_group(group_words)
            if group is None:
                return None
            ribu_value = group
        remainder = remainder[idx + 1 :]

    trailing_value = 0
    if remainder:
        group = parse_hundreds_group(remainder)
        if group is None:
            return None
        trailing_value = group

    total = Decimal(juta_value) * 1_000_000 + Decimal(ribu_value) * 1_000 + Decimal(trailing_value)
    return total


# ---------------------------------------------------------------------------
# Image OCR (handwritten receipt, transfer screenshot) via pytesseract.
# ---------------------------------------------------------------------------


class OcrUnavailableError(Exception):
    """Raised (and caught by extract_image_ocr) when the tesseract binary
    itself isn't installed — a deployment/environment issue, not a per
    -document extraction failure. Distinguished so callers can surface a
    clearer message than a generic parse failure.
    """


def extract_image_ocr(image_path) -> str | None:
    """Returns the raw OCR'd text, or None if the OCR backend is
    unavailable (never raises out to the caller — the whole point is that
    one bad/unavailable OCR backend must not crash the ingestion run).
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None

    try:
        return pytesseract.image_to_string(Image.open(image_path))
    except Exception:  # pytesseract.TesseractNotFoundError, or any decode failure
        return None
