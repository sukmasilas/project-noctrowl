"""Automatic monthly Google Drive folder provisioning.

CLAUDE.md's "Scheduling & triggers" section: "ahead of each new reporting
cycle, the backend automatically creates next month's upload folders
(including a new year folder when the year rolls over) under each
account... The user should always find a folder ready without creating it
themselves."

Uses ``ingestion.sync``'s own folder-naming constants and Year/Month
segment convention (``UPLOADS_ROOT_NAME``, ``EBAY_SALES_SUBFOLDER``,
``PAYONEER_SUBFOLDER``, ``BANK_STATEMENTS_SUBFOLDER``,
``INVOICES_SUBFOLDER``, ``_period_segments``) so this job's folder layout
can never drift from what ``ingestion.sync`` reads back later — this
module deliberately does NOT define a second, independently-maintained
path convention (per this milestone's brief).

``ingestion.sync.resolve_folder_path`` is deliberately READ-ONLY (its own
docstring: "never fabricates/creates an ID") — that's exactly right for
the sync pipeline, which must never silently create a folder it's only
supposed to be reading from. This module needs the necessary WRITE-capable
sibling, since its entire job IS to create folders ahead of time; see
``_find_or_create_path`` below, which walks the exact same segment order
via ``DriveClient.find_or_create_folder`` (already idempotent — see
``ingestion/drive_client.py``).
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from ingestion.sync import (
    BANK_STATEMENTS_SUBFOLDER,
    EBAY_SALES_SUBFOLDER,
    INVOICES_SUBFOLDER,
    PAYONEER_SUBFOLDER,
    UPLOADS_ROOT_NAME,
    _period_segments,
)
from scheduling.window import next_month_start
from webapp.scoping import list_ebay_accounts

logger = logging.getLogger(__name__)


@dataclass
class ProvisionedPath:
    scope: str  # 'ebay_account' | 'wallet_group' | 'master'
    scope_name: str
    subfolder: str
    period_month: _dt.date
    folder_id: str


@dataclass
class DriveProvisioningResult:
    ran_at: _dt.date
    target_period: _dt.date
    paths: list[ProvisionedPath] = field(default_factory=list)


def _find_or_create_path(drive_client, root_folder_id: str, *names: str) -> str:
    """Write-capable sibling of ``ingestion.sync.resolve_folder_path`` —
    same by-name, segment-by-segment walk, but creates any segment that
    doesn't exist yet instead of returning None. Every call is idempotent
    (``find_or_create_folder`` is a get-or-create by name), so re-running
    this against an already-provisioned tree is always safe.
    """
    current_id = root_folder_id
    for name in names:
        current_id = drive_client.find_or_create_folder(current_id, name)
    return current_id


def run_drive_folder_provisioning(
    engine: Engine,
    drive_client,
    *,
    root_folder_id: str,
    master_folder_name: str = "Master Account",
    today: _dt.date | None = None,
) -> DriveProvisioningResult:
    """Idempotently ensures NEXT month's (and, on a year rollover, next
    year's) upload folders exist for every active eBay account, every
    distinct active wallet-group, and the Master Account — matching
    CLAUDE.md's Architecture section's Drive folder structure exactly.
    Safe to run repeatedly (every call is a get-or-create by name, never a
    blind create).
    """
    today = today or _dt.date.today()
    target_period = next_month_start(today)
    year, ym = _period_segments(target_period)

    paths: list[ProvisionedPath] = []
    with engine.connect() as conn:
        accounts = list_ebay_accounts(conn)
        if not accounts:
            logger.warning("Drive folder provisioning: no active eBay accounts configured — nothing to provision.")

        for account in accounts:
            folder_id = _find_or_create_path(
                drive_client,
                root_folder_id,
                UPLOADS_ROOT_NAME,
                account.ebay_account_drive_folder_name_resolved,
                year,
                ym,
                EBAY_SALES_SUBFOLDER,
            )
            paths.append(ProvisionedPath("ebay_account", account.name, EBAY_SALES_SUBFOLDER, target_period, folder_id))

        seen_wallet_groups: set[int] = set()
        for account in accounts:
            if account.wallet_group_id in seen_wallet_groups:
                continue
            seen_wallet_groups.add(account.wallet_group_id)
            for subfolder in (PAYONEER_SUBFOLDER, BANK_STATEMENTS_SUBFOLDER):
                folder_id = _find_or_create_path(
                    drive_client,
                    root_folder_id,
                    UPLOADS_ROOT_NAME,
                    account.wallet_group_drive_folder_name_resolved,
                    year,
                    ym,
                    subfolder,
                )
                paths.append(
                    ProvisionedPath("wallet_group", account.wallet_group_name, subfolder, target_period, folder_id)
                )

        for subfolder in (BANK_STATEMENTS_SUBFOLDER, INVOICES_SUBFOLDER):
            folder_id = _find_or_create_path(
                drive_client, root_folder_id, UPLOADS_ROOT_NAME, master_folder_name, year, ym, subfolder
            )
            paths.append(ProvisionedPath("master", master_folder_name, subfolder, target_period, folder_id))

    logger.info(
        "Drive folder provisioning: ensured %d folder(s) exist for target period %s.", len(paths), target_period
    )
    return DriveProvisioningResult(ran_at=today, target_period=target_period, paths=paths)
