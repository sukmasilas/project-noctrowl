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
    # Explicit real Drive folder names (ledger.schema's
    # ebay_accounts.drive_folder_name / wallet_groups.drive_folder_name) —
    # NULL when not yet recorded for a given row. Use the
    # ``*_drive_folder_name_resolved`` properties below rather than these
    # raw fields directly; they apply the documented fallback.
    drive_folder_name: str | None
    wallet_group_drive_folder_name: str | None

    @property
    def ebay_account_drive_folder_name_resolved(self) -> str:
        """The real Drive folder name to look in for this eBay account's
        uploads. Prefers the explicit ``drive_folder_name`` field (added
        2026-09-02 to close the "Sync Now looks in the wrong folder" gap —
        see ledger/schema.py); falls back to the old derived-from-display
        -name convention only when no explicit value has been recorded yet,
        so a hypothetical future account without this field set still
        degrades to the previous (imperfect but non-crashing) behavior
        instead of erroring.
        """
        if self.drive_folder_name:
            return self.drive_folder_name
        return f"eBay Account - {self.name}"

    @property
    def wallet_group_drive_folder_name_resolved(self) -> str:
        """Same fallback pattern as above, for the wallet-group's own
        upload folder.
        """
        if self.wallet_group_drive_folder_name:
            return self.wallet_group_drive_folder_name
        return self.wallet_group_name


def list_ebay_accounts(conn: Connection) -> list[EbayAccountOption]:
    rows = conn.execute(
        select(
            ebay_accounts.c.id,
            ebay_accounts.c.name,
            ebay_accounts.c.wallet_group_id,
            ebay_accounts.c.drive_folder_name,
            wallet_groups.c.name.label("wallet_group_name"),
            wallet_groups.c.drive_folder_name.label("wallet_group_drive_folder_name"),
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
            drive_folder_name=r.drive_folder_name,
            wallet_group_drive_folder_name=r.wallet_group_drive_folder_name,
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
