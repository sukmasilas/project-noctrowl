"""One-off data-repair script: fix stale review_queue.match_status on rows
that have already posted.

WHY THIS EXISTS (2026-09-14): see CLAUDE.md's "Correcting a posted
review-queue row" section, "New low-priority gap found by QA 2026-09-05"
note. ``ingestion/matching.py::post_pending_rows`` was setting
``posted_at``/``posted_journal_entry_id`` (and clearing the mismatch/missing-
reference reasons) on a successful post, but never flipping
``match_status`` from ``needs_review`` to ``matched``. That normal-sync-path
bug has now been fixed directly in ``post_pending_rows`` (a one-line
addition to its existing success-path UPDATE) -- this script is the
one-off backfill for rows that already posted *before* that fix landed, so
the real local dev database doesn't keep showing an amber "Needs Review"
badge for rows that are actually done.

This is a pure metadata/status-field correction -- it never touches any
journal entry, ledger balance, or accounting logic. It does NOT touch
category, posted_at, posted_journal_entry_id, sign_mismatch_reason,
missing_reference_reason, or any other column -- only match_status, and
only on rows that meet the exact criteria below.

SCOPE: every review_queue row where posted_at IS NOT NULL AND
match_status != 'matched' (i.e. still 'needs_review' despite having
genuinely posted). Rows with posted_at IS NULL (genuinely still pending)
are never touched.

IDEMPOTENT: re-running this script after it's already fixed everything is
a no-op (the WHERE clause naturally selects zero rows the second time).

USAGE:
    python3 scripts/backfill_stale_needs_review_status.py

Reads DATABASE_URL from the environment via python-dotenv (same pattern as
scripts/run_migrations.py) -- never hardcodes a connection string.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select, update  # noqa: E402

import ingestion.schema  # noqa: E402,F401 - registers review_queue on the shared metadata
from ingestion.schema import review_queue  # noqa: E402
from ledger.db import get_engine  # noqa: E402


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    with engine.begin() as conn:
        stale = conn.execute(
            select(
                review_queue.c.id,
                review_queue.c.source_type,
                review_queue.c.transaction_date,
                review_queue.c.amount_idr,
                review_queue.c.raw_description,
                review_queue.c.category,
                review_queue.c.posted_at,
                review_queue.c.posted_journal_entry_id,
                review_queue.c.match_status,
            )
            .where(review_queue.c.posted_at.isnot(None))
            .where(review_queue.c.match_status != "matched")
            .order_by(review_queue.c.id)
        ).all()

        print(f"Found {len(stale)} row(s) with posted_at set but match_status != 'matched':\n")
        for row in stale:
            print(
                f"  id={row.id} source_type={row.source_type} date={row.transaction_date} "
                f"amount_idr={row.amount_idr} category={row.category!r} "
                f"posted_at={row.posted_at} posted_journal_entry_id={row.posted_journal_entry_id} "
                f"match_status(before)={row.match_status!r} "
                f"raw_description={row.raw_description!r}"
            )

        if not stale:
            print("Nothing to fix. Exiting.")
            return 0

        ids = [row.id for row in stale]
        conn.execute(
            update(review_queue)
            .where(review_queue.c.id.in_(ids))
            .values(match_status="matched")
        )

        after = conn.execute(
            select(review_queue.c.id, review_queue.c.match_status).where(review_queue.c.id.in_(ids))
        ).all()
        after_by_id = {row.id: row.match_status for row in after}

        print(f"\nUpdated {len(ids)} row(s) to match_status='matched':\n")
        for row in stale:
            print(f"  id={row.id}: match_status {row.match_status!r} -> {after_by_id[row.id]!r}")

    print(f"\nBackfill complete. {len(stale)} row(s) corrected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
