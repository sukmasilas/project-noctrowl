"""Tests for scheduling.fx_revaluation — the month-end unrealized FX
revaluation job. Covers: the flagged USD-balance-computation interpretation
(excluding prior fx_revaluation entries), the job's period-selection
(always the CLOSED period, per scheduling.window.closed_period_for),
missing-Kurs-Pajak-rate handling (skip, never guess), one wallet-group's
failure not blocking another's, and idempotency — BOTH the fast-path
convenience check AND the real DB unique-index backstop (tested by trying
to bypass the fast path, matching this project's established QA practice
for this exact class of bug — see ingestion/sync.py's own concurrency-fix
comment).
"""
from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.matching import RawLine, post_pending_rows, run_auto_match, stage_raw_lines
from ledger.posting import (
    post_consignor_reimbursement,
    post_inter_account_transfer,
    post_operating_expense,
    post_unrealized_fx_revaluation,
)
from ledger.schema import fx_revaluations
from scheduling.fx_revaluation import (
    MissingUsdReferenceError,
    _revalue_wallet_group,
    compute_payoneer_wallet_balance,
    run_fx_revaluation,
)
from tests.ingestion.conftest import make_source_document

AUG = dt.date(2026, 8, 1)
AUG_END = dt.date(2026, 8, 31)


def _transfer_to_payoneer(conn, topo, *, entry_date, amount_usd, rate_idr):
    amount_idr = (amount_usd * rate_idr).quantize(Decimal("1"))
    post_inter_account_transfer(
        conn,
        entry_date=entry_date,
        from_account_type_code="EBAY_WALLET",
        to_account_type_code="PAYONEER_WALLET",
        amount_idr=amount_idr,
        from_ebay_account_id=topo["ebay_account_id"],
        to_wallet_group_id=topo["wallet_group_id"],
        amount_usd_ref=amount_usd,
        fx_rate_used=rate_idr,
    )
    return amount_idr


# ---------------------------------------------------------------------------
# compute_payoneer_wallet_balance
# ---------------------------------------------------------------------------


def test_compute_balance_basic_transfer(stopology):
    conn, topo = stopology
    idr = _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    usd_balance, book_value = compute_payoneer_wallet_balance(
        conn, wallet_group_id=topo["wallet_group_id"], as_of_date=AUG_END
    )
    assert usd_balance == Decimal("1000.00")
    assert book_value == idr


def test_compute_balance_zero_when_no_activity_yet(stopology):
    conn, topo = stopology
    usd_balance, book_value = compute_payoneer_wallet_balance(
        conn, wallet_group_id=topo["wallet_group_id"], as_of_date=AUG_END
    )
    assert usd_balance == Decimal("0")
    assert book_value == Decimal("0")


