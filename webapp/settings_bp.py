"""Settings screen: Consignor Payout Tiers table.

Update-only on ``rate_percent`` for the 6 fixed tier rows — see
docs/design/ui-ux-design.md Screen 1.5 and
docs/design/milestone-4-web-app-design.md §5. No add/remove row UI (the
schedule is fixed), and no retroactive effect on already-posted
consignment sales — ``ledger.schema.consignment_sales.tier_rate_percent``
freezes whatever rate was actually used at confirmation time, independent
of anything this screen does afterward.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import select, update

from ledger.schema import consignor_payout_tiers
from webapp.auth import login_required
from webapp.db import get_db

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/payout-tiers")
@login_required
def payout_tiers():
    conn = get_db()
    rows = conn.execute(select(consignor_payout_tiers).order_by(consignor_payout_tiers.c.display_order)).all()
    return render_template("settings_payout_tiers.html", rows=rows)


@bp.route("/payout-tiers/<int:tier_id>", methods=["POST"])
@login_required
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
