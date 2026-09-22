"""Tests for the new General Ledger screen (webapp/general_ledger_bp.py) —
the per-account ledger view added 2026-09-16 alongside the Journal Entries
rename. See that module's docstring for the Journal-vs-Ledger distinction
this is built on: one row per journal LINE touching the selected account,
no "all accounts" mode, no other-side-of-the-entry detail shown inline.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ledger.posting import (
    post_cogs_purchase,
    post_income_line,
    post_reversal_entry,
    post_shipping_cost_purchase,
)
from webapp.general_ledger_bp import general_ledger_rows
from webapp.journal_entries_bp import list_all_accounts


def test_general_ledger_rows_show_only_lines_touching_the_selected_account(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("150000"))
    post_shipping_cost_purchase(conn, entry_date=_dt.date(2026, 7, 12), amount_idr=Decimal("20000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    rows = general_ledger_rows(conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False)
    assert len(rows) == 1
    assert rows[0].debit_idr == Decimal("150000")
    assert rows[0].credit_idr == Decimal("0")


def test_general_ledger_rows_never_include_the_other_side_of_the_entry(wtopology):
    """A COGS purchase posts two lines (debit COGS, credit BCA Main). When
    the General Ledger is filtered to COGS, only the COGS line should show
    up — not a second row for BCA Main. This is the entire point of this
    screen being different from Journal Entries.
    """
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("150000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")
    bca_main_opt = next(o for o in all_accounts if o.account_type_code == "BCA_MAIN")

    cogs_rows = general_ledger_rows(conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False)
    bca_rows = general_ledger_rows(
        conn, account_option=bca_main_opt, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    assert len(cogs_rows) == 1
    assert len(bca_rows) == 1
    # Each account sees its OWN side only, never leaking the other account's
    # amount into its own row.
    assert cogs_rows[0].debit_idr == Decimal("150000") and cogs_rows[0].credit_idr == Decimal("0")
    assert bca_rows[0].credit_idr == Decimal("150000") and bca_rows[0].debit_idr == Decimal("0")


def test_general_ledger_running_balance_matches_independently_summed_lines(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("100000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 20), amount_idr=Decimal("50000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    rows = general_ledger_rows(conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False)
    assert len(rows) == 2
    # COGS is debit-normal; no prior activity, so opening balance is 0.
    assert rows[0].running_balance_idr == Decimal("100000")
    assert rows[1].running_balance_idr == Decimal("150000")
    # Independently re-derive the running balance from the raw rows
    # themselves (debit-normal: running += debit - credit) rather than
    # trusting the function's own internal math.
    running = Decimal("0")
    for r in rows:
        running += r.debit_idr - r.credit_idr
        assert running == r.running_balance_idr


def test_general_ledger_opening_balance_carries_forward_via_shared_helper(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 6, 10), amount_idr=Decimal("40000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 15), amount_idr=Decimal("60000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    july_rows = general_ledger_rows(conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False)
    assert len(july_rows) == 1
    # Opening balance from June's activity (40000) carries forward, so
    # July's one 60000 line lands the running balance at 100000, not 60000.
    assert july_rows[0].running_balance_idr == Decimal("100000")


def test_general_ledger_scope_all_spans_multiple_periods(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 6, 10), amount_idr=Decimal("40000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 15), amount_idr=Decimal("60000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    period_only = general_ledger_rows(conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False)
    all_time = general_ledger_rows(conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=True)
    assert len(period_only) == 1
    assert len(all_time) == 2
    assert all_time[-1].running_balance_idr == Decimal("100000")


def test_general_ledger_reversal_badges_are_accurate(wtopology):
    conn, topo = wtopology
    original_id = post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("50000"))
    reversal_id = post_reversal_entry(
        conn,
        original_journal_entry_id=original_id,
        entry_date=_dt.date(2026, 7, 10),
        memo="test correction of journal_entry_id=" + str(original_id),
    )
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    rows = general_ledger_rows(conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False)
    by_entry = {r.journal_entry_id: r for r in rows}
    assert by_entry[original_id].reversed_by_id == reversal_id
    assert by_entry[original_id].reversal_of_id is None
    assert by_entry[reversal_id].reversal_of_id == original_id
    assert by_entry[reversal_id].reversed_by_id is None


def test_general_ledger_row_keeps_traceability_ref_without_other_account_detail(wtopology):
    conn, topo = wtopology
    entry_id = post_income_line(
        conn,
        entry_date=_dt.date(2026, 7, 3),
        income_account_type_code="OTHER_INCOME",
        amount_idr=Decimal("50000"),
        paying_account_type_code="BCA_BRIDGING",
        paying_wallet_group_id=topo["wallet_group_id"],
        memo="test other income",
    )
    conn.commit()

    all_accounts = list_all_accounts(conn)
    other_income_opt = next(o for o in all_accounts if o.account_type_code == "OTHER_INCOME")

    rows = general_ledger_rows(
        conn, account_option=other_income_opt, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    assert len(rows) == 1
    assert rows[0].journal_entry_id == entry_id
    assert rows[0].credit_idr == Decimal("50000")


def test_general_ledger_route_defaults_to_a_sensible_account_when_none_selected(client, wtopology):
    resp = client.get("/general-ledger/?period=2026-07")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No accounts exist yet" not in body


def test_general_ledger_route_handles_no_data_without_crashing(client, wtopology):
    resp = client.get("/general-ledger/?period=2020-01")
    assert resp.status_code == 200
    assert "No posted journal lines" in resp.get_data(as_text=True)


def test_general_ledger_route_shows_journal_entry_link_not_other_side_of_entry(client, wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("150000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    resp = client.get(f"/general-ledger/?period=2026-07&account_id={cogs_opt.account_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/journal-entries/" in body
    # This row's own account (COGS) is debited, not credited — the other
    # side of the entry (BCA Main, credited) must not leak into the ROW
    # itself, even though "BCA Main" legitimately still appears elsewhere on
    # the page (the account-selector dropdown lists every account).
    table_body = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "BCA Main" not in table_body
    assert "150.000" in table_body


def test_general_ledger_route_account_scope_all_toggle_works(client, wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 6, 10), amount_idr=Decimal("40000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 15), amount_idr=Decimal("60000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    resp_period = client.get(f"/general-ledger/?period=2026-07&account_id={cogs_opt.account_id}")
    resp_all = client.get(
        f"/general-ledger/?period=2026-07&account_id={cogs_opt.account_id}&scope=all"
    )
    assert resp_period.status_code == 200
    assert resp_all.status_code == 200
    body_period = resp_period.get_data(as_text=True)
    body_all = resp_all.get_data(as_text=True)
    assert "1 journal line." in body_period
    assert "2 journal lines." in body_all
