"""Tests for the new Subsidiary Ledger screen (webapp/subsidiary_ledger_bp.py)
— breaks CONSIGNOR_PAYABLE / EMPLOYEE_LOAN_RECEIVABLE down by individual
consignor/employee. See that module's docstring for why only these two
control accounts are wired up (Payroll deliberately excluded for now).

The one property every test here ultimately cares about: sum of sub-entity
balances must tie out EXACTLY to the control account's own aggregate
balance — that's the entire point of a subsidiary ledger.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ledger.posting import (
    create_consignment_sale,
    post_consignment_sale,
    post_consignor_reimbursement,
    post_employee_loan_disbursement,
    post_payroll_with_loan_repayment,
)
from webapp.journal_entries_bp import list_all_accounts
from webapp.subsidiary_ledger_bp import (
    list_subsidiary_ledger_accounts,
    reconciliation,
    sub_entity_balances,
    sub_entity_rows,
)

DAY = _dt.date(2026, 7, 10)
PERIOD = _dt.date(2026, 7, 1)
RATE = Decimal("16300")


def test_list_subsidiary_ledger_accounts_returns_both_wired_accounts(wtopology):
    conn, topo = wtopology
    accounts = list_subsidiary_ledger_accounts(conn)
    codes = [a.account_type_code for a in accounts]
    assert codes == ["CONSIGNOR_PAYABLE", "EMPLOYEE_LOAN_RECEIVABLE"]


def test_consignor_payable_empty_state_reconciles_at_exactly_zero(wtopology):
    """No real CONSIGN- sales exist yet — both sides of the reconciliation
    must be exactly zero, not crash, and not silently disagree.
    """
    conn, topo = wtopology
    all_accounts = list_all_accounts(conn)
    consignor_payable = next(a for a in all_accounts if a.account_type_code == "CONSIGNOR_PAYABLE")

    balances = sub_entity_balances(conn, account_option=consignor_payable, period_month=PERIOD)
    assert balances == []

    recon = reconciliation(conn, account_option=consignor_payable, period_month=PERIOD, balances=balances)
    assert recon.control_account_balance_idr == Decimal("0")
    assert recon.sum_of_sub_entities_idr == Decimal("0")
    assert recon.matches is True
    assert recon.difference_idr == Decimal("0")


def test_consignor_payable_reconciles_with_real_activity(wtopology):
    conn, topo = wtopology
    sale_id = create_consignment_sale(
        conn,
        item_price_usd=Decimal("100"),
        payout_model="tier",
        payout_amount_idr=Decimal("1300000"),
        consignor_item_ref="CONSIGN-1",
        tier_rate_percent=Decimal("80.00"),
        confirmed=True,
    )
    post_consignment_sale(
        conn,
        consignment_sale_id=sale_id,
        ebay_account_id=topo["ebay_account_id"],
        entry_date=DAY,
        gross_sale_price_usd=Decimal("110"),
        ebay_fee_usd=Decimal("10"),
        kurs_pajak_rate=RATE,
        ebay_order_ref="CONSIGN-ORDER",
    )
    # Partial reimbursement — the consignor is still owed the remainder.
    post_consignor_reimbursement(
        conn, entry_date=DAY, amount_idr=Decimal("500000"), consignor_item_ref="CONSIGN-1"
    )
    conn.commit()

    all_accounts = list_all_accounts(conn)
    consignor_payable = next(a for a in all_accounts if a.account_type_code == "CONSIGNOR_PAYABLE")

    balances = sub_entity_balances(conn, account_option=consignor_payable, period_month=PERIOD)
    assert len(balances) == 1
    assert balances[0].reference == "CONSIGN-1"
    assert balances[0].balance_idr == Decimal("800000")  # 1,300,000 accrued - 500,000 paid

    recon = reconciliation(conn, account_option=consignor_payable, period_month=PERIOD, balances=balances)
    assert recon.control_account_balance_idr == Decimal("800000")
    assert recon.sum_of_sub_entities_idr == Decimal("800000")
    assert recon.matches is True


def test_consignor_payable_second_consignor_kept_separate_and_still_reconciles(wtopology):
    conn, topo = wtopology
    for ref, payout in [("CONSIGN-1", Decimal("1300000")), ("CONSIGN-2", Decimal("400000"))]:
        sale_id = create_consignment_sale(
            conn,
            item_price_usd=Decimal("100"),
            payout_model="tier",
            payout_amount_idr=payout,
            consignor_item_ref=ref,
            tier_rate_percent=Decimal("80.00"),
            confirmed=True,
        )
        post_consignment_sale(
            conn,
            consignment_sale_id=sale_id,
            ebay_account_id=topo["ebay_account_id"],
            entry_date=DAY,
            gross_sale_price_usd=Decimal("110"),
            ebay_fee_usd=Decimal("10"),
            kurs_pajak_rate=RATE,
            ebay_order_ref=f"{ref}-ORDER",
        )
    conn.commit()

    all_accounts = list_all_accounts(conn)
    consignor_payable = next(a for a in all_accounts if a.account_type_code == "CONSIGNOR_PAYABLE")

    balances = sub_entity_balances(conn, account_option=consignor_payable, period_month=PERIOD)
    by_ref = {b.reference: b.balance_idr for b in balances}
    assert by_ref == {"CONSIGN-1": Decimal("1300000"), "CONSIGN-2": Decimal("400000")}

    recon = reconciliation(conn, account_option=consignor_payable, period_month=PERIOD, balances=balances)
    assert recon.matches is True
    assert recon.control_account_balance_idr == Decimal("1700000")


def test_employee_loan_receivable_shows_real_fariz_style_balance_and_reconciles(wtopology):
    """Real-shaped scenario: Rp 27,000,000 disbursed, two Rp 1,500,000
    monthly installments repaid via payroll deduction so far.
    """
    conn, topo = wtopology
    post_employee_loan_disbursement(
        conn,
        entry_date=_dt.date(2026, 6, 1),
        amount_idr=Decimal("27000000"),
        employee_ref="Fariz Pradana",
    )
    post_payroll_with_loan_repayment(
        conn,
        entry_date=_dt.date(2026, 6, 25),
        net_transfer_idr=Decimal("3500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )
    post_payroll_with_loan_repayment(
        conn,
        entry_date=DAY,
        net_transfer_idr=Decimal("3500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )
    conn.commit()

    all_accounts = list_all_accounts(conn)
    receivable = next(a for a in all_accounts if a.account_type_code == "EMPLOYEE_LOAN_RECEIVABLE")

    balances = sub_entity_balances(conn, account_option=receivable, period_month=PERIOD)
    assert len(balances) == 1
    assert balances[0].reference == "Fariz Pradana"
    assert balances[0].balance_idr == Decimal("24000000")  # 27,000,000 - 1,500,000*2

    recon = reconciliation(conn, account_option=receivable, period_month=PERIOD, balances=balances)
    assert recon.control_account_balance_idr == Decimal("24000000")
    assert recon.sum_of_sub_entities_idr == Decimal("24000000")
    assert recon.matches is True


def test_sub_entity_rows_running_balance_matches_reported_sub_entity_balance(wtopology):
    conn, topo = wtopology
    post_employee_loan_disbursement(
        conn, entry_date=_dt.date(2026, 6, 1), amount_idr=Decimal("27000000"), employee_ref="Fariz Pradana"
    )
    post_payroll_with_loan_repayment(
        conn,
        entry_date=DAY,
        net_transfer_idr=Decimal("3500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )
    conn.commit()

    all_accounts = list_all_accounts(conn)
    receivable = next(a for a in all_accounts if a.account_type_code == "EMPLOYEE_LOAN_RECEIVABLE")

    rows = sub_entity_rows(conn, account_option=receivable, period_month=PERIOD, reference="Fariz Pradana")
    assert len(rows) == 2
    assert rows[0].debit_idr == Decimal("27000000")
    assert rows[-1].running_balance_idr == Decimal("25500000")

    balances = sub_entity_balances(conn, account_option=receivable, period_month=PERIOD)
    assert balances[0].balance_idr == rows[-1].running_balance_idr


def test_sub_entity_rows_empty_for_unknown_reference(wtopology):
    conn, topo = wtopology
    all_accounts = list_all_accounts(conn)
    receivable = next(a for a in all_accounts if a.account_type_code == "EMPLOYEE_LOAN_RECEIVABLE")
    rows = sub_entity_rows(conn, account_option=receivable, period_month=PERIOD, reference="Nobody")
    assert rows == []


# ---------------------------------------------------------------------------
# HTTP route tests
# ---------------------------------------------------------------------------


def test_subsidiary_ledger_route_renders_empty_state_for_consignor_payable(client, wtopology):
    resp = client.get("/subsidiary-ledger/?account=CONSIGNOR_PAYABLE&period=2026-07")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No sub-entity activity" in body
    assert "Reconciled" in body


def test_subsidiary_ledger_route_shows_real_employee_balance(client, wtopology):
    conn, topo = wtopology
    post_employee_loan_disbursement(
        conn, entry_date=_dt.date(2026, 6, 1), amount_idr=Decimal("27000000"), employee_ref="Fariz Pradana"
    )
    post_payroll_with_loan_repayment(
        conn,
        entry_date=DAY,
        net_transfer_idr=Decimal("3500000"),
        loan_repayment_idr=Decimal("1500000"),
        employee_ref="Fariz Pradana",
    )
    conn.commit()

    resp = client.get("/subsidiary-ledger/?account=EMPLOYEE_LOAN_RECEIVABLE&period=2026-07")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Fariz Pradana" in body
    assert "Reconciled" in body
    assert "Warning" not in body


def test_subsidiary_ledger_route_drilldown_shows_transaction_rows(client, wtopology):
    conn, topo = wtopology
    post_employee_loan_disbursement(
        conn, entry_date=_dt.date(2026, 6, 1), amount_idr=Decimal("27000000"), employee_ref="Fariz Pradana"
    )
    conn.commit()

    resp = client.get(
        "/subsidiary-ledger/?account=EMPLOYEE_LOAN_RECEIVABLE&period=2026-07&entity=Fariz+Pradana"
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Transactions" in body
    assert "27.000.000" in body
