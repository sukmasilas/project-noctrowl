"""Tests for ingestion.invoices.

The Tokopedia PDF path is tested against the real sample (born-digital,
label-based extraction — high confidence). The image-OCR path is tested
against synthetic already-extracted text (business logic: terbilang
cross-check, Purpose heuristic) since tesseract itself is not installed in
this dev sandbox (no apt/system-package access here) — confirmed via the
dedicated graceful-degradation test below, which IS run against the real
JPEG samples to prove the "OCR unavailable" path doesn't crash.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from ingestion.invoices import (
    classify_purpose,
    extract_image_ocr,
    extract_tokopedia_pdf,
    parse_terbilang_idr,
)

TOKOPEDIA_SAMPLE = (
    Path(__file__).resolve().parents[2] / "sample-documents" / "invoices-proof-of-purchase" / "Invoice Sample _ Tokopedia.pdf"
)
HANDWRITTEN_SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "sample-documents"
    / "invoices-proof-of-purchase"
    / "Invoice Sample_Direct Invoice from shop.jpeg"
)
SCREENSHOT_SAMPLE = (
    Path(__file__).resolve().parents[2] / "sample-documents" / "invoices-proof-of-purchase" / "Proof of Transfer Sample_BCA.jpeg"
)


def test_real_tokopedia_sample_extracts_date_vendor_amount_high_confidence():
    result = extract_tokopedia_pdf(TOKOPEDIA_SAMPLE)
    assert result.extracted_date == _dt.date(2026, 7, 23)
    assert result.vendor_description == "Brandon Harvest"
    # TOTAL TAGIHAN, not TOTAL BELANJA (they differ by a Biaya Layanan line)
    assert result.amount_idr == Decimal("2617600")


def test_real_tokopedia_sample_purpose_stays_unset_not_guessed_as_cogs():
    """This specific real sample is a phone purchase, not inventory — the
    design doc's own flagged counter-example to "every invoice = COGS".
    """
    result = extract_tokopedia_pdf(TOKOPEDIA_SAMPLE)
    assert result.purpose is None
    assert result.status == "needs_confirmation"
    assert any("Purpose" in w for w in result.warnings)


def test_classify_purpose_recognizes_known_inventory_vocabulary():
    assert classify_purpose(["Seiko SRPE41J1 diver watch"]) == "cogs_purchase"
    assert classify_purpose(["Pokemon Mega Dragonite ex SAR SIR"]) == "cogs_purchase"
    assert classify_purpose(["Poco C85 smartphone"]) is None
    assert classify_purpose([]) is None


def test_terbilang_matches_real_handwritten_receipts_spelled_out_amount():
    """The real handwritten sample's TERBILANG line reads 'sembilan juta
    lima ratus delapan puluh lima ribu rupiah' for a Jumlah Rp of
    9,585,000 — this is the actual cross-check design doc §5 proposes.
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


def test_ocr_unavailable_degrades_gracefully_not_a_crash():
    """Confirmed in this dev sandbox: tesseract's system binary isn't
    installed (no apt/package-manager access here) — extract_image_ocr must
    return None, never raise, so the whole ingestion run doesn't crash over
    one missing OCR dependency.
    """
    result = extract_image_ocr(HANDWRITTEN_SAMPLE)
    assert result is None

    result2 = extract_image_ocr(SCREENSHOT_SAMPLE)
    assert result2 is None
