"""Tests for scripts/onboard_ebay_account.py's core logic
(``onboard_ebay_account``) — the real, first-ever exercise of onboarding a
genuinely SECOND eBay account into an EXISTING wallet-group, per CLAUDE.md's
Business model/Prototype scope sections.

Exercises the exact real 2026-09-29 scenario: eBay Account 1 already exists
(``prototype`` fixture — its own wallet-group, EBAY_WALLET, PAYONEER_WALLET,
BCA_BRIDGING already seeded), and eBay Account 2 joins that SAME
wallet-group, sharing its existing Payoneer Wallet / BCA Bridging Account
rather than getting its own.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from ledger.entities import get_account_id
from ledger.errors import UnknownAccountInstanceError
from ledger.schema import account_types, accounts, ebay_accounts
from scripts.onboard_ebay_account import OnboardingAbort, onboard_ebay_account


def test_onboard_creates_ebay_account_and_only_its_own_ebay_wallet(prototype):
    conn, topo = prototype
    wallet_group_id = topo["wallet_group_id"]

    outcome = onboard_ebay_account(
        conn,
        name="eBay Account 2",
        wallet_group_id=wallet_group_id,
        ebay_seller_username="ricky.garage",
        drive_folder_name="eBay Account - 2 (ricky-garage)",
    )

    assert outcome["ebay_account_created"] is True
    assert outcome["ebay_wallet_created"] is True

    row = conn.execute(
        select(
            ebay_accounts.c.name,
            ebay_accounts.c.wallet_group_id,
            ebay_accounts.c.ebay_seller_username,
            ebay_accounts.c.drive_folder_name,
            ebay_accounts.c.is_active,
        ).where(ebay_accounts.c.id == outcome["ebay_account_id"])
    ).first()
    assert row.name == "eBay Account 2"
    assert row.wallet_group_id == wallet_group_id
    assert row.ebay_seller_username == "ricky.garage"
    assert row.drive_folder_name == "eBay Account - 2 (ricky-garage)"
    assert row.is_active is True

    # Its own, genuinely new EBAY_WALLET instance — not shared with account 1.
    new_ebay_wallet_id = get_account_id(conn, "EBAY_WALLET", ebay_account_id=outcome["ebay_account_id"])
    assert new_ebay_wallet_id == outcome["ebay_wallet_account_id"]
    assert new_ebay_wallet_id != topo["ebay_wallet_id"]

    # No new PAYONEER_WALLET/BCA_BRIDGING was created — still exactly one of
    # each for the (now shared) wallet-group.
    for code in ("PAYONEER_WALLET", "BCA_BRIDGING"):
        rows = conn.execute(
            select(accounts.c.id)
            .select_from(accounts)
            .join(account_types, accounts.c.account_type_id == account_types.c.id)
            .where(account_types.c.code == code, accounts.c.wallet_group_id == wallet_group_id)
        ).all()
        assert len(rows) == 1, f"expected exactly one {code} for wallet_group_id={wallet_group_id}, found {len(rows)}"

    # And the ONE that exists is still the same id it always was (account 1's).
    assert get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=wallet_group_id) == topo["payoneer_wallet_id"]
    assert get_account_id(conn, "BCA_BRIDGING", wallet_group_id=wallet_group_id) == topo["bca_bridging_id"]


def test_onboard_does_not_touch_account_1s_existing_topology(prototype):
    conn, topo = prototype

    onboard_ebay_account(
        conn,
        name="eBay Account 2",
        wallet_group_id=topo["wallet_group_id"],
        ebay_seller_username="ricky.garage",
        drive_folder_name="eBay Account - 2 (ricky-garage)",
    )

    # Account 1's own EBAY_WALLET is untouched (same id, still resolvable).
    assert get_account_id(conn, "EBAY_WALLET", ebay_account_id=topo["ebay_account_id"]) == topo["ebay_wallet_id"]
    row = conn.execute(
        select(ebay_accounts.c.name, ebay_accounts.c.wallet_group_id).where(
            ebay_accounts.c.id == topo["ebay_account_id"]
        )
    ).first()
    assert row.name == "eBay Account 1"  # seed_prototype_topology's default name — untouched
    assert row.wallet_group_id == topo["wallet_group_id"]


def test_onboard_is_idempotent_safe_to_rerun(prototype):
    conn, topo = prototype
    kwargs = dict(
        name="eBay Account 2",
        wallet_group_id=topo["wallet_group_id"],
        ebay_seller_username="ricky.garage",
        drive_folder_name="eBay Account - 2 (ricky-garage)",
    )

    first = onboard_ebay_account(conn, **kwargs)
    assert first["ebay_account_created"] is True
    assert first["ebay_wallet_created"] is True

    second = onboard_ebay_account(conn, **kwargs)
    assert second["ebay_account_created"] is False
    assert second["ebay_wallet_created"] is False
    assert second["ebay_account_id"] == first["ebay_account_id"]
    assert second["ebay_wallet_account_id"] == first["ebay_wallet_account_id"]

    # Still exactly one ebay_accounts row named "eBay Account 2".
    count = conn.execute(
        select(ebay_accounts.c.id).where(ebay_accounts.c.name == "eBay Account 2")
    ).all()
    assert len(count) == 1


def test_onboard_aborts_on_mismatched_existing_account_rather_than_overwriting(prototype):
    conn, topo = prototype
    onboard_ebay_account(
        conn,
        name="eBay Account 2",
        wallet_group_id=topo["wallet_group_id"],
        ebay_seller_username="ricky.garage",
        drive_folder_name="eBay Account - 2 (ricky-garage)",
    )

    with pytest.raises(OnboardingAbort):
        onboard_ebay_account(
            conn,
            name="eBay Account 2",
            wallet_group_id=topo["wallet_group_id"],
            ebay_seller_username="some-other-username",  # mismatch
            drive_folder_name="eBay Account - 2 (ricky-garage)",
        )

    # Unchanged by the aborted attempt.
    row = conn.execute(
        select(ebay_accounts.c.ebay_seller_username).where(ebay_accounts.c.name == "eBay Account 2")
    ).first()
    assert row.ebay_seller_username == "ricky.garage"


def test_onboard_rejects_nonexistent_wallet_group(prototype):
    conn, topo = prototype
    bogus_wallet_group_id = topo["wallet_group_id"] + 999

    with pytest.raises(OnboardingAbort):
        onboard_ebay_account(
            conn,
            name="eBay Account 2",
            wallet_group_id=bogus_wallet_group_id,
            ebay_seller_username="ricky.garage",
            drive_folder_name="eBay Account - 2 (ricky-garage)",
        )

    assert (
        conn.execute(select(ebay_accounts.c.id).where(ebay_accounts.c.name == "eBay Account 2")).first()
        is None
    )


def test_onboard_reusable_for_a_brand_new_independent_wallet_group(conn):
    """Confirms the script is genuinely generic, not hardcoded to "join an
    existing shared wallet-group" — passing a freshly created, independent
    wallet-group id (the eventual eBay Account 3 shape) works identically.
    """
    from ledger.entities import create_wallet_group

    new_wallet_group_id = create_wallet_group(conn, name="Wallet Group 2 (independent)")

    outcome = onboard_ebay_account(
        conn,
        name="eBay Account 3",
        wallet_group_id=new_wallet_group_id,
        ebay_seller_username="some-future-account",
        drive_folder_name="eBay Account - 3 (some-future-account)",
    )
    assert outcome["ebay_account_created"] is True
    assert outcome["ebay_wallet_created"] is True

    with pytest.raises(UnknownAccountInstanceError):
        # No PAYONEER_WALLET was created for this brand-new wallet-group —
        # the onboarding script never creates wallet-group-scoped accounts,
        # only the eBay account's own EBAY_WALLET.
        get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=new_wallet_group_id)
