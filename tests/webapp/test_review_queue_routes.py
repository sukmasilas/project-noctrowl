"""Review Queue screen route tests — especially the "never edit/post a
row twice" guard, since that's a hard rule from CLAUDE.md, not just UI
polish.
"""
from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal

from sqlalchemy import select

from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.matching import post_pending_rows
from ingestion.schema import review_queue
from ledger.seed import seed_full_topology
from tests.helpers import lines_by_code
from tests.webapp.conftest import make_review_queue_row, make_source_document
from webapp.review_queue_bp import CATEGORY_OPTIONS

PERIOD = _dt.date(2026, 7, 1)
DAY = _dt.date(2026, 7, 5)


def test_review_queue_page_renders_resolved_duplicate_row_without_crashing(client, wtopology):
    """INCIDENT FIX (2026-10-01) regression test: a 'resolved_duplicate' row
    must render its own distinct badge (not the misleading default "Needs
    Review" the template's old binary matched/else check would have shown),
    and must NOT be clickable to open the inline labeling editor — this row
    is terminal, never meant to be relabeled/posted through the normal flow.
    """
    conn, topo = wtopology
    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"]
    )
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        amount_idr=Decimal("1640000"),
        amount_usd_ref=Decimal("100.00"),
        source_type="payoneer_csv",
        wallet_group_id=topo["wallet_group_id"],
        raw_description="Payment from eBay",
        match_status="resolved_duplicate",
        category="revenue_settlement",
        posted_at=_dt.datetime.now(_dt.timezone.utc),
        resolution_note="Resolved automatically: duplicate artifact, kept for traceability.",
    )
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Resolved" in body
    # Must not fall into the template's amber "Needs Review" badge default —
    # the summary card/filter dropdown legitimately say "Needs Review"
    # elsewhere on the page, so check the specific row-status badge markup,
    # not a blanket page-wide substring.
    assert '<span class="badge amber">Needs Review</span>' not in body


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


def test_posting_error_reason_renders_visibly_on_the_review_queue_page(client, wtopology):
    """INCIDENT FIX (2026-09-28): a row that fails while actually being
    posted (see ingestion.matching.post_pending_rows' per-row try/except and
    PostResult.failed_to_post) must be visibly explained in the Review Queue
    UI, mirroring how sign_mismatch_reason/missing_reference_reason already
    render — a human should never need to check server logs to see why a
    row didn't post.
    """
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_wallet_group", period_month=PERIOD, wallet_group_id=topo["wallet_group_id"])
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        wallet_group_id=topo["wallet_group_id"],
        category="revenue_settlement",
        posting_error_reason=(
            "Failed to post while classified as 'revenue_settlement': No accounts row for "
            "'EBAY_WALLET' (ebay_account_id=None, wallet_group_id=None). Not posted — please "
            "re-check the classification."
        ),
    )
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert resp.status_code == 200
    assert b"posting error" in resp.data
    assert b"EBAY_WALLET" in resp.data


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


def test_inventory_deposit_is_a_selectable_category(client, wtopology):
    """2026-09-29: the new 'inventory_deposit' category must be selectable
    from the UI, same interaction pattern as every other category."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    index_resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert index_resp.status_code == 200
    assert b"Inventory Deposit (Advance to Supplier)" in index_resp.data


def test_inventory_deposit_saves_with_deposit_reference(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-9840000
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "inventory_deposit", "consignor_item_ref": "DP Box op", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "inventory_deposit"
    assert row.consignor_item_ref == "DP Box op"
    assert row.posted_at is None  # saving a label never posts by itself


def test_inventory_deposit_requires_deposit_reference(client, wtopology):
    """Same reasoning as employee_loan_disbursement above — an aggregate
    asset account with no per-supplier sub-ledger needs a real
    per-transaction reference, never silently saved blank."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-9840000
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "inventory_deposit", "consignor_item_ref": "", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved with no deposit reference


