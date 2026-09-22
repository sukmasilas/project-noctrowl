"""Tests for the Journal Entries screen (webapp/journal_entries_bp.py) —
the posted-only, whole-chart-of-accounts, double-entry browse view. Renamed
from "General Ledger" (2026-09-16) — see webapp/journal_entries_bp.py's
module docstring for why, and webapp/general_ledger_bp.py for the new,
distinct per-account ledger screen that took over the old name. See
webapp/wallet_bp.py's module docstring for how this deliberately differs
from the Wallet screen.
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
from tests.webapp.conftest import make_review_queue_row, make_source_document
from webapp.journal_entries_bp import journal_entries_for_scope, list_all_accounts


def test_list_all_accounts_covers_every_statement_section(wtopology):
    conn, topo = wtopology
    options = list_all_accounts(conn)
    sections = {o.statement_section for o in options}
    assert sections == {"asset", "liability", "equity", "revenue", "cogs", "opex", "other_income_expense"}
    codes = {o.account_type_code for o in options}
    # Spot-check a representative code from each section is present.
    for expected in ("EBAY_WALLET", "CONSIGNOR_PAYABLE", "OWNERS_CAPITAL", "SALES_REVENUE", "COGS", "SHIPPING_COST", "OTHER_INCOME"):
        assert expected in codes


def test_journal_entries_all_accounts_mode_shows_both_lines_of_each_entry(wtopology):
    conn, topo = wtopology
    entry_id = post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("150000"))
    conn.commit()

    entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.id == entry_id
    assert len(e.lines) == 2
    codes = {l.account_label for l in e.lines}
    assert any("Cost of Goods Sold" in c for c in codes)
    assert any("BCA Main" in c for c in codes)
    total_debit = sum(l.debit_idr for l in e.lines)
    total_credit = sum(l.credit_idr for l in e.lines)
    assert total_debit == total_credit == Decimal("150000")


def test_journal_entries_all_accounts_mode_is_scoped_to_the_selected_period_only(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("150000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 8, 5), amount_idr=Decimal("90000"))
    conn.commit()

    july_entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    august_entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 8, 1), scope_all=False
    )
    assert len(july_entries) == 1
    assert len(august_entries) == 1
    assert july_entries[0].total_debit_idr == Decimal("150000")
    assert august_entries[0].total_debit_idr == Decimal("90000")


def test_journal_entries_account_filter_only_shows_entries_touching_that_account(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("150000"))
    post_shipping_cost_purchase(conn, entry_date=_dt.date(2026, 7, 12), amount_idr=Decimal("20000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    entries = journal_entries_for_scope(
        conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    assert len(entries) == 1
    assert entries[0].total_debit_idr == Decimal("150000")
    # Both sides of the entry are still shown even though we filtered by one account.
    assert len(entries[0].lines) == 2
    filtered_lines = [l for l in entries[0].lines if l.is_filtered_account]
    assert len(filtered_lines) == 1
    assert filtered_lines[0].debit_idr == Decimal("150000")


def test_journal_entries_account_filter_computes_running_balance_via_shared_helper(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 5), amount_idr=Decimal("100000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 20), amount_idr=Decimal("50000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    entries = journal_entries_for_scope(
        conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    assert len(entries) == 2
    # COGS is debit-normal; no prior activity, so opening balance is 0.
    assert entries[0].running_balance_idr == Decimal("100000")
    assert entries[1].running_balance_idr == Decimal("150000")


def test_journal_entries_account_filter_all_periods_scope_spans_multiple_months(wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 6, 10), amount_idr=Decimal("40000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 15), amount_idr=Decimal("60000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    period_only = journal_entries_for_scope(
        conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    all_time = journal_entries_for_scope(
        conn, account_option=cogs_opt, period_month=_dt.date(2026, 7, 1), scope_all=True
    )
    assert len(period_only) == 1
    assert len(all_time) == 2
    assert all_time[-1].running_balance_idr == Decimal("100000")


def test_journal_entries_reversal_pair_is_visibly_linked_both_directions(wtopology):
    conn, topo = wtopology
    original_id = post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("50000"))
    reversal_id = post_reversal_entry(
        conn,
        original_journal_entry_id=original_id,
        entry_date=_dt.date(2026, 7, 10),
        memo="test correction of journal_entry_id=" + str(original_id),
    )
    conn.commit()

    entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    by_id = {e.id: e for e in entries}
    assert by_id[original_id].reversed_by_id == reversal_id
    assert by_id[original_id].reversal_of_id is None
    assert by_id[reversal_id].reversal_of_id == original_id
    assert by_id[reversal_id].reversed_by_id is None


def test_journal_entries_real_reversal_pairs_from_actual_dev_data_shape(wtopology):
    """Reproduces the exact real-data shape found in the dev database
    (journal_entry_id 917 reversed by 947; 922/927/932 reversed by
    991/993/995 — see CLAUDE.md's Definition of done) using synthetic IDs,
    to make sure a MULTI-pair scenario (several independent reversals in the
    same period) all resolve correctly rather than only a single pair.
    """
    conn, topo = wtopology
    originals = [
        post_cogs_purchase(conn, entry_date=_dt.date(2026, 8, 5), amount_idr=Decimal("111000")),
        post_cogs_purchase(conn, entry_date=_dt.date(2026, 8, 11), amount_idr=Decimal("222000")),
        post_cogs_purchase(conn, entry_date=_dt.date(2026, 8, 28), amount_idr=Decimal("333000")),
    ]
    reversals = [
        post_reversal_entry(
            conn, original_journal_entry_id=oid, entry_date=oid_date, memo=f"Correction of journal_entry_id={oid}"
        )
        for oid, oid_date in zip(originals, [_dt.date(2026, 8, 5), _dt.date(2026, 8, 11), _dt.date(2026, 8, 28)])
    ]
    conn.commit()

    entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 8, 1), scope_all=False
    )
    by_id = {e.id: e for e in entries}
    for oid, rid in zip(originals, reversals):
        assert by_id[oid].reversed_by_id == rid
        assert by_id[rid].reversal_of_id == oid


def test_journal_entries_traces_review_queue_and_invoice_source(wtopology):
    conn, topo = wtopology
    from ingestion.schema import invoices as invoices_table

    inv_id = conn.execute(
        invoices_table.insert().values(
            drive_file_name="inv.pdf",
            period_month=_dt.date(2026, 7, 1),
            extracted_date=_dt.date(2026, 7, 5),
            vendor_description="Real Vendor Co",
            amount_idr=Decimal("150000"),
            purpose="cogs_purchase",
            status="parsed",
        )
    ).inserted_primary_key[0]

    entry_id = post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("150000"))

    src_id = make_source_document(conn, document_type="bank_statement_master", period_month=_dt.date(2026, 7, 1))
    make_review_queue_row(
        conn,
        source_document_id=src_id,
        transaction_date=_dt.date(2026, 7, 10),
        amount_idr=Decimal("-150000"),
        source_type="bank_statement",
        raw_description="COGS PAYMENT",
        category="cogs_purchase",
        linked_invoice_id=inv_id,
    )
    from sqlalchemy import update

    from ingestion.schema import review_queue

    conn.execute(
        update(review_queue)
        .where(review_queue.c.raw_description == "COGS PAYMENT")
        .values(posted_at=_dt.datetime.now(_dt.timezone.utc), posted_journal_entry_id=entry_id)
    )
    conn.commit()

    entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    e = next(e for e in entries if e.id == entry_id)
    kinds = {t.kind for t in e.source_traces}
    assert "review_queue" in kinds
    assert "invoice" in kinds
    assert any("Real Vendor Co" in t.label for t in e.source_traces)


def test_journal_entries_entry_with_no_linked_source_shows_empty_trace_not_a_crash(wtopology):
    conn, topo = wtopology
    entry_id = post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("50000"))
    conn.commit()

    entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    e = next(e for e in entries if e.id == entry_id)
    assert e.source_traces == []


def test_journal_entries_route_renders_reversal_badges_for_real_shape(client, wtopology):
    conn, topo = wtopology
    original_id = post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 10), amount_idr=Decimal("50000"))
    reversal_id = post_reversal_entry(
        conn,
        original_journal_entry_id=original_id,
        entry_date=_dt.date(2026, 7, 10),
        memo="Correction of journal_entry_id=" + str(original_id),
    )
    conn.commit()

    resp = client.get("/journal-entries/?period=2026-07")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f"Reversed by #{reversal_id}" in body
    assert f"Reversal of #{original_id}" in body


def test_journal_entries_route_handles_no_data_without_crashing(client, wtopology):
    resp = client.get("/journal-entries/?period=2020-01")
    assert resp.status_code == 200
    assert "No posted journal entries" in resp.get_data(as_text=True)


def test_journal_entries_route_account_scope_all_toggle_works(client, wtopology):
    conn, topo = wtopology
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 6, 10), amount_idr=Decimal("40000"))
    post_cogs_purchase(conn, entry_date=_dt.date(2026, 7, 15), amount_idr=Decimal("60000"))
    conn.commit()

    all_accounts = list_all_accounts(conn)
    cogs_opt = next(o for o in all_accounts if o.account_type_code == "COGS")

    resp_period = client.get(f"/journal-entries/?period=2026-07&account_id={cogs_opt.account_id}")
    resp_all = client.get(
        f"/journal-entries/?period=2026-07&account_id={cogs_opt.account_id}&scope=all"
    )
    assert resp_period.status_code == 200
    assert resp_all.status_code == 200
    body_period = resp_period.get_data(as_text=True)
    body_all = resp_all.get_data(as_text=True)
    assert "1 journal entry." in body_period
    assert "2 journal entries." in body_all


def test_journal_entries_owner_capital_line_shows_income_posting(wtopology):
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

    entries = journal_entries_for_scope(
        conn, account_option=None, period_month=_dt.date(2026, 7, 1), scope_all=False
    )
    e = next(e for e in entries if e.id == entry_id)
    labels = {l.account_label for l in e.lines}
    assert any("Other Income" in l for l in labels)
    assert any("Bridging" in l for l in labels)
