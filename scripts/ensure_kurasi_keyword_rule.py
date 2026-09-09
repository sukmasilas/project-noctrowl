"""One-off data-repair script: insert the real ``bank_keyword_rules`` row for
the new "KURASI" auto-match keyword (2026-09-09 — confirmed directly by the
user: Kurasi is a real shipping vendor, every bank line whose raw description
contains "KURASI" is a shipping cost, no exceptions) onto the already-seeded
real ``noctrowl`` database.

WHY THIS IS A SCRIPT, NOT JUST RE-RUNNING ``ingestion.seed.
seed_bank_keyword_rules``: that function is a plain INSERT loop over the
FULL ``BANK_KEYWORD_RULES`` list with no upsert/idempotency guard (see its
own docstring — "safe to call once per fresh schema"). The real database
already has the 5 pre-existing rules seeded; re-running the whole function
against it would duplicate all 5, not just add the new KURASI row. This
script inserts ONLY the new row, and only if a row with that exact keyword
doesn't already exist — the same idempotent lookup-then-insert pattern
already established by ``scripts/ensure_contract_labor_account.py`` for a
single new catalog row on an already-seeded table.

IDEMPOTENT: looks up an existing row by keyword first; does nothing if
found.

USAGE:
    python3 scripts/ensure_kurasi_keyword_rule.py

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

KEYWORD = "KURASI"
CATEGORY = "shipping_cost"
EXPENSE_ACCOUNT_TYPE_CODE = None  # not used for shipping_cost — see ingestion/matching.py's
# _post_one_row shipping_cost branch, which always posts to SHIPPING_COST directly (same
# convention already used for the 'interest_income' keyword rows).


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
