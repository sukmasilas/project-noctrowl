"""General Ledger screen: the traditional, per-account ledger view.

Added 2026-09-16 alongside the Journal Entries rename (see
``webapp/journal_entries_bp.py``'s module docstring for the full
Journal-vs-Ledger distinction this split is based on). A real General
Ledger is organized BY ACCOUNT: one row per journal LINE that touched the
selected account, in chronological order, with a running balance — it does
NOT show the other side of each entry inline. If a user needs the full
both-sides detail for a specific line, they follow that row's Journal Entry
# link over to the Journal Entries screen, which shows the whole entry.

Unlike Journal Entries' account filter (which is optional — "all accounts"
is itself a real, period-scoped mode there), a specific account selection is
MANDATORY here: there's no "General Ledger for all accounts at once" — a
row-per-line view across every account with no account context per row
would defeat the purpose of the screen. If no account is given, this screen
defaults to the first account in ``webapp.journal_entries_bp.
list_all_accounts``'s own ordering (CLAUDE.md's own Chart-of-accounts
statement-section order), the same "default sensibly, don't error" pattern
``webapp.wallet_bp`` already uses for its own mandatory wallet selector.

Running balance reuses ``ledger.balances.account_balance_before`` for the
period's opening balance, then walks forward line-by-line exactly the same
way Journal Entries' own running-balance computation does (and the same
underlying convention Wallet/Balance Sheet/reconciliation share) — not a
reinvented running total.

Reversal context is surfaced compactly per row (a row is flagged if the
ENTRY it belongs to has been reversed, or if the entry itself IS a
reversal) so a reader can tell at a glance without following the Journal
Entry link — the full reversal narrative (which entry, what it reversed)
still only lives on the Journal Entries screen itself.

Read-only, same as every other report/register in this app.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from flask import Blueprint, render_template, request
from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.balances import account_balance_before
from ledger.schema import journal_entries, journal_lines
from webapp.auth import login_required
from webapp.db import get_db
from webapp.journal_entries_bp import AccountOption, list_all_accounts
from webapp.scoping import parse_period

bp = Blueprint("general_ledger", __name__, url_prefix="/general-ledger")

ZERO = Decimal("0")


@dataclass
class GeneralLedgerRow:
    journal_entry_id: int
    entry_date: _dt.date
    period_month: _dt.date
    memo: str | None
    debit_idr: Decimal
    credit_idr: Decimal
    running_balance_idr: Decimal
    ebay_order_ref: str | None
    consignor_item_ref: str | None
    reversed_by_id: int | None  # the ENTRY this line belongs to has been reversed
    reversal_of_id: int | None  # the ENTRY this line belongs to IS a reversal


def _get_account_option(options: list[AccountOption], account_id: int) -> AccountOption | None:
    for opt in options:
        if opt.account_id == account_id:
            return opt
    return None


def _reversal_links_for_entries(conn: Connection, entry_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    """Same (reversed_by, reversal_of) derivation as
    ``webapp.journal_entries_bp._reversal_links`` — re-derived directly from
    ``journal_entries.reversed_by_id`` here rather than imported, since this
    screen's ``entry_ids`` set is line-oriented (only entries that actually
    have a line on the one selected account), a different scope than that
    module's own entry-level set.
    """
    if not entry_ids:
        return {}, {}

    reversed_by: dict[int, int] = {}
    for r in conn.execute(
        select(journal_entries.c.id, journal_entries.c.reversed_by_id)
        .where(journal_entries.c.id.in_(entry_ids))
        .where(journal_entries.c.reversed_by_id.is_not(None))
    ).all():
        reversed_by[r.id] = r.reversed_by_id

    reversal_of: dict[int, int] = {}
    for r in conn.execute(
        select(journal_entries.c.id, journal_entries.c.reversed_by_id).where(
            journal_entries.c.reversed_by_id.in_(entry_ids)
        )
    ).all():
        reversal_of[r.reversed_by_id] = r.id

    return reversed_by, reversal_of


def general_ledger_rows(
    conn: Connection, *, account_option: AccountOption, period_month: _dt.date, scope_all: bool
) -> list[GeneralLedgerRow]:
    account_id = account_option.account_id

    query = (
        select(
            journal_lines.c.journal_entry_id,
            journal_lines.c.id.label("line_id"),
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
            journal_entries.c.entry_date,
            journal_entries.c.period_month,
            journal_entries.c.memo,
        )
        .select_from(
            journal_lines.join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        )
        .where(journal_lines.c.account_id == account_id)
    )
    if not scope_all:
        query = query.where(journal_entries.c.period_month == period_month)
    query = query.order_by(journal_entries.c.entry_date, journal_lines.c.journal_entry_id, journal_lines.c.id)

    rows = conn.execute(query).all()
    if not rows:
        return []

    entry_ids = sorted({r.journal_entry_id for r in rows})
    reversed_by_map, reversal_of_map = _reversal_links_for_entries(conn, entry_ids)

    running = (
        ZERO if scope_all else account_balance_before(conn, account_id, period_month, account_option.normal_balance)
    )

    out: list[GeneralLedgerRow] = []
    for r in rows:
        if account_option.normal_balance == "debit":
            running += r.debit_amount_idr - r.credit_amount_idr
        else:
            running += r.credit_amount_idr - r.debit_amount_idr
        out.append(
            GeneralLedgerRow(
                journal_entry_id=r.journal_entry_id,
                entry_date=r.entry_date,
                period_month=r.period_month,
                memo=r.memo,
                debit_idr=r.debit_amount_idr,
                credit_idr=r.credit_amount_idr,
                running_balance_idr=running,
                ebay_order_ref=r.ebay_order_ref,
                consignor_item_ref=r.consignor_item_ref,
                reversed_by_id=reversed_by_map.get(r.journal_entry_id),
                reversal_of_id=reversal_of_map.get(r.journal_entry_id),
            )
        )
    return out


@bp.route("/")
@login_required
def index():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    all_accounts = list_all_accounts(conn)

    if not all_accounts:
        return render_template(
            "general_ledger.html",
            all_accounts=[],
            account_option=None,
            period_month=period_month,
            scope_all=False,
            opening_balance=None,
            closing_balance=None,
            rows=[],
        )

    raw_account_id = request.args.get("account_id", type=int)
    account_option = None
    if raw_account_id is not None:
        account_option = _get_account_option(all_accounts, raw_account_id)
    if account_option is None:
        account_option = all_accounts[0]

    scope_all = request.args.get("scope") == "all"

    opening_balance = (
        ZERO
        if scope_all
        else account_balance_before(conn, account_option.account_id, period_month, account_option.normal_balance)
    )

    rows = general_ledger_rows(conn, account_option=account_option, period_month=period_month, scope_all=scope_all)

    return render_template(
        "general_ledger.html",
        all_accounts=all_accounts,
        account_option=account_option,
        period_month=period_month,
        scope_all=scope_all,
        opening_balance=opening_balance,
        closing_balance=rows[-1].running_balance_idr if rows else opening_balance,
        rows=rows,
    )
