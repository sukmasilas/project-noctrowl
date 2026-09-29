"""Reusable onboarding script: bring a new eBay account into the schema.

Creates:
  1. A new ``ebay_accounts`` row (name, ``wallet_group_id``,
     ``ebay_seller_username``, ``drive_folder_name``).
  2. That account's own ``EBAY_WALLET`` postable account (``accounts`` row,
     ``ebay_account_id`` scoped, per CLAUDE.md's Business model -- an eBay
     Wallet is genuinely 1:1 per eBay account).

Deliberately does NOT create a ``PAYONEER_WALLET``/``BCA_BRIDGING`` account
-- those are per WALLET-GROUP (CLAUDE.md, Chart of accounts), so if the new
eBay account is joining an EXISTING wallet-group (the real 2026-09-29 case:
a real second eBay account, sharing eBay Account 1's existing Payoneer
wallet-group), those already exist and must not be duplicated. If a future
onboarding is for a genuinely NEW, independent wallet-group (e.g. eBay
Account 3), create that wallet-group first (see
``ledger.entities.create_wallet_group`` / an ``ensure_*`` script following
the same pattern as this one) and pass ITS id in -- this script never
creates a wallet-group itself, precisely so it can't accidentally create a
redundant one for a sharing account.

``wallet_group_id`` is a required parameter (not hardcoded) so this same
script is reusable for eBay Account 3 later, whichever wallet-group (shared
or its own new independent one) it turns out to belong to.

IDEMPOTENT, same lookup-before-insert convention as this project's other
``ensure_*`` scripts (e.g. scripts/ensure_contract_labor_account.py,
scripts/ensure_employee_loan_receivable_account.py):
  - If an ``ebay_accounts`` row with this exact ``name`` already exists,
    reuse it (don't create a second one) -- but verify its existing
    ``wallet_group_id``/``ebay_seller_username``/``drive_folder_name``
    actually match what was asked for, and ABORT (never silently overwrite)
    if they don't, since a name collision with different field values means
    something is wrong with the inputs, not a safe re-run.
  - If that account's ``EBAY_WALLET`` accounts-row already exists, reuse it.
  - The real DB-level backstops (``ebay_accounts.name`` UNIQUE,
    ``ux_accounts_per_ebay_account`` partial unique index -- see
    ledger/schema.py) are the last line of defense for a genuine concurrent
    double-run; a resulting IntegrityError is treated as "someone else just
    created this" rather than raised out.

USAGE (the real 2026-09-29 onboarding -- eBay Account 2, sharing wallet
-group 1 with eBay Account 1):

    python3 scripts/onboard_ebay_account.py \\
        --name "eBay Account 2" \\
        --wallet-group-id 1 \\
        --ebay-seller-username ricky.garage \\
        --drive-folder-name "eBay Account - 2 (ricky-garage)"

Reads DATABASE_URL from the environment via python-dotenv -- never
hardcodes a connection string. THIS SCRIPT IS NOT RUN AGAINST THE REAL
PRODUCTION DATABASE BY Builder -- see the onboarding brief's explicit scope
boundary; Main-agent runs the QA-approved script against production
directly.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from ledger.db import get_engine  # noqa: E402
from ledger.entities import create_account, create_ebay_account, get_account_id  # noqa: E402
from ledger.errors import UnknownAccountInstanceError  # noqa: E402
from ledger.schema import ebay_accounts, wallet_groups  # noqa: E402


class OnboardingAbort(RuntimeError):
    """Raised when the requested inputs conflict with what's already in the
    database -- never silently overwritten.
    """


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help='New eBay account name, e.g. "eBay Account 2".')
    parser.add_argument(
        "--wallet-group-id",
        required=True,
        type=int,
        help="Existing wallet_groups.id this account belongs to (shared or independent -- both work "
        "identically; this script never creates a new wallet-group).",
    )
    parser.add_argument(
        "--ebay-seller-username", default=None, help="eBay Seller Hub username, e.g. ricky.garage."
    )
    parser.add_argument(
        "--drive-folder-name",
        default=None,
        help='Real Drive upload folder name for this account, e.g. "eBay Account - 2 (ricky-garage)".',
    )
    return parser.parse_args(argv)


def onboard_ebay_account(
    conn,
    *,
    name: str,
    wallet_group_id: int,
    ebay_seller_username: str | None = None,
    drive_folder_name: str | None = None,
) -> dict:
    """Idempotent core logic, callable directly from tests/scripts without
    going through argparse/get_engine. Returns
    {"ebay_account_id": int, "ebay_wallet_account_id": int,
     "ebay_account_created": bool, "ebay_wallet_created": bool}.
    """
    wg_row = conn.execute(
        select(wallet_groups.c.id, wallet_groups.c.name).where(wallet_groups.c.id == wallet_group_id)
    ).first()
    if wg_row is None:
        raise OnboardingAbort(
            f"wallet_group_id={wallet_group_id} does not exist. This script never creates a "
            "wallet-group -- create it first (see ledger.entities.create_wallet_group) if this "
            "account genuinely needs a brand-new, independent wallet-group."
        )

    existing = conn.execute(
        select(
            ebay_accounts.c.id,
            ebay_accounts.c.wallet_group_id,
            ebay_accounts.c.ebay_seller_username,
            ebay_accounts.c.drive_folder_name,
        ).where(ebay_accounts.c.name == name)
    ).first()

    ebay_account_created = False
    if existing is not None:
        mismatches = []
        if existing.wallet_group_id != wallet_group_id:
            mismatches.append(f"wallet_group_id: existing={existing.wallet_group_id!r} requested={wallet_group_id!r}")
        if (existing.ebay_seller_username or None) != (ebay_seller_username or None):
            mismatches.append(
                f"ebay_seller_username: existing={existing.ebay_seller_username!r} requested={ebay_seller_username!r}"
            )
        if (existing.drive_folder_name or None) != (drive_folder_name or None):
            mismatches.append(
                f"drive_folder_name: existing={existing.drive_folder_name!r} requested={drive_folder_name!r}"
            )
        if mismatches:
            raise OnboardingAbort(
                f"An ebay_accounts row named {name!r} already exists (id={existing.id}) but its stored "
                f"fields don't match what was requested: {'; '.join(mismatches)}. Refusing to silently "
                "overwrite -- fix the inputs or edit the existing row deliberately."
            )
        ebay_account_id = existing.id
    else:
        try:
            ebay_account_id = create_ebay_account(
                conn,
                name=name,
                wallet_group_id=wallet_group_id,
                ebay_seller_username=ebay_seller_username,
                drive_folder_name=drive_folder_name,
            )
            ebay_account_created = True
        except IntegrityError:
            # A concurrent onboarding run already created it (name UNIQUE) --
            # look it up rather than double-creating or raising.
            existing = conn.execute(
                select(ebay_accounts.c.id).where(ebay_accounts.c.name == name)
            ).first()
            if existing is None:  # pragma: no cover - defensive, shouldn't happen
                raise
            ebay_account_id = existing.id

    ebay_wallet_created = False
    try:
        ebay_wallet_account_id = get_account_id(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
    except UnknownAccountInstanceError:
        try:
            ebay_wallet_account_id = create_account(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
            ebay_wallet_created = True
        except IntegrityError:
            # ux_accounts_per_ebay_account (see ledger/schema.py) -- a
            # concurrent run already created it.
            ebay_wallet_account_id = get_account_id(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)

    return {
        "ebay_account_id": ebay_account_id,
        "ebay_wallet_account_id": ebay_wallet_account_id,
        "ebay_account_created": ebay_account_created,
        "ebay_wallet_created": ebay_wallet_created,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        try:
            outcome = onboard_ebay_account(
                conn,
                name=args.name,
                wallet_group_id=args.wallet_group_id,
                ebay_seller_username=args.ebay_seller_username,
                drive_folder_name=args.drive_folder_name,
            )
        except OnboardingAbort as exc:
            print(f"ABORT: {exc}")
            return 1

    if outcome["ebay_account_created"]:
        print(f"Created ebay_accounts.id={outcome['ebay_account_id']} (name={args.name!r}).")
    else:
        print(f"Already existed -- ebay_accounts.id={outcome['ebay_account_id']} (name={args.name!r}). Reused it.")

    if outcome["ebay_wallet_created"]:
        print(f"Created EBAY_WALLET accounts.id={outcome['ebay_wallet_account_id']} for this account.")
    else:
        print(f"EBAY_WALLET already existed -- accounts.id={outcome['ebay_wallet_account_id']}. Reused it.")

    print(
        "\nNote: PAYONEER_WALLET/BCA_BRIDGING were NOT touched -- they belong to the wallet-group "
        f"(wallet_group_id={args.wallet_group_id}), not this eBay account. If this account is joining "
        "an existing wallet-group, those accounts already exist and are correctly shared."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
