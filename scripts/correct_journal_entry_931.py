"""One-off data-repair script: correct journal_entry_id=931 / review_queue.
id=428 — a real +Rp 1,358,000 inflow on 2026-08-31 ("TRSF E-BANKING CR 3108/
FTSCY/WS95271 / 1358000.00 / DENNY WIJAYA") that was wrongly labeled
'cogs_purchase' and posted with its direction flipped (debit COGS 1,358,000 /
credit BCA_MAIN 1,358,000 — an outflow shape, for a transaction that was
actually an inflow).

User-confirmed 2026-09-30: this is a real supplier refund from Denny Wijaya
for a prior purchase — exactly the second of the two triggering scenarios
ledger.posting.post_cogs_refund (added 2026-09-29) was built for ("a
supplier refund for inventory that couldn't be delivered").

THIS IS A DELIBERATE, NARROW, ONE-TIME EXCEPTION to CLAUDE.md's "corrections
to already-posted rows are explicitly out of scope for the prototype"
(2026-08-31 decision) — same class of exception as journal_entry_id=917's
and 922/927/932's corrections. Authorized by Main-agent's brief for this
specific entry only, per the user's explicit 2026-09-30 authorization.

This entry was posted 2026-09-03, two days before the
_DIRECTIONAL_CATEGORY_SIGNS safeguard (ingestion/matching.py, shipped
2026-09-05) was built specifically to catch this exact bug class (the same
root cause as journal_entry_id=917) — it slipped through just before the fix
landed. No further prospective fix is needed here since that safeguard
already covers 'cogs_purchase' going forward.

WHAT THIS SCRIPT DOES, IN ORDER:
1. Reverses journal_entry_id=931 via ledger.posting.post_reversal_entry —
   a NEW journal entry mirroring 931's lines with debit/credit swapped
   (credit COGS 1,358,000 / debit BCA_MAIN 1,358,000), net effect zero
   against the wrong original. 931 itself is left in place, untouched and
   readable, permanently flagged as reversed via reversed_by_id.
2. Posts the CORRECT entry via ledger.posting.post_cogs_refund: debit
   BCA_MAIN 1,358,000 / credit COGS 1,358,000, dated 2026-08-31 (the real
   transaction date) — recognizing the refund as a reduction of COGS
   expense, per post_cogs_refund's own documented purpose.
3. Updates review_queue.id=428 to point at the CORRECT entry
   (posted_journal_entry_id = the new entry from step 2, not the reversed
   931) and corrects its stored category to 'cogs_refund' (from the wrong
   'cogs_purchase').

TRACEABILITY: does NOT hardcode which accounts.id values are COGS/BCA_MAIN
anywhere — always resolved live via ledger.posting's own internal
_singleton lookups (through post_reversal_entry/post_cogs_refund
themselves). The Rp 1,358,000 figure and 2026-08-31 date ARE hardcoded,
deliberately: this is a one-off correction of ONE specific, already
-identified real entry, not a general-purpose repair tool — re-derives and
CONFIRMS them by reading the real, already-posted rows first and refusing to
proceed if they don't match what this script expects, rather than trusting
the hardcoded expectation blindly.

IDEMPOTENT: safe to re-run. If journal_entry_id=931 already has
reversed_by_id set (the reversal already ran), or review_queue.id=428
already points at a DIFFERENT already-posted correct entry, this script
reports the existing state and exits without posting anything a second time.

USAGE:
    python3 scripts/correct_journal_entry_931.py

Reads DATABASE_URL from the environment via python-dotenv — never hardcodes
a connection string.
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
from ledger.posting import post_cogs_refund, post_reversal_entry  # noqa: E402
from ledger.schema import journal_entries, journal_lines  # noqa: E402

ORIGINAL_JOURNAL_ENTRY_ID = 931
REVIEW_QUEUE_ID = 428
EXPECTED_AMOUNT_IDR = Decimal("1358000.00")
TRANSACTION_DATE = _dt.date(2026, 8, 31)
CORRECTION_MEMO = (
    f"Correction of journal_entry_id={ORIGINAL_JOURNAL_ENTRY_ID} — a real +Rp 1,358,000 inflow "
    "(a supplier refund from Denny Wijaya for a prior purchase, per the user's 2026-09-30 "
    "confirmation) was wrongly labeled 'cogs_purchase' and posted with its direction flipped "
    "(debited COGS, credited BCA_MAIN, as if it were an outflow). Corrected via "
    "ledger.posting.post_cogs_refund: debit BCA_MAIN / credit COGS, recognizing the refund as a "
    "reduction of COGS expense. Found during the 2026-09-30 August Bank Reconciliation "
    "investigation — this row's flipped direction accounted for the ~Rp 2,716,000 remainder of "
    "that month's Rp 6,815,999.83 discrepancy."
)


class _StateMismatch(Exception):
    """Raised when the real database doesn't match what this one-off script
    expects to find — refuses to proceed rather than blindly correcting the
    wrong thing."""


def _load_and_verify_original_entry(conn) -> bool:
    """Returns True if entry 931 is in its ORIGINAL (uncorrected) state and
    matches the exact wrong posting this script exists to fix. Returns
    False if it's already been reversed by THIS script (idempotent re-run).
    Raises _StateMismatch for anything else unexpected.
    """
    entry = conn.execute(
        select(journal_entries.c.id, journal_entries.c.entry_date, journal_entries.c.reversed_by_id).where(
            journal_entries.c.id == ORIGINAL_JOURNAL_ENTRY_ID
        )
    ).first()
    if entry is None:
        raise _StateMismatch(f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID} does not exist.")
    if entry.entry_date != TRANSACTION_DATE:
        raise _StateMismatch(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID} has entry_date={entry.entry_date}, "
            f"expected {TRANSACTION_DATE} — refusing to proceed against unexpected data."
        )
    if entry.reversed_by_id is not None:
        print(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID} is already reversed "
            f"(reversed_by_id={entry.reversed_by_id}) — this script has already run."
        )
        return False

    cogs_id = get_account_id(conn, "COGS")
    bca_main_id = get_account_id(conn, "BCA_MAIN")
    lines = conn.execute(
        select(journal_lines.c.account_id, journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(
            journal_lines.c.journal_entry_id == ORIGINAL_JOURNAL_ENTRY_ID
        )
    ).all()
    by_account = {l.account_id: l for l in lines}
    if cogs_id not in by_account or bca_main_id not in by_account:
        raise _StateMismatch(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID} lines don't touch the expected "
            f"COGS (account_id={cogs_id}) / BCA_MAIN (account_id={bca_main_id}) accounts: {by_account!r}"
        )
    if by_account[cogs_id].debit_amount_idr != EXPECTED_AMOUNT_IDR or by_account[cogs_id].credit_amount_idr != 0:
        raise _StateMismatch(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID}'s COGS line doesn't match the "
            f"expected wrong posting (debit {EXPECTED_AMOUNT_IDR}): {by_account[cogs_id]!r}"
        )
    if by_account[bca_main_id].credit_amount_idr != EXPECTED_AMOUNT_IDR or by_account[bca_main_id].debit_amount_idr != 0:
        raise _StateMismatch(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID}'s BCA_MAIN line doesn't match the "
            f"expected wrong posting (credit {EXPECTED_AMOUNT_IDR}): {by_account[bca_main_id]!r}"
        )
    return True


def _load_and_verify_review_queue_row(conn):
    row = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.transaction_date,
            review_queue.c.amount_idr,
            review_queue.c.category,
            review_queue.c.posted_journal_entry_id,
        ).where(review_queue.c.id == REVIEW_QUEUE_ID)
    ).first()
    if row is None:
        raise _StateMismatch(f"review_queue id={REVIEW_QUEUE_ID} does not exist.")
    if row.transaction_date != TRANSACTION_DATE or row.amount_idr != EXPECTED_AMOUNT_IDR:
        raise _StateMismatch(
            f"review_queue id={REVIEW_QUEUE_ID} doesn't match expected transaction_date="
            f"{TRANSACTION_DATE}/amount_idr={EXPECTED_AMOUNT_IDR}: date={row.transaction_date}, "
            f"amount_idr={row.amount_idr}"
        )
    return row


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    with engine.begin() as conn:
        try:
            needs_reversal = _load_and_verify_original_entry(conn)
            review_row = _load_and_verify_review_queue_row(conn)
        except _StateMismatch as exc:
            print(f"ABORT: {exc}")
            return 1

        if (
            not needs_reversal
            and review_row.category == "cogs_refund"
            and review_row.posted_journal_entry_id != ORIGINAL_JOURNAL_ENTRY_ID
        ):
            print(
                f"review_queue id={REVIEW_QUEUE_ID} already points at the corrected entry "
                f"(posted_journal_entry_id={review_row.posted_journal_entry_id}, category='cogs_refund'). "
                "Nothing to do — already corrected."
            )
            return 0

        if needs_reversal:
            print(
                f"Reversing journal_entry_id={ORIGINAL_JOURNAL_ENTRY_ID} "
                "(debit COGS 1,358,000 / credit BCA_MAIN 1,358,000)..."
            )
            reversal_id = post_reversal_entry(
                conn,
                original_journal_entry_id=ORIGINAL_JOURNAL_ENTRY_ID,
                entry_date=TRANSACTION_DATE,
                memo=CORRECTION_MEMO,
            )
            print(f"  Reversal posted: journal_entry_id={reversal_id} (credit COGS 1,358,000 / debit BCA_MAIN 1,358,000)")

            print(
                "Posting the correct entry (debit BCA_MAIN 1,358,000 / credit COGS 1,358,000, "
                "category='cogs_refund', inflow)..."
            )
            correct_entry_id = post_cogs_refund(
                conn,
                entry_date=TRANSACTION_DATE,
                amount_idr=EXPECTED_AMOUNT_IDR,
                memo=CORRECTION_MEMO,
            )
            print(f"  Correct entry posted: journal_entry_id={correct_entry_id}")

            print(
                f"Updating review_queue id={REVIEW_QUEUE_ID}: category 'cogs_purchase' -> 'cogs_refund', "
                f"posted_journal_entry_id {ORIGINAL_JOURNAL_ENTRY_ID} -> {correct_entry_id}..."
            )
            conn.execute(
                update(review_queue)
                .where(review_queue.c.id == REVIEW_QUEUE_ID)
                .values(
                    category="cogs_refund",
                    match_status="matched",
                    posted_journal_entry_id=correct_entry_id,
                    sign_mismatch_reason=None,
                )
            )
            print("\nCorrection complete.")
        else:
            reversed_by_id = conn.execute(
                select(journal_entries.c.reversed_by_id).where(journal_entries.c.id == ORIGINAL_JOURNAL_ENTRY_ID)
            ).scalar_one()
            print(
                f"journal_entry_id={ORIGINAL_JOURNAL_ENTRY_ID} was already reversed (reversed_by_id="
                f"{reversed_by_id}), but review_queue id={REVIEW_QUEUE_ID} wasn't fully updated yet. "
                "This script does not know which later entry is the correct replacement in that "
                "partial state — please investigate manually rather than guessing."
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
