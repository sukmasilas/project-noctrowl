"""Month-end unrealized FX revaluation job — a separate scheduled job from
routine sync (CLAUDE.md's "Scheduling & triggers": "Month-end FX
revaluation runs as its own scheduled job at period close, distinct from
the routine sync. It must always use that specific period's own
end-of-month Kurs Pajak rate — even though the job may actually execute a
few days later during the H+7 revision window, it revalues using the
correct closed period's rate, never a different month's.").

For each active wallet-group (derived from ``webapp.scoping.
list_ebay_accounts`` — never a hardcoded list, per CLAUDE.md's repeated
"don't hardcode a 1:1 eBay-account-to-Payoneer-wallet assumption" rule),
this module:

1. Determines the period that just closed (``scheduling.window.
   closed_period_for``) and looks up THAT period's own end-of-month Kurs
   Pajak rate. If no rate has been seeded for that date, it skips that
   wallet-group/period and logs it clearly — never guesses or falls back
   to the nearest available rate (CLAUDE.md's "never silently guess"
   principle, applied here to a missing reference rate).
2. Computes the wallet-group's Payoneer Wallet USD balance and current
   IDR book value as of that period-end (see ``compute_payoneer_wallet_
   balance`` below — read its docstring, this is a flagged, deliberate
   interpretation of an underspecified computation, not an obvious one).
3. Calls ``ledger.posting.post_unrealized_fx_revaluation`` with those
   values.

FLAGGED INTERPRETATION — money-math implication, explicitly called out to
Main-agent in this milestone's completion report rather than silently
assumed (per this milestone's own build brief, which asked for exactly
this): computing a wallet-group's Payoneer Wallet USD balance from
``journal_lines.amount_usd_ref`` is NOT safe to do by naively summing
debit-minus-credit across every journal line that ever touched that
account. ``ledger.posting.post_unrealized_fx_revaluation``'s own Payoneer
Wallet line stores ``amount_usd_ref = usd_balance`` — i.e. the WHOLE
balance being revalued — on the one line that touches Payoneer Wallet,
not a USD delta, because a revaluation entry never actually moves real
USD; it only restates the account's IDR book value to match a new rate.
If a naive sum included a PRIOR revaluation entry, it would silently
re-add that whole prior balance on top of the real one, corrupting every
subsequent month's computed balance (a genuine, easy-to-miss bug). This
module's ``compute_payoneer_wallet_balance`` therefore explicitly EXCLUDES
``journal_entries.source_type = 'fx_revaluation'`` rows from the USD sum,
while still including them in the IDR book-value sum (where they
correctly belong — restating IDR book value is the entire point of that
entry). This is grounded in the underlying accounting reality (revaluation
never moves real USD), not an arbitrary tie-break, and is covered by a
dedicated regression test (``tests/scheduling/test_fx_revaluation.py``)
proving a second consecutive month's computed balance is NOT corrupted by
the first month's revaluation entry.

IDEMPOTENCY: see ``ledger/schema.py``'s ``ux_fx_revaluations_wallet_group_
period`` unique index (also retrofitted via ``ledger/migrations.py`` for
already-provisioned databases) — the real backstop against a double-post,
per this milestone's brief ("this project has a real history of bugs
here... rely on the DB constraint as the real backstop, not just an
application-level check that could race"). This module's own "already
posted?" pre-check below is a fast-path convenience only (avoids
unnecessary log noise / repeated work on an ordinary sequential re-run —
and in practice a sequential re-run usually self-heals anyway, since a
prior successful revaluation makes the book value already match the
revalued figure, so the second run's diff is naturally zero and
``post_unrealized_fx_revaluation`` posts nothing at all). A genuinely
CONCURRENT second attempt is caught via the ``IntegrityError`` from the
unique index, inside a SAVEPOINT (``conn.begin_nested()``) so the failed
attempt's journal entry is atomically rolled back too — never left as a
dangling, untraceable posted entry with no matching ``fx_revaluations``
row.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ingestion.kurs_pajak import NoKursPajakRateError, lookup_kurs_pajak_rate
from ledger.entities import get_account_id
from ledger.errors import UnknownAccountInstanceError
from ledger.posting import post_unrealized_fx_revaluation
from ledger.schema import fx_revaluations, journal_entries, journal_lines
from scheduling.window import closed_period_for, month_end
from webapp.scoping import list_ebay_accounts

logger = logging.getLogger(__name__)


class MissingUsdReferenceError(Exception):
    """Raised by ``compute_payoneer_wallet_balance`` when it finds a
    non-``fx_revaluation`` Payoneer Wallet journal line with a real
    (nonzero) IDR amount but a NULL ``amount_usd_ref``.

    Self-defending backstop (QA-requested, 2026-09, added alongside the
    fix for the bug that made this possible in the first place — see
    ``ledger.posting.post_consignor_reimbursement``'s docstring and
    ``ingestion.matching._usd_reference_kwargs``): silently treating such
    a line as a $0 USD movement would corrupt the computed balance exactly
    the way the original bug did. Per CLAUDE.md's "never silently guess"
    principle, a caller hitting this must stop and get a human to either
    backfill the missing reference on the offending line(s) or confirm
    it's genuinely a $0 USD event — never estimate it here.
    """


def compute_payoneer_wallet_balance(
    conn: Connection, *, wallet_group_id: int, as_of_date: _dt.date
) -> tuple[Decimal, Decimal]:
    """Returns ``(usd_balance, current_book_value_idr)`` for this
    wallet-group's Payoneer Wallet, computed from real posted journal
    lines dated on or before ``as_of_date``. See module docstring for why
    the USD sum excludes prior ``fx_revaluation`` entries while the IDR
    sum includes them.

    Raises ``UnknownAccountInstanceError`` (via ``ledger.entities.
    get_account_id``) if this wallet-group has no Payoneer Wallet account
    set up yet — callers should treat that as "nothing to revalue yet",
    not crash the whole run.

    Raises ``MissingUsdReferenceError`` if any real (nonzero-IDR),
    non-``fx_revaluation`` line touching this Payoneer Wallet has no
    ``amount_usd_ref`` at all — see that exception's docstring. Callers
    should treat this as "can't safely compute a balance for this
    wallet-group yet", not crash the whole run either.
    """
    payoneer_wallet_id = get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=wallet_group_id)

    idr_query = (
        select(func.sum(journal_lines.c.debit_amount_idr) - func.sum(journal_lines.c.credit_amount_idr))
        .select_from(journal_lines.join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id))
        .where(journal_lines.c.account_id == payoneer_wallet_id)
        .where(journal_entries.c.entry_date <= as_of_date)
    )
    book_value_raw = conn.execute(idr_query).scalar()
    current_book_value_idr = Decimal(book_value_raw) if book_value_raw is not None else Decimal("0")

    # Defensive backstop: any real, non-fx_revaluation line with a NULL
    # amount_usd_ref would otherwise silently contribute $0 to the USD sum
    # below — exactly the bug this module was fixed for. Checked BEFORE
    # computing the USD sum so a caller never receives a number that's
    # already silently wrong.
    missing_ref_rows = conn.execute(
        select(journal_entries.c.id, journal_entries.c.entry_date, journal_entries.c.source_type)
        .select_from(journal_lines.join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id))
        .where(journal_lines.c.account_id == payoneer_wallet_id)
        .where(journal_entries.c.entry_date <= as_of_date)
        .where(journal_entries.c.source_type != "fx_revaluation")
        .where(journal_lines.c.amount_usd_ref.is_(None))
        .where((journal_lines.c.debit_amount_idr > 0) | (journal_lines.c.credit_amount_idr > 0))
    ).all()
    if missing_ref_rows:
        details = ", ".join(
            f"journal_entry_id={r.id} ({r.entry_date.isoformat()}, source_type={r.source_type!r})"
            for r in missing_ref_rows
        )
        raise MissingUsdReferenceError(
            f"wallet_group_id={wallet_group_id}: {len(missing_ref_rows)} journal line(s) touching this "
            f"Payoneer Wallet have a real IDR amount but no amount_usd_ref, so a USD balance can't be "
            f"computed safely as of {as_of_date.isoformat()}: {details}. Backfill amount_usd_ref on "
            "these lines (or confirm they're genuinely $0 USD events) before revaluing this wallet-group."
        )

    # Excludes fx_revaluation entries — see module docstring's "FLAGGED
    # INTERPRETATION" note for why this is required for correctness, not
    # an arbitrary choice.
    usd_debit = case((journal_lines.c.debit_amount_idr > 0, journal_lines.c.amount_usd_ref), else_=0)
    usd_credit = case((journal_lines.c.credit_amount_idr > 0, journal_lines.c.amount_usd_ref), else_=0)
    usd_query = (
        select(func.sum(usd_debit) - func.sum(usd_credit))
        .select_from(journal_lines.join(journal_entries, journal_entries.c.id == journal_lines.c.journal_entry_id))
        .where(journal_lines.c.account_id == payoneer_wallet_id)
        .where(journal_entries.c.entry_date <= as_of_date)
        .where(journal_entries.c.source_type != "fx_revaluation")
    )
    usd_balance_raw = conn.execute(usd_query).scalar()
    usd_balance = Decimal(usd_balance_raw) if usd_balance_raw is not None else Decimal("0")

    return usd_balance, current_book_value_idr


@dataclass
class WalletGroupFxOutcome:
    wallet_group_id: int
    period_month: _dt.date
    status: str  # see run_fx_revaluation's docstring for the full set of values
    detail: str | None = None
    journal_entry_id: int | None = None


@dataclass
class FxRevaluationRunResult:
    ran_at: _dt.date
    period_month: _dt.date
    period_end: _dt.date
    outcomes: list[WalletGroupFxOutcome] = field(default_factory=list)


def run_fx_revaluation(engine: Engine, *, today: _dt.date | None = None) -> FxRevaluationRunResult:
    """Revalues every active wallet-group's Payoneer Wallet for whichever
    period ``scheduling.window.closed_period_for(today)`` says just
    closed. Always runs (there's no "outside the window" no-op here, per
    CLAUDE.md — this job's OWN cron cadence decides how often it's
    invoked; see ``scripts/scheduled_fx_revaluation.py`` for the chosen
    cadence and reasoning). Each wallet-group's outcome is independent —
    one wallet-group's missing Kurs Pajak rate never blocks another's.

    Outcome statuses: ``'posted'`` (a new fx_revaluations row + journal
    entry), ``'no_change'`` (revalued amount exactly matched book value —
    nothing to post, per ``post_unrealized_fx_revaluation``'s own
    contract), ``'already_posted'`` (fast-path pre-check found an existing
    row for this exact wallet-group/period), ``'race_lost'`` (a genuinely
    concurrent second attempt lost to the DB unique-index backstop),
    ``'missing_kurs_pajak_rate'`` (no seeded rate covers this period-end —
    never guessed), ``'missing_payoneer_wallet'`` (this wallet-group has
    no Payoneer Wallet account set up yet), ``'missing_usd_reference'``
    (see ``MissingUsdReferenceError`` — a real Payoneer Wallet line dated
    on or before this period-end has no ``amount_usd_ref`` at all; never
    guessed as $0, this wallet-group is skipped until a human backfills
    the missing reference).
    """
    today = today or _dt.date.today()
    period_month = closed_period_for(today)
    period_end = month_end(period_month)

    outcomes: list[WalletGroupFxOutcome] = []
    with engine.connect() as conn:
        accounts = list_ebay_accounts(conn)
        wallet_group_ids = sorted({a.wallet_group_id for a in accounts})
        if not wallet_group_ids:
            logger.warning("FX revaluation: no active eBay accounts/wallet-groups configured — nothing to revalue.")
        for wg_id in wallet_group_ids:
            outcomes.append(
                _revalue_wallet_group(conn, wallet_group_id=wg_id, period_month=period_month, period_end=period_end)
            )

    return FxRevaluationRunResult(ran_at=today, period_month=period_month, period_end=period_end, outcomes=outcomes)


def _revalue_wallet_group(
    conn: Connection, *, wallet_group_id: int, period_month: _dt.date, period_end: _dt.date
) -> WalletGroupFxOutcome:
    # Fast-path only — see module docstring's IDEMPOTENCY note for why the
    # real backstop is the DB unique index, not this SELECT-before-INSERT.
    existing = conn.execute(
        select(fx_revaluations.c.id)
        .where(fx_revaluations.c.wallet_group_id == wallet_group_id)
        .where(fx_revaluations.c.period_month == period_month)
    ).first()
    if existing is not None:
        conn.rollback()
        logger.info(
            "FX revaluation: wallet_group_id=%s period=%s already posted (fx_revaluations.id=%s) — skipping.",
            wallet_group_id,
            period_month,
            existing.id,
        )
        return WalletGroupFxOutcome(
            wallet_group_id, period_month, status="already_posted", detail=f"fx_revaluations.id={existing.id}"
        )

    try:
        kemenkeu_rate = lookup_kurs_pajak_rate(conn, period_end)
    except NoKursPajakRateError as exc:
        conn.rollback()
        logger.warning(
            "FX revaluation: wallet_group_id=%s period=%s — %s — skipping this wallet-group, never guessing a rate.",
            wallet_group_id,
            period_month,
            exc,
        )
        return WalletGroupFxOutcome(wallet_group_id, period_month, status="missing_kurs_pajak_rate", detail=str(exc))

    try:
        usd_balance, book_value_idr = compute_payoneer_wallet_balance(
            conn, wallet_group_id=wallet_group_id, as_of_date=period_end
        )
    except UnknownAccountInstanceError as exc:
        conn.rollback()
        logger.warning(
            "FX revaluation: wallet_group_id=%s — Payoneer Wallet account not set up yet — %s",
            wallet_group_id,
            exc,
        )
        return WalletGroupFxOutcome(wallet_group_id, period_month, status="missing_payoneer_wallet", detail=str(exc))
    except MissingUsdReferenceError as exc:
        conn.rollback()
        logger.error(
            "FX revaluation: wallet_group_id=%s period=%s — can't safely compute a USD balance, refusing "
            "to guess — %s",
            wallet_group_id,
            period_month,
            exc,
        )
        return WalletGroupFxOutcome(wallet_group_id, period_month, status="missing_usd_reference", detail=str(exc))

    memo = (
        f"Month-end unrealized FX revaluation for {period_month.isoformat()} "
        f"(Kurs Pajak EOM rate as of {period_end.isoformat()})"
    )

    try:
        with conn.begin_nested():
            entry_id = post_unrealized_fx_revaluation(
                conn,
                wallet_group_id=wallet_group_id,
                period_month=period_month,
                usd_balance=usd_balance,
                current_book_value_idr=book_value_idr,
                kemenkeu_eom_rate_idr=kemenkeu_rate,
                memo=memo,
            )
    except IntegrityError:
        conn.rollback()
        logger.warning(
            "FX revaluation: wallet_group_id=%s period=%s — lost a concurrent race against another run "
            "(ux_fx_revaluations_wallet_group_period unique index) — treated as already posted.",
            wallet_group_id,
            period_month,
        )
        return WalletGroupFxOutcome(wallet_group_id, period_month, status="race_lost")

    conn.commit()

    if entry_id is None:
        logger.info(
            "FX revaluation: wallet_group_id=%s period=%s — revalued amount exactly matches current book value, "
            "nothing to post.",
            wallet_group_id,
            period_month,
        )
        return WalletGroupFxOutcome(wallet_group_id, period_month, status="no_change")

    logger.info(
        "FX revaluation: wallet_group_id=%s period=%s — posted journal_entry_id=%s.",
        wallet_group_id,
        period_month,
        entry_id,
    )
    return WalletGroupFxOutcome(wallet_group_id, period_month, status="posted", journal_entry_id=entry_id)
