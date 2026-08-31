"""Structural entity + account setup helpers.

These functions create the "who/what can be posted to" scaffolding: wallet
groups, eBay accounts, and the actual postable ``accounts`` rows derived
from ``account_types``. Nothing here posts a journal entry.

Deliberately generic: nothing in this module assumes a 1:1 mapping between
eBay accounts and Payoneer wallets / BCA bridging accounts. A wallet-group
can be (and, for this business, sometimes is) shared by more than one eBay
account — see CLAUDE.md's Business model section.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ledger.chart_of_accounts import USD_ACCOUNT_TYPE_CODES
from ledger.errors import InvalidStructuralEntityError, UnknownAccountInstanceError
from ledger.schema import account_types, accounts, ebay_accounts, wallet_groups


def create_wallet_group(conn: Connection, name: str) -> int:
    result = conn.execute(wallet_groups.insert().values(name=name))
    return result.inserted_primary_key[0]


def create_ebay_account(
    conn: Connection,
    name: str,
    wallet_group_id: int,
    *,
    ebay_seller_username: str | None = None,
    is_active: bool = True,
) -> int:
    result = conn.execute(
        ebay_accounts.insert().values(
            name=name,
            wallet_group_id=wallet_group_id,
            ebay_seller_username=ebay_seller_username,
            is_active=is_active,
        )
    )
    return result.inserted_primary_key[0]


def get_account_type_id(conn: Connection, code: str) -> int:
    row = conn.execute(select(account_types.c.id).where(account_types.c.code == code)).first()
    if row is None:
        raise UnknownAccountInstanceError(
            f"No account_types entry for code {code!r}. Account types must come "
            "from ledger/chart_of_accounts.py."
        )
    return row.id


def create_account(
    conn: Connection,
    account_type_code: str,
    *,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
) -> int:
    """Create a postable ledger account row for the given account_type,
    scoped to an eBay account, a wallet-group, or neither (consolidated
    singleton) — matching that account_type's declared ``scope_kind``.
    """
    row = conn.execute(
        select(account_types.c.id, account_types.c.scope_kind).where(
            account_types.c.code == account_type_code
        )
    ).first()
    if row is None:
        raise UnknownAccountInstanceError(f"No account_types entry for code {account_type_code!r}.")
    account_type_id, scope_kind = row.id, row.scope_kind

    if scope_kind == "per_ebay_account":
        if ebay_account_id is None or wallet_group_id is not None:
            raise InvalidStructuralEntityError(
                f"{account_type_code} is scoped per_ebay_account; pass ebay_account_id "
                "only (never wallet_group_id)."
            )
    elif scope_kind == "per_wallet_group":
        if wallet_group_id is None or ebay_account_id is not None:
            raise InvalidStructuralEntityError(
                f"{account_type_code} is scoped per_wallet_group; pass wallet_group_id "
                "only (never ebay_account_id) — do not hardcode a 1:1 mapping "
                "with a specific eBay account."
            )
    elif scope_kind == "consolidated":
        if ebay_account_id is not None or wallet_group_id is not None:
            raise InvalidStructuralEntityError(
                f"{account_type_code} is consolidated; it takes no ebay_account_id "
                "or wallet_group_id."
            )
    else:  # pragma: no cover - guarded by DB CHECK constraint too
        raise InvalidStructuralEntityError(f"Unknown scope_kind {scope_kind!r}")

    currency = "USD" if account_type_code in USD_ACCOUNT_TYPE_CODES else "IDR"

    result = conn.execute(
        accounts.insert().values(
            account_type_id=account_type_id,
            ebay_account_id=ebay_account_id,
            wallet_group_id=wallet_group_id,
            currency=currency,
        )
    )
    return result.inserted_primary_key[0]


def get_account_id(
    conn: Connection,
    account_type_code: str,
    *,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
) -> int:
    """Look up an existing postable account (an ``accounts`` row)."""
    query = (
        select(accounts.c.id)
        .join(account_types, account_types.c.id == accounts.c.account_type_id)
        .where(account_types.c.code == account_type_code)
    )
    if ebay_account_id is not None:
        query = query.where(accounts.c.ebay_account_id == ebay_account_id)
    else:
        query = query.where(accounts.c.ebay_account_id.is_(None))
    if wallet_group_id is not None:
        query = query.where(accounts.c.wallet_group_id == wallet_group_id)
    else:
        query = query.where(accounts.c.wallet_group_id.is_(None))

    row = conn.execute(query).first()
    if row is None:
        raise UnknownAccountInstanceError(
            f"No accounts row for {account_type_code!r} "
            f"(ebay_account_id={ebay_account_id}, wallet_group_id={wallet_group_id}). "
            "Has this structural entity been set up yet (see ledger/seed.py)?"
        )
    return row.id
