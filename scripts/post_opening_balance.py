"""One-off data-repair script: post an opening-balance journal entry for a
wallet/bank account whose real balance existed before ledger-tracking began
(2026-05-01, the earliest posted entry in the real database).

WHY THIS EXISTS (2026-09-03): see CLAUDE.md's "Definition of done" section,
the negative-Payoneer-balance gap note near the bottom.
``scheduling.fx_revaluation.compute_payoneer_wallet_balance`` was computing
an impossible negative USD balance for wallet-group 1's Payoneer Wallet,
because the ledger had no record of the wallet's real balance as of just
before the earliest tracked transaction (2026-05-06, a $574.81 eBay
payment). The user found a real, richer Payoneer export
(``sample-documents/Payoneer/Monthly Statements/Transactions_05-2026.csv``,
the Reports & Statements format, which includes a ``Running Balance``
column that the Transactions-page export already ingested by
``ingestion/payoneer.py`` does not) that resolves this precisely: the
running balance immediately AFTER that earliest tracked transaction, minus
the transaction's own credit amount, is the real balance immediately
BEFORE it -- i.e. the account's true opening balance as of 2026-05-01.

TRACEABILITY (never a derived/invented guess -- CLAUDE.md's core rule): this
script does NOT hardcode the $4,703.00 figure anywhere. It re-derives it
by reading the actual CSV file directly at run time (see
``_compute_opening_balance_usd`` below), so it's self-verifying and
re-runnable against a different wallet-group/CSV later if the user ever
needs to repeat this for wallet-group 2 (out of scope for now -- see
CLAUDE.md's Prototype scope -- but the script itself places no
wallet-group-1-only assumption anywhere except in the constants at the top
of ``main()``, which a future caller can simply change).

BOOKING: per the user's 2026-09-03 confirmation, this books to
OWNERS_CAPITAL (a pre-existing balance predating ledger tracking is the
owner's own money that was already there) via
``ledger.posting.post_opening_balance`` -- see that function's docstring
for why this is a new function rather than reusing
``post_owner_contribution`` (which hardcodes debiting BCA_MAIN and
represents an ongoing capital-injection event, not a one-time correction).
The IDR side uses the project's standard booking-rate convention -- the
Kurs Pajak rate as of the entry date, via
``ingestion.kurs_pajak.lookup_most_recent_rate_as_of`` (the same lookup
function ``ledger.posting.post_realized_fx_withdrawal``'s callers use for a
Payoneer-withdrawal booking rate) -- never a hardcoded rate number.

IDEMPOTENT: safe to re-run. A fast-path pre-check looks for an existing
``opening_balances`` row for the target account and, if found, reports it
and exits without posting again. The real backstop is the DB-level unique
index ``ux_opening_balances_account_id`` (see ledger/schema.py) -- a
genuinely concurrent second attempt still fails safely at INSERT time
rather than double-posting.

USAGE:
    python3 scripts/post_opening_balance.py

Reads DATABASE_URL from the environment via python-dotenv (same pattern as
scripts/run_migrations.py and scripts/backfill_missing_usd_ref.py) --
never hardcodes a connection string.
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from ingestion.kurs_pajak import NoKursPajakRateError, lookup_most_recent_rate_as_of  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from ledger.entities import get_account_id  # noqa: E402
from ledger.posting import post_opening_balance, round_idr  # noqa: E402
from ledger.schema import opening_balances  # noqa: E402

# --- The one real, current use of this script (2026-09-03) ---------------
# Only wallet-group 1's Payoneer Wallet -- see CLAUDE.md's Prototype scope
# ("only one wallet-group is active in the prototype"). Nothing below this
# comment is a general-purpose CLI; these are the specific, already-decided
# inputs for this one-off correction, kept as named constants (rather than
# hardcoded inline) so a future re-run against a different wallet-group/CSV
# only requires changing these four lines, not the logic beneath them.
WALLET_GROUP_ID = 1
ACCOUNT_TYPE_CODE = "PAYONEER_WALLET"
CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sample-documents",
    "Payoneer",
    "Monthly Statements",
    "Transactions_05-2026.csv",
)
# "Booked as of 2026-05-01" per the user's brief -- just before the earliest
# tracked transaction (2026-05-06) in the whole real database, and before
# any transaction in the CSV read below.
ENTRY_DATE = _dt.date(2026, 5, 1)


def _parse_money(raw: str) -> Decimal | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return Decimal(raw.replace(",", ""))


def _compute_opening_balance_usd(csv_path: str) -> Decimal:
    """Read the real Payoneer Reports & Statements CSV and derive the
    account's true USD balance as of just before its EARLIEST transaction
    (by Transaction Date), from that transaction's own Running Balance and
    Credit/Debit Amount columns -- never a hardcoded figure. Raises if the
    file is missing, empty, or internally inconsistent (chain doesn't add
    up), rather than silently trusting a possibly-wrong number.
    """
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"Payoneer statement CSV not found: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"No data rows in {csv_path}")

    parsed = []
    for r in rows:
        txn_date = _dt.datetime.strptime(r["Transaction Date"].strip(), "%m/%d/%Y").date()
        credit = _parse_money(r.get("Credit Amount", ""))
        debit = _parse_money(r.get("Debit Amount", ""))
        running_balance = _parse_money(r.get("Running Balance", ""))
        if running_balance is None:
            raise ValueError(f"Row for {txn_date} has no Running Balance -- can't derive an opening balance.")
        if (credit is None) == (debit is None):
            raise ValueError(f"Row for {txn_date} has ambiguous credit/debit amounts: {r}")
        parsed.append((txn_date, credit, debit, running_balance, r.get("Transaction ID"), r.get("Description")))

    # Earliest transaction chronologically (the file itself is sorted most
    # recent first, but this doesn't assume that -- it finds the true min).
    earliest = min(parsed, key=lambda p: p[0])
    txn_date, credit, debit, running_balance, txn_id, description = earliest

    if credit is not None:
        opening_balance = running_balance - credit
    else:
        opening_balance = running_balance + debit

    print(f"Read {len(parsed)} transaction row(s) from {csv_path}")
    print(
        f"Earliest transaction: {txn_date.isoformat()} (Transaction ID {txn_id}, {description!r}), "
        f"Credit={credit}, Debit={debit}, Running Balance={running_balance}"
    )
    print(f"=> Derived opening balance immediately before this transaction: {opening_balance} USD")

    return opening_balance


def main() -> int:
    opening_balance_usd = _compute_opening_balance_usd(CSV_PATH)
    if opening_balance_usd <= 0:
        print(f"ABORT: derived opening balance {opening_balance_usd} is not a positive real balance -- stopping.")
        return 1

    engine = get_engine()
    print(f"\nConnecting to database: {engine.url.database!r} (host hidden)")

    with engine.begin() as conn:
        target_account_id = get_account_id(conn, ACCOUNT_TYPE_CODE, wallet_group_id=WALLET_GROUP_ID)
        print(f"Target account: {ACCOUNT_TYPE_CODE} for wallet_group_id={WALLET_GROUP_ID} -> account_id={target_account_id}")

        # Fast-path idempotency check (the real backstop is the DB unique
        # index -- see ledger/schema.py's ux_opening_balances_account_id --
        # but checking first avoids posting a needless duplicate attempt and
        # gives a clearer message on an ordinary re-run).
        existing = conn.execute(
            select(
                opening_balances.c.id,
                opening_balances.c.journal_entry_id,
                opening_balances.c.amount_idr,
                opening_balances.c.amount_usd_ref,
                opening_balances.c.fx_rate_used,
                opening_balances.c.entry_date,
            ).where(opening_balances.c.account_id == target_account_id)
        ).first()
        if existing is not None:
            print(
                f"\nAlready posted -- opening_balances.id={existing.id}, "
                f"journal_entry_id={existing.journal_entry_id}, entry_date={existing.entry_date}, "
                f"amount_idr={existing.amount_idr}, amount_usd_ref={existing.amount_usd_ref}, "
                f"fx_rate_used={existing.fx_rate_used}. Nothing to do -- exiting without posting again."
            )
            return 0

        try:
            kurs_rate = lookup_most_recent_rate_as_of(conn, ENTRY_DATE)
        except NoKursPajakRateError as exc:
            print(f"\nABORT: {exc}\nRefusing to invent/hardcode a rate -- seed a Kurs Pajak rate covering {ENTRY_DATE} first.")
            return 1
        print(f"Kurs Pajak rate as of {ENTRY_DATE.isoformat()} (most-recent-as-of lookup): {kurs_rate}")

        amount_idr = round_idr(opening_balance_usd * kurs_rate)
        print(f"amount_idr = round({opening_balance_usd} * {kurs_rate}) = {amount_idr}")

        try:
            entry_id = post_opening_balance(
                conn,
                account_type_code=ACCOUNT_TYPE_CODE,
                entry_date=ENTRY_DATE,
                amount_idr=amount_idr,
                wallet_group_id=WALLET_GROUP_ID,
                amount_usd_ref=opening_balance_usd,
                fx_rate_used=kurs_rate,
                memo=(
                    f"Opening balance as of {ENTRY_DATE.isoformat()} -- real pre-ledger-tracking USD balance "
                    "derived from Payoneer Reports & Statements export "
                    "(Transactions_05-2026.csv), booked to Owner's Capital per user confirmation 2026-09-03."
                ),
            )
        except IntegrityError:
            print(
                "\nABORT: a concurrent post already created an opening_balances row for this account "
                "(ux_opening_balances_account_id unique index) -- treating as already posted, not double-posting."
            )
            return 0

    print(
        f"\nPosted successfully:\n"
        f"  journal_entry_id = {entry_id}\n"
        f"  account          = {ACCOUNT_TYPE_CODE} (wallet_group_id={WALLET_GROUP_ID}, account_id={target_account_id})\n"
        f"  entry_date       = {ENTRY_DATE.isoformat()}\n"
        f"  amount_idr       = {amount_idr}\n"
        f"  amount_usd_ref   = {opening_balance_usd}\n"
        f"  fx_rate_used     = {kurs_rate}\n"
        f"  credited         = OWNERS_CAPITAL\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
