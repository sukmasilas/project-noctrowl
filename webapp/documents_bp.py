"""Documents screen: ingestion status cards, invoice records, Sync Now.

See docs/design/ui-ux-design.md Screen 0 and
docs/design/milestone-4-web-app-design.md §3/§6.
"""
from __future__ import annotations

import datetime as _dt
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import exists, select, update
from sqlalchemy.engine import Connection

from ingestion.schema import invoices as invoices_table
from ingestion.schema import review_queue, source_documents
from ingestion.sync import SyncAlreadyRunningError, run_sync_for_period
from webapp.auth import login_required
from webapp.db import get_db
from webapp.finalization import expected_by
from webapp.scoping import list_ebay_accounts, parse_period
from webapp.sync_cooldown import record_sync_run, seconds_until_next_allowed

bp = Blueprint("documents", __name__, url_prefix="/documents")

# Purpose values a human may pick in the invoice editor — mirrors the DB
# CHECK constraint (ingestion.schema.invoices' ck_invoices_purpose) exactly,
# including the third value CLAUDE.md added 2026-09-01.
INVOICE_PURPOSE_OPTIONS = [
    ("cogs_purchase", "COGS Purchase"),
    ("consignment_purchase", "Consignment Purchase (consignor reimbursement)"),
    ("general_operating_expense", "General Operating Expense"),
]

FIXED_DOCUMENT_TYPES_PER_ACCOUNT = ["ebay_sales_csv", "payoneer_csv", "bank_statement_wallet_group"]


def list_untraceable_invoices(conn: Connection, *, period_month=None):
    """Invoice records with NO ``review_queue`` row anywhere linking back to
    them via ``linked_invoice_id`` — i.e. the invoice/proof-of-transfer
    exists (was uploaded and OCR-extracted), but no wallet transaction
    (Payoneer/bank-statement line) can be traced to it. Shared by the Wallet
    screen (webapp/wallet_bp.py — see CLAUDE.md's Wallet-screen brief: "all
    transactions should be traced in the wallet, including the invoices...
    if there are any invoices that are not traceable... this should be
    flagged") and available here for Documents to reuse if ever needed.

    Deliberately the REVERSE of ``unmatched_cogs`` in ``index()`` above
    (review_queue rows with no linked invoice) — this is invoices with no
    linking review_queue row. Flags only; never explains or auto-classifies
    WHY (per the brief) — that's for a human to investigate.
    """
    query = select(invoices_table).order_by(invoices_table.c.extracted_date)
    if period_month is not None:
        query = query.where(invoices_table.c.period_month == period_month)
    query = query.where(
        ~exists(
            select(review_queue.c.id).where(review_queue.c.linked_invoice_id == invoices_table.c.id)
        )
    )
    return conn.execute(query).all()


@bp.route("/")
@login_required
def index():
    conn = get_db()
    accounts = list_ebay_accounts(conn)
    if not accounts:
        return render_template("documents.html", accounts=[], no_accounts=True)

    ebay_account_id = request.args.get("account_id", type=int) or accounts[0].id
    account = next((a for a in accounts if a.id == ebay_account_id), accounts[0])
    period_month = parse_period(request.args.get("period"), conn)

    cards = _ingestion_cards(conn, ebay_account_id=account.id, wallet_group_id=account.wallet_group_id, period_month=period_month)

    invoice_rows = conn.execute(
        select(invoices_table).where(invoices_table.c.period_month == period_month).order_by(invoices_table.c.extracted_date)
    ).all()

    matched_invoice_ids = {
        r.linked_invoice_id
        for r in conn.execute(
            select(review_queue.c.linked_invoice_id).where(review_queue.c.linked_invoice_id.isnot(None))
        ).all()
    }

    unmatched_cogs = conn.execute(
        select(review_queue.c.id, review_queue.c.raw_description, review_queue.c.amount_idr)
        .where(review_queue.c.category.in_(["cogs_purchase", "consignment_payout"]))
        .where(review_queue.c.linked_invoice_id.is_(None))
    ).all()

    last_sync = seconds_until_next_allowed(conn)

    return render_template(
        "documents.html",
        accounts=accounts,
        selected_account=account,
        period_month=period_month,
        cards=cards,
        invoice_rows=invoice_rows,
        matched_invoice_ids=matched_invoice_ids,
        unmatched_cogs=unmatched_cogs,
        purpose_options=INVOICE_PURPOSE_OPTIONS,
        sync_cooldown_seconds=last_sync,
        no_accounts=False,
    )


