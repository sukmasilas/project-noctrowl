"""Payoneer CSV export + withdrawal-confirmation-PDF parsing.

Implements docs/design/milestone-3-ingestion-design.md §3. Two Payoneer CSV
row shapes get special direct handling (never through the generic
review_queue matching engine — see module docstring below); everything else
is staged as a raw line for ``ingestion.matching`` to auto-match generically.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from ingestion.matching import RawLine, stage_raw_lines
from ingestion.schema import ebay_expected_payouts, payoneer_csv_posted_transactions, review_queue
from ledger import posting

_PAYOUT_ID_RE = re.compile(r"P\s*(\d+)")


def _parse_money(raw: str) -> Decimal:
    raw = (raw or "").strip()
    if not raw:
        return Decimal("0")
    return Decimal(raw.replace(",", ""))


def _already_posted_payoneer_row(conn: Connection, wallet_group_id: int, payoneer_transaction_id: str) -> int | None:
    """Posting idempotency (BUG FIX, QA 2026-09) — see
    ingestion/schema.py's payoneer_csv_posted_transactions docstring. Both
    direct-posting branches below (eBay-payment confirmation and withdrawal
    confirmation) must check this BEFORE doing anything, not just before
    the final posting call — the eBay-payment branch's own fallback logic
    would otherwise misclassify an already-posted row as Needs Review on a
    second sync run once its ebay_expected_payouts row is already consumed.
    """
    row = conn.execute(
        select(payoneer_csv_posted_transactions.c.journal_entry_id).where(
            payoneer_csv_posted_transactions.c.wallet_group_id == wallet_group_id,
            payoneer_csv_posted_transactions.c.payoneer_transaction_id == payoneer_transaction_id,
        )
    ).first()
    return row.journal_entry_id if row is not None else None


def _mark_payoneer_row_posted(conn: Connection, wallet_group_id: int, payoneer_transaction_id: str, journal_entry_id: int) -> None:
    conn.execute(
        payoneer_csv_posted_transactions.insert().values(
            wallet_group_id=wallet_group_id,
            payoneer_transaction_id=payoneer_transaction_id,
            journal_entry_id=journal_entry_id,
        )
    )


_REPORTS_STATEMENTS_DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3},?\s+\d{4}$")


def _normalize_reports_statements_row(row: dict[str, str]) -> dict[str, str]:
    """Convert a "Reports & Statements"-export row (columns: Date /
    Description / Amount / Currency / Status / Transaction ID — a single
    signed Amount, not separate Credit/Debit columns; Date as '28 May,
    2026', not MM/DD/YYYY) into the SAME canonical shape
    ``process_payoneer_rows``/``_process_ebay_payment_row`` already expect
    from the older "Transactions page" export (Transaction Date, Credit
    Amount, Debit Amount, Reference ID, Additional Description, Transaction
    ID, Status) — see module docstring's "two real CSV export shapes" note,
    added 2026-09 once a real ``report_*.csv`` sample (this second shape)
    was collected. Neither ``Reference ID`` nor ``Additional Description``
    exists in this export — left blank, which the existing amount+date
    fallback matching in ``_process_ebay_payment_row``/the withdrawal branch
    already handles (both already tolerate a missing/no-match id and fall
    back to amount+date, per the design doc — no new fallback path needed).
    """
    day, month_abbr, year = row["Date"].replace(",", "").split()
    txn_date = _dt.datetime.strptime(f"{day} {month_abbr} {year}", "%d %b %Y").date()
    amount = _parse_money(row["Amount"])
    return {
        "Transaction Date": txn_date.strftime("%m/%d/%Y"),
        "Description": row.get("Description", ""),
        "Credit Amount": str(amount) if amount > 0 else "",
        "Debit Amount": str(-amount) if amount < 0 else "",
        "Status": row.get("Status", ""),
        "Reference ID": "",
        "Additional Description": "",
        "Transaction ID": row.get("Transaction ID", ""),
    }


def parse_payoneer_csv_rows(text_content: str) -> list[dict[str, str]]:
    """Structured CSV. utf-8-sig strips a leading BOM if present (confirmed
    present in real samples of both formats).

    **Two real Payoneer CSV export shapes exist** (confirmed 2026-09 —
    CLAUDE.md itself names both: "Transactions page or Reports &
    Statements"). The original milestone-3 sample
    (``Payoneer_Transactions_04-2026.csv``) is the "Transactions page"
    export (Transaction Date/Credit Amount/Debit Amount/Reference ID/
    Additional Description columns). The real ``sample-documents/Payoneer/
    report_*.csv`` files collected afterward are the "Reports & Statements"
    export — a different column set entirely (Date/Amount single signed
    column/no Reference ID at all). Detected here by header shape (whichever
    is actually present dictates the branch — never assumed), normalized to
    one canonical row shape so ``process_payoneer_rows`` and everything
    downstream of it needs no format-awareness at all.

    Both real header rows also have trailing whitespace/tab characters on
    some column names and cell values (confirmed against the real "Reports &
    Statements" samples specifically, e.g. a header literally named
    ``"Transaction ID  "`` with trailing spaces, and values like
    ``"992116509\\t   "``) — every key and value is stripped before use.
    """
    reader = csv.DictReader(io.StringIO(text_content))
    raw_rows = [
        {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        for row in reader
        if any((v or "").strip() for v in row.values())
    ]
    if not raw_rows:
        return []

    if "Transaction Date" in raw_rows[0]:
        return raw_rows  # "Transactions page" export — already canonical shape
    return [_normalize_reports_statements_row(row) for row in raw_rows]


@dataclass
class WithdrawalConfirmation:
    transaction_id: str
    transfer_id: str
    date_time_utc: _dt.datetime
    amount_withdrawn_usd: Decimal
    fee_usd: Decimal
    exchange_rate_excl_fee: Decimal
    amount_sent_idr: Decimal
    beneficiary_bank: str | None


_CONFIRMATION_FIELD_PATTERNS = {
    "transaction_id": re.compile(r"Transaction ID\s+(\S+)"),
    "transfer_id": re.compile(r"Transfer ID\s+(\S+)"),
    "date_time": re.compile(r"Date/Time\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{2}:\d{2})"),
    "amount_withdrawn": re.compile(r"Amount withdrawn\s+([\d,]+\.\d{2})\s*USD"),
    "fee": re.compile(r"Fee\s+([\d,]+\.\d{2})\s*USD"),
    # 2-6 decimal digits, not 2-4: a real confirmation (Transfer ID
    # 4366185363529719, one of the Jan-Apr 2026 withdrawals) states its rate
    # to 5 decimal places ("1.00 USD = 16,643.35632 IDR") — found 2026-09-30
    # while backfilling Jan-Apr 2026 real data. The other 20 real
    # confirmations on file state 2-4 decimals, so this widens tolerance
    # rather than assuming a fixed precision Payoneer doesn't actually
    # guarantee. See test_parse_five_decimal_exchange_rate_confirmation_pdf.
    "exchange_rate": re.compile(r"Exchange rate \(excluding fee\)\s+1\.00\s*USD\s*=\s*([\d,]+\.\d{2,6})\s*IDR"),
    "amount_sent": re.compile(r"Amount sent\s+([\d,]+\.\d{2})\s*IDR"),
    "beneficiary_bank": re.compile(r"Beneficiary bank\s+(.+)"),
}


def parse_confirmation_text(text: str) -> WithdrawalConfirmation:
    """Label-based extraction — every figure is stated explicitly on its own
    line in the real sample (e.g. 'Fee 200.00 USD'), so this is a
    high-confidence structured read, not OCR guesswork, per CLAUDE.md's
    "use those stated figures directly rather than inferring them."
    """
    values = {}
    for key, pattern in _CONFIRMATION_FIELD_PATTERNS.items():
        m = pattern.search(text)
        if not m:
            raise ValueError(f"Could not find {key!r} in the withdrawal confirmation text")
        values[key] = m

    date_str, time_str = values["date_time"].group(1), values["date_time"].group(2)
    day, month, year = (int(p) for p in date_str.split("/"))
    hour, minute = (int(p) for p in time_str.split(":"))

    return WithdrawalConfirmation(
        transaction_id=values["transaction_id"].group(1),
        transfer_id=values["transfer_id"].group(1),
        date_time_utc=_dt.datetime(year, month, day, hour, minute, tzinfo=_dt.timezone.utc),
        amount_withdrawn_usd=_parse_money(values["amount_withdrawn"].group(1)),
        fee_usd=_parse_money(values["fee"].group(1)),
        exchange_rate_excl_fee=_parse_money(values["exchange_rate"].group(1)),
        amount_sent_idr=_parse_money(values["amount_sent"].group(1)),
        beneficiary_bank=values["beneficiary_bank"].group(1).strip(),
    )


def parse_confirmation_pdf(pdf_path_or_file) -> WithdrawalConfirmation:
    import pdfplumber  # lazy import, milestone-3-only dependency

    with pdfplumber.open(pdf_path_or_file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_confirmation_text(text)


@dataclass
class PayoneerIngestResult:
    revenue_settlements_posted: int = 0
    withdrawals_posted: int = 0
    staged_for_review: int = 0
    parse_warnings: list[str] = field(default_factory=list)
    # Added 2026-10-01 (incident fix) — see
    # _resolve_orphaned_review_queue_duplicate below. Counts a genuinely
    # different outcome from staged_for_review/revenue_settlements_posted:
    # an EARLIER sync pass staged this same "Payment from eBay" row as a
    # generic Needs-Review line (no expected payout known yet at the time),
    # and THIS pass's successful direct-posting match just resolved that
    # now-orphaned row rather than leaving it stuck forever or re-posting the
    # same money a second time.
    orphaned_duplicates_resolved: int = 0


def process_payoneer_rows(
    conn: Connection,
    *,
    wallet_group_id: int,
    source_document_id: int,
    rows: list[dict[str, str]],
    confirmations: list[WithdrawalConfirmation] | None = None,
    booking_rate_lookup,
) -> PayoneerIngestResult:
    """``booking_rate_lookup`` is a ``(conn, date) -> Decimal`` callable —
    injected rather than imported directly so tests can supply a fixed rate
    without needing a full kurs_pajak_rates fixture for every case; real
    callers pass ``ingestion.kurs_pajak.lookup_most_recent_rate_as_of``.
    """
    result = PayoneerIngestResult()
    confirmations = confirmations or []
    confirmations_by_transfer_id = {c.transfer_id: c for c in confirmations}
    used_confirmation_ids: set[str] = set()

    generic_lines: list[RawLine] = []

    for row in rows:
        if (row.get("Status") or "").strip() != "Completed":
            continue  # only completed transactions represent real cash events

        description = (row.get("Description") or "").strip()
        txn_date = _dt.datetime.strptime(row["Transaction Date"], "%m/%d/%Y").date()
        credit = _parse_money(row.get("Credit Amount", ""))
        debit = _parse_money(row.get("Debit Amount", ""))
        txn_id = (row.get("Transaction ID") or "").strip() or None

        if description == "Payment from eBay" and credit > 0:
            # Posting idempotency (BUG FIX, QA 2026-09): checked before the
            # ebay_expected_payouts lookup, not just before the final post —
            # otherwise a second sync run would find the payout already
            # matched_at-consumed from the FIRST run and misclassify this
            # already-posted row as a generic Needs Review line instead of
            # recognizing it as already handled.
            if txn_id is not None and _already_posted_payoneer_row(conn, wallet_group_id, txn_id) is not None:
                continue
            _process_ebay_payment_row(
                conn,
                row=row,
                wallet_group_id=wallet_group_id,
                entry_date=txn_date,
                credit_usd=credit,
                booking_rate_lookup=booking_rate_lookup,
                result=result,
                generic_lines=generic_lines,
                source_document_id=source_document_id,
                txn_id=txn_id,
            )
            continue

        if description.startswith("Withdrawal to") and debit > 0:
            # Posting idempotency (BUG FIX, QA 2026-09): this branch had NO
            # guard at all before — re-running the sync with the same CSV
            # row + confirmation PDF re-posted the same realized-FX
            # withdrawal a second time. Checked first, before any
            # confirmation-matching work.
            if txn_id is not None and _already_posted_payoneer_row(conn, wallet_group_id, txn_id) is not None:
                continue

            ref_id = (row.get("Reference ID") or "").strip().lstrip("#") or None
            confirmation = confirmations_by_transfer_id.get(ref_id) if ref_id else None
            if confirmation is None:
                # Fall back to amount+date matching against any unused
                # confirmation — the design doc's documented ±3-day / ±Rp100
                # (here: USD cents) tolerance, since a Reference ID isn't
                # guaranteed to be parseable/comparable in every real export.
                for c in confirmations:
                    if c.transfer_id in used_confirmation_ids:
                        continue
                    if abs(c.amount_withdrawn_usd - debit) <= Decimal("0.01") and abs(
                        (c.date_time_utc.date() - txn_date).days
                    ) <= 3:
                        confirmation = c
                        break
            if confirmation is None:
                result.parse_warnings.append(
                    f"Withdrawal row on {txn_date} for {debit} USD (Reference ID {ref_id!r}) has no "
                    "matching withdrawal confirmation document — not posted, needs the confirmation "
                    "PDF ingested before this can be booked (fee/exchange-rate figures must come from "
                    "it, never inferred)."
                )
                continue

            used_confirmation_ids.add(confirmation.transfer_id)
            booking_rate = booking_rate_lookup(conn, confirmation.date_time_utc.date())
            entry_id = posting.post_realized_fx_withdrawal(
                conn,
                wallet_group_id=wallet_group_id,
                entry_date=confirmation.date_time_utc.date(),
                gross_usd=confirmation.amount_withdrawn_usd,
                payoneer_fee_usd=confirmation.fee_usd,
                exchange_rate_excl_fee=confirmation.exchange_rate_excl_fee,
                booking_rate_used_idr=booking_rate,
                memo=f"Payoneer withdrawal {confirmation.transfer_id} to {confirmation.beneficiary_bank}",
            )
            if txn_id is not None:
                _mark_payoneer_row_posted(conn, wallet_group_id, txn_id, entry_id)
            result.withdrawals_posted += 1
            continue

        # Anything else (a Payoneer-charged fee, an uncategorized credit/debit,
        # etc.) — stage generically for the shared auto-match engine.
        signed_amount_usd = credit if credit > 0 else -debit
        if signed_amount_usd == 0:
            continue
        rate = booking_rate_lookup(conn, txn_date)
        generic_lines.append(
            RawLine(
                transaction_date=txn_date,
                raw_description=description or "(no description)",
                amount_idr=posting.round_idr(signed_amount_usd * rate),
                amount_usd_ref=signed_amount_usd,
                external_ref=txn_id,
            )
        )

    if generic_lines:
        staged_ids = stage_raw_lines(
            conn,
            source_type="payoneer_csv",
            source_document_id=source_document_id,
            wallet_group_id=wallet_group_id,
            ebay_account_id=None,
            lines=generic_lines,
        )
        result.staged_for_review += len(staged_ids)

    return result


def _resolve_orphaned_review_queue_duplicate(
    conn: Connection,
    *,
    wallet_group_id: int,
    txn_id: str | None,
    journal_entry_id: int,
) -> int | None:
    """INCIDENT FIX (2026-10-01): closes a real orphaned-review_queue-row gap
    found during the real Jan-Apr 2026 backfill — see Main-agent's brief for
    the full root-cause diagnosis. Summary: the same combined multi-month
    Payoneer CSV, present in every month's Drive folder, gets reprocessed in
    full on EVERY period's sync pass (``ingestion.sync.run_sync_for_period``
    runs sequentially, Jan -> Feb -> Mar -> Apr). A given "Payment from eBay"
    row for a LATER month can get processed during an EARLIER month's pass —
    at that point its matching ``ebay_expected_payouts`` row (populated from
    that LATER month's own eBay sales CSV `Payout` row) doesn't exist yet, so
    ``_process_ebay_payment_row`` falls through to generic Needs-Review
    staging (see its ``expected is None`` branch) and creates a real
    ``review_queue`` row for it, keyed by this row's own Payoneer Transaction
    ID (``external_ref``).

    Once the real month's OWN pass later runs, its eBay CSV IS ingested, so
    the SAME Payoneer CSV row (reprocessed again in that pass, since the
    identical full file sits in every month's folder) now finds a match and
    posts correctly via the DIRECT path (``post_inter_account_transfer``,
    never touching review_queue for the success case at all — see this
    module's docstring). Before this fix, nothing ever went back to resolve
    the EARLIER pass's now-stale orphaned row — the real transaction posted
    exactly once, correctly, but a permanent, un-postable, un-resolvable
    "Needs Review" artifact was left behind for it, with a live risk that a
    human manually posting it through the normal Review Queue flow would
    double-post the same money a second time.

    Called from the SUCCESS branch of ``_process_ebay_payment_row`` (after
    the real transfer is posted) — looks for a pre-existing, UNTOUCHED
    orphan row (``category IS NULL AND posted_at IS NULL`` — i.e. nothing a
    human or any other process has already acted on) with the same
    ``(source_type='payoneer_csv', external_ref=txn_id)`` key the row-
    creation idempotency in ``ingestion.matching.stage_raw_lines`` already
    uses, and resolves it: ``match_status='resolved_duplicate'``,
    ``duplicate_of_journal_entry_id`` pointing at the REAL entry,
    ``posted_at`` set (so ``post_pending_rows``' ``WHERE posted_at IS NULL``
    selection never picks this row up again), but deliberately
    ``posted_journal_entry_id`` left NULL — this row itself never posted
    anything; see ingestion/schema.py's column docstring for why that
    distinction matters. ``category`` is set to 'revenue_settlement' (an
    honest, accurate label for what the underlying transaction actually was)
    — safe to set alongside ``posted_at`` specifically because
    ``post_pending_rows`` only ever considers ``posted_at IS NULL`` rows, so
    a non-NULL category here can never trigger a second posting attempt.

    Returns the resolved row's id, or None if no untouched orphan exists for
    this transaction (the overwhelmingly common case — most "Payment from
    eBay" rows match on their very first processing pass and never create a
    review_queue row at all).

    Deliberately does NOT touch a row that already has a category or
    posted_at set (i.e. a human or some other process already acted on it
    before this fix shipped, or in some other unanticipated ordering) —
    silently "fixing" that would risk masking a genuine double-post that
    needs human eyes, not an automatic rewrite. See
    scripts/resolve_orphaned_payoneer_duplicates.py for the one-off,
    state-verifying cleanup of the specific rows already affected in
    production before this fix existed.
    """
    if txn_id is None:
        return None
    row = conn.execute(
        select(review_queue.c.id).where(
            review_queue.c.source_type == "payoneer_csv",
            review_queue.c.external_ref == txn_id,
            review_queue.c.wallet_group_id == wallet_group_id,
            review_queue.c.category.is_(None),
            review_queue.c.posted_at.is_(None),
        )
    ).first()
    if row is None:
        return None

    conn.execute(
        update(review_queue)
        .where(review_queue.c.id == row.id)
        .values(
            match_status="resolved_duplicate",
            category="revenue_settlement",
            posted_at=_dt.datetime.now(_dt.timezone.utc),
            duplicate_of_journal_entry_id=journal_entry_id,
            resolution_note=(
                "Resolved automatically (2026-10-01 incident fix): this row was originally "
                "staged as a generic Needs-Review line because no matching expected eBay "
                "payout existed yet when this period's Payoneer CSV was first processed — a "
                "known cross-period re-processing gap (see CLAUDE.md / the Jan-Apr 2026 "
                "backfill incident). The real underlying transaction has already posted "
                f"correctly, exactly once, as journal_entry_id={journal_entry_id} (an "
                "inter-account transfer from the eBay Wallet to the Payoneer Wallet), once "
                "the matching eBay Payout row was ingested in a later sync pass. This row "
                "itself was never posted and never will be — it is a duplicate artifact only, "
                "kept here, unposted, for traceability."
            ),
            sign_mismatch_reason=None,
            missing_reference_reason=None,
            posting_error_reason=None,
        )
    )
    return row.id


def _process_ebay_payment_row(
    conn: Connection,
    *,
    row: dict[str, str],
    wallet_group_id: int,
    entry_date: _dt.date,
    credit_usd: Decimal,
    booking_rate_lookup,
    result: PayoneerIngestResult,
    generic_lines: list[RawLine],
    source_document_id: int,
    txn_id: str | None,
) -> None:
    additional_description = (row.get("Additional Description") or "").strip()
    m = _PAYOUT_ID_RE.search(additional_description)
    payout_id = m.group(1) if m else None

    expected = None
    if payout_id:
        expected = conn.execute(
            select(
                ebay_expected_payouts.c.id,
                ebay_expected_payouts.c.ebay_account_id,
                ebay_expected_payouts.c.net_amount_usd,
            ).where(
                ebay_expected_payouts.c.ebay_payout_id == payout_id,
                ebay_expected_payouts.c.matched_at.is_(None),
            )
        ).first()

    if expected is None:
        # Fall back to amount+date matching within the tolerance the design
        # doc documents (±3 days, ±0.01 USD) across ANY of this wallet
        # -group's eBay accounts' unmatched expected payouts — needed since
        # a bank-PDF-sourced line (or a CSV export without a parseable
        # payout id) won't carry the id text at all.
        from ledger.schema import ebay_accounts

        candidates = conn.execute(
            select(
                ebay_expected_payouts.c.id,
                ebay_expected_payouts.c.ebay_account_id,
                ebay_expected_payouts.c.net_amount_usd,
                ebay_expected_payouts.c.payout_date,
            )
            .join(ebay_accounts, ebay_accounts.c.id == ebay_expected_payouts.c.ebay_account_id)
            .where(
                ebay_accounts.c.wallet_group_id == wallet_group_id,
                ebay_expected_payouts.c.matched_at.is_(None),
            )
        ).all()
        for c in candidates:
            if abs(c.net_amount_usd - credit_usd) <= Decimal("0.01") and abs((c.payout_date - entry_date).days) <= 3:
                expected = c
                break

    if expected is None:
        # No known payout to confirm — per CLAUDE.md's explicit Prototype
        # scope note, this is EXPECTED (not a bug) when the wallet-group's
        # Payoneer export contains a settlement for a not-yet-onboarded
        # sibling eBay account. Stage generically -> falls to Needs Review.
        rate = booking_rate_lookup(conn, entry_date)
        generic_lines.append(
            RawLine(
                transaction_date=entry_date,
                raw_description=f"Payment from eBay (Additional Description: {additional_description!r}, no matching expected payout)",
                amount_idr=posting.round_idr(credit_usd * rate),
                amount_usd_ref=credit_usd,
                external_ref=txn_id,
            )
        )
        return

    rate = booking_rate_lookup(conn, entry_date)
    entry_id = posting.post_inter_account_transfer(
        conn,
        entry_date=entry_date,
        from_account_type_code="EBAY_WALLET",
        to_account_type_code="PAYONEER_WALLET",
        amount_idr=posting.round_idr(credit_usd * rate),
        from_ebay_account_id=expected.ebay_account_id,
        to_wallet_group_id=wallet_group_id,
        amount_usd_ref=credit_usd,
        fx_rate_used=rate,
        memo=f"eBay payout {payout_id or ''} confirmed arrived in Payoneer".strip(),
    )
    conn.execute(
        ebay_expected_payouts.update()
        .where(ebay_expected_payouts.c.id == expected.id)
        .values(matched_at=_dt.datetime.now(_dt.timezone.utc))
    )
    if txn_id is not None:
        _mark_payoneer_row_posted(conn, wallet_group_id, txn_id, entry_id)
    # INCIDENT FIX (2026-10-01): resolve any earlier-pass orphaned
    # Needs-Review row for this exact transaction now that it's confirmed to
    # have posted correctly here — see
    # _resolve_orphaned_review_queue_duplicate's docstring for the full
    # cross-period scenario this closes.
    if _resolve_orphaned_review_queue_duplicate(
        conn, wallet_group_id=wallet_group_id, txn_id=txn_id, journal_entry_id=entry_id
    ) is not None:
        result.orphaned_duplicates_resolved += 1
    result.revenue_settlements_posted += 1
