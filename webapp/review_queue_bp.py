"""Review Queue screen — the one screen the user labels bank/Payoneer
transaction data on. See docs/design/ui-ux-design.md Screen 1 and
docs/design/milestone-4-web-app-design.md §3.
"""
from __future__ import annotations

import datetime as _dt

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
    ("shipping_cost", "Shipping Cost"),
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
    consignor_item_ref = request.form.get("consignor_item_ref") or None
    valid_categories = {c for c, _ in CATEGORY_OPTIONS}
    if category not in valid_categories:
        flash("Please choose a valid category.", "error")
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