def test_inventory_deposit_rejects_whitespace_only_deposit_reference(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-9840000
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "inventory_deposit", "consignor_item_ref": "   ", "period": PERIOD.isoformat()},
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
    expected order below deliberately instead.

    Updated 2026-09-29 for the two new refund categories (see CLAUDE.md):
    'customer_refund' is revenue-side (posts to Sales Returns & Allowances,
    a contra-revenue line) — placed right after 'revenue_settlement'.
    'cogs_refund' is COGS-side (a credit reducing the existing COGS account)
    — placed with the other COGS labels, right before 'payroll'.

    Updated again 2026-09-29 for 'inventory_deposit' (a new, non-P&L
    balance-sheet asset category, same grouping as 'employee_loan_
    disbursement' — see CLAUDE.md's INVENTORY_DEPOSITS note) — placed right
    after 'employee_loan_disbursement'."""
    expected_order = [
        "revenue_settlement",
        "customer_refund",
        "cogs_purchase",
        "item_purchase",
        "inbound_shipping",
        "item_purchase_and_inbound_shipping",
        "cogs_refund",
        "payroll",
        "operating_expense",
        "shipping_cost",
        "shipping_cost_refund",
        "packaging_supplies",
        "contract_labor",
        "staff_meals_welfare",
        "interest_income",
        "internal_transfer",
        "consignment_payout",
        "employee_loan_disbursement",
        "inventory_deposit",
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


def test_ajax_save_returns_json_success_and_does_not_redirect(client, wtopology):
    """2026-09-29 UX fix: a request that signals it wants an AJAX response
    (X-Requested-With: XMLHttpRequest, set by review_queue.html's fetch()
    handler) must get a JSON body back — not the redirect a plain form POST
    gets — so the front end can update the row in place instead of
    navigating. The save itself (DB row updated, never auto-posted) is
    identical to the non-AJAX path; only the response shape differs."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "operating_expense", "consignor_item_ref": "", "period": PERIOD.isoformat()},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    assert resp.is_json
    body = resp.get_json()
    assert body["success"] is True
    assert body["category_label"] == "Operating Expense"
    assert "Location" not in resp.headers  # no redirect for the AJAX path

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "operating_expense"
    assert row.labeled_at is not None
    assert row.posted_at is None  # saving a label never posts by itself


def test_ajax_save_returns_json_failure_for_invalid_category(client, wtopology):
    """The AJAX path must surface a validation failure as a non-2xx JSON
    response (so fetch() can tell success apart from failure), not a
    redirect — and the row must be left unlabeled, same guard as the plain
    form path."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "not_a_real_category"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert resp.is_json
    body = resp.get_json()
    assert body["success"] is False
    assert body["message"]

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_ajax_save_returns_json_failure_for_missing_loan_reference(client, wtopology):
    """A validation rule with money implications (loan repayment needs a
    real employee reference) must still be enforced identically on the AJAX
    path — only the response shape changes, never the rule itself."""
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
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_ajax_save_returns_json_failure_for_already_posted_row(client, wtopology):
    """A row that's already posted must never be silently relabeled via the
    AJAX path either — same 'corrections to a posted row are out of scope'
    guard as the plain-form path, just returned as a non-2xx JSON body
    instead of a redirect+flash."""
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
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["success"] is False

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "operating_expense"  # unchanged


def test_plain_form_post_still_redirects_when_ajax_header_absent(client, wtopology):
    """Regression guard for the AJAX addition: a request with no
    X-Requested-With header (the graceful-degradation/no-JS case) must keep
    getting the exact original redirect-based response, not JSON."""
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
    assert not resp.is_json


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


def test_row_markup_wired_with_stable_posted_cell_id(client, wtopology):
    """2026-09-29 (Posted-cell AJAX fix): the Posted column's <td> needs a
    stable per-row id, same convention as rq-category-<id>, so the AJAX
    success handler in review_queue.html can update it in place without a
    page reload."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    html = resp.data.decode()
    assert f'id="rq-posted-{row_id}"' in html


