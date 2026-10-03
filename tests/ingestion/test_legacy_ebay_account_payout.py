"""Tests for the SYSTEM-ONLY 'legacy_ebay_account_payout' category (added
2026-10-03): posting function, matching/posting path, auto-match exclusion,
and scripts/post_legacy_ebay_account_payouts.py's verify/apply logic.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from ingestion.matching import RawLine, post_pending_rows, run_auto_match, stage_raw_lines
from ingestion.schema import bank_keyword_rules, review_queue, source_documents
from ledger import posting
from ledger.schema import journal_entries, journal_lines
from scripts.post_legacy_ebay_account_payouts import (
    CATEGORY,
    LEGACY_ROWS,
    _StateMismatch,
    _apply_one,
    _load_and_verify_one,
)
from tests.helpers import assert_balanced, lines_by_code
from tests.ingestion.conftest import make_source_document

FEB_ROW = LEGACY_ROWS[0]


def _master_doc(conn, file_name):
    return conn.execute(
        source_documents.insert().values(
            document_type="bank_statement_master",
            period_month=_dt.date(2026, 2, 1),
            drive_file_name=file_name,
        )
    ).inserted_primary_key[0]


def _row(conn, spec, *, file_name=None, doc_id=None, category="revenue_settlement", **overrides):
    doc_id = doc_id or _master_doc(conn, file_name or spec["source_file_name"])
    values = dict(
        source_type="bank_statement",
        source_document_id=doc_id,
        transaction_date=spec["transaction_date"],
        amount_idr=spec["amount_idr"],
        raw_description="KR OTOMATIS LLG-MANDIRI 0938 / NUSA SATU INTI ART",
        match_status="needs_review",
        category=category,
        posting_error_reason="No accounts row for 'EBAY_WALLET'",
        external_ref=f"t-{spec['review_queue_id']}",
    )
    values.update(overrides)
    return conn.execute(review_queue.insert().values(**values)).inserted_primary_key[0]


def _spec_for(rq_id, base=FEB_ROW):
    return {**base, "review_queue_id": rq_id}


# --- posting function -------------------------------------------------------


def test_post_legacy_payout_balances_and_has_caveat_memo(iprototype):
    conn, _ = iprototype
    je = posting.post_legacy_ebay_account_payout(
        conn, entry_date=_dt.date(2026, 2, 19), amount_idr=Decimal("48643836"), memo="KR OTOMATIS"
    )
    lines = lines_by_code(conn, je)
    assert lines["BCA_MAIN"][0].debit_amount_idr == Decimal("48643836")
    assert lines["SALES_REVENUE"][0].credit_amount_idr == Decimal("48643836")
    assert lines["BCA_MAIN"][0].amount_usd_ref is None
    assert_balanced(conn, je)
    memo = conn.execute(select(journal_entries.c.memo).where(journal_entries.c.id == je)).scalar_one()
    assert "NET of" in memo and "not when the underlying sale happened" in memo


@pytest.mark.parametrize("amt", [Decimal("0"), Decimal("-5")])
def test_post_legacy_payout_rejects_non_positive(iprototype, amt):
    conn, _ = iprototype
    with pytest.raises(ValueError):
        posting.post_legacy_ebay_account_payout(conn, entry_date=_dt.date(2026, 2, 19), amount_idr=amt)


# --- post_pending_rows path -------------------------------------------------


def test_post_pending_rows_posts_master_row_once(iprototype):
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW, category=CATEGORY)
    assert post_pending_rows(conn).posted == 1
    assert post_pending_rows(conn).posted == 0
    row = conn.execute(select(review_queue).where(review_queue.c.id == rq)).one()
    assert row.posted_at is not None and row.posted_journal_entry_id is not None


def test_outflow_is_sign_mismatch_not_posted(iprototype):
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW, category=CATEGORY, amount_idr=Decimal("-48643836"))
    res = post_pending_rows(conn)
    assert res.posted == 0 and res.skipped_sign_mismatch == 1
    assert conn.execute(select(review_queue.c.posted_at).where(review_queue.c.id == rq)).scalar_one() is None


def test_scoped_row_fails_cleanly(iprototype):
    conn, topo = iprototype
    rq = _row(conn, FEB_ROW, category=CATEGORY, wallet_group_id=topo["wallet_group_id"])
    res = post_pending_rows(conn)
    assert res.posted == 0 and res.failed_to_post == 1
    row = conn.execute(select(review_queue).where(review_queue.c.id == rq)).one()
    assert row.posted_at is None and "legacy_ebay_account_payout" in row.posting_error_reason


# --- auto-match never assigns it -------------------------------------------


def test_auto_match_never_assigns_legacy_category(iprototype):
    conn, _ = iprototype
    src = make_source_document(conn, document_type="bank_statement_master", period_month=_dt.date(2026, 2, 1))
    stage_raw_lines(
        conn,
        source_type="bank_statement",
        source_document_id=src,
        lines=[
            RawLine(
                transaction_date=_dt.date(2026, 2, 19),
                raw_description="KR OTOMATIS LLG-MANDIRI 0938 / NUSA SATU INTI ART / Payoneer HK",
                amount_idr=Decimal("48643836"),
                occurrence_index=1,
            )
        ],
    )
    run_auto_match(conn)
    cats = [c for (c,) in conn.execute(select(review_queue.c.category)).all()]
    assert CATEGORY not in cats
    assert CATEGORY not in [c for (c,) in conn.execute(select(bank_keyword_rules.c.category)).all()]


# --- script verify / apply --------------------------------------------------


def test_verify_accepts_good_row(iprototype):
    conn, _ = iprototype
    doc = _master_doc(conn, FEB_ROW["source_file_name"])
    for start in ("revenue_settlement", None):
        rq = _row(conn, FEB_ROW, doc_id=doc, category=start, external_ref=f"x-{start}")
        v = _load_and_verify_one(conn, _spec_for(rq))
        assert v["already_posted"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        {"amount_idr": Decimal("48643837")},
        {"transaction_date": _dt.date(2026, 2, 20)},
    ],
)
def test_verify_rejects_wrong_amount_or_date(iprototype, mutate):
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW, **mutate)
    with pytest.raises(_StateMismatch):
        _load_and_verify_one(conn, _spec_for(rq))


def test_verify_rejects_wrong_source_document(iprototype):
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW, file_name="1790345891_MAR_2026.pdf")
    with pytest.raises(_StateMismatch):
        _load_and_verify_one(conn, _spec_for(rq))


def test_verify_rejects_scoped_row(iprototype):
    conn, topo = iprototype
    rq = _row(conn, FEB_ROW, ebay_account_id=topo["ebay_account_id"])
    with pytest.raises(_StateMismatch):
        _load_and_verify_one(conn, _spec_for(rq))


def test_verify_rejects_unexpected_starting_category(iprototype):
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW, category="operating_expense")
    with pytest.raises(_StateMismatch):
        _load_and_verify_one(conn, _spec_for(rq))


def test_verify_rejects_row_posted_by_something_else(iprototype):
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW, category="revenue_settlement")
    conn.execute(
        update(review_queue).where(review_queue.c.id == rq).values(posted_at=_dt.datetime.now(_dt.timezone.utc))
    )
    with pytest.raises(_StateMismatch):
        _load_and_verify_one(conn, _spec_for(rq))


def test_verify_rejects_missing_row(iprototype):
    conn, _ = iprototype
    with pytest.raises(_StateMismatch):
        _load_and_verify_one(conn, _spec_for(999999))


def test_apply_posts_marks_row_and_is_idempotent(iprototype):
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW)
    spec = _spec_for(rq)
    je = _apply_one(conn, spec)
    row = conn.execute(select(review_queue).where(review_queue.c.id == rq)).one()
    assert row.category == CATEGORY
    assert row.match_status == "matched"
    assert row.posted_at is not None
    assert row.posted_journal_entry_id == je
    assert row.posting_error_reason is None
    assert_balanced(conn, je)

    # Re-verify reports already-posted (script skips it); pending-rows pass doesn't re-post.
    v = _load_and_verify_one(conn, spec)
    assert v["already_posted"] is True and v["journal_entry_id"] == je
    assert post_pending_rows(conn).posted == 0
    n = conn.execute(select(journal_entries.c.id).where(journal_entries.c.id == je)).all()
    assert len(n) == 1


def test_apply_raises_and_posts_nothing_if_row_posted_between_verify_and_apply(iprototype):
    """QA scenario: verify passes, then (during the DB-identity prompt wait)
    the row is labeled and posted by post_pending_rows, then apply runs. It
    must raise, not post a second entry, and leave exactly one journal entry.
    """
    conn, _ = iprototype
    rq = _row(conn, FEB_ROW)
    spec = _spec_for(rq)
    _load_and_verify_one(conn, spec)  # phase 1 passes

    # Concurrent actor labels + posts the row by another path.
    conn.execute(update(review_queue).where(review_queue.c.id == rq).values(category=CATEGORY))
    assert post_pending_rows(conn).posted == 1
    before = conn.execute(select(journal_entries.c.id)).all()
    assert len(before) == 1

    with pytest.raises(_StateMismatch):
        _apply_one(conn, spec)

    after = conn.execute(select(journal_entries.c.id)).all()
    assert len(after) == 1
    row = conn.execute(select(review_queue).where(review_queue.c.id == rq)).one()
    assert row.category == CATEGORY and row.posted_journal_entry_id == before[0].id
