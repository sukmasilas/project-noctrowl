"""``ledger.posting.post_employee_loan_disbursement`` /
``post_payroll_with_loan_repayment`` — the new EMPLOYEE_LOAN_RECEIVABLE
asset account (2026-09-10). See CLAUDE.md's Core accounting rules and the
real Fariz Pradana loan (Rp 27,000,000 disbursed 2026-08-17, repaid Rp
1,500,000/month for 18 months, no interest) that motivated this feature.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ledger.entities import get_account_id
from ledger.posting import post_employee_loan_disbursement, post_payroll_with_loan_repayment
from tests.helpers import assert_balanced, get_lines, get_source_type, lines_by_code


def test_disbursement_debits_receivable_credits_paying_account(prototype):
    conn, topo = prototype

    entry_id = post_employee_loan_disbursement(
        conn,
        entry_date=dt.date(2026, 8, 17),
        amount_idr=Decimal("27000000"),
        employee_ref="Fariz Pradana",
        memo="Employee loan disbursement",
    )
    assert_balanced(conn, entry_id)
    assert get_source_type(conn, entry_id) == "bank_other"

    lines = lines_by_code(conn, entry_id)
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].debit_amount_idr == Decimal("27000000")
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].consignor_item_ref == "Fariz Pradana"
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("27000000")
    assert lines["BCA_MAIN"][0].consignor_item_ref == "Fariz Pradana"


def test_disbursement_never_touches_pl_or_equity(prototype):
    """The loan is a pure asset movement — never Payroll, never a General
    Opex line, never Owner's Draw/Capital."""
    conn, topo = prototype

    entry_id = post_employee_loan_disbursement(
        conn,
        entry_date=dt.date(2026, 8, 17),
        amount_idr=Decimal("27000000"),
        employee_ref="Fariz Pradana",
    )
    lines = lines_by_code(conn, entry_id)
    assert set(lines.keys()) == {"EMPLOYEE_LOAN_RECEIVABLE", "BCA_MAIN"}