def test_successful_relabel_clears_stale_posting_error_reason(client, wtopology):
    """2026-09-29 (Posted-cell AJAX fix): a row can reach label_row a second
    time already carrying a stale sign_mismatch_reason / missing_reference_
    reason / posting_error_reason left over from an earlier failed
    post_pending_rows attempt (that's exactly what flips it back to
    needs_review with a reason set, but posted_at still NULL, making the
    editor reachable again — see ingestion/matching.py).

    Before this fix, a successful relabel left the OLD reason on the row, so
    the server-side Posted-column template would keep showing the stale red
    "Not posted — ..." badge even though the row was freshly relabeled and
    genuinely queued for the next sync with no error yet. A successful save
    must always mean 'labeled, not posted, no error reason' — the same
    invariant post_pending_rows itself already guarantees on an actual
    successful post.
    """
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        category="revenue_settlement",
        posting_error_reason="Failed to post while classified as 'revenue_settlement': some earlier error.",
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "operating_expense", "consignor_item_ref": "", "period": PERIOD.isoformat()},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "operating_expense"
    assert row.posting_error_reason is None
    assert row.sign_mismatch_reason is None
    assert row.missing_reference_reason is None
    assert row.posted_at is None


def test_row_markup_wired_for_ajax_save(client, wtopology):
    """2026-09-29 UX fix: the row/editor markup needed by review_queue.html's
    fetch() handler must actually be present — the category cell has a
    stable id to update in place, the form is flagged for the AJAX handler
    to pick up (class + data-row-id, since a plain <form method=post> alone
    would just be a normal submission), and there's a dedicated inline error
    element scoped to this row."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    html = resp.data.decode()
    assert f'id="rq-category-{row_id}"' in html
    assert f'data-row-id="{row_id}"' in html
    assert 'class="rq-editor-form' in html
    assert f'id="rq-editor-error-{row_id}"' in html
    assert "rq-editor-form" in html and "addEventListener('submit'" in html


# ---------------------------------------------------------------------------
# Optional bundled item-purchase + outbound-shipping split (2026-09-29). See
# CLAUDE.md and ledger.posting.post_cogs_purchase_with_shipping_split. Real
# trigger: a Master Account bank line, "TRSF E-BANKING DB ... / BANK NEO
# COM ...", -Rp 4,140,000, confirmed by the user as Rp 2,140,000 item
# purchase + Rp 2,000,000 outbound shipping bundled into one payment.
# ---------------------------------------------------------------------------


def test_cogs_row_can_save_with_shipping_portion_amount(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "cogs_purchase",
            "shipping_portion_idr": "2000000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "cogs_purchase"
    assert row.shipping_portion_idr == 2000000


def test_item_purchase_row_can_also_save_with_shipping_portion_amount(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "item_purchase",
            "shipping_portion_idr": "2000000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "item_purchase"
    assert row.shipping_portion_idr == 2000000


def test_cogs_row_can_still_save_with_no_shipping_portion(client, wtopology):
    """Leaving the field blank is a no-op — the row posts exactly as it
    always has (verified end-to-end by the ingestion-layer tests)."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-300000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "cogs_purchase", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "cogs_purchase"
    assert row.shipping_portion_idr is None


