"""Shared test helpers."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ledger.schema import account_types, accounts, journal_entries, journal_lines


def get_lines(conn, journal_entry_id: int):
    return conn.execute(
        select(
            journal_lines.c.account_id,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.amount_usd_ref,
            journal_lines.c.fx_rate_used,
            journal_lines.c.category_id,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
            account_types.c.code.label("account_code"),
        )
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .where(journal_lines.c.journal_entry_id == journal_entry_id)
    ).all()


def lines_by_code(conn, journal_entry_id: int) -> dict[str, list]:
    out: dict[str, list] = {}
    for row in get_lines(conn, journal_entry_id):
        out.setdefault(row.account_code, []).append(row)
    return out


def total_debit(lines) -> Decimal:
    return sum((l.debit_amount_idr for l in lines), Decimal("0"))


def total_credit(lines) -> Decimal:
    return sum((l.credit_amount_idr for l in lines), Decimal("0"))


def assert_balanced(conn, journal_entry_id: int) -> None:
    lines = get_lines(conn, journal_entry_id)
    assert total_debit(lines) == total_credit(lines), (
        f"Entry {journal_entry_id} is not balanced: debits={total_debit(lines)} "
        f"credits={total_credit(lines)}"
    )


def get_source_type(conn, journal_entry_id: int) -> str:
    row = conn.execute(
        select(journal_entries.c.source_type).where(journal_entries.c.id == journal_entry_id)
    ).first()
    return row.source_type