def test_compute_balance_excludes_a_prior_fx_revaluation_entry(stopology):
    """FLAGGED INTERPRETATION regression test (see scheduling/fx_revaluation.py's
    module docstring): a prior month's revaluation entry must NOT get
    double-counted into a later month's USD balance, even though its own
    journal line stores amount_usd_ref = the WHOLE balance (not a delta).
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    # August: a real $1000 transfer lands in Payoneer.
    aug_idr = _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    # August month-end revaluation: books a gain (rate moved from 16300 to
    # 16420), diff = 1000*16420 - aug_idr.
    entry_id = post_unrealized_fx_revaluation(
        conn,
        wallet_group_id=wg,
        period_month=AUG,
        usd_balance=Decimal("1000.00"),
        current_book_value_idr=aug_idr,
        kemenkeu_eom_rate_idr=Decimal("16420"),
    )
    assert entry_id is not None
    conn.commit()

    # September: a second real transfer of $500 lands.
    sep_idr = _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 9, 10), amount_usd=Decimal("500.00"), rate_idr=Decimal("16450"))
    conn.commit()

    sep_end = dt.date(2026, 9, 30)
    usd_balance, book_value = compute_payoneer_wallet_balance(conn, wallet_group_id=wg, as_of_date=sep_end)

    # The REAL USD balance is exactly 1000 + 500 = 1500 — the revaluation
    # entry moved IDR book value only, never real USD. A naive (non
    # -excluding) implementation would double the August $1000 by also
    # summing the revaluation line's amount_usd_ref, landing on 2500.
    assert usd_balance == Decimal("1500.00")

    # IDR book value correctly INCLUDES the revaluation's IDR effect.
    aug_diff = (Decimal("1000.00") * Decimal("16420")).quantize(Decimal("1")) - aug_idr
    assert book_value == aug_idr + aug_diff + sep_idr


def test_compute_balance_reflects_a_payoneer_paid_operating_expense(stopology):
    """QA-reported bug regression test — exact scenario QA reproduced:
    a real $1000 USD inflow to the wallet-group's Payoneer Wallet, then a
    real ~$20-equivalent operating expense paid FROM that same wallet.
    Before the fix (threading amount_usd_ref through post_operating_expense
    from ingestion/matching.py), the expense posted with a NULL USD
    reference, so compute_payoneer_wallet_balance kept reporting
    usd_balance=1000.00 (a $20 phantom gap) even though book_value_idr
    correctly dropped by the payment. Now both sides move together.
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    aug_idr = _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    expense_usd = Decimal("20.00")
    expense_idr = (expense_usd * Decimal("16300")).quantize(Decimal("1"))
    rate = expense_idr / expense_usd
    post_operating_expense(
        conn,
        entry_date=dt.date(2026, 8, 15),
        expense_account_type_code="GENERAL_OPEX",
        amount_idr=expense_idr,
        paying_account_type_code="PAYONEER_WALLET",
        paying_wallet_group_id=wg,
        amount_usd_ref=expense_usd,
        fx_rate_used=rate,
    )
    conn.commit()

    usd_balance, book_value = compute_payoneer_wallet_balance(conn, wallet_group_id=wg, as_of_date=AUG_END)
    assert usd_balance == Decimal("980.00")  # 1000 - 20, NOT 1000 (the pre-fix phantom-gap bug)
    assert book_value == aug_idr - expense_idr


def test_full_pipeline_openai_subscription_yields_correct_balance_not_just_correct_journal_lines(stopology, sengine):
    """QA BUG FIX #2 (2026-09) regression test — chains actual INGESTION
    (stage_raw_lines -> run_auto_match -> post_pending_rows), not just a
    direct, hand-constructed posting.post_operating_expense call, into
    compute_payoneer_wallet_balance. This is the exact gap QA found: the
    first fix's own tests (test_ingestion/test_matching.py) only checked
    that journal_lines.amount_usd_ref matched the staged (SIGNED) input —
    never that the resulting balance was still correct — which is exactly
    where a sign bug hid. ingestion/payoneer.py stages a real outflow like
    the recurring "OPENAI *CHATGPT SUBSCR" Payoneer card charge with a
    NEGATIVE amount_usd_ref (its own same-sign convention, see
    RawLine below); this proves that negative reference value still nets
    out correctly once it reaches compute_payoneer_wallet_balance, not
    just that it round-trips into the journal_lines row unchanged.

    Real numbers, matching QA's own repro #2 exactly: $1000.00 inflow,
    then a -334130.00 IDR / -20.37 USD outflow -> expected USD balance is
    1000.00 - 20.37 = 979.63.
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    aug_idr = _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    src_id = make_source_document(
        conn, document_type="payoneer_csv", period_month=AUG, wallet_group_id=wg
    )
    stage_raw_lines(
        conn,
        source_type="payoneer_csv",
        source_document_id=src_id,
        wallet_group_id=wg,
        lines=[
            RawLine(
                transaction_date=dt.date(2026, 8, 28),
                raw_description="Card charge (OPENAI *CHATGPT SUBSCR)",
                amount_idr=Decimal("-334130.00"),
                amount_usd_ref=Decimal("-20.37"),
                external_ref="287852392",
            )
        ],
    )
    run_auto_match(conn)
    post_result = post_pending_rows(conn)
    assert post_result.posted == 1
    conn.commit()

    usd_balance, book_value = compute_payoneer_wallet_balance(conn, wallet_group_id=wg, as_of_date=AUG_END)
    assert usd_balance == Decimal("979.63")  # NOT 1020.37 — the sign-flip bug QA found
    assert book_value == aug_idr - Decimal("334130.00")


def test_compute_balance_reflects_a_payoneer_paid_consignment_reimbursement(stopology):
    """Same class of fix, exercised via post_consignor_reimbursement (the
    other function QA flagged) instead of post_operating_expense.
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    aug_idr = _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("500.00"), rate_idr=Decimal("16300"))
    conn.commit()

    payout_usd = Decimal("150.00")
    payout_idr = (payout_usd * Decimal("16300")).quantize(Decimal("1"))
    rate = payout_idr / payout_usd
    post_consignor_reimbursement(
        conn,
        entry_date=dt.date(2026, 8, 20),
        amount_idr=payout_idr,
        consignor_item_ref="CONSIGN-Z:order-9",
        paying_account_type_code="PAYONEER_WALLET",
        paying_wallet_group_id=wg,
        amount_usd_ref=payout_usd,
        fx_rate_used=rate,
    )
    conn.commit()

    usd_balance, book_value = compute_payoneer_wallet_balance(conn, wallet_group_id=wg, as_of_date=AUG_END)
    assert usd_balance == Decimal("350.00")  # 500 - 150
    assert book_value == aug_idr - payout_idr


