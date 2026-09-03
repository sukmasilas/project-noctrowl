"""Tests for scheduling.drive_provisioning — automatic month-ahead (and,
on a year rollover, year-ahead) Google Drive upload-folder provisioning.
Uses a plain in-memory fake satisfying find_or_create_folder's shape (no
real Drive/network) — mirrors the same no-network testing style already
established for the rest of this project's Drive-touching code
(tests/ingestion/test_sync.py's FakeDriveClient).
"""
from __future__ import annotations

import datetime as dt

from ingestion.drive_client import FOLDER_MIME_TYPE, DriveFile
from ingestion.sync import (
    BANK_STATEMENTS_SUBFOLDER,
    EBAY_SALES_SUBFOLDER,
    INVOICES_SUBFOLDER,
    PAYONEER_SUBFOLDER,
    UPLOADS_ROOT_NAME,
)
from scheduling.drive_provisioning import run_drive_folder_provisioning


class FakeProvisioningDriveClient:
    """Same idempotent get-or-create-by-name semantics as the real
    ingestion.drive_client.DriveClient.find_or_create_folder, tracked in
    plain memory so a test can assert exactly what got created.
    """

    def __init__(self):
        self._next_id = 0
        self._entries: dict[str, dict] = {}
        self.root_id = self._add(parent=None, name="root")
        self.create_calls: list[tuple[str, str]] = []

    def _add(self, *, parent, name) -> str:
        self._next_id += 1
        fid = f"fake-{self._next_id}"
        self._entries[fid] = {"parent": parent, "name": name}
        return fid

    def find_or_create_folder(self, parent_id: str, name: str) -> str:
        existing = next(
            (fid for fid, e in self._entries.items() if e["parent"] == parent_id and e["name"] == name),
            None,
        )
        if existing:
            return existing
        self.create_calls.append((parent_id, name))
        return self._add(parent=parent_id, name=name)

    def list_files(self, folder_id: str, mime_types=None):
        results = []
        for fid, e in self._entries.items():
            if e["parent"] != folder_id:
                continue
            results.append(DriveFile(id=fid, name=e["name"], mime_type=FOLDER_MIME_TYPE, modified_time=None))
        return results

    def path_exists(self, *names: str) -> bool:
        current = self.root_id
        for name in names:
            match = next(
                (fid for fid, e in self._entries.items() if e["parent"] == current and e["name"] == name), None
            )
            if match is None:
                return False
            current = match
        return True


def test_provisions_next_months_folders_for_the_prototype_account(stopology, sengine):
    from webapp.scoping import list_ebay_accounts

    conn, topo = stopology
    account = list_ebay_accounts(conn)[0]
    client = FakeProvisioningDriveClient()

    result = run_drive_folder_provisioning(
        sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20)
    )
    assert result.target_period == dt.date(2026, 9, 1)

    assert client.path_exists(
        UPLOADS_ROOT_NAME, account.ebay_account_drive_folder_name_resolved, "2026", "2026-09", EBAY_SALES_SUBFOLDER
    )
    assert client.path_exists(
        UPLOADS_ROOT_NAME,
        account.wallet_group_drive_folder_name_resolved,
        "2026",
        "2026-09",
        PAYONEER_SUBFOLDER,
    )
    assert client.path_exists(UPLOADS_ROOT_NAME, "Master Account", "2026", "2026-09", BANK_STATEMENTS_SUBFOLDER)
    assert client.path_exists(UPLOADS_ROOT_NAME, "Master Account", "2026", "2026-09", INVOICES_SUBFOLDER)


def test_provisions_year_rollover_folders(stopology, sengine):
    conn, topo = stopology
    client = FakeProvisioningDriveClient()

    result = run_drive_folder_provisioning(
        sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 12, 20)
    )
    assert result.target_period == dt.date(2027, 1, 1)
    assert client.path_exists(UPLOADS_ROOT_NAME, "Master Account", "2027", "2027-01", BANK_STATEMENTS_SUBFOLDER)


def test_idempotent_second_run_creates_nothing_new(stopology, sengine):
    conn, topo = stopology
    client = FakeProvisioningDriveClient()

    run_drive_folder_provisioning(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20))
    first_call_count = len(client.create_calls)
    assert first_call_count > 0

    run_drive_folder_provisioning(sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 21))
    assert len(client.create_calls) == first_call_count  # no NEW folders created on the re-run


def test_provisions_for_every_active_account_and_deduplicates_shared_wallet_group(sfull_topology, sengine):
    """seed_full_topology has 2 eBay accounts sharing one wallet-group and a
    3rd with its own — confirms this job creates the wallet-group's
    Payoneer/Bank folder only ONCE for the shared pair, not twice (same
    "never double-provision the shared pool" spirit as the Cash Flow
    dedup logic elsewhere in this project), while still creating each
    eBay account's OWN sales-export folder individually.
    """
    conn, topo = sfull_topology
    client = FakeProvisioningDriveClient()

    result = run_drive_folder_provisioning(
        sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20)
    )

    ebay_account_paths = [p for p in result.paths if p.scope == "ebay_account"]
    assert len(ebay_account_paths) == 3  # one per eBay account, each its own sales-export folder

    wallet_group_paths = [p for p in result.paths if p.scope == "wallet_group"]
    # 2 distinct wallet-groups x 2 subfolders (Payoneer, Bank Statements) = 4
    # — the shared wallet-group must NOT appear twice just because 2 eBay
    # accounts reference it.
    assert len(wallet_group_paths) == 4


def test_provisioning_is_read_only_on_folders_that_already_exist(stopology, sengine):
    """A folder created by a PRIOR provisioning run (or manually) must be
    reused (get-or-create), never duplicated — proven by seeding one
    target folder ahead of time and confirming find_or_create_folder
    returns that exact same id rather than minting a new one.
    """
    conn, topo = stopology
    client = FakeProvisioningDriveClient()

    # Pre-create the Master Account's September Bank Statements folder by
    # hand, simulating "already provisioned by an earlier run".
    uploads = client.find_or_create_folder(client.root_id, UPLOADS_ROOT_NAME)
    master = client.find_or_create_folder(uploads, "Master Account")
    year = client.find_or_create_folder(master, "2026")
    ym = client.find_or_create_folder(year, "2026-09")
    pre_existing_id = client.find_or_create_folder(ym, BANK_STATEMENTS_SUBFOLDER)
    client.create_calls.clear()

    result = run_drive_folder_provisioning(
        sengine, client, root_folder_id=client.root_id, today=dt.date(2026, 8, 20)
    )
    matching = [p for p in result.paths if p.scope == "master" and p.subfolder == BANK_STATEMENTS_SUBFOLDER]
    assert len(matching) == 1
    assert matching[0].folder_id == pre_existing_id
    # No NEW create call for this already-existing folder chain.
    assert (uploads, "Master Account") not in client.create_calls
