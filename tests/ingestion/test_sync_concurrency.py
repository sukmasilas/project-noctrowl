"""Proves the 2026-09-01 concurrency fix under REAL concurrent execution —
two separate DB connections/transactions, forced to maximal overlap with a
``threading.Barrier`` — not just sequential tests (which could never have
caught this: QA found it precisely because sequential execution can't
exercise a race).

Mirrors QA's own reproduction: without the ``pg_try_advisory_xact_lock`` in
``ingestion.sync.run_sync_for_period``, two overlapping calls whose
``post_pending_rows`` both try to claim/post the SAME real Bridging <-> Main
sweep pair can each post their own separate journal entry for it. With the
fix, exactly one call wins the lock and does the work; the other fails fast
with ``SyncAlreadyRunningError`` instead of racing.

Uses its OWN engine/schema setup (not the shared ``iprototype`` fixture,
which hands back a single open connection/transaction — a real concurrency
test needs two independent connections a la two separate Flask requests).
"""
from __future__ import annotations

import datetime as _dt
import threading
from decimal import Decimal

import pytest
from sqlalchemy import select

from ingestion.drive_client import FOLDER_MIME_TYPE, DriveFile
from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.matching import RawLine, stage_raw_lines
from ingestion.schema import review_queue, source_documents
from ingestion.sync import SyncAlreadyRunningError, run_sync_for_period
from ledger import posting
from ledger.db import get_engine
from ledger.schema import create_schema, drop_schema, journal_entries
from ledger.seed import seed_catalogs, seed_prototype_topology
from tests._db_safety import resolve_test_database_url


class _EmptyDriveClient:
    """A Drive client with nothing in it at all — every folder lookup
    correctly resolves to None ("not yet uploaded"), so run_sync_for_period
    races through its Drive-fed steps near-instantly and the real,
    concurrency-sensitive work is entirely the final auto-match/post phase
    against rows staged directly below. root_id is never even asked for a
    child, since resolve_folder_path's first hop already finds nothing.
    """

    root_id = "fake-empty-root"

    def list_files(self, folder_id, mime_types=None):
        return []

    def download_file(self, file_id):
        raise AssertionError("should never be called — no files exist in this fake Drive tree")


@pytest.fixture()
def concurrency_setup():
    """Provision schema + topology + a real staged Bridging/Main sweep pair
    ONCE, on a dedicated setup connection that COMMITS (so both worker
    threads' own separate connections can see it) — then hand back the
    engine + topology for the test to open its own concurrent connections
    against.
    """
    engine = get_engine(resolve_test_database_url())
    drop_schema(engine)
    create_schema(engine)

    with engine.connect() as setup_conn:
        seed_catalogs(setup_conn)
        topo = seed_prototype_topology(setup_conn)
        wg = topo["wallet_group_id"]
        seed_kurs_pajak_rate(setup_conn, effective_date=_dt.date(2026, 4, 25), rate_idr=Decimal("17968.32"))

        # A real withdrawal already posted (mirrors "the Payoneer CSV +
        # confirmation were already ingested" — irrelevant to THIS race,
        # included only so the fixture looks like a realistic period, not
        # load-bearing for the concurrency assertion itself).
        posting.post_realized_fx_withdrawal(
            setup_conn,
            wallet_group_id=wg,
            entry_date=_dt.date(2026, 4, 28),
            gross_usd=Decimal("5000.00"),
            payoneer_fee_usd=Decimal("200.00"),
            exchange_rate_excl_fee=Decimal("17968.32"),
            booking_rate_used_idr=Decimal("17968.32"),
        )

        # The genuinely separate real sweep pair — exactly the shape
        # QA's own reproduction targeted: a Bridging outflow and a Master
        # inflow for the SAME real transfer, an amount that does NOT equal
        # net_idr_landed (per CLAUDE.md's Bridging Account correction).
        bridging_src = setup_conn.execute(
            source_documents.insert().values(
                document_type="bank_statement_wallet_group",
                period_month=_dt.date(2026, 4, 1),
                wallet_group_id=wg,
                drive_file_name="concurrency-test-fixture",
            )
        ).inserted_primary_key[0]
        master_src = setup_conn.execute(
            source_documents.insert().values(
                document_type="bank_statement_master",
                period_month=_dt.date(2026, 4, 1),
                drive_file_name="concurrency-test-fixture",
            )
        ).inserted_primary_key[0]

        stage_raw_lines(
            setup_conn,
            source_type="bank_statement",
            source_document_id=bridging_src,
            wallet_group_id=wg,
            lines=[
                RawLine(
                    transaction_date=_dt.date(2026, 4, 29),
                    raw_description="Transfer BI Fast / Ke BCA / DENNY WIJAYA 1790345891",
                    amount_idr=Decimal("-86250000.00"),
                    occurrence_index=1,
                )
            ],
        )
        stage_raw_lines(
            setup_conn,
            source_type="bank_statement",
            source_document_id=master_src,
            lines=[
                RawLine(
                    transaction_date=_dt.date(2026, 4, 30),
                    raw_description="BI-FAST CR BIF TRANSFER DR / 008 / RICO",
                    amount_idr=Decimal("86250000.00"),
                    occurrence_index=1,
                )
            ],
        )
        setup_conn.commit()

    yield engine, topo
    engine.dispose()


