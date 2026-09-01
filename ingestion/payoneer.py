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

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ingestion.matching import RawLine, stage_raw_lines
from ingestion.schema import ebay_expected_payouts, payoneer_csv_posted_transactions
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


def parse_payoneer_csv_rows(text_content: str) -> list[dict[str, str]]:
    """Structured CSV — no header-locating tricks needed, unlike the eBay
    export. utf-8-sig strips a leading BOM if present (confirmed present in
    the real sample).
    """
    reader = csv.DictReader(io.StringIO(text_content))
    return [row for row in reader if any(v.strip() for v in row.values())]


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
    "exchange_rate": re.compile(r"Exchange rate \(excluding fee\)\s+1\.00\s*USD\s*=\s*([\d,]+\.\d{2,4})\s*IDR"),
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
    result.revenue_settlements_posted += 1
