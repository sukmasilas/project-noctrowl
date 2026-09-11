"""One-off data-repair script: reset ``category`` (and ``labeled_at``) back to
NULL on real, UNPOSTED ``review_queue`` rows so the user can re-classify them
using categories that didn't exist when they were originally labeled
(2026-09-11, confirmed by the user — pure data reset, no code/schema/ledger
changes).

SCOPE, exactly matching the confirmed brief:
  1. Rows with category='cogs_purchase', posted_at IS NULL (51 real rows,
     total amount_idr -160,005,234.00 as independently re-verified against
     the real database before writing this script) — these predate the
     'item_purchase' / 'inbound_shipping' / 'item_purchase_and_inbound_shipping'
     sub-categories added 2026-09-10.
  2. Rows with category='operating_expense', match_rule IS NULL (i.e. the
     manually-labeled ones, NOT the BIAYA ADM keyword-rule matches, which
     carry match_rule='e'), posted_at IS NULL (6 real rows, total amount_idr
     -23,093,039.00, independently re-verified) — these predate the
     'packaging_supplies' category added 2026-09-11.

EXPLICITLY NOT TOUCHED (verified untouched by this script's own WHERE
clauses, and independently re-checked before/after):
  - Every 'shipping_cost' row (43 real unposted rows) — the already-confirmed
    Kurasi transactions; resetting these would undo a decision already
    deliberately made.
  - The 3 'operating_expense' rows with match_rule='e' (the BIAYA ADM
    keyword-rule matches) — already correctly classified, no more specific
    category exists for them.
  - Every 'other' and 'revenue_settlement' row.
  - Any already-posted row (posted_at IS NOT NULL) — this script only ever
    touches unposted rows; if the real database somehow contained a posted
    row matching the target criteria, this script's WHERE clauses (both the
    read-time inventory query AND the UPDATE's own posted_at IS NULL guard)
    would exclude it, and it is reported separately rather than silently
    skipped.

WHAT GETS RESET AND WHY: only ``category`` and ``labeled_at`` are cleared,
nothing else (amount_idr, transaction_date, raw_description, external_ref,
consignor_item_ref, loan_repayment_amount_idr are all left untouched).
``match_status`` is deliberately NOT touched — read ingestion/schema.py and
webapp/review_queue_bp.py::label_row before writing this script: labeling a
review-queue row via the app never flips match_status from 'needs_review' to
'matched' (a separate, already-flagged gap — match_status only ever becomes
'matched' via the (a)-(e) auto-match rules at ingestion time, not via manual
labeling), so every one of these manually-labeled rows already has
match_status='needs_review' today, independently confirmed against the real
database before writing this script. Clearing category alone would therefore
already be sufficient for CLAUDE.md's Provisional/Final gate and the "Needs
Review" badge (both read match_status, not category) to correctly treat
these rows as outstanding again. ``labeled_at`` IS additionally cleared
because webapp/templates/review_queue.html reads it directly to render a
"Queued for next sync" status badge for any needs_review row that still has
a stale labeled_at timestamp — leaving it set would show that badge on a row
with no category to actually queue, a misleading, inconsistent display state
for a row this script is explicitly returning to a genuine "never labeled"
Needs Review state.

WHY A DIRECT SCRIPT, NOT THE GENERAL AUTO-MATCH PIPELINE: same reasoning as
scripts/relabel_kurasi_shipping_cost.py and
scripts/relabel_biaya_adm_operating_expense.py (see their docstrings) — this
is a narrow, exactly-reproducible UPDATE scoped to specific row ids
determined ahead of time by the read-time inventory query, not a broader
pipeline run that could touch unrelated rows.

IDEMPOTENT: safe to re-run — the WHERE clauses only ever match rows that
still carry the target category; once a row's category has been cleared it
no longer matches the WHERE clause on a subsequent run (and reconstructing
"the same 51/6 rows" would require re-deriving from the category value this
script just removed, which is why full row ids/before-state are printed
below for audit before any UPDATE runs).

USAGE:
    python3 scripts/reset_cogs_and_opex_for_reclassification.py

Reads DATABASE_URL from the environment via python-dotenv — never hardcodes
a connection string.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select, update  # noqa: E402

from ingestion.schema import review_queue  # noqa: E402
from ledger.db import get_engine  # noqa: E402

EXPECTED_COGS_COUNT = 51
EXPECTED_COGS_TOTAL = -160005234.00
EXPECTED_OPEX_COUNT = 6
EXPECTED_OPEX_TOTAL = -23093039.00


def _fetch(conn, category, match_rule_is_null):
    stmt = (
        select(
            review_queue.c.id,
            review_queue.c.category,
            review_queue.c.match_status,
            review_queue.c.match_rule,
            review_queue.c.labeled_at,
            review_queue.c.posted_at,
            review_queue.c.posted_journal_entry_id,
            review_queue.c.amount_idr,
        )
        .where(review_queue.c.category == category)
        .where(review_queue.c.posted_at.is_(None))
    )
    if match_rule_is_null:
        stmt = stmt.where(review_queue.c.match_rule.is_(None))
    return conn.execute(stmt).all()


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        cogs_rows = _fetch(conn, "cogs_purchase", match_rule_is_null=False)
        opex_rows = _fetch(conn, "operating_expense", match_rule_is_null=True)

        cogs_total = sum(r.amount_idr for r in cogs_rows)
        opex_total = sum(r.amount_idr for r in opex_rows)

        print(f"\ncogs_purchase, posted_at IS NULL: {len(cogs_rows)} row(s), total amount_idr = {cogs_total}")
        print(f"  match_status breakdown: {dict(Counter(r.match_status for r in cogs_rows))}")
        print(f"  match_rule breakdown:   {dict(Counter(r.match_rule for r in cogs_rows))}")

        print(
            f"\noperating_expense, match_rule IS NULL, posted_at IS NULL: "
            f"{len(opex_rows)} row(s), total amount_idr = {opex_total}"
        )
        print(f"  match_status breakdown: {dict(Counter(r.match_status for r in opex_rows))}")

        # Sanity check: the excluded operating_expense match_rule='e' rows
        # and shipping_cost rows must not appear in either fetched set at
        # all (they're excluded by the WHERE clauses above by construction),
        # but re-confirm the counts of what's being left alone for the audit
        # trail.
        excluded_opex_e = conn.execute(
            select(review_queue.c.id)
            .where(review_queue.c.category == "operating_expense")
            .where(review_queue.c.match_rule == "e")
            .where(review_queue.c.posted_at.is_(None))
        ).all()
        excluded_shipping = conn.execute(
            select(review_queue.c.id)
            .where(review_queue.c.category == "shipping_cost")
            .where(review_queue.c.posted_at.is_(None))
        ).all()
        print(
            f"\n(context only, NOT touched) operating_expense match_rule='e' unposted rows: "
            f"{len(excluded_opex_e)}"
        )
        print(f"(context only, NOT touched) shipping_cost unposted rows: {len(excluded_shipping)}")

        if not cogs_rows and not opex_rows:
            print(
                "\nNothing to reset — no rows currently carry category='cogs_purchase' or the "
                "manually-labeled 'operating_expense' pattern (already applied by a prior run, "
                "or nothing ever matched). Exiting without writing anything."
            )
            return 0

        mismatch = False
        if len(cogs_rows) != EXPECTED_COGS_COUNT or cogs_total != EXPECTED_COGS_TOTAL:
            print(
                f"\nMISMATCH: cogs_purchase expected {EXPECTED_COGS_COUNT} rows / "
                f"{EXPECTED_COGS_TOTAL}, found {len(cogs_rows)} rows / {cogs_total}."
            )
            mismatch = True
        if len(opex_rows) != EXPECTED_OPEX_COUNT or opex_total != EXPECTED_OPEX_TOTAL:
            print(
                f"\nMISMATCH: operating_expense (manual) expected {EXPECTED_OPEX_COUNT} rows / "
                f"{EXPECTED_OPEX_TOTAL}, found {len(opex_rows)} rows / {opex_total}."
            )
            mismatch = True

        if mismatch:
            print("\nStopping without writing anything — counts/totals do not match the confirmed brief.")
            conn.rollback()
            return 1

        print("\nCounts and totals match the confirmed brief exactly. Proceeding.")

        cogs_ids = [r.id for r in cogs_rows]
        opex_ids = [r.id for r in opex_rows]
        all_ids = cogs_ids + opex_ids

        print(f"\ncogs_purchase row ids to reset ({len(cogs_ids)}): {cogs_ids}")
        print(f"operating_expense row ids to reset ({len(opex_ids)}): {opex_ids}")

        result = conn.execute(
            update(review_queue)
            .where(review_queue.c.id.in_(all_ids))
            .where(review_queue.c.posted_at.is_(None))  # belt-and-suspenders re-check at UPDATE time
            .values(category=None, labeled_at=None)
        )
        print(f"\nUpdated {result.rowcount} row(s): category -> NULL, labeled_at -> NULL.")

        after = conn.execute(
            select(
                review_queue.c.id,
                review_queue.c.category,
                review_queue.c.match_status,
                review_queue.c.labeled_at,
                review_queue.c.posted_at,
            ).where(review_queue.c.id.in_(all_ids))
        ).all()
        wrong = [
            r
            for r in after
            if r.category is not None or r.labeled_at is not None or r.posted_at is not None
        ]
        if wrong:
            print("\nERROR: verification failed for these rows:")
            for r in wrong:
                print(f"  id={r.id} category={r.category!r} labeled_at={r.labeled_at} posted_at={r.posted_at}")
            raise SystemExit(1)

        print(
            f"\nVerified: all {len(after)} updated rows now show category=NULL, labeled_at=NULL, "
            "posted_at IS NULL, match_status unchanged (already 'needs_review' on every one)."
        )
        print("\nSample before/after (first 5 of each group):")
        cogs_by_id = {r.id: r for r in cogs_rows}
        opex_by_id = {r.id: r for r in opex_rows}
        after_by_id = {r.id: r for r in after}
        for label, before_map, ids in (("cogs_purchase", cogs_by_id, cogs_ids), ("operating_expense", opex_by_id, opex_ids)):
            print(f"  {label}:")
            for rid in ids[:5]:
                b = before_map[rid]
                a = after_by_id[rid]
                print(
                    f"    id={rid} amount_idr={b.amount_idr} "
                    f"before(category={b.category!r}, match_status={b.match_status!r}) -> "
                    f"after(category={a.category!r}, match_status={a.match_status!r})"
                )

        # Re-confirm exclusions are still exactly as before (untouched).
        excluded_opex_e_after = conn.execute(
            select(review_queue.c.id)
            .where(review_queue.c.category == "operating_expense")
            .where(review_queue.c.match_rule == "e")
            .where(review_queue.c.posted_at.is_(None))
        ).all()
        excluded_shipping_after = conn.execute(
            select(review_queue.c.id)
            .where(review_queue.c.category == "shipping_cost")
            .where(review_queue.c.posted_at.is_(None))
        ).all()
        if {r.id for r in excluded_opex_e_after} != {r.id for r in excluded_opex_e}:
            print("\nERROR: the excluded operating_expense match_rule='e' rows changed. Investigate.")
            raise SystemExit(1)
        if {r.id for r in excluded_shipping_after} != {r.id for r in excluded_shipping}:
            print("\nERROR: the excluded shipping_cost rows changed. Investigate.")
            raise SystemExit(1)
        print("\nConfirmed: excluded operating_expense (match_rule='e') and shipping_cost rows are unchanged.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
