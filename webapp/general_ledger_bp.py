"""General Ledger screen: the formal, posted-only, double-entry accounting
record across the ENTIRE chart of accounts — not just the four wallet
accounts (see webapp/wallet_bp.py for that screen and its own docstring on
how the two differ).

Read-only, same as every other report/register in this app — no posting, no
editing, no correction mechanism lives here. Every row this screen shows is
already a real ``journal_entries`` row; there is no "unposted" concept in
this table (unlike the Wallet screen, which also surfaces still-needs-review
rows from ``review_queue``/``ebay_csv_transactions``) — so "posted only" is
satisfied simply by querying ``journal_entries`` directly, nothing extra to
filter out.

Scope/UX decisions (flagged in the brief as this screen's own call to make,
not something to guess past silently):

- Default view is scoped to ONE selected period (``webapp.scoping.
  parse_period`` — the exact same month-selector convention already used by
  Reports/Wallet/Data Quality), not "every entry ever" — with ~950 real rows
  today and growing, an unfiltered default would only get worse over time.
  This is also the natural reading of "reuse whatever period-selection
  conventions already exist... rather than inventing a new one": scoping.py
  only has a month selector, no arbitrary from/to date-range picker, so this
  screen doesn't invent one either.
- Filtering by a SINGLE account additionally unlocks an explicit, opt-in
  "all periods" toggle (``scope=all``) — a genuine "browse this account's
  full history" view. This is safe to allow unbounded because it's bounded
  by that ONE account's own line count (inherently far smaller than the
  whole ledger), and it's opt-in, never the default. Selecting "All
  accounts" always stays period-scoped, no all-time option — that combination
  really would be the full, growing table.
- Every entry shown includes ALL of its lines (both sides of the double
  entry), even when a specific account filter is active — filtering narrows
  which ENTRIES appear (only ones touching that account), not which LINES of
  a shown entry are visible. This matches the brief's "showing both sides of
  every journal entry together, not one line floating disconnected from its
  pair."
- When a single account is filtered, each shown entry also gets a running
  balance for THAT account, computed via ``ledger.balances`` (opening balance
  from ``account_balance_before``, then walking forward) — the same shared
  helper Wallet/Balance Sheet/reconciliation already use, not a reinvented
  running total.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal

from flask import Blueprint, render_template, request
from sqlalchemy import select
from sqlalchemy.engine import Connection

from ingestion.schema import (
    ebay_csv_transactions,
    invoice_journal_links,
    invoices,
    review_queue,
)
from ledger.balances import account_balance_before
from ledger.schema import (
    account_types,
    accounts,
    consignment_sales,
    ebay_accounts,
    fx_revaluations,
    journal_entries,
    journal_lines,
    opening_balances,
    payoneer_withdrawals,
    wallet_groups,
)
from webapp.auth import login_required
from webapp.db import get_db
from webapp.scoping import parse_period

bp = Blueprint("general_ledger", __name__, url_prefix="/general-ledger")

ZERO = Decimal("0")

# CLAUDE.md's own Chart-of-accounts statement-section ordering — used to sort
# the account filter dropdown the same "readable, not alphabetical" way
# wallet_bp/reporting.py already order the wallet dropdown / Balance Sheet.
_SECTION_ORDER = ["asset", "liability", "equity", "revenue", "cogs", "opex", "other_income_expense"]


@dataclass
class AccountOption:
    account_id: int
    account_type_code: str
    statement_section: str
    label: str
    normal_balance: str


@dataclass
class GLLine:
    account_id: int
    account_label: str
    debit_idr: Decimal
    credit_idr: Decimal
    ebay_order_ref: str | None
    consignor_item_ref: str | None
    is_filtered_account: bool


@dataclass
class SourceTrace:
    kind: str  # 'review_queue' | 'ebay_csv' | 'consignment_sale' | 'opening_balance' | 'payoneer_withdrawal' | 'fx_revaluation' | 'invoice'
    label: str


@dataclass
class GLEntry:
    id: int
    entry_date: _dt.date
    period_month: _dt.date
    source_type: str
    memo: str | None
    lines: list[GLLine]
    total_debit_idr: Decimal
    reversed_by_id: int | None  # this entry HAS been reversed -> points at the reversal entry
    reversal_of_id: int | None  # this entry itself IS a reversal -> points at the original
    source_traces: list[SourceTrace] = field(default_factory=list)
    running_balance_idr: Decimal | None = None  # only set in single-account mode


def list_all_accounts(conn: Connection) -> list[AccountOption]:
    """Every real, individually-postable ``accounts`` row across the WHOLE
    chart of accounts (not just the 4 wallet types — see wallet_bp.
    list_wallet_options for the wallet-only equivalent this deliberately
    generalizes), for the General Ledger's account filter dropdown.
    """
    rows = conn.execute(
        select(
            accounts.c.id.label("account_id"),
            account_types.c.code,
            account_types.c.statement_section,
            account_types.c.name.label("type_name"),
            account_types.c.normal_balance,
            ebay_accounts.c.name.label("ebay_account_name"),
            wallet_groups.c.name.label("wallet_group_name"),
        )
        .select_from(accounts)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .outerjoin(ebay_accounts, ebay_accounts.c.id == accounts.c.ebay_account_id)
        .outerjoin(wallet_groups, wallet_groups.c.id == accounts.c.wallet_group_id)
        .order_by(accounts.c.id)
    ).all()

    order_index = {section: i for i, section in enumerate(_SECTION_ORDER)}
    rows = sorted(rows, key=lambda r: (order_index.get(r.statement_section, 99), r.code, r.account_id))

    options = []
    for r in rows:
        if r.ebay_account_name:
            label = f"{r.type_name} — {r.ebay_account_name}"
        elif r.wallet_group_name:
            label = f"{r.type_name} — {r.wallet_group_name}"
        else:
            label = r.type_name
        options.append(
            AccountOption(
                account_id=r.account_id,
                account_type_code=r.code,
                statement_section=r.statement_section,
                label=label,
                normal_balance=r.normal_balance,
            )
        )
    return options


def _get_account_option(options: list[AccountOption], account_id: int) -> AccountOption | None:
    for opt in options:
        if opt.account_id == account_id:
            return opt
    return None


def _next_month(d: _dt.date) -> _dt.date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


def _entry_ids_for_scope(
    conn: Connection, *, account_id: int | None, period_month: _dt.date, scope_all: bool
) -> list[int]:
    if account_id is not None:
        query = (
            select(journal_lines.c.journal_entry_id)
            .distinct()
            .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
            .where(journal_lines.c.account_id == account_id)
        )
        if not scope_all:
            query = query.where(journal_entries.c.period_month == period_month)
        rows = conn.execute(query).all()
        return [r.journal_entry_id for r in rows]

    # "All accounts" always stays period-scoped — see module docstring.
    rows = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.period_month == period_month)
    ).all()
    return [r.id for r in rows]


def _source_traces(conn: Connection, entry_ids: list[int]) -> dict[int, list[SourceTrace]]:
    """Traces each entry_id back to whatever real source record produced it
    (review_queue row, eBay CSV row, consignment sale, opening balance,
    Payoneer withdrawal, FX revaluation, and any linked invoice) — per
    CLAUDE.md's "numbers are traceable" rule. Deliberately reads whatever
    linkage already exists rather than reconstructing it approximately; an
    entry posted by a one-off script with no such linkage (e.g. a reversal
    entry) simply gets an empty list, not a guessed trace.
    """
    traces: dict[int, list[SourceTrace]] = {eid: [] for eid in entry_ids}
    if not entry_ids:
        return traces

    invoice_ids_needed: set[int] = set()
    invoice_trace_targets: list[tuple[int, int]] = []  # (entry_id, invoice_id)

    for r in conn.execute(
        select(
            review_queue.c.posted_journal_entry_id,
            review_queue.c.id,
            review_queue.c.source_type,
            review_queue.c.raw_description,
            review_queue.c.linked_invoice_id,
        ).where(review_queue.c.posted_journal_entry_id.in_(entry_ids))
    ).all():
        desc = (r.raw_description or "").strip()
        if len(desc) > 80:
            desc = desc[:77] + "..."
        traces[r.posted_journal_entry_id].append(
            SourceTrace(
                kind="review_queue",
                label=f"Review Queue row #{r.id} ({r.source_type}) — “{desc}”",
            )
        )
        if r.linked_invoice_id is not None:
            invoice_ids_needed.add(r.linked_invoice_id)
            invoice_trace_targets.append((r.posted_journal_entry_id, r.linked_invoice_id))

    for r in conn.execute(
        select(
            ebay_csv_transactions.c.journal_entry_id,
            ebay_csv_transactions.c.id,
            ebay_csv_transactions.c.row_type,
            ebay_csv_transactions.c.order_number,
            ebay_csv_transactions.c.external_ref,
        ).where(ebay_csv_transactions.c.journal_entry_id.in_(entry_ids))
    ).all():
        ref = r.order_number or r.external_ref or "—"
        traces[r.journal_entry_id].append(
            SourceTrace(kind="ebay_csv", label=f"eBay CSV row #{r.id} ({r.row_type}, order/ref {ref})")
        )

    for r in conn.execute(
        select(
            consignment_sales.c.journal_entry_id,
            consignment_sales.c.id,
            consignment_sales.c.consignor_item_ref,
            consignment_sales.c.payout_model,
        ).where(consignment_sales.c.journal_entry_id.in_(entry_ids))
    ).all():
        traces[r.journal_entry_id].append(
            SourceTrace(
                kind="consignment_sale",
                label=f"Consignment sale #{r.id} ({r.consignor_item_ref}, {r.payout_model} payout model)",
            )
        )

    for r in conn.execute(
        select(opening_balances.c.journal_entry_id, opening_balances.c.id).where(
            opening_balances.c.journal_entry_id.in_(entry_ids)
        )
    ).all():
        traces[r.journal_entry_id].append(
            SourceTrace(kind="opening_balance", label=f"Opening balance entry #{r.id}")
        )

    for r in conn.execute(
        select(
            payoneer_withdrawals.c.journal_entry_id,
            payoneer_withdrawals.c.id,
            payoneer_withdrawals.c.withdrawal_date,
        ).where(payoneer_withdrawals.c.journal_entry_id.in_(entry_ids))
    ).all():
        traces[r.journal_entry_id].append(
            SourceTrace(
                kind="payoneer_withdrawal",
                label=f"Payoneer withdrawal #{r.id} ({r.withdrawal_date})",
            )
        )

    for r in conn.execute(
        select(
            fx_revaluations.c.journal_entry_id,
            fx_revaluations.c.id,
            fx_revaluations.c.period_month,
        ).where(fx_revaluations.c.journal_entry_id.in_(entry_ids))
    ).all():
        traces[r.journal_entry_id].append(
            SourceTrace(kind="fx_revaluation", label=f"FX revaluation #{r.id} ({r.period_month})")
        )

    for r in conn.execute(
        select(invoice_journal_links.c.journal_entry_id, invoice_journal_links.c.invoice_id).where(
            invoice_journal_links.c.journal_entry_id.in_(entry_ids)
        )
    ).all():
        invoice_ids_needed.add(r.invoice_id)
        invoice_trace_targets.append((r.journal_entry_id, r.invoice_id))

    if invoice_ids_needed:
        invoice_rows = {
            r.id: r
            for r in conn.execute(
                select(
                    invoices.c.id,
                    invoices.c.vendor_description,
                    invoices.c.amount_idr,
                    invoices.c.purpose,
                    invoices.c.drive_file_name,
                ).where(invoices.c.id.in_(invoice_ids_needed))
            ).all()
        }
        seen: set[tuple[int, int]] = set()
        for entry_id, invoice_id in invoice_trace_targets:
            key = (entry_id, invoice_id)
            if key in seen:
                continue
            seen.add(key)
            inv = invoice_rows.get(invoice_id)
            if inv is None:
                continue
            vendor = inv.vendor_description or inv.drive_file_name or "—"
            traces[entry_id].append(
                SourceTrace(
                    kind="invoice",
                    label=f"Invoice #{inv.id}: {vendor} ({inv.purpose or 'purpose unset'})",
                )
            )

    return traces


def _reversal_links(conn: Connection, entry_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    """Returns (reversed_by, reversal_of) maps for the given entry_ids.

    ``reversed_by[entry_id]`` -> id of the entry that reversed it (this
    entry HAS been reversed).
    ``reversal_of[entry_id]`` -> id of the original entry it reverses (this
    entry IS a reversal).

    ``journal_entries.reversed_by_id`` only stores the forward pointer
    (original -> its reversal), so the reverse direction is derived here by
    inverting that map — cheap, since real reversal activity is a handful of
    rows total (see CLAUDE.md's Definition of done for the two known real
    pairs: 917->947, 922/927/932->991/993/995).
    """
    if not entry_ids:
        return {}, {}

    reversed_by: dict[int, int] = {}
    for r in conn.execute(
        select(journal_entries.c.id, journal_entries.c.reversed_by_id).where(
            journal_entries.c.id.in_(entry_ids)
        )
        .where(journal_entries.c.reversed_by_id.is_not(None))
    ).all():
        reversed_by[r.id] = r.reversed_by_id

    # For entries in scope that might themselves BE a reversal (their id
    # appears as someone else's reversed_by_id), look that direction up too
    # — including originals that may not be in `entry_ids` at all (e.g. an
    # account-filtered view might show the reversal entry without the
    # original touching the same account/period).
    reversal_of: dict[int, int] = {}
    for r in conn.execute(
        select(journal_entries.c.id, journal_entries.c.reversed_by_id).where(
            journal_entries.c.reversed_by_id.in_(entry_ids)
        )
    ).all():
        reversal_of[r.reversed_by_id] = r.id

    return reversed_by, reversal_of


def general_ledger_entries(
    conn: Connection,
    *,
    account_option: AccountOption | None,
    period_month: _dt.date,
    scope_all: bool,
) -> list[GLEntry]:
    account_id = account_option.account_id if account_option else None
    entry_ids = _entry_ids_for_scope(conn, account_id=account_id, period_month=period_month, scope_all=scope_all)
    if not entry_ids:
        return []

    entry_rows = {
        r.id: r
        for r in conn.execute(
            select(
                journal_entries.c.id,
                journal_entries.c.entry_date,
                journal_entries.c.period_month,
                journal_entries.c.source_type,
                journal_entries.c.memo,
                journal_entries.c.reversed_by_id,
            ).where(journal_entries.c.id.in_(entry_ids))
        ).all()
    }

    all_accounts = list_all_accounts(conn)
    account_label_by_id = {a.account_id: a.label for a in all_accounts}

    line_rows = conn.execute(
        select(
            journal_lines.c.journal_entry_id,
            journal_lines.c.account_id,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
            journal_lines.c.id,
        )
        .where(journal_lines.c.journal_entry_id.in_(entry_ids))
        .order_by(journal_lines.c.journal_entry_id, journal_lines.c.id)
    ).all()

    lines_by_entry: dict[int, list[GLLine]] = {}
    for r in line_rows:
        lines_by_entry.setdefault(r.journal_entry_id, []).append(
            GLLine(
                account_id=r.account_id,
                account_label=account_label_by_id.get(r.account_id, f"Account #{r.account_id}"),
                debit_idr=r.debit_amount_idr,
                credit_idr=r.credit_amount_idr,
                ebay_order_ref=r.ebay_order_ref,
                consignor_item_ref=r.consignor_item_ref,
                is_filtered_account=(account_id is not None and r.account_id == account_id),
            )
        )

    reversed_by_map, reversal_of_map = _reversal_links(conn, entry_ids)
    traces = _source_traces(conn, entry_ids)

    ordered_ids = sorted(entry_ids, key=lambda eid: (entry_rows[eid].entry_date, eid))

    entries: list[GLEntry] = []
    for eid in ordered_ids:
        row = entry_rows[eid]
        lines = lines_by_entry.get(eid, [])
        total_debit = sum((l.debit_idr for l in lines), ZERO)
        entries.append(
            GLEntry(
                id=row.id,
                entry_date=row.entry_date,
                period_month=row.period_month,
                source_type=row.source_type,
                memo=row.memo,
                lines=lines,
                total_debit_idr=total_debit,
                reversed_by_id=reversed_by_map.get(eid),
                reversal_of_id=reversal_of_map.get(eid),
                source_traces=traces.get(eid, []),
            )
        )

    if account_option is not None:
        running = (
            ZERO if scope_all else account_balance_before(conn, account_id, period_month, account_option.normal_balance)
        )
        for entry in entries:
            net = ZERO
            for l in entry.lines:
                if l.account_id != account_id:
                    continue
                if account_option.normal_balance == "debit":
                    net += l.debit_idr - l.credit_idr
                else:
                    net += l.credit_idr - l.debit_idr
            running += net
            entry.running_balance_idr = running

    return entries


@bp.route("/")
@login_required
def index():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    all_accounts = list_all_accounts(conn)

    raw_account_id = request.args.get("account_id", "all")
    account_option = None
    if raw_account_id != "all":
        try:
            account_option = _get_account_option(all_accounts, int(raw_account_id))
        except ValueError:
            account_option = None

    scope_all = account_option is not None and request.args.get("scope") == "all"

    opening_balance = None
    if account_option is not None:
        opening_balance = ZERO if scope_all else account_balance_before(
            conn, account_option.account_id, period_month, account_option.normal_balance
        )

    entries = general_ledger_entries(
        conn, account_option=account_option, period_month=period_month, scope_all=scope_all
    )

    return render_template(
        "general_ledger.html",
        all_accounts=all_accounts,
        account_option=account_option,
        period_month=period_month,
        scope_all=scope_all,
        opening_balance=opening_balance,
        closing_balance=entries[-1].running_balance_idr if (account_option and entries) else None,
        entries=entries,
    )
