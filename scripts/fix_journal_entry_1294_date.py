"""One-off data-repair script: backdate journal_entry_id=1294's entry_date/
period_month to match the ORIGINAL transaction it corrects (journal_entry_id
=912, the Bank Neo Com COGS+shipping-split reversal, 2026-08-05), instead of
2026-09-30 (the date the correction was actually performed).

THIS IS A DELIBERATE, NARROW, ONE-TIME EXCEPTION to CLAUDE.md's "corrections
to already-posted rows are explicitly out of scope for the prototype"
(2026-08-31 decision). Authorized by Main-agent's brief for this specific
entry only, per the user's explicit 2026-09-30 authorization.

WHY: journal_entry_id=1294 reversed journal_entry_id=912 (the Bank Neo Com
entry originally posted before the COGS+outbound-shipping-split feature
existed, per the user's explicit request "I would need to reverse posting
for Bank Neo Com related transactions"). Every other one-off correction in
this project's history (entries 947, 991/993/995) backdated its reversal to
match the ORIGINAL entry's real transaction date — this one didn't, and was
instead dated on the day the correction was performed. That inconsistency
left the reversal's Rp 4,100,000 net effect sitting in September's books
instead of August's, which is exactly what the 2026-09-30 Bank Reconciliation
investigation found: it was the single largest contributor to a real,
material August BCA Main Account discrepancy (Rp 6,815,999.83 total, this
entry accounting for ~Rp 4.1M of it).

This script ONLY updates entry_date/period_month on journal_entries id=1294.
It does not touch journal_lines, does not change any amount, and does not
re-open or re-post anything — 1294's own economic effect (reversing 912) is
already correct, only its date was wrong.

IDEMPOTENT: safe to re-run. If 1294 already has entry_date=2026-08-05, this
script reports that and exits without writing anything.

USAGE:
    python3 scripts/fix_journal_entry_1294_date.py

Reads DATABASE_URL from the environment via python-dotenv — never hardcodes
a connection string.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select, update  # noqa: E402

from ledger.db import get_engine  # noqa: E402
from ledger.schema import journal_entries  # noqa: E402

TARGET_JOURNAL_ENTRY_ID = 1294
ORIGINAL_JOURNAL_ENTRY_ID = 912
CORRECT_ENTRY_DATE = _dt.date(2026, 8, 5)
CORRECT_PERIOD_MONTH = _dt.date(2026, 8, 1)


class _StateMismatch(Exception):
    pass


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    with engine.begin() as conn:
        entry = conn.execute(
            select(
                journal_entries.c.id,
                journal_entries.c.entry_date,
                journal_entries.c.period_month,
                journal_entries.c.source_type,
            ).where(journal_entries.c.id == TARGET_JOURNAL_ENTRY_ID)
        ).first()
        if entry is None:
            print(f"ABORT: journal_entries id={TARGET_JOURNAL_ENTRY_ID} does not exist.")
            return 1

        if entry.entry_date == CORRECT_ENTRY_DATE and entry.period_month == CORRECT_PERIOD_MONTH:
            print(
                f"journal_entries id={TARGET_JOURNAL_ENTRY_ID} already has entry_date="
                f"{CORRECT_ENTRY_DATE}, period_month={CORRECT_PERIOD_MONTH}. Nothing to do."
            )
            return 0

        # Confirm this really is the reversal of 912 before touching anything,
        # rather than trusting the hardcoded ID blindly.
        original = conn.execute(
            select(journal_entries.c.reversed_by_id).where(journal_entries.c.id == ORIGINAL_JOURNAL_ENTRY_ID)
        ).first()
        if original is None or original.reversed_by_id != TARGET_JOURNAL_ENTRY_ID:
            print(
                f"ABORT: journal_entries id={ORIGINAL_JOURNAL_ENTRY_ID}'s reversed_by_id is "
                f"{original.reversed_by_id if original else 'N/A'}, expected {TARGET_JOURNAL_ENTRY_ID}. "
                "Refusing to proceed against unexpected data."
            )
            return 1

        print(
            f"journal_entries id={TARGET_JOURNAL_ENTRY_ID}: entry_date {entry.entry_date} -> "
            f"{CORRECT_ENTRY_DATE}, period_month {entry.period_month} -> {CORRECT_PERIOD_MONTH}"
        )
        conn.execute(
            update(journal_entries)
            .where(journal_entries.c.id == TARGET_JOURNAL_ENTRY_ID)
            .values(entry_date=CORRECT_ENTRY_DATE, period_month=CORRECT_PERIOD_MONTH)
        )
        print("\nCorrection complete.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
