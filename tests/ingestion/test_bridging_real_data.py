"""End-to-end proof of the 2026-09-01 Bridging Account double-posting fix,
against the REAL 4-month Mandiri (Bridging) + BCA (Main) statement samples
and the 11 real Payoneer withdrawal confirmation PDFs — per Main-agent's
brief ("Testing — use the real data, not just synthetic").

Deliberately does NOT go through ingestion.sync's Drive-fed orchestration
(that's already covered, with fakes, in test_sync.py) — this file drives
ingestion.matching directly against real-parser output, so it stays focused
on proving the review-queue-level fix itself: zero phantom landing-echo
double-posts, real sweeps paired and posted exactly once each (independent
of net_idr_landed), no crash on a manually-labeled orphan row, and every
other real Bridging Account line (fees, interest, tax, e-money top-ups,
third-party transfers, cash withdrawal) safely falls to Needs Review rather
than being silently dropped or wrongly classified.

Updated 2026-09-02 (bank_keyword_rules seeding fix — see ingestion/seed.py):
the shared ``iprototype`` fixture now seeds the 5 real, confirmed rule-(e)
keyword rules by default, so this real dataset now DOES exercise rule (e)
against real data too (BI Fast transfer fees, the Bridging account admin
fee, and BUNGA/PAJAK BUNGA bank interest all auto-match and post) — see part
3b/6 in the test body below for the exact counts and the one real, flagged
asymmetry this surfaced (Bridging's "Pajak rekening" tax-on-interest line
does NOT auto-match, since "PAJAK BUNGA" the keyword and "Pajak rekening"
the real Bridging wording genuinely don't share a substring — left as
Needs Review, not silently dropped).
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update

from ingestion import bank_statement, mandiri_statement
from ingestion import payoneer as payoneer_mod
from ingestion.matching import RawLine, post_pending_rows, run_auto_match, stage_raw_lines
from ingestion.schema import review_queue, source_documents
from ledger import posting
from ledger.entities import get_account_id
from ledger.schema import journal_entries, journal_lines, payoneer_withdrawals

SAMPLES_ROOT = Path(__file__).resolve().parents[2] / "sample-documents"
BRIDGING_DIR = SAMPLES_ROOT / "Bridging Account (Mandiri)"
MAIN_DIR = SAMPLES_ROOT / "Main Account (BCA)"
CONFIRMATIONS_DIR = SAMPLES_ROOT / "Payoneer" / "Confirmation of Transfer"

pytestmark = pytest.mark.skipif(
    not BRIDGING_DIR.is_dir() or not MAIN_DIR.is_dir() or not CONFIRMATIONS_DIR.is_dir(),
    reason="Real sample-documents/ fixtures not present in this environment (gitignored, local-only).",
)


def _make_source_document(conn, *, document_type: str, period_month: _dt.date, **scope) -> int:
    result = conn.execute(
        source_documents.insert().values(
            document_type=document_type, period_month=period_month, drive_file_name="real-sample-test", **scope
        )
    )
    return result.inserted_primary_key[0]


def _load_and_post_real_withdrawals(conn, wallet_group_id: int) -> dict[int, payoneer_mod.WithdrawalConfirmation]:
    """Parses and posts all 11 real withdrawal confirmations, exactly as
    ingestion.payoneer would have at Payoneer-CSV-ingestion time (BEFORE
    the corresponding Bridging bank statement line is ever staged) — the
    real sequencing this bug depends on. Uses each confirmation's own
    stated exchange rate as a stand-in booking rate too (the realized-FX
    split's own correctness is covered elsewhere, e.g. tests/test_fx.py —
    not the concern of this file).
    """
    confirmations = [payoneer_mod.parse_confirmation_pdf(f) for f in sorted(CONFIRMATIONS_DIR.rglob("*.pdf"))]
    assert len(confirmations) == 11  # pin the real fixture's known shape

    entries: dict[int, payoneer_mod.WithdrawalConfirmation] = {}
    for c in confirmations:
        entry_id = posting.post_realized_fx_withdrawal(
            conn,
            wallet_group_id=wallet_group_id,
            entry_date=c.date_time_utc.date(),
            gross_usd=c.amount_withdrawn_usd,
            payoneer_fee_usd=c.fee_usd,
            exchange_rate_excl_fee=c.exchange_rate_excl_fee,
            booking_rate_used_idr=c.exchange_rate_excl_fee,
        )
        entries[entry_id] = c
    return entries


def _stage_all_bridging_lines(conn, wallet_group_id: int) -> int:
    total = 0
    for f in sorted(BRIDGING_DIR.glob("*.pdf")):
        parsed = mandiri_statement.parse_mandiri_statement(f)
        assert parsed.reconciles, f"{f.name} did not reconcile against its own printed totals"
        src_id = _make_source_document(
            conn,
            document_type="bank_statement_wallet_group",
            period_month=parsed.period_month,
            wallet_group_id=wallet_group_id,
        )
        lines = [
            RawLine(
                transaction_date=l.transaction_date,
                raw_description=l.raw_description,
                amount_idr=l.amount_idr,
                occurrence_index=l.occurrence_index,
            )
            for l in parsed.lines
        ]
        ids = stage_raw_lines(
            conn, source_type="bank_statement", source_document_id=src_id, wallet_group_id=wallet_group_id, lines=lines
        )
        total += len(ids)
    return total


def _stage_all_main_lines(conn) -> int:
    total = 0
    for f in sorted(MAIN_DIR.glob("*.pdf")):
        pages_text = bank_statement.extract_pdf_text_per_page(f)
        parsed = bank_statement.parse_bca_statement_text(pages_text)
        assert parsed.reconciles, f"{f.name} did not reconcile against its own printed CR/DB totals"
        period_month = parsed.lines[0].transaction_date.replace(day=1)
        src_id = _make_source_document(conn, document_type="bank_statement_master", period_month=period_month)
        lines = [
            RawLine(
                transaction_date=l.transaction_date,
                raw_description=l.raw_description,
                amount_idr=l.amount_idr,
                occurrence_index=l.occurrence_index,
            )
            for l in parsed.lines
        ]
        ids = stage_raw_lines(conn, source_type="bank_statement", source_document_id=src_id, lines=lines)
        total += len(ids)
    return total


def test_real_four_month_bridging_and_main_data_no_phantom_double_post(iprototype):
    """The core proof: run all 4 real months of Bridging + Main statement
    data, plus the 11 real withdrawal confirmations, through the full
    matching + posting pipeline, and verify every requirement of the
    2026-09-01 fix holds simultaneously against real data.
    """
    conn, topo = iprototype
    wg = topo["wallet_group_id"]

    withdrawal_entries = _load_and_post_real_withdrawals(conn, wg)
    assert len(withdrawal_entries) == 11
    entries_before_bank_data = conn.execute(select(journal_entries.c.id)).scalars().all()
    assert len(entries_before_bank_data) == 11  # just the 11 withdrawals so far, nothing else

    staged_bridging = _stage_all_bridging_lines(conn, wg)
    staged_main = _stage_all_main_lines(conn)
    assert staged_bridging == 63
    assert staged_main == 361

    match_result = run_auto_match(conn)
    post_result = post_pending_rows(conn)

    # --- 1. No unlabeled row silently posted, nothing crashed. ---
    assert post_result.skipped_pending_pair == 0  # every matched sweep found its pair
    assert match_result.matched + match_result.needs_review == staged_bridging + staged_main

    rows = conn.execute(
        select(review_queue.c.category, review_queue.c.match_rule, review_queue.c.wallet_group_id, review_queue.c.amount_idr)
    ).all()
    by_rule = Counter((r.category, r.match_rule) for r in rows)

    # --- 2. Landing echoes: exactly 11 (one per real withdrawal), each
    # reconciled (posted) but posting NOTHING NEW. ---
    landing_rows = [r for r in rows if r.match_rule == "c-landing"]
    assert len(landing_rows) == 11
    assert all(r.category == "internal_transfer_landing" for r in landing_rows)

    # --- 3. Real sweeps: the real data has 12 genuine "Ke BCA" / "BI-FAST CR
    # ... RICO" pairs (one extra sweep, 3 May 2026, whose landing predates
    # our 11-confirmation sample window — a real, legitimate transfer this
    # fix correctly finds from bank-statement evidence ALONE, proving rule
    # c-sweep genuinely never depends on payoneer_withdrawals/
    # net_idr_landed). 12 pairs = 24 rows.
    sweep_rows = [r for r in rows if r.match_rule == "c-sweep"]
    assert len(sweep_rows) == 24
    assert all(r.category == "internal_transfer" for r in sweep_rows)

    # --- 3b. Rule (e) keyword matches (2026-09-02 fix — bank_keyword_rules
    # was empty before this; see ingestion/seed.py). This test's iprototype
    # fixture now seeds the 5 real, confirmed keyword rules by default (same
    # as consignor_payout_tiers always being seeded), so — unlike before —
    # this real dataset DOES have keyword fixtures loaded and rule (e) DOES
    # fire on real data:
    #   - "Biaya transfer BI Fast" (Bridging, -Rp2,500 each) x12 across the
    #     4 real months (4+3+2+3) -> operating_expense/GENERAL_OPEX.
    #   - "Biaya administrasi rekening" (Bridging, -Rp6,000 each) x4 (once a
    #     month) -> operating_expense/GENERAL_OPEX. Deliberately does NOT
    #     also catch the Bridging statement's textually distinct "Biaya
    #     administrasi kartu debit" lines (a different real fee) — those
    #     stay Needs Review, see part 6 below.
    #   = 16 operating_expense/'e' rows total.
    #   - "BUNGA" matches BOTH BCA Main's own exact "BUNGA" line (x4, once a
    #     month) AND the Bridging statement's differently-worded "Bunga
    #     rekening" line (x4, once a month) — "BUNGA" is a substring of
    #     both. Confirmed intentional (see ingestion/seed.py's note): rule
    #     (e) has no per-statement scoping, and bank-credited interest is
    #     the same real fact regardless of which of the business's two bank
    #     accounts it was credited to.
    #   - "PAJAK BUNGA" matches ONLY BCA Main's exact "PAJAK BUNGA" line
    #     (x4) — it does NOT match the Bridging statement's "Pajak rekening"
    #     line (different wording after "Pajak "), so that Bridging-side tax
    #     -on-interest line is NOT netted against Bridging's own interest
    #     credit and correctly stays Needs Review (see part 6) — a real,
    #     flagged asymmetry, not a bug in this test.
    #   = 8 + 4 = 12 interest_income/'e' rows total.
    keyword_rows = [r for r in rows if r.match_rule == "e"]
    assert len(keyword_rows) == 28
    opex_keyword_rows = [r for r in keyword_rows if r.category == "operating_expense"]
    interest_keyword_rows = [r for r in keyword_rows if r.category == "interest_income"]
    assert len(opex_keyword_rows) == 16
    assert len(interest_keyword_rows) == 12

    # --- 4. Journal-entry-level proof: exactly 11 withdrawal entries (no
    # phantom second one from a landing echo) + exactly 12 sweep transfer
    # entries (no double-post, none missing) + 28 keyword-matched entries
    # (16 operating expense + 12 interest income — see part 3b), each its
    # own separate journal entry (one per real bank line, per
    # ledger.posting.post_interest_income_line's docstring on why BUNGA/
    # PAJAK BUNGA are never combined into one entry). ---
    all_entries = conn.execute(select(journal_entries.c.id, journal_entries.c.source_type)).all()
    entries_by_type = Counter(e.source_type for e in all_entries)
    assert entries_by_type["payoneer_withdrawal"] == 11
    assert entries_by_type["inter_account_transfer"] == 12
    assert entries_by_type["bank_other"] == 28
    assert len(all_entries) == 51  # 11 + 12 + 28 — see above; every one of them accounted for

    # Every landing-echo row's posted_journal_entry_id points at one of the
    # 11 EXISTING withdrawal entries, never a new one.
    posted_landing_entry_ids = conn.execute(
        select(review_queue.c.posted_journal_entry_id).where(review_queue.c.match_rule == "c-landing")
    ).scalars().all()
    assert set(posted_landing_entry_ids) <= set(withdrawal_entries.keys())

    # Each real sweep pair shares exactly one journal entry (never two).
    sweep_entry_ids = conn.execute(
        select(review_queue.c.posted_journal_entry_id).where(review_queue.c.match_rule == "c-sweep")
    ).scalars().all()
    assert len(sweep_entry_ids) == 24
    assert len(set(sweep_entry_ids)) == 12  # 24 rows, but only 12 distinct entries

    # --- 5. Books balance across all of it. ---
    all_lines = conn.execute(select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)).all()
    assert sum(l.debit_amount_idr for l in all_lines) == sum(l.credit_amount_idr for l in all_lines)

    # --- 5b. Money-correctness check on the new keyword-matched postings
    # specifically (not just "books balance globally", which a symmetric bug
    # could still satisfy): INTEREST_INCOME's own balance must net to the
    # true net interest actually received across BOTH statements (8 credit
    # lines minus 4 debit lines — see part 3b on why Bridging's "Pajak
    # rekening" isn't among the debits), and GENERAL_OPEX must carry exactly
    # the 16 keyword-matched fee amounts, not fewer/more/double-posted.
    interest_income_id = get_account_id(conn, "INTEREST_INCOME")
    general_opex_id = get_account_id(conn, "GENERAL_OPEX")
    interest_lines = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(
            journal_lines.c.account_id == interest_income_id
        )
    ).all()
    assert len(interest_lines) == 12
    net_interest_income = sum(l.credit_amount_idr - l.debit_amount_idr for l in interest_lines)
    assert net_interest_income == Decimal("5273.95")  # BUNGA(x4) + Bunga rekening(x4) - PAJAK BUNGA(x4)

    opex_lines_from_keywords = conn.execute(
        select(journal_lines.c.debit_amount_idr).where(
            journal_lines.c.account_id == general_opex_id, journal_lines.c.debit_amount_idr.in_([Decimal("2500.00"), Decimal("6000.00")])
        )
    ).scalars().all()
    assert len(opex_lines_from_keywords) == 16
    assert sum(opex_lines_from_keywords) == Decimal("54000.00")  # 12*2500 (BI Fast) + 4*6000 (admin fee)

    # --- 6. Every OTHER real Bridging/Main line (the DIFFERENT "Biaya
    # administrasi kartu debit" fee, "Pajak rekening" tax-on-interest,
    # e-money top-ups, third-party RICO/STEPHANUS transfers, cardless cash
    # withdrawal) is neither dropped nor wrongly classified — it safely
    # falls to Needs Review (nothing else in the seeded keyword catalog
    # legitimately matches any of these).
    needs_review_descriptions = conn.execute(
        select(review_queue.c.raw_description).where(review_queue.c.category.is_(None))
    ).scalars().all()
    assert len(needs_review_descriptions) == staged_bridging + staged_main - len(landing_rows) - len(sweep_rows) - len(
        keyword_rows
    )
    assert any("Pajak rekening" in d for d in needs_review_descriptions)  # tax on Bridging interest — see part 3b
    assert any("Biaya administrasi kartu debit" in d for d in needs_review_descriptions)  # a DIFFERENT fee than admin-fee-rekening
    assert not any(d == "Bunga rekening" for d in needs_review_descriptions)  # now correctly auto-matched, not left behind
    assert any("Top-up e-money" in d for d in needs_review_descriptions)
    assert any("Penarikan tunai tanpa kartu" in d for d in needs_review_descriptions)  # cardless cash withdrawal
    assert any("STEPHANUS" in d for d in needs_review_descriptions)  # third-party transfer

    # --- 7. Full-pipeline idempotency: re-running the ENTIRE real dataset's
    # matching + posting a second time (simulating a re-sync) matches and
    # posts NOTHING new. ---
    match_result_2 = run_auto_match(conn)
    post_result_2 = post_pending_rows(conn)
    assert match_result_2.matched == 0
    assert post_result_2.posted == 0
    assert post_result_2.skipped_pending_pair == 0
    entries_after_rerun = conn.execute(select(journal_entries.c.id)).scalars().all()
    assert len(entries_after_rerun) == 51  # still exactly 51, not 102


def test_real_data_adversarial_duplicate_landing_line_via_raw_sql_does_not_double_reconcile(iprototype):
    """Adversarial check (per Main-agent's brief: "try to break it yourself
    with a raw-SQL bypass"): insert a SECOND review_queue row, directly via
    SQL (bypassing stage_raw_lines' external_ref dedup entirely), that looks
    exactly like a real landing echo for an already-reconciled withdrawal.
    It must fall to Needs Review, not silently reconcile a second time.
    """
    conn, topo = iprototype
    wg = topo["wallet_group_id"]
    withdrawal_entries = _load_and_post_real_withdrawals(conn, wg)
    _stage_all_bridging_lines(conn, wg)
    _stage_all_main_lines(conn)
    run_auto_match(conn)
    post_pending_rows(conn)

    reconciled_before = set(
        conn.execute(
            select(payoneer_withdrawals.c.id).where(
                payoneer_withdrawals.c.bridging_landing_reconciled_review_queue_id.isnot(None)
            )
        ).scalars().all()
    )
    assert len(reconciled_before) == 11

    # Pick a real, already-reconciled withdrawal and forge a duplicate
    # landing line for it directly via raw SQL — bypassing every app-layer
    # idempotency guard (stage_raw_lines' external_ref/occurrence_index
    # dedup never runs at all here).
    target_entry_id, target_conf = next(iter(withdrawal_entries.items()))
    # Reuse an existing source_documents row (period_month + wallet_group is
    # unique) rather than creating a new one — the May Bridging statement's
    # row already exists from _stage_all_bridging_lines() above.
    src_id = conn.execute(
        select(source_documents.c.id).where(
            source_documents.c.document_type == "bank_statement_wallet_group",
            source_documents.c.wallet_group_id == wg,
            source_documents.c.period_month == _dt.date(2026, 5, 1),
        )
    ).scalar_one()
    net_idr_landed = conn.execute(
        select(payoneer_withdrawals.c.net_idr_landed).where(payoneer_withdrawals.c.journal_entry_id == target_entry_id)
    ).scalar_one()
    conn.execute(
        review_queue.insert().values(
            source_type="bank_statement",
            source_document_id=src_id,
            wallet_group_id=wg,
            external_ref=None,
            transaction_date=target_conf.date_time_utc.date(),
            amount_idr=net_idr_landed,
            raw_description="FORGED duplicate landing line (adversarial test)",
            match_status="needs_review",
            match_rule=None,
            category=None,
        )
    )

    match_result = run_auto_match(conn)
    forged_row = conn.execute(
        select(review_queue.c.category, review_queue.c.match_status).where(
            review_queue.c.raw_description == "FORGED duplicate landing line (adversarial test)"
        )
    ).one()
    # Must NOT have reconciled a second time against the already-claimed withdrawal.
    assert forged_row.category is None
    assert forged_row.match_status == "needs_review"

    post_result = post_pending_rows(conn)
    assert post_result.skipped_unclassified >= 1  # the forged row never posts

    # The withdrawal's reconciliation guard is still pointed at the
    # original, real landing row — not stolen by the forged one.
    still_reconciled = conn.execute(
        select(payoneer_withdrawals.c.bridging_landing_reconciled_review_queue_id).where(
            payoneer_withdrawals.c.journal_entry_id == target_entry_id
        )
    ).scalar_one()
    real_landing_row_id = conn.execute(
        select(review_queue.c.id).where(
            review_queue.c.match_rule == "c-landing",
            review_queue.c.linked_payoneer_withdrawal_id
            == conn.execute(
                select(payoneer_withdrawals.c.id).where(payoneer_withdrawals.c.journal_entry_id == target_entry_id)
            ).scalar_one(),
        )
    ).scalar_one()
    assert still_reconciled == real_landing_row_id

    # No phantom extra journal entry from the forged row.
    all_entries = conn.execute(select(journal_entries.c.id, journal_entries.c.source_type)).all()
    assert Counter(e.source_type for e in all_entries)["payoneer_withdrawal"] == 11


def test_real_data_manual_label_of_out_of_sample_sweep_before_its_pair_does_not_crash(iprototype):
    """Requirement 3, at real-data scale: stage ONLY the Bridging side of
    the one real sweep in this fixture that has no confirmation PDF backing
    it (3 May 2026, -65,950,000.00 — see the main test's docstring), human
    -label it 'internal_transfer' immediately, and confirm posting doesn't
    crash even though its Master-side pair hasn't been ingested in this
    run. Then ingest the real Main statement and confirm it resolves.
    """
    conn, topo = iprototype
    wg = topo["wallet_group_id"]

    may_bridging = next(f for f in BRIDGING_DIR.glob("*.pdf") if "Mei" in f.name)
    parsed = mandiri_statement.parse_mandiri_statement(may_bridging)
    src_id = _make_source_document(
        conn, document_type="bank_statement_wallet_group", period_month=parsed.period_month, wallet_group_id=wg
    )
    target_line = next(l for l in parsed.lines if l.amount_idr == Decimal("-65950000.00"))
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src_id,
        wallet_group_id=wg,
        lines=[
            RawLine(
                transaction_date=target_line.transaction_date,
                raw_description=target_line.raw_description,
                amount_idr=target_line.amount_idr,
                occurrence_index=target_line.occurrence_index,
            )
        ],
    )
    row_id = conn.execute(select(review_queue.c.id)).scalar_one()
    conn.execute(
        update(review_queue)
        .where(review_queue.c.id == row_id)
        .values(category="internal_transfer", labeled_at=_dt.datetime.now(_dt.timezone.utc))
    )

    post_result = post_pending_rows(conn)  # must not raise
    assert post_result.posted == 0
    assert post_result.skipped_pending_pair == 1

    # Now the real Main statement (containing the matching inflow) arrives.
    _stage_all_main_lines(conn)
    run_auto_match(conn)
    post_result_2 = post_pending_rows(conn)
    assert post_result_2.skipped_pending_pair == 0

    posted_at = conn.execute(select(review_queue.c.posted_at).where(review_queue.c.id == row_id)).scalar_one()
    assert posted_at is not None
