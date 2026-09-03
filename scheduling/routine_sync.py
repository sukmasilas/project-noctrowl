"""Automatic routine sync job.

Wraps ``ingestion.sync.run_sync_for_period`` (the full, already-proven
milestone-3 pipeline — list Drive folder, parse, stage, auto-match, post)
with the H-15/H+7 active-window gating from CLAUDE.md's "Scheduling &
triggers" section. Builds NO new ingestion/matching/posting logic of its
own (see CLAUDE.md's milestone-3-vs-5 boundary) — this module only decides
WHEN ``run_sync_for_period`` runs automatically, for every active eBay
account, for whichever period(s) ``scheduling.window.periods_to_sync``
says are in scope today.

Scheduling mechanism: a plain system cron job invoking
``scripts/scheduled_routine_sync.py`` once daily — NOT an in-process
scheduler (e.g. APScheduler) running inside the Flask process. See that
script's docstring for the exact crontab line and the full reasoning
(short version: the droplet is 1 vCPU/1GB RAM already running Postgres +
Flask; this job does OCR-heavy, potentially slow work that shouldn't
share a process/memory space with the always-on web app, and a crashed
cron job can't take the web app down with it).
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from ingestion.sync import SyncAlreadyRunningError, SyncResult, run_sync_for_period
from scheduling.window import periods_to_sync
from webapp.scoping import EbayAccountOption, list_ebay_accounts

logger = logging.getLogger(__name__)


@dataclass
class AccountPeriodOutcome:
    ebay_account_id: int
    ebay_account_name: str
    period_month: _dt.date
    status: str  # 'synced' | 'sync_already_running' | 'error'
    detail: str | None = None
    result: SyncResult | None = None


@dataclass
class RoutineSyncRunResult:
    ran_at: _dt.date
    active: bool
    periods: list[_dt.date] = field(default_factory=list)
    outcomes: list[AccountPeriodOutcome] = field(default_factory=list)


def run_routine_sync(
    engine: Engine,
    drive_client,
    *,
    root_folder_id: str,
    master_folder_name: str = "Master Account",
    today: _dt.date | None = None,
) -> RoutineSyncRunResult:
    """Runs the full sync pipeline for every active eBay account, for
    every period ``scheduling.window.periods_to_sync(today)`` says is in
    scope — or does nothing at all (returns ``active=False``) outside the
    H-15/H+7 window.

    One account/period failing (a raised exception, or losing the
    advisory-lock race to a concurrent manual "Sync Now" click — see
    ``ingestion.sync.SyncAlreadyRunningError``) never aborts the others;
    each account/period gets its own commit/rollback boundary on the same
    connection, exactly like ``webapp/documents_bp.py``'s manual trigger.
    """
    today = today or _dt.date.today()
    periods = periods_to_sync(today)
    if not periods:
        logger.info("Routine sync: outside the H-15/H+7 active window today (%s) — no-op.", today)
        return RoutineSyncRunResult(ran_at=today, active=False, periods=[])

    logger.info("Routine sync: active window today (%s) — syncing period(s): %s", today, periods)

    outcomes: list[AccountPeriodOutcome] = []
    with engine.connect() as conn:
        accounts = list_ebay_accounts(conn)
        if not accounts:
            logger.warning("Routine sync: no active eBay accounts configured — nothing to sync.")
        for account in accounts:
            for period in periods:
                outcomes.append(
                    _sync_one(conn, drive_client, account, period, root_folder_id, master_folder_name)
                )

    return RoutineSyncRunResult(ran_at=today, active=True, periods=periods, outcomes=outcomes)


def _sync_one(
    conn,
    drive_client,
    account: EbayAccountOption,
    period_month: _dt.date,
    root_folder_id: str,
    master_folder_name: str,
) -> AccountPeriodOutcome:
    try:
        result = run_sync_for_period(
            conn,
            drive_client,
            root_folder_id=root_folder_id,
            period_month=period_month,
            ebay_account_id=account.id,
            ebay_account_folder_name=account.ebay_account_drive_folder_name_resolved,
            wallet_group_id=account.wallet_group_id,
            wallet_group_folder_name=account.wallet_group_drive_folder_name_resolved,
            master_folder_name=master_folder_name,
        )
        conn.commit()
        posted = result.posted.posted if result.posted else 0
        logger.info(
            "Routine sync: account=%s period=%s — %d step(s), %d row(s) posted.",
            account.name,
            period_month,
            len(result.steps),
            posted,
        )
        return AccountPeriodOutcome(
            ebay_account_id=account.id,
            ebay_account_name=account.name,
            period_month=period_month,
            status="synced",
            result=result,
        )
    except SyncAlreadyRunningError as exc:
        conn.rollback()
        logger.warning("Routine sync: account=%s period=%s — %s", account.name, period_month, exc)
        return AccountPeriodOutcome(
            ebay_account_id=account.id,
            ebay_account_name=account.name,
            period_month=period_month,
            status="sync_already_running",
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — one account/period's failure must never abort the others
        conn.rollback()
        logger.exception("Routine sync: account=%s period=%s failed", account.name, period_month)
        return AccountPeriodOutcome(
            ebay_account_id=account.id,
            ebay_account_name=account.name,
            period_month=period_month,
            status="error",
            detail=str(exc),
        )
