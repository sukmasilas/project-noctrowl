"""Data Quality screen: automated reconciliation-gap detection results.

See CLAUDE.md's reconciliation-gap-detection feature and
``ingestion/reconciliation.py``'s module docstring. Purely informational,
read-only — the user only views this screen; there is deliberately NO
"fix it" / reopen / correct action anywhere here or in its route. If a real
discrepancy shows up, it's a flag for a human to go investigate outside
this app, same "detection and surfacing only" boundary as the rest of this
feature.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request
from sqlalchemy import select

from ledger.schema import account_types, accounts, reconciliation_checks, wallet_groups
from webapp.auth import login_required
from webapp.db import get_db
from webapp.finalization import review_queue_status
from webapp.scoping import list_ebay_accounts, parse_period

bp = Blueprint("data_quality", __name__, url_prefix="/data-quality")


@bp.route("/")
@login_required
def index():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    accounts_list = list_ebay_accounts(conn)

    rows = conn.execute(
        select(
            reconciliation_checks,
            account_types.c.name.label("account_type_name"),
            accounts.c.wallet_group_id,
            wallet_groups.c.name.label("wallet_group_name"),
        )
        .select_from(
            reconciliation_checks.join(accounts, accounts.c.id == reconciliation_checks.c.account_id)
            .join(account_types, account_types.c.id == accounts.c.account_type_id)
            .outerjoin(wallet_groups, wallet_groups.c.id == accounts.c.wallet_group_id)
        )
        .where(reconciliation_checks.c.period_month == period_month)
        .order_by(account_types.c.code)
    ).all()

    cards = []
    for r in rows:
        scope_label = r.wallet_group_name or "Consolidated"
        # A discrepancy's outstanding-Needs-Review count for the SAME
        # scope — lets the screen distinguish "you have unlabeled rows
        # contributing to this" (an unresolved row is probably why the
        # numbers don't match yet — nothing new to investigate beyond the
        # review queue itself) from "everything's already labeled and it
        # STILL doesn't match" (a genuinely unexplained gap that needs a
        # human to actually dig in — this app cannot resolve it further).
        needs_review_count = review_queue_status(
            conn, period_month=period_month, wallet_group_id=r.wallet_group_id
        )
        cards.append(
            {
                "account_label": f"{r.account_type_name} — {scope_label}",
                "expected_opening_idr": r.expected_opening_idr,
                "actual_opening_idr": r.actual_opening_idr,
                "opening_discrepancy_idr": r.opening_discrepancy_idr,
                "expected_closing_idr": r.expected_closing_idr,
                "actual_closing_idr": r.actual_closing_idr,
                "closing_discrepancy_idr": r.closing_discrepancy_idr,
                "is_material": r.is_material,
                "checked_at": r.checked_at,
                "needs_review_count": needs_review_count,
            }
        )

    return render_template(
        "data_quality.html",
        accounts=accounts_list,
        period_month=period_month,
        cards=cards,
    )