def test_concurrent_sync_runs_never_double_post_the_same_sweep_transfer(concurrency_setup):
    """The real proof: fire two run_sync_for_period() calls on two
    independent connections, held at a Barrier until BOTH are ready, then
    released simultaneously. Exactly one must succeed and post the real
    transfer; the other must fail fast with SyncAlreadyRunningError — never
    both succeeding, never both racing into a double post.
    """
    engine, topo = concurrency_setup
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def worker(name: str) -> None:
        with engine.connect() as conn:
            barrier.wait()  # both threads release at (as close to) the same instant
            try:
                result = run_sync_for_period(
                    conn,
                    _EmptyDriveClient(),
                    root_folder_id=_EmptyDriveClient.root_id,
                    period_month=_dt.date(2026, 4, 1),
                    ebay_account_id=topo["ebay_account_id"],
                    ebay_account_folder_name="eBay Account 1",
                    wallet_group_id=topo["wallet_group_id"],
                    wallet_group_folder_name="Wallet Group 1",
                )
                conn.commit()
                outcomes[name] = ("ok", result)
            except SyncAlreadyRunningError as exc:
                conn.rollback()
                outcomes[name] = ("rejected", exc)
            except Exception as exc:  # noqa: BLE001 — capture ANY unexpected failure, don't let a thread vanish silently
                conn.rollback()
                outcomes[name] = ("error", exc)

    t1 = threading.Thread(target=worker, args=("t1",))
    t2 = threading.Thread(target=worker, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert set(outcomes) == {"t1", "t2"}, f"a worker thread never reported an outcome: {outcomes}"
    statuses = [status for status, _ in outcomes.values()]
    assert statuses.count("error") == 0, f"unexpected exception in a worker: {outcomes}"
    assert statuses.count("ok") == 1, f"expected exactly one winner: {outcomes}"
    assert statuses.count("rejected") == 1, f"expected exactly one SyncAlreadyRunningError: {outcomes}"

    # --- The real assertion: exactly ONE journal entry for the transfer,
    # never two, no matter how tight the race was. ---
    with engine.connect() as verify_conn:
        transfer_entries = verify_conn.execute(
            select(journal_entries.c.id).where(journal_entries.c.source_type == "inter_account_transfer")
        ).all()
        assert len(transfer_entries) == 1, (
            f"double-posted! expected exactly 1 inter_account_transfer entry, found {len(transfer_entries)}"
        )

        posted_rows = verify_conn.execute(
            select(review_queue.c.id, review_queue.c.posted_at, review_queue.c.posted_journal_entry_id).where(
                review_queue.c.match_rule == "c-sweep"
            )
        ).all()
        assert len(posted_rows) == 2
        assert all(r.posted_at is not None for r in posted_rows)
        assert posted_rows[0].posted_journal_entry_id == posted_rows[1].posted_journal_entry_id

        # A later, legitimate sequential retry (simulating the rejected
        # click's user just clicking Sync Now again after the "already
        # running" message) finds nothing left to do — already fully
        # resolved by the winning thread, not silently stuck half-done.
        result = run_sync_for_period(
            verify_conn,
            _EmptyDriveClient(),
            root_folder_id=_EmptyDriveClient.root_id,
            period_month=_dt.date(2026, 4, 1),
            ebay_account_id=topo["ebay_account_id"],
            ebay_account_folder_name="eBay Account 1",
            wallet_group_id=topo["wallet_group_id"],
            wallet_group_folder_name="Wallet Group 1",
        )
        verify_conn.commit()
        assert result.posted.posted == 0
        assert result.posted.skipped_pending_pair == 0


def test_lock_is_reentrant_within_the_same_connection_sequential_calls(concurrency_setup):
    """Not a concurrency test — a guard against over-correcting: the SAME
    connection/transaction calling run_sync_for_period twice in a row
    (already exercised throughout test_sync.py's idempotency tests) must
    NOT be rejected by its own lock. Pins this explicitly here since it's
    the one behavior that would silently break if pg_try_advisory_xact_lock
    were ever swapped for a non-reentrant primitive.
    """
    engine, topo = concurrency_setup
    with engine.connect() as conn:
        r1 = run_sync_for_period(
            conn,
            _EmptyDriveClient(),
            root_folder_id=_EmptyDriveClient.root_id,
            period_month=_dt.date(2026, 4, 1),
            ebay_account_id=topo["ebay_account_id"],
            ebay_account_folder_name="eBay Account 1",
            wallet_group_id=topo["wallet_group_id"],
            wallet_group_folder_name="Wallet Group 1",
        )
        r2 = run_sync_for_period(  # same connection, same open transaction — must not raise
            conn,
            _EmptyDriveClient(),
            root_folder_id=_EmptyDriveClient.root_id,
            period_month=_dt.date(2026, 4, 1),
            ebay_account_id=topo["ebay_account_id"],
            ebay_account_folder_name="eBay Account 1",
            wallet_group_id=topo["wallet_group_id"],
            wallet_group_folder_name="Wallet Group 1",
        )
        conn.commit()
        assert r1.posted.posted == 2
        assert r2.posted.posted == 0  # already posted by r1, nothing left
