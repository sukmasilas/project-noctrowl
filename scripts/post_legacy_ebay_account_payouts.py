"""One-off, idempotent, state-verifying script (2026-10-03): posts the two
real BCA Main deposits that are revenue from an old, retired eBay account
the business no longer uses (the system holds no eBay/Payoneer data for it
and never will):

  review_queue.id=1833, 2026-02-19, +Rp 48,643,836, source document
      1790345891_FEB_2026.pdf ("KR OTOMATIS LLG-MANDIRI 0938 / NUSA SATU INTI ART ...")
  review_queue.id=2030, 2026-04-23, +Rp 81,480,157, source document
      1790345891_APR_2026.pdf ("KR OTOMATIS LLG-DBS INDONESIA 0938 / Payoneer HK ...")

ACCOUNTING (decided with the user): debit BCA_MAIN, credit SALES_REVENUE at
the IDR actually received, via ledger.posting.post_legacy_ebay_account_payout.
A documented approximation — revenue is NET of the old account's eBay fees,
Payoneer fee and FX, and recognized when cash arrives, not when the sale
happened. Both caveats are written into each journal entry's memo. No USD
reference, no new account.

The category 'legacy_ebay_account_payout' is SYSTEM-ONLY: not in the Review
Queue dropdown, not assignable by auto-match. This script is the only way to
apply it.

PHASE 1 (read-only, all-or-nothing): every row must exist, be unposted (or
already posted by a prior run of this script -> reported and skipped), have
category NULL or 'revenue_settlement' (or already the legacy category if
resolved), be Master-Account-scoped (ebay_account_id and wallet_group_id both
NULL, source_type 'bank_statement', source document type
'bank_statement_master'), and match its expected date, amount and source
document file name. Any mismatch aborts the whole batch, nothing written.

PHASE 2: after the DB-identity confirmation, sets the category and posts,
setting posted_at / posted_journal_entry_id / match_status='matched' and
clearing posting_error_reason exactly as post_pending_rows does. One atomic
transaction.

USAGE: python3 scripts/post_legacy_ebay_account_payouts.py
Reads DATABASE_URL from the environment (python-dotenv); never hardcodes it.
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

from ingestion.matching import _post_legacy_ebay_account_payout  # noqa: E402
from ingestion.schema import review_queue, source_documents  # noqa: E402
from ledger.db import get_engine  # noqa: E402

CATEGORY = "legacy_ebay_account_payout"
ALLOWED_STARTING_CATEGORIES = (None, "revenue_settlement")

LEGACY_ROWS = [
    {
        "review_queue_id": 1833,
        "transaction_date": _dt.date(2026, 2, 19),
        "amount_idr": Decimal("48643836"),
        "source_file_name": "1790345891_FEB_2026.pdf",
    },
    {
        "review_queue_id": 2030,
        "transaction_date": _dt.date(2026, 4, 23),
        "amount_idr": Decimal("81480157"),
        "source_file_name": "1790345891_APR_2026.pdf",
    },
]


class _StateMismatch(Exception):
    """The real database doesn't match what this one-off script expects for
    at least one row; nothing is written for any row."""


def _load_and_verify_one(conn, spec: dict) -> dict:
    rq_id = spec["review_queue_id"]
    row = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.source_type,
            review_queue.c.transaction_date,
            review_queue.c.amount_idr,
            review_queue.c.ebay_account_id,
            review_queue.c.wallet_group_id,
            review_queue.c.category,
            review_queue.c.posted_at,
            review_queue.c.posted_journal_entry_id,
            source_documents.c.document_type.label("doc_type"),
            source_documents.c.drive_file_name,
        )
        .select_from(
            review_queue.outerjoin(source_documents, source_documents.c.id == review_queue.c.source_document_id)
        )
        .where(review_queue.c.id == rq_id)
        # Row lock held until the transaction ends (through the DB-identity
        # prompt and phase 2), so a concurrent sync/label can't change the
        # verified state. of=review_queue: Postgres can't lock the nullable
        # side of the outer join.
        .with_for_update(of=review_queue)
    ).first()
    if row is None:
        raise _StateMismatch(f"review_queue id={rq_id} does not exist.")
    if row.transaction_date != spec["transaction_date"]:
        raise _StateMismatch(
            f"review_queue id={rq_id}: transaction_date {row.transaction_date} != expected {spec['transaction_date']}."
        )
    if row.amount_idr != spec["amount_idr"]:
        raise _StateMismatch(f"review_queue id={rq_id}: amount_idr {row.amount_idr} != expected {spec['amount_idr']}.")
    if row.drive_file_name != spec["source_file_name"]:
        raise _StateMismatch(
            f"review_queue id={rq_id}: source document {row.drive_file_name!r} != expected {spec['source_file_name']!r}."
        )
    if row.doc_type != "bank_statement_master" or row.source_type != "bank_statement":
        raise _StateMismatch(
            f"review_queue id={rq_id}: not a Master Account bank-statement row "
            f"(source_type={row.source_type!r}, document_type={row.doc_type!r})."
        )
    if row.ebay_account_id is not None or row.wallet_group_id is not None:
        raise _StateMismatch(
            f"review_queue id={rq_id} is scoped (ebay_account_id={row.ebay_account_id!r}, "
            f"wallet_group_id={row.wallet_group_id!r}); expected Master-Account-scoped (both NULL)."
        )

    if row.posted_at is not None:
        if row.category == CATEGORY and row.posted_journal_entry_id is not None:
            return {**spec, "already_posted": True, "journal_entry_id": row.posted_journal_entry_id}
        raise _StateMismatch(
            f"review_queue id={rq_id} is already posted (category={row.category!r}, "
            f"posted_journal_entry_id={row.posted_journal_entry_id!r}) by something other than this script."
        )
    if row.category not in ALLOWED_STARTING_CATEGORIES:
        raise _StateMismatch(
            f"review_queue id={rq_id} has category {row.category!r}; allowed starting states are "
            f"{ALLOWED_STARTING_CATEGORIES!r}."
        )
    return {**spec, "already_posted": False, "journal_entry_id": None}


def _confirm_database_identity(engine) -> bool:
    """Same safety checkpoint as scripts/resolve_orphaned_payoneer_duplicates.py."""
    db_name = engine.url.database
    print(f"About to COMMIT real changes to database {db_name!r} on host {engine.url.host!r}.")
    try:
        confirm = input(f"Type the database name ({db_name!r}) to confirm this is the intended target: ")
    except EOFError:
        print("No confirmation received (stdin closed/empty) -- treating this as a non-match.")
        return False
    return confirm.strip() == db_name


def _apply_one(conn, spec: dict) -> int:
    """Set the category, post exactly as post_pending_rows would, and mark
    the row posted. Raises on any failure (whole transaction rolls back)."""
    rq_id = spec["review_queue_id"]
    res = conn.execute(
        update(review_queue)
        .where(
            review_queue.c.id == rq_id,
            review_queue.c.posted_at.is_(None),
            review_queue.c.category.is_(None) | (review_queue.c.category == "revenue_settlement"),
        )
        .values(category=CATEGORY)
    )
    if res.rowcount != 1:
        raise _StateMismatch(
            f"review_queue id={rq_id}: state changed since verification (already posted or relabeled); "
            "refusing to post. Nothing is committed."
        )
    row = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.source_type,
            review_queue.c.transaction_date,
            review_queue.c.amount_idr,
            review_queue.c.raw_description,
            review_queue.c.wallet_group_id,
            review_queue.c.ebay_account_id,
        ).where(review_queue.c.id == rq_id)
    ).one()
    if row.amount_idr <= 0:
        raise _StateMismatch(f"review_queue id={rq_id}: non-positive amount, refusing to post.")
    journal_entry_id = _post_legacy_ebay_account_payout(conn, row)
    res = conn.execute(
        update(review_queue)
        .where(review_queue.c.id == rq_id, review_queue.c.posted_at.is_(None))
        .values(
            match_status="matched",
            posted_at=_dt.datetime.now(_dt.timezone.utc),
            posted_journal_entry_id=journal_entry_id,
            sign_mismatch_reason=None,
            missing_reference_reason=None,
            posting_error_reason=None,
        )
    )
    if res.rowcount != 1:
        # Raising rolls back the whole transaction, including the journal entry just posted.
        raise _StateMismatch(f"review_queue id={rq_id}: final posted-marker update affected {res.rowcount} rows.")
    return journal_entry_id


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    with engine.begin() as conn:
        print("PHASE 1: verifying all rows before writing anything")
        verified = []
        for spec in LEGACY_ROWS:
            v = _load_and_verify_one(conn, spec)
            state = (
                f"already posted as journal_entry_id={v['journal_entry_id']} (skipped)"
                if v["already_posted"]
                else "verified, ready to post"
            )
            print(f"  review_queue id={v['review_queue_id']} ({v['transaction_date']}, Rp {v['amount_idr']}): {state}")
            verified.append(v)

        to_apply = [v for v in verified if not v["already_posted"]]
        if not to_apply:
            print("\nEvery row is already posted -- nothing to commit.")
            return 0

        print(f"\nDB IDENTITY CHECK -- about to post {len(to_apply)} row(s)")
        if not _confirm_database_identity(engine):
            print("\nABORT: database identity was not confirmed -- nothing has been touched.", file=sys.stderr)
            return 1

        print("\nPHASE 2: applying")
        for v in to_apply:
            je_id = _apply_one(conn, v)
            print(f"  review_queue id={v['review_queue_id']}: posted as journal_entry_id={je_id}")
        print(f"\n{len(to_apply)} row(s) posted. Committing.")

    print("\nDone. REAL commit -- safe to re-run; posted rows are reported and skipped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except _StateMismatch as exc:
        print(f"\nABORT: {exc}\n\nNothing was written -- the transaction is rolled back in full.", file=sys.stderr)
        raise SystemExit(1)
