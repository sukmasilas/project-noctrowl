"""Seed data: chart of accounts, categories, consignor payout tiers, and
structural entities (wallet groups / eBay accounts / accounts).

``seed_account_types``, ``seed_categories`` and ``seed_consignor_payout_tiers``
are always run in full (they're global catalogs, not per-business-scale data).

``seed_full_topology`` builds the *complete* structural shape this business
actually has (3 eBay accounts across 2 wallet-groups, one shared) — used by
tests that need to prove the schema doesn't hardcode a 1:1 eBay-account-to
-Payoneer-wallet assumption.

``seed_prototype_topology`` builds only what CLAUDE.md's "Prototype scope"
section calls for: one eBay account and its wallet-group (whichever type,
shared or independent — both work structurally the same way).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from ledger.chart_of_accounts import ACCOUNT_TYPES, CATEGORIES, CONSIGNOR_PAYOUT_TIERS
from ledger.entities import create_account, create_ebay_account, create_wallet_group
from ledger.schema import account_types, categories, consignor_payout_tiers


def seed_account_types(conn: Connection) -> None:
    """Insert every row in ACCOUNT_TYPES, skipping any ``code`` that's
    already present (ON CONFLICT DO NOTHING on the table's unique ``code``
    constraint — same pattern already used by ingestion/ebay_csv.py and
    ingestion/matching.py for their own idempotent inserts).

    This guard exists specifically so this function stays safe to call on a
    freshly-created schema regardless of whether ledger.migrations.
    run_migrations() (called automatically at the end of
    ledger.schema.create_schema(), BEFORE this function ever runs) has
    already seeded a brand-new account_type row added after some earlier
    account_types were only ever seeded by hand against the real database
    (e.g. CONTRACT_LABOR, added 2026-09-05 — see ledger/migrations.py's
    account_types_contract_labor step). Without this guard, a fresh test
    schema would hit a duplicate-key IntegrityError here the moment ANY
    such migration-seeded row exists, since this function was previously a
    plain unconditional INSERT.
    """
    for code, name, statement_section, normal_balance, scope_kind, is_contra in ACCOUNT_TYPES:
        conn.execute(
            pg_insert(account_types)
            .values(
                code=code,
                name=name,
                statement_section=statement_section,
                normal_balance=normal_balance,
                scope_kind=scope_kind,
                is_contra=is_contra,
            )
            .on_conflict_do_nothing(index_elements=[account_types.c.code])
        )


def seed_categories(conn: Connection) -> None:
    for name in CATEGORIES:
        conn.execute(categories.insert().values(name=name))


def seed_consignor_payout_tiers(conn: Connection) -> None:
    for min_price, max_price, rate_percent, requires_manual_contact, display_order in CONSIGNOR_PAYOUT_TIERS:
        conn.execute(
            consignor_payout_tiers.insert().values(
                min_price_usd=Decimal(min_price),
                max_price_usd=Decimal(max_price) if max_price is not None else None,
                rate_percent=Decimal(rate_percent) if rate_percent is not None else None,
                requires_manual_contact=requires_manual_contact,
                display_order=display_order,
            )
        )


def seed_catalogs(conn: Connection) -> None:
    """Convenience: seed all three global catalogs in one call."""
    seed_account_types(conn)
    seed_categories(conn)
    seed_consignor_payout_tiers(conn)


def _consolidated_singletons(conn: Connection) -> dict[str, int]:
    codes = [code for code, *rest in ACCOUNT_TYPES if rest[3] == "consolidated"]
    return {code: create_account(conn, code) for code in codes}


def seed_prototype_topology(conn: Connection, ebay_account_name: str = "eBay Account 1") -> dict:
    """One eBay account + its own wallet-group (per CLAUDE.md's Prototype
    scope section). Returns the created ids/instances for convenience.
    """
    wallet_group_id = create_wallet_group(conn, name=f"Wallet Group for {ebay_account_name}")
    ebay_account_id = create_ebay_account(conn, name=ebay_account_name, wallet_group_id=wallet_group_id)

    ebay_wallet_id = create_account(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
    payoneer_wallet_id = create_account(conn, "PAYONEER_WALLET", wallet_group_id=wallet_group_id)
    bca_bridging_id = create_account(conn, "BCA_BRIDGING", wallet_group_id=wallet_group_id)

    singletons = _consolidated_singletons(conn)

    return {
        "wallet_group_id": wallet_group_id,
        "ebay_account_id": ebay_account_id,
        "ebay_wallet_id": ebay_wallet_id,
        "payoneer_wallet_id": payoneer_wallet_id,
        "bca_bridging_id": bca_bridging_id,
        **singletons,
    }


def seed_full_topology(conn: Connection) -> dict:
    """The business's actual full shape: 3 eBay accounts, 2 wallet-groups
    (one shared by two eBay accounts, one independent). Used by tests that
    verify the schema generalizes beyond a naive 1:1 assumption.
    """
    shared_group_id = create_wallet_group(conn, name="Wallet Group 1 (shared)")
    independent_group_id = create_wallet_group(conn, name="Wallet Group 2 (independent)")

    ebay_1 = create_ebay_account(conn, name="eBay Account 1", wallet_group_id=shared_group_id)
    ebay_2 = create_ebay_account(conn, name="eBay Account 2", wallet_group_id=shared_group_id)
    ebay_3 = create_ebay_account(conn, name="eBay Account 3", wallet_group_id=independent_group_id)

    ebay_wallets = {
        ebay_1: create_account(conn, "EBAY_WALLET", ebay_account_id=ebay_1),
        ebay_2: create_account(conn, "EBAY_WALLET", ebay_account_id=ebay_2),
        ebay_3: create_account(conn, "EBAY_WALLET", ebay_account_id=ebay_3),
    }

    payoneer_wallets = {
        shared_group_id: create_account(conn, "PAYONEER_WALLET", wallet_group_id=shared_group_id),
        independent_group_id: create_account(conn, "PAYONEER_WALLET", wallet_group_id=independent_group_id),
    }
    bca_bridging = {
        shared_group_id: create_account(conn, "BCA_BRIDGING", wallet_group_id=shared_group_id),
        independent_group_id: create_account(conn, "BCA_BRIDGING", wallet_group_id=independent_group_id),
    }

    singletons = _consolidated_singletons(conn)

    return {
        "wallet_groups": {"shared": shared_group_id, "independent": independent_group_id},
        "ebay_accounts": {"1": ebay_1, "2": ebay_2, "3": ebay_3},
        "ebay_wallets": ebay_wallets,
        "payoneer_wallets": payoneer_wallets,
        "bca_bridging": bca_bridging,
        **singletons,
    }
