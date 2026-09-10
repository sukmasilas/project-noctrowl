"""Review Queue screen — the one screen the user labels bank/Payoneer
transaction data on. See docs/design/ui-ux-design.md Screen 1 and
docs/design/milestone-4-web-app-design.md §3.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select, update

from ingestion.schema import review_queue
from webapp.auth import login_required
from webapp.db import get_db
from webapp.scoping import list_ebay_accounts, parse_period

bp = Blueprint("review_queue", __name__, url_prefix="/review-queue")

CATEGORY_OPTIONS = [
    ("revenue_settlement", "Revenue Settlement"),
    ("cogs_purchase", "COGS"),
    ("internal_transfer", "Internal Transfer"),
    ("consignment_payout", "Consignment Payout"),
    ("operating_expense", "Operating Expense"),
    ("owners_draw", "Owner's Draw"),
    ("owners_contribution", "Owner's Contribution"),
    # Added 2026-09-02 alongside the bank_keyword_rules seeding fix (see
    # ingestion/seed.py, ledger.posting.post_interest_income_line): without
    # this, a human could never correctly hand-label a leftover
    # interest-related Needs Review row (e.g. the Bridging account's "Pajak
    # rekening" line, which the seeded keyword rules deliberately do NOT
    # auto-match — see ingestion/seed.py's note) — they'd be forced to
    # mislabel it 'operating_expense', posting it to GENERAL_OPEX instead of
    # netting it against INTEREST_INCOME as CLAUDE.md's Chart of accounts
    # section requires.
    ("interest_income", "Interest Income"),
    # Added 2026-09-05 alongside the new CONTRACT_LABOR operating-expense
    # account (see ledger/chart_of_accounts.py and CLAUDE.md) — same reason
    # 'interest_income' needed its own category above: a plain
    # 'operating_expense' label always resolves to GENERAL_OPEX (see
    # ingestion.matching._post_one_row), which would misclassify a real
    # contract-labor cost instead of posting it to its own dedicated line.
    ("contract_labor", "Contract Labor"),
    # Added 2026-09-09 — Kurasi is a confirmed real shipping vendor (every
    # bank line whose raw description contains "KURASI" is a shipping cost,
    # no exceptions). Same reason 'contract_labor' needed its own category:
    # a plain 'operating_expense' label always resolves to GENERAL_OPEX (see
    # ingestion.matching._post_one_row), which would misclassify a real
    # shipping cost instead of posting it to the dedicated SHIPPING_COST
    # account that already exists in the chart of accounts.
    #
    # Label only (2026-09-10, Main-agent's brief): renamed from plain
    # "Shipping Cost" to make the OUTBOUND direction explicit in the
    # dropdown, now that COGS also has its own "Inbound Shipping" label
    # below — the category CODE and the account it posts to (SHIPPING_COST)
    # are UNCHANGED, this is purely a display-label clarity improvement, not
    # a reclassification. Kurasi (and this category generally) is confirmed
    # genuinely outbound (to customers) — never touched by this change.
    ("shipping_cost", "Outbound Shipping (to Customer)"),
    # Added 2026-09-10 — more specific COGS sub-labels (see CLAUDE.md and
    # ledger/chart_of_accounts.py's COGS account). All three post to the
    # SAME existing COGS account as 'cogs_purchase' below (kept, unchanged,
    # for when the distinction isn't relevant/known) — a labeling/
    # traceability improvement only, not a new expense type. "Inbound"
    # here means freight-in (getting PURCHASED inventory delivered to the
    # business) — never confused with 'shipping_cost' above, which is
    # OUTBOUND shipping to a customer.
    ("item_purchase", "COGS — Item Purchase"),
    ("inbound_shipping", "COGS — Inbound Shipping / Freight-In"),
    ("item_purchase_and_inbound_shipping", "COGS — Item Purchase + Inbound Shipping"),
    # Added 2026-09-10 — the existing PAYROLL account had no review-queue
    # category/posting path at all until now (same pre-existing gap
    # CONTRACT_LABOR/SHIPPING_COST each had before their own category was
    # added). Selecting this reveals an optional "loan repayment" field in
    # the editor below (see review_queue.html) — see CLAUDE.md's Core
    # accounting rules and ledger.posting.post_payroll_with_loan_repayment.
    ("payroll", "Payroll"),
    # Added 2026-09-10 — a real, one-off loan disbursement to an employee
    # (see ledger/chart_of_accounts.py's EMPLOYEE_LOAN_RECEIVABLE note).
    # Posts to that new asset account, never P&L. Uses the same "Consignor/
    # Item Ref" field below for the employee's name (traceability only, one
    # aggregate account, same pattern as Consignor Payable).
    ("employee_loan_disbursement", "Employee Loan Disbursement"),
    # Added 2026-09-10 — some real Shopee/Tokopedia (and possibly other
    # vendor) purchases are for packaging supplies (boxes, bubble wrap, poly
    # mailers, etc.), not inventory items, and need their own category for
    # the same reason 'contract_labor'/'shipping_cost' did: a plain
    # 'operating_expense' label always resolves to GENERAL_OPEX (see
    # ingestion.matching._post_one_row), which would misclassify a real
    # packaging cost instead of posting it to its own dedicated line.
    # Deliberately NO keyword auto-match rule for this — the user confirmed
    # the same Shopee/Tokopedia bank line could be EITHER an item purchase
    # OR packaging supplies (no way to tell from the raw description alone),
    # so this always stays a human-selected, per-transaction judgment call
    # in the Review Queue.
    ("packaging_supplies", "Packaging Supplies"),
    ("other", "Other"),
]


@bp.route("/")
@login_required
def index():
    conn = get_db()
    accounts = list_ebay_accounts(conn)
    period_month = parse_period(request.args.get("period"), conn)
    account_id = request.args.get("account_id", type=int)
    status_filter = request.args.get("status", "all")
    source_filter = request.args.get("source", "all")

    query = select(review_queue).where(
        review_queue.c.transaction_date >= period_month,
        review_queue.c.transaction_date < _next_month(period_month),
    )
    if account_id:
        account = next((a for a in accounts if a.id == account_id), None)
        if account is not None:
            query = query.where(
                (review_queue.c.ebay_account_id == account_id)
                | (review_queue.c.wallet_group_id == account.wallet_group_id)
            )
    if status_filter in ("matched", "needs_review"):
        query = query.where(review_queue.c.match_status == status_filter)
    if source_filter in ("payoneer_csv", "bank_statement", "ebay_sales_csv"):
        query = query.where(review_queue.c.source_type == source_filter)

    rows = conn.execute(query.order_by(review_queue.c.transaction_date)).all()

    summary = conn.execute(
        select(
            review_queue.c.match_status,
            func.count().label("n"),
        )
        .where(
            review_queue.c.transaction_date >= period_month,
            review_queue.c.transaction_date < _next_month(period_month),
        )
        .group_by(review_queue.c.match_status)
    ).all()
    summary_counts = {r.match_status: r.n for r in summary}

    return render_template(
        "review_queue.html",
        accounts=accounts,
        selected_account_id=account_id,
        period_month=period_month,
        status_filter=status_filter,
        source_filter=source_filter,
        rows=rows,
        matched_count=summary_counts.get("matched", 0),
        needs_review_count=summary_counts.get("needs_review", 0),
        category_options=CATEGORY_OPTIONS,
    )


def _next_month(d: _dt.date) -> _dt.date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


@bp.route("/<int:row_id>", methods=["POST"])
@login_required
def label_row(row_id: int):
    conn = get_db()
    category = request.form.get("category") or None
    # QA-found gap (2026-09-10): stripped, same as loan_repayment_amount_idr
    # below — a whitespace-only submission ("   ") is truthy and previously
    # passed every "if not consignor_item_ref" check here unstripped,
    # letting a row save as falsely "labeled" with no real employee
    # reference (the post_pending_rows backstop still caught it before
    # posting, but the row misleadingly looked done in the UI until the
    # next sync re-flagged it).
    consignor_item_ref = (request.form.get("consignor_item_ref") or "").strip() or None
    valid_categories = {c for c, _ in CATEGORY_OPTIONS}
    if category not in valid_categories:
        flash("Please choose a valid category.", "error")
        return _back_to_queue(request)

    # Added 2026-09-10 — the optional embedded employee-loan-repayment split
    # on a 'payroll' row (see CLAUDE.md's Core accounting rules and
    # ledger.posting.post_payroll_with_loan_repayment). Deliberately only
    # ever set by an explicit human entry here, never inferred — matches
    # every other "never silently guess" rule in this file.
    raw_loan_repayment = (request.form.get("loan_repayment_amount_idr") or "").strip()
    loan_repayment_amount_idr = None
    if raw_loan_repayment:
        if category != "payroll":
            flash("Loan repayment amount only applies to the Payroll category.", "error")
            return _back_to_queue(request)
        try:
            loan_repayment_amount_idr = Decimal(raw_loan_repayment)
        except InvalidOperation:
            flash("Loan repayment amount must be a number (e.g. 1500000).", "error")
            return _back_to_queue(request)
        if loan_repayment_amount_idr <= 0:
            flash("Loan repayment amount must be greater than zero.", "error")
            return _back_to_queue(request)
        if not consignor_item_ref:
            flash(
                "Please also fill in the employee's name (Consignor/Item Ref field) "
                "when specifying a loan repayment amount — it's needed to know whose "
                "loan balance to draw down.",
                "error",
            )
            return _back_to_queue(request)

    # QA-found gap (2026-09-10): 'employee_loan_disbursement' ALWAYS needs a
    # real employee reference — same reasoning as the loan-repayment check
    # above, and CLAUDE.md's existing Consignor Payable traceability rule.
    # Without this, a blank Consignor/Item Ref would previously have been
    # silently posted as a placeholder "unspecified" employee reference
    # (see ingestion/matching.py's _missing_employee_ref_reason, the
    # matching defense-in-depth backstop for this same requirement).
    if category == "employee_loan_disbursement" and not consignor_item_ref:
        flash(
            "Please fill in the employee's name (Consignor/Item Ref field) for an "
            "Employee Loan Disbursement — it's needed to know whose loan this is.",
            "error",
        )
        return _back_to_queue(request)

    # The posted_at IS NULL guard is a deliberate, explicit match to
    # CLAUDE.md's "corrections to an already-posted row are out of scope"
    # rule (and the DB's own ck_review_queue_no_post_without_category /
    # posted_journal_entry_id-requires-posted_at constraints) — this route
    # must never silently edit a row that's already posted to the ledger.
    result = conn.execute(
        update(review_queue)
        .where(review_queue.c.id == row_id)
        .where(review_queue.c.posted_at.is_(None))
        .values(
            category=category,
            consignor_item_ref=consignor_item_ref,
            loan_repayment_amount_idr=loan_repayment_amount_idr,
            labeled_at=_dt.datetime.now(_dt.timezone.utc),
        )
    )
    if result.rowcount == 0:
        existing = conn.execute(
            select(review_queue.c.posted_at).where(review_queue.c.id == row_id)
        ).first()
        if existing is not None and existing.posted_at is not None:
            flash(
                "This row has already posted to the ledger — corrections to a posted row "
                "are not supported yet (see CLAUDE.md's deferred-corrections note).",
                "error",
            )
        else:
            flash("Could not find that review-queue row.", "error")
        conn.rollback()
        return _back_to_queue(request)

    conn.commit()
    flash("Saved — queued for the next sync run.", "success")
    return _back_to_queue(request)


def _back_to_queue(req):
    return redirect(
        url_for(
            "review_queue.index",
            period=req.form.get("period") or req.args.get("period"),
            account_id=req.form.get("account_id") or req.args.get("account_id"),
        )
    )
