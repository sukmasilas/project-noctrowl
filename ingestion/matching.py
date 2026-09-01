"""The shared auto-match priority engine.

Implements CLAUDE.md's Bank transaction classification rules (a)-(e), in
that exact priority order, over any staged raw line (Payoneer CSV or
bank-statement-PDF-sourced) — per
docs/design/milestone-3-ingestion-design.md §6.

Two separate concerns, deliberately kept as two separate functions (see
that design doc's §7):
- ``stage_raw_lines`` — ROW-CREATION idempotency (never duplicate a
  review_queue row on re-ingest).
- ``run_auto_match`` then ``post_pending_rows`` — classification, then
  POSTING idempotency (never post the same row twice, via
  ``review_queue.posted_at``).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from ingestion.schema import bank_keyword_rules, ebay_expected_payouts, invoice_journal_links, invoices, review_queue
from ledger import posting
from ledger.schema import consignment_sales, ebay_accounts, payoneer_withdrawals

# Concrete, documented thresholds — see design doc §4. Not "close enough";
# these are the exact numbers every rule below uses.
AMOUNT_TOLERANCE_IDR = Decimal("100")
DATE_TOLERANCE_DAYS = 3


@dataclass
class RawLine:
    transaction_date: _dt.date
    raw_description: str
    amount_idr: Decimal  # signed: positive = inflow/credit, negative = outflow/debit
    amount_usd_ref: Decimal | None = None
    external_ref: str | None = None  # e.g. a Payoneer Transaction ID; None for bank-PDF lines
    occurrence_index: int | None = None  # for synthesizing a dedup key when external_ref is None


def _synthetic_external_ref(source_document_id: int, line: RawLine) -> str:
    return (
        f"doc{source_document_id}:{line.transaction_date.isoformat()}:"
        f"{hash(line.raw_description)}:{line.amount_idr}:{line.occurrence_index or 1}"
    )


def stage_raw_lines(
    conn: Connection,
    *,
    source_type: str,
    source_document_id: int,
    lines: list[RawLine],
    wallet_group_id: int | None = None,
    ebay_account_id: int | None = None,
) -> list[int]:
    """Insert each line as a review_queue row, skipping any whose
    (source_type, external_ref) already exists — row-creation idempotency,
    per design doc §7. Every line always starts match_status='needs_review',
    category=NULL; ``run_auto_match`` classifies afterward as a separate
    pass, so staging and matching are independently testable.
    """
    created_ids: list[int] = []
    for line in lines:
        external_ref = line.external_ref or _synthetic_external_ref(source_document_id, line)
        stmt = (
            pg_insert(review_queue)
            .values(
                source_type=source_type,
                source_document_id=source_document_id,
                ebay_account_id=ebay_account_id,
                wallet_group_id=wallet_group_id,
                external_ref=external_ref,
                transaction_date=line.transaction_date,
                amount_idr=line.amount_idr,
                amount_usd_ref=line.amount_usd_ref,
                raw_description=line.raw_description,
                match_status="needs_review",
                match_rule=None,
                category=None,
            )
            .on_conflict_do_nothing(
                index_elements=[review_queue.c.source_type, review_queue.c.external_ref],
                index_where=review_queue.c.external_ref.isnot(None),
            )
            .returning(review_queue.c.id)
        )
        row = conn.execute(stmt).first()
        if row is not None:
            created_ids.append(row.id)
    return created_ids


@dataclass
class AutoMatchResult:
    matched: int = 0
    needs_review: int = 0


def _within_tolerance(a: Decimal, b: Decimal, date_a: _dt.date, date_b: _dt.date) -> bool:
    return abs(a - b) <= AMOUNT_TOLERANCE_IDR and abs((date_a - date_b).days) <= DATE_TOLERANCE_DAYS


def _try_rule_a_expected_payout(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(a) match an expected eBay payout. Applies to inflow lines only.
    Scoped to the row's wallet_group (via its eBay accounts) if set, else
    its own ebay_account_id.
    """
    if row.amount_idr <= 0 or row.amount_usd_ref is None:
        return None
    query = select(
        ebay_expected_payouts.c.id, ebay_expected_payouts.c.ebay_account_id, ebay_expected_payouts.c.net_amount_usd,
        ebay_expected_payouts.c.payout_date,
    ).where(ebay_expected_payouts.c.matched_at.is_(None))
    if row.wallet_group_id is not None:
        query = query.join(ebay_accounts, ebay_accounts.c.id == ebay_expected_payouts.c.ebay_account_id).where(
            ebay_accounts.c.wallet_group_id == row.wallet_group_id
        )
    elif row.ebay_account_id is not None:
        query = query.where(ebay_expected_payouts.c.ebay_account_id == row.ebay_account_id)
    else:
        return None

    for candidate in conn.execute(query).all():
        if abs(candidate.net_amount_usd - row.amount_usd_ref) <= Decimal("0.01") and _within_tolerance(
            row.amount_idr, row.amount_idr, row.transaction_date, candidate.payout_date
        ):
            return "revenue_settlement", "a", {"expected_payout_id": candidate.id, "ebay_account_id": candidate.ebay_account_id}
    return None


