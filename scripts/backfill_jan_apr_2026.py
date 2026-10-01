"""One-off, idempotent, state-verifying script: extends the real ledger
backward from 2026-05-01 to cover January-April 2026, using real source
documents now available (see CLAUDE.md's 2026-09-30 Jan-Apr 2026 backfill
entry, and Builder's escalation/preview thread this same day).

BACKGROUND (why this exists): when May 2026 was originally established as
the ledger's start date, three accounts each had no record of the real
money that already existed in them before ledger-tracking began, so each
got a synthetic "opening balance" journal entry dated 2026-05-01, crediting
the ENTIRE pre-tracking balance to Owner's Capital in one lump sum (see
ledger.posting.post_opening_balance, scripts/post_opening_balance.py,
scripts/post_bank_opening_balances.py):
  - Payoneer Wallet (wallet-group 1): $4,703.00 USD / Rp 75,483,150.00
  - BCA Main:                          Rp 10,102,960.00
  - BCA Bridging (wallet-group 1):     Rp 66,004,188.00

Real Jan-Apr 2026 source documents (BCA Main + Mandiri Bridging statements,
a Payoneer CSV + 10 withdrawal confirmations, and 4 months of eBay Account 1
sales CSVs) now exist and chain EXACTLY into these same three May-1 figures
(independently confirmed by Builder before this script was written) — so
simply layering Jan-Apr postings on top of the existing May-1 lump sums
would double-count that money: once as a "pre-tracking" capital injection,
once again as the real trading activity that actually produced it.

THE FIX, PER STEP:
1. For each of the 3 accounts above: reverse its 2026-05-01 lump-sum entry
   via ledger.posting.post_reversal_entry (backdated to 2026-05-01, its own
   original date — this project's established convention, see
   scripts/correct_journal_entry_917.py), delete its now-stale
   opening_balances tracking row, and post a NEW, smaller opening balance
   dated 2026-01-01 — each account's REAL balance as of just before the
   ledger's new Jan-Apr coverage begins:
     - BCA Main:      Rp 631,809.61  (1790345891_JAN_2026.pdf's own Saldo Awal)
     - BCA Bridging:  Rp 59,499.07   (the Jan Mandiri e-Statement's own Saldo Awal)
     - Payoneer:      $7,157.89 USD  (DERIVED — see PAYONEER_JAN1_USD below;
                       no January "Monthly Statement"/Running-Balance export
                       exists yet, so this works backward from the already
                       -verified real May-1 $4,703.00 figure using the real
                       Jan-Apr "Reports & Statements" CSV's own 22 stated row
                       amounts, net USD movement Jan2-Apr29 = -$2,454.89.
                       Confirmed these are the exact same amounts
                       post_inter_account_transfer/post_realized_fx_withdrawal
                       actually move against the wallet (gross USD, not
                       net-of-fee) before relying on this, so it's not an
                       independent guess.)
   The real Jan-Apr trading activity ingested in step 3 below then carries
   each account forward from its real Jan-1 balance to the real, already
   -known May-1 balance organically — no gap, no double-count.
2. Seeds a PLACEHOLDER flat Kurs Pajak rate, 2026-01-01 through the day
   before the real 2026-04-27 rate already in the database — see
   PLACEHOLDER_KURS_RATE_IDR below. NOT a real Kemenkeu rate: no real weekly
   Kurs Pajak rates for Jan-Apr 2026 have been sourced yet (CLAUDE.md
   requires these be manually seeded from the real Kemenkeu publication, not
   invented or scraped). This affects the exact IDR value of USD-denominated
   Jan-Apr postings (eBay sales revenue, Payoneer withdrawals) — treat those
   figures as directionally correct but not final until real weekly rates
   are sourced and this placeholder is superseded by real seeded rows.
3. Runs the real, already-proven, UNMODIFIED ingestion.sync.run_sync_for_period
   pipeline against the REAL Google Drive folders (ingestion.drive_client.
   DriveClient — never a fake/local substitute for a real commit) for eBay
   Account 1 / its wallet-group / the Master Account, once per period
   (January, February, March, April 2026).
4. As a natural, correct consequence of step 3 — ingestion.matching.
   run_auto_match / post_pending_rows are GLOBAL (scan the entire
   review_queue table, not scoped to whichever period run_sync_for_period
   was called for; see their own docstrings and CLAUDE.md's Bank transaction
   classification rule 4, "picked up here on the next sync") — this ALSO
   sweeps and posts a real, pre-existing 66-row May-August 2026
   review-queue backlog (already user-labeled, never posted). Confirmed and
   approved by the user 2026-09-30 (see Builder's isolated-repro escalation
   to Main-agent the same day) — this is intentional, not a bug, and would
   happen the next time ANY sync runs regardless of this script.
5. Prints a full summary: every new journal entry created, Owner's
   Capital/Retained Earnings/Net Income before and after for May-August
   2026, the new January-April 2026 figures, and a Balance Sheet identity
   check (Assets = Liabilities + Equity).

PREREQUISITE — NOT SOMETHING THIS SCRIPT DOES: the real Jan-Apr 2026 source
documents must already be uploaded to the real Google Drive folders before
running this. Confirmed 2026-09-30 (read-only check against the real Drive
tree, via the real OAuth-authenticated DriveClient): none of the four
period folders exist there yet. This script's own preflight check (see
_preflight_check_drive_documents below) re-verifies this itself, at run
time, and ABORTS with an itemized list of exactly what's missing where
if anything is still absent — it will never silently proceed as a
no-op ingestion while still reversing/reposting the opening balances (that
would leave the ledger's equity composition changed with no real Jan-Apr
activity yet booked to explain the difference).

IDEMPOTENT / SAFE TO RE-RUN, same pattern as scripts/correct_journal_entry_917.py
and scripts/correct_journal_entry_931.py:
  - The opening-balance correction step (per account) checks the real,
    current opening_balances row before touching anything: if it already
    shows the NEW 2026-01-01 state, that account is skipped; if it doesn't
    match the expected ORIGINAL 2026-05-01 state either, the script aborts
    with a clear error rather than guessing.
  - The Kurs Pajak placeholder-seed step checks for an existing row first;
    if one exists with a DIFFERENT rate (e.g. a real rate has since been
    seeded), it aborts rather than silently overwriting a real number with a
    placeholder.
  - The ingestion step (ingestion.sync.run_sync_for_period) is already
    idempotent by design (source_documents upsert, review_queue external
    -ref/occurrence-index dedup, per-category posted-row checks) — re
    -running this script after a partial failure simply continues correctly,
    it does not double-post anything already posted.
  - Does NOT hardcode journal_entry_id/opening_balances.id values anywhere
    (deliberately — the real production database's IDs may differ from any
    local/dev database's, since Main-agent confirmed 2026-09-30 that
    production has already diverged from local dev with independent recent
    fixes). Every ID this script touches is resolved live, by account type
    + scope, and cross-checked against its EXPECTED AMOUNT before being
    touched.

USAGE:
    python3 scripts/backfill_jan_apr_2026.py

Reads DATABASE_URL, GOOGLE_DRIVE_ROOT_FOLDER_ID, and Google Drive OAuth
credentials from the environment via python-dotenv — never hardcodes a
connection string or credential.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import delete, select  # noqa: E402

import ingestion.schema  # noqa: E402,F401 - registers milestone-3 tables on the shared metadata
from ingestion import matching  # noqa: E402
from ingestion.drive_client import DriveClient, DriveCredentialError  # noqa: E402
from ingestion.kurs_pajak import seed_kurs_pajak_rate  # noqa: E402
from ingestion.schema import kurs_pajak_rates  # noqa: E402
from ingestion.sync import (  # noqa: E402
    BANK_STATEMENTS_SUBFOLDER,
    EBAY_SALES_SUBFOLDER,
    PAYONEER_SUBFOLDER,
    UPLOADS_ROOT_NAME,
    resolve_folder_path,
    run_sync_for_period,
)
from ledger.db import get_engine  # noqa: E402
from ledger.entities import get_account_id  # noqa: E402
from ledger.posting import post_opening_balance, post_reversal_entry, round_idr  # noqa: E402
from ledger.schema import journal_entries, opening_balances  # noqa: E402
from webapp.reporting import balance_sheet_report, equity_report, pnl_report  # noqa: E402
from webapp.scoping import EbayAccountOption, list_ebay_accounts  # noqa: E402

MASTER_FOLDER_NAME = "Master Account"

# The one real, specific eBay account this script's Jan-Apr 2026 documents
# belong to -- exact Drive folder name, matched EXACTLY (not a loose
# substring check) against ebay_accounts.drive_folder_name. Found 2026-10-01:
# production now has a SECOND active eBay account ("eBay Account 2" /
# "eBay Account - 2 (ricky-garage)", onboarded separately via
# scripts/onboard_ebay_account.py) that joined the SAME wallet_group_id=1 as
# Account 1 (see tests/test_onboard_ebay_account.py's own docstring: "eBay
# Account 2 joins that SAME wallet-group, sharing its existing Payoneer
# Wallet / BCA Bridging Account") -- so wallet_group_id==1 alone does NOT
# disambiguate between the two accounts; it matches both. The account's own
# Drive folder name is the one genuinely distinguishing identifier here
# (assigned once, at onboarding, never shared between accounts), so that's
# the primary selector -- wallet_group_id==1 is then used as a SECOND,
# independent cross-check (this script's other 2 corrections --
# BCA_BRIDGING and PAYONEER_WALLET -- are already hardcoded to
# wallet_group_id==1 elsewhere below; if the folder-matched account's own
# wallet_group_id ever disagreed with that, something structural would be
# wrong and this script should refuse to guess, not silently proceed).
TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME = "eBay Account - 1 (ricky-game)"
TARGET_WALLET_GROUP_ID = 1
OLD_ENTRY_DATE = _dt.date(2026, 5, 1)
NEW_ENTRY_DATE = _dt.date(2026, 1, 1)

PERIODS = [_dt.date(2026, 1, 1), _dt.date(2026, 2, 1), _dt.date(2026, 3, 1), _dt.date(2026, 4, 1)]
REPORT_PERIODS_MAY_AUG = [_dt.date(2026, 5, 1), _dt.date(2026, 6, 1), _dt.date(2026, 7, 1), _dt.date(2026, 8, 1)]

# --- Payoneer's real Jan-1 2026 USD balance, derived (never hardcoded blind
# -- see this module's docstring for the full derivation and traceability
# argument). $4,703.00 is the already-verified real 2026-05-01 figure
# (scripts/post_opening_balance.py); -$2,454.89 is the real, independently
# -confirmed net USD movement across all 22 rows of the real combined Jan
# -Apr 2026 Payoneer CSV (12 "Payment from eBay" inflows, 10 "Withdrawal to
# BANK MANDIRI (7498)" outflows).
PAYONEER_MAY1_USD = Decimal("4703.00")
PAYONEER_JAN_APR_NET_MOVEMENT_USD = Decimal("-2454.89")
PAYONEER_JAN1_USD = PAYONEER_MAY1_USD - PAYONEER_JAN_APR_NET_MOVEMENT_USD  # 7157.89

# PLACEHOLDER, not a real Kemenkeu Kurs Pajak rate -- see module docstring
# point 2. Held flat from 2026-01-01 through the day before the real
# 2026-04-27 rate already seeded in the database.
PLACEHOLDER_KURS_RATE_IDR = Decimal("16050.0000")
PLACEHOLDER_KURS_EFFECTIVE_DATE = _dt.date(2026, 1, 1)

CORRECTION_MEMO_TEMPLATE = (
    "Correction, {date}: reversing the {old_amount} lump-sum 'pre-tracking' opening balance dated "
    "2026-05-01 for {label}, because real Jan-Apr 2026 source documents now exist and organically "
    "chain into that exact same 2026-05-01 balance. Reposting a new, smaller opening balance dated "
    "2026-01-01 ({new_amount}) -- the account's real balance as of just before the ledger's new "
    "Jan-Apr 2026 coverage begins -- so the real Jan-Apr trading activity being ingested in this same "
    "script carries the account forward organically instead of double-counting it. "
    "See scripts/backfill_jan_apr_2026.py's module docstring for the full derivation."
)


class _StateMismatch(Exception):
    """Raised when the real database doesn't match what this script expects
    to find -- refuses to proceed rather than guessing or touching the wrong
    data. Every account this script touches is resolved live and
    cross-checked against an expected amount before anything is written."""


def _resolve_target_ebay_account(accounts: list[EbayAccountOption]) -> EbayAccountOption:
    """Selects, from every active eBay account, the ONE this script's real
    Jan-Apr 2026 documents belong to -- by exact Drive-folder-name match
    (TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME), cross-checked against
    TARGET_WALLET_GROUP_ID. Does NOT assume "exactly one active account
    exists" (that broke for real 2026-10-01, once a second account was
    onboarded into the SAME wallet-group as the first -- see this module's
    top-of-file comment on TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME for why
    wallet_group_id alone can't disambiguate here). Raises _StateMismatch
    (never guesses) if zero, more than one, or a wallet-group-mismatched
    account matches.
    """
    matches = [
        a for a in accounts if a.ebay_account_drive_folder_name_resolved == TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME
    ]
    if len(matches) == 0:
        raise _StateMismatch(
            f"No active eBay account has Drive folder name {TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME!r} -- "
            f"found {len(accounts)} active account(s) total: "
            f"{[(a.id, a.name, a.ebay_account_drive_folder_name_resolved) for a in accounts]}. "
            "This script doesn't know which one to target -- investigate/update "
            "TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME deliberately rather than guessing."
        )
    if len(matches) > 1:
        raise _StateMismatch(
            f"More than one active eBay account has Drive folder name "
            f"{TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_NAME!r}: {[(a.id, a.name) for a in matches]}. This should "
            "never happen (Drive folder names are supposed to be unique per account) -- investigate "
            "rather than guessing which one is the real target."
        )
    target = matches[0]
    if target.wallet_group_id != TARGET_WALLET_GROUP_ID:
        raise _StateMismatch(
            f"Matched eBay account {target.name!r} (id={target.id}) by Drive folder name, but its "
            f"wallet_group_id={target.wallet_group_id} does not match the expected "
            f"TARGET_WALLET_GROUP_ID={TARGET_WALLET_GROUP_ID}. This script's BCA Bridging / Payoneer "
            "Wallet corrections are hardcoded to that wallet-group -- refusing to proceed against an "
            "inconsistent structural assumption rather than guessing which one is stale."
        )
    return target


# ---------------------------------------------------------------------------
# Step 0: preflight -- confirm the real Google Drive folders actually have
# the Jan-Apr 2026 documents before touching the database at all.
# ---------------------------------------------------------------------------


def _preflight_check_drive_documents(drive_client, root_folder_id: str, ebay_account) -> list[str]:
    """Returns a list of human-readable problem descriptions (empty list =
    all clear). Checks, for each of the 4 periods, the 4 fixed-expectation
    document types (eBay sales CSV, Payoneer CSV, Bridging bank statement,
    Master bank statement) -- the same 4 CLAUDE.md's Report finalization
    status section already treats as fixed expectations per account/period.
    Invoices are NOT checked here -- none are expected for this period at
    all (confirmed deliberate, per CLAUDE.md; a missing invoice never gates
    anything).
    """
    problems: list[str] = []
    for period_month in PERIODS:
        year, ym = str(period_month.year), f"{period_month.year}-{period_month.month:02d}"

        ebay_folder = resolve_folder_path(
            drive_client, root_folder_id, UPLOADS_ROOT_NAME, ebay_account.ebay_account_drive_folder_name_resolved,
            year, ym, EBAY_SALES_SUBFOLDER,
        )
        if not ebay_folder or not drive_client.list_files(ebay_folder):
            problems.append(
                f"[{ym}] eBay sales CSV missing: '{UPLOADS_ROOT_NAME}/"
                f"{ebay_account.ebay_account_drive_folder_name_resolved}/{year}/{ym}/{EBAY_SALES_SUBFOLDER}/' "
                "has no file (or the folder doesn't exist yet)."
            )

        payoneer_folder = resolve_folder_path(
            drive_client, root_folder_id, UPLOADS_ROOT_NAME, ebay_account.wallet_group_drive_folder_name_resolved,
            year, ym, PAYONEER_SUBFOLDER,
        )
        if not payoneer_folder or not drive_client.list_files(payoneer_folder):
            problems.append(
                f"[{ym}] Payoneer CSV (+ withdrawal confirmation PDFs) missing: '{UPLOADS_ROOT_NAME}/"
                f"{ebay_account.wallet_group_drive_folder_name_resolved}/{year}/{ym}/{PAYONEER_SUBFOLDER}/' "
                "has no file (or the folder doesn't exist yet)."
            )

        bridging_folder = resolve_folder_path(
            drive_client, root_folder_id, UPLOADS_ROOT_NAME, ebay_account.wallet_group_drive_folder_name_resolved,
            year, ym, BANK_STATEMENTS_SUBFOLDER,
        )
        if not bridging_folder or not drive_client.list_files(bridging_folder):
            problems.append(
                f"[{ym}] Bridging (Mandiri) bank statement missing: '{UPLOADS_ROOT_NAME}/"
                f"{ebay_account.wallet_group_drive_folder_name_resolved}/{year}/{ym}/{BANK_STATEMENTS_SUBFOLDER}/' "
                "has no file (or the folder doesn't exist yet)."
            )

        master_folder = resolve_folder_path(
            drive_client, root_folder_id, UPLOADS_ROOT_NAME, MASTER_FOLDER_NAME, year, ym, BANK_STATEMENTS_SUBFOLDER
        )
        if not master_folder or not drive_client.list_files(master_folder):
            problems.append(
                f"[{ym}] Master Account (BCA Main) bank statement missing: '{UPLOADS_ROOT_NAME}/"
                f"{MASTER_FOLDER_NAME}/{year}/{ym}/{BANK_STATEMENTS_SUBFOLDER}/' has no file (or the "
                "folder doesn't exist yet)."
            )
    return problems


# ---------------------------------------------------------------------------
# Step 1: reverse + repost the 3 opening balances, dynamically resolved by
# account type/scope -- never by a hardcoded journal_entry_id/
# opening_balances.id, since those may differ between databases.
# ---------------------------------------------------------------------------


def _ensure_jan1_opening_balance(
    conn,
    *,
    label: str,
    account_type_code: str,
    wallet_group_id: int | None,
    expected_old_amount_idr: Decimal,
    new_amount_idr: Decimal,
    new_amount_usd_ref: Decimal | None = None,
    new_fx_rate_used: Decimal | None = None,
) -> dict | None:
    account_id = get_account_id(conn, account_type_code, wallet_group_id=wallet_group_id)
    existing = conn.execute(
        select(
            opening_balances.c.id,
            opening_balances.c.journal_entry_id,
            opening_balances.c.entry_date,
            opening_balances.c.amount_idr,
        ).where(opening_balances.c.account_id == account_id)
    ).first()

    if existing is None:
        raise _StateMismatch(
            f"{label}: no opening_balances row exists for account_id={account_id} -- expected to find "
            f"the original {OLD_ENTRY_DATE.isoformat()} lump-sum entry ({expected_old_amount_idr}) still "
            "in place before this correction. Investigate before proceeding -- refusing to invent one."
        )

    if existing.entry_date == NEW_ENTRY_DATE and existing.amount_idr == new_amount_idr:
        print(
            f"  {label}: already corrected (opening_balances.id={existing.id}, "
            f"journal_entry_id={existing.journal_entry_id}, entry_date={existing.entry_date}, "
            f"amount_idr={existing.amount_idr}). Skipping."
        )
        return None

    if existing.entry_date != OLD_ENTRY_DATE or existing.amount_idr != expected_old_amount_idr:
        raise _StateMismatch(
            f"{label}: opening_balances row (account_id={account_id}) doesn't match the expected "
            f"ORIGINAL state (entry_date={OLD_ENTRY_DATE}, amount_idr={expected_old_amount_idr}) or the "
            f"expected CORRECTED state (entry_date={NEW_ENTRY_DATE}, amount_idr={new_amount_idr}) -- "
            f"found entry_date={existing.entry_date}, amount_idr={existing.amount_idr}. Refusing to "
            "touch data that doesn't match either expected state."
        )

    original_entry = conn.execute(
        select(journal_entries.c.reversed_by_id).where(journal_entries.c.id == existing.journal_entry_id)
    ).first()
    if original_entry is None:
        raise _StateMismatch(f"{label}: journal_entries id={existing.journal_entry_id} does not exist.")
    if original_entry.reversed_by_id is not None:
        raise _StateMismatch(
            f"{label}: journal_entry_id={existing.journal_entry_id} is already reversed "
            f"(reversed_by_id={original_entry.reversed_by_id}) but its opening_balances row "
            f"(id={existing.id}) was never removed/updated -- a partial/unknown state from a prior "
            "interrupted run. Investigate manually rather than guessing which way to resolve it."
        )

    memo = CORRECTION_MEMO_TEMPLATE.format(
        date=_dt.date.today().isoformat(),
        old_amount=f"Rp {expected_old_amount_idr:,.2f}",
        label=label,
        new_amount=f"Rp {new_amount_idr:,.2f}"
        + (f" / ${new_amount_usd_ref:,.2f} USD" if new_amount_usd_ref is not None else ""),
    )

    print(f"  {label}: reversing journal_entry_id={existing.journal_entry_id} (Rp {expected_old_amount_idr:,.2f} "
          f"dated {OLD_ENTRY_DATE})...")
    reversal_id = post_reversal_entry(
        conn, original_journal_entry_id=existing.journal_entry_id, entry_date=OLD_ENTRY_DATE, memo=memo
    )
    print(f"    reversed -> journal_entry_id={reversal_id}")

    conn.execute(delete(opening_balances).where(opening_balances.c.id == existing.id))
    print(f"    deleted stale opening_balances.id={existing.id}")

    new_entry_id = post_opening_balance(
        conn,
        account_type_code=account_type_code,
        entry_date=NEW_ENTRY_DATE,
        amount_idr=new_amount_idr,
        wallet_group_id=wallet_group_id,
        amount_usd_ref=new_amount_usd_ref,
        fx_rate_used=new_fx_rate_used,
        memo=memo,
    )
    print(f"    posted new opening balance -> journal_entry_id={new_entry_id}, "
          f"entry_date={NEW_ENTRY_DATE}, amount_idr={new_amount_idr}")

    return {"reversal_id": reversal_id, "new_entry_id": new_entry_id}


def _confirm_database_identity(engine) -> bool:
    """Forces an explicit, checkable confirmation of which real database is
    about to be committed to -- independent of the Drive preflight check
    above (STEP 0). QA's finding, 2026-10-01: Drive-readiness and DB-target
    identity are two independent signals that can desync -- exactly what
    happened in the real near-miss this guards against (a local
    DATABASE_URL pointed at a disposable local snapshot while the real
    Drive happened to already have real documents uploaded to it, and
    nothing forced an explicit check of the DB target before the commit
    proceeded).

    Shows the real host (not hidden -- a bare hostname isn't a credential,
    and it's the one piece of information that would have caught that
    near-miss immediately: ``localhost`` vs. the real droplet).

    Reads the confirmation via plain ``input()`` -- works identically for
    an interactive terminal or a piped/non-interactive stdin (e.g. a remote
    ``echo "dbname" | python3 scripts/backfill_jan_apr_2026.py`` over SSH).
    Explicitly tested both ways, including a real OS-level pipe (not just a
    mocked ``input``) -- see tests/test_backfill_jan_apr_2026.py.

    Never raises: a closed/empty stdin (EOFError) is treated as a
    non-match, same as any other wrong answer -- always returns a plain
    bool, never guesses in either direction.
    """
    db_name = engine.url.database
    print(f"About to COMMIT real changes to database {db_name!r} on host {engine.url.host!r}.")
    try:
        confirm = input(f"Type the database name ({db_name!r}) to confirm this is the intended target: ")
    except EOFError:
        print("No confirmation received (stdin closed/empty) -- treating this as a non-match.")
        return False
    return confirm.strip() == db_name


def _ensure_placeholder_kurs_rate(conn) -> None:
    existing = conn.execute(
        select(kurs_pajak_rates.c.rate_idr).where(kurs_pajak_rates.c.effective_date == PLACEHOLDER_KURS_EFFECTIVE_DATE)
    ).first()
    if existing is not None:
        if existing.rate_idr != PLACEHOLDER_KURS_RATE_IDR:
            raise _StateMismatch(
                f"A kurs_pajak_rates row already exists for {PLACEHOLDER_KURS_EFFECTIVE_DATE} with rate "
                f"{existing.rate_idr}, not the expected placeholder {PLACEHOLDER_KURS_RATE_IDR} -- this "
                "looks like a REAL rate may already have been seeded. Refusing to overwrite it. If this "
                "is a real rate, this script no longer needs to seed a placeholder at all -- remove this "
                "step and re-run."
            )
        print(f"  Kurs Pajak placeholder rate already seeded for {PLACEHOLDER_KURS_EFFECTIVE_DATE} "
              f"({existing.rate_idr}). Skipping.")
        return
    seed_kurs_pajak_rate(conn, effective_date=PLACEHOLDER_KURS_EFFECTIVE_DATE, rate_idr=PLACEHOLDER_KURS_RATE_IDR)
    print(f"  Seeded PLACEHOLDER Kurs Pajak rate {PLACEHOLDER_KURS_RATE_IDR} effective "
          f"{PLACEHOLDER_KURS_EFFECTIVE_DATE} (holds until the real 2026-04-27 rate already in the "
          "database takes over). NOT a real Kemenkeu rate -- see this script's module docstring, point 2.")


def _print_report_snapshot(conn, periods: list[_dt.date]) -> dict:
    snapshot = {}
    for pm in periods:
        eq = equity_report(conn, period_month=pm)
        pnl = pnl_report(conn, period_month=pm)
        snapshot[pm] = {
            "owners_capital": eq.owners_capital_idr,
            "retained_earnings": eq.retained_earnings_idr,
            "net_income_period": pnl.net_income_idr,
            "total_revenue": pnl.total_revenue_idr,
            "cogs": pnl.cogs_idr,
            "total_opex": pnl.total_opex_idr,
        }
        print(
            f"    {pm.isoformat()}: Owner's Capital=Rp {snapshot[pm]['owners_capital']:,.2f}  "
            f"Retained Earnings=Rp {snapshot[pm]['retained_earnings']:,.2f}  "
            f"Net Income (period)=Rp {snapshot[pm]['net_income_period']:,.2f}  "
            f"(Revenue=Rp {snapshot[pm]['total_revenue']:,.2f}, COGS=Rp {snapshot[pm]['cogs']:,.2f}, "
            f"OpEx=Rp {snapshot[pm]['total_opex']:,.2f})"
        )
    return snapshot


def main() -> int:
    root_folder_id = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID")
    if not root_folder_id:
        print("ABORT: GOOGLE_DRIVE_ROOT_FOLDER_ID is not set -- cannot reach Google Drive.", file=sys.stderr)
        return 1
    try:
        drive_client = DriveClient()
    except DriveCredentialError as exc:
        print(f"ABORT: Google Drive credentials are not configured: {exc}", file=sys.stderr)
        return 1

    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)\n")

    # --- Resolve the one specific target eBay account dynamically (never a
    # bare hardcoded id/an assumption that exactly one account exists --
    # see _resolve_target_ebay_account and TARGET_EBAY_ACCOUNT_DRIVE_FOLDER_
    # NAME above for why). -------------------------------------------------
    with engine.connect() as probe_conn:
        accounts = list_ebay_accounts(probe_conn)
    try:
        ebay_account = _resolve_target_ebay_account(accounts)
    except _StateMismatch as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 1
    print(f"Target eBay account: id={ebay_account.id} name={ebay_account.name!r} "
          f"wallet_group_id={ebay_account.wallet_group_id} "
          f"(Drive folders: {ebay_account.ebay_account_drive_folder_name_resolved!r} / "
          f"{ebay_account.wallet_group_drive_folder_name_resolved!r})\n")

    # --- Step 0: preflight -- real Drive documents must already be there. -
    print("=" * 100)
    print("STEP 0: preflight -- confirming real Jan-Apr 2026 documents exist in Google Drive")
    print("=" * 100)
    problems = _preflight_check_drive_documents(drive_client, root_folder_id, ebay_account)
    if problems:
        print("\nABORT: the real Google Drive folders are missing documents this script needs. Nothing "
              "has been touched in the database. Upload the following, then re-run:\n")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("  All 4 fixed-expectation document types found for all 4 periods (Jan-Apr 2026). Proceeding.\n")

    # --- DB identity confirmation -- independent of the Drive preflight
    # above (see _confirm_database_identity's own docstring for why this
    # exists as its own separate checkpoint, added 2026-10-01 per QA).
    print("=" * 100)
    print("DB IDENTITY CHECK -- independent of the Drive preflight above")
    print("=" * 100)
    if not _confirm_database_identity(engine):
        print("\nABORT: database identity was not confirmed -- nothing has been touched.", file=sys.stderr)
        return 1
    print()

    with engine.begin() as conn:
        print("=" * 100)
        print("STEP 1: BEFORE snapshot (May-Aug 2026, current live state)")
        print("=" * 100)
        _print_report_snapshot(conn, REPORT_PERIODS_MAY_AUG)

        print()
        print("=" * 100)
        print("STEP 2: reverse + repost the 3 opening-balance entries")
        print("=" * 100)
        _ensure_jan1_opening_balance(
            conn,
            label="BCA Main",
            account_type_code="BCA_MAIN",
            wallet_group_id=None,
            expected_old_amount_idr=Decimal("10102960.00"),
            new_amount_idr=Decimal("631809.61"),
        )
        _ensure_jan1_opening_balance(
            conn,
            label="BCA Bridging (wallet-group 1)",
            account_type_code="BCA_BRIDGING",
            wallet_group_id=ebay_account.wallet_group_id,
            expected_old_amount_idr=Decimal("66004188.00"),
            new_amount_idr=Decimal("59499.07"),
        )
        payoneer_new_amount_idr = round_idr(PAYONEER_JAN1_USD * PLACEHOLDER_KURS_RATE_IDR)
        _ensure_jan1_opening_balance(
            conn,
            label="Payoneer Wallet (wallet-group 1)",
            account_type_code="PAYONEER_WALLET",
            wallet_group_id=ebay_account.wallet_group_id,
            expected_old_amount_idr=Decimal("75483150.00"),
            new_amount_idr=payoneer_new_amount_idr,
            new_amount_usd_ref=PAYONEER_JAN1_USD,
            new_fx_rate_used=PLACEHOLDER_KURS_RATE_IDR,
        )

        print()
        print("=" * 100)
        print("STEP 3: seed the placeholder Kurs Pajak rate for 2026-01-01")
        print("=" * 100)
        _ensure_placeholder_kurs_rate(conn)

        print()
        print("=" * 100)
        print("STEP 4: run the real ingestion pipeline for Jan-Apr 2026 (real Google Drive)")
        print("=" * 100)
        for period_month in PERIODS:
            print(f"\n  --- Syncing period {period_month.isoformat()} ---")
            result = run_sync_for_period(
                conn,
                drive_client,
                root_folder_id=root_folder_id,
                period_month=period_month,
                ebay_account_id=ebay_account.id,
                ebay_account_folder_name=ebay_account.ebay_account_drive_folder_name_resolved,
                wallet_group_id=ebay_account.wallet_group_id,
                wallet_group_folder_name=ebay_account.wallet_group_drive_folder_name_resolved,
                master_folder_name=MASTER_FOLDER_NAME,
            )
            for step in result.steps:
                status = "found" if step.found_file else "NOT FOUND"
                print(f"    {step.document_type}: {status} (rows={step.row_count})")
                for w in step.warnings:
                    print(f"      WARNING: {w}")
            posted = result.posted.posted if result.posted else 0
            failed = result.posted.failed_to_post if result.posted else 0
            print(f"    posted={posted} failed_to_post={failed}")

        print()
        print("=" * 100)
        print("STEP 5: final global sweep (also posts the real, pre-existing May-Aug review-queue "
              "backlog -- approved by the user 2026-09-30)")
        print("=" * 100)
        final_match = matching.run_auto_match(conn)
        final_post = matching.post_pending_rows(conn)
        print(f"  run_auto_match: matched={final_match.matched} needs_review={final_match.needs_review}")
        print(f"  post_pending_rows: posted={final_post.posted} failed_to_post={final_post.failed_to_post}")

        print()
        print("=" * 100)
        print("STEP 6: AFTER snapshot")
        print("=" * 100)
        print("  May-Aug 2026 (corrected):")
        _print_report_snapshot(conn, REPORT_PERIODS_MAY_AUG)
        print("\n  Jan-Apr 2026 (new):")
        _print_report_snapshot(conn, PERIODS)

        print()
        print("=" * 100)
        print("STEP 7: Balance Sheet identity check (August 2026)")
        print("=" * 100)
        bs = balance_sheet_report(conn, period_month=_dt.date(2026, 8, 1))
        print(f"  total_assets_idr               = Rp {bs.total_assets_idr:,.2f}")
        print(f"  total_liabilities_idr           = Rp {bs.total_liabilities_idr:,.2f}")
        print(f"  total_equity_idr                = Rp {bs.total_equity_idr:,.2f}")
        print(f"  total_liabilities_and_equity_idr = Rp {bs.total_liabilities_and_equity_idr:,.2f}")
        print(f"  difference_idr                   = {bs.difference_idr}  (must be 0 for a clean commit)")
        if bs.difference_idr != 0:
            print("\n  ABORT: Balance Sheet identity does not hold -- rolling back this entire transaction "
                  "rather than committing an unbalanced ledger.")
            raise _StateMismatch(f"Balance Sheet identity failed: difference_idr={bs.difference_idr}")

        print()
        print("Everything above checks out -- committing this transaction now.")

    print("\nDone. This was a REAL commit (no rollback) -- re-run this script any time; every step is "
          "idempotent and will report 'already done'/'skipping' for anything already applied.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except _StateMismatch as exc:
        print(f"\nABORT: {exc}", file=sys.stderr)
        raise SystemExit(1)
