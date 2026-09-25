"""Review Queue screen route tests — especially the "never edit/post a
row twice" guard, since that's a hard rule from CLAUDE.md, not just UI
polish.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import select

from ingestion.schema import review_queue
from tests.webapp.conftest import make_review_queue_row, make_source_document
from webapp.review_queue_bp import CATEGORY_OPTIONS

PERIOD = _dt.date(2026, 7, 1)
DAY = _dt.date(2026, 7, 5)


def test_labeling_a_needs_review_row_saves_but_does_not_post(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"])
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, wallet_group_id=topo["wallet_group_id"]
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "operating_expense", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "operating_expense"
    assert row.labeled_at is not None
    assert row.posted_at is None  # Save never posts — only the next sync run does


def test_cannot_relabel_an_already_posted_row(client, wtopology):
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

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "owners_draw", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "operating_expense"  # unchanged — the attempted relabel was rejected


def test_invalid_category_rejected(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"])
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, wallet_group_id=topo["wallet_group_id"]
    )
    conn.commit()

    resp = client.post(f"/review-queue/{row_id}", data={"category": "not_a_real_category"})
    assert resp.status_code in (301, 302)
    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_review_queue_page_renders_with_empty_state(client, wtopology):
    resp = client.get("/review-queue/?period=2026-07")
    assert resp.status_code == 200
    assert b"No bank statement uploaded yet" in resp.data


def test_contract_labor_is_a_selectable_category(client, wtopology):
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
    index_resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert index_resp.status_code == 200
    assert b"Contract Labor" in index_resp.data

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "contract_labor", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "contract_labor"
    assert row.labeled_at is not None
    assert row.posted_at is None  # saving a label never posts by itself


def test_employee_loan_disbursement_and_cogs_subcategories_are_selectable(client, wtopology):
    """2026-09-10: new categories must be selectable from the UI, same
    interaction pattern as every other category."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    index_resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert index_resp.status_code == 200
    assert b"Employee Loan Disbursement" in index_resp.data
    assert b"COGS \xe2\x80\x94 Item Purchase" in index_resp.data
    assert b"COGS \xe2\x80\x94 Inbound Shipping" in index_resp.data
    assert b"Payroll" in index_resp.data
    assert b"Outbound Shipping (to Customer)" in index_resp.data


def test_employee_loan_disbursement_saves_with_employee_reference(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-27000000
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "employee_loan_disbursement", "consignor_item_ref": "Fariz Pradana", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "employee_loan_disbursement"
    assert row.consignor_item_ref == "Fariz Pradana"
    assert row.posted_at is None  # saving a label never posts by itself


def test_employee_loan_disbursement_requires_employee_reference(client, wtopology):
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

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "employee_loan_disbursement", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved with no employee reference


def test_employee_loan_disbursement_rejects_whitespace_only_employee_reference(client, wtopology):
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

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "employee_loan_disbursement", "consignor_item_ref": "   ", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved as "labeled" with a blank reference


def test_payroll_row_can_save_with_loan_repayment_amount(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-8500000
    )
    conn.commit()

    resp = client.post(
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


def test_loan_repayment_amount_rejected_for_non_payroll_category(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-100000)
    conn.commit()

    resp = client.post(
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


def test_loan_repayment_amount_requires_employee_reference(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-8500000)
    conn.commit()

    resp = client.post(
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


def test_category_options_follow_pl_statement_order():
    """2026-09-25: the dropdown must mirror the P&L's actual flow — Revenue,
    then every COGS variant grouped together, then every Operating Expense
    grouped together (matching the Chart of Accounts order in CLAUDE.md),
    then Other Income, then the non-P&L balance-sheet/equity transaction
    types, with 'other' staying last. This is an intentional display-order
    change (values/labels/posting logic are untouched) — not a regression to
    revert if it ever fails after another category is added; update the
    expected order below deliberately instead."""
    expected_order = [
        "revenue_settlement",
        "cogs_purchase",
        "item_purchase",
        "inbound_shipping",
        "item_purchase_and_inbound_shipping",
        "payroll",
        "operating_expense",
        "shipping_cost",
        "packaging_supplies",
        "contract_labor",
        "staff_meals_welfare",
        "interest_income",
        "internal_transfer",
        "consignment_payout",
        "employee_loan_disbursement",
        "owners_draw",
        "owners_contribution",
        "other",
    ]
    assert [value for value, _label in CATEGORY_OPTIONS] == expected_order


def test_loan_repayment_amount_rejects_whitespace_only_employee_reference(client, wtopology):
    """QA-found gap (2026-09-10, round 2) — same whitespace-only regression
    check as the employee_loan_disbursement case above, for the payroll +
    loan_repayment_amount_idr path."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-8500000)
    conn.commit()

    resp = client.post(
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


def test_successful_save_redirects_anchored_to_the_saved_row(client, wtopology):
    """2026-09-25 (Fix 3): after a successful save, the redirect must land
    the user back at the row they just worked on (via a #rq-row-<id> URL
    fragment) instead of the top of a long page."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "operating_expense", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)
    assert resp.headers["Location"].endswith(f"#rq-row-{row_id}")


def test_validation_failure_redirect_has_no_row_anchor(client, wtopology):
    """The validation-failure path never posted a row_id through, so it
    should not carry a #rq-row-<id> fragment — only a successful save does."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.post(f"/review-queue/{row_id}", data={"category": "not_a_real_category"})
    assert resp.status_code in (301, 302)
    assert "#rq-row-" not in resp.headers["Location"]


def test_row_and_editor_markup_wired_for_loan_field_toggle(client, wtopology):
    """2026-09-25 (Fixes 2 & 3): the row has a stable id for the redirect
    anchor, the category select calls the per-row toggle function, and the
    loan-repayment input starts disabled unless the row's own category is
    already 'payroll'."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    html = resp.data.decode()
    assert f'id="rq-row-{row_id}"' in html
    assert f'onchange="rqCategoryChanged({row_id})"' in html
    assert "function rqCategoryChanged(rowId)" in html
    # Not labeled payroll yet, so the loan field must start disabled.
    import re

    editor_match = re.search(rf'id="rq-editor-{row_id}".*?</tr>', html, re.DOTALL)
    assert editor_match is not None
    assert re.search(r'name="loan_repayment_amount_idr"[^>]*disabled', editor_match.group(0))
