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
import hashlib
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


def _stable_description_hash(raw_description: str) -> str:
    """A deterministic digest of ``raw_description``, safe to embed in a
    persisted dedup key across separate process runs.

    BUG FIX (2026-09-02, found against the real live-Drive validation run —
    see docs/build-briefs): this previously used Python's built-in
    ``hash()``, which is **randomized per-process** by default
    (``PYTHONHASHSEED``) for every ``str``/``bytes`` object — deliberately,
    for hash-flooding DoS protection, per CPython's own docs. That's fine
    for an in-memory dict key, but catastrophic for a value persisted into
    ``review_queue.external_ref`` specifically to survive across re-syncs:
    every existing test only ever calls ``stage_raw_lines`` twice within
    ONE pytest process, where ``hash()`` of the same string IS stable (the
    seed is fixed once per process, not per call) — so this never failed a
    test. It only breaks the moment two DIFFERENT real processes ingest the
    same statement (e.g. a one-off script, then the Flask app; or, in real
    production, any two separate runs of a cron job / gunicorn worker
    restart) — each gets its own random seed, so the SAME bank line
    produces a DIFFERENT synthetic external_ref, silently defeating the
    ON CONFLICT DO NOTHING dedup in ``stage_raw_lines`` entirely. Confirmed
    concretely: a real second sync run (different process) against an
    already-fully-ingested period re-staged and re-posted 9 already-posted
    transactions (3 COGS purchases + 3 paired inter-account transfers) as
    brand-new rows — a real double-post, not a theoretical risk.

    ``hashlib.sha256`` is deterministic for the same input in any process,
    forever (no per-process seed) — exactly what a persisted dedup key
    needs. Truncated to 16 hex chars: still far more collision-resistant
    than the tolerances (``AMOUNT_TOLERANCE_IDR``/date) anything downstream
    of this key actually needs, while keeping ``external_ref`` short.
    """
    return hashlib.sha256(raw_description.encode("utf-8")).hexdigest()[:16]


