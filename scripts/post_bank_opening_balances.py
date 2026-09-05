"""One-off data-repair script: post opening-balance journal entries for the
BCA Main Account and the Mandiri Bridging Account (wallet-group 1), whose
real balances existed before ledger-tracking began (2026-05-01) -- the same
gap already closed for wallet-group 1's Payoneer Wallet by
``scripts/post_opening_balance.py``.

WHY THIS EXISTS (2026-09-05): both real source statements state an explicit
opening balance for the period 01 May 2026 - 31 May 2026:

- BCA Main Account (``sample-documents/Main Account (BCA)/1790345891_MAY_2026.pdf``):
  "01/05 SALDO AWAL 10,102,959.83" (IDR).
- Mandiri Bridging Account (``sample-documents/Bridging Account (Mandiri)/
  e-Statement_XXXXXXXXX7498_01 Mei 2026-31 Mei 2026_unlocked.pdf``):
  "Saldo Awal/Initial Balance : 66.004.187,57" (IDR).

Neither has ever been posted to the ledger, so (per the Mandiri account's
real activity of its own -- withdrawals, top-ups, fees, interest --
see CLAUDE.md's Money flow correction, 2026-09-01) its running book balance
has been understated by its true pre-tracking opening balance the whole
time, which is exactly what produced the impossible-looking negative
Bridging Account balance flagged in this task's brief.

TRACEABILITY: this script does NOT hardcode either rupiah figure anywhere.
Both are re-derived at run time by calling the REAL, already-tested parsers
directly against the real sample PDFs (``ingestion.bank_statement.
parse_bca_statement`` / ``ingestion.mandiri_statement.parse_mandiri_
statement``), so it's self-verifying -- if a parser's extraction of the
opening-balance line ever regresses, this script fails loudly instead of
silently posting a wrong/stale number.

BOOKING: both accounts are IDR-native (BCA Main Account and the Mandiri
Bridging Account are both real local IDR bank accounts -- see CLAUDE.md's
Chart of accounts), so unlike the Payoneer Wallet opening-balance entry,
there is no USD reference / Kurs Pajak conversion step here at all --
``amount_usd_ref``/``fx_rate_used`` are left at their default ``None`` on
both postings. Both credit OWNERS_CAPITAL via ``ledger.posting.
post_opening_balance``, same as every other opening-balance entry (a
pre-existing balance predating ledger tracking is the owner's own money
that was already there -- see that function's docstring).

IDEMPOTENT: safe to re-run. Same two-layer guard as
``scripts/post_opening_balance.py`` -- a fast-path pre-check per account
that looks for an existing ``opening_balances`` row and skips posting again
if found, backstopped by the DB-level unique index
``ux_opening_balances_account_id`` (see ledger/schema.py) for genuine
concurrent-attempt safety.

USAGE:
    python3 scripts/post_bank_opening_balances.py

Reads DATABASE_URL from the environment via python-dotenv (same pattern as
scripts/post_opening_balance.py) -- never hardcodes a connection string.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from ingestion.bank_statement import parse_bca_statement  # noqa: E402
from ingestion.mandiri_statement import parse_mandiri_statement  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from ledger.entities import get_account_id  # noqa: E402
from ledger.posting import post_opening_balance, round_idr  # noqa: E402
from ledger.schema import opening_balances  # noqa: E402

_SAMPLE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample-documents")

# "Booked as of 2026-05-01" -- the first day of the earliest real statement
# period covered by the sample set (May 2026) for both accounts, matching
# the convention already used for the Payoneer Wallet opening balance.
ENTRY_DATE = _dt.date(2026, 5, 1)

# Only wallet-group 1 exists in the prototype (see CLAUDE.md's Prototype
# scope) -- named as a constant, not hardcoded inline, so a future re-run
# for wallet-group 2 only needs this one line changed.
WALLET_GROUP_ID = 1


@dataclasses.dataclass(frozen=True)
class _Target:
    label: str
    account_type_code: str
    wallet_group_id: int | None
    pdf_path: str
    parse: "callable"  # returns something with .opening_balance_idr


TARGETS = [
    _Target(
        label="BCA Main Account",
        account_type_code="BCA_MAIN",
        wallet_group_id=None,  # consolidated singleton
        pdf_path=os.path.join(_SAMPLE_ROOT, "Main Account (BCA)", "1790345891_MAY_2026.pdf"),
        parse=parse_bca_statement,
    ),
    _Target(
        label="Mandiri Bridging Account (wallet-group 1)",
        account_type_code="BCA_BRIDGING",
        wallet_group_id=WALLET_GROUP_ID,
        pdf_path=os.path.join(
            _SAMPLE_ROOT,
            "Bridging Account (Mandiri)",
            "e-Statement_XXXXXXXXX7498_01 Mei 2026-31 Mei 2026_unlocked.pdf",
        ),
        parse=parse_mandiri_statement,
    ),
]


def _derive_opening_balance_idr(target: _Target) -> Decimal:
    if not os.path.isfile(target.pdf_path):
        raise FileNotFoundError(f"[{target.label}] statement PDF not found: {target.pdf_path}")

    result = target.parse(target.pdf_path)
    if result.opening_balance_idr is None:
        raise ValueError(
            f"[{target.label}] parser found no opening balance in {target.pdf_path} "
            f"(parse_warnings={result.parse_warnings!r}). Refusing to invent a figure."
        )
    opening_balance = round_idr(result.opening_balance_idr)
    print(f"[{target.label}] parsed opening balance from {os.path.basename(target.pdf_path)}: {opening_balance} IDR")
    return opening_balance


def main() -> int:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    exit_code = 0
    with engine.begin() as conn:
        for target in TARGETS:
            print(f"--- {target.label} ---")
            try:
                amount_idr = _derive_opening_balance_idr(target)
            except (FileNotFoundError, ValueError) as exc:
                print(f"ABORT for {target.label}: {exc}\n")
                exit_code = 1
                continue

            target_account_id = get_account_id(
                conn, target.account_type_code, wallet_group_id=target.wallet_group_id
            )
            print(f"Target account: {target.account_type_code} -> account_id={target_account_id}")

            existing = conn.execute(
                select(
                    opening_balances.c.id,
                    opening_balances.c.journal_entry_id,
                    opening_balances.c.amount_idr,
                    opening_balances.c.entry_date,
                ).where(opening_balances.c.account_id == target_account_id)
            ).first()
            if existing is not None:
                print(
                    f"Already posted -- opening_balances.id={existing.id}, "
                    f"journal_entry_id={existing.journal_entry_id}, entry_date={existing.entry_date}, "
                    f"amount_idr={existing.amount_idr}. Nothing to do -- skipping.\n"
                )
                continue

            try:
                entry_id = post_opening_balance(
                    conn,
                    account_type_code=target.account_type_code,
                    entry_date=ENTRY_DATE,
                    amount_idr=amount_idr,
                    wallet_group_id=target.wallet_group_id,
                    memo=(
                        f"Opening balance as of {ENTRY_DATE.isoformat()} -- real pre-ledger-tracking "
                        f"IDR balance derived from {os.path.basename(target.pdf_path)} "
                        "(SALDO AWAL / Saldo Awal), booked to Owner's Capital."
                    ),
                )
            except IntegrityError:
                print(
                    f"ABORT for {target.label}: a concurrent post already created an opening_balances "
                    "row for this account (ux_opening_balances_account_id unique index) -- treating as "
                    "already posted, not double-posting.\n"
                )
                continue

            print(
                f"Posted successfully:\n"
                f"  journal_entry_id = {entry_id}\n"
                f"  account          = {target.account_type_code} (account_id={target_account_id})\n"
                f"  entry_date       = {ENTRY_DATE.isoformat()}\n"
                f"  amount_idr       = {amount_idr}\n"
                f"  credited         = OWNERS_CAPITAL\n"
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