def _try_rule_b_invoice(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(b) match an uploaded invoice amount. Applies to outflow lines only
    (paying an invoice is money leaving). Only invoices with a non-NULL
    amount_idr participate, regardless of parsed/needs_confirmation status
    — see design doc §6.
    """
    if row.amount_idr >= 0:
        return None
    outflow = -row.amount_idr
    candidates = conn.execute(
        select(invoices.c.id, invoices.c.amount_idr, invoices.c.extracted_date, invoices.c.purpose).where(
            invoices.c.amount_idr.isnot(None)
        )
    ).all()
    for c in candidates:
        if c.extracted_date is None:
            continue
        if abs(c.amount_idr - outflow) <= AMOUNT_TOLERANCE_IDR and abs(
            (row.transaction_date - c.extracted_date).days
        ) <= DATE_TOLERANCE_DAYS:
            category = "cogs_purchase" if c.purpose == "cogs_purchase" else "consignment_payout"
            return category, "b", {"invoice_id": c.id}
    return None


def _try_rule_c_internal_transfer(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(c) match a known/expected inter-account transfer amount — sourced
    from payoneer_withdrawals.net_idr_landed (the Bridging-leg amount a
    withdrawal already landed). Matches either the outflow line (in the
    wallet-group's Bridging statement) or the inflow line (in the Master
    statement) — see design doc §6 for the paired single-post guard applied
    at posting time, not here.
    """
    target = abs(row.amount_idr)
    candidates = conn.execute(
        select(
            payoneer_withdrawals.c.id,
            payoneer_withdrawals.c.net_idr_landed,
            payoneer_withdrawals.c.withdrawal_date,
            payoneer_withdrawals.c.wallet_group_id,
        )
    ).all()
    for c in candidates:
        if abs(c.net_idr_landed - target) <= AMOUNT_TOLERANCE_IDR and abs(
            (row.transaction_date - c.withdrawal_date).days
        ) <= DATE_TOLERANCE_DAYS:
            return "internal_transfer", "c", {"payoneer_withdrawal_id": c.id}
    return None


def _try_rule_d_consignment_reimbursement(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(d) match a consignment reimbursement owed — a confirmed, posted
    consignment_sales row not yet reimbursed. Outflow lines only.
    """
    if row.amount_idr >= 0:
        return None
    outflow = -row.amount_idr
    candidates = conn.execute(
        select(consignment_sales.c.id, consignment_sales.c.payout_amount_idr, consignment_sales.c.consignor_item_ref).where(
            consignment_sales.c.confirmed_at.isnot(None),
            consignment_sales.c.journal_entry_id.isnot(None),
            consignment_sales.c.reimbursed_journal_entry_id.is_(None),
        )
    ).all()
    for c in candidates:
        if abs(c.payout_amount_idr - outflow) <= AMOUNT_TOLERANCE_IDR:
            return "consignment_payout", "d", {"consignment_sale_id": c.id, "consignor_item_ref": c.consignor_item_ref}
    return None


def _try_rule_e_keyword(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(e) recurring-description keyword rules — the lowest-confidence
    tier, case-insensitive substring containment only (no fuzzy scoring).
    """
    rules = conn.execute(
        select(bank_keyword_rules.c.keyword, bank_keyword_rules.c.category, bank_keyword_rules.c.expense_account_type_code)
        .where(bank_keyword_rules.c.is_active.is_(True))
    ).all()
    desc_upper = row.raw_description.upper()
    for r in rules:
        if r.keyword.upper() in desc_upper:
            return r.category, "e", {"expense_account_type_code": r.expense_account_type_code}
    return None


_RULES = [
    _try_rule_a_expected_payout,
    _try_rule_b_invoice,
    _try_rule_c_internal_transfer,
    _try_rule_d_consignment_reimbursement,
    _try_rule_e_keyword,
]


def run_auto_match(conn: Connection) -> AutoMatchResult:
    """One pass over every review_queue row that hasn't been classified yet
    (category IS NULL — covers rows freshly staged, but never re-touches a
    row a human already labeled or that's already posted). Rules run in
    CLAUDE.md's exact (a)-(e) priority order; the first one that fires wins.
    """
    result = AutoMatchResult()
    pending = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.transaction_date,
            review_queue.c.amount_idr,
            review_queue.c.amount_usd_ref,
            review_queue.c.raw_description,
            review_queue.c.wallet_group_id,
            review_queue.c.ebay_account_id,
        ).where(review_queue.c.category.is_(None))
    ).all()

    for row in pending:
        outcome = None
        for rule_fn in _RULES:
            outcome = rule_fn(conn, row)
            if outcome is not None:
                break

        if outcome is None:
            result.needs_review += 1
            continue

        category, rule_name, extra = outcome
        values = {"match_status": "matched", "match_rule": rule_name, "category": category}
        if rule_name == "a":
            values["ebay_account_id"] = extra["ebay_account_id"]
        if rule_name == "b":
            values["linked_invoice_id"] = extra["invoice_id"]
            values["consignor_item_ref"] = f"invoice:{extra['invoice_id']}"
        if rule_name == "d":
            values["consignor_item_ref"] = extra["consignor_item_ref"]
        conn.execute(update(review_queue).where(review_queue.c.id == row.id).values(**values))
        if rule_name == "a":
            conn.execute(
                update(ebay_expected_payouts)
                .where(ebay_expected_payouts.c.id == extra["expected_payout_id"])
                .values(matched_at=_dt.datetime.now(_dt.timezone.utc))
            )
        result.matched += 1

    return result


