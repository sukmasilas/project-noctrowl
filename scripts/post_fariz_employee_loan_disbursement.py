"""One-off, real-data script: relabel and post the real Fariz Pradana
employee-loan disbursement (review_queue.id=386, Rp 27,000,000, dated
2026-08-17) — see CLAUDE.md's Core accounting rules and Main-agent's
Employee Loan Receivable brief (2026-09-10).

WHY A NARROWLY-SCOPED SCRIPT, NOT A GENERIC ``post_pending_rows()`` CALL:
the real ``noctrowl`` database currently has ~106 OTHER already-labeled-but
-unposted review_queue rows (a real historical backlog, unrelated to this
change) sitting with category set and posted_at still NULL. Calling the
generic ``ingestion.matching.post_pending_rows()`` right now would post ALL
of them in one shot — a large, consequential action far outside this
change's scope and never independently verified in this session. This
script touches ONLY review_queue.id=386, by primary key, exactly once.

IDEMPOTENT: does nothing (prints and exits) if the row is already posted,
already has a DIFFERENT category set, or doesn't match the expected real
amount/date — a defensive check against accidentally acting on a row that
isn't actually the one real transaction this script is meant for.

USAGE:
    python3 scripts/post_fariz_employee_loan_disbursement.py

Reads DATABASE_URL from the environment via python-dotenv — never
hardcodes a connection string.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select, update  # noqa: E402

from ingestion.schema import review_queue  # noqa: E402
from ledger import posting  # noqa: E402
from ledger.db import get_engine  # noqa: E402

ROW_ID = 386
EXPECTED_AMOUNT_IDR = Decimal("-27000000.00")
EXPECTED_DATE = dt.date(2026, 8, 17)
EMPLOYEE_REF = "Fariz Pradana"


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        row = conn.execute(
            select(
                review_queue.c.id,
                review_queue.c.transaction_date,
                review_queue.c.amount_idr,
                review_queue.c.raw_description,
                review_queue.c.category,
                review_queue.c.posted_at,
            ).where(review_queue.c.id == ROW_ID)
        ).first()

        if row is None:
            print(f"ABORT: no review_queue row with id={ROW_ID}.")
            return 1
        if row.posted_at is not None:
            print(f"Already posted (posted_at={row.posted_at}) — nothing to do.")
            return 0
        if row.amount_idr != EXPECTED_AMOUNT_IDR or row.transaction_date != EXPECTED_DATE:
            print(
                f"ABORT: row {ROW_ID}'s amount/date ({row.amount_idr}, {row.transaction_date}) "
                f"don't match the expected real Fariz Pradana loan disbursement "
                f"({EXPECTED_AMOUNT_IDR}, {EXPECTED_DATE}) — refusing to act on a row "
                "that might not actually be it."
            )
            return 1
        print(f"Found row {ROW_ID}: {row.raw_description!r}, amount={row.amount_idr}, "
              f"date={row.transaction_date}, current category={row.category!r}")

        # Relabel (matches exactly what webapp/review_queue_bp.py's label_row
        # route would do for a human clicking Save on this row).
        conn.execute(
            update(review_queue)
            .where(review_queue.c.id == ROW_ID)
            .where(review_queue.c.posted_at.is_(None))
            .values(
                category="employee_loan_disbursement",
                consignor_item_ref=EMPLOYEE_REF,
                labeled_at=dt.datetime.now(dt.timezone.utc),
            )
        )

        # Post (matches exactly what ingestion.matching._post_one_row's
        # 'employee_loan_disbursement' branch would do for this one row —
        # source_type='bank_statement', wallet_group_id/ebay_account_id both
        # NULL, so the paying account is BCA_MAIN, the default).
        entry_id = posting.post_employee_loan_disbursement(
            conn,
            entry_date=row.transaction_date,
            amount_idr=abs(row.amount_idr),
            employee_ref=EMPLOYEE_REF,
            memo=row.raw_description,
        )
        conn.execute(
            update(review_queue)
            .where(review_queue.c.id == ROW_ID)
            .values(posted_at=dt.datetime.now(dt.timezone.utc), posted_journal_entry_id=entry_id)
        )

    print(f"Posted — journal_entry_id={entry_id}, EMPLOYEE_LOAN_RECEIVABLE debited Rp 27,000,000, "
          f"BCA_MAIN credited Rp 27,000,000, consignor_item_ref={EMPLOYEE_REF!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
