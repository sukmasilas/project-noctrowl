"""One-off data-repair script: create the real, postable ``accounts`` row
(consolidated singleton, IDR) for the new CONTRACT_LABOR operating-expense
account type on the already-seeded real ``noctrowl`` database.

WHY THIS IS A SCRIPT, NOT A ``ledger.migrations`` STEP: ``ledger.migrations``
covers purely additive DDL (columns/indexes/CHECK-constraint widenings — see
that module's own docstring) plus, as of 2026-09-05, one narrow exception
(``account_types_contract_labor``) for a brand-new chart-of-accounts CATALOG
row. The actual ``accounts`` row instantiating that catalog entry as a real,
postable ledger account is normal SEED DATA territory
(``ledger.seed._consolidated_singletons``, via ``ledger.entities.
create_account`` — a plain INSERT with no ON CONFLICT guard, same as every
other consolidated singleton). Adding that INSERT to ``ledger.migrations``
too would fire automatically inside every test's fresh
``ledger.schema.create_schema()`` call (which runs ``run_migrations()``
before any test fixture seeds anything), colliding with
``create_account``'s own plain INSERT the moment a test's
``seed_prototype_topology``/``seed_full_topology`` tries to create the exact
same consolidated singleton — confirmed by a real IntegrityError while
building this fix. A plain idempotent script, run once against the real
database (same pattern already established by ``scripts/
post_opening_balance.py`` and ``scripts/post_bank_opening_balances.py``),
avoids that collision entirely.

IDEMPOTENT: looks up the account first (``ledger.entities.get_account_id``)
and does nothing if it already exists; only calls ``create_account`` when
the lookup raises ``UnknownAccountInstanceError``. The real backstop is the
DB-level partial unique index ``ux_accounts_consolidated`` (see
ledger/schema.py) for genuine concurrent-attempt safety.

USAGE:
    python3 scripts/ensure_contract_labor_account.py

Reads DATABASE_URL from the environment via python-dotenv — never
hardcodes a connection string.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy.exc import IntegrityError  # noqa: E402

from ledger.db import get_engine  # noqa: E402
from ledger.entities import create_account, get_account_id  # noqa: E402
from ledger.errors import UnknownAccountInstanceError  # noqa: E402

ACCOUNT_TYPE_CODE = "CONTRACT_LABOR"


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        try:
            existing_id = get_account_id(conn, ACCOUNT_TYPE_CODE)
        except UnknownAccountInstanceError:
            existing_id = None

        if existing_id is not None:
            print(f"Already exists — {ACCOUNT_TYPE_CODE} accounts.id={existing_id}. Nothing to do.")
            return 0

        try:
            new_id = create_account(conn, ACCOUNT_TYPE_CODE)
        except IntegrityError:
            print(
                "ABORT: a concurrent create already added this account "
                "(ux_accounts_consolidated unique index) — treating as already "
                "present, not double-creating."
            )
            return 0

    print(f"Created — {ACCOUNT_TYPE_CODE} accounts.id={new_id} (consolidated singleton, IDR).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
