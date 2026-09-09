"""Report Provisional/Final status computation.

New logic for milestone 4 — CLAUDE.md's "Report finalization status"
section names the two gating conditions but nothing before this milestone
computed or rendered them. Pure functions over a ``Connection``, no Flask
dependency, so they're independently unit-testable (see
tests/webapp/test_finalization.py) without spinning up the app.

A report (per CLAUDE.md, updated 2026-09-01 per Main-agent's resolution of
this design's open question #2) is Final only when ALL THREE hold:
  1. Every review_queue row for that scope/period is resolved (no
     ``match_status = 'needs_review'`` rows left).
  2. Every fixed-expectation source document for that scope/period has been
     ingested — and for the CONSOLIDATED scope specifically, this means
     every per-account/per-wallet-group document too, not just the
     consolidated Master Account statement's own expectation. Rationale
     (Main-agent, 2026-09-01): Consolidated P&L/Cash Flow depend on data
     (e.g. FX gain/loss, which crystallizes at the Payoneer withdrawal
     step) that only exists once every wallet-group's Payoneer export has
     actually been ingested — a Consolidated report can't honestly claim
     Final while an underlying per-account/wallet-group document is still
     missing, even if the Master statement itself arrived.
  3. (Added 2026-09, reconciliation-gap-detection feature) No MATERIAL
     reconciliation discrepancy exists for any account in scope — see
     ``ingestion.reconciliation``. This catches a class of gap the first
     two conditions structurally cannot see: every review-queue row can be
     labeled/posted and every expected document can be ingested, and the
     ledger can STILL disagree with what the real bank/wallet statement
     itself says (a wrong-but-fully-posted transaction, or a missing
     opening balance) — exactly the class of real gap found manually this
     week. This condition is pure detection surfaced here; it never
     corrects anything (see ingestion/reconciliation.py's module
     docstring).
"""
from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import or_ as _or, select
from sqlalchemy.engine import Connection

from ingestion.schema import review_queue, source_documents
from ledger.schema import account_types, accounts, ebay_accounts, reconciliation_checks
from webapp.report_extras import format_idr


def expected_by(period_month: _dt.date) -> _dt.date:
    """The H+7 deadline (see CLAUDE.md's Scheduling section) for a fixed-
    expectation document belonging to ``period_month``: 7 days after that
    period's last calendar day.
    """
    last_day = calendar.monthrange(period_month.year, period_month.month)[1]
    period_end = period_month.replace(day=last_day)
    return period_end + _dt.timedelta(days=7)


@dataclass
class MissingDocument:
    document_type: str
    period_month: _dt.date
    ebay_account_id: int | None = None
    wallet_group_id: int | None = None
    expected_by_date: _dt.date = field(init=False)

    def __post_init__(self) -> None:
        self.expected_by_date = expected_by(self.period_month)

    @property
    def is_overdue(self) -> bool:
        return _dt.date.today() > self.expected_by_date


@dataclass
class ReconciliationDiscrepancy:
    account_id: int
    account_label: str
    opening_discrepancy_idr: Decimal
    closing_discrepancy_idr: Decimal


@dataclass
class ReportStatus:
    is_final: bool
    needs_review_count: int
    missing_documents: list[MissingDocument]
    reconciliation_discrepancies: list[ReconciliationDiscrepancy] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.needs_review_count:
            plural = "item" if self.needs_review_count == 1 else "items"
            out.append(f"{self.needs_review_count} {plural} need review")
        for doc in self.missing_documents:
            label = _DOCUMENT_TYPE_LABELS.get(doc.document_type, doc.document_type)
            if doc.is_overdue:
                out.append(f"{label} not yet received (expected by {doc.expected_by_date.isoformat()})")
            else:
                out.append(f"{label} not yet received")
        for disc in self.reconciliation_discrepancies:
            # Whichever side (opening/closing) is actually material drives
            # the figure shown — usually closing (the more recently-checked
            # balance), but an opening-only discrepancy (e.g. a missing
            # opening-balance entry with an otherwise-correct period) must
            # still show its own real number, not a misleading Rp 0.
            amount = (
                disc.closing_discrepancy_idr
                if abs(disc.closing_discrepancy_idr) >= abs(disc.opening_discrepancy_idr)
                else disc.opening_discrepancy_idr
            )
            out.append(
                f"{disc.account_label} balance doesn't match the bank statement "
                f"(off by {format_idr(abs(amount))})"
            )
        return out


