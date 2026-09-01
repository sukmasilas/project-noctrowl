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
    # "bearing"/"switch rem"/"stop switch" added 2026-09 against real August
    # invoices (sample-documents/Invoice/Item Purchase/August/bearing.pdf,
    # waterpump.pdf) — real auto-parts item titles ("Bearing Roda Depan...",
    # "SWITCH REM STOP SWITCH CAMRY...") that the original narrower list
    # didn't recognize, which would have wrongly left a genuine COGS
    # purchase's Purpose unset. Deliberately still narrow/specific (not a
    # bare "switch", which would false-positive on unrelated electronics).
    "Auto Parts": ["sparepart", "spare part", "onderdil", "aki", "ban", "oli", "bearing", "switch rem", "stop switch"],
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
# Shopee-style born-digital PDF ("Nota Pesanan") — a genuinely different
# real invoice format discovered 2026-09 in
# sample-documents/Invoice/Item Purchase/August/ (3 real samples: a
# single-item order, a two-item order including a free-gift Rp0 line, and an
# SPayLater-paid order). Label-based extraction, same confidence tier as
# Tokopedia — every figure is explicitly labeled on its own line/row, not
# OCR guesswork. NOT forced through the Tokopedia regexes (different labels
# entirely: "Nama Penjual:"/"Tanggal Transaksi"/"Total Pembayaran" vs.
# Tokopedia's "Penjual :"/"Tanggal Pembelian :"/"TOTAL TAGIHAN") — a fresh,
# format-specific parser, same design principle as the Mandiri statement
# parser needing its own module rather than being squeezed into BCA's shape.
# ---------------------------------------------------------------------------

_SHOPEE_VENDOR_RE = re.compile(r"Nama Penjual:\s*(.+)")
_SHOPEE_DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_SHOPEE_TOTAL_RE = re.compile(r"Total Pembayaran\s*Rp([\d.]+)")
_SHOPEE_ITEMS_RE = re.compile(r"Rincian Pesanan\n(.*?)\nSubtotal Rp", re.DOTALL)


def parse_shopee_text(text: str) -> InvoiceExtraction:
    warnings: list[str] = []

    vendor_m = _SHOPEE_VENDOR_RE.search(text)
    vendor = vendor_m.group(1).strip() if vendor_m else None
    if vendor is None:
        warnings.append("Could not find 'Nama Penjual:' label")

    date_m = _SHOPEE_DATE_RE.search(text)
    extracted_date = None
    if date_m:
        day, month, year = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
        try:
            extracted_date = _dt.date(year, month, day)
        except ValueError:
            warnings.append(f"'Tanggal Transaksi' date {date_m.group(0)!r} is not a valid DD/MM/YYYY date")
    else:
        warnings.append("Could not find a 'Tanggal Transaksi' (DD/MM/YYYY) date")

    # Total Pembayaran ("what was actually charged", after all vouchers/
    # promo discounts/service fees) — the Shopee analog of Tokopedia's TOTAL
    # TAGIHAN: what should reconcile against the actual paying bank
    # transaction, not the pre-discount Subtotal Pesanan.
    amount_m = _SHOPEE_TOTAL_RE.search(text)
    amount = _parse_idr_amount(amount_m.group(1)) if amount_m else None
    if amount is None:
        warnings.append("Could not find 'Total Pembayaran' label")

    items_m = _SHOPEE_ITEMS_RE.search(text)
    item_descriptions = [items_m.group(1).strip()] if items_m else []
    purpose = classify_purpose(item_descriptions) if item_descriptions else None
    if purpose is None:
        warnings.append(
            "Could not confidently classify Purpose from item content — staying unset "
            "(never assumed COGS just because it's in the invoices folder)."
        )

    status = "parsed" if (extracted_date and vendor and amount and not warnings) else "needs_confirmation"

    return InvoiceExtraction(
        extracted_date=extracted_date,
        vendor_description=vendor,
        amount_idr=amount,
        purpose=purpose,
        status=status,
        ocr_raw_text=text,
        warnings=warnings,
    )