def test_shipping_portion_rejected_for_non_cogs_category(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "operating_expense",
            "shipping_portion_idr": "2000000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved with the wrong category


def test_shipping_portion_rejected_for_inbound_shipping_category(client, wtopology):
    """'inbound_shipping' already represents a DIFFERENT (inbound
    freight-in) shipping concept — must not accept the outbound-shipping
    split field."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "inbound_shipping",
            "shipping_portion_idr": "2000000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_shipping_portion_rejected_for_item_purchase_and_inbound_shipping_category(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "item_purchase_and_inbound_shipping",
            "shipping_portion_idr": "2000000",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_shipping_portion_rejected_when_zero(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "cogs_purchase", "shipping_portion_idr": "0", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_shipping_portion_rejected_when_negative(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "cogs_purchase", "shipping_portion_idr": "-1", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_shipping_portion_rejected_when_equal_to_total(client, wtopology):
    """There must be something left over for COGS — a shipping portion
    equal to the row's own total amount is invalid."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "cogs_purchase", "shipping_portion_idr": "4140000", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_shipping_portion_rejected_when_greater_than_total(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY, amount_idr=-4140000)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "cogs_purchase", "shipping_portion_idr": "5000000", "period": PERIOD.isoformat()},
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None


def test_shipping_portion_field_disabled_in_markup_for_non_cogs_category(client, wtopology):
    """Mirrors test_row_and_editor_markup_wired_for_loan_field_toggle — the
    Outbound Shipping portion input starts disabled in the rendered markup
    for a row whose category isn't 'cogs_purchase'/'item_purchase' yet."""
    import re

    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    html = resp.data.decode()
    editor_match = re.search(rf'id="rq-editor-{row_id}".*?</tr>', html, re.DOTALL)
    assert editor_match is not None
    assert re.search(r'name="shipping_portion_idr"[^>]*disabled', editor_match.group(0))


# ---------------------------------------------------------------------------
# Manual eBay Account override for 'revenue_settlement' rows (2026-09-30).
# See CLAUDE.md's Prototype scope (a shared-Payoneer wallet-group's export
# can contain a settlement line for a not-yet-onboarded eBay account) and
# the real incident this closes: a Payoneer CSV row (Rp 242,408 / $14.79,
# Aug 24 2026) manually labeled 'revenue_settlement' with no ebay_account_id
# failed to post and required a one-off database script to unblock — this
# makes that fixable entirely through the Review Queue UI.
# ---------------------------------------------------------------------------


def test_ebay_account_dropdown_renders_with_real_active_accounts(client, wconn):
    """The new eBay Account override field must be populated from the same
    live list of active eBay accounts as the page's own top-of-page account
    filter (webapp.scoping.list_ebay_accounts) — never a hardcoded
    assumption about how many eBay accounts exist. Exercised against the
    real 2-shared/1-independent topology (see CLAUDE.md's Business model),
    not just the single-account prototype shape, so this is meaningfully
    tested with more than one option in the dropdown.
    """
    conn = wconn
    topo = seed_full_topology(conn)
    src_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=PERIOD,
        wallet_group_id=topo["wallet_groups"]["shared"],
    )
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        amount_idr=Decimal("242408"),
        source_type="payoneer_csv",
        wallet_group_id=topo["wallet_groups"]["shared"],
        category="revenue_settlement",
    )
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    assert resp.status_code == 200
    html = resp.data.decode()
    editor_match = re.search(rf'id="rq-editor-{row_id}".*?</tr>', html, re.DOTALL)
    assert editor_match is not None
    editor_html = editor_match.group(0)
    assert 'name="ebay_account_id"' in editor_html
    assert "eBay Account 1" in editor_html
    assert "eBay Account 2" in editor_html
    assert "eBay Account 3" in editor_html
    # Already labeled 'revenue_settlement' but with no real ebay_account_id
    # yet — the select must be enabled, not disabled.
    assert not re.search(r'name="ebay_account_id"[^>]*disabled', editor_html)


def test_ebay_account_field_disabled_in_markup_for_non_revenue_settlement_category(client, wtopology):
    """Mirrors the loan-repayment/shipping-portion disabled-by-default
    pattern — the eBay Account override only ever applies to
    'revenue_settlement' and must start disabled for any row not already
    labeled that way."""
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}")
    html = resp.data.decode()
    editor_match = re.search(rf'id="rq-editor-{row_id}".*?</tr>', html, re.DOTALL)
    assert editor_match is not None
    assert re.search(r'name="ebay_account_id"[^>]*disabled', editor_match.group(0))


def test_saving_revenue_settlement_with_selected_ebay_account_sets_it(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=PERIOD,
        wallet_group_id=topo["wallet_group_id"],
    )
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        amount_idr=Decimal("242408"),
        amount_usd_ref=Decimal("14.79"),
        source_type="payoneer_csv",
        wallet_group_id=topo["wallet_group_id"],
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "revenue_settlement",
            "ebay_account_id": str(topo["ebay_account_id"]),
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "revenue_settlement"
    assert row.ebay_account_id == topo["ebay_account_id"]
    assert row.posted_at is None  # saving a label never posts by itself


def test_blank_ebay_account_selection_never_overwrites_an_already_set_value(client, wtopology):
    """A row whose ebay_account_id was already correctly resolved (e.g. by
    auto-matching) must never have it silently cleared or changed by a
    save that leaves the new field blank — the "never silently guess or
    overwrite" rule from CLAUDE.md applies here too."""
    conn, topo = wtopology
    src_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=PERIOD,
        wallet_group_id=topo["wallet_group_id"],
    )
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        amount_idr=Decimal("242408"),
        source_type="payoneer_csv",
        wallet_group_id=topo["wallet_group_id"],
        category="revenue_settlement",
        ebay_account_id=topo["ebay_account_id"],
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "revenue_settlement",
            "consignor_item_ref": "some unrelated note",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.ebay_account_id == topo["ebay_account_id"]  # untouched by the blank submission
    assert row.consignor_item_ref == "some unrelated note"  # the rest of the save still went through


