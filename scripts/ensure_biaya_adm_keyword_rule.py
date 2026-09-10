"""One-off data-repair script: insert the real ``bank_keyword_rules`` row for
the new "BIAYA ADM" auto-match keyword (2026-09-10 — closes a real gap: the
real BCA Main Account statement's own admin-fee line is literally "BIAYA ADM"
/ "BIAYA ADM 0998", shorter than the existing "Biaya administrasi rekening"
keyword and never matched by it) onto the already-seeded real ``noctrowl``
database.

See ``ingestion/matching.py``'s ``_keyword_matches`` docstring for why this
keyword is safe (word-boundary-at-the-end refinement) against the textually
similar but genuinely different Bridging (Mandiri) "Biaya administrasi
rekening"/"Biaya administrasi kartu debit" lines, which must stay unmatched.

WHY THIS IS A SCRIPT, NOT JUST RE-RUNNING ``ingestion.seed.
seed_bank_keyword_rules``: same reasoning as ``scripts/
ensure_kurasi_keyword_rule.py`` (see its own docstring) — that function has
no upsert/idempotency guard, so re-running it against the already-seeded
real database would duplicate every existing row, not just add this new one.

IDEMPOTENT: looks up an existing row by keyword first; does nothing if
found.

USAGE:
    python3 scripts/ensure_biaya_adm_keyword_rule.py

Reads DATABASE_URL from the environment via python-dotenv — never hardcodes
a connection string.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select  # noqa: E402

from ingestion.schema import bank_keyword_rules  # noqa: E402
from ledger.db import get_engine  # noqa: E402

KEYWORD = "BIAYA ADM"
CATEGORY = "operating_expense"
EXPENSE_ACCOUNT_TYPE_CODE = "GENERAL_OPEX"


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        existing = conn.execute(
            select(bank_keyword_rules.c.id, bank_keyword_rules.c.category).where(
                bank_keyword_rules.c.keyword == KEYWORD
            )
        ).first()

        if existing is not None:
            print(
                f"Already exists — bank_keyword_rules.id={existing.id}, "
                f"category={existing.category!r}. Nothing to do."
            )
            return 0

        result = conn.execute(
            bank_keyword_rules.insert().values(
                keyword=KEYWORD,
                category=CATEGORY,
                expense_account_type_code=EXPENSE_ACCOUNT_TYPE_CODE,
                is_active=True,
            )
        )
        new_id = result.inserted_primary_key[0]

    print(
        f"Created — bank_keyword_rules.id={new_id}, keyword={KEYWORD!r}, "
        f"category={CATEGORY!r}, is_active=True."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