def extract_shopee_pdf(pdf_path_or_file) -> InvoiceExtraction:
    import pdfplumber  # lazy import, milestone-3-only dependency

    with pdfplumber.open(pdf_path_or_file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_shopee_text(text)


# ---------------------------------------------------------------------------
# "Operations" invoices — hosting/subscription vendors (DigitalOcean,
# Namecheap, and, per Main-agent's brief, whatever the two
# Invoice-RF0M92EB-*.pdf files turn out to be — confirmed by reading them to
# be Anthropic/Claude Pro subscription invoices, the same recurring-SaaS-
# subscription pattern as DigitalOcean/Namecheap, not a one-off). Added
# 2026-09 alongside Purpose's new 'general_operating_expense' value.
#
# **Deliberately does NOT convert the charged amount to IDR.** Every one of
# these real samples is denominated in a foreign currency (DigitalOcean/
# Namecheap in USD, Anthropic in SGD — confirmed by reading each PDF, not
# assumed from the filename), charged to a credit card, not paid via a
# Payoneer/eBay-USD flow this system already has an FX rate for. The actual
# IDR amount that lands on the BCA Main statement is set by the card
# network's own FX rate at settlement, which this system has no way to know
# from the invoice alone — inventing an IDR figure (e.g. via Kurs Pajak,
# which CLAUDE.md scopes to eBay sale booking and which the Payoneer
# ingestion module already reuses for OTHER Payoneer-wallet USD amounts,
# but NOT for a card-network FX conversion this system has no visibility
# into) would produce a confident-looking but likely-wrong number that
# probably wouldn't even match the real bank line within review-queue rule
# (b)'s tolerance. Left amount_idr=None / status='needs_confirmation'
# instead — same "never guess a money figure" principle as everywhere else
# in this pipeline — so a human fills in the real IDR amount from the
# bank/card statement. Flagged to Main-agent as a design note, not an open
# blocker: this doesn't change any existing behavior, it only decides how a
# NEW invoice class degrades safely.
# ---------------------------------------------------------------------------

_OPERATIONS_VENDOR_PATTERNS: dict[str, dict] = {
    "DigitalOcean": {
        "marker": re.compile(r"DigitalOcean", re.IGNORECASE),
        "date": re.compile(r"Date of issue\s*:?\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})"),
        "amount": re.compile(r"Total due\s*\$\s*([\d,]+\.\d{2})"),
        "reference": re.compile(r"Invoice number\s*:?\s*(\S+)"),
        "currency": "USD",
    },
    "Namecheap": {
        "marker": re.compile(r"Namecheap", re.IGNORECASE),
        "date": re.compile(r"Transaction Date\s*:?\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})"),
        "amount": re.compile(r"Charge Amount\s*:?\s*\$\s*([\d,]+\.\d{2})"),
        "reference": re.compile(r"Transaction Id\s*:?\s*(\S+)"),
        "currency": "USD",
    },
    "Anthropic": {
        "marker": re.compile(r"Anthropic", re.IGNORECASE),
        # Anthropic's own PDF generator drops some punctuation glyphs from
        # the extractable text layer (confirmed against the real samples —
        # "Invoice number RF0M92EB 0002" and "Date of issue May 23, 2026"
        # both come out with no colon/dash at all, unlike DigitalOcean's
        # equivalent labels) — patterns here tolerate that rather than
        # assuming punctuation will be present.
        "date": re.compile(r"Date of issue\s*:?\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})"),
        "amount": re.compile(r"Amount due\s*S\$\s*([\d,]+\.\d{2})"),
        # Captures to end-of-line rather than "up to 2 tokens" (unlike the
        # other two vendors' single-token invoice numbers) — confirmed
        # necessary against the real sample: with the NUL-byte artifact
        # above stripped, "RF0M92EB 0002" collapses into one token, and a
        # generic "up to 2 tokens" pattern would then wrongly swallow the
        # next line's leading word ("Date...") as a phantom second token.
        "reference": re.compile(r"Invoice number\s*:?\s*([^\n]+)"),
        "currency": "SGD",
    },
}


def parse_operations_invoice_text(text: str) -> InvoiceExtraction:
    warnings: list[str] = []

    # Confirmed against the real Anthropic samples: their PDF generator's
    # font/cmap maps at least one punctuation glyph (the dash in
    # "RF0M92EB-0002") to a literal NUL codepoint (U+0000) rather than
    # dropping it or mapping it to a real dash — pdfplumber faithfully
    # extracts that NUL byte as-is. Left in place, it defeats \S+/\s+-based
    # regexes (a NUL is neither whitespace nor a normal token-separator), so
    # it's stripped here as a generic PDF-extraction-artifact cleanup, not a
    # content guess — same category of "born-digital but not entirely clean
    # text" issue as this module already handles for other formats.
    text = text.replace("\x00", "")

    vendor_key = next((name for name, cfg in _OPERATIONS_VENDOR_PATTERNS.items() if cfg["marker"].search(text)), None)
    if vendor_key is None:
        return InvoiceExtraction(
            extracted_date=None,
            vendor_description=None,
            amount_idr=None,
            purpose=None,
            status="needs_confirmation",
            ocr_raw_text=text,
            warnings=["Unrecognized hosting/subscription vendor — no known Operations-invoice pattern matched."],
        )

    cfg = _OPERATIONS_VENDOR_PATTERNS[vendor_key]

    extracted_date = None
    date_m = cfg["date"].search(text)
    if date_m:
        raw_date = re.sub(r"\s+", " ", date_m.group(1)).strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                extracted_date = _dt.datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        if extracted_date is None:
            warnings.append(f"Could not parse {vendor_key} invoice date text {raw_date!r}")
    else:
        warnings.append(f"Could not find an invoice date for a {vendor_key} invoice")

    amount_m = cfg["amount"].search(text)
    native_amount = Decimal(amount_m.group(1).replace(",", "")) if amount_m else None
    if native_amount is None:
        warnings.append(f"Could not find the charged amount for a {vendor_key} invoice")

    ref_m = cfg["reference"].search(text)
    reference = ref_m.group(1).strip() if ref_m else None

    currency = cfg["currency"]
    vendor_bits = [vendor_key]
    if reference:
        vendor_bits.append(reference)
    if native_amount is not None:
        vendor_bits.append(f"{native_amount} {currency}")
    vendor_description = " — ".join(vendor_bits)

    warnings.append(
        f"Amount is {currency}, not IDR (the card network's own FX rate at settlement is not known to this "
        "system) — needs manual entry of the actual IDR amount charged, read off the bank/card statement."
    )

    return InvoiceExtraction(
        extracted_date=extracted_date,
        vendor_description=vendor_description,
        amount_idr=None,
        purpose="general_operating_expense",
        status="needs_confirmation",
        ocr_raw_text=text,
        warnings=warnings,
    )


def extract_operations_invoice_pdf(pdf_path_or_file) -> InvoiceExtraction:
    import pdfplumber  # lazy import, milestone-3-only dependency

    with pdfplumber.open(pdf_path_or_file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_operations_invoice_text(text)


# ---------------------------------------------------------------------------
# Format-sniffing dispatcher — the single entrypoint ingestion.sync should
# call for any PDF invoice, so it doesn't need to hardcode an assumption
# about which of the (now three) known PDF formats a given upload is. Sniffs
# on content, not filename, since Drive filenames aren't a format
# guarantee (see e.g. "waterpump.pdf" actually containing an unrelated
# brake-switch item — a filename that describes the PURCHASE, not the
# document FORMAT).
# ---------------------------------------------------------------------------


def extract_pdf_invoice(pdf_path_or_file) -> InvoiceExtraction:
    import pdfplumber  # lazy import, milestone-3-only dependency

    with pdfplumber.open(pdf_path_or_file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    lowered = text.lower()
    if "tokopedia" in lowered:
        return parse_tokopedia_text(text)
    if "shopee" in lowered:
        return parse_shopee_text(text)
    if any(cfg["marker"].search(text) for cfg in _OPERATIONS_VENDOR_PATTERNS.values()):
        return parse_operations_invoice_text(text)

    return InvoiceExtraction(
        extracted_date=None,
        vendor_description=None,
        amount_idr=None,
        purpose=None,
        status="needs_confirmation",
        ocr_raw_text=text,
        warnings=["Unrecognized PDF invoice format — no known marketplace/vendor pattern matched; needs manual entry."],
    )


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