def _ingestion_cards(conn, *, ebay_account_id: int, wallet_group_id: int, period_month: _dt.date):
    cards = []
    for document_type, scope_kwargs, label in [
        ("ebay_sales_csv", {"ebay_account_id": ebay_account_id, "wallet_group_id": None}, "eBay Sales Export"),
        ("payoneer_csv", {"ebay_account_id": None, "wallet_group_id": wallet_group_id}, "Payoneer Export"),
        (
            "bank_statement_wallet_group",
            {"ebay_account_id": None, "wallet_group_id": wallet_group_id},
            "Bank Statement (Bridging)",
        ),
        ("bank_statement_master", {"ebay_account_id": None, "wallet_group_id": None}, "Master Bank Statement"),
    ]:
        query = select(source_documents).where(
            source_documents.c.document_type == document_type,
            source_documents.c.period_month == period_month,
        )
        query = query.where(
            source_documents.c.ebay_account_id == scope_kwargs["ebay_account_id"]
            if scope_kwargs["ebay_account_id"] is not None
            else source_documents.c.ebay_account_id.is_(None)
        )
        query = query.where(
            source_documents.c.wallet_group_id == scope_kwargs["wallet_group_id"]
            if scope_kwargs["wallet_group_id"] is not None
            else source_documents.c.wallet_group_id.is_(None)
        )
        row = conn.execute(query).first()
        deadline = expected_by(period_month)
        if row is not None and row.ingested_at is not None:
            state = "uploaded"
        elif _dt.date.today() > deadline:
            state = "missing"
        else:
            state = "not_yet_uploaded"
        cards.append(
            {
                "document_type": document_type,
                "label": label,
                "state": state,
                "row_count": row.row_count if row is not None else None,
                "ingested_at": row.ingested_at if row is not None else None,
                "drive_file_name": row.drive_file_name if row is not None else None,
                "parse_warning": row.parse_warning if row is not None else None,
                "expected_by": deadline,
            }
        )
    return cards


@bp.route("/invoices/<int:invoice_id>", methods=["POST"])
@login_required
def update_invoice(invoice_id: int):
    conn = get_db()
    extracted_date = request.form.get("extracted_date") or None
    vendor_description = request.form.get("vendor_description") or None
    amount_idr = request.form.get("amount_idr") or None
    purpose = request.form.get("purpose") or None

    values = {
        "vendor_description": vendor_description,
        "purpose": purpose,
        "confirmed_at": _dt.datetime.now(_dt.timezone.utc),
        "status": "parsed",
    }
    if extracted_date:
        try:
            values["extracted_date"] = _dt.date.fromisoformat(extracted_date)
        except ValueError:
            flash("Invalid date format.", "error")
            return redirect(url_for("documents.index", period=request.form.get("period")))
    if amount_idr:
        try:
            from decimal import Decimal

            values["amount_idr"] = Decimal(amount_idr)
        except Exception:
            flash("Invalid amount.", "error")
            return redirect(url_for("documents.index", period=request.form.get("period")))

    conn.execute(update(invoices_table).where(invoices_table.c.id == invoice_id).values(**values))
    conn.commit()
    flash("Invoice updated.", "success")
    return redirect(url_for("documents.index", period=request.form.get("period"), account_id=request.form.get("account_id")))


