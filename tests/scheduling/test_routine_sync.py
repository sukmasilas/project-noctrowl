"""Tests for scheduling.routine_sync — the H-15/H+7 active-window wrapper
around ingestion.sync.run_sync_for_period. Deliberately does NOT re-prove
parser/matching correctness (already covered in tests/ingestion/
test_sync.py against real sample documents) — this file is specifically
about the ORCHESTRATION concerns unique to this milestone: window gating,
looping across every active account/period, and one account/period's
failure never blocking another's.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

import scheduling.routine_sync as routine_sync_module
from ingestion.schema import source_documents
from ingestion.sync import SyncAlreadyRunningError
from scheduling.routine_sync import run_routine_sync
from tests.ingestion.test_sync import FakeDriveClient


def test_run_routine_sync_no_op_outside_active_window(stopology, sengine):
    result = run_routine_sync(sengine, FakeDriveClient(), root_folder_id="root", today=dt.date(2026, 8, 10))
    assert result.active is False
    assert result.periods == []
    assert result.outcomes == []


def test_run_routine_sync_syncs_current_account_for_active_period(stopology, sengine):
    conn, topo = stopology
    client = FakeDriveClient()

    result = run_routine_sync(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20))
    assert result.active is True
    assert result.periods == [dt.date(2026, 8, 1)]
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status == "synced"
    assert outcome.ebay_account_id == topo["ebay_account_id"]
    assert outcome.period_month == dt.date(2026, 8, 1)

    # A source_documents row for "not yet uploaded" (empty fake Drive tree)
    # confirms the underlying pipeline actually ran, not just returned a
    # fake success.
    with sengine.connect() as check_conn:
        row = check_conn.execute(
            select(source_documents.c.ingested_at).where(
                source_documents.c.document_type == "ebay_sales_csv",
                source_documents.c.ebay_account_id == topo["ebay_account_id"],
                source_documents.c.period_month == dt.date(2026, 8, 1),
            )
        ).first()
    assert row is not None
    assert row.ingested_at is None  # nothing uploaded in the fake tree yet


def test_run_routine_sync_h7_tail_syncs_both_current_and_previous_period(stopology, sengine):
    client = FakeDriveClient()
    result = run_routine_sync(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 9, 3))
    assert result.periods == [dt.date(2026, 9, 1), dt.date(2026, 8, 1)]
    assert {o.period_month for o in result.outcomes} == {dt.date(2026, 9, 1), dt.date(2026, 8, 1)}
    assert all(o.status == "synced" for o in result.outcomes)


def test_run_routine_sync_loops_every_active_account(sfull_topology, sengine):
    """Never hardcode 'there's exactly one eBay account' — confirms every
    active account from list_ebay_accounts gets its own sync attempt.
    """
    conn, topo = sfull_topology
    client = FakeDriveClient()

    result = run_routine_sync(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20))
    synced_account_ids = {o.ebay_account_id for o in result.outcomes}
    assert synced_account_ids == set(topo["ebay_accounts"].values())
    assert all(o.status == "synced" for o in result.outcomes)


def test_run_routine_sync_no_active_accounts_is_a_clean_no_op(sconn, sengine):
    client = FakeDriveClient()
    result = run_routine_sync(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20))
    assert result.active is True
    assert result.outcomes == []


def test_run_routine_sync_one_accounts_error_does_not_block_another(sfull_topology, sengine, monkeypatch):
    conn, topo = sfull_topology
    client = FakeDriveClient()
    failing_account_id = topo["ebay_accounts"]["2"]

    real_run_sync_for_period = routine_sync_module.run_sync_for_period

    def flaky_run_sync_for_period(conn, drive_client, **kwargs):
        if kwargs.get("ebay_account_id") == failing_account_id:
            raise RuntimeError("simulated parser crash")
        return real_run_sync_for_period(conn, drive_client, **kwargs)

    monkeypatch.setattr(routine_sync_module, "run_sync_for_period", flaky_run_sync_for_period)

    result = run_routine_sync(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20))
    by_account = {o.ebay_account_id: o.status for o in result.outcomes}
    assert by_account[failing_account_id] == "error"
    # The other two accounts still synced successfully despite the failure.
    other_ids = set(topo["ebay_accounts"].values()) - {failing_account_id}
    assert all(by_account[aid] == "synced" for aid in other_ids)


def test_run_routine_sync_surfaces_sync_already_running_without_crashing(stopology, sengine, monkeypatch):
    def always_busy(conn, drive_client, **kwargs):
        raise SyncAlreadyRunningError("another sync is already running")

    monkeypatch.setattr(routine_sync_module, "run_sync_for_period", always_busy)

    client = FakeDriveClient()
    result = run_routine_sync(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20))
    assert result.outcomes[0].status == "sync_already_running"
