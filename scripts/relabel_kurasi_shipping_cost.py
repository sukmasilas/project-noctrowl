"""One-off data-repair script: relabel every real, UNPOSTED review_queue row
whose ``raw_description`` mentions "KURASI" to ``category='shipping_cost'``
on the real ``noctrowl`` database (2026-09-09, confirmed directly by the
user: Kurasi is a real shipping vendor — every bank line matching "KURASI"
is a shipping cost, no exceptions).

WHY A DIRECT SCRIPT, NOT THE GENERAL AUTO-MATCH PIPELINE: the general
``ingestion.matching.run_auto_match`` re-evaluates every ``category IS NULL``
row in the ENTIRE ``review_queue`` table (268 real rows as of this script's
writing, not just the 34 Kurasi ones), running rules (a)-(e) against every
one of them — far outside this task's scope ("Don't touch anything else — no
changes to ... other categories/keyword rules beyond what's described").
Even the newly-seeded KURASI keyword rule alone would still require running
that broader pass to reach the Kurasi rows, since ``run_auto_match`` has no
own scoping mechanism. A direct, narrowly-scoped UPDATE (matching
Main-agent's own guardrail: "pick the more direct, verifiable option ...
rather than relying on implicit pipeline behavior you can't fully confirm")
is safer and exactly reproducible.

SCOPE, exactly matching Main-agent's brief:
  - 34 rows with category IS NULL (never touched) -> relabeled to
    'shipping_cost'.
  - 9 rows with category='cogs_purchase' (a real mislabel made during an
    earlier project validation pass, before Kurasi's identity was
    confirmed) that are NOT YET POSTED (posted_at IS NULL) -> relabeled to
    'shipping_cost'.
  - Total real rows touched by this script: 43, not the 46 originally
    described in the brief.

GUARDRAIL HIT, ESCALATED — 3 ROWS DELIBERATELY *NOT* TOUCHED: querying the
real database ahead of writing this script found 3 additional rows matching
"KURASI" that had ALREADY POSTED (review_queue.id 345 / 371 / 419 ->
posted_journal_entry_id 922 / 927 / 932 respectively), each mislabeled
'cogs_purchase' and posted to the COGS account instead of SHIPPING_COST.
This contradicts the brief's stated premise ("none of these 46 rows are
posted, posted_at IS NULL on every one"). Per Main-agent's own explicit
guardrail ("If you find any row matching 'KURASI' that HAS already posted,
stop and report back rather than touching it") and CLAUDE.md's deferred
"corrections to a posted row" rule, this script's WHERE clause explicitly
excludes ``posted_at IS NOT NULL`` rows — they are left completely
untouched, both their ``category`` and their already-posted journal entries.
Reported back to Main-agent/QA as a separate open item; NOT silently fixed
here.

IDEMPOTENT: safe to re-run — the WHERE clause only ever matches rows that
still need correcting (category IS NULL, or category still
'cogs_purchase'); once a row is fixed to 'shipping_cost' it no longer
matches the WHERE clause on a subsequent run.

USAGE:
    python3 scripts/relabel_kurasi_shipping_cost.py

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

from sqlalchemy import select, update  # noqa: E402

from ingestion.schema import review_queue  # noqa: E402
from ledger.db import get_engine  # noqa: E402

KURASI_FILTER = review_queue.c.raw_description.ilike("%kurasi%")


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        all_kurasi = conn.execute(
            select(
                review_queue.c.id,
                review_queue.c.category,
                review_queue.c.posted_at,
                review_queue.c.posted_journal_entry_id,
                review_queue.c.amount_idr,
            ).where(KURASI_FILTER)
        ).all()

        posted = [r for r in all_kurasi if r.posted_at is not None]
        unposted = [r for r in all_kurasi if r.posted_at is None]
        null_category = [r for r in unposted if r.category is None]
        mislabeled = [r for r in unposted if r.category == "cogs_purchase"]
        other_unposted = [r for r in unposted if r.category not in (None, "cogs_purchase")]

        print(f"\nFound {len(all_kurasi)} total review_queue row(s) matching '%kurasi%':")
        print(f"  {len(null_category)} unposted, category IS NULL")
        print(f"  {len(mislabeled)} unposted, category='cogs_purchase' (mislabel to fix)")
        print(f"  {len(other_unposted)} unposted, some OTHER category (left untouched, unexpected — inspect manually)")
        print(f"  {len(posted)} ALREADY POSTED (left untouched — see this script's docstring)")

        if posted:
            print("\n  Already-posted rows NOT touched by this script:")
            for r in posted:
                print(
                    f"    id={r.id} category={r.category!r} amount_idr={r.amount_idr} "
                    f"posted_journal_entry_id={r.posted_journal_entry_id}"
                )

        if other_unposted:
            print("\n  WARNING: unexpected unposted category values found, NOT touched:")
            for r in other_unposted:
                print(f"    id={r.id} category={r.category!r} amount_idr={r.amount_idr}")

        to_fix_ids = [r.id for r in null_category] + [r.id for r in mislabeled]
        if not to_fix_ids:
            print("\nNothing to fix — no unposted, incorrectly-categorized Kurasi rows found.")
            return 0

        result = conn.execute(
            update(review_queue)
            .where(review_queue.c.id.in_(to_fix_ids))
            .where(review_queue.c.posted_at.is_(None))  # belt-and-suspenders re-check at UPDATE time
            .values(category="shipping_cost", labeled_at=_dt.datetime.now(_dt.timezone.utc))
        )
        print(f"\nUpdated {result.rowcount} row(s) to category='shipping_cost'.")

        # Verify directly against the database, not just trust rowcount.
        after = conn.execute(
            select(review_queue.c.id, review_queue.c.category, review_queue.c.posted_at).where(
                review_queue.c.id.in_(to_fix_ids)
            )
        ).all()
        wrong = [r for r in after if r.category != "shipping_cost" or r.posted_at is not None]
        if wrong:
            print("\nERROR: verification failed for these rows:")
            for r in wrong:
                print(f"  id={r.id} category={r.category!r} posted_at={r.posted_at}")
            raise SystemExit(1)
        print(f"Verified: all {len(after)} updated rows now show category='shipping_cost', posted_at IS NULL.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
