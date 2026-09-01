"""eBay Seller Hub Transaction Report CSV parser + ingestion.

Implements docs/design/milestone-3-ingestion-design.md §2, confirmed against
the real sample `sample-documents/ebay-sales-export/
Transaction_report_20260701_20260731.csv`. This is a **transaction report**
(rows typed Order/Refund/Hold/Other fee/Payout), not a simple orders list —
see the module-level branch-on-Type logic in ``process_transaction_report``.

**Real-sample discovery beyond what the design doc anticipated**: a single
eBay order can span MULTIPLE 'Order'-typed CSV rows when it contains more
than one line item. Confirmed against 4 of the real sample's 100 Order rows
(e.g. Order 05-14932-33362 spans 4 rows). One row per group carries the
order-level Gross transaction amount/Net amount (with Item
subtotal/Shipping/Custom label/fee columns all blank); the rest carry their
own Item subtotal/Shipping/Custom label/fee columns (with Gross transaction
amount blank). Rows must be grouped by Order number and merged before
reconciliation/posting — see ``_group_order_rows``/``_merge_order_group``.
This wasn't caught in the Phase A design doc's read of the sample (which
only inspected a handful of rows); it's a genuine parsing-structure fix, not
an accounting-policy question, so it's implemented directly rather than
flagged back as an open question — but it IS called out explicitly in the
milestone report as a design-doc correction.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from ingestion.kurs_pajak import lookup_kurs_pajak_rate
from ingestion.schema import ebay_csv_posted_transactions, ebay_expected_payouts, review_queue
from ledger import posting
from ledger.consignment import calc_tier_payout_usd, lookup_tier_rate
from ledger.schema import consignment_sales

# Columns that make up eBay's own take on an Order row — deliberately
# excludes "Charity donation" (a seller-elected donation, not an eBay fee —
# see design doc open question 8 / CLAUDE.md's "Other eBay Wallet debits"
# note).
_EBAY_FEE_COLUMNS = [
    "Final Value Fee - fixed",
    "Final Value Fee - variable",
    "Regulatory operating fee",
    'Very high "item not as described" fee',
    "Below standard performance fee",
    "International fee",
    "Deposit processing fee",
]

_RECONCILIATION_TOLERANCE_USD = Decimal("0.01")  # applied before FX conversion

_DATE_FORMATS = ("%b %d, %Y",)  # e.g. "Jul 31, 2026"


def _parse_money(raw: str | None) -> Decimal:
    """eBay CSV money fields: '--' = 0/blank, thousands-comma-separated,
    optionally negative. Never a float.
    """
    if raw is None:
        return Decimal("0")
    raw = raw.strip()
    if raw in ("", "--"):
        return Decimal("0")
    raw = raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse eBay CSV money field {raw!r}") from exc


def _clean_ref(raw: str | None) -> str | None:
    """eBay CSV uses '--' as its blank placeholder for ID/reference fields
    too, not just money fields — confirmed against the real sample: a
    multi-item order's "order total" row has Transaction ID='--' and
    Custom label='--'.
    """
    if raw is None:
        return None
    raw = raw.strip()
    return None if raw in ("", "--") else raw


def _parse_ebay_date(raw: str) -> _dt.date:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not parse eBay CSV date {raw!r} (expected e.g. 'Jul 31, 2026')")


def parse_ebay_csv_rows(text_content: str) -> tuple[list[str], list[dict[str, str]]]:
    """Locate the real header row by CONTENT (not a hardcoded line number —
    the notes/metadata block preceding it isn't a format guarantee) and
    return (header, data_rows) as plain dicts of raw strings.
    """
    reader = csv.reader(io.StringIO(text_content))
    all_rows = list(reader)

    header_idx = None
    for i, row in enumerate(all_rows):
        if row and row[0].strip() == "Transaction creation date":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            "Could not locate the eBay Transaction Report header row "
            "(expected a row starting with 'Transaction creation date')"
        )

    header = all_rows[header_idx]
    data_rows = []
    for row in all_rows[header_idx + 1 :]:
        if not row or all(not cell.strip() for cell in row):
            continue
        # Pad/truncate defensively to header length so dict(zip(...)) never
        # silently drops trailing columns on a short row.
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        data_rows.append(dict(zip(header, row)))
    return header, data_rows


@dataclass
class EbayIngestResult:
    orders_posted: int = 0
    refunds_posted: int = 0
    other_fees_posted: int = 0
    holds_skipped: int = 0
    payouts_recorded: int = 0
    consignment_sales_created: int = 0
    review_queue_rows_created: int = 0
    parse_warnings: list[str] = field(default_factory=list)


def _make_review_queue_row(
    conn: Connection,
    *,
    source_document_id: int,
    ebay_account_id: int,
    transaction_date: _dt.date,
    amount_idr: Decimal,
    amount_usd_ref: Decimal | None,
    raw_description: str,
    external_ref: str | None,
) -> int | None:
    """Row-creation idempotency (BUG FIX, QA 2026-09): this used to be a
    plain INSERT with no ON CONFLICT handling — re-running the eBay CSV
    sync for an unchanged period would either duplicate this row (if
    external_ref was None) or, more likely, crash the whole sync with an
    IntegrityError (if external_ref WAS set and collided with the unique
    index — the actual, worse failure mode this had in practice). Now
    matches ingestion.matching.stage_raw_lines' own idempotent-insert
    pattern exactly. Returns None (not an error) if this exact row already
    existed — callers that were counting "rows created" should only count a
    non-None return.
    """
    stmt = (
        pg_insert(review_queue)
        .values(
            source_type="ebay_sales_csv",
            source_document_id=source_document_id,
            ebay_account_id=ebay_account_id,
            wallet_group_id=None,
            external_ref=external_ref,
            transaction_date=transaction_date,
            amount_idr=amount_idr,
            amount_usd_ref=amount_usd_ref,
            raw_description=raw_description,
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
    return row.id if row is not None else None


def _already_posted_ebay_csv_row(conn: Connection, ebay_account_id: int, row_key: str) -> int | None:
    """Posting idempotency (BUG FIX, QA 2026-09) for the eBay CSV's DIRECT
    -posting rows (Order/Refund/Other fee — Payout rows already had their
    own guard via ebay_expected_payouts' unique index). See
    ingestion/schema.py's ebay_csv_posted_transactions table docstring for
    the full story: this table didn't exist before QA's review found
    re-running ingestion.sync.run_sync_for_period on an unchanged period
    silently re-posted every sale/refund/fee a second time.
    """
    row = conn.execute(
        select(ebay_csv_posted_transactions.c.journal_entry_id).where(
            ebay_csv_posted_transactions.c.ebay_account_id == ebay_account_id,
            ebay_csv_posted_transactions.c.row_key == row_key,
        )
    ).first()
    return row.journal_entry_id if row is not None else None


def _mark_ebay_csv_row_posted(conn: Connection, ebay_account_id: int, row_key: str, journal_entry_id: int) -> None:
    conn.execute(
        ebay_csv_posted_transactions.insert().values(
            ebay_account_id=ebay_account_id, row_key=row_key, journal_entry_id=journal_entry_id
        )
    )


# ---------------------------------------------------------------------------
# Order-row grouping/merging — see the module docstring's "real-sample
# discovery" note above for why this exists.
# ---------------------------------------------------------------------------


def _group_order_rows(rows: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """Group Type='Order' rows by Order number, preserving first-seen order."""
    order_numbers_seen: list[str] = []
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        key = row.get("Order number") or ""
        if key not in groups:
            groups[key] = []
            order_numbers_seen.append(key)
        groups[key].append(row)
    return [groups[k] for k in order_numbers_seen]


@dataclass
class MergedOrder:
    entry_date: _dt.date
    order_number: str | None
    currency: str
    gross: Decimal
    item_subtotal_sum: Decimal
    shipping_sum: Decimal
    charity: Decimal
    ebay_fee_usd: Decimal
    custom_labels: list[str]
    item_titles: list[str]
    txn_ids: list[str]


def _merge_order_group(group: list[dict[str, str]]) -> MergedOrder | str:
    """Combine a same-Order-number group of 'Order' rows into one postable
    sale. Returns a warning string (not raised) if the group's shape isn't
    one we can safely merge — callers route that to a parse warning rather
    than guessing.
    """
    entry_date = _parse_ebay_date(group[0]["Transaction creation date"])
    order_number = _clean_ref(group[0].get("Order number"))

    if len(group) == 1:
        row = group[0]
        return MergedOrder(
            entry_date=entry_date,
            order_number=order_number,
            currency=(row.get("Transaction currency") or "").strip(),
            gross=_parse_money(row.get("Gross transaction amount")),
            item_subtotal_sum=_parse_money(row.get("Item subtotal")),
            shipping_sum=_parse_money(row.get("Shipping and handling")),
            charity=_parse_money(row.get("Charity donation")),
            ebay_fee_usd=abs(sum((_parse_money(row.get(c)) for c in _EBAY_FEE_COLUMNS), Decimal("0"))),
            custom_labels=[c] if (c := _clean_ref(row.get("Custom label"))) else [],
            item_titles=[t] if (t := _clean_ref(row.get("Item title"))) else [],
            txn_ids=[t] if (t := _clean_ref(row.get("Transaction ID"))) else [],
        )

    # Multi-row order: exactly one row should carry the order-level Gross
    # transaction amount (the rest carry blank Gross, real Item
    # subtotal/Shipping/Custom label/fees each).
    totals_rows = [r for r in group if _parse_money(r.get("Gross transaction amount")) != 0]
    line_rows = [r for r in group if r not in totals_rows]

    if len(totals_rows) != 1:
        return (
            f"Order {order_number!r} spans {len(group)} CSV rows but "
            f"{len(totals_rows)} of them carry a nonzero Gross transaction amount "
            "(expected exactly 1) — can't safely merge, needs manual handling."
        )

    totals_row = totals_rows[0]
    currency = (totals_row.get("Transaction currency") or "").strip()

    custom_labels = [c for r in line_rows if (c := _clean_ref(r.get("Custom label")))]
    consign_labels = [c for c in custom_labels if c.upper().startswith("CONSIGN-")]
    # BUG FIX (QA, 2026-09): this must compare against len(line_rows) — the
    # TOTAL number of line items in the group — not len(custom_labels).
    # Comparing only against other *labeled* items silently missed the case
    # where some line items have NO Custom label at all (a legitimate,
    # common shape: per CLAUDE.md, an unlabeled listing defaults to normal
    # sale treatment) — that comparison would find e.g. 1 CONSIGN- label out
    # of 1 total *labeled* item and wrongly conclude "not mixed", even
    # though the order also contains unlabeled (implicitly normal) line
    # items. Caught by test_mixed_consign_and_normal_line_items_in_one_
    # order_refuses_to_post, which previously found this incorrectly
    # created a consignment_sales row covering the WHOLE order's gross
    # amount, including the normal items' revenue — a real money-math bug,
    # not just a missing warning.
    if consign_labels and len(consign_labels) != len(line_rows):
        non_consign_titles = [
            _clean_ref(r.get("Item title")) for r in line_rows if _clean_ref(r.get("Custom label")) not in consign_labels
        ]
        return (
            f"Order {order_number!r} mixes CONSIGN- line item(s) ({consign_labels}) with "
            f"{len(line_rows) - len(consign_labels)} non-CONSIGN- (or unlabeled) line item(s) "
            f"{non_consign_titles} — no supported posting model for a mixed order, needs manual handling."
        )

    return MergedOrder(
        entry_date=entry_date,
        order_number=order_number,
        currency=currency,
        gross=_parse_money(totals_row.get("Gross transaction amount")),
        item_subtotal_sum=sum((_parse_money(r.get("Item subtotal")) for r in line_rows), Decimal("0")),
        shipping_sum=sum((_parse_money(r.get("Shipping and handling")) for r in line_rows), Decimal("0")),
        charity=sum((_parse_money(r.get("Charity donation")) for r in group), Decimal("0")),
        ebay_fee_usd=abs(
            sum((_parse_money(r.get(c)) for r in group for c in _EBAY_FEE_COLUMNS), Decimal("0"))
        ),
        custom_labels=custom_labels,
        item_titles=[t for r in line_rows if (t := _clean_ref(r.get("Item title")))],
        txn_ids=[t for r in group if (t := _clean_ref(r.get("Transaction ID")))],
    )


def _process_order_group(
    conn: Connection,
    *,
    group: list[dict[str, str]],
    ebay_account_id: int,
    source_document_id: int,
    result: EbayIngestResult,
) -> None:
    merged = _merge_order_group(group)
    if isinstance(merged, str):
        result.parse_warnings.append(merged)
        return

    order_number = merged.order_number
    txn_id = merged.txn_ids[0] if merged.txn_ids else None
    is_consignment = any(label.upper().startswith("CONSIGN-") for label in merged.custom_labels)

    # Posting idempotency (BUG FIX, QA 2026-09) — see
    # ingestion/schema.py's ebay_csv_posted_transactions docstring. If this
    # order already posted on a previous sync run, there is nothing left to
    # do for it at all; skip before re-running any lookups/side effects.
    row_key = f"Order:{order_number}"
    if _already_posted_ebay_csv_row(conn, ebay_account_id, row_key) is not None:
        return

    if merged.currency != "USD":
        result.parse_warnings.append(
            f"Order {order_number!r} has non-USD Transaction currency ({merged.currency!r}) — "
            "not ingested, needs manual handling (no defined non-USD conversion path yet)."
        )
        return

    if abs((merged.item_subtotal_sum + merged.shipping_sum) - merged.gross) > _RECONCILIATION_TOLERANCE_USD:
        rate = lookup_kurs_pajak_rate(conn, merged.entry_date)
        rqid = _make_review_queue_row(
            conn,
            source_document_id=source_document_id,
            ebay_account_id=ebay_account_id,
            transaction_date=merged.entry_date,
            amount_idr=posting.round_idr(merged.gross * rate),
            amount_usd_ref=merged.gross,
            raw_description=(
                f"Order {order_number!r} reconciliation mismatch: item_subtotal+shipping="
                f"{merged.item_subtotal_sum + merged.shipping_sum} != gross={merged.gross}"
            ),
            external_ref=txn_id,
        )
        if rqid is not None:
            result.review_queue_rows_created += 1
        return

    if merged.charity != 0:
        rate = lookup_kurs_pajak_rate(conn, merged.entry_date)
        rqid = _make_review_queue_row(
            conn,
            source_document_id=source_document_id,
            ebay_account_id=ebay_account_id,
            transaction_date=merged.entry_date,
            amount_idr=posting.round_idr(abs(merged.charity) * rate),
            amount_usd_ref=abs(merged.charity),
            raw_description=(
                f"Order {order_number!r} has a nonzero Charity donation ({merged.charity}) — "
                "needs manual booking decision."
            ),
            external_ref=f"{txn_id}-charity" if txn_id else None,
        )
        if rqid is not None:
            result.review_queue_rows_created += 1
        # The rest of the order (gross/fee) still posts normally below —
        # the donation is flagged separately, it doesn't block the sale.

    rate = lookup_kurs_pajak_rate(conn, merged.entry_date)

    if is_consignment:
        item_price_usd = merged.item_subtotal_sum  # excludes shipping, per CLAUDE.md
        tier = lookup_tier_rate(conn, item_price_usd)
        if tier.requires_manual_contact:
            # $7,500+ (or no matching tier row) — "no fixed rate, requires
            # manual contact, never auto-applied" per CLAUDE.md. We can't
            # even suggest a payout amount, so this doesn't become a
            # consignment_sales row at all yet — flagged for manual
            # handling rather than posting a fabricated placeholder amount.
            result.parse_warnings.append(
                f"CONSIGN- order {order_number!r} (item price {item_price_usd} USD) requires "
                "manual tier contact — not staged as a consignment_sales row, needs manual entry."
            )
            return

        consignor_item_ref = f"{merged.custom_labels[0]}:{order_number}"
        # Idempotency for the unconfirmed-staging row too: consignor_item_ref
        # is built from order_number, which is already globally unique per
        # eBay order, so an existing row here means this exact order was
        # already staged on a previous sync run.
        already_staged = conn.execute(
            select(consignment_sales.c.id).where(consignment_sales.c.consignor_item_ref == consignor_item_ref)
        ).first()
        if already_staged is not None:
            return

        suggested_payout_usd = calc_tier_payout_usd(item_price_usd, tier.rate_percent)
        posting.create_consignment_sale(
            conn,
            item_price_usd=item_price_usd,
            payout_model="tier",
            payout_amount_idr=posting.round_idr(suggested_payout_usd * rate),
            consignor_item_ref=consignor_item_ref,
            shipping_cost_usd=merged.shipping_sum if merged.shipping_sum > 0 else None,
            tier_rate_percent=tier.rate_percent,
            confirmed=False,
        )
        result.consignment_sales_created += 1
        # Deliberately NOT posted — milestone 2's two-phase flow requires an
        # explicit human confirmation before any consignment payout posts,
        # even one detected via the CONSIGN- SKU convention. See design §2.
        return

    entry_id = posting.post_ebay_sale(
        conn,
        ebay_account_id=ebay_account_id,
        entry_date=merged.entry_date,
        gross_sale_price_usd=merged.gross,
        ebay_fee_usd=merged.ebay_fee_usd,
        kurs_pajak_rate=rate,
        category_id=None,  # eBay Transaction Report carries no category data — see CLAUDE.md
        ebay_order_ref=order_number,
        memo="; ".join(merged.item_titles)[:250] or None,
    )
    _mark_ebay_csv_row_posted(conn, ebay_account_id, row_key, entry_id)
    result.orders_posted += 1


def _process_refund_row(
    conn: Connection,
    *,
    row: dict[str, str],
    ebay_account_id: int,
    entry_date: _dt.date,
    order_number: str | None,
    currency: str,
    result: EbayIngestResult,
) -> None:
    if currency != "USD":
        result.parse_warnings.append(
            f"Refund on order {order_number!r} has non-USD Transaction currency ({currency!r}) — "
            "not ingested, needs manual handling."
        )
        return

    # Posting idempotency (BUG FIX, QA 2026-09) — Refund rows have a blank
    # Transaction ID in the real sample but a unique, non-blank Reference ID
    # (e.g. "Cancel ID 5447174021"); that's the stable key used here, since
    # Order number alone isn't guaranteed unique across all Refund rows the
    # way it is for a single Order group.
    reference_id = _clean_ref(row.get("Reference ID"))
    row_key = f"Refund:{reference_id or order_number}"
    if _already_posted_ebay_csv_row(conn, ebay_account_id, row_key) is not None:
        return

    gross = _parse_money(row.get("Gross transaction amount"))  # negative
    fee_credit = sum((_parse_money(row.get(col)) for col in _EBAY_FEE_COLUMNS), Decimal("0"))  # positive
    rate = lookup_kurs_pajak_rate(conn, entry_date)

    entry_id = posting.post_refund(
        conn,
        entry_date=entry_date,
        amount_idr=posting.round_idr(abs(gross) * rate),
        stage="ebay_wallet",
        ebay_account_id=ebay_account_id,
        usd_amount=abs(gross),
        kurs_pajak_rate=rate,
        category_id=None,
        ebay_order_ref=order_number,
        memo=_clean_ref(row.get("Item title")),
    )
    _mark_ebay_csv_row_posted(conn, ebay_account_id, row_key, entry_id)
    result.refunds_posted += 1

    if fee_credit > 0:
        posting.post_refund_fee_credit(
            conn,
            entry_date=entry_date,
            amount_idr=posting.round_idr(fee_credit * rate),
            stage="ebay_wallet",
            ebay_account_id=ebay_account_id,
            amount_usd_ref=fee_credit,
            fx_rate_used=rate,
            ebay_order_ref=order_number,
            memo="Final Value Fee credited back on refund",
        )


def _process_other_fee_row(
    conn: Connection,
    *,
    row: dict[str, str],
    ebay_account_id: int,
    entry_date: _dt.date,
    order_number: str | None,
    currency: str,
    result: EbayIngestResult,
) -> None:
    if currency != "USD":
        result.parse_warnings.append(
            f"Other fee on order {order_number!r} has non-USD Transaction currency ({currency!r}) — "
            "not ingested, needs manual handling."
        )
        return
    net_amount = _parse_money(row.get("Net amount"))
    if net_amount == 0:
        return

    # Posting idempotency (BUG FIX, QA 2026-09) — same story as Refund rows:
    # Transaction ID is blank in the real sample, Reference ID (e.g.
    # "FEE-7250394870018_11") is unique and non-blank.
    reference_id = _clean_ref(row.get("Reference ID"))
    row_key = f"OtherFee:{reference_id or (order_number, entry_date.isoformat(), str(net_amount))}"
    if _already_posted_ebay_csv_row(conn, ebay_account_id, row_key) is not None:
        return

    rate = lookup_kurs_pajak_rate(conn, entry_date)
    # Per Main-agent's 2026-08-31 decision (CLAUDE.md Chart of accounts,
    # "Other eBay Wallet debits" note): Promoted Listings + Store
    # subscription fees both post to GENERAL_OPEX.
    entry_id = posting.post_ebay_wallet_operating_expense(
        conn,
        ebay_account_id=ebay_account_id,
        entry_date=entry_date,
        amount_idr=posting.round_idr(abs(net_amount) * rate),
        expense_account_type_code="GENERAL_OPEX",
        amount_usd_ref=abs(net_amount),
        fx_rate_used=rate,
        ebay_order_ref=order_number,
        memo=_clean_ref(row.get("Description")),
    )
    _mark_ebay_csv_row_posted(conn, ebay_account_id, row_key, entry_id)
    result.other_fees_posted += 1


def _process_payout_row(
    conn: Connection,
    *,
    row: dict[str, str],
    ebay_account_id: int,
    source_document_id: int,
    entry_date: _dt.date,
    result: EbayIngestResult,
) -> None:
    payout_id = _clean_ref(row.get("Payout ID"))
    net_amount = _parse_money(row.get("Net amount"))
    if payout_id is None or net_amount == 0:
        return

    existing = conn.execute(
        select(ebay_expected_payouts.c.id).where(
            ebay_expected_payouts.c.ebay_account_id == ebay_account_id,
            ebay_expected_payouts.c.ebay_payout_id == payout_id,
        )
    ).first()
    if existing is not None:
        return  # row-creation idempotency — re-parsing the same CSV is a no-op here

    conn.execute(
        ebay_expected_payouts.insert().values(
            ebay_account_id=ebay_account_id,
            ebay_payout_id=payout_id,
            payout_date=entry_date,
            net_amount_usd=abs(net_amount),
            source_document_id=source_document_id,
        )
    )
    result.payouts_recorded += 1


def process_transaction_report(
    conn: Connection,
    *,
    ebay_account_id: int,
    source_document_id: int,
    rows: list[dict[str, str]],
) -> EbayIngestResult:
    """Branch on Type and post/stage each row per the design doc's §2 rules
    (Order rows are grouped/merged first — see the module docstring).

    ``rows`` is the ``data_rows`` output of ``parse_ebay_csv_rows``.
    """
    result = EbayIngestResult()

    order_rows = [r for r in rows if (r.get("Type") or "").strip() == "Order"]
    non_order_rows = [r for r in rows if (r.get("Type") or "").strip() != "Order"]

    for group in _group_order_rows(order_rows):
        _process_order_group(
            conn,
            group=group,
            ebay_account_id=ebay_account_id,
            source_document_id=source_document_id,
            result=result,
        )

    for row in non_order_rows:
        row_type = (row.get("Type") or "").strip()
        txn_id = _clean_ref(row.get("Transaction ID"))
        order_number = _clean_ref(row.get("Order number"))
        currency = (row.get("Transaction currency") or "").strip()
        entry_date = _parse_ebay_date(row["Transaction creation date"])

        if row_type == "Hold":
            # Always skipped — a temporary availability restriction on
            # funds already recognized via the underlying Order row, never
            # a distinct revenue/expense event by itself. See design §2.
            result.holds_skipped += 1
            continue

        if row_type == "Refund":
            _process_refund_row(
                conn,
                row=row,
                ebay_account_id=ebay_account_id,
                entry_date=entry_date,
                order_number=order_number,
                currency=currency,
                result=result,
            )
            continue

        if row_type == "Other fee":
            _process_other_fee_row(
                conn,
                row=row,
                ebay_account_id=ebay_account_id,
                entry_date=entry_date,
                order_number=order_number,
                currency=currency,
                result=result,
            )
            continue

        if row_type == "Payout":
            _process_payout_row(
                conn,
                row=row,
                ebay_account_id=ebay_account_id,
                source_document_id=source_document_id,
                entry_date=entry_date,
                result=result,
            )
            continue

        # Any other Type CLAUDE.md's own header notes mention as possible
        # (claim, payment dispute, shipping label, charge, transfer,
        # adjustment, purchase, secondary payout, withheld tax, reserve) but
        # never observed in the real sample — never silently skipped or
        # guess-posted. Only routed to review_queue if we can at least value
        # it in IDR (USD-denominated); otherwise it's a document-level
        # parse warning (we have no defined conversion path for a
        # non-USD-denominated surprise row).
        net_amount = _parse_money(row.get("Net amount"))
        if currency == "USD" and net_amount != 0:
            rate = lookup_kurs_pajak_rate(conn, entry_date)
            rqid = _make_review_queue_row(
                conn,
                source_document_id=source_document_id,
                ebay_account_id=ebay_account_id,
                transaction_date=entry_date,
                amount_idr=posting.round_idr(net_amount * rate),
                amount_usd_ref=net_amount,
                raw_description=f"eBay CSV row of unrecognized Type={row_type!r}: {row.get('Description', '')}",
                external_ref=txn_id,
            )
            if rqid is not None:
                result.review_queue_rows_created += 1
        elif net_amount != 0:
            result.parse_warnings.append(
                f"Unrecognized Type={row_type!r} on order {order_number!r} could not be "
                f"valued in IDR (currency={currency!r}) — not ingested, needs manual handling."
            )

    return result
