"""Review Queue screen route tests — especially the "never edit/post a
row twice" guard, since that's a hard rule from CLAUDE.md, not just UI
polish.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import select

from ingestion.schema import review_queue
from tests.webapp.conftest import make_review_queue_row, make_source_document

PERIOD = _dt.date(2026, 7, 1)
DAY = _dt.date(2026, 7, 5)


def test_labeling_a_needs_review_row_saves_but_does_not_post(logged_in_client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"])
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, wallet_group_id=topo["wallet_group_id"]
    )
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={"category": "operating_expense", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "operating_expense"
    assert row.labeled_at is not None
    assert row.posted_at is None  # Save never posts — only the next sync run does


def test_cannot_relabel_an_already_posted_row(logged_in_client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"])
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, wallet_group_id=topo["wallet_group_id"]
    )
    conn.execute(
        review_queue.update()
        .where(review_queue.c.id == row_id)
        .values(category="operating_expense", labeled_at=_dt.datetime.now(_dt.timezone.utc), posted_at=_dt.datetime.now(_dt.timezone.utc))
    )
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={"category": "owners_draw", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "operating_expense"  # unchanged — the attempted relabel was rejected


def test_invalid_category_rejected(logged_in_client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"])
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, wallet_group_id=topo["wallet_group_id"]
    )
    conn.commit()

    resp = logged_in_client.post(f"/review-queue/{row_id}", data={"category": "not_a_real_category"})
    assert resp.status_code in (301, 302)
    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_review_queue_page_renders_with_empty_state(logged_in_client, wtopology):
    resp = logged_in_client.get("/review-queue/?period=2026-07")
    assert resp.status_code == 200
    assert b"No bank statement uploaded yet" in resp.data


def test_contract_labor_is_a_selectable_category(logged_in_client, wtopology):
    """2026-09-05: the new CONTRACT_LABOR operating-expense account (see
    ledger/chart_of_accounts.py) must be selectable from the Review Queue
    UI, same interaction pattern as every other category — a human can pick
    it directly rather than it always falling back to Operating Expense/
    GENERAL_OPEX (see ingestion.matching._post_one_row's dedicated branch).
    """
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"])
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, wallet_group_id=topo["wallet_group_id"]
    )
    conn.commit()

    # The option is actually present in the rendered page, not just defined
    # in Python — a real functional check, not just a code-reading one.
    index_resp = logged_in_client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert index_resp.status_code == 200
    assert b"Contract Labor" in index_resp.data

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={"category": "contract_labor", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "contract_labor"
    assert row.labeled_at is not None
    assert row.posted_at is None  # saving a label never posts by itself
