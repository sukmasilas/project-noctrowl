"""Wallet screen: the full parsed transaction register for each of the
business's real wallets (eBay Wallet per account, Payoneer Wallet / BCA
Bridging Account per wallet-group, BCA Main Account consolidated) — plus a
real-money-implication addition: flagging invoices that have no matching
transaction anywhere in the wallets.

This is a source-of-truth transaction BROWSER, not a computed statement —
deliberately separate from the Reports nav (see CLAUDE.md's Wallet-screen
brief). It shows literally what came off each wallet's statement/export,
including rows that haven't posted yet (still needs_review, or an
unconfirmed consignment sale) — read-only, same as every other screen; the
Review Queue remains the one screen where the user actually labels a row.

Two underlying raw-row sources, per wallet type:
  - eBay Wallet: ``ingestion.schema.ebay_csv_transactions`` (added alongside
    this screen — see that table's docstring for why it was needed: eBay CSV
    rows previously bypassed ``review_queue`` entirely for confident/
    structured rows, leaving no browsable trace of the original row).
  - Payoneer Wallet / BCA Bridging Account / BCA Main Account: already fully
    preserved in ``review_queue`` regardless of match status (source_type
    'payoneer_csv' or 'bank_statement', scoped by wallet_group_id — NULL for
    the consolidated BCA Main Account, set for the wallet-group-scoped
    Payoneer Wallet / BCA Bridging Account — see ingestion/sync.py).

"Running balance" reuses ``ledger.balances``' account-balance-through-date
helpers directly (per the brief: "don't reinvent this") rather than
constructing a fragile per-row cumulative total — a naive per-raw-row
running balance would actually be WRONG for eBay Wallet specifically, since
several raw CSV rows in a merged multi-item Order group share exactly one
net posting effect (see ingestion/ebay_csv.py's module docstring on
multi-row orders). Opening/closing balance for the period is shown instead,
framing the register the same way Data Quality's reconciliation cards
already do.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from flask import Blueprint, render_template, request
from sqlalchemy import select
from sqlalchemy.engine import Connection

from ingestion.schema import ebay_csv_transactions, review_queue
from ledger.balances import account_balance_before, account_balance_through
from ledger.schema import account_types, accounts, consignment_sales, ebay_accounts, wallet_groups
from webapp.auth import login_required
from webapp.db import get_db
from webapp.documents_bp import list_untraceable_invoices
from webapp.review_queue_bp import CATEGORY_OPTIONS
from webapp.scoping import list_ebay_accounts, parse_period

bp = Blueprint("wallet", __name__, url_prefix="/wallet")

_CATEGORY_LABELS = dict(CATEGORY_OPTIONS)

# The 4 wallet account_types, in CLAUDE.md's Chart of accounts order.
_WALLET_ACCOUNT_TYPE_CODES = ["EBAY_WALLET", "PAYONEER_WALLET", "BCA_BRIDGING", "BCA_MAIN"]


@dataclass
class WalletOption:
    account_id: int
    account_type_code: str
    label: str
    normal_balance: str


@dataclass
class WalletTransaction:
    transaction_date: _dt.date
    description: str
    amount_idr: Decimal | None
    amount_usd: Decimal | None
    category_or_type: str
    category_label: str
    status: str  # 'posted' | 'matched_pending_post' | 'needs_review' | 'not_posted' | 'awaiting_confirmation'
    status_detail: str | None
    journal_entry_id: int | None
    source: str  # 'ebay_csv' | 'review_queue'


def list_wallet_options(conn: Connection) -> list[WalletOption]:
    """Every real wallet instance (3 eBay Wallets, 2 Payoneer Wallets, 2 BCA
    Bridging Accounts, 1 BCA Main Account for a fully-onboarded business —
    fewer for the current prototype) — mirrors
    ``webapp.reporting._account_instance_lines(statement_section='asset')``
    but deliberately lighter (no period-end balance computed here — that's
    only needed for whichever ONE wallet is actually selected, not the
    whole dropdown list).
    """
    rows = conn.execute(
        select(
            accounts.c.id.label("account_id"),
            account_types.c.code,
            account_types.c.name.label("type_name"),
            account_types.c.normal_balance,
            ebay_accounts.c.name.label("ebay_account_name"),
            wallet_groups.c.name.label("wallet_group_name"),
        )
        .select_from(accounts)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .outerjoin(ebay_accounts, ebay_accounts.c.id == accounts.c.ebay_account_id)
        .outerjoin(wallet_groups, wallet_groups.c.id == accounts.c.wallet_group_id)
        .where(account_types.c.code.in_(_WALLET_ACCOUNT_TYPE_CODES))
        .order_by(accounts.c.id)
    ).all()

    # Sort by CLAUDE.md's own Chart-of-accounts asset ordering (eBay Wallet,
    # Payoneer Wallet, BCA Bridging, BCA Main), not the alphabetical
    # account_types.code order the query above would otherwise return —
    # purely a dropdown-readability nicety, no accounting-logic implication.
    order_index = {code: i for i, code in enumerate(_WALLET_ACCOUNT_TYPE_CODES)}
    rows = sorted(rows, key=lambda r: (order_index[r.code], r.account_id))

    options = []
    for r in rows:
        if r.ebay_account_name:
            label = f"{r.type_name} — {r.ebay_account_name}"
        elif r.wallet_group_name:
            label = f"{r.type_name} — {r.wallet_group_name}"
        else:
            label = r.type_name
        options.append(
            WalletOption(account_id=r.account_id, account_type_code=r.code, label=label, normal_balance=r.normal_balance)
        )
    return options


def _get_wallet_option(conn: Connection, account_id: int) -> WalletOption | None:
    for opt in list_wallet_options(conn):
        if opt.account_id == account_id:
            return opt
    return None


def _next_month(d: _dt.date) -> _dt.date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


def _ebay_wallet_register(conn: Connection, *, ebay_account_id: int, period_month: _dt.date) -> list[WalletTransaction]:
    period_start = period_month
    period_end = _next_month(period_month)
    rows = conn.execute(
        select(ebay_csv_transactions)
        .where(ebay_csv_transactions.c.ebay_account_id == ebay_account_id)
        .where(ebay_csv_transactions.c.transaction_date >= period_start)
        .where(ebay_csv_transactions.c.transaction_date < period_end)
        .order_by(ebay_csv_transactions.c.transaction_date, ebay_csv_transactions.c.row_index)
    ).all()

    # Batch-resolve review_queue / consignment_sales status for whichever
    # rows link to one, rather than one query per row.
    rq_ids = {r.review_queue_id for r in rows if r.review_queue_id is not None}
    rq_status = {}
    if rq_ids:
        for r in conn.execute(
            select(
                review_queue.c.id,
                review_queue.c.match_status,
                review_queue.c.category,
                review_queue.c.posted_at,
            ).where(review_queue.c.id.in_(rq_ids))
        ).all():
            rq_status[r.id] = r

    cs_ids = {r.consignment_sale_id for r in rows if r.consignment_sale_id is not None}
    cs_status = {}
    if cs_ids:
        for r in conn.execute(
            select(
                consignment_sales.c.id,
                consignment_sales.c.confirmed_at,
                consignment_sales.c.journal_entry_id,
            ).where(consignment_sales.c.id.in_(cs_ids))
        ).all():
            cs_status[r.id] = r

    out: list[WalletTransaction] = []
    for r in rows:
        amount = r.amount_gross_usd if r.amount_gross_usd is not None else r.amount_net_usd
        if r.journal_entry_id is not None:
            status, detail, journal_entry_id = "posted", None, r.journal_entry_id
        elif r.consignment_sale_id is not None:
            cs = cs_status.get(r.consignment_sale_id)
            if cs is not None and cs.journal_entry_id is not None:
                status, detail, journal_entry_id = "posted", None, cs.journal_entry_id
            else:
                status, detail, journal_entry_id = (
                    "awaiting_confirmation",
                    "Detected as a CONSIGN- sale — awaiting human confirmation of the payout before it posts.",
                    None,
                )
        elif r.review_queue_id is not None:
            rq = rq_status.get(r.review_queue_id)
            journal_entry_id = None
            if rq is None:
                status, detail = "not_posted", None
            elif rq.posted_at is not None:
                status, detail = "posted", None
            elif rq.match_status == "needs_review":
                status, detail = "needs_review", "Flagged to the Review Queue — needs a category before it can post."
            else:
                status, detail = "matched_pending_post", "Matched — queued for the next sync."
        else:
            status, detail, journal_entry_id = "not_posted", _NOT_POSTED_HINTS.get(r.row_type), None

        out.append(
            WalletTransaction(
                transaction_date=r.transaction_date,
                description=r.description or f"{r.row_type} — order {r.order_number or '—'}",
                amount_idr=None,
                amount_usd=amount,
                category_or_type=r.row_type,
                category_label=r.row_type,
                status=status,
                status_detail=detail,
                journal_entry_id=journal_entry_id,
                source="ebay_csv",
            )
        )
    return out


_NOT_POSTED_HINTS = {
    "Hold": "A temporary availability hold — never a distinct posting event on its own.",
}


def _bank_or_payoneer_register(
    conn: Connection, *, source_type: str, wallet_group_id: int | None, period_month: _dt.date
) -> list[WalletTransaction]:
    period_start = period_month
    period_end = _next_month(period_month)
    query = (
        select(review_queue)
        .where(review_queue.c.source_type == source_type)
        .where(review_queue.c.transaction_date >= period_start)
        .where(review_queue.c.transaction_date < period_end)
    )
    query = query.where(
        review_queue.c.wallet_group_id == wallet_group_id
        if wallet_group_id is not None
        else review_queue.c.wallet_group_id.is_(None)
    )
    rows = conn.execute(query.order_by(review_queue.c.transaction_date)).all()

    out: list[WalletTransaction] = []
    for r in rows:
        if r.posted_at is not None:
            status, detail = "posted", None
        elif r.match_status == "needs_review":
            status, detail = "needs_review", r.sign_mismatch_reason or None
        else:
            status, detail = "matched_pending_post", "Matched — queued for the next sync."
        out.append(
            WalletTransaction(
                transaction_date=r.transaction_date,
                description=r.raw_description,
                amount_idr=r.amount_idr,
                amount_usd=r.amount_usd_ref,
                category_or_type=r.category or "—",
                category_label=_CATEGORY_LABELS.get(r.category, r.category or "Uncategorized"),
                status=status,
                status_detail=detail,
                journal_entry_id=r.posted_journal_entry_id,
                source="review_queue",
            )
        )
    return out


def wallet_register(
    conn: Connection, opt: WalletOption, *, period_month: _dt.date
) -> list[WalletTransaction]:
    if opt.account_type_code == "EBAY_WALLET":
        row = conn.execute(select(accounts.c.ebay_account_id).where(accounts.c.id == opt.account_id)).first()
        return _ebay_wallet_register(conn, ebay_account_id=row.ebay_account_id, period_month=period_month)

    if opt.account_type_code == "PAYONEER_WALLET":
        row = conn.execute(select(accounts.c.wallet_group_id).where(accounts.c.id == opt.account_id)).first()
        return _bank_or_payoneer_register(
            conn, source_type="payoneer_csv", wallet_group_id=row.wallet_group_id, period_month=period_month
        )

    if opt.account_type_code == "BCA_BRIDGING":
        row = conn.execute(select(accounts.c.wallet_group_id).where(accounts.c.id == opt.account_id)).first()
        return _bank_or_payoneer_register(
            conn, source_type="bank_statement", wallet_group_id=row.wallet_group_id, period_month=period_month
        )

    # BCA_MAIN — consolidated, wallet_group_id always NULL.
    return _bank_or_payoneer_register(conn, source_type="bank_statement", wallet_group_id=None, period_month=period_month)


@bp.route("/")
@login_required
def index():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    wallet_options = list_wallet_options(conn)

    if not wallet_options:
        return render_template("wallet.html", wallet_options=[], no_wallets=True, period_month=period_month)

    account_id = request.args.get("account_id", type=int) or wallet_options[0].account_id
    selected = _get_wallet_option(conn, account_id) or wallet_options[0]

    transactions = wallet_register(conn, selected, period_month=period_month)

    opening_balance = account_balance_before(conn, selected.account_id, period_month, selected.normal_balance)
    closing_balance = account_balance_through(conn, selected.account_id, period_month, selected.normal_balance)

    untraceable_invoices = list_untraceable_invoices(conn, period_month=period_month)

    return render_template(
        "wallet.html",
        wallet_options=wallet_options,
        selected=selected,
        period_month=period_month,
        transactions=transactions,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        net_movement=closing_balance - opening_balance,
        untraceable_invoices=untraceable_invoices,
        no_wallets=False,
    )
