"""One-off, idempotent, state-verifying data-repair script: resolves the 10
real orphaned "Payment from eBay" review_queue rows found in production
during tonight's Jan-Apr 2026 backfill — see Main-agent's "orphaned Payoneer
eBay-payment review_queue rows" brief (2026-10-01) for the full root-cause
diagnosis, and ingestion/payoneer.py's
``_resolve_orphaned_review_queue_duplicate`` for the matching PROSPECTIVE
fix (this script only cleans up rows that were already created BEFORE that
fix shipped — the fix itself prevents any new ones from this point forward).

BACKGROUND, BRIEFLY: the same combined Jan-Apr 2026 Payoneer CSV was present
in all 4 months' Drive folders, and each period's sync pass reprocesses the
full file. A "Payment from eBay" row for a LATER month, processed during an
EARLIER month's pass (before that later month's own eBay CSV `Payout` row
existed yet), fell through to generic Needs-Review staging. Once the real
month's own pass later ran and the match succeeded, the REAL transaction
posted correctly, exactly once, via a DIFFERENT, direct posting path — but
nothing ever went back to resolve the earlier pass's now-stale orphaned row.
10 real review_queue rows are stuck in this state right now, each a genuine
duplicate of an already-posted, correct journal entry — NOT a row that
should ever be posted (doing so would double-post real revenue a second
time).

WHAT THIS SCRIPT DOES:
For each of the 10 known (review_queue_id, expected duplicate
journal_entry_id) pairs below — the pairing Main-agent independently derived
by matching each orphan's amount_usd_ref 1:1 against a real posted
journal_entry — this script:

  PHASE 1 (read-only verification of ALL 10 rows, nothing written yet):
    1. Loads the review_queue row and confirms it is EITHER:
       (a) still a fresh, untouched orphan — source_type='payoneer_csv',
           raw_description mentions "Payment from eBay", category IS NULL,
           posted_at IS NULL, match_status='needs_review'; OR
       (b) already resolved by a prior run of this exact script —
           match_status='resolved_duplicate' and duplicate_of_journal_entry_id
           already equals the expected journal_entry_id (safe to re-run).
       Any OTHER state (already posted some other way, already labeled by a
       human, resolved against a DIFFERENT entry, etc.) aborts the ENTIRE
       script before anything is written — this script never guesses which
       way to resolve an unexpected state.
    2. INDEPENDENTLY re-derives the duplicate relationship rather than
       trusting the hardcoded pairing blindly (per the brief's explicit
       instruction): confirms the expected journal_entry_id is a real,
       posted 'inter_account_transfer' entry whose PAYONEER_WALLET-side
       journal line (scoped to this row's own wallet_group_id) has an
       amount_usd_ref within $0.01 of this row's amount_usd_ref, and whose
       entry_date is within the same ±3-day tolerance
       (ingestion.matching.DATE_TOLERANCE_DAYS) the auto-match engine itself
       uses everywhere else. A mismatch here aborts the entire script too.
  Only if EVERY one of the 10 rows passes phase 1 does phase 2 run:

  PHASE 2 (apply, only for rows still in state (a) above — state (b) rows
  are no-ops, already done by a prior run):
    Marks the row match_status='resolved_duplicate',
    category='revenue_settlement', posted_at=now(),
    duplicate_of_journal_entry_id=<the confirmed real entry>, and a
    resolution_note explaining why. Deliberately does NOT set
    posted_journal_entry_id (this row never posted anything itself — see
    ingestion/schema.py's column docstring) and deliberately does NOT touch
    the real journal_entry_id in any way (it's already correct).

This NEVER posts anything new, NEVER deletes anything, and NEVER touches an
already-correct journal entry — purely a review_queue status/labeling
correction, closing the SAME gap
ingestion.payoneer._resolve_orphaned_review_queue_duplicate now closes
automatically for any FUTURE occurrence of this pattern.

IDEMPOTENT: safe to re-run in full. Rows already resolved by a prior run are
detected (state (b) above) and left untouched; the script reports them and
moves on. The whole 10-row batch runs as ONE atomic transaction — if phase 1
finds even one row in an unexpected state, NOTHING is written for any of the
10 (no partial application).

USAGE:
    python3 scripts/resolve_orphaned_payoneer_duplicates.py

Reads DATABASE_URL from the environment via python-dotenv — never hardcodes
a connection string. Prompts for an explicit database-name confirmation
before committing anything (same safety checkpoint as
scripts/backfill_jan_apr_2026.py's ``_confirm_database_identity``) — skipped
entirely if every row is already resolved (nothing to commit).
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

from ingestion.matching import DATE_TOLERANCE_DAYS  # noqa: E402
from ingestion.schema import review_queue  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from ledger.schema import account_types, accounts, journal_entries, journal_lines  # noqa: E402

AMOUNT_USD_TOLERANCE = Decimal("0.01")

# The 10 real orphaned rows, as independently diagnosed by Main-agent
# (amount_usd_ref matched 1:1 against a real posted journal_entry — see this
# module's docstring). Re-verified live against the real database by THIS
# script before anything is touched — never trusted blindly, per the brief.
ORPHANED_ROWS = [
    {"review_queue_id": 1709, "expected_duplicate_journal_entry_id": 2164},  # 2026-04-28
    {"review_queue_id": 1710, "expected_duplicate_journal_entry_id": 2165},  # 2026-04-21
    {"review_queue_id": 1711, "expected_duplicate_journal_entry_id": 2166},  # 2026-04-14
    {"review_queue_id": 1712, "expected_duplicate_journal_entry_id": 2168},  # 2026-04-07
    {"review_queue_id": 1713, "expected_duplicate_journal_entry_id": 2007},  # 2026-03-31
    {"review_queue_id": 1714, "expected_duplicate_journal_entry_id": 2008},  # 2026-03-24
    {"review_queue_id": 1715, "expected_duplicate_journal_entry_id": 2010},  # 2026-03-17
    {"review_queue_id": 1716, "expected_duplicate_journal_entry_id": 2012},  # 2026-03-10
    {"review_queue_id": 1717, "expected_duplicate_journal_entry_id": 1820},  # 2026-02-24
    {"review_queue_id": 1718, "expected_duplicate_journal_entry_id": 1822},  # 2026-02-10
]


class _StateMismatch(Exception):
    """Raised when the real database doesn't match what this one-off script
    expects to find for ANY of the 10 rows — refuses to write anything for
    any of them rather than partially applying a batch it can't fully
    verify."""


def _resolution_note(journal_entry_id: int) -> str:
    return (
        "Resolved by scripts/resolve_orphaned_payoneer_duplicates.py (2026-10-01): this row was "
        "originally staged as a generic Needs-Review line because no matching expected eBay payout "
        "existed yet when this period's combined Jan-Apr 2026 Payoneer CSV was first processed — a "
        "known cross-period re-processing gap (see CLAUDE.md / the 2026-10-01 Jan-Apr 2026 backfill "
        "incident). The real underlying transaction already posted correctly, exactly once, as "
        f"journal_entry_id={journal_entry_id} (an inter-account transfer from the eBay Wallet to the "
        "Payoneer Wallet) in a later sync pass, once that pass's own eBay Payout row was ingested. "
        "This row itself was never posted and never will be — it is a duplicate artifact only, kept "
        "here, unposted, for traceability."
    )


def _load_and_verify_one(conn, pair: dict) -> dict:
    rq_id = pair["review_queue_id"]
    expected_je_id = pair["expected_duplicate_journal_entry_id"]

    row = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.source_type,
            review_queue.c.raw_description,
            review_queue.c.transaction_date,
            review_queue.c.amount_idr,
            review_queue.c.amount_usd_ref,
            review_queue.c.wallet_group_id,
            review_queue.c.category,
            review_queue.c.match_status,
            review_queue.c.posted_at,
            review_queue.c.duplicate_of_journal_entry_id,
        ).where(review_queue.c.id == rq_id)
    ).first()
    if row is None:
        raise _StateMismatch(f"review_queue id={rq_id} does not exist.")
    if row.source_type != "payoneer_csv":
        raise _StateMismatch(f"review_queue id={rq_id} has source_type={row.source_type!r}, expected 'payoneer_csv'.")
    if "Payment from eBay" not in (row.raw_description or ""):
        raise _StateMismatch(
            f"review_queue id={rq_id} raw_description={row.raw_description!r} does not mention "
            "'Payment from eBay' — this doesn't look like the orphan this script expects."
        )
    if row.amount_usd_ref is None or row.wallet_group_id is None:
        raise _StateMismatch(
            f"review_queue id={rq_id} is missing amount_usd_ref/wallet_group_id — cannot "
            "independently re-derive its duplicate relationship."
        )

    already_resolved = (
        row.match_status == "resolved_duplicate" and row.duplicate_of_journal_entry_id == expected_je_id
    )
    fresh_orphan = row.category is None and row.posted_at is None and row.match_status == "needs_review"

    if not already_resolved and not fresh_orphan:
        raise _StateMismatch(
            f"review_queue id={rq_id} is in neither the expected FRESH-ORPHAN state (category IS NULL, "
            f"posted_at IS NULL, match_status='needs_review') nor the expected ALREADY-RESOLVED state "
            f"(match_status='resolved_duplicate', duplicate_of_journal_entry_id={expected_je_id}) — found "
            f"category={row.category!r}, match_status={row.match_status!r}, posted_at={row.posted_at!r}, "
            f"duplicate_of_journal_entry_id={row.duplicate_of_journal_entry_id!r}. Refusing to touch data "
            "that doesn't match either expected state."
        )

    # Independently re-derive the duplicate relationship — never trust the
    # hardcoded expected_je_id blindly, per the brief.
    je = conn.execute(
        select(journal_entries.c.id, journal_entries.c.entry_date, journal_entries.c.source_type).where(
            journal_entries.c.id == expected_je_id
        )
    ).first()
    if je is None:
        raise _StateMismatch(f"journal_entries id={expected_je_id} (expected duplicate target) does not exist.")
    if je.source_type != "inter_account_transfer":
        raise _StateMismatch(
            f"journal_entries id={expected_je_id} has source_type={je.source_type!r}, expected "
            "'inter_account_transfer' — this doesn't look like the real posted payout transfer."
        )
    if abs((row.transaction_date - je.entry_date).days) > DATE_TOLERANCE_DAYS:
        raise _StateMismatch(
            f"review_queue id={rq_id} (transaction_date={row.transaction_date}) is more than "
            f"{DATE_TOLERANCE_DAYS} days from journal_entries id={expected_je_id} (entry_date="
            f"{je.entry_date}) — too far apart to independently confirm as the same real event."
        )

    payoneer_line = conn.execute(
        select(journal_lines.c.amount_usd_ref)
        .join(accounts, accounts.c.id == journal_lines.c.account_id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .where(
            journal_lines.c.journal_entry_id == expected_je_id,
            account_types.c.code == "PAYONEER_WALLET",
            accounts.c.wallet_group_id == row.wallet_group_id,
        )
    ).first()
    if payoneer_line is None:
        raise _StateMismatch(
            f"journal_entries id={expected_je_id} has no PAYONEER_WALLET line scoped to "
            f"wallet_group_id={row.wallet_group_id} (review_queue id={rq_id}'s own wallet-group) — "
            "cannot independently confirm this is the same real transfer."
        )
    if payoneer_line.amount_usd_ref is None or abs(payoneer_line.amount_usd_ref - abs(row.amount_usd_ref)) > AMOUNT_USD_TOLERANCE:
        raise _StateMismatch(
            f"journal_entries id={expected_je_id}'s PAYONEER_WALLET line has amount_usd_ref="
            f"{payoneer_line.amount_usd_ref!r}, but review_queue id={rq_id} has amount_usd_ref="
            f"{row.amount_usd_ref!r} — these do not match within ${AMOUNT_USD_TOLERANCE}. Refusing to "
            "resolve this row against an unconfirmed duplicate."
        )

    return {
        "review_queue_id": rq_id,
        "journal_entry_id": expected_je_id,
        "already_resolved": already_resolved,
        "amount_usd_ref": row.amount_usd_ref,
        "transaction_date": row.transaction_date,
    }


def _confirm_database_identity(engine) -> bool:
    """Same safety checkpoint as scripts/backfill_jan_apr_2026.py's
    ``_confirm_database_identity`` — an explicit, checkable confirmation of
    which real database this script is about to commit a real change to,
    independent of anything else. Never raises: a closed/empty stdin
    (EOFError) is treated as a non-match, same as any other wrong answer.
    """
    db_name = engine.url.database
    print(f"About to COMMIT real changes to database {db_name!r} on host {engine.url.host!r}.")
    try:
        confirm = input(f"Type the database name ({db_name!r}) to confirm this is the intended target: ")
    except EOFError:
        print("No confirmation received (stdin closed/empty) -- treating this as a non-match.")
        return False
    return confirm.strip() == db_name


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    with engine.begin() as conn:
        print("=" * 100)
        print(f"PHASE 1: verifying all {len(ORPHANED_ROWS)} rows before writing anything")
        print("=" * 100)
        verified = []
        for pair in ORPHANED_ROWS:
            v = _load_and_verify_one(conn, pair)
            state = "already resolved (idempotent re-run)" if v["already_resolved"] else "fresh orphan, confirmed"
            print(
                f"  review_queue id={v['review_queue_id']}: duplicate of journal_entry_id="
                f"{v['journal_entry_id']} (${v['amount_usd_ref']}, {v['transaction_date']}) -- {state}"
            )
            verified.append(v)
        print(f"\nAll {len(verified)} rows verified successfully.\n")

        to_apply = [v for v in verified if not v["already_resolved"]]
        if not to_apply:
            print("Every row is already resolved by a prior run of this script -- nothing to commit.")
            return 0

        print("=" * 100)
        print(f"DB IDENTITY CHECK -- about to resolve {len(to_apply)} row(s)")
        print("=" * 100)
        if not _confirm_database_identity(engine):
            print("\nABORT: database identity was not confirmed -- nothing has been touched.", file=sys.stderr)
            return 1
        print()

        print("=" * 100)
        print("PHASE 2: applying the resolution")
        print("=" * 100)
        now = _dt.datetime.now(_dt.timezone.utc)
        for v in to_apply:
            conn.execute(
                update(review_queue)
                .where(review_queue.c.id == v["review_queue_id"])
                .values(
                    match_status="resolved_duplicate",
                    category="revenue_settlement",
                    posted_at=now,
                    duplicate_of_journal_entry_id=v["journal_entry_id"],
                    resolution_note=_resolution_note(v["journal_entry_id"]),
                    sign_mismatch_reason=None,
                    missing_reference_reason=None,
                    posting_error_reason=None,
                )
            )
            print(
                f"  review_queue id={v['review_queue_id']}: resolved -- duplicate_of_journal_entry_id="
                f"{v['journal_entry_id']}"
            )

        print(f"\n{len(to_apply)} row(s) resolved. Committing this transaction now.")

    print("\nDone. This was a REAL commit (no rollback) -- safe to re-run any time; already-resolved "
          "rows will be reported as such and skipped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except _StateMismatch as exc:
        print(f"\nABORT: {exc}\n\nNothing was written -- phase 1 verification failed for at least one row; "
              "this transaction is being rolled back in full.", file=sys.stderr)
        raise SystemExit(1)