_DOCUMENT_TYPE_LABELS = {
    "ebay_sales_csv": "eBay Sales Export",
    "payoneer_csv": "Payoneer Export",
    "bank_statement_wallet_group": "Bank Statement",
    "bank_statement_master": "Master Bank Statement",
}


def review_queue_status(
    conn: Connection,
    *,
    period_month: _dt.date,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
) -> int:
    """Count of unresolved 'needs_review' rows in scope for this
    account/wallet-group + period. Consolidated (both None) counts every
    row for the period, regardless of scope.

    For a per-account scope, ``wallet_group_id`` should be that account's
    OWN wallet-group (resolved by the caller — see report_status): an
    unresolved Payoneer-sourced row (wallet-group scoped, shared for a
    paired account) blocks that account's report too, since everything
    from the Payoneer stage onward is the shared pool for that account —
    an account can't honestly show Final while its shared pool still has
    something unclassified in it. So the filter is an OR of
    "belongs to this exact account" and "belongs to this account's
    wallet-group", not an either/or choice between the two.
    """
    query = select(review_queue.c.id).where(
        review_queue.c.match_status == "needs_review",
    )
    # transaction_date -> period is a plain date-range filter (period_month
    # is always the 1st of the month).
    period_start = period_month
    period_end = _next_month(period_month)
    query = query.where(review_queue.c.transaction_date >= period_start).where(
        review_queue.c.transaction_date < period_end
    )
    if ebay_account_id is not None:
        conditions = [review_queue.c.ebay_account_id == ebay_account_id]
        if wallet_group_id is not None:
            conditions.append(review_queue.c.wallet_group_id == wallet_group_id)
        query = query.where(_or(*conditions))
    elif wallet_group_id is not None:
        query = query.where(review_queue.c.wallet_group_id == wallet_group_id)
    return len(conn.execute(query).all())


def _next_month(d: _dt.date) -> _dt.date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


def _expected_documents_for_scope(
    conn: Connection, *, period_month: _dt.date, ebay_account_id: int | None, wallet_group_id: int | None
) -> list[tuple[str, int | None, int | None]]:
    """Returns a list of (document_type, ebay_account_id, wallet_group_id)
    tuples this scope is expected to have ingested for this period.
    """
    if ebay_account_id is not None:
        row = conn.execute(
            select(ebay_accounts.c.wallet_group_id).where(ebay_accounts.c.id == ebay_account_id)
        ).first()
        wg_id = row.wallet_group_id if row is not None else wallet_group_id
        return [
            ("ebay_sales_csv", ebay_account_id, None),
            ("payoneer_csv", None, wg_id),
            ("bank_statement_wallet_group", None, wg_id),
        ]
    if wallet_group_id is not None:
        return [
            ("payoneer_csv", None, wallet_group_id),
            ("bank_statement_wallet_group", None, wallet_group_id),
        ]
    # Consolidated: the master statement, PLUS every active eBay account's
    # own expectations (see module docstring — Main-agent's 2026-09-01
    # resolution).
    expected: list[tuple[str, int | None, int | None]] = [("bank_statement_master", None, None)]
    accounts = conn.execute(
        select(ebay_accounts.c.id, ebay_accounts.c.wallet_group_id).where(ebay_accounts.c.is_active.is_(True))
    ).all()
    seen_wallet_groups: set[int] = set()
    for acc in accounts:
        expected.append(("ebay_sales_csv", acc.id, None))
        if acc.wallet_group_id not in seen_wallet_groups:
            seen_wallet_groups.add(acc.wallet_group_id)
            expected.append(("payoneer_csv", None, acc.wallet_group_id))
            expected.append(("bank_statement_wallet_group", None, acc.wallet_group_id))
    return expected


