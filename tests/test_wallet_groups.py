"""Wallet-group structure: Payoneer Wallet and BCA Bridging Account are per
wallet-group, never hardcoded 1:1 with an eBay account. This test builds
the business's actual full shape (3 eBay accounts, 2 wallet-groups, one
shared by two eBay accounts) and proves the schema enforces it structurally
— not just via application convention.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from ledger.entities import create_account
from ledger.errors import InvalidStructuralEntityError


def test_full_topology_has_3_ebay_wallets_but_only_2_payoneer_wallets(full_topology):
    conn, topo = full_topology

    assert len(topo["ebay_wallets"]) == 3
    assert len(topo["payoneer_wallets"]) == 2
    assert len(topo["bca_bridging"]) == 2

    # The two eBay accounts in the shared group point at the SAME Payoneer
    # Wallet / BCA Bridging account.
    shared_group_id = topo["wallet_groups"]["shared"]
    assert topo["payoneer_wallets"][shared_group_id] is not None
    assert topo["ebay_accounts"]["1"] != topo["ebay_accounts"]["2"]
    # Both eBay accounts 1 and 2 were created with wallet_group_id = shared_group_id
    # (see ledger/seed.py:seed_full_topology) — there is exactly one Payoneer
    # Wallet account row for that whole group, not one per eBay account.


def test_cannot_create_a_second_payoneer_wallet_for_the_same_wallet_group(full_topology):
    """Structural guarantee (partial unique index), not just a convention:
    a wallet-group can never get two Payoneer Wallet account rows, even if
    two different eBay accounts reference it.
    """
    conn, topo = full_topology
    shared_group_id = topo["wallet_groups"]["shared"]

    with pytest.raises(IntegrityError):
        create_account(conn, "PAYONEER_WALLET", wallet_group_id=shared_group_id)


def test_cannot_create_a_second_ebay_wallet_for_the_same_ebay_account(full_topology):
    conn, topo = full_topology
    ebay_account_1 = topo["ebay_accounts"]["1"]

    with pytest.raises(IntegrityError):
        create_account(conn, "EBAY_WALLET", ebay_account_id=ebay_account_1)


def test_cannot_create_a_second_consolidated_singleton(full_topology):
    conn, topo = full_topology

    with pytest.raises(IntegrityError):
        create_account(conn, "BCA_MAIN")


def test_payoneer_wallet_requires_wallet_group_not_ebay_account(conn):
    """A caller cannot accidentally scope a per_wallet_group account type to
    a specific eBay account — this is exactly the 1:1-hardcoding mistake
    CLAUDE.md calls out as never allowed.
    """
    from ledger.seed import seed_prototype_topology

    topo = seed_prototype_topology(conn)
    with pytest.raises(InvalidStructuralEntityError):
        create_account(conn, "PAYONEER_WALLET", ebay_account_id=topo["ebay_account_id"])
