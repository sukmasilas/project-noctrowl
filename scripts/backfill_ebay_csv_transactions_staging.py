"""One-off backfill: populate ``ebay_csv_transactions`` staging rows for
real, already-posted months that predate the Wallet-screen feature.

WHY THIS EXISTS (2026-09-09): ``ebay_csv_transactions`` (see
``ingestion/schema.py``) is a brand-new table added alongside the Wallet
screen — it did not exist when the real May-Aug 2026 eBay CSVs were first
ingested against the real ``noctrowl`` database, so those four months'
worth of activity (782 rows already recorded in
``ebay_csv_posted_transactions``/``ebay_expected_payouts``) has no staging
record for the Wallet screen to read yet.

This is also the concrete regression QA found and Builder fixed the same
day: ``ingestion.ebay_csv``'s four idempotency early-return paths (Order,
Refund, Other fee, Payout) used to discard the id
``_already_posted_ebay_csv_row``/the payout-existence check found and
return WITHOUT staging — so simply re-running ``process_transaction_report``
against already-posted data used to leave staging silently empty. That's
now fixed (see ``ingestion/ebay_csv.py``'s "QA BUG FIX, 2026-09-09" notes),
so re-running ingestion for these four months is now safe AND actually
populates the missing staging rows, instead of being a no-op.

SAFE, NON-DESTRUCTIVE: this only calls ``ingestion.ebay_csv.
process_transaction_report`` again for each already-ingested month — every
posting function it can reach is guarded by the SAME idempotency checks
that already protect a normal re-sync (``ebay_csv_posted_transactions``,
``ebay_expected_payouts``' unique index, ``consignment_sales``' unique
index). It cannot create a new journal entry, a new expected payout, or a
new consignment_sales row for data that's already there — it can only ever
INSERT new ``ebay_csv_transactions`` rows (idempotent on
(source_document_id, row_index) itself, so even re-running THIS script is
a no-op the second time).

USAGE:
    python3 scripts/backfill_ebay_csv_transactions_staging.py

Reads DATABASE_URL from the environment (via python-dotenv, same as
wsgi.py/scripts/run_migrations.py) — never hardcodes a connection string.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import ingestion.schema  # noqa: E402,F401 - registers ebay_csv_transactions on the shared metadata
from ingestion.ebay_csv import parse_ebay_csv_rows, process_transaction_report  # noqa: E402
from ingestion.schema import ebay_csv_transactions, source_documents  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from sqlalchemy import select  # noqa: E402

# The real sample files each already-ingested month's source_documents row
# points at (drive_file_name, confirmed by reading the real database —
# these are the SAME files this project's own real ingestion already used).
_SAMPLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sample-documents",
    "eBay account 1_ricky-game",
)


def main() -> None:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        before = len(conn.execute(select(ebay_csv_transactions.c.id)).all())
        print(f"ebay_csv_transactions rows before: {before}")

        docs = conn.execute(
            select(
                source_documents.c.id,
                source_documents.c.period_month,
                source_documents.c.ebay_account_id,
                source_documents.c.drive_file_name,
            )
            .where(source_documents.c.document_type == "ebay_sales_csv")
            .order_by(source_documents.c.period_month)
        ).all()
        if not docs:
            print("No ebay_sales_csv source_documents rows found — nothing to backfill.")
            return

        for doc in docs:
            csv_path = os.path.join(_SAMPLE_DIR, doc.drive_file_name)
            if not os.path.exists(csv_path):
                raise FileNotFoundError(
                    f"Expected sample file for source_document id={doc.id} "
                    f"({doc.period_month}, ebay_account_id={doc.ebay_account_id}) at {csv_path!r} "
                    "but it doesn't exist — refusing to guess a substitute file."
                )
            text = open(csv_path, encoding="utf-8-sig").read()
            _, rows = parse_ebay_csv_rows(text)
            result = process_transaction_report(
                conn, ebay_account_id=doc.ebay_account_id, source_document_id=doc.id, rows=rows
            )
            print(
                f"  {doc.period_month} (source_document id={doc.id}, {doc.drive_file_name}): "
                f"orders_posted={result.orders_posted} refunds_posted={result.refunds_posted} "
                f"other_fees_posted={result.other_fees_posted} payouts_recorded={result.payouts_recorded} "
                f"consignment_sales_created={result.consignment_sales_created} "
                f"review_queue_rows_created={result.review_queue_rows_created} "
                f"parse_warnings={len(result.parse_warnings)}"
            )
            # Every one of these must be 0 for an already-fully-posted month —
            # a nonzero value here would mean this script somehow posted
            # something NEW, which should never happen against
            # already-ingested data. Fail loudly rather than silently commit
            # an unexpected new posting.
            if (
                result.orders_posted
                or result.refunds_posted
                or result.other_fees_posted
                or result.payouts_recorded
                or result.consignment_sales_created
            ):
                raise RuntimeError(
                    f"Backfill for source_document id={doc.id} ({doc.period_month}) posted NEW "
                    "activity — expected 0 for an already-ingested month. Aborting without "
                    "committing; investigate before re-running."
                )

        after = len(conn.execute(select(ebay_csv_transactions.c.id)).all())
        print(f"ebay_csv_transactions rows after: {after}")


if __name__ == "__main__":
    main()
