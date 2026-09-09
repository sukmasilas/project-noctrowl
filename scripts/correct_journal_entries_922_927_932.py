"""One-off data-repair script: correct THREE known real historical bad
postings — journal_entry_id=922 / review_queue.id=345 (Rp 948,000),
journal_entry_id=927 / review_queue.id=371 (Rp 1,473,000), and
journal_entry_id=932 / review_queue.id=419 (Rp 1,370,000), total
Rp 3,791,000 — all three real "KURASI" bank-transfer lines (a real shipping
vendor, confirmed by the user 2026-09-09 — see CLAUDE.md's Chart of accounts
/ "Shipping cost data source" note) that were posted to COGS (debit COGS /
credit BCA_MAIN) before Kurasi's identity as a shipping vendor was confirmed.
They should have posted to Shipping Cost instead.

THIS IS THE SAME DELIBERATE, NARROW, ONE-TIME EXCEPTION to CLAUDE.md's
"corrections to already-posted rows are explicitly out of scope for the
prototype" (2026-08-31 decision — see Bank transaction classification,
Definition of done) already used for journal_entry_id=917 (see
scripts/correct_journal_entry_917.py, the established pattern this script
follows exactly). It does NOT establish a general reopen/reverse workflow,
is not exposed through the web app, and must not be treated as evidence one
now exists — see ledger.posting.post_reversal_entry's own docstring for the
same caveat. Authorized by Main-agent's brief for these three specific
entries only (QA already independently confirmed, prior to this script
being written, that all three rows/entries are genuinely untouched by the
separate recent Kurasi-automation work — still category='cogs_purchase' on
the review_queue rows, journal entries unchanged).

WHAT THIS SCRIPT DOES, FOR EACH OF THE THREE CORRECTIONS, IN ORDER:
1. Reverses the original journal_entry_id via ledger.posting.
   post_reversal_entry — a NEW journal entry mirroring the original's lines
   with debit/credit swapped (credit COGS / debit BCA_MAIN, same amount),
   and marks the original's own ``reversed_by_id`` to point at it. The
   original entry itself is left in place, untouched and readable,
   permanently flagged as reversed — never deleted or edited in place.
2. Posts the CORRECT entry via ledger.posting.post_shipping_cost_purchase:
   debit SHIPPING_COST / credit BCA_MAIN, same date and amount as the
   original.
3. Updates the corresponding review_queue row to point at the CORRECT entry
   (posted_journal_entry_id = the new entry from step 2, not the reversed
   original) and corrects its stored category to 'shipping_cost' (from the
   wrong 'cogs_purchase') — so the row's own record of "what actually
   posted" stays consistent with reality, per this project's established
   posted_at/posted_journal_entry_id tracking convention
   (ingestion/matching.py's post_pending_rows).

Both new journal entries per correction are clearly memoed as "correction of
journal_entry_id=<N>" for traceability, per the brief.

TRACEABILITY: does NOT hardcode which accounts.id values are COGS/
BCA_MAIN/SHIPPING_COST anywhere — always resolved live via
ledger.entities.get_account_id / ledger.posting's own singleton lookups
(through the posting functions themselves), same as every other script in
this project. The Rp amounts/dates/review_queue ids ARE hardcoded,
deliberately: this is a one-off correction of THREE specific,
already-identified real entries, not a general-purpose repair tool —
re-derives and CONFIRMS them by reading the real, already-posted rows first
and refusing to proceed if they don't match what this script expects (see
_load_and_verify_original_entry / _load_and_verify_review_queue_row below),
rather than trusting the hardcoded expectations blindly.

IDEMPOTENT: safe to re-run, per correction independently. If a given
original journal_entry_id already has reversed_by_id set (its reversal
already ran), or its review_queue row already points at a DIFFERENT
already-posted correct entry with category='shipping_cost', this script
reports the existing state for that correction and moves on without posting
anything a second time. The real backstop for the reversal step is
ledger.posting.post_reversal_entry's own "already reversed" guard.

USAGE:
    python3 scripts/correct_journal_entries_922_927_932.py

Reads DATABASE_URL from the environment via python-dotenv — never
hardcodes a connection string.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select, update  # noqa: E402

from ingestion.schema import review_queue  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from ledger.entities import get_account_id  # noqa: E402
from ledger.posting import post_reversal_entry, post_shipping_cost_purchase  # noqa: E402
from ledger.schema import journal_entries, journal_lines  # noqa: E402


class _StateMismatch(Exception):
    """Raised when the real database doesn't match what this one-off script
    expects to find — refuses to proceed rather than blindly correcting the
    wrong thing."""


CORRECTIONS = [
    {
        "journal_entry_id": 922,
        "review_queue_id": 345,
        "amount_idr": Decimal("948000.00"),
        "date": _dt.date(2026, 8, 5),
    },
    {
        "journal_entry_id": 927,
        "review_queue_id": 371,
        "amount_idr": Decimal("1473000.00"),
        "date": _dt.date(2026, 8, 11),
    },
    {
        "journal_entry_id": 932,
        "review_queue_id": 419,
        "amount_idr": Decimal("1370000.00"),
        "date": _dt.date(2026, 8, 28),
    },
]


def _memo(journal_entry_id: int) -> str:
    return (
        f"Correction of journal_entry_id={journal_entry_id} — a real KURASI shipping-vendor bank "
        "line (confirmed by the user 2026-09-09 as a real shipping cost, no exceptions — see "
        "CLAUDE.md's Chart of accounts / 'Shipping cost data source' note) was wrongly posted to "
        "COGS before Kurasi's identity was confirmed. Reclassified to Shipping Cost, per "
        "Main-agent's 2026-09-09 one-off correction brief."
    )


def _load_and_verify_original_entry(conn, correction: dict) -> bool:
    """Returns True if the original entry is in its ORIGINAL (uncorrected)
    state and matches the exact wrong posting this script exists to fix.
    Returns False if it's already been reversed by THIS script (idempotent
    re-run). Raises _StateMismatch for anything else unexpected.
    """
    je_id = correction["journal_entry_id"]
    expected_amount = correction["amount_idr"]
    expected_date = correction["date"]

    entry = conn.execute(
        select(journal_entries.c.id, journal_entries.c.entry_date, journal_entries.c.reversed_by_id).where(
            journal_entries.c.id == je_id
        )
    ).first()
    if entry is None:
        raise _StateMismatch(f"journal_entries id={je_id} does not exist.")
    if entry.entry_date != expected_date:
        raise _StateMismatch(
            f"journal_entries id={je_id} has entry_date={entry.entry_date}, "
            f"expected {expected_date} — refusing to proceed against unexpected data."
        )
    if entry.reversed_by_id is not None:
        print(
            f"journal_entries id={je_id} is already reversed "
            f"(reversed_by_id={entry.reversed_by_id}) — this script has already run for this entry."
        )
        return False

    cogs_id = get_account_id(conn, "COGS")
    bca_main_id = get_account_id(conn, "BCA_MAIN")
    lines = conn.execute(
        select(journal_lines.c.account_id, journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(
            journal_lines.c.journal_entry_id == je_id
        )
    ).all()
    by_account = {l.account_id: l for l in lines}
    if cogs_id not in by_account or bca_main_id not in by_account:
        raise _StateMismatch(
            f"journal_entries id={je_id} lines don't touch the expected COGS (account_id={cogs_id}) "
            f"/ BCA_MAIN (account_id={bca_main_id}) accounts: {by_account!r}"
        )
    if by_account[cogs_id].debit_amount_idr != expected_amount or by_account[cogs_id].credit_amount_idr != 0:
        raise _StateMismatch(
            f"journal_entries id={je_id}'s COGS line doesn't match the expected wrong posting "
            f"(debit {expected_amount}): {by_account[cogs_id]!r}"
        )
    if by_account[bca_main_id].credit_amount_idr != expected_amount or by_account[bca_main_id].debit_amount_idr != 0:
        raise _StateMismatch(
            f"journal_entries id={je_id}'s BCA_MAIN line doesn't match the expected wrong posting "
            f"(credit {expected_amount}): {by_account[bca_main_id]!r}"
        )
    return True


def _load_and_verify_review_queue_row(conn, correction: dict):
    rq_id = correction["review_queue_id"]
    expected_amount = correction["amount_idr"]
    expected_date = correction["date"]

    row = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.transaction_date,
            review_queue.c.amount_idr,
            review_queue.c.category,
            review_queue.c.posted_journal_entry_id,
        ).where(review_queue.c.id == rq_id)
    ).first()
    if row is None:
        raise _StateMismatch(f"review_queue id={rq_id} does not exist.")
    if row.transaction_date != expected_date or abs(row.amount_idr) != expected_amount:
        raise _StateMismatch(
            f"review_queue id={rq_id} doesn't match expected transaction_date={expected_date}/"
            f"amount_idr magnitude={expected_amount}: date={row.transaction_date}, "
            f"amount_idr={row.amount_idr}"
        )
    return row


def _run_one_correction(conn, correction: dict) -> dict:
    je_id = correction["journal_entry_id"]
    rq_id = correction["review_queue_id"]
    amount = correction["amount_idr"]
    date = correction["date"]

    needs_reversal = _load_and_verify_original_entry(conn, correction)
    review_row = _load_and_verify_review_queue_row(conn, correction)

    if (
        not needs_reversal
        and review_row.category == "shipping_cost"
        and review_row.posted_journal_entry_id != je_id
    ):
        print(
            f"review_queue id={rq_id} already points at the corrected entry "
            f"(posted_journal_entry_id={review_row.posted_journal_entry_id}, category='shipping_cost'). "
            "Nothing to do — already corrected."
        )
        return {
            "review_queue_id": rq_id,
            "original_journal_entry_id": je_id,
            "already_corrected": True,
            "correct_journal_entry_id": review_row.posted_journal_entry_id,
            "amount_idr": amount,
        }

    if not needs_reversal:
        # Already reversed by a prior run of this script, but review_queue
        # wasn't fully updated yet for some reason (e.g. a prior run crashed
        # between the two steps). Investigate manually rather than guessing
        # which later entry is the correct replacement.
        reversed_by_id = conn.execute(
            select(journal_entries.c.reversed_by_id).where(journal_entries.c.id == je_id)
        ).scalar_one()
        raise _StateMismatch(
            f"journal_entry_id={je_id} was already reversed (reversed_by_id={reversed_by_id}), but "
            f"review_queue id={rq_id} wasn't fully updated yet. This script does not know which "
            "later entry is the correct replacement in that partial state — please investigate "
            "manually rather than guessing."
        )

    memo = _memo(je_id)

    print(f"Reversing journal_entry_id={je_id} (debit COGS {amount} / credit BCA_MAIN {amount})...")
    reversal_id = post_reversal_entry(conn, original_journal_entry_id=je_id, entry_date=date, memo=memo)
    print(f"  Reversal posted: journal_entry_id={reversal_id} (credit COGS {amount} / debit BCA_MAIN {amount})")

    print(f"Posting the correct entry (debit SHIPPING_COST {amount} / credit BCA_MAIN {amount})...")
    correct_entry_id = post_shipping_cost_purchase(conn, entry_date=date, amount_idr=amount, memo=memo)
    print(f"  Correct entry posted: journal_entry_id={correct_entry_id}")

    print(
        f"Updating review_queue id={rq_id}: category 'cogs_purchase' -> 'shipping_cost', "
        f"posted_journal_entry_id {je_id} -> {correct_entry_id}..."
    )
    conn.execute(
        update(review_queue)
        .where(review_queue.c.id == rq_id)
        .values(
            category="shipping_cost",
            match_status="matched",
            posted_journal_entry_id=correct_entry_id,
            sign_mismatch_reason=None,
        )
    )

    return {
        "review_queue_id": rq_id,
        "original_journal_entry_id": je_id,
        "already_corrected": False,
        "reversal_journal_entry_id": reversal_id,
        "correct_journal_entry_id": correct_entry_id,
        "amount_idr": amount,
    }


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    results = []
    with engine.begin() as conn:
        for correction in CORRECTIONS:
            print(f"--- Correcting journal_entry_id={correction['journal_entry_id']} "
                  f"(review_queue.id={correction['review_queue_id']}) ---")
            try:
                results.append(_run_one_correction(conn, correction))
            except _StateMismatch as exc:
                print(f"ABORT: {exc}")
                return 1
            print()

    print("=== Summary ===")
    total = Decimal("0")
    for r in results:
        total += r["amount_idr"]
        if r.get("already_corrected"):
            print(
                f"review_queue.id={r['review_queue_id']}: original journal_entry_id="
                f"{r['original_journal_entry_id']} -> already corrected, "
                f"correct journal_entry_id={r['correct_journal_entry_id']}, "
                f"amount=Rp {r['amount_idr']}"
            )
        else:
            print(
                f"review_queue.id={r['review_queue_id']}: original journal_entry_id="
                f"{r['original_journal_entry_id']} -> reversal journal_entry_id="
                f"{r['reversal_journal_entry_id']} -> correct journal_entry_id="
                f"{r['correct_journal_entry_id']}, amount=Rp {r['amount_idr']}"
            )
    print(f"Total corrected: Rp {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
