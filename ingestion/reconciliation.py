"""Automated reconciliation-gap detection.

Compares the ledger's own computed balance for an account against what the
source bank statement document itself states as its printed opening/
closing balance. This closes the exact class of gap found manually this
week (a missing Payoneer opening balance, a missing Bridging Account
opening balance, a mis-posted transaction) — all of which were only found
because a human happened to compare the ledger against the real statement
by hand.

DETECTION AND SURFACING ONLY. This module never corrects a discrepancy,
never reopens or reverses a posted journal entry, and never auto-adjusts
the ledger — that's a separate, deliberately deferred decision (see
CLAUDE.md's "Correcting a posted review-queue row" item). If a real gap is
found here, it is recorded so a human can investigate; nothing in this
module (or anywhere downstream of it — webapp.finalization,
webapp/bank_reconciliation_bp.py, renamed 2026-09-16 from
webapp/data_quality_bp.py — pure rename, no logic change) can act on it
beyond showing it.

ONLY ACCOUNTS WITH A REAL PARSED (opening, closing) BALANCE PAIR ARE EVER
CHECKED. Today that's exactly two document formats:
  - BCA statements (ingestion.bank_statement.BcaStatementParseResult) — now
    has both opening_balance_idr and closing_balance_idr (the closing-
    balance extension added alongside this module).
  - Mandiri e-Statements (ingestion.mandiri_statement.
    MandiriStatementParseResult) — already had both.
Payoneer's currently-used recurring CSV export has NO balance column at
all, and the eBay Wallet has no bank-statement-equivalent document at all
— there is deliberately no forced/fabricated check for either. This is not
hardcoded to "exactly BCA Main + Mandiri Bridging" by account NAME: this
module's own function (``check_account_reconciliation``) takes any
account_id plus a statement-stated opening/closing pair and works
identically regardless of caller — a future document format that starts
carrying a real balance figure gets the same treatment automatically, by
its own caller passing that data in, with zero change needed here. See
``ingestion.sync.sync_bank_statement`` for the actual wiring/mapping from
document_type to which ledger account to check.

MATERIALITY THRESHOLD — Rp 1 (one whole Rupiah):
CORRECTION (2026-09, QA finding): an earlier version of this note claimed
"nothing this system ever posts can introduce a sub-Rupiah discrepancy" —
that overclaims what the code actually guarantees and has been corrected
below. ``ledger.posting.round_idr`` rounds most posted amounts to the
nearest whole Rupiah (sales, refunds, COGS, consignment, FX, Payoneer
withdrawals — "IDR has no practical sub-unit" is this project's own stated
rule, see that function's docstring), but NOT every posting path: interest
-income lines (``ledger.posting.post_interest_income``/
``post_interest_income_line``, and the generalized ``post_income_line`` that
any "other" Other-Income/Expense bank line also goes through — see
``ingestion.matching._post_one_row``) post ``amount_idr`` straight through
from the parsed bank-statement line, unrounded. Real BCA statements do
contain fractional-Rupiah BUNGA/PAJAK BUNGA lines (e.g. Rp 1,319.32), and
these post to the ledger at that exact fractional value — confirmed against
the real data, not assumed away.

The Rp 1 threshold still holds, for a narrower and more precise reason:
every one of those fractional-Rupiah postings is drawn 1:1 from the SAME
statement being reconciled against here — the cents the ledger posts for a
BUNGA/PAJAK BUNGA line are the exact cents that line already contributes to
this same statement's own printed MUTASI CR/DB totals and SALDO AKHIR
figure. They can never diverge from the statement, so they can never be the
SOURCE of a discrepancy — at most they pass their own fractional value
through unchanged on both sides of the comparison. Every OTHER
bank/Payoneer-sourced posting category in the real data (sales, COGS,
transfers, fees, payouts) IS rounded to whole Rupiah via ``round_idr``
before posting. So a genuinely missing, duplicated, or wrong transaction —
the actual thing this check exists to catch — is always a whole-Rupiah
amount in the real data this project runs against today, and can never
manifest as a residual smaller than Rp 1. A sub-Rupiah residual (e.g. the
Rp 0.43 / Rp 0.17 found during this feature's own real-database backfill,
after a genuine posting bug had already been fixed) is therefore
quantization noise from comparing a statement's own fractional-Rupiah
figure against the ledger — real, but not actionable, and not evidence of
any further gap. Anything >= Rp 1 reflects an actual, traceable Rupiah
-level difference and is flagged as material.

(If a future posting path ever introduces an UNROUNDED amount that does
NOT trace 1:1 back to the same statement being checked — e.g. a computed
figure with its own independent fractional rounding — this reasoning would
no longer hold and the threshold should be revisited.)
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.balances import account_balance_before, account_balance_through
from ledger.schema import account_types, accounts, reconciliation_checks

# See module docstring's "MATERIALITY THRESHOLD" section for the full
# reasoning (corrected 2026-09, QA finding): NOT "nothing ever posts a
# sub-Rupiah amount" (interest-income lines do) — the threshold holds
# because those fractional postings are drawn 1:1 from the same statement
# being reconciled against, so they never diverge from it; a real gap is
# always whole-Rupiah in today's data.
MATERIALITY_THRESHOLD_IDR = Decimal("1")


class UnknownReconciliationAccountError(ValueError):
    """Raised when ``check_account_reconciliation`` is given an account_id
    with no matching ``accounts`` row."""


@dataclass
class ReconciliationResult:
    id: int
    account_id: int
    period_month: _dt.date
    expected_opening_idr: Decimal
    actual_opening_idr: Decimal
    opening_discrepancy_idr: Decimal
    expected_closing_idr: Decimal
    actual_closing_idr: Decimal
    closing_discrepancy_idr: Decimal
    is_material: bool

    @property
    def matches(self) -> bool:
        return not self.is_material


def is_material_discrepancy(delta: Decimal) -> bool:
    return abs(delta) >= MATERIALITY_THRESHOLD_IDR


def check_account_reconciliation(
    conn: Connection,
    *,
    account_id: int,
    period_month: _dt.date,
    statement_opening_idr: Decimal,
    statement_closing_idr: Decimal,
    source_document_id: int | None = None,
) -> ReconciliationResult:
    """Compare a bank statement's own printed opening/closing balance for
    ``account_id``/``period_month`` against the ledger's own computed
    balance (via ``ledger.balances``), and persist the result.

    Idempotent by design: a second call for the same (account_id,
    period_month) — e.g. a routine sync re-processing a period whose
    statement was already ingested — UPDATEs the existing row in place
    (``ux_reconciliation_checks_account_period`` is the real DB-level
    backstop against ever accumulating a duplicate), it never appends a
    second row for the same account/period.

    Discrepancy sign convention: ``actual - expected`` (ledger minus
    statement) — positive means the ledger shows MORE than the statement
    states.
    """
    row = conn.execute(
        select(account_types.c.normal_balance)
        .select_from(accounts.join(account_types, account_types.c.id == accounts.c.account_type_id))
        .where(accounts.c.id == account_id)
    ).first()
    if row is None:
        raise UnknownReconciliationAccountError(f"No accounts row with id={account_id}")
    normal_balance = row.normal_balance

    actual_opening = account_balance_before(conn, account_id, period_month, normal_balance)
    actual_closing = account_balance_through(conn, account_id, period_month, normal_balance)

    opening_discrepancy = actual_opening - statement_opening_idr
    closing_discrepancy = actual_closing - statement_closing_idr
    is_material = is_material_discrepancy(opening_discrepancy) or is_material_discrepancy(closing_discrepancy)

    values = dict(
        expected_opening_idr=statement_opening_idr,
        actual_opening_idr=actual_opening,
        opening_discrepancy_idr=opening_discrepancy,
        expected_closing_idr=statement_closing_idr,
        actual_closing_idr=actual_closing,
        closing_discrepancy_idr=closing_discrepancy,
        is_material=is_material,
        source_document_id=source_document_id,
        checked_at=_dt.datetime.now(_dt.timezone.utc),
    )

    existing = conn.execute(
        select(reconciliation_checks.c.id)
        .where(reconciliation_checks.c.account_id == account_id)
        .where(reconciliation_checks.c.period_month == period_month)
    ).first()

    if existing is not None:
        conn.execute(
            reconciliation_checks.update().where(reconciliation_checks.c.id == existing.id).values(**values)
        )
        check_id = existing.id
    else:
        result = conn.execute(
            reconciliation_checks.insert().values(account_id=account_id, period_month=period_month, **values)
        )
        check_id = result.inserted_primary_key[0]

    return ReconciliationResult(
        id=check_id,
        account_id=account_id,
        period_month=period_month,
        expected_opening_idr=statement_opening_idr,
        actual_opening_idr=actual_opening,
        opening_discrepancy_idr=opening_discrepancy,
        expected_closing_idr=statement_closing_idr,
        actual_closing_idr=actual_closing,
        closing_discrepancy_idr=closing_discrepancy,
        is_material=is_material,
    )
