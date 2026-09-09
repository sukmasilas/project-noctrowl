"""Tests for ingestion.reconciliation — the reconciliation-gap-detection
feature's core computation: compare the ledger's own computed balance
against a bank statement's own printed opening/closing balance, flag a
material discrepancy, and persist an idempotent, upsertable result.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from ingestion.reconciliation import (
    MATERIALITY_THRESHOLD_IDR,
    UnknownReconciliationAccountError,
    check_account_reconciliation,
)
from ledger import posting
from ledger.entities import get_account_id
from ledger.schema import reconciliation_checks


def _post_opening(conn, *, account_type_code, wallet_group_id=None, amount_idr):
    return posting.post_opening_balance(
        conn,
        account_type_code=account_type_code,
        entry_date=_dt.date(2026, 6, 30),
        amount_idr=amount_idr,
        wallet_group_id=wallet_group_id,
    )


def test_materiality_threshold_is_one_rupiah():
    assert MATERIALITY_THRESHOLD_IDR == Decimal("1")


def test_clean_match_is_not_material(iprototype):
    conn, topo = iprototype
    account_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    _post_opening(conn, account_type_code="BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"], amount_idr=Decimal("1000000"))

    outcome = check_account_reconciliation(
        conn,
        account_id=account_id,
        period_month=_dt.date(2026, 7, 1),
        statement_opening_idr=Decimal("1000000"),
        statement_closing_idr=Decimal("1000000"),
    )
    assert outcome.opening_discrepancy_idr == Decimal("0")
    assert outcome.closing_discrepancy_idr == Decimal("0")
    assert outcome.is_material is False
    assert outcome.matches is True


def test_sub_rupiah_residual_is_immaterial_rounding_noise(iprototype):
    """The exact real-world case this threshold exists for: the ledger only
    ever posts whole-Rupiah amounts (ledger.posting.round_idr), but a real
    statement's own printed figure carries cents — a residual smaller than
    Rp 1 can only be that kind of quantization noise, never a real missing
    transaction, and must not be flagged.
    """
    conn, topo = iprototype
    account_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    _post_opening(conn, account_type_code="BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"], amount_idr=Decimal("1000000"))

    outcome = check_account_reconciliation(
        conn,
        account_id=account_id,
        period_month=_dt.date(2026, 7, 1),
        statement_opening_idr=Decimal("1000000.43"),
        statement_closing_idr=Decimal("1000000.43"),
    )
    assert outcome.opening_discrepancy_idr == Decimal("-0.43")
    assert outcome.is_material is False


def test_exactly_one_rupiah_discrepancy_is_material(iprototype):
    conn, topo = iprototype
    account_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    _post_opening(conn, account_type_code="BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"], amount_idr=Decimal("1000000"))

    outcome = check_account_reconciliation(
        conn,
        account_id=account_id,
        period_month=_dt.date(2026, 7, 1),
        statement_opening_idr=Decimal("999999"),
        statement_closing_idr=Decimal("1000000"),
    )
    assert outcome.opening_discrepancy_idr == Decimal("1")
    assert outcome.is_material is True


def test_missing_ledger_activity_produces_a_material_discrepancy(iprototype):
    """A real gap this feature exists to catch: nothing has been posted to
    this account yet (e.g. a missing opening balance), so the ledger's
    computed balance is 0 while the real statement shows a large nonzero
    balance — a large, obviously material discrepancy.
    """
    conn, topo = iprototype
    account_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])

    outcome = check_account_reconciliation(
        conn,
        account_id=account_id,
        period_month=_dt.date(2026, 7, 1),
        statement_opening_idr=Decimal("62470973.37"),
        statement_closing_idr=Decimal("75620613.34"),
    )
    assert outcome.actual_opening_idr == Decimal("0")
    assert outcome.opening_discrepancy_idr == Decimal("-62470973.37")
    assert outcome.is_material is True


def test_rerun_for_same_account_period_updates_in_place_not_duplicates(iprototype):
    conn, topo = iprototype
    account_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    period = _dt.date(2026, 7, 1)

    # Nothing posted yet — first run correctly finds a material gap.
    first = check_account_reconciliation(
        conn,
        account_id=account_id,
        period_month=period,
        statement_opening_idr=Decimal("1000000"),
        statement_closing_idr=Decimal("1000000"),
    )
    assert first.is_material is True

    # Ledger activity changes between the two runs (e.g. a late-arriving
    # opening balance post) — a re-run must UPDATE the existing row with
    # the new figures, never insert a second row for the same scope.
    _post_opening(conn, account_type_code="BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"], amount_idr=Decimal("1000000"))
    second = check_account_reconciliation(
        conn,
        account_id=account_id,
        period_month=period,
        statement_opening_idr=Decimal("1000000"),
        statement_closing_idr=Decimal("1000000"),
    )

    assert second.id == first.id
    assert second.is_material is False

    rows = conn.execute(
        select(reconciliation_checks.c.id).where(
            reconciliation_checks.c.account_id == account_id, reconciliation_checks.c.period_month == period
        )
    ).all()
    assert len(rows) == 1


def test_opening_balance_entry_dated_in_the_period_counts_toward_that_periods_opening(iprototype):
    """Regression test for a real bug found during this feature's own
    backfill against the live database: ``ledger.posting.
    post_opening_balance`` books its one-time entry with ``entry_date`` =
    the FIRST DAY of the very first tracked period (there is no earlier
    date to use) — e.g. 2026-05-01 for May. A naive "balance through the
    end of the PRIOR month only" definition of "opening balance" would
    therefore exclude that entry entirely (its period_month IS May, not
    April), making a freshly-onboarded account look like it has a large,
    spurious opening-balance discrepancy against the real statement's own
    printed opening balance — exactly what happened on the first real
    backfill run before ``ledger.balances.account_balance_before`` was
    fixed to special-case ``source_type = 'opening_balance'`` entries in.
    """
    conn, topo = iprototype
    account_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=topo["wallet_group_id"])
    posting.post_opening_balance(
        conn,
        account_type_code="BCA_BRIDGING",
        wallet_group_id=topo["wallet_group_id"],
        entry_date=_dt.date(2026, 5, 1),  # dated IN May, the period it represents the opening of
        amount_idr=Decimal("66004188"),
    )

    outcome = check_account_reconciliation(
        conn,
        account_id=account_id,
        period_month=_dt.date(2026, 5, 1),
        statement_opening_idr=Decimal("66004187.57"),
        statement_closing_idr=Decimal("66004188"),
    )
    # Off by the sub-Rupiah rounding residual only (66004188 - 66004187.57 =
    # 0.43) — immaterial, NOT the ~Rp 66 million false discrepancy a naive
    # "prior month only" definition would have produced.
    assert outcome.opening_discrepancy_idr == Decimal("0.43")
    assert outcome.is_material is False


def test_fractional_interest_income_posting_does_not_break_reconciliation(iprototype):
    """QA finding (2026-09): unlike most posting functions, ``ledger.
    posting.post_interest_income_line`` posts ``amount_idr`` unrounded —
    real BCA statements do contain fractional-Rupiah BUNGA lines (e.g. Rp
    1,319.32), so this IS a real posting path that puts sub-Rupiah cents
    into the ledger, contrary to what an earlier version of this module's
    docstring claimed. This test proves the Rp 1 threshold still holds with
    a real fractional posting actually present, not just in its absence:

    1. A fractional interest posting that matches the statement's own
       stated closing balance EXACTLY (as it always will in practice — the
       cents came from that same statement) reconciles to a precise zero
       discrepancy, not merely "under threshold".
    2. That same fractional posting does not mask or interfere with
       detecting a genuinely missing WHOLE-Rupiah transaction alongside it
       — the discrepancy is still flagged material, at the exact
       whole-Rupiah amount, with the fractional cents cancelling out of the
       comparison entirely.
    """
    conn, topo = iprototype
    bca_main_id = topo["BCA_MAIN"]

    # A real fractional-Rupiah BUNGA line, posted via the unrounded
    # post_interest_income_line path.
    posting.post_interest_income_line(
        conn, entry_date=_dt.date(2026, 7, 15), amount_idr=Decimal("1319.32")
    )

    # Case 1: the statement's own stated closing balance embeds that exact
    # same fractional value (as it always does — the ledger's cents came
    # FROM this statement) -> reconciles to exactly zero.
    outcome = check_account_reconciliation(
        conn,
        account_id=bca_main_id,
        period_month=_dt.date(2026, 7, 1),
        statement_opening_idr=Decimal("0"),
        statement_closing_idr=Decimal("1319.32"),
    )
    assert outcome.closing_discrepancy_idr == Decimal("0")
    assert outcome.is_material is False

    # Case 2: a genuine whole-Rupiah gap (e.g. a missing transaction) is
    # layered on top of the same fractional posting — still correctly
    # flagged material, at exactly the whole-Rupiah amount; the fractional
    # cents contribute nothing to the discrepancy either way.
    outcome_with_gap = check_account_reconciliation(
        conn,
        account_id=bca_main_id,
        period_month=_dt.date(2026, 7, 1),
        statement_opening_idr=Decimal("0"),
        statement_closing_idr=Decimal("501319.32"),  # statement shows Rp 500,000 more than the ledger has
    )
    assert outcome_with_gap.closing_discrepancy_idr == Decimal("-500000")
    assert outcome_with_gap.is_material is True


def test_unknown_account_id_raises(iconn):
    with pytest.raises(UnknownReconciliationAccountError):
        check_account_reconciliation(
            iconn,
            account_id=999999999,
            period_month=_dt.date(2026, 7, 1),
            statement_opening_idr=Decimal("0"),
            statement_closing_idr=Decimal("0"),
        )