def test_compute_balance_raises_defensively_on_a_missing_usd_reference(stopology):
    """QA's strongly-recommended defensive backstop: a real (nonzero-IDR),
    non-fx_revaluation Payoneer Wallet line with NO amount_usd_ref must
    make compute_payoneer_wallet_balance refuse to compute a number at
    all, rather than silently treating it as a $0 USD movement (which is
    exactly how the original bug corrupted the balance).
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    # Simulate the pre-fix bug directly: an expense posted with NO USD
    # reference at all (paying_account_type_code=PAYONEER_WALLET, but
    # amount_usd_ref left at its default None).
    post_operating_expense(
        conn,
        entry_date=dt.date(2026, 8, 15),
        expense_account_type_code="GENERAL_OPEX",
        amount_idr=Decimal("326000"),
        paying_account_type_code="PAYONEER_WALLET",
        paying_wallet_group_id=wg,
    )
    conn.commit()

    with pytest.raises(MissingUsdReferenceError, match="amount_usd_ref"):
        compute_payoneer_wallet_balance(conn, wallet_group_id=wg, as_of_date=AUG_END)


def test_compute_balance_ignores_a_missing_reference_on_a_zero_amount_or_later_line(stopology):
    """The defensive check must not false-positive on lines that are
    genuinely irrelevant: a zero-amount line, or a real line dated AFTER
    as_of_date (out of scope for this particular balance computation).
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    # A real expense with no USD ref, but dated in SEPTEMBER — out of scope
    # for an August 31 as_of_date, so it must NOT trigger the defensive
    # check when revaluing August.
    post_operating_expense(
        conn,
        entry_date=dt.date(2026, 9, 5),
        expense_account_type_code="GENERAL_OPEX",
        amount_idr=Decimal("50000"),
        paying_account_type_code="PAYONEER_WALLET",
        paying_wallet_group_id=wg,
    )
    conn.commit()

    usd_balance, book_value = compute_payoneer_wallet_balance(conn, wallet_group_id=wg, as_of_date=AUG_END)
    assert usd_balance == Decimal("1000.00")


