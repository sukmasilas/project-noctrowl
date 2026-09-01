"""Tests for ingestion.mandiri_statement against the real Bridging Account
(Mandiri) e-Statement samples — May/Jun/Jul/Aug 2026, per Main-agent's Fix 1
brief (2026-09-01 follow-up to the already-QA-approved milestone 3).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from ingestion.mandiri_statement import (
    extract_pdf_page_words,
    extract_pdf_text_per_page,
    parse_mandiri_statement,
    parse_mandiri_statement_pages,
)

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "Bridging Account (Mandiri)"
MAY_SAMPLE = SAMPLES_DIR / "e-Statement_XXXXXXXXX7498_01 Mei 2026-31 Mei 2026_unlocked.pdf"
JUN_SAMPLE = SAMPLES_DIR / "e-Statement_XXXXXXXXX7498_01 Jun 2026-30 Jun 2026_unlocked.pdf"
JUL_SAMPLE = SAMPLES_DIR / "e-Statement_XXXXXXXXX7498_01 Jul 2026-31 Jul 2026-unlocked.pdf"
AUG_SAMPLE = SAMPLES_DIR / "e-Statement_XXXXXXXXX7498_01 Agu 2026-31 Agu 2026_unlocked.pdf"


def test_may_sample_reconciles_against_statements_own_printed_totals():
    result = parse_mandiri_statement(MAY_SAMPLE)

    assert result.period_month == _dt.date(2026, 5, 1)
    assert result.opening_balance_idr == Decimal("66004187.57")
    assert result.closing_balance_idr == Decimal("30166.15")
    assert result.reported_dana_masuk == Decimal("219928111.98")
    assert result.reported_dana_keluar == Decimal("285902133.40")
    assert result.parse_warnings == []
    assert result.reconciles is True
    assert len(result.lines) == 15


def test_all_four_real_monthly_samples_reconcile_cleanly():
    """The strongest correctness signal available, same discipline as the
    BCA parser's own real-sample test: every one of the 4 real months
    reconciles against ITS OWN printed Dana Masuk/Dana Keluar/Saldo Awal/
    Saldo Akhir figures, with zero parse warnings.
    """
    for sample in (MAY_SAMPLE, JUN_SAMPLE, JUL_SAMPLE, AUG_SAMPLE):
        result = parse_mandiri_statement(sample)
        assert result.parse_warnings == [], f"{sample.name}: {result.parse_warnings}"
        assert result.reconciles is True, f"{sample.name} did not reconcile"
        assert len(result.lines) > 0


def test_closing_balance_chains_month_to_month_across_real_samples():
    """An independent cross-file correctness signal beyond each statement's
    own self-check: May's Saldo Akhir must equal June's Saldo Awal, and so
    on — these are 4 consecutive real months of the same account.
    """
    may = parse_mandiri_statement(MAY_SAMPLE)
    jun = parse_mandiri_statement(JUN_SAMPLE)
    jul = parse_mandiri_statement(JUL_SAMPLE)
    aug = parse_mandiri_statement(AUG_SAMPLE)

    assert may.closing_balance_idr == jun.opening_balance_idr
    assert jun.closing_balance_idr == jul.opening_balance_idr
    assert jul.closing_balance_idr == aug.opening_balance_idr


def test_every_line_has_nonzero_amount_and_matches_period_month():
    result = parse_mandiri_statement(MAY_SAMPLE)
    for line in result.lines:
        assert line.amount_idr != 0
        assert line.transaction_date.year == 2026
        assert line.transaction_date.month == 5
        assert line.raw_description  # never blank


def test_signed_amounts_and_multiline_descriptions_extracted_correctly():
    """Spot-checks against the real May sample's own printed figures —
    confirms the explicit +/- sign convention (not a DB-suffix convention
    like BCA) and that a 4-line wrapped Keterangan cell (e.g. the JPMORGAN
    withdrawal-landing line) is reconstructed as one raw_description, not
    split or interleaved with the next row's date (see module docstring's
    explanation of why word-level column bucketing was needed).
    """
    result = parse_mandiri_statement(MAY_SAMPLE)
    by_amount = {l.amount_idr: l for l in result.lines}

    inflow = by_amount[Decimal("66928398.00")]
    assert inflow.transaction_date == _dt.date(2026, 5, 14)
    assert "JPMORGAN" in inflow.raw_description
    assert "Withdrawal To Bank" in inflow.raw_description

    fee = by_amount[Decimal("-2500.00")]
    assert fee.raw_description == "Biaya transfer BI Fast"

    outflow = by_amount[Decimal("-65950000.00")]
    assert "Ke BCA" in outflow.raw_description
    assert "DENNY WIJAYA 1790345891" in outflow.raw_description

    interest = by_amount[Decimal("666.98")]
    assert interest.raw_description == "Bunga rekening"


def test_reprocessing_same_pages_produces_identical_occurrence_indices():
    """Row-creation idempotency building block, same discipline as the BCA
    parser's equivalent test — re-parsing the exact same extracted
    text/words must produce the exact same sequence of dedup keys.
    """
    pages_text = extract_pdf_text_per_page(MAY_SAMPLE)
    pages_words = extract_pdf_page_words(MAY_SAMPLE)
    r1 = parse_mandiri_statement_pages(pages_text, pages_words)
    r2 = parse_mandiri_statement_pages(pages_text, pages_words)

    keys1 = [(l.transaction_date, l.raw_description, l.amount_idr, l.occurrence_index) for l in r1.lines]
    keys2 = [(l.transaction_date, l.raw_description, l.amount_idr, l.occurrence_index) for l in r2.lines]
    assert keys1 == keys2
    assert len(set(keys1)) == len(keys1)  # every key unique within one parse


def test_real_activity_is_not_pure_pass_through_flagged_finding():
    """Documents a real finding, not just a parser assertion: CLAUDE.md
    describes the Bridging Account as "pure pass-through, no real
    operational activity", but the real May statement alone contains a
    cardless cash withdrawal (per later months) and, here, a debit-card
    admin fee, a monthly account admin fee, account tax, and interest —
    genuine operational activity distinct from the Payoneer-landing/
    BCA-sweep pair. Reported to Main-agent (see Builder's completion
    report) rather than silently reclassified — this test just pins the
    concrete evidence so it doesn't silently regress out of the fixture.
    """
    result = parse_mandiri_statement(MAY_SAMPLE)
    descriptions = {l.raw_description for l in result.lines}
    assert "Biaya administrasi kartu debit" in descriptions  # card admin fee
    assert "Biaya administrasi rekening" in descriptions  # account admin fee
    assert "Pajak rekening" in descriptions  # tax on interest
    assert "Bunga rekening" in descriptions  # interest

    # The Bridging->Main "Ke BCA" outflow amounts do NOT equal the Payoneer
    # withdrawal's net_idr_landed amount (the corresponding inflow) — the
    # user sweeps a smaller, rounder figure and keeps a buffer, rather than
    # passing through the exact landed amount. Concretely, for the 14 May
    # JPMORGAN landing (66,928,398.00) the next "Ke BCA" outflow two days
    # later is 66,930,000.00 — close in magnitude but NOT equal, and well
    # outside review-queue rule (c)'s Rp100 auto-match tolerance.
    inflow = next(l for l in result.lines if l.amount_idr == Decimal("66928398.00"))
    outflow = next(l for l in result.lines if l.amount_idr == Decimal("-66930000.00"))
    assert abs(inflow.amount_idr + outflow.amount_idr) > Decimal("100")
