"""Tests for ingestion.bank_statement against the real BCA sample.

Per CLAUDE.md's 2026-08-31 confirmation, this real sample is the BCA MAIN
ACCOUNT statement (not Bridging — see Prototype scope). The Bridging-account
path is exercised separately with a synthetic fixture in the same format
(see test_bridging_account_synthetic_fixture below), since no real Bridging
statement has been collected yet — per Main-agent's explicit instruction not
to block on that and not to claim real-sample coverage for it.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

from ingestion.bank_statement import parse_bca_statement, parse_bca_statement_text

REAL_SAMPLE = (
    Path(__file__).resolve().parents[2] / "sample-documents" / "bank-statements" / "BCA Bank_1790345891_APR_2026.pdf"
)


def test_real_sample_reconciles_against_statements_own_printed_totals():
    """The strongest correctness signal available: the statement itself
    prints its total credit/debit amount AND row count. If our parser
    disagrees with either, that's a real extraction bug — not the
    statement's fault.
    """
    result = parse_bca_statement(REAL_SAMPLE)

    assert result.period_month == _dt.date(2026, 4, 1)
    assert result.opening_balance_idr == Decimal("13794994.94")
    assert result.parse_warnings == []
    assert result.reconciles is True

    credits = [l for l in result.lines if l.amount_idr > 0]
    debits = [l for l in result.lines if l.amount_idr < 0]
    assert len(credits) == 6
    assert len(debits) == 73
    assert sum((l.amount_idr for l in credits), Decimal("0")) == Decimal("164233187.11")
    assert -sum((l.amount_idr for l in debits), Decimal("0")) == Decimal("167925222.22")


def test_real_sample_every_line_has_nonzero_amount_and_valid_date():
    result = parse_bca_statement(REAL_SAMPLE)
    for line in result.lines:
        assert line.amount_idr != 0
        assert line.transaction_date.year == 2026
        assert line.transaction_date.month == 4
        assert line.raw_description  # never blank


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
    assert bunga[0].amount_idr == Decimal("596.11")  # credit — no DB suffix in the source

    assert len(pajak_bunga) == 1
    assert pajak_bunga[0].amount_idr == Decimal("-119.22")  # debit — DB suffix in the source


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