@bp.route("/sync", methods=["POST"])
@login_required
def sync_now():
    conn = get_db()
    ebay_account_id = request.form.get("account_id", type=int)
    period_str = request.form.get("period")
    accounts = list_ebay_accounts(conn)
    account = next((a for a in accounts if a.id == ebay_account_id), accounts[0] if accounts else None)
    if account is None:
        flash("No eBay account configured yet — nothing to sync.", "error")
        return redirect(url_for("documents.index"))

    period_month = parse_period(period_str, conn)

    remaining = seconds_until_next_allowed(conn)
    if remaining > 0:
        flash(f"Sync Now is on cooldown — try again in {remaining} seconds.", "error")
        return redirect(url_for("documents.index", period=period_month.isoformat(), account_id=account.id))

    root_folder_id = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID")
    if not root_folder_id:
        flash("GOOGLE_DRIVE_ROOT_FOLDER_ID is not configured — cannot reach Google Drive.", "error")
        return redirect(url_for("documents.index", period=period_month.isoformat(), account_id=account.id))

    drive_client = current_app.config.get("DRIVE_CLIENT")
    if drive_client is None:
        flash("Google Drive client is not configured on this server.", "error")
        return redirect(url_for("documents.index", period=period_month.isoformat(), account_id=account.id))

    try:
        result = run_sync_for_period(
            conn,
            drive_client,
            root_folder_id=root_folder_id,
            period_month=period_month,
            ebay_account_id=account.id,
            ebay_account_folder_name=account.ebay_account_drive_folder_name_resolved,
            wallet_group_id=account.wallet_group_id,
            wallet_group_folder_name=account.wallet_group_drive_folder_name_resolved,
        )
        conn.commit()
    except SyncAlreadyRunningError:
        # Fix (2026-09-01, QA-found race): the cooldown check above only
        # protects against a SLOW second click, not a near-simultaneous one
        # (double-click, two tabs, a client retry) — record_sync_run() only
        # fires after a successful run completes, so two overlapping
        # requests could both pass the check above before either finishes.
        # run_sync_for_period() itself now serializes via a Postgres
        # advisory lock (see ingestion/sync.py) and raises this specific,
        # expected exception instead of racing — surfaced here as a plain,
        # non-alarming message rather than the generic "Sync failed" below.
        conn.rollback()
        flash("A sync is already running — try again in a moment.", "error")
        return redirect(url_for("documents.index", period=period_month.isoformat(), account_id=account.id))
    except Exception as exc:  # noqa: BLE001 — surface any sync failure to the user, never crash the request
        conn.rollback()
        current_app.logger.exception("Sync Now failed")
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("documents.index", period=period_month.isoformat(), account_id=account.id))

    record_sync_run(
        conn,
        triggered_by=session.get("logged_in") and os.environ.get("APP_LOGIN_USERNAME"),
        ebay_account_id=account.id,
        period_month=period_month,
        result_summary=f"{len(result.steps)} steps, "
        f"{result.posted.posted if result.posted else 0} rows posted",
    )
    conn.commit()

    new_files = sum(1 for s in result.steps if s.found_file)
    # BUG FIX (2026-09-02, found during the real live-Drive validation run):
    # result.auto_match.needs_review is the TOTAL count of every still
    # -unclassified review_queue row across ALL periods (run_auto_match has
    # no period filter — see ingestion/matching.py), not "new" items created
    # by this one sync. The old wording ("N new Needs Review item(s)") made
    # every single Sync Now click look like it had just created N fresh
    # problems, even on a fully-idempotent re-sync that added zero rows —
    # confirmed misleading in practice when a real re-sync legitimately
    # produced 0 new rows but the message still said "392 new".
    posted_count = result.posted.posted if result.posted else 0
    needs_review = result.auto_match.needs_review if result.auto_match else 0
    flash(
        f"Synced just now — {new_files} sources checked, {posted_count} row(s) posted, "
        f"{needs_review} Needs Review item(s) outstanding (across all periods).",
        "success",
    )
    return redirect(url_for("documents.index", period=period_month.isoformat(), account_id=account.id))
