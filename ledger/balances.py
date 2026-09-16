"""Shared "account balance as of a date" helper.

Extracted (2026-09, reconciliation-gap-detection feature) from
``webapp.reporting._account_balance_through`` — originally a webapp-only
private helper — so ``ingestion.reconciliation`` can reuse the EXACT same
balance computation instead of re-implementing it. This is the correct
layering direction: ``ledger`` has no dependents of its own (``ingestion``
depends on ``ledger``; ``webapp`` depends on both), so a shared helper
belongs here, not in ``webapp`` — moving/duplicating this into
``ingestion`` importing ``webapp.reporting`` would invert that direction
for no reason (``scheduling.fx_revaluation``'s existing ``scheduling ->
webapp`` import is a separate, pre-existing case elsewhere in this
project, not a precedent to extend to ``ingestion`` too).

``webapp.reporting`` keeps its own ``_account_balance_through`` name (for
every existing call site) but delegates to ``account_balance_through``
below rather than keeping a second, drifting copy of the same query.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.schema import journal_entries, journal_lines

ZERO = Decimal("0")


def account_balance_through(
    conn: Connection, account_id: int, period_month: _dt.date, normal_balance: str
) -> Decimal:
    """Signed balance of ``account_id`` as of the END of ``period_month``
    (inclusive) — sums every ``journal_lines`` row on a ``journal_entries``
    with ``period_month <= period_month``. Debit-normal accounts (assets,
    e.g. BCA Main/Bridging) return debit-credit; credit-normal accounts
    (liabilities/equity/revenue) return credit-debit.
    """
    rows = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(journal_lines.c.account_id == account_id)
        .where(journal_entries.c.period_month <= period_month)
    ).all()
    if normal_balance == "debit":
        return sum((r.debit_amount_idr - r.credit_amount_idr for r in rows), ZERO)
    return sum((r.credit_amount_idr - r.debit_amount_idr for r in rows), ZERO)


def account_balance_through_by_reference(
    conn: Connection, account_id: int, period_month: _dt.date, normal_balance: str, reference: str
) -> Decimal:
    """Same computation as ``account_balance_through`` above, but further
    restricted to ``journal_lines`` whose ``consignor_item_ref`` exactly
    matches ``reference`` — the per-sub-entity balance within a subsidiary
    ledger (see ``webapp/subsidiary_ledger_bp.py``).

    This is the load-bearing identity a subsidiary ledger exists to prove:
    summing this across every distinct ``reference`` that has ever posted to
    ``account_id`` must always tie out exactly to ``account_balance_through``'s
    own aggregate for that same ``account_id``/``period_month`` — any gap
    means some line posted to the control account without a reference (or a
    reference typo), which is exactly the kind of thing a subsidiary ledger
    is supposed to surface, not hide.

    Deliberately a new, separate function rather than adding an optional
    ``reference=`` parameter to ``account_balance_through`` — the two callers
    (whole-account balance vs. one-reference-within-an-account balance) have
    different enough call shapes (no caller ever wants to filter
    ``account_balance_through`` by reference AND get the unfiltered
    behavior from the same call site) that a shared function with an
    optional filter would only add a branch no one needs.
    """
    rows = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(journal_lines.c.account_id == account_id)
        .where(journal_lines.c.consignor_item_ref == reference)
        .where(journal_entries.c.period_month <= period_month)
    ).all()
    if normal_balance == "debit":
        return sum((r.debit_amount_idr - r.credit_amount_idr for r in rows), ZERO)
    return sum((r.credit_amount_idr - r.debit_amount_idr for r in rows), ZERO)


def distinct_references_for_account(conn: Connection, account_id: int, period_month: _dt.date) -> list[str]:
    """Every distinct non-null ``consignor_item_ref`` that has posted to
    ``account_id`` on or before the end of ``period_month`` — the sub-entity
    list for a subsidiary ledger screen. Scoped "through period end" (not
    "within period only") for the same reason a balance is always "as of a
    date": a consignor/employee whose only activity is in a later period
    shouldn't appear on an earlier period's subsidiary ledger.
    """
    rows = conn.execute(
        select(journal_lines.c.consignor_item_ref)
        .distinct()
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(journal_lines.c.account_id == account_id)
        .where(journal_lines.c.consignor_item_ref.is_not(None))
        .where(journal_entries.c.period_month <= period_month)
    ).all()
    return sorted(r.consignor_item_ref for r in rows)


def _prior_month(d: _dt.date) -> _dt.date:
    if d.month == 1:
        return d.replace(year=d.year - 1, month=12)
    return d.replace(month=d.month - 1)


def account_balance_before(
    conn: Connection, account_id: int, period_month: _dt.date, normal_balance: str
) -> Decimal:
    """Signed balance of ``account_id`` as of the START of ``period_month``
    — i.e. the ledger's own computed "opening balance" for that period.

    Balance through the end of the PRIOR month, PLUS any ``source_type =
    'opening_balance'`` entries actually dated IN ``period_month`` itself.

    Why the second half is needed (found 2026-09, reconciliation-gap
    -detection backfill against the real database): ``ledger.posting.
    post_opening_balance`` records a wallet/bank account's real balance
    "as of just before ledger-tracking began" (see that function's
    docstring), but in practice it's booked with ``entry_date`` = the
    FIRST DAY of the very first period being tracked (e.g. 2026-05-01 for
    this business's real May 2026 start) — there is no earlier date to
    book it on. That entry's ``period_month`` is therefore May, the same
    period it represents the OPENING of. A naive "balance through the
    prior month only" definition would exclude it entirely, making a
    freshly-onboarded account's computed opening balance look like a
    large, spurious discrepancy against the real statement's own printed
    opening balance — exactly the false-positive this carve-out prevents.
    A normal, non-opening-balance entry dated on that same first day (a
    same-day sale, say) is correctly still excluded — only the
    'opening_balance' source_type is special-cased in.
    """
    prior = account_balance_through(conn, account_id, _prior_month(period_month), normal_balance)

    rows = conn.execute(
        select(journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr)
        .join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id)
        .where(journal_lines.c.account_id == account_id)
        .where(journal_entries.c.period_month == period_month)
        .where(journal_entries.c.source_type == "opening_balance")
    ).all()
    if normal_balance == "debit":
        opening_entry_amount = sum((r.debit_amount_idr - r.credit_amount_idr for r in rows), ZERO)
    else:
        opening_entry_amount = sum((r.credit_amount_idr - r.debit_amount_idr for r in rows), ZERO)

    return prior + opening_entry_amount
