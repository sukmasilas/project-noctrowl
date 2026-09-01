"""Sync Now cooldown tracking.

See docs/design/milestone-4-web-app-design.md §6. A durable (DB-backed,
not in-process) cooldown so it survives an app restart / multiple worker
processes on the droplet. Only the cooldown mechanism itself is in scope
here — the H-15/H+7 automatic scheduling window stays milestone 5, per
CLAUDE.md's Build milestones boundary.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import select
from sqlalchemy.engine import Connection

from webapp.schema import webapp_sync_runs

# "A few minutes" per CLAUDE.md's Scheduling section language.
SYNC_COOLDOWN = _dt.timedelta(minutes=5)


def last_sync_run(conn: Connection):
    return conn.execute(select(webapp_sync_runs).order_by(webapp_sync_runs.c.triggered_at.desc())).first()


def seconds_until_next_allowed(conn: Connection) -> int:
    """0 if a sync may run right now; otherwise how many seconds remain."""
    row = last_sync_run(conn)
    if row is None:
        return 0
    triggered_at = row.triggered_at
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=_dt.timezone.utc)
    elapsed = _dt.datetime.now(_dt.timezone.utc) - triggered_at
    remaining = SYNC_COOLDOWN - elapsed
    return max(0, int(remaining.total_seconds()))


def record_sync_run(
    conn: Connection,
    *,
    triggered_by: str | None,
    ebay_account_id: int | None,
    period_month: _dt.date | None,
    result_summary: str | None,
) -> int:
    result = conn.execute(
        webapp_sync_runs.insert().values(
            triggered_by=triggered_by,
            ebay_account_id=ebay_account_id,
            period_month=period_month,
            completed_at=_dt.datetime.now(_dt.timezone.utc),
            result_summary=result_summary,
        )
    )
    return result.inserted_primary_key[0]
