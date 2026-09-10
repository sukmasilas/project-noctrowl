"""One-off data-repair script: relabel every real, UNPOSTED review_queue row
whose ``raw_description`` is the real BCA Main Account admin-fee line
("BIAYA ADM" / "BIAYA ADM 0998") to ``category='operating_expense'`` on the
real ``noctrowl`` database (2026-09-10 — closes the real gap Main-agent's
brief flagged: the existing "Biaya administrasi rekening" keyword is LONGER
than, and can never match, this shorter real description).

WHY A DIRECT SCRIPT, NOT THE GENERAL AUTO-MATCH PIPELINE: same reasoning as
``scripts/relabel_kurasi_shipping_cost.py`` (see its own docstring) — the
general ``ingestion.matching.run_auto_match`` re-evaluates every
``category IS NULL`` row in the ENTIRE ``review_queue`` table (over 100 real
rows as of this script's writing, not just the BIAYA ADM ones), far outside
this task's narrow scope. A direct, narrowly-scoped UPDATE, restricted to
rows whose raw_description IS (exactly, case-insensitively) one of the two
real observed forms, is safer and exactly reproducible — it can never touch
the textually similar but genuinely different "Biaya administrasi
rekening"/"Biaya administrasi kartu debit" lines (which don't match this
exact-string filter at all, let alone the word-boundary-aware substring
check the real keyword rule itself uses).

SCOPE: only rows whose raw_description, trimmed, case-insensitively equals
"BIAYA ADM" or "BIAYA ADM 0998" — the two real forms confirmed across the
real May-Aug 2026 BCA Main Account statements (see ingestion/seed.py's
BANK_KEYWORD_RULES note).

IDEMPOTENT: safe to re-run — the WHERE clause only ever matches rows that
still need correcting (category IS NULL); once a row is fixed to
'operating_expense' it no longer matches.

USAGE:
    python3 scripts/relabel_biaya_adm_operating_expense.py

Reads DATABASE_URL from the environment via python-dotenv — never
hardcodes a connection string.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import func, select, update  # noqa: E402

from ingestion.schema import review_queue  # noqa: E402
from ledger.db import get_engine  # noqa: E402

BIAYA_ADM_FILTER = func.upper(review_queue.c.raw_description).in_(["BIAYA ADM", "BIAYA ADM 0998"])


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        all_rows = conn.execute(
            select(
                review_queue.c.id,
                review_queue.c.raw_description,
                review_queue.c.category,
                review_queue.c.posted_at,
                review_queue.c.posted_journal_entry_id,
                review_queue.c.amount_idr,
            ).where(BIAYA_ADM_FILTER)
        ).all()

        posted = [r for r in all_rows if r.posted_at is not None]
        unposted = [r for r in all_rows if r.posted_at is None]
        null_category = [r for r in unposted if r.category is None]
        other_unposted = [r for r in unposted if r.category is not None]

        print(f"\nFound {len(all_rows)} total review_queue row(s) matching 'BIAYA ADM'/'BIAYA ADM 0998':")
        print(f"  {len(null_category)} unposted, category IS NULL (the real gap to fix)")
        print(f"  {len(other_unposted)} unposted, ALREADY has some category (left untouched)")
        print(f"  {len(posted)} ALREADY POSTED (left untouched, per the deferred-corrections rule)")

        if other_unposted:
            for r in other_unposted:
                print(f"    unposted-but-labeled: id={r.id} category={r.category!r} amount_idr={r.amount_idr}")
        if posted:
            for r in posted:
                print(
                    f"    already-posted: id={r.id} category={r.category!r} amount_idr={r.amount_idr} "
                    f"posted_journal_entry_id={r.posted_journal_entry_id}"
                )

        to_fix_ids = [r.id for r in null_category]
        if not to_fix_ids:
            print("\nNothing to fix — no unposted, uncategorized BIAYA ADM rows found.")
            return 0

        result = conn.execute(
            update(review_queue)
            .where(review_queue.c.id.in_(to_fix_ids))
            .where(review_queue.c.posted_at.is_(None))  # belt-and-suspenders re-check at UPDATE time
            .values(
                category="operating_expense",
                match_status="matched",
                match_rule="e",
                labeled_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        print(f"\nUpdated {result.rowcount} row(s) to category='operating_expense' (match_rule='e').")

        after = conn.execute(
            select(review_queue.c.id, review_queue.c.category, review_queue.c.posted_at).where(
                review_queue.c.id.in_(to_fix_ids)
            )
        ).all()
        wrong = [r for r in after if r.category != "operating_expense" or r.posted_at is not None]
        if wrong:
            print("\nERROR: verification failed for these rows:")
            for r in wrong:
                print(f"  id={r.id} category={r.category!r} posted_at={r.posted_at}")
            raise SystemExit(1)
        print(f"Verified: all {len(after)} updated rows now show category='operating_expense', posted_at IS NULL.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