# ---------------------------------------------------------------------------
# Posting step — CLAUDE.md rules 4-6: matched rows post automatically,
# labeled needs_review rows post once labeled, a row never posts twice.
# ---------------------------------------------------------------------------


@dataclass
class PostResult:
    posted: int = 0
    skipped_unclassified: int = 0


def _paying_account_for_row(row) -> tuple[str, dict]:
    """Which asset account a generic (non-COGS/consignment) outflow was
    actually paid from, based on which feed the line came from and its
    scope — see design doc §6. Returned kwargs use the exact parameter
    names ``post_operating_expense``/``post_consignor_reimbursement``
    expect (``paying_wallet_group_id``/``paying_ebay_account_id``), so
    callers can always just do ``**paying_kwargs``.
    """
    if row.source_type == "payoneer_csv":
        return "PAYONEER_WALLET", {"paying_wallet_group_id": row.wallet_group_id}
    if row.wallet_group_id is not None:
        return "BCA_BRIDGING", {"paying_wallet_group_id": row.wallet_group_id}
    return "BCA_MAIN", {}


def post_pending_rows(conn: Connection) -> PostResult:
    """Post every review_queue row with category set and posted_at still
    NULL — covers both auto-matched rows (posted immediately, no human
    wait) and needs_review rows a human just labeled (picked up here on
    the "next sync", per CLAUDE.md rule 4). Never touches a row that
    already has posted_at set (rule 6).
    """
    result = PostResult()
    pending = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.source_type,
            review_queue.c.transaction_date,
            review_queue.c.amount_idr,
            review_queue.c.amount_usd_ref,
            review_queue.c.raw_description,
            review_queue.c.wallet_group_id,
            review_queue.c.ebay_account_id,
            review_queue.c.category,
            review_queue.c.consignor_item_ref,
            review_queue.c.linked_invoice_id,
        ).where(review_queue.c.posted_at.is_(None))
    ).all()

    for row in pending:
        if row.category is None:
            result.skipped_unclassified += 1
            continue  # never a silent best-guess post — CLAUDE.md rule 5

        journal_entry_id = _post_one_row(conn, row)
        conn.execute(
            update(review_queue)
            .where(review_queue.c.id == row.id)
            .values(posted_at=_dt.datetime.now(_dt.timezone.utc), posted_journal_entry_id=journal_entry_id)
        )
        if row.linked_invoice_id is not None:
            # QA fix (2026-09): the design doc's traceability chain for a
            # rule-(b) invoice match wasn't actually being written anywhere
            # — invoice_journal_links existed in the schema but nothing
            # ever inserted into it. This is the one place every
            # invoice-matched posting (COGS or consignment reimbursement)
            # passes through, so it's the right single place to write it.
            # Deliberately NOT re-adding a redundant back-reference column
            # on `invoices` itself (the original design sketch's
            # `matched_review_queue_id`) — `review_queue.linked_invoice_id`
            # already answers "which line matched this invoice" from the
            # review_queue side; this table answers "which journal entry did
            # this invoice justify", which is the traceability chain
            # CLAUDE.md's Definition of Done actually requires.
            conn.execute(
                invoice_journal_links.insert().values(
                    journal_entry_id=journal_entry_id, invoice_id=row.linked_invoice_id
                )
            )
        result.posted += 1

    return result


