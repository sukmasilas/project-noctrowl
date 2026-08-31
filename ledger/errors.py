"""Exceptions raised by the posting engine.

These are application-layer, fail-fast checks that run *before* any SQL is
issued. They exist alongside (not instead of) the Postgres structural
triggers in ledger/schema.py — the DB triggers are the real backstop; these
give callers (and tests) a clear, specific Python exception rather than a
raw database error.
"""


class LedgerError(Exception):
    """Base class for all ledger posting errors."""


class UnbalancedEntryError(LedgerError):
    """Raised when a proposed posting's debits and credits don't match."""


class MissingConfirmedAmountError(LedgerError):
    """Raised when a consignment payout (or other confirmation-required
    figure) was not supplied explicitly.

    The posting engine never auto-computes and posts a consignor payout —
    see CLAUDE.md's Core accounting rules. The calculator functions in
    ledger/consignment.py may be used to *suggest* a number to a human
    reviewer, but the posting functions always require the confirmed amount
    as an explicit argument.
    """


class InvalidTransferError(LedgerError):
    """Raised when an inter-account transfer targets a non-transfer account."""


class UnknownAccountInstanceError(LedgerError):
    """Raised when a requested account/entity combination has no matching
    account_instances row (e.g. asking for a Payoneer Wallet for an eBay
    account whose wallet-group hasn't been set up yet)."""


class InvalidStructuralEntityError(LedgerError):
    """Raised when structural setup data would violate the per-eBay-account /
    per-wallet-group / consolidated scoping rules (see ledger/schema.py)."""
