"""One-off data-repair script: correct the ONE known real historical bad
posting — journal_entry_id=917 / review_queue.id=321 — a real +Rp 50,000
inflow on 2026-08-11 ("Transfer BI Fast / Dari / RICO 6283874841900 / DANA
...") that the account owner sent from his own personal DANA e-wallet into
wallet-group 1's Mandiri Bridging Account. It was wrongly labeled
'cogs_purchase' and posted with its direction flipped (debit COGS 50,000 /
credit BCA_BRIDGING 50,000 — an outflow shape, for a transaction that was
actually an inflow).

THIS IS A DELIBERATE, NARROW, ONE-TIME EXCEPTION to CLAUDE.md's "corrections
to already-posted rows are explicitly out of scope for the prototype"
(2026-08-31 decision — see Bank transaction classification, Definition of
done). It is authorized by Main-agent's brief for this specific entry only.
It does NOT establish a general reopen/reverse workflow, is not exposed
through the web app, and must not be treated as evidence one now exists —
see ledger.posting.post_reversal_entry's own docstring for the same caveat.

Main-agent's confirmed decisions this script implements:
1. This class of transaction (owner's personal e-wallet money passing
   through the Bridging Account) is a NEUTRAL PASS-THROUGH, categorized
   'other' — NOT an Owner's Contribution.
2. A new Other Income account (see ledger/chart_of_accounts.py, added
   alongside this fix) is the correct destination for a genuine 'other'
   -category INFLOW, mirroring how General Operating Expenses already
   serves as the catch-all for 'other'-category outflows.

WHAT THIS SCRIPT DOES, IN ORDER:
1. Reverses journal_entry_id=917 via ledger.posting.post_reversal_entry —
   a NEW journal entry mirroring 917's lines with debit/credit swapped
   (credit COGS 50,000 / debit BCA_BRIDGING 50,000), and marks 917's own
   ``reversed_by_id`` to point at it. 917 itself is left in place,
   untouched and readable, permanently flagged as reversed — never deleted
   or edited in place.
2. Posts the CORRECT entry via ledger.posting.post_income_line: debit
   BCA_BRIDGING (wallet-group 1) 50,000 / credit OTHER_INCOME 50,000,
   dated 2026-08-11 (the real transaction date), categorized as the
   'other' inflow it actually is.
3. Updates review_queue.id=321 to point at the CORRECT entry
   (posted_journal_entry_id = the new entry from step 2, not the reversed
   917) and corrects its stored category to 'other' (from the wrong
   'cogs_purchase') — so the row's own record of "what actually posted"
   stays consistent with reality, per this project's established
   posted_at/posted_journal_entry_id tracking convention
   (ingestion/matching.py's post_pending_rows). sign_mismatch_reason is
   cleared (this row never actually mismatched under 'other' — it was
   simply mislabeled before 'other' existed as a real, correct spot for
   this transaction's direction).

Both new journal entries are clearly memoed as "correction of
journal_entry_id=917" for traceability, per the brief.

TRACEABILITY: does NOT hardcode which accounts.id values are COGS/
BCA_BRIDGING/OTHER_INCOME anywhere — always resolved live via
ledger.entities.get_account_id / ledger.posting._singleton (through the
posting functions themselves), same as every other script in this project.
The Rp 50,000 figure and 2026-08-11 date ARE hardcoded, deliberately: this
is a one-off correction of ONE specific, already-identified real entry, not
a general-purpose repair tool — re-derives and CONFIRMS them by reading the
real, already-posted rows first and refusing to proceed if they don't match
what this script expects (see _load_and_verify_state below), rather than
trusting the hardcoded expectation blindly.

IDEMPOTENT: safe to re-run. If journal_entry_id=917 already has
reversed_by_id set (the reversal already ran), or review_queue.id=321
already points at a DIFFERENT already-posted correct entry, this script
reports the existing state and exits without posting anything a second
time. The real backstop for the reversal step is
ledger.posting.post_reversal_entry's own "already reversed" guard.

USAGE:
    python3 scripts/correct_journal_entry_917.py

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
from ledger.posting import post_income_line, post_reversal_entry  # noqa: E402
from ledger.schema import journal_entries, journal_lines  # noqa: E402

ORIGINAL_JOURNAL_ENTRY_ID = 917
REVIEW_QUEUE_ID = 321
WALLET_GROUP_ID = 1
EXPECTED_AMOUNT_IDR = Decimal("50000.00")
TRANSACTION_DATE = _dt.date(2026, 8, 11)
CORRECTION_MEMO = (
    f"Correction of journal_entry_id={ORIGINAL_JOURNAL_ENTRY_ID} — a real +Rp 50,000 inflow "
    "(the account owner moving his own money from a personal DANA e-wallet into the Bridging "
    "Account) was wrongly labeled 'cogs_purchase' and posted with its direction flipped. "
    "Reclassified as a neutral pass-through under the 'other' category, per Main-agent's "
    "2026-09-05 decision (see CLAUDE.md's Chart of accounts, Other Income)."
)


class _StateMismatch(Exception):
    """Raised when the real database doesn't match what this one-off script
    expects to find — refuses to proceed rather than blindly correcting the
    wrong thing."""


def _load_and_verify_original_entry(conn) -> bool:
    """Returns True if entry 917 is in its ORIGINAL (uncorrected) state and
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
    bridging_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=WALLET_GROUP_ID)
    lines = conn.execute(
        select(journal_lines.c.account_id, journal_lines.c.debit_amount_idr, journal_lines.c.credit_amount_idr).where(
            journal_lines.c.journal_entry_id == ORIGINAL_JOURNAL_ENTRY_ID
        )
    ).all()
    by_account = {l.account_id: l for l in lines}
    if cogs_id not in by_account or bridging_id not in by_account:
        raise _StateMismatch(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID} lines don't touch the expected "
            f"COGS (account_id={cogs_id}) / BCA_BRIDGING wg1 (account_id={bridging_id}) accounts: "
            f"{by_account!r}"
        )
    if by_account[cogs_id].debit_amount_idr != EXPECTED_AMOUNT_IDR or by_account[cogs_id].credit_amount_idr != 0:
        raise _StateMismatch(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID}'s COGS line doesn't match the "
            f"expected wrong posting (debit {EXPECTED_AMOUNT_IDR}): {by_account[cogs_id]!r}"
        )
    if by_account[bridging_id].credit_amount_idr != EXPECTED_AMOUNT_IDR or by_account[bridging_id].debit_amount_idr != 0:
        raise _StateMismatch(
            f"journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID}'s BCA_BRIDGING line doesn't match the "
            f"expected wrong posting (credit {EXPECTED_AMOUNT_IDR}): {by_account[bridging_id]!r}"
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

        if not needs_reversal and review_row.category == "other" and review_row.posted_journal_entry_id != ORIGINAL_JOURNAL_ENTRY_ID:
            print(
                f"review_queue id={REVIEW_QUEUE_ID} already points at the corrected entry "
                f"(posted_journal_entry_id={review_row.posted_journal_entry_id}, category='other'). "
                "Nothing to do — already corrected."
            )
            return 0

        if needs_reversal:
            print(f"Reversing journal_entry_id={ORIGINAL_JOURNAL_ENTRY_ID} (debit COGS 50,000 / credit BCA_BRIDGING 50,000)...")
            reversal_id = post_reversal_entry(
                conn,
                original_journal_entry_id=ORIGINAL_JOURNAL_ENTRY_ID,
                entry_date=TRANSACTION_DATE,
                memo=CORRECTION_MEMO,
            )
            print(f"  Reversal posted: journal_entry_id={reversal_id} (credit COGS 50,000 / debit BCA_BRIDGING 50,000)")

            print(
                "Posting the correct entry (debit BCA_BRIDGING 50,000 / credit OTHER_INCOME 50,000, "
                "category='other', inflow)..."
            )
            correct_entry_id = post_income_line(
                conn,
                entry_date=TRANSACTION_DATE,
                income_account_type_code="OTHER_INCOME",
                amount_idr=EXPECTED_AMOUNT_IDR,
                paying_account_type_code="BCA_BRIDGING",
                paying_wallet_group_id=WALLET_GROUP_ID,
                memo=CORRECTION_MEMO,
            )
            print(f"  Correct entry posted: journal_entry_id={correct_entry_id}")

            print(f"Updating review_queue id={REVIEW_QUEUE_ID}: category 'cogs_purchase' -> 'other', "
                  f"posted_journal_entry_id {ORIGINAL_JOURNAL_ENTRY_ID} -> {correct_entry_id}...")
            conn.execute(
                update(review_queue)
                .where(review_queue.c.id == REVIEW_QUEUE_ID)
                .values(
                    category="other",
                    match_status="matched",
                    posted_journal_entry_id=correct_entry_id,
                    sign_mismatch_reason=None,
                )
            )
            print("\nCorrection complete.")
        else:
            # needs_reversal was False (already reversed by a prior run of
            # this script) but review_queue wasn't updated yet for some
            # reason (e.g. a prior run crashed between the two steps) —
            # re-point it at the entry 917's reversed_by_id now points to.
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
