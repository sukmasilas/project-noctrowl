"""Inventory Deposits screen — the one place a human resolves an outstanding
down-payment/deposit paid toward inventory not yet received (see
``ledger/chart_of_accounts.py``'s INVENTORY_DEPOSITS note and
CLAUDE.md's Business model / Stock model section).

WHY THIS IS ITS OWN SMALL SCREEN, NOT A REVIEW QUEUE CATEGORY (the brief's
own suggested starting point) OR A COPY OF THE EMPLOYEE LOANS SCREEN:

Every OTHER review-queue-driven posting in this codebase is triggered by a
real, staged bank/Payoneer/eBay CSV line — the amount posted is always that
line's own amount (see ``ingestion/matching.py``). Converting an outstanding
inventory deposit to COGS once goods actually arrive
(``ledger.posting.post_inventory_deposit_received``) is fundamentally
different: NO cash moves at that moment — a human is simply confirming "the
goods showed up," which may happen with no accompanying bank line at all
(the deposit could have covered the full purchase price, with nothing left
to pay on delivery). Modeling this as a review-queue category would either
(a) require tying it to some unrelated bank line whose amount has nothing to
do with the deposit being cleared, which would misuse the review queue's
"this category's amount IS this line's amount" convention everywhere else,
or (b) silently fail to handle the common real case of a deposit that
already covered the purchase in full. Building it as a plain review-queue
category was therefore rejected as the wrong mechanism, not merely "the more
complex option" — this is flagged explicitly for QA/Main-agent to confirm
rather than silently decided.

This screen is also NOT a copy of ``webapp/settings_bp.py``'s Employee Loans
screen, even though both are "a small admin-editable screen sitting next to
an aggregate asset account" — Employee Loans NEVER posts to the ledger
itself (real disbursement/repayment journal entries only ever come from the
Review Queue); this screen'S WHOLE JOB is to post the one real,
non-cash conversion entry directly, because there is no bank line to route
that posting through instead. This is a deliberate, documented difference in
money mechanics (a loan repays in cash installments over time; a deposit
resolves in one lump conversion-to-expense event), not an oversight.

VISIBILITY into which deposits are currently outstanding is deliberately
NOT duplicated here — ``webapp/subsidiary_ledger_bp.py`` (INVENTORY_DEPOSITS
added to its ``SUBSIDIARY_LEDGER_ACCOUNTS`` allowlist) already shows exactly
that, generically, for free, with its own reconciliation-check guarantee.
This screen re-derives the same "outstanding balance per reference" figures
independently (same underlying ``ledger.balances`` helpers) purely so the
convert form can default to and validate against the correct amount — it
does not replace the Subsidiary Ledger screen's own read-only, full-history
view.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ledger import posting
from ledger.balances import account_balance_through_by_reference, distinct_references_for_account
from ledger.entities import get_account_id
from ledger.errors import UnknownAccountInstanceError
from webapp.db import get_db

bp = Blueprint("inventory_deposits", __name__, url_prefix="/inventory-deposits")

ZERO = Decimal("0")


@dataclass
class OutstandingDeposit:
    reference: str
    outstanding_idr: Decimal


def _as_of_period(conn) -> _dt.date:
    """"Right now" expressed as the first-of-month ``period_month`` value
    every balance helper in this app expects — this screen is an
    operational, always-current view (not a per-period report), so it
    always looks through the end of the CURRENT month, picking up
    everything posted to date.
    """
    return _dt.date.today().replace(day=1)


def _deposits_account_id(conn) -> int | None:
    try:
        return get_account_id(conn, "INVENTORY_DEPOSITS")
    except UnknownAccountInstanceError:
        # Same "no data yet" tolerance as every other screen in this app —
        # the account_types catalog row can exist before the real
        # consolidated `accounts` row has been provisioned (see
        # scripts/ensure_inventory_deposits_account.py).
        return None


def outstanding_deposits(conn) -> list[OutstandingDeposit]:
    """Every deposit reference with a nonzero outstanding balance right now
    — the actionable list this screen exists to show. A reference that's
    already been fully converted (outstanding == 0) is intentionally
    excluded here (nothing left to do) but stays fully visible, forever, on
    the Subsidiary Ledger screen's own read-only history.
    """
    account_id = _deposits_account_id(conn)
    if account_id is None:
        return []
    period = _as_of_period(conn)
    refs = distinct_references_for_account(conn, account_id, period)
    out = []
    for ref in refs:
        balance = account_balance_through_by_reference(conn, account_id, period, "debit", ref)
        if balance != ZERO:
            out.append(OutstandingDeposit(reference=ref, outstanding_idr=balance))
    return out


@bp.route("/")
def index():
    conn = get_db()
    deposits = outstanding_deposits(conn)
    return render_template("inventory_deposits.html", deposits=deposits, today=_dt.date.today().isoformat())


@bp.route("/convert", methods=["POST"])
def convert():
    conn = get_db()
    deposit_ref = (request.form.get("deposit_ref") or "").strip()
    raw_amount = (request.form.get("amount_idr") or "").strip()
    raw_date = (request.form.get("entry_date") or "").strip()

    if not deposit_ref:
        flash("Please choose which deposit this clears.", "error")
        return redirect(url_for("inventory_deposits.index"))

    try:
        amount_idr = Decimal(raw_amount)
    except InvalidOperation:
        flash("Amount must be a number (e.g. 9840000).", "error")
        return redirect(url_for("inventory_deposits.index"))
    if amount_idr <= 0:
        flash("Amount must be greater than zero.", "error")
        return redirect(url_for("inventory_deposits.index"))

    try:
        entry_date = _dt.date.fromisoformat(raw_date) if raw_date else _dt.date.today()
    except ValueError:
        flash("Date must be a valid date.", "error")
        return redirect(url_for("inventory_deposits.index"))

    try:
        posting.post_inventory_deposit_received(
            conn,
            entry_date=entry_date,
            amount_idr=amount_idr,
            deposit_ref=deposit_ref,
            memo=f"Goods received — converting inventory deposit '{deposit_ref}' to COGS",
        )
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "error")
        return redirect(url_for("inventory_deposits.index"))

    conn.commit()
    flash(
        f"Converted {amount_idr} to COGS for deposit '{deposit_ref}' — the goods have been "
        "recorded as received.",
        "success",
    )
    return redirect(url_for("inventory_deposits.index"))
