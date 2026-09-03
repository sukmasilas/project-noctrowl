"""One-off data-repair script: backfill amount_usd_ref/fx_rate_used on the
two real pre-Milestone-5-fix journal_lines rows that predate the
amount_usd_ref-threading fix.

WHY THIS EXISTS (2026-09-03): see CLAUDE.md's "Definition of done" section,
"Real pre-existing data gap found, needs a decision" note. The real local
dev database (910+ real posted journal entries) has two journal entries
that predate the Milestone 5 ``amount_usd_ref``-threading fix in
``ingestion/matching.py::_usd_reference_kwargs`` /
``ledger/posting.py::post_operating_expense`` --
``journal_entry_id`` 906 and 907, dated 2026-08-28, an OpenAI/ChatGPT
subscription fee (IDR 333,864 each) paid from wallet-group 1's Payoneer
Wallet. Their ``journal_lines`` rows were posted with ``amount_usd_ref``
NULL, which makes ``scheduling.fx_revaluation.compute_payoneer_wallet_
balance`` correctly refuse (``MissingUsdReferenceError``) to compute that
wallet-group's August 2026 unrealized FX revaluation, rather than silently
posting a wrong number.

TRACEABILITY (never a derived/invented guess -- CLAUDE.md's core rule):
both journal entries were posted from a real, still-present
``review_queue`` row (id 315 for entry 906, id 316 for entry 907; linked
via ``review_queue.posted_journal_entry_id``, a 1:1, unambiguous mapping
confirmed by the investigation this script's docstring was written from --
each review_queue row also has a distinct Payoneer ``external_ref``
Transaction ID, 287852392 and 287852382, confirming these are two
genuinely separate real subscription charges, not a duplicate). Each
review_queue row still carries its own real ``amount_usd_ref`` (-20.37 USD)
and ``amount_idr`` (-333,864.00 IDR), staged directly from the original
Payoneer CSV row by ``ingestion/payoneer.py`` at ingestion time, well
before the posting-side bug ever had a chance to drop it. This script reads
those two review_queue rows and threads their real amount_usd_ref back onto
the journal_lines rows their own posting produced -- using the EXACT SAME
``ingestion.matching._usd_reference_kwargs`` function real ingestion code
calls today (imported directly, not reimplemented), so the backfilled
values are computed identically to how a fresh, bug-free ingestion run
would have computed them the first time. No IDR/rate back-calculation
invented for this script -- ``_usd_reference_kwargs`` derives fx_rate_used
from the review_queue row's own already-real amount_idr/amount_usd_ref
pair (the same idiom used everywhere else in this codebase for this
purpose), and the resulting rate lands within a few thousandths of the
real Kurs Pajak rate actually seeded for that week (16390.0000, see
``kurs_pajak_rates`` effective_date 2026-08-24) -- consistent with it being
the real historical rate, just re-derived through IDR/USD rounding, same
as every other Payoneer-wallet-paid line already posted in this database.

SCOPE: touches ONLY journal_lines.amount_usd_ref and journal_lines.
fx_rate_used, ONLY on the specific line ids belonging to journal_entry_id
906 and 907 (found dynamically by journal_entry_id, never by a broad
UPDATE). Does not touch amount_idr, account_id, debit/credit amounts,
review_queue, or any other journal entry.

IDEMPOTENT: if a targeted line's amount_usd_ref is already set to the
expected value, this is a no-op for that line (skipped, reported). If it's
already set to something ELSE, this script refuses to touch it and exits
nonzero rather than silently overwriting -- that would need a human
decision, not a script guess.

USAGE:
    python3 scripts/backfill_missing_usd_ref.py

Reads DATABASE_URL from the environment via python-dotenv (same pattern as
scripts/run_migrations.py) -- never hardcodes a connection string.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select, update  # noqa: E402

import ingestion.schema  # noqa: E402,F401 - registers review_queue on the shared metadata
from ingestion.matching import _usd_reference_kwargs  # noqa: E402
from ingestion.schema import review_queue  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from ledger.schema import journal_lines  # noqa: E402

# Hardcoded on purpose: this is a narrow, one-off repair for these two
# specific, already-identified real rows -- not a general-purpose tool. See
# module docstring.
TARGET_JOURNAL_ENTRY_IDS = (906, 907)

_RATE_TOLERANCE = Decimal("0.0001")
_USD_TOLERANCE = Decimal("0.01")


@dataclass
class _RowNamespace:
    """Minimal stand-in with the two attributes _usd_reference_kwargs
    actually reads (amount_idr, amount_usd_ref) -- lets us call the real
    production function directly against a review_queue row without faking
    a full SQLAlchemy Row object.
    """

    amount_idr: Decimal
    amount_usd_ref: Decimal | None


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    with engine.begin() as conn:
        for entry_id in TARGET_JOURNAL_ENTRY_IDS:
            print(f"=== journal_entry_id {entry_id} ===")

            rq_rows = conn.execute(
                select(
                    review_queue.c.id,
                    review_queue.c.amount_idr,
                    review_queue.c.amount_usd_ref,
                    review_queue.c.external_ref,
                    review_queue.c.raw_description,
                ).where(review_queue.c.posted_journal_entry_id == entry_id)
            ).all()

            if len(rq_rows) != 1:
                print(
                    f"  ABORT: expected exactly one review_queue row with "
                    f"posted_journal_entry_id={entry_id}, found {len(rq_rows)}. "
                    "Refusing to guess which one is authoritative -- stopping."
                )
                return 1

            rq = rq_rows[0]
            if rq.amount_usd_ref is None:
                print(
                    f"  ABORT: review_queue row {rq.id} (the source of this journal entry) has "
                    "amount_usd_ref = NULL itself -- there is no traceable original USD amount to "
                    "backfill from. Refusing to derive one (e.g. from amount_idr / an assumed rate) -- "
                    "stopping."
                )
                return 1

            print(
                f"  Source: review_queue.id={rq.id}, external_ref={rq.external_ref!r}, "
                f"raw_description={rq.raw_description!r}"
            )
            print(f"  review_queue.amount_idr={rq.amount_idr}, review_queue.amount_usd_ref={rq.amount_usd_ref}")

            kwargs = _usd_reference_kwargs(_RowNamespace(amount_idr=rq.amount_idr, amount_usd_ref=rq.amount_usd_ref))
            expected_usd_ref: Decimal = kwargs["amount_usd_ref"]
            expected_rate: Decimal = kwargs["fx_rate_used"]
            print(
                f"  Computed via ingestion.matching._usd_reference_kwargs: "
                f"amount_usd_ref={expected_usd_ref}, fx_rate_used={expected_rate}"
            )

            lines = conn.execute(
                select(
                    journal_lines.c.id,
                    journal_lines.c.account_id,
                    journal_lines.c.debit_amount_idr,
                    journal_lines.c.credit_amount_idr,
                    journal_lines.c.amount_usd_ref,
                    journal_lines.c.fx_rate_used,
                ).where(journal_lines.c.journal_entry_id == entry_id)
            ).all()

            for line in lines:
                side = "debit" if line.debit_amount_idr > 0 else "credit"
                before = (line.amount_usd_ref, line.fx_rate_used)

                if line.amount_usd_ref is not None:
                    already_correct = abs(line.amount_usd_ref - expected_usd_ref) <= _USD_TOLERANCE and (
                        line.fx_rate_used is not None
                        and abs(line.fx_rate_used - expected_rate) <= _RATE_TOLERANCE
                    )
                    if already_correct:
                        print(
                            f"  journal_lines.id={line.id} (account_id={line.account_id}, {side}): "
                            f"already set to {before} -- no-op."
                        )
                        continue
                    print(
                        f"  ABORT: journal_lines.id={line.id} (account_id={line.account_id}, {side}) already has "
                        f"amount_usd_ref={line.amount_usd_ref}, fx_rate_used={line.fx_rate_used}, which does NOT "
                        f"match the expected value derived above (amount_usd_ref={expected_usd_ref}, "
                        f"fx_rate_used={expected_rate}). Refusing to silently overwrite -- stopping for human review."
                    )
                    return 1

                conn.execute(
                    update(journal_lines)
                    .where(journal_lines.c.id == line.id)
                    .values(amount_usd_ref=expected_usd_ref, fx_rate_used=expected_rate)
                )
                after_row = conn.execute(
                    select(journal_lines.c.amount_usd_ref, journal_lines.c.fx_rate_used).where(
                        journal_lines.c.id == line.id
                    )
                ).one()
                print(
                    f"  journal_lines.id={line.id} (account_id={line.account_id}, {side}): "
                    f"amount_usd_ref/fx_rate_used {before} -> "
                    f"({after_row.amount_usd_ref}, {after_row.fx_rate_used})"
                )
            print()

    print("Backfill complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