def test_revenue_settlement_can_save_without_selecting_an_ebay_account(client, wtopology):
    """Saving 'revenue_settlement' with no account chosen must NOT be
    blocked at save time — it should simply fail later, at posting time,
    exactly as it does today (see PostResult.failed_to_post)."""
    conn, topo = wtopology
    src_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=PERIOD,
        wallet_group_id=topo["wallet_group_id"],
    )
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        amount_idr=Decimal("242408"),
        source_type="payoneer_csv",
        wallet_group_id=topo["wallet_group_id"],
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "revenue_settlement", "period": PERIOD.isoformat()},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category == "revenue_settlement"
    assert row.ebay_account_id is None
    assert row.posted_at is None


def test_ebay_account_selection_rejected_for_non_revenue_settlement_category(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    row_id = make_review_queue_row(conn, source_document_id=src_id, transaction_date=DAY)
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "operating_expense",
            "ebay_account_id": str(topo["ebay_account_id"]),
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely, never silently saved with the wrong category


def test_ebay_account_selection_rejects_unknown_account_id(client, wtopology):
    conn, topo = wtopology
    src_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=PERIOD,
        wallet_group_id=topo["wallet_group_id"],
    )
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        amount_idr=Decimal("242408"),
        source_type="payoneer_csv",
        wallet_group_id=topo["wallet_group_id"],
    )
    conn.commit()

    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "revenue_settlement",
            "ebay_account_id": "999999",
            "period": PERIOD.isoformat(),
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert row.category is None  # rejected entirely — never a category set alongside a bogus account


def test_setting_ebay_account_then_syncing_posts_the_revenue_settlement(client, wtopology):
    """End-to-end reproduction of the real incident and its fix: a Payoneer
    CSV row (Rp 242,408 / $14.79, shaped like the real Aug 24 2026 case)
    manually labeled 'revenue_settlement' with no ebay_account_id fails to
    post via post_pending_rows' existing per-row failure isolation — then,
    once a human sets the eBay account through this new field, the very
    next sync posts it correctly. No direct database access required, unlike
    the real one-off unblock this closes.
    """
    conn, topo = wtopology
    src_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=PERIOD,
        wallet_group_id=topo["wallet_group_id"],
    )
    row_id = make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=DAY,
        amount_idr=Decimal("242408"),
        amount_usd_ref=Decimal("14.79"),
        source_type="payoneer_csv",
        wallet_group_id=topo["wallet_group_id"],
        raw_description="Payment from eBay (Additional Description: '', no matching expected payout)",
        category="revenue_settlement",
    )
    conn.commit()

    # Step 1: reproduce the real failure — no ebay_account_id yet, so the
    # row is structurally not postable.
    first_result = post_pending_rows(conn)
    conn.commit()
    assert first_result.failed_to_post == 1
    assert first_result.posted == 0
    failed_row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert failed_row.posted_at is None
    assert failed_row.posting_error_reason is not None
    assert failed_row.match_status == "needs_review"

    # Step 2: a human fixes it through the new UI field.
    resp = client.post(
        f"/review-queue/{row_id}",
        data={
            "category": "revenue_settlement",
            "ebay_account_id": str(topo["ebay_account_id"]),
            "period": PERIOD.isoformat(),
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    # Step 3: the next sync now posts it correctly.
    second_result = post_pending_rows(conn)
    conn.commit()
    assert second_result.posted == 1
    assert second_result.failed_to_post == 0

    posted_row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).first()
    assert posted_row.posted_at is not None
    assert posted_row.ebay_account_id == topo["ebay_account_id"]
    assert posted_row.match_status == "matched"
    assert posted_row.posting_error_reason is None

    lines = lines_by_code(conn, posted_row.posted_journal_entry_id)
    assert "EBAY_WALLET" in lines
    assert "PAYONEER_WALLET" in lines
    assert lines["EBAY_WALLET"][0].credit_amount_idr == Decimal("242408")
    assert lines["PAYONEER_WALLET"][0].debit_amount_idr == Decimal("242408")
