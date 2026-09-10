"""Employee Loans screen route tests (added 2026-09-10) — see
webapp/settings_bp.py and CLAUDE.md's Core accounting rules. Mirrors
tests/webapp/test_settings_routes.py's pattern for the sibling Payout
Tiers screen.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from sqlalchemy import select

from ledger.posting import post_employee_loan_disbursement, post_payroll_with_loan_repayment
from ledger.schema import employee_loans


def test_employee_loans_page_renders_empty_state(logged_in_client, wtopology):
    resp = logged_in_client.get("/settings/employee-loans")
    assert resp.status_code == 200
    assert b"No employee loan records yet" in resp.data


def test_create_employee_loan_record(logged_in_client, wtopology):
    conn, topo = wtopology
    resp = logged_in_client.post(
        "/settings/employee-loans",
        data={
            "employee_name": "Fariz Pradana",
            "original_amount_idr": "27000000",
            "monthly_installment_idr": "1500000",
            "loan_start_date": "2026-09-01",
        },
    )
    assert resp.status_code in (301, 302)

    row = conn.execute(select(employee_loans).where(employee_loans.c.employee_name == "Fariz Pradana")).first()
    assert row is not None
    assert row.original_amount_idr == Decimal("27000000")
    assert row.monthly_installment_idr == Decimal("1500000")
    assert row.loan_start_date == _dt.date(2026, 9, 1)


def test_create_employee_loan_rejects_non_positive_amounts(logged_in_client, wtopology):
    resp = logged_in_client.post(
        "/settings/employee-loans",
        data={
            "employee_name": "Fariz Pradana",
            "original_amount_idr": "0",
            "monthly_installment_idr": "1500000",
            "loan_start_date": "2026-09-01",
        },
    )
    assert resp.status_code in (301, 302)
    resp2 = logged_in_client.get("/settings/employee-loans")
    assert b"No employee loan records yet" in resp2.data  # rejected, nothing created


def test_remaining_balance_computed_live_from_posted_repayments(logged_in_client, wtopology):
    """The core promise of this screen: remaining balance always reflects
    what's ACTUALLY been posted, never a manually-maintained running total.
    """
    conn, topo = wtopology

    logged_in_client.post(
        "/settings/employee-loans",
        data={
            "employee_name": "Fariz Pradana",
            "original_amount_idr": "27000000",
            "monthly_installment_idr": "1500000",
            "loan_start_date": "2026-09-01",
        },
    )

    resp = logged_in_client.get("/settings/employee-loans")
    assert b"27.000.000" in resp.data

    # No repayments posted yet — remaining balance equals the original amount.
    row = conn.execute(select(employee_loans)).first()
    assert row is not None

    # Post the real disbursement + one repayment directly via the ledger
    # (same functions the review-queue posting path calls).
    post_employee_loan_disbursement(
        conn, entry_date=_dt.date(2026, 8, 17), amount_idr=Decimal("27000000"), employee_ref="Fariz Pradana"
    )
    post_payroll_with_loan_repayment(
        conn,
        entry_date=_dt.date(2026, 9, 25),
        net_transfer_idr=Decimal("8500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )
    conn.commit()

    resp2 = logged_in_client.get("/settings/employee-loans")
    assert resp2.status_code == 200
    # Remaining = 27,000,000 - 1,500,000 = 25,500,000 — computed live, not stored.
    assert b"25.500.000" in resp2.data


def test_update_employee_loan_record(logged_in_client, wtopology):
    conn, topo = wtopology
    conn.execute(
        employee_loans.insert().values(
            employee_name="Fariz Pradana",
            original_amount_idr=Decimal("27000000"),
            monthly_installment_idr=Decimal("1500000"),
            loan_start_date=_dt.date(2026, 9, 1),
        )
    )
    conn.commit()
    row = conn.execute(select(employee_loans)).first()

    resp = logged_in_client.post(
        f"/settings/employee-loans/{row.id}",
        data={
            "employee_name": "Fariz Pradana",
            "original_amount_idr": "27000000",
            "monthly_installment_idr": "1600000",
            "loan_start_date": "2026-09-01",
        },
    )
    assert resp.status_code in (301, 302)

    updated = conn.execute(select(employee_loans).where(employee_loans.c.id == row.id)).first()
    assert updated.monthly_installment_idr == Decimal("1600000")
