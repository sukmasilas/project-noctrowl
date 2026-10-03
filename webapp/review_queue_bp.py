"""Review Queue screen — the one screen the user labels bank/Payoneer
transaction data on. See docs/design/ui-ux-design.md Screen 1 and
docs/design/milestone-4-web-app-design.md §3.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, select, update

from ingestion.schema import review_queue
from webapp.db import get_db
from webapp.scoping import list_ebay_accounts, parse_period

bp = Blueprint("review_queue", __name__, url_prefix="/review-queue")

CATEGORY_OPTIONS = [
    ("revenue_settlement", "Revenue Settlement"),
    # Added 2026-09-29 — a real gap found in live use: a Payoneer CSV row,
    # "Card charge (PAYPAL *CHRISNELFRANCO)", -Rp 1,899,765 (-$115.91),
    # confirmed by the user as a refund issued to a customer, had no
    # category that fit. Wires to the existing ledger.posting.post_refund()
    # (Sales Returns & Allowances, contra-revenue — never netted into Sales
    # Revenue), previously only reachable via the eBay-CSV Refund path.
    # Placed next to Revenue Settlement — same revenue side of the P&L.
    ("customer_refund", "Customer Refund"),
    # Added 2026-10-03 — inflow mirror of Customer Refund: eBay returns money
    # after the seller wins a buyer dispute. Manual only, no keyword rule.
    ("ebay_dispute_won", "eBay Dispute Won (refund reversed)"),
    ("cogs_purchase", "COGS"),
    # Added 2026-09-10 — more specific COGS sub-labels (see CLAUDE.md and
    # ledger/chart_of_accounts.py's COGS account). All three post to the
    # SAME existing COGS account as 'cogs_purchase' below (kept, unchanged,
    # for when the distinction isn't relevant/known) — a labeling/
    # traceability improvement only, not a new expense type. "Inbound"
    # here means freight-in (getting PURCHASED inventory delivered to the
    # business) — never confused with 'shipping_cost' above, which is
    # OUTBOUND shipping to a customer.
    # 'item_purchase'/'cogs_purchase' (not 'inbound_shipping'/
    # 'item_purchase_and_inbound_shipping' — those already represent a
    # DIFFERENT, inbound freight-in shipping concept blended into the same
    # COGS line, see below) reveal an optional "Outbound Shipping portion"
    # field in the editor (see review_queue.html) — a single bundled bank
    # payment covering both an item purchase and outbound shipping to a
    # customer, split into a COGS line + a Shipping Cost line instead of
    # posting the whole amount to COGS. Added 2026-09-29 — real trigger: a
    # Master Account bank line, "TRSF E-BANKING DB ... / BANK NEO COM ...",
    # -Rp 4,140,000 (Rp 2,140,000 item purchase + Rp 2,000,000 outbound
    # shipping), confirmed by the user as a recurring bundling pattern. See
    # ledger.posting.post_cogs_purchase_with_shipping_split.
    ("item_purchase", "COGS — Item Purchase"),
    ("inbound_shipping", "COGS — Inbound Shipping / Freight-In"),
    ("item_purchase_and_inbound_shipping", "COGS — Item Purchase + Inbound Shipping"),
    # Added 2026-09-29 — a real gap found in live use, same underlying
    # mechanism for two confirmed examples: (1) an employee's unspent
    # cash-advance excess refunded back the day after a purchase already
    # posted as COGS, (2) a supplier refund for inventory that couldn't be
    # delivered. Both reduce a COGS figure already posted or about to be —
    # ONE unified category (the raw bank description already documents
    # which reason applies), not split by reason. Posts via the new
    # ledger.posting.post_cogs_refund() (a credit reducing the existing COGS
    # account — no new GL line). Placed next to the other COGS labels above.
    ("cogs_refund", "COGS Refund / Purchase Return"),
    # Added 2026-09-10 — the existing PAYROLL account had no review-queue
    # category/posting path at all until now (same pre-existing gap
    # CONTRACT_LABOR/SHIPPING_COST each had before their own category was
    # added). Selecting this reveals an optional "loan repayment" field in
    # the editor below (see review_queue.html) — see CLAUDE.md's Core
    # accounting rules and ledger.posting.post_payroll_with_loan_repayment.
    ("payroll", "Payroll"),
    ("operating_expense", "Operating Expense"),
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
    # Added 2026-10-03 — shipping provider refunds an outbound shipping charge
    # (credit reducing SHIPPING_COST). Manual only, no keyword rule.
    ("shipping_cost_refund", "Shipping Cost Refund"),
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
    # Added 2026-09-05 alongside the new CONTRACT_LABOR operating-expense
    # account (see ledger/chart_of_accounts.py and CLAUDE.md) — same reason
    # 'interest_income' needed its own category above: a plain
    # 'operating_expense' label always resolves to GENERAL_OPEX (see
    # ingestion.matching._post_one_row), which would misclassify a real
    # contract-labor cost instead of posting it to its own dedicated line.
    ("contract_labor", "Contract Labor"),
    # Added 2026-09-24 — a real, roughly-monthly recurring cost: the business
    # periodically pays for a team meal (e.g. a QR-code debit to a local
    # cafe — the real trigger, a -Rp 520,000 "MLINJO CAF" line). Needs its
    # own category for the same reason 'contract_labor'/'shipping_cost'/
    # 'packaging_supplies' did: a plain 'operating_expense' label always
    # resolves to GENERAL_OPEX (see ingestion.matching._post_one_row), which
    # would bury a real, recurring cost the user wants separately visible.
    # Deliberately NO keyword auto-match rule for this — same reasoning as
    # 'packaging_supplies': a QR/debit line to a cafe or restaurant could
    # plausibly be something else (a business meeting, a different kind of
    # expense) with no way to tell from the raw bank line alone, so this
    # always stays a human-selected, per-transaction judgment call in the
    # Review Queue.
    ("staff_meals_welfare", "Staff Meals & Welfare"),
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
    ("internal_transfer", "Internal Transfer"),
    ("consignment_payout", "Consignment Payout"),
    # Added 2026-09-10 — a real, one-off loan disbursement to an employee
    # (see ledger/chart_of_accounts.py's EMPLOYEE_LOAN_RECEIVABLE note).
    # Posts to that new asset account, never P&L. Uses the same "Consignor/
    # Item Ref" field below for the employee's name (traceability only, one
    # aggregate account, same pattern as Consignor Payable).
    ("employee_loan_disbursement", "Employee Loan Disbursement"),
    # Added 2026-09-29 — the INITIAL down-payment/deposit paid toward
    # inventory not yet received (real trigger: a Master Account bank line,
    # "DP Box op / FARIZ PRADANA", -Rp 9,840,000, confirmed by the user as a
    # deposit for goods not yet received). Posts to the new
    # INVENTORY_DEPOSITS asset account (see ledger/chart_of_accounts.py) via
    # ledger.posting.post_inventory_deposit — deliberately NOT COGS yet,
    # since nothing has been received. Placed next to
    # 'employee_loan_disbursement' (same non-P&L, balance-sheet-only,
    # aggregate-account-plus-per-transaction-reference grouping — see
    # test_category_options_follow_pl_statement_order). Selecting this
    # reveals the same "Consignor/Item Ref" field below, reused here as the
    # deposit reference (required — identifies which outstanding deposit a
    # later conversion-to-COGS should clear; see
    # webapp/inventory_deposits_bp.py, which is where that later conversion
    # actually happens — NOT a review-queue category, since it has no cash
    # movement/bank line of its own to attach one to).
    ("inventory_deposit", "Inventory Deposit (Advance to Supplier)"),
    ("owners_draw", "Owner's Draw"),
    ("owners_contribution", "Owner's Contribution"),
    ("other", "Other"),
]

# Lookup used to build the AJAX JSON response's human-readable
# "category_label" (see label_row below) — same labels shown in the
# <select>, just addressable by code without re-walking the list.
CATEGORY_LABELS = dict(CATEGORY_OPTIONS)


@bp.route("/")
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
        # Added 2026-09-30, alongside the new eBay Account editor field
        # (see label_row) — lets the row editor show "(currently: <name>)"
        # for a row that already has a real ebay_account_id, without
        # pre-selecting it in the dropdown itself (see review_queue.html).
        ebay_account_names={a.id: a.name for a in accounts},
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


def _wants_json() -> bool:
    """AJAX-style requests set this header explicitly (see
    review_queue.html's fetch() handler) — a plain browser form POST never
    sends it, so this is a reliable, additive signal to branch the response
    shape on without touching the existing redirect-based behavior at all.
    """
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _fail(message: str, status: int = 400):
    """Shared failure path for label_row: JSON for the AJAX caller (so
    fetch() can distinguish success/failure cleanly via a non-2xx status),
    the original flash-and-redirect for a plain form submission. Validation
    rules themselves are unchanged — this only changes how the outcome is
    delivered.
    """
    if _wants_json():
        return jsonify({"success": False, "message": message}), status
    flash(message, "error")
    return _back_to_queue(request)


@bp.route("/<int:row_id>", methods=["POST"])
def label_row(row_id: int):
    conn = get_db()
    category = request.form.get("category") or None
    # Added 2026-09-30 — a human-set eBay account for a 'revenue_settlement'
    # row whose auto-matching couldn't determine one (see
    # ingestion/matching.py's 'revenue_settlement' branch, which needs a real
    # ebay_account_id to know which eBay account's wallet to debit, and
    # CLAUDE.md's Prototype scope note about a shared-Payoneer wallet-group's
    # export containing a settlement line for a not-yet-onboarded account).
    # review_queue.ebay_account_id already exists and is already nullable —
    # this is the first time it's exposed as something a human can fill in
    # directly, not a new column.
    #
    # Deliberately validated and staged here, but only merged into
    # update_values below if a real selection was made (see that comment) —
    # never included in the UPDATE at all when left blank, so a blank
    # submission can NEVER null out or silently overwrite an already-correct
    # ebay_account_id (e.g. one auto-matched by rule (a)). This is the
    # "safer option" CLAUDE.md's own posture ("never silently guess or
    # overwrite") calls for: correcting/setting the value always requires an
    # explicit, visible selection by a human, not a default.
    raw_ebay_account_id = (request.form.get("ebay_account_id") or "").strip()
    ebay_account_id_override: int | None = None
    if raw_ebay_account_id:
        if category != "revenue_settlement":
            return _fail("eBay Account only applies to the Revenue Settlement category.")
        try:
            ebay_account_id_override = int(raw_ebay_account_id)
        except ValueError:
            return _fail("eBay Account must be a valid selection.")
        valid_ebay_account_ids = {a.id for a in list_ebay_accounts(conn)}
        if ebay_account_id_override not in valid_ebay_account_ids:
            return _fail("Please choose a valid eBay account.")
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
        return _fail("Please choose a valid category.")

    # Added 2026-09-10 — the optional embedded employee-loan-repayment split
    # on a 'payroll' row (see CLAUDE.md's Core accounting rules and
    # ledger.posting.post_payroll_with_loan_repayment). Deliberately only
    # ever set by an explicit human entry here, never inferred — matches
    # every other "never silently guess" rule in this file.
    raw_loan_repayment = (request.form.get("loan_repayment_amount_idr") or "").strip()
    loan_repayment_amount_idr = None
    if raw_loan_repayment:
        if category != "payroll":
            return _fail("Loan repayment amount only applies to the Payroll category.")
        try:
            loan_repayment_amount_idr = Decimal(raw_loan_repayment)
        except InvalidOperation:
            return _fail("Loan repayment amount must be a number (e.g. 1500000).")
        if loan_repayment_amount_idr <= 0:
            return _fail("Loan repayment amount must be greater than zero.")
        if not consignor_item_ref:
            return _fail(
                "Please also fill in the employee's name (Consignor/Item Ref field) "
                "when specifying a loan repayment amount — it's needed to know whose "
                "loan balance to draw down."
            )

    # Added 2026-09-29 — the optional bundled-item-purchase +
    # outbound-shipping split on a 'cogs_purchase'/'item_purchase' row (see
    # CLAUDE.md and ledger.posting.post_cogs_purchase_with_shipping_split).
    # Deliberately only ever set by an explicit human entry here, never
    # inferred — same "never silently guess" rule as every other category
    # in this file. Unlike loan_repayment_amount_idr (which is ADDITIVE, no
    # upper bound), this field is SUBTRACTIVE — carved OUT of the row's own
    # total amount — so it needs its own "strictly less than the total"
    # check that loan_repayment_amount_idr never needed.
    raw_shipping_portion = (request.form.get("shipping_portion_idr") or "").strip()
    shipping_portion_idr = None
    if raw_shipping_portion:
        if category not in ("cogs_purchase", "item_purchase"):
            return _fail(
                "Outbound Shipping portion only applies to the COGS / COGS — Item Purchase "
                "categories (a plain item purchase with no shipping already implied) — not to "
                "COGS — Inbound Shipping / Freight-In or COGS — Item Purchase + Inbound "
                "Shipping, which already represent a different, inbound shipping concept."
            )
        try:
            shipping_portion_idr = Decimal(raw_shipping_portion)
        except InvalidOperation:
            return _fail("Outbound Shipping portion must be a number (e.g. 2000000).")
        if shipping_portion_idr <= 0:
            return _fail("Outbound Shipping portion must be greater than zero.")
        existing_row = conn.execute(
            select(review_queue.c.amount_idr).where(review_queue.c.id == row_id)
        ).first()
        if existing_row is None:
            return _fail("Could not find that review-queue row.", status=404)
        total_amount = abs(existing_row.amount_idr)
        if shipping_portion_idr >= total_amount:
            return _fail(
                "Outbound Shipping portion must be less than this line's total amount "
                f"({total_amount}) — there must be something left over for COGS."
            )

    # QA-found gap (2026-09-10): 'employee_loan_disbursement' ALWAYS needs a
    # real employee reference — same reasoning as the loan-repayment check
    # above, and CLAUDE.md's existing Consignor Payable traceability rule.
    # Without this, a blank Consignor/Item Ref would previously have been
    # silently posted as a placeholder "unspecified" employee reference
    # (see ingestion/matching.py's _missing_employee_ref_reason, the
    # matching defense-in-depth backstop for this same requirement).
    if category == "employee_loan_disbursement" and not consignor_item_ref:
        return _fail(
            "Please fill in the employee's name (Consignor/Item Ref field) for an "
            "Employee Loan Disbursement — it's needed to know whose loan this is."
        )

    # Added 2026-09-29 (new INVENTORY_DEPOSITS asset account) — same
    # reasoning as 'employee_loan_disbursement' above: an aggregate asset
    # account with no per-supplier sub-ledger needs a real per-transaction
    # deposit reference so it can be found and resolved later (see
    # webapp/inventory_deposits_bp.py). Mirrored as a defense-in-depth
    # backstop in ingestion.matching._missing_employee_ref_reason.
    if category == "inventory_deposit" and not consignor_item_ref:
        return _fail(
            "Please fill in a deposit reference (Consignor/Item Ref field) for an Inventory "
            "Deposit — it's needed to identify and later resolve this specific deposit."
        )

    # Added 2026-09-30 — see the ebay_account_id_override comment above: the
    # key is only present in this dict (and therefore only touched by the
    # UPDATE at all) when a human explicitly picked a real account. A blank
    # submission leaves review_queue.ebay_account_id completely untouched —
    # whether it was already NULL (still unresolved, unchanged) or already a
    # real value (already-correct, never silently cleared/overwritten).
    update_values = {
        "category": category,
        "consignor_item_ref": consignor_item_ref,
        "loan_repayment_amount_idr": loan_repayment_amount_idr,
        "shipping_portion_idr": shipping_portion_idr,
        "labeled_at": _dt.datetime.now(_dt.timezone.utc),
        # 2026-09-29 (AJAX Posted-cell fix): a row can reach this route a
        # second time already carrying a stale sign_mismatch_reason /
        # missing_reference_reason / posting_error_reason from an earlier
        # failed post_pending_rows attempt (see ingestion/matching.py —
        # that's exactly what flips it back to needs_review with a reason
        # set, and not posted_at, so the editor here is reachable again).
        # Without clearing these, a freshly-relabeled row would still
        # show its OLD red "Not posted — ..." reason (review_queue.html's
        # Posted-column chain checks these before labeled_at) even though
        # nothing has attempted to re-post it yet under the new label.
        # Cleared here so a successful label_row response always means
        # exactly "labeled, not posted, no error reason" — the same
        # clearing post_pending_rows itself already does on an actual
        # successful post (see its own sign_mismatch_reason=None etc.) —
        # the next sync re-validates and will re-set these if the new
        # label still doesn't work.
        "sign_mismatch_reason": None,
        "missing_reference_reason": None,
        "posting_error_reason": None,
    }
    if ebay_account_id_override is not None:
        update_values["ebay_account_id"] = ebay_account_id_override

    # The posted_at IS NULL guard is a deliberate, explicit match to
    # CLAUDE.md's "corrections to an already-posted row are out of scope"
    # rule (and the DB's own ck_review_queue_no_post_without_category /
    # posted_journal_entry_id-requires-posted_at constraints) — this route
    # must never silently edit a row that's already posted to the ledger.
    result = conn.execute(
        update(review_queue)
        .where(review_queue.c.id == row_id)
        .where(review_queue.c.posted_at.is_(None))
        .values(**update_values)
    )
    if result.rowcount == 0:
        existing = conn.execute(
            select(review_queue.c.posted_at).where(review_queue.c.id == row_id)
        ).first()
        conn.rollback()
        if existing is not None and existing.posted_at is not None:
            return _fail(
                "This row has already posted to the ledger — corrections to a posted row "
                "are not supported yet (see CLAUDE.md's deferred-corrections note).",
                status=409,
            )
        return _fail("Could not find that review-queue row.", status=404)

    conn.commit()
    success_message = "Saved — queued for the next sync run."
    if _wants_json():
        return jsonify(
            {
                "success": True,
                "message": success_message,
                "category_label": CATEGORY_LABELS.get(category, category),
            }
        )
    flash(success_message, "success")
    return _back_to_queue(request, row_id=row_id)


def _back_to_queue(req, row_id: int | None = None):
    target = url_for(
        "review_queue.index",
        period=req.form.get("period") or req.args.get("period"),
        account_id=req.form.get("account_id") or req.args.get("account_id"),
    )
    # Anchors the redirect to the row the user just saved (Fix 3, 2026-09-25)
    # so a full page reload doesn't dump the user back at the top of a long
    # list — Flask's redirect()/url_for() don't support fragments natively,
    # so it's appended to the built URL directly.
    if row_id is not None:
        target = f"{target}#rq-row-{row_id}"
    return redirect(target)
