"""Tests for ingestion.bank_statement against the real BCA sample.

Per CLAUDE.md's 2026-08-31 confirmation, this real sample is the BCA MAIN
ACCOUNT statement (not Bridging — see Prototype scope). The real Bridging
Account statement, collected 2026-09-01, turned out to be Mandiri-issued in
a completely different layout — see ingestion/mandiri_statement.py and
tests/ingestion/test_mandiri_statement.py, NOT this module. The synthetic
BCA-shaped Bridging fixture below (test_bridging_account_synthetic_fixture)
is kept as-is: it still exercises "a BCA-formatted bridging account" as a
hypothetical shape this parser can handle structurally, even though we now
know THIS business's actual Bridging Account isn't BCA-formatted.

Real sample note (2026-09-01): the old `sample-documents/bank-statements/`
single-sample layout was replaced with `sample-documents/Main Account
(BCA)/`, now holding 4 real consecutive months (May-Aug 2026). July is used
as the primary REAL_SAMPLE here for continuity with the other ingestion
tests' shared July 2026 period; May/Jun/Aug are covered by
test_all_four_real_monthly_samples_reconcile_cleanly below.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from ingestion.bank_statement import parse_bca_statement, parse_bca_statement_text

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "sample-documents" / "Main Account (BCA)"
REAL_SAMPLE = SAMPLES_DIR / "1790345891_JUL_2026.pdf"
MAY_SAMPLE = SAMPLES_DIR / "1790345891_MAY_2026.pdf"
JUN_SAMPLE = SAMPLES_DIR / "1790345891_JUN_2026.pdf"
AUG_SAMPLE = SAMPLES_DIR / "1790345891_AUG_2026.pdf"


def test_real_sample_reconciles_against_statements_own_printed_totals():
    """The strongest correctness signal available: the statement itself
    prints its total credit/debit amount AND row count. If our parser
    disagrees with either, that's a real extraction bug — not the
    statement's fault.
    """
    result = parse_bca_statement(REAL_SAMPLE)

    assert result.period_month == _dt.date(2026, 7, 1)
    assert result.opening_balance_idr == Decimal("75620613.34")
    assert result.parse_warnings == []
    assert result.reconciles is True

    credits = [l for l in result.lines if l.amount_idr > 0]
    debits = [l for l in result.lines if l.amount_idr < 0]
    assert len(credits) == 5
    assert len(debits) == 65
    assert sum((l.amount_idr for l in credits), Decimal("0")) == Decimal("173666961.57")
    assert -sum((l.amount_idr for l in debits), Decimal("0")) == Decimal("158201691.31")


def test_real_sample_every_line_has_nonzero_amount_and_valid_date():
    result = parse_bca_statement(REAL_SAMPLE)
    for line in result.lines:
        assert line.amount_idr != 0
        assert line.transaction_date.year == 2026
        assert line.transaction_date.month == 7
        assert line.raw_description  # never blank


def test_all_four_real_monthly_samples_reconcile_cleanly():
    """Fix 3 validation pass (2026-09-01): re-run the existing BCA parser
    against the newly-collected real multi-month data (May-Aug 2026) — it
    was only ever tested against a single real month before. All 4 reconcile
    cleanly with zero parse warnings; no parser change was needed.
    """
    for sample in (MAY_SAMPLE, JUN_SAMPLE, REAL_SAMPLE, AUG_SAMPLE):
        result = parse_bca_statement(sample)
        assert result.parse_warnings == [], f"{sample.name}: {result.parse_warnings}"
        assert result.reconciles is True, f"{sample.name} did not reconcile"


def test_reprocessing_same_pages_text_produces_identical_occurrence_indices():
    """Row-creation idempotency building block (design doc §7): re-parsing
    the exact same extracted text must produce the exact same sequence of
    (date, description, amount, occurrence_index) dedup keys.
    """
    import ingestion.bank_statement as bs

    pages_text = bs.extract_pdf_text_per_page(REAL_SAMPLE)
    r1 = parse_bca_statement_text(pages_text)
    r2 = parse_bca_statement_text(pages_text)

    keys1 = [(l.transaction_date, l.raw_description, l.amount_idr, l.occurrence_index) for l in r1.lines]
    keys2 = [(l.transaction_date, l.raw_description, l.amount_idr, l.occurrence_index) for l in r2.lines]
    assert keys1 == keys2
    assert len(set(keys1)) == len(keys1)  # every key is unique within one parse


def test_bank_admin_fee_and_interest_lines_present_and_correctly_signed():
    result = parse_bca_statement(REAL_SAMPLE)
    biaya_adm = [l for l in result.lines if "BIAYA ADM" in l.raw_description]
    bunga = [l for l in result.lines if l.raw_description == "BUNGA"]
    pajak_bunga = [l for l in result.lines if l.raw_description == "PAJAK BUNGA"]

    assert len(biaya_adm) == 1
    assert biaya_adm[0].amount_idr == Decimal("-10000.00")

    assert len(bunga) == 1
    assert bunga[0].amount_idr == Decimal("1261.57")  # credit — no DB suffix in the source

    assert len(pajak_bunga) == 1
    assert pajak_bunga[0].amount_idr == Decimal("-252.31")  # debit — DB suffix in the source


def test_bridging_account_synthetic_fixture_same_format_parses_cleanly():
    """Synthetic fixture in the identical BCA statement text format, built
    to exercise the BCA Bridging Account ingestion path CLAUDE.md confirms
    exists for this business but has no real sample yet (2026-08-31 note in
    Prototype scope). Deliberately small and self-consistent (own printed
    CR/DB totals match), same as the real file's own self-check.
    """
    synthetic_page = """
REKENING TAHAPAN XPRESI
KCP SYNTHETIC BRANCH
BRIDGING ACCOUNT HOLDER NO. REKENING : 9999999999
SYNTHETIC ADDRESS HALAMAN : 1 /1
PERIODE : MEI 2026
MATA UANG : IDR
CATATAN:
• Apabila nasabah tidak melakukan sanggahan...
TANGGAL KETERANGAN CBG MUTASI SALDO
01/05 SALDO AWAL 0.00
03/05 KR OTOMATIS LLG-DBS INDONESIA 86,247,936.00
Payoneer HK
id:synthetic
05/05 TRSF E-BANKING DB 0505/FTSCY/WS00000 86,247,936.00 DB 0.00
to BCA Main
SALDO AWAL : 0.00
MUTASI CR : 86,247,936.00 1
MUTASI DB : 86,247,936.00 1
SALDO AKHIR : 0.00
"""
    result = parse_bca_statement_text([synthetic_page])

    assert result.period_month == _dt.date(2026, 5, 1)
    assert result.reconciles is True
    assert len(result.lines) == 2
    inflow = [l for l in result.lines if l.amount_idr > 0][0]
    outflow = [l for l in result.lines if l.amount_idr < 0][0]
    assert inflow.amount_idr == Decimal("86247936.00")
    assert outflow.amount_idr == Decimal("-86247936.00")
