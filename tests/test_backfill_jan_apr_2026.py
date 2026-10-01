"""Tests for scripts/backfill_jan_apr_2026.py's account-selection logic
(``_resolve_target_ebay_account``).

Real incident, 2026-10-01: this script originally assumed "exactly one
active eBay account exists" (true in local dev, false in production once a
second account, "eBay Account 2" (ricky-garage), was onboarded via
scripts/onboard_ebay_account.py). It aborted safely in production (per its
own design -- no transaction had been opened yet), but needed a real fix:
select the ONE account this script's real Jan-Apr 2026 documents actually
belong to, not just "whichever one happens to be the only one."

Critically, Account 2 joined the SAME wallet_group_id as Account 1 (see
tests/test_onboard_ebay_account.py's own docstring) -- so wallet_group_id
alone can't disambiguate between them; these tests exercise that exact real
shape (two accounts, same wallet_group_id, different Drive folder names)
using plain ``EbayAccountOption`` instances -- no database needed at all,
since this function is a pure selection over a list.

Also tests ``_confirm_database_identity`` (added 2026-10-01, QA's
recommendation after a real near-miss: running this script locally for a
quick smoke-check of the account-selection fix above accidentally proceeded
to a REAL commit against a local dev database, because the real Drive
preflight check happened to pass independently of DB identity -- the two
signals desynced with nothing forcing an explicit check of the second one).
Covers the monkeypatched-input cases AND a real OS-level piped-stdin
subprocess case (the actual ``echo "dbname" | python3 ...`` shape this will
run as over SSH in production, non-interactively) -- per Main-agent's
explicit instruction not to just assume ``input()`` behaves the same way
under a pipe as it does in an interactive terminal.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from webapp.scoping import EbayAccountOption

from scripts.backfill_jan_apr_2026 import (
    TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME,
    TARGET_WALLET_GROUP_ID,
    _confirm_database_identity,
    _resolve_target_ebay_account,
    _StateMismatch,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeURL:
    def __init__(self, database: str, host: str):
        self.database = database
        self.host = host


class _FakeEngine:
    def __init__(self, database: str, host: str = "localhost"):
        self.url = _FakeURL(database, host)

ACCOUNT_1 = EbayAccountOption(
    id=1,
    name="eBay Account 1",
    wallet_group_id=1,
    wallet_group_name="Wallet Group for 1 (ricky-game)",
    wallet_group_is_shared=True,
    drive_folder_name="eBay Account - 1 (ricky-game)",
    wallet_group_drive_folder_name="Wallet Group for 1 (ricky-game)",
)
# The real 2026-10-01 production shape: Account 2 shares Account 1's
# wallet_group_id (1), not its own -- only the Drive folder name actually
# distinguishes the two.
ACCOUNT_2_SHARED_WALLET_GROUP = EbayAccountOption(
    id=2,
    name="eBay Account 2",
    wallet_group_id=1,
    wallet_group_name="Wallet Group for 1 (ricky-game)",
    wallet_group_is_shared=True,
    drive_folder_name="eBay Account - 2 (ricky-garage)",
    wallet_group_drive_folder_name="Wallet Group for 1 (ricky-game)",
)


def test_selects_account_1_when_two_accounts_share_a_wallet_group():
    """The real 2026-10-01 production scenario this fix exists for."""
    selected = _resolve_target_ebay_account([ACCOUNT_1, ACCOUNT_2_SHARED_WALLET_GROUP])
    assert selected.id == 1
    assert selected.ebay_account_drive_folder_name_resolved == TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME


def test_selects_account_1_regardless_of_list_order():
    selected = _resolve_target_ebay_account([ACCOUNT_2_SHARED_WALLET_GROUP, ACCOUNT_1])
    assert selected.id == 1


def test_still_works_with_only_account_1_present():
    """The original (local-dev, prototype-scope) shape this script was
    first written against -- must keep working, not just the new 2-account
    case.
    """
    selected = _resolve_target_ebay_account([ACCOUNT_1])
    assert selected.id == 1


def test_aborts_when_no_account_matches_the_target_folder_name():
    renamed = EbayAccountOption(
        id=1, name="eBay Account 1", wallet_group_id=1,
        wallet_group_name="Wallet Group for 1 (ricky-game)", wallet_group_is_shared=False,
        drive_folder_name="some other folder name entirely",
        wallet_group_drive_folder_name="Wallet Group for 1 (ricky-game)",
    )
    try:
        _resolve_target_ebay_account([renamed])
        assert False, "expected _StateMismatch"
    except _StateMismatch as exc:
        assert "No active eBay account has Drive folder name" in str(exc)


def test_aborts_when_more_than_one_account_matches_the_target_folder_name():
    """Should never happen in practice (Drive folder names are supposed to
    be unique per account) -- but if it ever does, refuse to guess.
    """
    duplicate = EbayAccountOption(
        id=99, name="eBay Account 1 (duplicate)", wallet_group_id=1,
        wallet_group_name="Wallet Group for 1 (ricky-game)", wallet_group_is_shared=True,
        drive_folder_name=TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME,
        wallet_group_drive_folder_name="Wallet Group for 1 (ricky-game)",
    )
    try:
        _resolve_target_ebay_account([ACCOUNT_1, duplicate])
        assert False, "expected _StateMismatch"
    except _StateMismatch as exc:
        assert "More than one active eBay account" in str(exc)


def test_aborts_on_wallet_group_mismatch_even_if_folder_name_matches():
    """Extra safety cross-check: a folder-name match alone isn't trusted if
    the account's own wallet_group_id disagrees with TARGET_WALLET_GROUP_ID
    (this script's other two corrections, BCA Bridging and Payoneer Wallet,
    are hardcoded to that wallet-group).
    """
    inconsistent = EbayAccountOption(
        id=1, name="eBay Account 1", wallet_group_id=TARGET_WALLET_GROUP_ID + 1,
        wallet_group_name="Some Other Wallet Group", wallet_group_is_shared=False,
        drive_folder_name=TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME,
        wallet_group_drive_folder_name="Some Other Wallet Group",
    )
    try:
        _resolve_target_ebay_account([inconsistent])
        assert False, "expected _StateMismatch"
    except _StateMismatch as exc:
        assert "wallet_group_id" in str(exc)


def test_aborts_on_zero_accounts():
    try:
        _resolve_target_ebay_account([])
        assert False, "expected _StateMismatch"
    except _StateMismatch as exc:
        assert "found 0 active account(s)" in str(exc)


# ---------------------------------------------------------------------------
# _confirm_database_identity -- the DB-identity confirmation checkpoint
# ---------------------------------------------------------------------------


def test_confirm_database_identity_matching_input_returns_true(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "noctrowl")
    assert _confirm_database_identity(_FakeEngine("noctrowl")) is True


def test_confirm_database_identity_matching_input_strips_whitespace(monkeypatch):
    """A real human typing this over SSH may leave trailing whitespace/a
    newline depending on terminal — the comparison should still succeed."""
    monkeypatch.setattr("builtins.input", lambda prompt: "  noctrowl  \n")
    assert _confirm_database_identity(_FakeEngine("noctrowl")) is True


def test_confirm_database_identity_mismatched_input_returns_false(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "some_other_database")
    assert _confirm_database_identity(_FakeEngine("noctrowl")) is False


def test_confirm_database_identity_empty_input_returns_false(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert _confirm_database_identity(_FakeEngine("noctrowl")) is False


def test_confirm_database_identity_closed_stdin_eof_returns_false(monkeypatch):
    """input() raises EOFError when stdin is closed/exhausted (e.g. a
    non-interactive invocation with nothing piped in at all) -- must be
    treated as a non-match, never crash, never guess True."""

    def _raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    assert _confirm_database_identity(_FakeEngine("noctrowl")) is False


def test_confirm_database_identity_prints_real_host_not_hidden(monkeypatch, capsys):
    """QA's specific ask: the real host must be shown here (unlike the
    earlier '(host hidden)' connect-time line elsewhere in this script) --
    it's the one piece of information that would have caught the real
    2026-10-01 near-miss (localhost vs. the real droplet) immediately."""
    monkeypatch.setattr("builtins.input", lambda prompt: "noctrowl")
    _confirm_database_identity(_FakeEngine("noctrowl", host="10.1.2.3"))
    captured = capsys.readouterr()
    assert "10.1.2.3" in captured.out


def test_confirm_database_identity_via_real_os_level_piped_stdin_match():
    """Not a monkeypatch -- a genuine OS-level pipe into a subprocess,
    exactly the real ``echo "dbname" | python3 scripts/backfill_jan_apr_2026.py``
    shape this runs as over SSH, non-interactively. Proves input() under a
    real pipe behaves the way the monkeypatched tests above assume it does.
    """
    code = (
        "import sys; sys.path.insert(0, '.'); "
        "from scripts.backfill_jan_apr_2026 import _confirm_database_identity\n"
        "class U:\n"
        "    database = 'noctrowl'\n"
        "    host = 'localhost'\n"
        "class E:\n"
        "    url = U()\n"
        "print('RESULT', _confirm_database_identity(E()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input="noctrowl\n",
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "RESULT True" in result.stdout, result.stdout + result.stderr


def test_confirm_database_identity_via_real_os_level_piped_stdin_mismatch():
    code = (
        "import sys; sys.path.insert(0, '.'); "
        "from scripts.backfill_jan_apr_2026 import _confirm_database_identity\n"
        "class U:\n"
        "    database = 'noctrowl'\n"
        "    host = 'localhost'\n"
        "class E:\n"
        "    url = U()\n"
        "print('RESULT', _confirm_database_identity(E()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input="wrong_database_name\n",
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "RESULT False" in result.stdout, result.stdout + result.stderr


def test_confirm_database_identity_via_real_os_level_closed_stdin():
    """The real /dev/null-equivalent case: stdin closed/empty entirely
    (e.g. a misconfigured non-interactive invocation with nothing piped at
    all) -- must resolve to False, not hang waiting for input that will
    never arrive, and not crash with an unhandled EOFError.
    """
    code = (
        "import sys; sys.path.insert(0, '.'); "
        "from scripts.backfill_jan_apr_2026 import _confirm_database_identity\n"
        "class U:\n"
        "    database = 'noctrowl'\n"
        "    host = 'localhost'\n"
        "class E:\n"
        "    url = U()\n"
        "print('RESULT', _confirm_database_identity(E()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input="",
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "RESULT False" in result.stdout, result.stdout + result.stderr