def _post_one_row(conn: Connection, row) -> int:
    entry_date = row.transaction_date

    if row.category == "internal_transfer":
        return _post_internal_transfer(conn, row)

    if row.category == "consignment_payout":
        amount_idr = abs(row.amount_idr)
        consignor_item_ref = row.consignor_item_ref or "unspecified"
        # If this matched a specific accrued consignment_sales row (rule d),
        # mark it reimbursed so it drops out of future rule-d candidates.
        cs_row = conn.execute(
            select(consignment_sales.c.id).where(
                consignment_sales.c.consignor_item_ref == consignor_item_ref,
                consignment_sales.c.reimbursed_journal_entry_id.is_(None),
                consignment_sales.c.confirmed_at.isnot(None),
            )
        ).first()
        paying_code, paying_kwargs = _paying_account_for_row(row)
        entry_id = posting.post_consignor_reimbursement(
            conn,
            entry_date=entry_date,
            amount_idr=amount_idr,
            consignor_item_ref=consignor_item_ref,
            paying_account_type_code=paying_code,
            **paying_kwargs,
        )
        if cs_row is not None:
            conn.execute(
                update(consignment_sales).where(consignment_sales.c.id == cs_row.id).values(reimbursed_journal_entry_id=entry_id)
            )
        return entry_id

    if row.category == "cogs_purchase":
        paying_code, paying_kwargs = _paying_account_for_row(row)
        return posting.post_operating_expense(
            conn,
            entry_date=entry_date,
            expense_account_type_code="COGS",
            amount_idr=abs(row.amount_idr),
            paying_account_type_code=paying_code,
            **paying_kwargs,
        ) if paying_code != "BCA_MAIN" else posting.post_cogs_purchase(
            conn, entry_date=entry_date, amount_idr=abs(row.amount_idr), memo=row.raw_description
        )

    if row.category == "operating_expense":
        outcome = _try_rule_e_keyword(conn, row)
        expense_code = outcome[2]["expense_account_type_code"] if outcome else "GENERAL_OPEX"
        paying_code, paying_kwargs = _paying_account_for_row(row)
        return posting.post_operating_expense(
            conn,
            entry_date=entry_date,
            expense_account_type_code=expense_code or "GENERAL_OPEX",
            amount_idr=abs(row.amount_idr),
            paying_account_type_code=paying_code,
            **paying_kwargs,
        )

    if row.category == "owners_draw":
        return posting.post_owner_draw(conn, entry_date=entry_date, amount_idr=abs(row.amount_idr), memo=row.raw_description)

    if row.category == "owners_contribution":
        return posting.post_owner_contribution(
            conn, entry_date=entry_date, amount_idr=abs(row.amount_idr), memo=row.raw_description
        )

    if row.category == "revenue_settlement":
        # Rare path — the common case (Payoneer CSV "Payment from eBay")
        # posts directly at ingestion time (see ingestion/payoneer.py) and
        # never touches review_queue at all. This only fires for a line
        # some OTHER feed matched against rule (a) generically.
        rate = row.amount_usd_ref and (row.amount_idr / row.amount_usd_ref)
        return posting.post_inter_account_transfer(
            conn,
            entry_date=entry_date,
            from_account_type_code="EBAY_WALLET",
            to_account_type_code="PAYONEER_WALLET",
            amount_idr=row.amount_idr,
            from_ebay_account_id=row.ebay_account_id,
            to_wallet_group_id=row.wallet_group_id,
            amount_usd_ref=row.amount_usd_ref,
            fx_rate_used=rate,
            memo=row.raw_description,
        )

    if row.category == "other":
        paying_code, paying_kwargs = _paying_account_for_row(row)
        return posting.post_operating_expense(
            conn,
            entry_date=entry_date,
            expense_account_type_code="GENERAL_OPEX",
            amount_idr=abs(row.amount_idr),
            paying_account_type_code=paying_code,
            **paying_kwargs,
        )

    raise ValueError(f"No posting handler for review_queue category {row.category!r}")


