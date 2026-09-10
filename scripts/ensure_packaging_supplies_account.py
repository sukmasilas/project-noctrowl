"""One-off data-repair script: create the real, postable ``accounts`` row
(consolidated singleton, IDR) for the new PACKAGING_SUPPLIES operating
-expense account type on the already-seeded real ``noctrowl`` database.

Same exact pattern/reasoning as ``scripts/ensure_contract_labor_account.py``
(see that script's own docstring for the full explanation of why this is a
plain idempotent script rather than a ``ledger.migrations`` step): the
``account_types`` catalog row is added via ``ledger.migrations`` (see
``account_types_packaging_supplies``), but the actual postable ``accounts``
row instantiating that catalog entry is normal SEED DATA territory
(``ledger.seed._consolidated_singletons``, via ``ledger.entities.
create_account`` — a plain INSERT with no ON CONFLICT guard). Adding that
INSERT to ``ledger.migrations`` too would collide with ``create_account``'s
own plain INSERT the moment a test's ``seed_prototype_topology``/
``seed_full_topology`` tries to create the exact same consolidated
singleton.

IDEMPOTENT: looks up the account first (``ledger.entities.get_account_id``)
and does nothing if it already exists; only calls ``create_account`` when
the lookup raises ``UnknownAccountInstanceError``. The real backstop is the
DB-level partial unique index ``ux_accounts_consolidated`` (see
ledger/schema.py) for genuine concurrent-attempt safety.

USAGE:
    python3 scripts/ensure_packaging_supplies_account.py

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

ACCOUNT_TYPE_CODE = "PACKAGING_SUPPLIES"


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