def test_disbursement_requires_employee_ref(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_employee_loan_disbursement(
            conn, entry_date=dt.date(2026, 8, 17), amount_idr=Decimal("27000000"), employee_ref=""
        )


def test_disbursement_rejects_non_positive_amount(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_employee_loan_disbursement(
            conn, entry_date=dt.date(2026, 8, 17), amount_idr=Decimal("0"), employee_ref="Fariz Pradana"
        )


def test_disbursement_respects_paying_wallet_group(prototype):
    conn, topo = prototype
    entry_id = post_employee_loan_disbursement(
        conn,
        entry_date=dt.date(2026, 8, 17),
        amount_idr=Decimal("27000000"),
        employee_ref="Fariz Pradana",
        paying_account_type_code="BCA_BRIDGING",
        paying_wallet_group_id=topo["wallet_group_id"],
    )
    lines = lines_by_code(conn, entry_id)
    # The credit landed on the wallet-group's own Bridging account, not
    # BCA_MAIN (the default) — confirms paying_account_type_code/
    # paying_wallet_group_id are actually threaded through.
    assert "BCA_MAIN" not in lines
    assert lines["BCA_BRIDGING"][0].credit_amount_idr == Decimal("27000000")


def test_payroll_with_loan_repayment_posts_three_lines(prototype):
    """The real Fariz Pradana case: a normal ~Rp10,000,000 salary reduced by
    a Rp1,500,000 installment shows up as an ~Rp8,500,000 net transfer.
    Gross Payroll expense must be the FULL amount, not the reduced net
    transfer, and EMPLOYEE_LOAN_RECEIVABLE must draw down by exactly the
    installment.
    """
    conn, topo = prototype

    entry_id = post_payroll_with_loan_repayment(
        conn,
        entry_date=dt.date(2026, 9, 25),
        net_transfer_idr=Decimal("8500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
        memo="Payroll — Fariz Pradana (incl. loan repayment)",
    )
    assert_balanced(conn, entry_id)
    lines = lines_by_code(conn, entry_id)

    assert lines["PAYROLL"][0].debit_amount_idr == Decimal("10000000")  # gross, not the reduced net transfer
    assert lines["PAYROLL"][0].consignor_item_ref == "Fariz Pradana"
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].credit_amount_idr == Decimal("1500000")
    assert lines["EMPLOYEE_LOAN_RECEIVABLE"][0].consignor_item_ref == "Fariz Pradana"
    assert lines["BCA_MAIN"][0].credit_amount_idr == Decimal("8500000")  # the actual amount transferred


def test_payroll_with_loan_repayment_rejects_non_positive_repayment(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_payroll_with_loan_repayment(
            conn,
            entry_date=dt.date(2026, 9, 25),
            net_transfer_idr=Decimal("10000000"),
            loan_repayment_idr=Decimal("0"),
            employee_ref="Fariz Pradana",
        )


def test_payroll_with_loan_repayment_requires_employee_ref(prototype):
    conn, topo = prototype
    with pytest.raises(ValueError):
        post_payroll_with_loan_repayment(
            conn,
            entry_date=dt.date(2026, 9, 25),
            net_transfer_idr=Decimal("8500000"),
            loan_repayment_idr=Decimal("1500000"),
            employee_ref="",
        )


def test_payroll_with_loan_repayment_threads_usd_reference_on_all_lines(prototype):
    """QA-found gap fix (2026-09-10): post_payroll_with_loan_repayment must
    accept and thread amount_usd_ref/fx_rate_used, same as every other
    posting function a Payoneer-wallet-sourced row can reach — see
    post_consignor_reimbursement's docstring for the exact bug class
    (Milestone 5) this closes. Tagged on ALL THREE lines, matching the
    "reference on the whole transaction" convention.
    """
    conn, topo = prototype

    entry_id = post_payroll_with_loan_repayment(
        conn,
        entry_date=dt.date(2026, 9, 25),
        net_transfer_idr=Decimal("8500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
        paying_account_type_code="PAYONEER_WALLET",
        paying_wallet_group_id=topo["wallet_group_id"],
        amount_usd_ref=Decimal("535.32"),
        fx_rate_used=Decimal("15879.90"),
    )
    assert_balanced(conn, entry_id)
    lines = get_lines(conn, entry_id)
    assert len(lines) == 3
    for line in lines:
        assert line.amount_usd_ref == Decimal("535.32")
        assert line.fx_rate_used == Decimal("15879.90")


def test_remaining_loan_balance_computed_from_disbursement_minus_repayments(prototype):
    """Simulates the Employee Loans screen's own live-computation logic
    (webapp.settings_bp._repayments_by_employee): original amount minus
    cumulative EMPLOYEE_LOAN_RECEIVABLE credits for that employee.
    """
    conn, topo = prototype

    post_employee_loan_disbursement(
        conn, entry_date=dt.date(2026, 8, 17), amount_idr=Decimal("27000000"), employee_ref="Fariz Pradana"
    )
    post_payroll_with_loan_repayment(
        conn,
        entry_date=dt.date(2026, 9, 25),
        net_transfer_idr=Decimal("8500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )
    post_payroll_with_loan_repayment(
        conn,
        entry_date=dt.date(2026, 10, 25),
        net_transfer_idr=Decimal("8500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )

    from sqlalchemy import select

    from ledger.schema import journal_lines

    receivable_id = get_account_id(conn, "EMPLOYEE_LOAN_RECEIVABLE")
    total_credits = conn.execute(
        select(journal_lines.c.credit_amount_idr).where(
            journal_lines.c.account_id == receivable_id, journal_lines.c.credit_amount_idr > 0
        )
    ).scalars().all()
    remaining = Decimal("27000000") - sum(total_credits, Decimal("0"))
    assert remaining == Decimal("24000000")  # 27,000,000 - 1,500,000 - 1,500,000