def _post_internal_transfer(conn: Connection, row) -> int:
    """The paired-transfer double-posting guard (design doc §6): find the
    matching payoneer_withdrawals row again; if its
    bridging_to_main_journal_entry_id is already set (a DIFFERENT
    review_queue row already posted this exact transfer), link to that
    existing entry instead of posting a second time.
    """
    target = abs(row.amount_idr)
    candidate = None
    for c in conn.execute(
        select(
            payoneer_withdrawals.c.id,
            payoneer_withdrawals.c.net_idr_landed,
            payoneer_withdrawals.c.withdrawal_date,
            payoneer_withdrawals.c.wallet_group_id,
            payoneer_withdrawals.c.bridging_to_main_journal_entry_id,
        )
    ).all():
        if abs(c.net_idr_landed - target) <= AMOUNT_TOLERANCE_IDR and abs(
            (row.transaction_date - c.withdrawal_date).days
        ) <= DATE_TOLERANCE_DAYS:
            candidate = c
            break

    if candidate is None:
        raise ValueError(
            f"review_queue row categorized internal_transfer but no matching payoneer_withdrawals "
            f"row found for amount={target} near {row.transaction_date} — matching/posting logic drifted."
        )

    if candidate.bridging_to_main_journal_entry_id is not None:
        return candidate.bridging_to_main_journal_entry_id

    entry_id = posting.post_inter_account_transfer(
        conn,
        entry_date=row.transaction_date,
        from_account_type_code="BCA_BRIDGING",
        to_account_type_code="BCA_MAIN",
        amount_idr=candidate.net_idr_landed,
        from_wallet_group_id=candidate.wallet_group_id,
        memo="Periodic transfer from BCA Bridging Account to BCA Main Account",
    )
    conn.execute(
        update(payoneer_withdrawals)
        .where(payoneer_withdrawals.c.id == candidate.id)
        .values(bridging_to_main_journal_entry_id=entry_id)
    )
    return entry_id