def test_run_fx_revaluation_skips_gracefully_on_missing_usd_reference(stopology, sengine):
    """The job-level orchestration must catch MissingUsdReferenceError as a
    clean, logged skip for that wallet-group — never an unhandled crash
    that takes the whole run down.
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    post_operating_expense(
        conn,
        entry_date=dt.date(2026, 8, 15),
        expense_account_type_code="GENERAL_OPEX",
        amount_idr=Decimal("326000"),
        paying_account_type_code="PAYONEER_WALLET",
        paying_wallet_group_id=wg,
    )
    conn.commit()

    result = run_fx_revaluation(sengine, today=dt.date(2026, 9, 2))
    assert result.outcomes[0].status == "missing_usd_reference"
    assert result.outcomes[0].journal_entry_id is None

    with sengine.connect() as check_conn:
        count = check_conn.execute(select(func.count()).select_from(fx_revaluations)).scalar()
    assert count == 0  # never posted a number it couldn't trust


# ---------------------------------------------------------------------------
# run_fx_revaluation — period selection, missing rate, missing wallet
# ---------------------------------------------------------------------------


def test_run_fx_revaluation_uses_the_closed_period_not_todays_month(stopology, sengine):
    conn, topo = stopology
    _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    # Run "today" a few days into September's H+7 tail — must still revalue
    # AUGUST (the period that actually closed), using August's own EOM rate.
    result = run_fx_revaluation(sengine, today=dt.date(2026, 9, 4))
    assert result.period_month == AUG
    assert result.period_end == AUG_END
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status == "posted"
    assert outcome.wallet_group_id == topo["wallet_group_id"]

    # Confirm the posted entry actually used August's EOM rate (16420, per
    # the stopology fixture), not September's (16450).
    with sengine.connect() as check_conn:
        row = check_conn.execute(
            select(fx_revaluations.c.kemenkeu_eom_rate_idr).where(
                fx_revaluations.c.wallet_group_id == topo["wallet_group_id"], fx_revaluations.c.period_month == AUG
            )
        ).first()
    assert row.kemenkeu_eom_rate_idr == Decimal("16420.0000")


def test_run_fx_revaluation_no_change_posts_nothing_when_diff_is_zero(stopology, sengine):
    conn, topo = stopology
    # Transfer booked at EXACTLY August's EOM rate -> zero revaluation diff.
    _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16420"))
    conn.commit()

    result = run_fx_revaluation(sengine, today=dt.date(2026, 9, 2))
    assert result.outcomes[0].status == "no_change"
    assert result.outcomes[0].journal_entry_id is None


def test_run_fx_revaluation_missing_kurs_pajak_rate_skips_without_guessing(sengine, sconn):
    from ledger.seed import seed_prototype_topology

    topo = seed_prototype_topology(sconn)
    sconn.commit()
    _transfer_to_payoneer(sconn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    sconn.commit()

    # Deliberately no Kurs Pajak rate seeded at all for this fixture.
    result = run_fx_revaluation(sengine, today=dt.date(2026, 9, 2))
    assert result.outcomes[0].status == "missing_kurs_pajak_rate"
    assert result.outcomes[0].journal_entry_id is None

    with sengine.connect() as check_conn:
        count = check_conn.execute(select(func.count()).select_from(fx_revaluations)).scalar()
    assert count == 0


def test_run_fx_revaluation_missing_payoneer_wallet_skips_gracefully(sconn, sengine):
    from ledger.entities import create_ebay_account, create_wallet_group

    wg_id = create_wallet_group(sconn, name="Wallet Group With No Payoneer Account Yet")
    create_ebay_account(sconn, name="Half-Set-Up Account", wallet_group_id=wg_id)
    # A Kurs Pajak rate IS seeded here — this test is specifically about the
    # missing-PAYONEER_WALLET-account path, which is only reached AFTER the
    # rate lookup succeeds (see _revalue_wallet_group's ordering). Without
    # this, the rate lookup would fail first and mask what this test means
    # to exercise.
    seed_kurs_pajak_rate(sconn, effective_date=AUG_END, rate_idr=Decimal("16420.0000"))
    sconn.commit()

    result = run_fx_revaluation(sengine, today=dt.date(2026, 9, 2))
    assert result.outcomes[0].status == "missing_payoneer_wallet"


def test_run_fx_revaluation_one_wallet_groups_missing_wallet_does_not_block_another(sfull_topology, sengine):
    """Kurs Pajak is a single global reference rate (Kemenkeu publishes one
    figure for all of Indonesia, not per-business) — it can never be
    "missing for wallet-group A but present for wallet-group B" the way a
    wallet-group-scoped fact could be. So the realistic version of "one
    wallet-group's problem doesn't block another's" is a wallet-group
    -specific setup gap (no Payoneer Wallet account yet), not a missing
    rate — exercised here across TWO real wallet-groups from
    seed_full_topology (never hardcoding "there's only ever one
    wallet-group", per CLAUDE.md).
    """
    conn, topo = sfull_topology
    shared_wg = topo["wallet_groups"]["shared"]
    independent_wg = topo["wallet_groups"]["independent"]

    seed_kurs_pajak_rate(conn, effective_date=AUG_END, rate_idr=Decimal("16420.0000"))
    conn.commit()

    # Only the INDEPENDENT wallet-group gets real Payoneer activity —
    # nothing posted for the shared one, so its Payoneer Wallet balance is
    # zero (diff computed against a zero/zero baseline, still a valid
    # 'no_change' outcome, not a crash) while the independent one posts a
    # real revaluation.
    ebay_3 = topo["ebay_accounts"]["3"]  # belongs to the independent wallet-group
    post_inter_account_transfer(
        conn, entry_date=dt.date(2026, 8, 5), from_account_type_code="EBAY_WALLET", to_account_type_code="PAYONEER_WALLET",
        amount_idr=Decimal("8150000"), from_ebay_account_id=ebay_3, to_wallet_group_id=independent_wg,
        amount_usd_ref=Decimal("500.00"), fx_rate_used=Decimal("16300"),
    )
    conn.commit()

    result = run_fx_revaluation(sengine, today=dt.date(2026, 9, 2))
    by_wg = {o.wallet_group_id: o.status for o in result.outcomes}
    assert by_wg[shared_wg] == "no_change"
    assert by_wg[independent_wg] == "posted"


# ---------------------------------------------------------------------------
# Idempotency: fast-path convenience check, and the real DB backstop.
# ---------------------------------------------------------------------------


def test_run_fx_revaluation_second_call_is_a_no_op_fast_path(stopology, sengine):
    conn, topo = stopology
    _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    first = run_fx_revaluation(sengine, today=dt.date(2026, 9, 2))
    assert first.outcomes[0].status == "posted"

    second = run_fx_revaluation(sengine, today=dt.date(2026, 9, 3))
    assert second.outcomes[0].status == "already_posted"

    with sengine.connect() as check_conn:
        count = check_conn.execute(
            select(func.count()).select_from(fx_revaluations).where(
                fx_revaluations.c.wallet_group_id == topo["wallet_group_id"], fx_revaluations.c.period_month == AUG
            )
        ).scalar()
    assert count == 1


def test_fx_revaluations_unique_index_is_the_real_backstop_not_just_app_check(stopology):
    """Proves the DB-level constraint itself blocks a duplicate, bypassing
    this module's own fast-path SELECT entirely — the scenario a truly
    concurrent race would hit (two overlapping runs that both pass the
    fast-path check before either commits).
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]

    first_id = post_unrealized_fx_revaluation(
        conn, wallet_group_id=wg, period_month=AUG, usd_balance=Decimal("1000.00"),
        current_book_value_idr=Decimal("16300000"), kemenkeu_eom_rate_idr=Decimal("16420"),
    )
    assert first_id is not None
    conn.commit()

    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            post_unrealized_fx_revaluation(
                conn, wallet_group_id=wg, period_month=AUG, usd_balance=Decimal("1000.00"),
                current_book_value_idr=Decimal("16300000"), kemenkeu_eom_rate_idr=Decimal("16500"),  # different -> still nonzero diff
            )
    conn.rollback()

    count = conn.execute(
        select(func.count()).select_from(fx_revaluations).where(
            fx_revaluations.c.wallet_group_id == wg, fx_revaluations.c.period_month == AUG
        )
    ).scalar()
    assert count == 1


