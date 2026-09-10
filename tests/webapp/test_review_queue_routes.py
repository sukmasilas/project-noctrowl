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


def test_employee_loan_disbursement_and_cogs_subcategories_are_selectable(logged_in_client, wtopology):
    """2026-09-10: new categories must be selectable from the UI, same
    interaction pattern as every other category."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    index_resp = logged_in_client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert index_resp.status_code == 200
    assert b"Employee Loan Disbursement" in index_resp.data
    assert b"COGS \xe2\x80\x94 Item Purchase" in index_resp.data
    assert b"COGS \xe2\x80\x94 Inbound Shipping" in index_resp.data
    assert b"Payroll" in index_resp.data
    assert b"Outbound Shipping (to Customer)" in index_resp.data


def test_employee_loan_disbursement_saves_with_employee_reference(logged_in_client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-27000000
    )
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={"category": "employee_loan_disbursement", "consignor_item_ref": "Fariz Pradana", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "employee_loan_disbursement"
    assert row.consignor_item_ref == "Fariz Pradana"
    assert row.posted_at is None  # saving a label never posts by itself


def test_employee_loan_disbursement_requires_employee_reference(logged_in_client, wtopology):
    """QA-found gap (2026-09-10): a blank employee reference must be
    rejected at the UI layer too, same as the loan-repayment case — never
    silently saved and later posted as a placeholder "unspecified" employee
    reference (see ingestion/matching.py's _missing_employee_ref_reason for
    the matching defense-in-depth backstop)."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-27000000
    )
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={"category": "employee_loan_disbursement", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved with no employee reference


def test_employee_loan_disbursement_rejects_whitespace_only_employee_reference(logged_in_client, wtopology):
    """QA-found gap (2026-09-10, round 2): a whitespace-only submission
    ("   ") is truthy in Python, so an unstripped check would have let it
    silently pass as a "real" reference — must be treated identically to a
    genuinely empty string."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-27000000
    )
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={"category": "employee_loan_disbursement", "consignor_item_ref": "   ", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved as "labeled" with a blank reference


def test_payroll_row_can_save_with_loan_repayment_amount(logged_in_client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-8500000
    )
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "payroll",
            "consignor_item_ref": "Fariz Pradana",
            "loan_repayment_amount_idr": "1500000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "payroll"
    assert row.consignor_item_ref == "Fariz Pradana"
    assert row.loan_repayment_amount_idr == 1500000


def test_loan_repayment_amount_rejected_for_non_payroll_category(logged_in_client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-100000)
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "operating_expense",
            "loan_repayment_amount_idr": "1500000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved with the wrong category


def test_loan_repayment_amount_requires_employee_reference(logged_in_client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-8500000)
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "payroll",
            "consignor_item_ref": "",
            "loan_repayment_amount_idr": "1500000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected — no employee reference to draw the loan balance down against


def test_loan_repayment_amount_rejects_whitespace_only_employee_reference(logged_in_client, wtopology):
    """QA-found gap (2026-09-10, round 2) — same whitespace-only regression
    check as the employee_loan_disbursement case above, for the payroll +
    loan_repayment_amount_idr path."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-8500000)
    conn.commit()

    resp = logged_in_client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "payroll",
            "consignor_item_ref": "   ",
            "loan_repayment_amount_idr": "1500000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected — whitespace-only is not a real employee reference