def missing_source_documents(
    conn: Connection,
    *,
    period_month: _dt.date,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
) -> list[MissingDocument]:
    expected = _expected_documents_for_scope(
        conn, period_month=period_month, ebay_account_id=ebay_account_id, wallet_group_id=wallet_group_id
    )
    missing: list[MissingDocument] = []
    for document_type, acc_id, wg_id in expected:
        query = select(source_documents.c.ingested_at).where(
            source_documents.c.document_type == document_type,
            source_documents.c.period_month == period_month,
        )
        query = query.where(
            source_documents.c.ebay_account_id == acc_id
            if acc_id is not None
            else source_documents.c.ebay_account_id.is_(None)
        )
        query = query.where(
            source_documents.c.wallet_group_id == wg_id
            if wg_id is not None
            else source_documents.c.wallet_group_id.is_(None)
        )
        row = conn.execute(query).first()
        if row is None or row.ingested_at is None:
            missing.append(
                MissingDocument(
                    document_type=document_type,
                    period_month=period_month,
                    ebay_account_id=acc_id,
                    wallet_group_id=wg_id,
                )
            )
    return missing


def material_reconciliation_discrepancies(
    conn: Connection,
    *,
    period_month: _dt.date,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
) -> list[ReconciliationDiscrepancy]:
    """Every MATERIAL ``reconciliation_checks`` row in scope for this
    account/wallet-group + period. Deliberately queries ``reconciliation_
    checks`` itself rather than enumerating "the accounts we expect to
    check" — a row only ever exists for an account/period a real bank
    statement was actually reconciled against (see
    ingestion.reconciliation), so this naturally covers whatever accounts
    have real balance data (BCA Main, Mandiri Bridging today) with zero
    hardcoded account list here, and automatically covers any future
    account/document-type that starts getting checked too.

    Scoping mirrors ``review_queue_status``: a per-account report is also
    gated by its own wallet-group's shared-pool accounts (e.g. its
    Bridging Account) — consistent with the fact that everything from the
    Payoneer stage onward is that account's shared pool. A consolidated
    -only account (BCA Main) never blocks a single account/wallet-group's
    own report. Consolidated (both None) sees every material row for the
    period, regardless of scope.
    """
    resolved_wallet_group_id = wallet_group_id
    if ebay_account_id is not None:
        row = conn.execute(
            select(ebay_accounts.c.wallet_group_id).where(ebay_accounts.c.id == ebay_account_id)
        ).first()
        if row is not None:
            resolved_wallet_group_id = row.wallet_group_id

    query = (
        select(
            reconciliation_checks.c.account_id,
            reconciliation_checks.c.opening_discrepancy_idr,
            reconciliation_checks.c.closing_discrepancy_idr,
            account_types.c.name.label("account_type_name"),
        )
        .select_from(
            reconciliation_checks.join(accounts, accounts.c.id == reconciliation_checks.c.account_id).join(
                account_types, account_types.c.id == accounts.c.account_type_id
            )
        )
        .where(reconciliation_checks.c.period_month == period_month)
        .where(reconciliation_checks.c.is_material.is_(True))
    )
    if ebay_account_id is not None or wallet_group_id is not None:
        query = query.where(accounts.c.wallet_group_id == resolved_wallet_group_id)

    rows = conn.execute(query).all()
    return [
        ReconciliationDiscrepancy(
            account_id=r.account_id,
            account_label=r.account_type_name,
            opening_discrepancy_idr=r.opening_discrepancy_idr,
            closing_discrepancy_idr=r.closing_discrepancy_idr,
        )
        for r in rows
    ]


def report_status(
    conn: Connection,
    *,
    period_month: _dt.date,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
) -> ReportStatus:
    resolved_wallet_group_id = wallet_group_id
    if ebay_account_id is not None:
        row = conn.execute(
            select(ebay_accounts.c.wallet_group_id).where(ebay_accounts.c.id == ebay_account_id)
        ).first()
        if row is not None:
            resolved_wallet_group_id = row.wallet_group_id

    needs_review = review_queue_status(
        conn,
        period_month=period_month,
        ebay_account_id=ebay_account_id,
        wallet_group_id=resolved_wallet_group_id,
    )
    missing = missing_source_documents(
        conn, period_month=period_month, ebay_account_id=ebay_account_id, wallet_group_id=wallet_group_id
    )
    discrepancies = material_reconciliation_discrepancies(
        conn, period_month=period_month, ebay_account_id=ebay_account_id, wallet_group_id=wallet_group_id
    )
    return ReportStatus(
        is_final=(needs_review == 0 and not missing and not discrepancies),
        needs_review_count=needs_review,
        missing_documents=missing,
        reconciliation_discrepancies=discrepancies,
    )
