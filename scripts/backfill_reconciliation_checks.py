"""One-off backfill: run the new reconciliation-gap-detection check
(``ingestion.reconciliation.check_account_reconciliation``) against the real,
already-ingested BCA Main Account and Mandiri Bridging Account statements
for all 4 real months (May-Aug 2026), against the real ``noctrowl`` database.

WHY THIS EXISTS (2026-09): the reconciliation-gap-detection feature is now
wired into ``ingestion.sync.sync_bank_statement`` going forward, but the 4
real months in this database were ingested BEFORE this feature existed, so
no ``reconciliation_checks`` row exists yet for any of them. This script
closes that gap for the already-ingested history, the same "backfill past
periods once a new check is added" pattern as
``scripts/post_bank_opening_balances.py``.

TRACEABILITY: neither the expected-opening/closing figures nor the account
mapping are hardcoded here beyond the same small, explicit document_type ->
account_type_code mapping already used by ``ingestion.sync`` itself
(``_RECONCILIATION_ACCOUNT_CODE_BY_DOCUMENT_TYPE``) — every number is
re-derived at run time by calling the real, already-tested parsers directly
against the real sample PDFs, so a parser regression fails loudly here
rather than silently backfilling a wrong number.

Idempotent: safe to re-run — ``check_account_reconciliation`` upserts by
(account_id, period_month), so re-running this script just re-computes and
overwrites each row with the current (identical, assuming nothing else in
the ledger changed) figures.

USAGE:
    python3 scripts/backfill_reconciliation_checks.py
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from ingestion import bank_statement, mandiri_statement  # noqa: E402
from ingestion.reconciliation import check_account_reconciliation  # noqa: E402
from ingestion.schema import source_documents  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from ledger.entities import get_account_id  # noqa: E402
from ledger.schema import ebay_accounts, wallet_groups  # noqa: E402
from sqlalchemy import select  # noqa: E402

SAMPLES_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample-documents")
BCA_MAIN_DIR = os.path.join(SAMPLES_ROOT, "Main Account (BCA)")
BRIDGING_DIR = os.path.join(SAMPLES_ROOT, "Bridging Account (Mandiri)")

BCA_MAIN_FILES = {
    _dt.date(2026, 5, 1): "1790345891_MAY_2026.pdf",
    _dt.date(2026, 6, 1): "1790345891_JUN_2026.pdf",
    _dt.date(2026, 7, 1): "1790345891_JUL_2026.pdf",
    _dt.date(2026, 8, 1): "1790345891_AUG_2026.pdf",
}
BRIDGING_FILES = {
    _dt.date(2026, 5, 1): "e-Statement_XXXXXXXXX7498_01 Mei 2026-31 Mei 2026_unlocked.pdf",
    _dt.date(2026, 6, 1): "e-Statement_XXXXXXXXX7498_01 Jun 2026-30 Jun 2026_unlocked.pdf",
    _dt.date(2026, 7, 1): "e-Statement_XXXXXXXXX7498_01 Jul 2026-31 Jul 2026-unlocked.pdf",
    _dt.date(2026, 8, 1): "e-Statement_XXXXXXXXX7498_01 Agu 2026-31 Agu 2026_unlocked.pdf",
}


def _source_document_id(conn, *, document_type, period_month, wallet_group_id=None):
    query = select(source_documents.c.id).where(
        source_documents.c.document_type == document_type,
        source_documents.c.period_month == period_month,
    )
    query = query.where(
        source_documents.c.wallet_group_id == wallet_group_id
        if wallet_group_id is not None
        else source_documents.c.wallet_group_id.is_(None)
    )
    row = conn.execute(query).first()
    return row.id if row is not None else None


def main() -> None:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.connect() as conn:
        wg_row = conn.execute(select(ebay_accounts.c.wallet_group_id).limit(1)).first()
        if wg_row is None:
            print("No eBay accounts configured yet — nothing to backfill.")
            return
        wallet_group_id = wg_row.wallet_group_id
        wg_name = conn.execute(
            select(wallet_groups.c.name).where(wallet_groups.c.id == wallet_group_id)
        ).scalar()
        print(f"Using wallet_group_id={wallet_group_id} ({wg_name!r})\n")

        bca_main_id = get_account_id(conn, "BCA_MAIN")
        bridging_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=wallet_group_id)

        print("=== BCA Main Account (consolidated) ===")
        for period_month, filename in sorted(BCA_MAIN_FILES.items()):
            path = os.path.join(BCA_MAIN_DIR, filename)
            parsed = bank_statement.parse_bca_statement(path)
            assert parsed.opening_balance_idr is not None and parsed.closing_balance_idr is not None, (
                f"{filename}: parser did not extract both opening and closing balance — refusing to backfill "
                "a partial/guessed figure."
            )
            src_id = _source_document_id(conn, document_type="bank_statement_master", period_month=period_month)
            outcome = check_account_reconciliation(
                conn,
                account_id=bca_main_id,
                period_month=period_month,
                statement_opening_idr=parsed.opening_balance_idr,
                statement_closing_idr=parsed.closing_balance_idr,
                source_document_id=src_id,
            )
            status = "MATERIAL DISCREPANCY" if outcome.is_material else "clean"
            print(
                f"  {period_month.isoformat()}: expected open={outcome.expected_opening_idr} "
                f"actual open={outcome.actual_opening_idr} (Δ{outcome.opening_discrepancy_idr}) | "
                f"expected close={outcome.expected_closing_idr} actual close={outcome.actual_closing_idr} "
                f"(Δ{outcome.closing_discrepancy_idr}) -> {status}"
            )

        print("\n=== Mandiri Bridging Account (wallet-group) ===")
        for period_month, filename in sorted(BRIDGING_FILES.items()):
            path = os.path.join(BRIDGING_DIR, filename)
            parsed = mandiri_statement.parse_mandiri_statement(path)
            assert parsed.opening_balance_idr is not None and parsed.closing_balance_idr is not None, (
                f"{filename}: parser did not extract both opening and closing balance — refusing to backfill "
                "a partial/guessed figure."
            )
            src_id = _source_document_id(
                conn, document_type="bank_statement_wallet_group", period_month=period_month, wallet_group_id=wallet_group_id
            )
            outcome = check_account_reconciliation(
                conn,
                account_id=bridging_id,
                period_month=period_month,
                statement_opening_idr=parsed.opening_balance_idr,
                statement_closing_idr=parsed.closing_balance_idr,
                source_document_id=src_id,
            )
            status = "MATERIAL DISCREPANCY" if outcome.is_material else "clean"
            print(
                f"  {period_month.isoformat()}: expected open={outcome.expected_opening_idr} "
                f"actual open={outcome.actual_opening_idr} (Δ{outcome.opening_discrepancy_idr}) | "
                f"expected close={outcome.expected_closing_idr} actual close={outcome.actual_closing_idr} "
                f"(Δ{outcome.closing_discrepancy_idr}) -> {status}"
            )

        conn.commit()

    print("\nDone.")


if __name__ == "__main__":
    main()
