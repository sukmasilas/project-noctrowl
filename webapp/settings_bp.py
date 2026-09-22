"""Settings screens: Consignor Payout Tiers table, and (added 2026-09-10)
Employee Loans tracking table.

Consignor Payout Tiers: update-only on ``rate_percent`` for the 6 fixed
tier rows — see docs/design/ui-ux-design.md Screen 1.5 and
docs/design/milestone-4-web-app-design.md §5. No add/remove row UI (the
schedule is fixed), and no retroactive effect on already-posted
consignment sales — ``ledger.schema.consignment_sales.tier_rate_percent``
freezes whatever rate was actually used at confirmation time, independent
of anything this screen does afterward.

Employee Loans: an admin-editable table of loan TERMS (who, how much, the
monthly installment, when it started) — same "simple admin-editable table
in the app" pattern as Payout Tiers, but growable (new loans get added over
time, unlike the fixed 7-row tier schedule) — see ledger/schema.py's
``employee_loans`` table and CLAUDE.md's Core accounting rules. This screen
itself never posts anything to the ledger; the real disbursement/repayment
journal entries are posted via the Review Queue ('employee_loan_
disbursement' / 'payroll' categories — see ingestion/matching.py) and
reference the employee by the same free-text name recorded here. Remaining
balance is always COMPUTED live (original amount minus cumulative
repayment credits posted to EMPLOYEE_LOAN_RECEIVABLE for that employee
name, matched case/whitespace-insensitively against
``journal_lines.consignor_item_ref``) — never a stored running total that
could drift out of sync with what's actually posted.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select, update

from ledger.entities import get_account_id
from ledger.errors import UnknownAccountInstanceError
from ledger.schema import consignor_payout_tiers, employee_loans, journal_lines
from webapp.db import get_db

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/payout-tiers")
def payout_tiers():
    conn = get_db()
    rows = conn.execute(select(consignor_payout_tiers).order_by(consignor_payout_tiers.c.display_order)).all()
    return render_template("settings_payout_tiers.html", rows=rows)


@bp.route("/payout-tiers/<int:tier_id>", methods=["POST"])
def update_payout_tier(tier_id: int):
    conn = get_db()
    row = conn.execute(
        select(consignor_payout_tiers).where(consignor_payout_tiers.c.id == tier_id)
    ).first()
    if row is None:
        flash("No such tier row.", "error")
        return redirect(url_for("settings.payout_tiers"))

    if row.requires_manual_contact:
        flash("The $7,500+ tier has no fixed rate — it always requires manual contact, never auto-applied.", "error")
        return redirect(url_for("settings.payout_tiers"))

    raw_rate = request.form.get("rate_percent", "").strip()
    try:
        rate = Decimal(raw_rate)
    except (InvalidOperation, ValueError):
        flash("Rate must be a number (e.g. 72.00).", "error")
        return redirect(url_for("settings.payout_tiers"))
    if not (Decimal("0") < rate <= Decimal("100")):
        flash("Rate must be between 0 and 100.", "error")
        return redirect(url_for("settings.payout_tiers"))

    conn.execute(
        update(consignor_payout_tiers).where(consignor_payout_tiers.c.id == tier_id).values(rate_percent=rate)
    )
    conn.commit()
    flash(
        "Tier rate updated — this only affects the lookup used for sales confirmed from now on, "
        "never anything already posted.",
        "success",
    )
    return redirect(url_for("settings.payout_tiers"))


# ---------------------------------------------------------------------------
# Employee Loans (added 2026-09-10)
# ---------------------------------------------------------------------------


def _repayments_by_employee(conn) -> dict[str, Decimal]:
    """Cumulative credits posted to EMPLOYEE_LOAN_RECEIVABLE, grouped by the
    employee reference on each line (``journal_lines.consignor_item_ref`` —
    see that column's reuse for this purpose in ledger/posting.py's
    ``post_payroll_with_loan_repayment``). Keyed by a normalized (stripped +
    lowercased) name so a real employee_loans.employee_name entry matches
    regardless of minor case/whitespace differences in how a reviewer typed
    it on a review-queue row — matching is still by the SAME real name, just
    forgiving of trivial formatting differences, not fuzzy/guessed.

    Returns an empty dict (no crash) if EMPLOYEE_LOAN_RECEIVABLE doesn't
    exist yet in this database (e.g. before ``scripts/ensure_employee_loan_
    receivable_account.py`` has been run) — same "no data yet" tolerance
    CLAUDE.md's Definition of Done requires everywhere else.
    """
    try:
        account_id = get_account_id(conn, "EMPLOYEE_LOAN_RECEIVABLE")
    except UnknownAccountInstanceError:
        return {}

    rows = conn.execute(
        select(
            journal_lines.c.consignor_item_ref,
            func.coalesce(func.sum(journal_lines.c.credit_amount_idr), 0).label("total_credit"),
        )
        .where(journal_lines.c.account_id == account_id)
        .where(journal_lines.c.credit_amount_idr > 0)
        .group_by(journal_lines.c.consignor_item_ref)
    ).all()
    totals: dict[str, Decimal] = {}
    for r in rows:
        key = (r.consignor_item_ref or "").strip().lower()
        totals[key] = totals.get(key, Decimal("0")) + Decimal(r.total_credit)
    return totals


@bp.route("/employee-loans")
def employee_loans_index():
    conn = get_db()
    rows = conn.execute(select(employee_loans).order_by(employee_loans.c.loan_start_date, employee_loans.c.id)).all()
    repayments = _repayments_by_employee(conn)

    loans = []
    for row in rows:
        key = (row.employee_name or "").strip().lower()
        repaid = repayments.get(key, Decimal("0"))
        remaining = row.original_amount_idr - repaid
        loans.append(
            {
                "id": row.id,
                "employee_name": row.employee_name,
                "original_amount_idr": row.original_amount_idr,
                "monthly_installment_idr": row.monthly_installment_idr,
                "loan_start_date": row.loan_start_date,
                "repaid_idr": repaid,
                "remaining_idr": remaining,
            }
        )
    return render_template("settings_employee_loans.html", loans=loans)


@bp.route("/employee-loans", methods=["POST"])
def create_employee_loan():
    conn = get_db()
    employee_name = (request.form.get("employee_name") or "").strip()
    raw_original = (request.form.get("original_amount_idr") or "").strip()
    raw_installment = (request.form.get("monthly_installment_idr") or "").strip()
    raw_start_date = (request.form.get("loan_start_date") or "").strip()

    if not employee_name:
        flash("Employee name is required.", "error")
        return redirect(url_for("settings.employee_loans_index"))

    try:
        original_amount = Decimal(raw_original)
        installment = Decimal(raw_installment)
    except InvalidOperation:
        flash("Loan amount and monthly installment must both be numbers (e.g. 27000000).", "error")
        return redirect(url_for("settings.employee_loans_index"))
    if original_amount <= 0 or installment <= 0:
        flash("Loan amount and monthly installment must both be greater than zero.", "error")
        return redirect(url_for("settings.employee_loans_index"))

    try:
        start_date = _dt.date.fromisoformat(raw_start_date)
    except ValueError:
        flash("Loan start date must be a valid date.", "error")
        return redirect(url_for("settings.employee_loans_index"))

    conn.execute(
        employee_loans.insert().values(
            employee_name=employee_name,
            original_amount_idr=original_amount,
            monthly_installment_idr=installment,
            loan_start_date=start_date,
        )
    )
    conn.commit()
    flash(f"Added loan record for {employee_name}.", "success")
    return redirect(url_for("settings.employee_loans_index"))


@bp.route("/employee-loans/<int:loan_id>", methods=["POST"])
def update_employee_loan(loan_id: int):
    conn = get_db()
    row = conn.execute(select(employee_loans).where(employee_loans.c.id == loan_id)).first()
    if row is None:
        flash("No such employee loan record.", "error")
        return redirect(url_for("settings.employee_loans_index"))

    employee_name = (request.form.get("employee_name") or "").strip()
    raw_original = (request.form.get("original_amount_idr") or "").strip()
    raw_installment = (request.form.get("monthly_installment_idr") or "").strip()
    raw_start_date = (request.form.get("loan_start_date") or "").strip()

    if not employee_name:
        flash("Employee name is required.", "error")
        return redirect(url_for("settings.employee_loans_index"))

    try:
        original_amount = Decimal(raw_original)
        installment = Decimal(raw_installment)
    except InvalidOperation:
        flash("Loan amount and monthly installment must both be numbers.", "error")
        return redirect(url_for("settings.employee_loans_index"))
    if original_amount <= 0 or installment <= 0:
        flash("Loan amount and monthly installment must both be greater than zero.", "error")
        return redirect(url_for("settings.employee_loans_index"))

    try:
        start_date = _dt.date.fromisoformat(raw_start_date)
    except ValueError:
        flash("Loan start date must be a valid date.", "error")
        return redirect(url_for("settings.employee_loans_index"))

    conn.execute(
        update(employee_loans)
        .where(employee_loans.c.id == loan_id)
        .values(
            employee_name=employee_name,
            original_amount_idr=original_amount,
            monthly_installment_idr=installment,
            loan_start_date=start_date,
        )
    )
    conn.commit()
    flash(
        f"Updated loan record for {employee_name} — this record is for tracking/cross-reference "
        "only and never retroactively changes anything already posted to the ledger.",
        "success",
    )
    return redirect(url_for("settings.employee_loans_index"))
