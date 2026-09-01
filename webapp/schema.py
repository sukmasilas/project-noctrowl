"""Milestone 4 schema addition: the Sync Now cooldown tracker.

The ONLY new table this milestone needs at the database level (everything
else the web app reads/writes already exists in ``ledger.schema`` /
``ingestion.schema``). Registered on the SAME shared SQLAlchemy ``MetaData``
object those two packages already use (imported, not recreated) — the same
additive pattern ``ingestion/schema.py`` established for milestone 3's
tables, so ``create_webapp_schema(engine)`` below only ever needs to run
after ``ledger.schema.create_schema`` / ``ingestion.schema.create_ingestion_schema``
to fully provision the database, and a plain ``metadata.create_all(engine)``
anywhere still picks up all three milestones' tables together.

See docs/design/milestone-4-web-app-design.md §6 for why this table exists:
the Sync Now button's cooldown (a few minutes after each run, per CLAUDE.md's
Scheduling section) needs *some* durable place to remember "when did the
last run finish" that survives an app restart — a plain DB table rather than
an in-process global, since this app may run under more than one worker
process on the droplet.
"""
from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, Integer, Table, Text, func
from sqlalchemy.engine import Engine

from ledger.schema import metadata

webapp_sync_runs = Table(
    "webapp_sync_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("triggered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    # Always the shared-login username for this prototype (no per-user
    # identity beyond that) — kept for the action-log visibility CLAUDE.md
    # expects, not for any access-control purpose.
    Column("triggered_by", Text, nullable=True),
    Column("ebay_account_id", Integer, nullable=True),
    Column("period_month", Date, nullable=True),
    Column("result_summary", Text, nullable=True),
)


def create_webapp_schema(engine: Engine) -> None:
    """Create the milestone-4 table on top of an already-created
    milestone-2/3 schema. Safe to call standalone: by the time this module
    is imported, the table is already registered on the shared ``metadata``
    object, so a plain ``metadata.create_all(engine)`` here picks up
    everything (idempotent — ``checkfirst`` is the default).
    """
    metadata.create_all(engine)


def drop_webapp_schema(engine: Engine) -> None:
    """Drop only the milestone-4 table (test/dev use only)."""
    webapp_sync_runs.drop(engine, checkfirst=True)
