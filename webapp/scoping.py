"""Account/period selector helpers shared by every screen's chrome.

Nothing here hardcodes "there's exactly one eBay account" even though
that's true for the prototype (CLAUDE.md's Prototype scope) — it queries
``ebay_accounts``/``wallet_groups`` for whatever actually exists, same
principle as the rest of the ledger/ingestion code never assuming a fixed
account count.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.schema import ebay_accounts, journal_entries, wallet_groups


@dataclass
class EbayAccountOption:
    id: int
    name: str
    wallet_group_id: int
    wallet_group_name: str
    wallet_group_is_shared: bool


def list_ebay_accounts(conn: Connection) -> list[EbayAccountOption]:
    rows = conn.execute(
        select(
            ebay_accounts.c.id,
            ebay_accounts.c.name,
            ebay_accounts.c.wallet_group_id,
            wallet_groups.c.name.label("wallet_group_name"),
        )
        .join(wallet_groups, wallet_groups.c.id == ebay_accounts.c.wallet_group_id)
        .where(ebay_accounts.c.is_active.is_(True))
        .order_by(ebay_accounts.c.id)
    ).all()

    # An account's wallet-group is "shared" if >1 active eBay account
    # references it — computed once here rather than per-row so callers
    # (Cash Flow's shared-pool banner — see reporting.py) don't each
    # re-derive it.
    counts: dict[int, int] = {}
    for r in rows:
        counts[r.wallet_group_id] = counts.get(r.wallet_group_id, 0) + 1

    return [
        EbayAccountOption(
            id=r.id,
            name=r.name,
            wallet_group_id=r.wallet_group_id,
            wallet_group_name=r.wallet_group_name,
            wallet_group_is_shared=counts[r.wallet_group_id] > 1,
        )
        for r in rows
    ]


def get_ebay_account(conn: Connection, ebay_account_id: int) -> EbayAccountOption | None:
    for opt in list_ebay_accounts(conn):
        if opt.id == ebay_account_id:
            return opt
    return None


def list_available_periods(conn: Connection) -> list[_dt.date]:
    """Every distinct period_month with at least one posted journal entry,
    most recent first. Used to default the period selector to "the most
    recent period with data" (per ui-ux-design.md's shared chrome spec)
    instead of always defaulting to the current calendar month, which may
    have nothing posted yet.
    """
    rows = conn.execute(
        select(journal_entries.c.period_month).distinct().order_by(journal_entries.c.period_month.desc())
    ).all()
    return [r.period_month for r in rows]


def default_period(conn: Connection) -> _dt.date:
    periods = list_available_periods(conn)
    if periods:
        return periods[0]
    return _dt.date.today().replace(day=1)


def parse_period(value: str | None, conn: Connection) -> _dt.date:
    """Parse a 'YYYY-MM' query-string value, falling back to the default
    period when missing/invalid rather than raising — a malformed/missing
    period selector should degrade to "show me something sensible", not
    500.
    """
    if value:
        try:
            year_str, month_str = value.split("-")
            return _dt.date(int(year_str), int(month_str), 1)
        except (ValueError, TypeError):
            pass
    return default_period(conn)