def test_run_fx_revaluation_concurrent_runs_never_double_post(stopology, sengine):
    """Real two-connection concurrency test, same style already established
    in this project for this exact class of bug (see ingestion/sync.py's
    concurrency-fix comment: "a two-thread test... forced to maximal
    overlap with a threading.Barrier"). Both threads race to revalue the
    SAME wallet-group/period; exactly one must win, and the DB unique index
    must be what decides it, not app-layer luck.
    """
    conn, topo = stopology
    wg = topo["wallet_group_id"]
    _transfer_to_payoneer(conn, topo, entry_date=dt.date(2026, 8, 5), amount_usd=Decimal("1000.00"), rate_idr=Decimal("16300"))
    conn.commit()

    barrier = threading.Barrier(2)
    results: list = []
    lock = threading.Lock()

    def worker():
        with sengine.connect() as thread_conn:
            barrier.wait()
            outcome = _revalue_wallet_group(
                thread_conn, wallet_group_id=wg, period_month=AUG, period_end=AUG_END
            )
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(o.status for o in results)
    assert statuses in (["already_posted", "posted"], ["posted", "race_lost"]), statuses

    with sengine.connect() as check_conn:
        count = check_conn.execute(
            select(func.count()).select_from(fx_revaluations).where(
                fx_revaluations.c.wallet_group_id == wg, fx_revaluations.c.period_month == AUG
            )
        ).scalar()
    assert count == 1