def _synthetic_external_ref(source_document_id: int, line: RawLine) -> str:
    return (
        f"doc{source_document_id}:{line.transaction_date.isoformat()}:"
        f"{_stable_description_hash(line.raw_description)}:{line.amount_idr}:{line.occurrence_index or 1}"
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

    Three Purpose values route to three different review_queue categories
    (a third branch, 'general_operating_expense' -> 'operating_expense',
    added 2026-09 alongside Purpose's third value — see CLAUDE.md's Invoice
    & proof-of-purchase capture section). 'consignment_purchase' (and any
    other/unset purpose, matching the original two-value design's default)
    still falls through to 'consignment_payout' — unchanged behavior.
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
            if c.purpose == "cogs_purchase":
                category = "cogs_purchase"
            elif c.purpose == "general_operating_expense":
                category = "operating_expense"
            else:
                category = "consignment_payout"
            return category, "b", {"invoice_id": c.id}
    return None


def _try_rule_c_internal_transfer(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(c) match a known/expected inter-account transfer.

    FIX (2026-09-01, Main-agent brief "Bridging Account double-posting
    fix"): CLAUDE.md's Bridging Account correction (Money flow section,
    "Correction, 2026-09-01") establishes that the Bridging Account is NOT
    a pure pass-through — the amount that LANDS there (from a Payoneer
    withdrawal) and the amount that later SWEEPS OUT to BCA Main are two
    genuinely separate real-world events, for two genuinely different
    (rounded) numbers, not the same ``net_idr_landed`` figure. The old
    single check here matched ANY inflow or outflow against
    ``net_idr_landed`` alone, with no direction constraint and no
    already-consumed guard, which (a) let the Bridging statement's own
    landing-echo line (already posted via ``post_realized_fx_withdrawal``
    at Payoneer-CSV-ingestion time) wrongly re-match and phantom-post a
    SECOND transfer, and (b) never matched the real, differently-sized
    sweep at all — and crashed if a human manually labeled it, since
    ``_post_internal_transfer`` (old) re-derived the match against
    ``net_idr_landed`` and raised if nothing was found.

    Replaced with two genuinely distinct sub-checks, tried in this order —
    see each one's own docstring:
    """
    outcome = _try_rule_c_landing_echo(conn, row)
    if outcome is not None:
        return outcome
    return _try_rule_c_sweep_transfer(conn, row)


def _try_rule_c_landing_echo(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(c-landing) The Bridging Account statement's OWN line showing a
    Payoneer withdrawal LANDING — an inflow that's merely an echo of an
    event ALREADY posted (``post_realized_fx_withdrawal``, called directly
    from ``ingestion.payoneer`` when the Payoneer CSV + confirmation PDF
    were processed, well before this bank-statement line ever arrives).
    Recognizing this reconciles it for traceability but posts NOTHING NEW —
    the money movement is already in the ledger.

    Constrained to:
    - inflow lines (amount_idr > 0) on a wallet-group-scoped (Bridging)
      bank statement (wallet_group_id is not None) — a Master-statement
      line is never a landing echo, since the landing only ever touches the
      Bridging Account, never Main directly.
    - ``payoneer_withdrawals`` rows not already reconciled against a
      DIFFERENT Bridging line (``bridging_landing_reconciled_review_queue_id
      IS NULL``) — so the same withdrawal can't be claimed twice either.

    Amount/date tolerance is the same tight, shared threshold as everywhere
    else in this module — appropriate here since both sides are
    structured/near-simultaneous (confirmed against all 4 real Mandiri
    samples: the statement's landing line always lands the exact same date
    as the withdrawal confirmation, for the exact rounded IDR amount).
    """
    if row.wallet_group_id is None or row.amount_idr <= 0:
        return None
    candidates = conn.execute(
        select(
            payoneer_withdrawals.c.id,
            payoneer_withdrawals.c.net_idr_landed,
            payoneer_withdrawals.c.withdrawal_date,
        ).where(
            payoneer_withdrawals.c.wallet_group_id == row.wallet_group_id,
            payoneer_withdrawals.c.bridging_landing_reconciled_review_queue_id.is_(None),
        )
    ).all()
    for c in candidates:
        if abs(c.net_idr_landed - row.amount_idr) <= AMOUNT_TOLERANCE_IDR and abs(
            (row.transaction_date - c.withdrawal_date).days
        ) <= DATE_TOLERANCE_DAYS:
            return "internal_transfer_landing", "c-landing", {"payoneer_withdrawal_id": c.id}
    return None


def _try_rule_c_sweep_transfer(conn: Connection, row) -> tuple[str, str, dict] | None:
    """(c-sweep) The real, later, genuinely SEPARATE Bridging -> Main
    transfer — a rounded amount the business actually swept, which can be
    smaller OR larger than ``net_idr_landed`` (confirmed against all 4 real
    Mandiri months: e.g. a 66,928,398.00 landing swept out as
    66,930,000.00; an 85,361,499.00 landing swept out as 85,350,000.00) —
    never the same number, and never within this module's own Rp100
    tolerance of it. This check deliberately never references
    ``payoneer_withdrawals``/``net_idr_landed`` at all — it pairs a
    Bridging outflow line directly against its Master-statement inflow
    counterpart, by amount + date alone, exactly like reconciling two ends
    of the same real bank transfer.

    A row only qualifies as a "self" candidate for this check in its own
    expected direction: a Bridging-scoped (wallet_group_id is not None)
    OUTFLOW (the sweep leaving Bridging), or a Master-scoped
    (wallet_group_id is None) INFLOW (the same sweep landing in Main).

    The counterpart search allows a candidate that's either not yet
    classified (category IS NULL — the common case: both sides of a fresh
    sweep usually arrive in the same sync and get classified independently,
    each finding the other) OR already classified 'internal_transfer' (a
    previously auto-matched or human-labeled row still waiting on its pair
    — see ``_post_internal_transfer_sweep``'s no-crash handling for how
    that resolves once the pair does show up). Any OTHER category means
    that line was already explained as something else entirely and must
    never be stolen for a transfer it isn't part of.
    """
    if row.wallet_group_id is not None:
        if row.amount_idr >= 0:
            return None  # only the OUTFLOW leg of a Bridging-scoped line is a sweep-out candidate
        counterpart_where = (review_queue.c.wallet_group_id.is_(None)) & (review_queue.c.amount_idr > 0)
    else:
        if row.amount_idr <= 0:
            return None  # only the INFLOW leg of a Master-scoped line is a sweep-landing candidate
        counterpart_where = (review_queue.c.wallet_group_id.isnot(None)) & (review_queue.c.amount_idr < 0)

    candidates = conn.execute(
        select(review_queue.c.id, review_queue.c.amount_idr, review_queue.c.transaction_date).where(
            review_queue.c.id != row.id,
            review_queue.c.source_type == "bank_statement",
            counterpart_where,
            (review_queue.c.category.is_(None)) | (review_queue.c.category == "internal_transfer"),
        )
    ).all()
    for c in candidates:
        if abs(abs(c.amount_idr) - abs(row.amount_idr)) <= AMOUNT_TOLERANCE_IDR and abs(
            (row.transaction_date - c.transaction_date).days
        ) <= DATE_TOLERANCE_DAYS:
            return "internal_transfer", "c-sweep", {}
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
        if rule_name == "c-landing":
            values["linked_payoneer_withdrawal_id"] = extra["payoneer_withdrawal_id"]
        conn.execute(update(review_queue).where(review_queue.c.id == row.id).values(**values))
        if rule_name == "a":
            conn.execute(
                update(ebay_expected_payouts)
                .where(ebay_expected_payouts.c.id == extra["expected_payout_id"])
                .values(matched_at=_dt.datetime.now(_dt.timezone.utc))
            )
        if rule_name == "c-landing":
            # Claim this withdrawal immediately (same connection, same
            # sequential loop — no two rows in this pass can race for it)
            # so no OTHER Bridging line can also match it as a landing echo.
            conn.execute(
                update(payoneer_withdrawals)
                .where(payoneer_withdrawals.c.id == extra["payoneer_withdrawal_id"])
                .values(bridging_landing_reconciled_review_queue_id=row.id)
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
    # A row IS classified ('internal_transfer' — the c-sweep case) but its
    # pair hasn't shown up in review_queue yet (e.g. a human manually
    # labeled one side before the other was ever ingested). Not an error —
    # nothing in this system force-posts an unbalanced/unconfirmed transfer
    # (see CLAUDE.md). Left unposted; a later sync's post_pending_rows call
    # picks it up once the pair does arrive. See _post_internal_transfer.
    skipped_pending_pair: int = 0
    # A row IS classified, but its category is inherently directional (an
    # expense/purchase/payout/draw is always an outflow; a contribution/
    # revenue settlement is always an inflow) and the row's own raw
    # amount_idr sign disagrees — see _sign_mismatch_reason. Never posted;
    # flagged back to needs_review with a reason instead. Found against a
    # real historical bad entry (journal_entry_id=917 / review_queue.id=321
    # — see CLAUDE.md's Definition of done).
    skipped_sign_mismatch: int = 0


# Category -> the one real-world direction that category inherently implies,
# for every category where that's actually true. Deliberately excludes:
# - 'interest_income': legitimately bidirectional BY DESIGN (BUNGA credited
#   interest is an inflow; PAJAK BUNGA withheld tax on it is an outflow —
#   see ledger.posting.post_interest_income_line, which is already
#   sign-aware and never abs()'s its amount).
# - 'internal_transfer' / 'internal_transfer_landing': already have their
#   own narrower, structural direction guards (_post_internal_transfer
#   raises ValueError on a wrong-signed row; rule c-landing's own matching
#   query only ever considers inflow lines) — this dict would be redundant
#   for them, not additional safety.
# - 'other': deliberately NOT treated as inherently directional — unlike
#   every category below (each named for one specific, always-one-direction
#   real-world event), 'other' is this taxonomy's genuine catch-all for a
#   line that doesn't confidently fit anywhere else, and (2026-09-05, see
#   OTHER_INCOME in ledger/chart_of_accounts.py and _post_one_row's 'other'
#   branch below) is now explicitly, correctly BIDIRECTIONAL BY DESIGN, same
#   as 'interest_income' above: an inflow posts to Other Income, an outflow
#   posts to General Operating Expenses exactly as before. Forcing a single-
#   direction check on it would reject a legitimate real transaction in
#   whichever direction wasn't picked; this dict staying silent on 'other'
#   is still correct post-fix, not an oversight.
_DIRECTIONAL_CATEGORY_SIGNS: dict[str, str] = {
    "cogs_purchase": "outflow",
    "operating_expense": "outflow",
    "contract_labor": "outflow",
    "shipping_cost": "outflow",
    "consignment_payout": "outflow",
    "owners_draw": "outflow",
    "owners_contribution": "inflow",
    "revenue_settlement": "inflow",
}


def _sign_mismatch_reason(category: str, amount_idr: Decimal) -> str | None:
    """None if ``category`` isn't inherently directional, or its direction
    agrees with ``amount_idr``'s actual sign. Otherwise a human-readable
    reason to record on the row (see review_queue.sign_mismatch_reason).
    """
    expected = _DIRECTIONAL_CATEGORY_SIGNS.get(category)
    if expected is None:
        return None
    if expected == "outflow" and amount_idr >= 0:
        return (
            f"Category '{category}' is inherently an outflow (an expense, purchase, or "
            f"payout), but this line's amount ({amount_idr}) is a non-negative inflow. "
            "Not posted — please re-check the classification."
        )
    if expected == "inflow" and amount_idr <= 0:
        return (
            f"Category '{category}' is inherently an inflow, but this line's amount "
            f"({amount_idr}) is a non-positive outflow. Not posted — please re-check the "
            "classification."
        )
    return None


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


def _usd_reference_kwargs(row) -> dict:
    """Best-effort USD reference figure to thread through a generic
    (non-COGS/consignment-accrual) posting.

    QA-FOUND BUG FIX (2026-09): before this, none of post_operating_expense
    / post_consignor_reimbursement / post_interest_income_line ever
    received row.amount_usd_ref, even though it's sitting right there on
    the review_queue row for every payoneer_csv-sourced line (see
    ingestion/payoneer.py's generic-line staging). scheduling.fx_
    revaluation.compute_payoneer_wallet_balance sums amount_usd_ref across
    every non-fx_revaluation line touching a Payoneer Wallet account to
    determine its real USD balance — a Payoneer-wallet-paid expense/
    reimbursement/interest line posted with NO USD reference silently
    corrupted that sum (the IDR side dropped correctly, the USD side
    didn't move at all), producing a phantom gap that gets multiplied by
    the Kurs Pajak rate and posted as a fictitious Unrealized FX Gain/Loss
    at the next month-end revaluation. Reproduced and confirmed by QA.

    Same ``row.amount_usd_ref and (row.amount_idr / row.amount_usd_ref)``
    rate-derivation idiom already used by the 'revenue_settlement' branch
    below — safe (short-circuits to falsy/None rather than dividing by
    zero) and correct: ingestion/payoneer.py stores amount_idr and
    amount_usd_ref with the SAME sign for a given staged line, so their
    ratio is always the correct positive rate regardless of sign.

    QA-FOUND BUG FIX #2 (2026-09, rejected the first version of this
    function): ``amount_usd_ref`` is returned as an always-positive USD
    MAGNITUDE (``abs(row.amount_usd_ref)``), never the raw signed value —
    matching every existing convention in ledger/posting.py
    (post_ebay_sale, post_realized_fx_withdrawal,
    post_unrealized_fx_revaluation): direction is encoded structurally by
    which side (debit vs credit) the line sits on, never by the sign of
    amount_usd_ref itself. scheduling.fx_revaluation.compute_payoneer_
    wallet_balance's SQL (``sum(debit amount_usd_ref) - sum(credit
    amount_usd_ref)``) depends on that convention holding. The first
    version of this function passed ``row.amount_usd_ref`` through raw —
    for a Payoneer-wallet-paid OUTFLOW (the common real case, e.g. the
    OpenAI-subscription pattern), both ``row.amount_idr`` and
    ``row.amount_usd_ref`` are NEGATIVE per ingestion/payoneer.py's
    same-sign staging convention, and that negative value landed on the
    Payoneer Wallet's CREDIT line — since the credit term is already
    negated in the balance formula, subtracting a negative flipped the
    sign, so the expense was silently ADDED to the computed USD balance
    instead of subtracted (and by double the true amount). QA reproduced
    this concretely both via a direct post_operating_expense call and via
    the full ingestion pipeline. ``rate`` is unaffected by this fix and
    stays exactly as before — it's already a positive value whenever both
    inputs share a sign (which they always do here), so it does not need
    (and must not get) its own ``abs()``.

    A row from any OTHER source_type (bank_statement, etc.) never carries
    a real amount_usd_ref — this returns (None, None) for those, a
    harmless no-op identical to every other optional amount_usd_ref/
    fx_rate_used kwarg pair already used throughout ledger/posting.py.
    """
    rate = row.amount_usd_ref and (row.amount_idr / row.amount_usd_ref)
    magnitude = abs(row.amount_usd_ref) if row.amount_usd_ref is not None else None
    return {"amount_usd_ref": magnitude, "fx_rate_used": rate}


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
            review_queue.c.linked_payoneer_withdrawal_id,
        ).where(review_queue.c.posted_at.is_(None))
    ).all()

    for row in pending:
        if row.category is None:
            result.skipped_unclassified += 1
            continue  # never a silent best-guess post — CLAUDE.md rule 5

        mismatch_reason = _sign_mismatch_reason(row.category, row.amount_idr)
        if mismatch_reason is not None:
            # Never silently flip direction via abs() — see
            # PostResult.skipped_sign_mismatch. Flag back to needs_review
            # (covers both a human-mislabeled row, which is already
            # needs_review, and an auto-matched row via rule (e)'s
            # direction-blind keyword lookup, which needs pulling OUT of
            # 'matched' so a human actually sees it) with a visible reason,
            # and leave the row entirely unposted.
            conn.execute(
                update(review_queue)
                .where(review_queue.c.id == row.id)
                .values(match_status="needs_review", sign_mismatch_reason=mismatch_reason)
            )
            result.skipped_sign_mismatch += 1
            continue

        journal_entry_id = _post_one_row(conn, row)
        if journal_entry_id is None:
            # c-sweep, pair not found yet — see PostResult.skipped_pending_pair.
            result.skipped_pending_pair += 1
            continue
        conn.execute(
            update(review_queue)
            .where(review_queue.c.id == row.id)
            .values(
                posted_at=_dt.datetime.now(_dt.timezone.utc),
                posted_journal_entry_id=journal_entry_id,
                # Clear any earlier mismatch flag now that this row posted
                # correctly (e.g. a human fixed the category after seeing
                # the flag) — no stale reason left on a resolved row.
                sign_mismatch_reason=None,
            )
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


def _post_one_row(conn: Connection, row) -> int | None:
    """Returns the journal_entry_id this row should link to, or None if
    it's classified but genuinely not ready to post yet (see
    PostResult.skipped_pending_pair / _post_internal_transfer).
    """
    entry_date = row.transaction_date

    if row.category == "internal_transfer_landing":
        return _post_internal_transfer_landing(conn, row)

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
            **_usd_reference_kwargs(row),
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
            **_usd_reference_kwargs(row),
        ) if paying_code != "BCA_MAIN" else posting.post_cogs_purchase(
            conn, entry_date=entry_date, amount_idr=abs(row.amount_idr), memo=row.raw_description
        )

    if row.category == "operating_expense":
        if row.linked_invoice_id is not None:
            # Matched via rule (b) — a 'general_operating_expense' invoice
            # (see _try_rule_b_invoice). Always GENERAL_OPEX; deliberately
            # does NOT re-run the rule-(e) keyword lookup below, since that
            # keyword table is scoped to bank-line-DESCRIPTION heuristics
            # (rule e) and re-running it here against an invoice-matched
            # row's raw bank-line text could accidentally pick a different,
            # unrelated expense account if the description happens to
            # contain some other keyword — the invoice's own Purpose is the
            # authoritative signal for this row, not a coincidental keyword
            # hit on the bank line's description.
            expense_code = "GENERAL_OPEX"
        else:
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
            **_usd_reference_kwargs(row),
        )

    if row.category == "contract_labor":
        # A human reviewing a bank/invoice line selected "Contract Labor"
        # directly (see webapp/review_queue_bp.py's CATEGORY_OPTIONS) —
        # posts straight to its own dedicated CONTRACT_LABOR account via the
        # same generic post_operating_expense used for cogs_purchase/
        # operating_expense/other above, never GENERAL_OPEX's default.
        paying_code, paying_kwargs = _paying_account_for_row(row)
        return posting.post_operating_expense(
            conn,
            entry_date=entry_date,
            expense_account_type_code="CONTRACT_LABOR",
            amount_idr=abs(row.amount_idr),
            paying_account_type_code=paying_code,
            **paying_kwargs,
            **_usd_reference_kwargs(row),
        )

    if row.category == "shipping_cost":
        # A human reviewing a bank line selected "Shipping Cost" directly
        # (see webapp/review_queue_bp.py's CATEGORY_OPTIONS), or the
        # KURASI keyword rule (rule (e) — see ingestion/seed.py) auto-
        # matched it — posts straight to its own dedicated SHIPPING_COST
        # account (already in the chart of accounts, added 2026-08-31 for
        # the experimental consignment payout model) via the same generic
        # post_operating_expense used for cogs_purchase/operating_expense/
        # contract_labor/other above, never GENERAL_OPEX's default. Same
        # pattern as the CONTRACT_LABOR branch immediately above.
        paying_code, paying_kwargs = _paying_account_for_row(row)
        return posting.post_operating_expense(
            conn,
            entry_date=entry_date,
            expense_account_type_code="SHIPPING_COST",
            amount_idr=abs(row.amount_idr),
            paying_account_type_code=paying_code,
            **paying_kwargs,
            **_usd_reference_kwargs(row),
        )

    if row.category == "interest_income":
        # BUNGA (credit inflow) / PAJAK BUNGA (debit outflow) — see
        # ledger.posting.post_interest_income_line's docstring. Sign-aware,
        # unlike 'operating_expense' (which always assumes an outflow), so
        # amount_idr is passed through SIGNED here, not abs()'d.
        paying_code, paying_kwargs = _paying_account_for_row(row)
        return posting.post_interest_income_line(
            conn,
            entry_date=entry_date,
            amount_idr=row.amount_idr,
            paying_account_type_code=paying_code,
            memo=row.raw_description,
            **paying_kwargs,
            **_usd_reference_kwargs(row),
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
        # Bidirectional by design (2026-09-05 fix — see
        # _DIRECTIONAL_CATEGORY_SIGNS's note above and CLAUDE.md's Chart of
        # accounts, "Other Income" line): a human-labeled 'other' row can be
        # a real inflow or a real outflow, and each has its own correct
        # destination. Found against a real historical bad entry — a real
        # +Rp 50,000 inflow (the account owner moving his own money from a
        # personal DANA e-wallet into the Bridging Account) was wrongly
        # labeled 'cogs_purchase' and posted with its direction flipped (see
        # journal_entry_id=917 / review_queue.id=321); before this fix, even
        # a CORRECTLY-labeled 'other' inflow had nowhere to post but the
        # outflow-shaped path below, which would have silently flipped it
        # the same way.
        paying_code, paying_kwargs = _paying_account_for_row(row)
        if row.amount_idr > 0:
            # Genuine inflow that doesn't confidently fit any named category
            # — e.g. the owner's own money passing through a wallet-group's
            # bank account. A neutral pass-through, not an Owner's
            # Contribution (per Main-agent's 2026-09-05 decision) — posts to
            # OTHER_INCOME, the inflow-side counterpart to GENERAL_OPEX.
            return posting.post_income_line(
                conn,
                entry_date=entry_date,
                income_account_type_code="OTHER_INCOME",
                amount_idr=row.amount_idr,
                paying_account_type_code=paying_code,
                **paying_kwargs,
                **_usd_reference_kwargs(row),
            )
        # Outflow — unchanged from before this fix (regression-safe): still
        # posts to General Operating Expenses exactly as always.
        return posting.post_operating_expense(
            conn,
            entry_date=entry_date,
            expense_account_type_code="GENERAL_OPEX",
            amount_idr=abs(row.amount_idr),
            paying_account_type_code=paying_code,
            **paying_kwargs,
            **_usd_reference_kwargs(row),
        )

    raise ValueError(f"No posting handler for review_queue category {row.category!r}")


def _post_internal_transfer_landing(conn: Connection, row) -> int:
    """(c-landing) posting: never inserts a new journal entry. The money
    movement was already posted at Payoneer-CSV-ingestion time (see
    ``post_realized_fx_withdrawal``) — this just resolves the already
    -established link (``review_queue.linked_payoneer_withdrawal_id``, set
    by rule c-landing in ``run_auto_match``) to that existing entry, so
    ``post_pending_rows`` can record it as this row's ``posted_journal_entry_id``
    for traceability.
    """
    if row.linked_payoneer_withdrawal_id is None:
        # Should never happen via the normal auto-match path (rule c-landing
        # always sets this link atomically with the category) — this
        # category isn't meant to be a human-selectable review-queue label.
        # Defense in depth only: treat as "not ready" rather than crash, same
        # philosophy as the c-sweep "pending pair" case below.
        return None
    return conn.execute(
        select(payoneer_withdrawals.c.journal_entry_id).where(
            payoneer_withdrawals.c.id == row.linked_payoneer_withdrawal_id
        )
    ).scalar_one()


def _post_internal_transfer(conn: Connection, row) -> int | None:
    """(c-sweep) posting: pairs this row directly against its
    opposite-direction counterpart in review_queue (never against
    payoneer_withdrawals/net_idr_landed — see CLAUDE.md's 2026-09-01
    correction and ``_try_rule_c_sweep_transfer``). Whichever side is
    processed FIRST (within this call, or in an earlier sync run) posts the
    real transfer and its own row ends up with ``posted_journal_entry_id``
    set; the second side finds that already set and just links to it
    instead of posting again — same paired-transfer single-post guard as
    before, just matched against review_queue directly now.

    Returns None — posts nothing, YET — if no counterpart is found at all.
    This is an expected, non-error "still waiting for its pair" state (e.g.
    a human manually labeled one side 'internal_transfer' before the other
    side was ever ingested) — see PostResult.skipped_pending_pair. Nothing
    in this system force-posts an unconfirmed/unbalanced transfer.

    Hardening (found in adversarial self-review, 2026-09-01): without a
    "claimed" guard, two different candidate rows that both happen to
    plausibly match the SAME single real counterpart (amount+date within
    tolerance — e.g. two same-day, same-amount Bridging outflows and only
    one genuine Master-side inflow) could each post their own separate
    transfer for it, recreating the exact double-posting bug class this
    whole fix exists to close, one level up. Closing this needs TWO parts,
    not just a candidate-side exclusion filter:

    1. ``row``'s OWN current ``paired_review_queue_id`` is re-checked LIVE
       (never trusted from the caller's possibly-stale snapshot — it may
       have been set by the counterpart's own processing earlier in this
       very ``post_pending_rows`` call). If already claimed, this function
       goes STRAIGHT to that specific counterpart — no broader search at
       all — so a THIRD, coincidentally-matching row processed later can
       never cause an already-correctly-paired row to be re-paired with
       someone else (the exact scenario a naive "exclude already-claimed
       candidates" filter alone does NOT prevent, if the row initiating the
       search is itself the one with multiple plausible partners).
    2. Only once row is confirmed still unclaimed does the normal
       amount+date candidate search run, restricted to UNCLAIMED
       counterparts (``paired_review_queue_id IS NULL``) — claiming both
       sides immediately, before posting/linking.
    """
    current_pairing = conn.execute(
        select(review_queue.c.paired_review_queue_id).where(review_queue.c.id == row.id)
    ).scalar_one()
    if current_pairing is not None:
        return conn.execute(
            select(review_queue.c.posted_journal_entry_id).where(review_queue.c.id == current_pairing)
        ).scalar_one()  # None if the counterpart genuinely hasn't posted yet — never a crash

    if row.wallet_group_id is not None:
        if row.amount_idr >= 0:
            raise ValueError(
                f"review_queue row {row.id} is a Bridging-scoped 'internal_transfer' row with a "
                f"non-negative amount ({row.amount_idr}) — only an outflow should ever reach this "
                "category on the Bridging side; matching logic drifted."
            )
        counterpart_where = (review_queue.c.wallet_group_id.is_(None)) & (review_queue.c.amount_idr > 0)
    else:
        if row.amount_idr <= 0:
            raise ValueError(
                f"review_queue row {row.id} is a Master-scoped 'internal_transfer' row with a "
                f"non-positive amount ({row.amount_idr}) — only an inflow should ever reach this "
                "category on the Master side; matching logic drifted."
            )
        counterpart_where = (review_queue.c.wallet_group_id.isnot(None)) & (review_queue.c.amount_idr < 0)

    candidates = conn.execute(
        select(
            review_queue.c.id,
            review_queue.c.amount_idr,
            review_queue.c.transaction_date,
            review_queue.c.wallet_group_id,
            review_queue.c.posted_journal_entry_id,
        ).where(
            review_queue.c.id != row.id,
            review_queue.c.source_type == "bank_statement",
            review_queue.c.category == "internal_transfer",
            review_queue.c.paired_review_queue_id.is_(None),
            counterpart_where,
        )
    ).all()

    match = None
    for c in candidates:
        if abs(abs(c.amount_idr) - abs(row.amount_idr)) <= AMOUNT_TOLERANCE_IDR and abs(
            (row.transaction_date - c.transaction_date).days
        ) <= DATE_TOLERANCE_DAYS:
            match = c
            break

    if match is None:
        return None  # still waiting for its pair — see docstring, never a crash

    # Claim this pairing on BOTH sides now, before posting/linking — closes
    # the ambiguous-candidate race described in this function's docstring.
    conn.execute(update(review_queue).where(review_queue.c.id == row.id).values(paired_review_queue_id=match.id))
    conn.execute(update(review_queue).where(review_queue.c.id == match.id).values(paired_review_queue_id=row.id))

    if match.posted_journal_entry_id is not None:
        return match.posted_journal_entry_id

    wallet_group_id = row.wallet_group_id if row.wallet_group_id is not None else match.wallet_group_id
    return posting.post_inter_account_transfer(
        conn,
        entry_date=row.transaction_date,
        from_account_type_code="BCA_BRIDGING",
        to_account_type_code="BCA_MAIN",
        amount_idr=abs(row.amount_idr),
        from_wallet_group_id=wallet_group_id,
        memo="Periodic transfer from BCA Bridging Account to BCA Main Account",
    )
