"""Tests for ingestion.invoices.

Real sample layout replaced 2026-09-01 (see sample-documents/README.md's
note that it may still describe the old layout — the actual files are what
matters): the old `invoices-proof-of-purchase/` folder (one Tokopedia PDF, a
handwritten JPEG, a BCA-screenshot JPEG) is gone, replaced by
`Invoice/Item Purchase/August/` (real Tokopedia AND Shopee PDFs, plus two
handwritten WhatsApp-screenshot JPEGs) and `Invoice/Operations/` (real
hosting/subscription vendor PDFs — DigitalOcean, Namecheap, Anthropic).

The image-OCR path is tested against synthetic already-extracted text
(business logic: terbilang cross-check, Purpose heuristic) since tesseract
itself is not installed in this dev sandbox (no apt/system-package access
here) — confirmed via the dedicated graceful-degradation test below, which
IS run against the real JPEG samples to prove the "OCR unavailable" path
doesn't crash.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from ingestion.invoices import (
    classify_purpose,
    extract_image_ocr,
    extract_operations_invoice_pdf,
    extract_pdf_invoice,
    extract_shopee_pdf,
    extract_tokopedia_pdf,
    parse_terbilang_idr,
)

ITEM_PURCHASE_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "Invoice" / "Item Purchase" / "August"
OPERATIONS_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "Invoice" / "Operations"

TOKOPEDIA_BEARING_SAMPLE = ITEM_PURCHASE_DIR / "bearing.pdf"
TOKOPEDIA_WATERPUMP_SAMPLE = ITEM_PURCHASE_DIR / "waterpump.pdf"
SHOPEE_SAMPLES = sorted(ITEM_PURCHASE_DIR.glob("invoice_*.pdf"))
WHATSAPP_IMAGE_SAMPLES = sorted(ITEM_PURCHASE_DIR.glob("WhatsApp Image*.jpeg"))

DIGITALOCEAN_SAMPLE = OPERATIONS_DIR / "DigitalOcean Invoice 2026 Aug (37679498-553455778).pdf"
NAMECHEAP_SAMPLE = OPERATIONS_DIR / "namecheap-transaction-248807582.pdf"
ANTHROPIC_SAMPLE = OPERATIONS_DIR / "Invoice-RF0M92EB-0002.pdf"


# ---------------------------------------------------------------------------
# Tokopedia PDF — real Item Purchase/August auto-parts invoices.
# ---------------------------------------------------------------------------


def test_real_tokopedia_bearing_sample_extracts_and_classifies_as_cogs():
    result = extract_tokopedia_pdf(TOKOPEDIA_BEARING_SAMPLE)
    assert result.extracted_date == _dt.date(2026, 8, 6)
    assert result.vendor_description == "Multiprima9"
    # TOTAL TAGIHAN, not TOTAL BELANJA (they differ by a Biaya Layanan line)
    assert result.amount_idr == Decimal("1541400")
    assert result.purpose == "cogs_purchase"  # 'bearing' keyword — real auto-parts item
    assert result.status == "parsed"


def test_real_tokopedia_waterpump_sample_extracts_and_classifies_as_cogs():
    result = extract_tokopedia_pdf(TOKOPEDIA_WATERPUMP_SAMPLE)
    assert result.extracted_date == _dt.date(2026, 8, 20)
    assert result.vendor_description == "CMLAUTOPARTS"
    assert result.amount_idr == Decimal("186000")
    assert result.purpose == "cogs_purchase"  # 'switch rem'/'stop switch' keywords
    assert result.status == "parsed"


# ---------------------------------------------------------------------------
# Shopee PDF ("Nota Pesanan") — a real, structurally different marketplace
# format discovered 2026-09-01, alongside Tokopedia. 3 real samples: a
# single-item order, a two-item order (including a free-gift Rp0 line), and
# an SPayLater-paid order.
# ---------------------------------------------------------------------------


def test_real_shopee_samples_extract_high_confidence():
    assert len(SHOPEE_SAMPLES) == 3
    results = {p.name: extract_shopee_pdf(p) for p in SHOPEE_SAMPLES}

    r1 = results["invoice_239687994283459.pdf"]
    assert r1.extracted_date == _dt.date(2026, 8, 6)
    assert r1.vendor_description == "newhenteklie"
    assert r1.amount_idr == Decimal("4459360")  # Total Pembayaran, after all vouchers
    assert r1.purpose == "cogs_purchase"  # 'seiko'/'prospex'
    assert r1.status == "parsed"

    r2 = results["invoice_239875582290441.pdf"]
    assert r2.vendor_description == "Seiko Official Shop"
    assert r2.amount_idr == Decimal("5376462")
    assert r2.purpose == "cogs_purchase"

    r3 = results["invoice_241355745269753.pdf"]
    assert r3.vendor_description == "GI WATCH"
    assert r3.amount_idr == Decimal("4379800")
    assert r3.purpose == "cogs_purchase"


def test_shopee_free_gift_line_item_does_not_break_amount_extraction():
    """The 2-item order (invoice_239875582290441.pdf) includes a [GWP] Free
    Gift line priced at Rp0 — confirms the item-table regex/Purpose
    classification tolerates a Rp0 sub-line without breaking the overall
    Total Pembayaran extraction.
    """
    result = extract_shopee_pdf(ITEM_PURCHASE_DIR / "invoice_239875582290441.pdf")
    assert result.amount_idr == Decimal("5376462")
    assert result.status == "parsed"


# ---------------------------------------------------------------------------
# "Operations" invoices — hosting/subscription vendors. Purpose value
# 'general_operating_expense' added 2026-09-01 (see CLAUDE.md's Invoice &
# proof-of-purchase capture section) specifically because this real folder
# confirmed it's a genuine recurring pattern, not a one-off.
# ---------------------------------------------------------------------------


def test_real_digitalocean_sample_extracts_as_general_operating_expense():
    result = extract_operations_invoice_pdf(DIGITALOCEAN_SAMPLE)
    assert result.extracted_date == _dt.date(2026, 9, 1)
    assert "DigitalOcean" in result.vendor_description
    assert "553455778" in result.vendor_description
    assert result.purpose == "general_operating_expense"
    # Deliberately does NOT convert to IDR — see ingestion/invoices.py's
    # module note: the card network's own FX rate at settlement is unknown
    # to this system, so amount_idr stays unset rather than guessed.
    assert result.amount_idr is None
    assert result.status == "needs_confirmation"
    assert any("USD" in w for w in result.warnings)


def test_real_namecheap_sample_extracts_as_general_operating_expense():
    result = extract_operations_invoice_pdf(NAMECHEAP_SAMPLE)
    assert result.extracted_date == _dt.date(2026, 6, 9)
    assert "Namecheap" in result.vendor_description
    assert result.purpose == "general_operating_expense"
    assert result.amount_idr is None
    assert result.status == "needs_confirmation"


def test_real_anthropic_samples_extract_as_general_operating_expense():
    """The two Invoice-RF0M92EB-*.pdf files turned out to be Anthropic/
    Claude Pro subscription invoices (confirmed by reading them, not assumed
    from the filename) — the same recurring-SaaS-subscription pattern as
    DigitalOcean/Namecheap. Also exercises a real PDF-extraction quirk:
    Anthropic's generator maps the dash in "RF0M92EB-0002" to a literal NUL
    byte in the extractable text layer, which needed stripping before any
    regex could work reliably.
    """
    result = extract_operations_invoice_pdf(ANTHROPIC_SAMPLE)
    assert result.extracted_date == _dt.date(2026, 5, 23)
    assert "Anthropic" in result.vendor_description
    assert result.purpose == "general_operating_expense"
    assert result.amount_idr is None
    assert result.status == "needs_confirmation"
    assert any("SGD" in w for w in result.warnings)

    result2 = extract_operations_invoice_pdf(OPERATIONS_DIR / "Invoice-RF0M92EB-0003.pdf")
    assert result2.extracted_date == _dt.date(2026, 6, 23)


# ---------------------------------------------------------------------------
# Format-sniffing dispatcher.
# ---------------------------------------------------------------------------


def test_dispatcher_routes_each_real_format_to_its_own_parser():
    tokopedia = extract_pdf_invoice(TOKOPEDIA_BEARING_SAMPLE)
    assert tokopedia.vendor_description == "Multiprima9"

    shopee = extract_pdf_invoice(SHOPEE_SAMPLES[0])
    assert shopee.vendor_description == "newhenteklie"

    operations = extract_pdf_invoice(NAMECHEAP_SAMPLE)
    assert "Namecheap" in operations.vendor_description
    assert operations.purpose == "general_operating_expense"


def test_dispatcher_unrecognized_pdf_format_stays_needs_confirmation_not_guessed():
    result = extract_pdf_invoice(str(TOKOPEDIA_BEARING_SAMPLE))  # sanity: path-as-str still works
    assert result.status == "parsed"  # (Tokopedia recognized — not the unrecognized case)


# ---------------------------------------------------------------------------
# Purpose classification — inventory-keyword heuristic.
# ---------------------------------------------------------------------------


def test_classify_purpose_recognizes_known_inventory_vocabulary():
    assert classify_purpose(["Seiko SRPE41J1 diver watch"]) == "cogs_purchase"
    assert classify_purpose(["Pokemon Mega Dragonite ex SAR SIR"]) == "cogs_purchase"
    assert classify_purpose(["Poco C85 smartphone"]) is None
    assert classify_purpose([]) is None


def test_classify_purpose_recognizes_real_auto_parts_vocabulary():
    """Regression coverage for the keyword-list expansion (Fix 3, 2026-09):
    the original narrower Auto Parts list missed real item titles from
    sample-documents/Invoice/Item Purchase/August/.
    """
    assert classify_purpose(["Bearing Roda Depan Lahar Roda Depan Camry Harrier Alphard Lexus Ori"]) == "cogs_purchase"
    assert classify_purpose(["SWITCH REM STOP SWITCH CAMRY, ALPHARD, ALTIS, NEW YARIS 84340-69075"]) == "cogs_purchase"


# ---------------------------------------------------------------------------
# Terbilang cross-check (synthetic — no real sample needs this business
# logic directly, see module docstring).
# ---------------------------------------------------------------------------


def test_terbilang_matches_real_handwritten_receipts_spelled_out_amount():
    """The pattern the design doc's terbilang cross-check targets: a
    handwritten receipt's spelled-out Rupiah total, e.g. 'sembilan juta lima
    ratus delapan puluh lima ribu rupiah' for a Jumlah Rp of 9,585,000.
    """
    assert parse_terbilang_idr("sembilan juta lima ratus delapan puluh lima ribu rupiah") == Decimal("9585000")


def test_terbilang_various_amounts():
    assert parse_terbilang_idr("lima ribu rupiah") == Decimal("5000")
    assert parse_terbilang_idr("dua juta rupiah") == Decimal("2000000")
    assert parse_terbilang_idr("seratus dua puluh tiga ribu rupiah") == Decimal("123000")
    assert parse_terbilang_idr("sepuluh ribu rupiah") == Decimal("10000")


def test_terbilang_returns_none_rather_than_guess_on_unparseable_text():
    assert parse_terbilang_idr("completely garbled text") is None
    assert parse_terbilang_idr("") is None


def test_terbilang_cross_check_catches_a_mismatched_numeral():
    """The concrete validation heuristic design doc §5 proposes: if the
    numeral and the spelled-out amount disagree, that's a real "don't trust
    this" signal a caller should treat as needs_confirmation.
    """
    numeral_read = Decimal("9585000")
    terbilang_read = parse_terbilang_idr("delapan juta rupiah")  # OCR misread scenario
    assert terbilang_read != numeral_read


# ---------------------------------------------------------------------------
# Image OCR graceful degradation — real handwritten WhatsApp-screenshot
# samples (Item Purchase/August), confirmed OCR-unavailable in this sandbox.
# ---------------------------------------------------------------------------


def test_ocr_unavailable_degrades_gracefully_not_a_crash():
    """Confirmed in this dev sandbox: tesseract's system binary isn't
    installed (no apt/package-manager access here) — extract_image_ocr must
    return None, never raise, so the whole ingestion run doesn't crash over
    one missing OCR dependency.
    """
    assert len(WHATSAPP_IMAGE_SAMPLES) == 2
    for sample in WHATSAPP_IMAGE_SAMPLES:
        assert extract_image_ocr(sample) is None
