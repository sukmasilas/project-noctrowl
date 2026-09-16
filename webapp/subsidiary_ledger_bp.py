"""Subsidiary Ledger screen: breaks an aggregate GL control account down by
the individual real-world party behind each transaction — one level more
granular than ``webapp/general_ledger_bp.py`` (read that module's docstring
first; this screen follows the same conceptual approach).

Two control accounts are wired up for now, per Main-agent's brief:
- CONSIGNOR_PAYABLE (aggregate liability) — by individual consignor.
- EMPLOYEE_LOAN_RECEIVABLE (aggregate asset) — by individual employee.

Both already carry a per-transaction party reference on every posted line
(``journal_lines.consignor_item_ref`` — reused for both consignors and
employees, see CLAUDE.md's Core accounting rules and
``ledger/chart_of_accounts.py``'s EMPLOYEE_LOAN_RECEIVABLE note). Payroll is
explicitly NOT added as a third subsidiary ledger yet — not every payroll
row carries an employee reference today (only loan-repayment rows do), so a
Payroll subsidiary ledger would silently misrepresent employees with no
loan as having zero payroll activity. Revisit once that gap is closed.

The defining property of a subsidiary ledger, and the one thing this screen
must show explicitly rather than assume: the sum of every sub-entity's own
balance must tie out EXACTLY to the control account's own aggregate balance
(computed the identical way General Ledger computes it). See
``_reconciliation`` below and the "Reconciliation check" section of the
template — a real, visible warning banner if it ever doesn't match, same
convention as the Balance Sheet's own Assets = Liabilities + Equity check.

``SUBSIDIARY_LEDGER_ACCOUNTS`` below is a small, explicit, documented list —
deliberately NOT auto-detected from the schema (e.g. "any account that has a
non-null consignor_item_ref on any line" would also catch invoice-matched
COGS lines like "invoice:123" tagged via that same generic field, which are
NOT a real subsidiary-ledger sub-entity in the accounting sense). Adding a
third control account later means adding one entry here, nothing else.

Read-only, same as every other report/register in this app — no
editing/posting capability lives here.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from flask import Blueprint, render_template, request
from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.balances import (
    account_balance_through,
    account_balance_through_by_reference,
    distinct_references_for_account,
)
from ledger.schema import journal_entries, journal_lines
from webapp.auth import login_required
from webapp.db import get_db
from webapp.general_ledger_bp import GeneralLedgerRow, _reversal_links_for_entries
from webapp.journal_entries_bp import AccountOption, list_all_accounts
from webapp.scoping import parse_period

bp = Blueprint("subsidiary_ledger", __name__, url_prefix="/subsidiary-ledger")

ZERO = Decimal("0")

# The explicit, documented set of control accounts that have a subsidiary
# ledger — see module docstring for why this isn't auto-detected. Order here
# is the order shown in the account selector dropdown.
SUBSIDIARY_LEDGER_ACCOUNTS: list[str] = [
    "CONSIGNOR_PAYABLE",
    "EMPLOYEE_LOAN_RECEIVABLE",
]


@dataclass
class SubEntityBalance:
    reference: str
    balance_idr: Decimal


@dataclass
class Reconciliation:
    control_account_balance_idr: Decimal
    sum_of_sub_entities_idr: Decimal

    @property
    def difference_idr(self) -> Decimal:
        return self.sum_of_sub_entities_idr - self.control_account_balance_idr

    @property
    def matches(self) -> bool:
        return self.difference_idr == ZERO


def list_subsidiary_ledger_accounts(conn: Connection) -> list[AccountOption]:
    """The ``AccountOption`` (with its ``normal_balance``, needed for every
    balance computation below) for each code in ``SUBSIDIARY_LEDGER_ACCOUNTS``
    that actually exists in this database yet. Skips a not-yet-provisioned
    control account gracefully rather than erroring — same "no data yet"
    tolerance every other screen in this app follows.
    """
    all_accounts = {opt.account_type_code: opt for opt in list_all_accounts(conn)}
    return [all_accounts[code] for code in SUBSIDIARY_LEDGER_ACCOUNTS if code in all_accounts]


def sub_entity_balances(
    conn: Connection, *, account_option: AccountOption, period_month: _dt.date
) -> list[SubEntityBalance]:
    """Every distinct sub-entity that has posted to ``account_option`` on or
    before the end of ``period_month``, each with its own balance as of that
    date. Empty list (not an error) when the control account has no real
    activity yet — e.g. CONSIGNOR_PAYABLE with zero real CONSIGN- sales.
    """
    references = distinct_references_for_account(conn, account_option.account_id, period_month)
    return [
        SubEntityBalance(
            reference=ref,
            balance_idr=account_balance_through_by_reference(
                conn, account_option.account_id, period_month, account_option.normal_balance, ref
            ),
        )
        for ref in references
    ]


def reconciliation(
    conn: Connection, *, account_option: AccountOption, period_month: _dt.date, balances: list[SubEntityBalance]
) -> Reconciliation:
    control_balance = account_balance_through(conn, account_option.account_id, period_month, account_option.normal_balance)
    total = sum((b.balance_idr for b in balances), ZERO)
    return Reconciliation(control_account_balance_idr=control_balance, sum_of_sub_entities_idr=total)


def sub_entity_rows(
    conn: Connection, *, account_option: AccountOption, period_month: _dt.date, reference: str
) -> list[GeneralLedgerRow]:
    """That one sub-entity's own subset of journal lines on this control
    account, chronological, with its OWN running balance — reuses
    ``GeneralLedgerRow``'s exact shape so the drill-down table can reuse
    General Ledger's row template/style directly (see module docstring).

    Scoped "through period end" (all history up to and including the
    selected period), starting the running balance at zero — there is no
    separate "opening balance carried from a prior screen" concept here
    (unlike General Ledger's whole-account view, a consignor/employee
    sub-entity never has an ``opening_balance`` entry of its own), so this
    walk's own history through the period IS the full picture.
    """
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
        .select_from(journal_lines.join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id))
        .where(journal_lines.c.account_id == account_id)
        .where(journal_lines.c.consignor_item_ref == reference)
        .where(journal_entries.c.period_month <= period_month)
        .order_by(journal_entries.c.entry_date, journal_lines.c.journal_entry_id, journal_lines.c.id)
    )
    rows = conn.execute(query).all()
    if not rows:
        return []

    entry_ids = sorted({r.journal_entry_id for r in rows})
    reversed_by_map, reversal_of_map = _reversal_links_for_entries(conn, entry_ids)

    running = ZERO
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


def _get_account_option(options: list[AccountOption], code: str) -> AccountOption | None:
    for opt in options:
        if opt.account_type_code == code:
            return opt
    return None


@bp.route("/")
@login_required
def index():
    conn = get_db()
    period_month = parse_period(request.args.get("period"), conn)
    ledger_accounts = list_subsidiary_ledger_accounts(conn)

    if not ledger_accounts:
        return render_template(
            "subsidiary_ledger.html",
            ledger_accounts=[],
            account_option=None,
            period_month=period_month,
            balances=[],
            recon=None,
            selected_reference=None,
            entity_rows=[],
        )

    requested_code = request.args.get("account")
    account_option = _get_account_option(ledger_accounts, requested_code) if requested_code else None
    if account_option is None:
        account_option = ledger_accounts[0]

    balances = sub_entity_balances(conn, account_option=account_option, period_month=period_month)
    recon = reconciliation(conn, account_option=account_option, period_month=period_month, balances=balances)

    selected_reference = request.args.get("entity") or None
    entity_rows: list[GeneralLedgerRow] = []
    if selected_reference:
        entity_rows = sub_entity_rows(
            conn, account_option=account_option, period_month=period_month, reference=selected_reference
        )

    return render_template(
        "subsidiary_ledger.html",
        ledger_accounts=ledger_accounts,
        account_option=account_option,
        period_month=period_month,
        balances=balances,
        recon=recon,
        selected_reference=selected_reference,
        entity_rows=entity_rows,
    )
