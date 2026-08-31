"""The double-entry posting engine.

Every function here builds the full set of debit/credit lines for one
business event, validates that they balance, and inserts the journal_entry
header + journal_lines atomically (all-or-nothing, in a single DB
transaction). The Postgres deferred constraint trigger in ledger/schema.py
re-validates the balance at COMMIT time as a structural backstop — this
module's own balance check is a fast, clear fail-fast layer on top of that,
not a replacement for it.

Money-math rules implemented here follow CLAUDE.md's Core accounting rules
section. See the module-level notes below for two shared, deliberately
documented interpretation calls on genuinely underspecified parts of the
spec (both flagged in the milestone report — neither changes any required
test scenario, both are explicit, traceable assumptions rather than silent
guesses):

1. Experimental ("net_of_fees_and_shipping") consignment payout model:
   CLAUDE.md gives the payout *formula* (gross − eBay fees − shipping cost,
   consignor absorbs both, seller earns $0 commission) but not the exact
   ledger mechanics for how that "absorption" nets through the books. This
   module posts the fee as a real expense (as always) and then posts an
   equal-and-offsetting recovery credit to eBay Selling Fees and Shipping
   Cost within the *same* sale entry, funded by the reduced consignor
   payout — net P&L effect from fee+shipping on this sale is $0, matching
   "seller earns no explicit commission", while keeping the gross fee and
   shipping figures independently traceable (not silently netted away).

2. Realized FX spread at Payoneer withdrawal: computed as
   (actual IDR landed + payout fee valued at the realized rate) minus
   (gross USD withdrawn x the original booking rate) — i.e. it isolates the
   pure rate-movement effect on the *whole* withdrawn amount, cleanly
   separate from the fee's own cost (booked in full as Payout Fee). This
   is the reading that keeps fee vs. FX cleanly split and makes the entry
   balance; a literal "rate applied to net USD converted" reading would
   double-count the fee's IDR-equivalent impact into the FX spread and
   leave the entry unbalanced.

Consignment sales are a *two-phase* flow, per docs/design/schema-design.md:
1. ``create_consignment_sale()`` records the deal terms and a payout
   amount as an unconfirmed ``consignment_sales`` row (confirmed_at NULL).
2. ``confirm_consignment_sale()`` marks it confirmed (a human decision).
3. ``post_consignment_sale()`` posts the journal entry — and refuses
   (MissingConfirmedAmountError) unless step 2 already happened. The
   schema itself also structurally forbids journal_entry_id being set
   while confirmed_at is NULL (a DB CHECK constraint), so this isn't just
   an application-layer convention.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection

from ledger.entities import get_account_id
from ledger.errors import (
    InvalidTransferError,
    MissingConfirmedAmountError,
    UnbalancedEntryError,
)
from ledger.schema import (
    consignment_sales,
    consignor_payout_tiers,  # noqa: F401  (imported for discoverability from this module)
    fx_revaluations,
    journal_entries,
    journal_lines,
    payoneer_withdrawals,
)

IDR_ONE = Decimal("1")

# Accounts an inter_account_transfer is allowed to touch — the entity's own
# wallet/bank accounts only, never revenue or expense. Mirrors the Postgres
# trigger check_transfer_accounts() in ledger/schema.py exactly, so a bad
# call fails fast in Python *and* is structurally blocked at the DB level.
TRANSFERABLE_ACCOUNT_TYPE_CODES = {"EBAY_WALLET", "PAYONEER_WALLET", "BCA_BRIDGING", "BCA_MAIN"}


def round_idr(value: Decimal) -> Decimal:
    """Round a money value to whole Rupiah (IDR has no practical sub-unit)."""
    _require_decimal(value, "value")
    return value.quantize(IDR_ONE, rounding=ROUND_HALF_UP)


def _require_decimal(value, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(
            f"{name} must be a decimal.Decimal (never a float) — got {type(value).__name__}. "
            "Money math must never use binary floats."
        )


def _period_month(entry_date: _dt.date) -> _dt.date:
    return entry_date.replace(day=1)


class Line:
    __slots__ = (
        "account_id",
        "debit_amount_idr",
        "credit_amount_idr",
        "amount_usd_ref",
        "fx_rate_used",
        "category_id",
        "ebay_order_ref",
        "consignor_item_ref",
    )

    def __init__(
        self,
        account_id: int,
        debit_amount_idr: Decimal,
        credit_amount_idr: Decimal,
        *,
        amount_usd_ref: Decimal | None = None,
        fx_rate_used: Decimal | None = None,
        category_id: int | None = None,
        ebay_order_ref: str | None = None,
        consignor_item_ref: str | None = None,
    ) -> None:
        self.account_id = account_id
        self.debit_amount_idr = debit_amount_idr
        self.credit_amount_idr = credit_amount_idr
        self.amount_usd_ref = amount_usd_ref
        self.fx_rate_used = fx_rate_used
        self.category_id = category_id
        self.ebay_order_ref = ebay_order_ref
        self.consignor_item_ref = consignor_item_ref


def debit(account_id: int, amount_idr: Decimal, **kwargs) -> Line:
    _require_decimal(amount_idr, "amount_idr")
    if amount_idr <= 0:
        raise ValueError("A journal line amount must be > 0 (zero/negative lines are meaningless).")
    return Line(account_id, amount_idr, Decimal("0"), **kwargs)


def credit(account_id: int, amount_idr: Decimal, **kwargs) -> Line:
    _require_decimal(amount_idr, "amount_idr")
    if amount_idr <= 0:
        raise ValueError("A journal line amount must be > 0 (zero/negative lines are meaningless).")
    return Line(account_id, Decimal("0"), amount_idr, **kwargs)


def _insert_journal_entry(
    conn: Connection,
    *,
    entry_date: _dt.date,
    source_type: str,
    lines: Iterable[Line],
    memo: str | None = None,
) -> int:
    lines = list(lines)
    if len(lines) < 2:
        raise UnbalancedEntryError("A posting needs at least 2 journal lines.")

    total_debit = sum((l.debit_amount_idr for l in lines), Decimal("0"))
    total_credit = sum((l.credit_amount_idr for l in lines), Decimal("0"))
    if total_debit != total_credit:
        raise UnbalancedEntryError(
            f"Unbalanced posting: total debits {total_debit} != total credits {total_credit}"
        )

    result = conn.execute(
        journal_entries.insert().values(
            entry_date=entry_date,
            period_month=_period_month(entry_date),
            source_type=source_type,
            memo=memo,
        )
    )
    entry_id = result.inserted_primary_key[0]

    conn.execute(
        journal_lines.insert(),
        [
            {
                "journal_entry_id": entry_id,
                "account_id": l.account_id,
                "debit_amount_idr": l.debit_amount_idr,
                "credit_amount_idr": l.credit_amount_idr,
                "amount_usd_ref": l.amount_usd_ref,
                "fx_rate_used": l.fx_rate_used,
                "category_id": l.category_id,
                "ebay_order_ref": l.ebay_order_ref,
                "consignor_item_ref": l.consignor_item_ref,
            }
            for l in lines
        ],
    )
    return entry_id


def _singleton(conn: Connection, code: str) -> int:
    return get_account_id(conn, code)


# ---------------------------------------------------------------------------
# Sales: stock / pre-order (COGS timing is identical, only handled by
# *when* post_cogs_purchase is called relative to this)
# ---------------------------------------------------------------------------

def post_ebay_sale(
    conn: Connection,
    *,
    ebay_account_id: int,
    entry_date: _dt.date,
    gross_sale_price_usd: Decimal,
    ebay_fee_usd: Decimal,
    kurs_pajak_rate: Decimal,
    category_id: int | None = None,
    ebay_order_ref: str | None = None,
    memo: str | None = None,
) -> int:
    """Normal (non-consignment) sale — used for both the stock and
    pre-order models, since the posting logic is identical for both (see
    CLAUDE.md's Business model). COGS is NOT posted here — see
    post_cogs_purchase(). For a stock sale that purchase happens before
    this call; for a pre-order sale, after.

    Revenue recognized gross; eBay's fee posts to its own expense account;
    net cash lands in the eBay Wallet.
    """
    for name, val in [
        ("gross_sale_price_usd", gross_sale_price_usd),
        ("ebay_fee_usd", ebay_fee_usd),
        ("kurs_pajak_rate", kurs_pajak_rate),
    ]:
        _require_decimal(val, name)

    gross_idr = round_idr(gross_sale_price_usd * kurs_pajak_rate)
    fee_idr = round_idr(ebay_fee_usd * kurs_pajak_rate)
    net_wallet_idr = gross_idr - fee_idr

    ebay_wallet_id = get_account_id(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
    sales_revenue_id = _singleton(conn, "SALES_REVENUE")
    ebay_fee_id = _singleton(conn, "EBAY_SELLING_FEES")

    lines = [
        debit(
            ebay_wallet_id,
            net_wallet_idr,
            amount_usd_ref=gross_sale_price_usd,
            fx_rate_used=kurs_pajak_rate,
            ebay_order_ref=ebay_order_ref,
        ),
        debit(
            ebay_fee_id,
            fee_idr,
            amount_usd_ref=ebay_fee_usd,
            fx_rate_used=kurs_pajak_rate,
            ebay_order_ref=ebay_order_ref,
        ),
        # category_id only ever goes on the Sales Revenue line — see
        # CLAUDE.md's Category tagging section ("revenue-side analytics
        # only"). Never used to allocate any shared cost.
        credit(
            sales_revenue_id,
            gross_idr,
            amount_usd_ref=gross_sale_price_usd,
            fx_rate_used=kurs_pajak_rate,
            category_id=category_id,
            ebay_order_ref=ebay_order_ref,
        ),
    ]

    return _insert_journal_entry(conn, entry_date=entry_date, source_type="ebay_sale", lines=lines, memo=memo)


# ---------------------------------------------------------------------------
# COGS (stock + pre-order both: expensed at time of purchase/shipment)
# ---------------------------------------------------------------------------

def post_cogs_purchase(
    conn: Connection,
    *,
    entry_date: _dt.date,
    amount_idr: Decimal,
    memo: str | None = None,
) -> int:
    """COGS recognized at time of purchase (stock or pre-order both — see
    CLAUDE.md's Business model). Funded from the consolidated BCA Main
    account (purchase invoices are filed/paid at the consolidated level,
    never per eBay account — see CLAUDE.md's Data sources & inputs).
    """
    _require_decimal(amount_idr, "amount_idr")
    cogs_id = _singleton(conn, "COGS")
    bca_main_id = _singleton(conn, "BCA_MAIN")

    lines = [debit(cogs_id, amount_idr), credit(bca_main_id, amount_idr)]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="cogs_purchase", lines=lines, memo=memo)


def post_shipping_cost_purchase(
    conn: Connection,
    *,
    entry_date: _dt.date,
    amount_idr: Decimal,
    memo: str | None = None,
) -> int:
    """The seller's real outbound shipping expense (paid to the carrier),
    funded from BCA Main. Distinct from the experimental consignment
    model's shipping-cost *recovery* line, which is posted as part of
    post_consignment_sale() and nets against this expense. There's no
    dedicated source_type for this in the schema (see schema-design.md's
    enum) so it uses 'bank_other', the same catch-all used for
    Payroll/General Opex payments that also have no dedicated type.
    """
    _require_decimal(amount_idr, "amount_idr")
    shipping_id = _singleton(conn, "SHIPPING_COST")
    bca_main_id = _singleton(conn, "BCA_MAIN")

    lines = [debit(shipping_id, amount_idr), credit(bca_main_id, amount_idr)]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="bank_other", lines=lines, memo=memo)


# ---------------------------------------------------------------------------
# Consignment sale — two-phase: create (unconfirmed) -> confirm -> post.
# ---------------------------------------------------------------------------

def create_consignment_sale(
    conn: Connection,
    *,
    item_price_usd: Decimal,
    payout_model: str,  # 'tier' | 'net_of_fees_and_shipping'
    payout_amount_idr: Decimal,
    consignor_item_ref: str,
    shipping_cost_usd: Decimal | None = None,
    tier_rate_percent: Decimal | None = None,
    confirmed: bool = False,
) -> int:
    """Record consignment deal terms + a proposed payout amount as an
    UNCONFIRMED row (confirmed_at NULL unless confirmed=True is passed
    explicitly by a caller that's already gotten human sign-off).

    This never computes payout_amount_idr itself — callers should use
    ledger/consignment.py's calculator functions to produce a suggestion
    first, then pass in whatever amount was actually confirmed.
    """
    if payout_model not in ("tier", "net_of_fees_and_shipping"):
        raise ValueError(f"payout_model must be 'tier' or 'net_of_fees_and_shipping' (got {payout_model!r})")
    if not consignor_item_ref:
        raise ValueError("consignor_item_ref is required (traceability).")
    for name, val in [("item_price_usd", item_price_usd), ("payout_amount_idr", payout_amount_idr)]:
        _require_decimal(val, name)
    if shipping_cost_usd is not None:
        _require_decimal(shipping_cost_usd, "shipping_cost_usd")
    if tier_rate_percent is not None:
        _require_decimal(tier_rate_percent, "tier_rate_percent")
    if payout_amount_idr <= 0:
        raise ValueError("payout_amount_idr must be > 0")

    result = conn.execute(
        consignment_sales.insert().values(
            item_price_usd=item_price_usd,
            shipping_cost_usd=shipping_cost_usd,
            payout_model=payout_model,
            tier_rate_percent=tier_rate_percent,
            payout_amount_idr=payout_amount_idr,
            consignor_item_ref=consignor_item_ref,
            confirmed_at=func.now() if confirmed else None,
            journal_entry_id=None,
        )
    )
    return result.inserted_primary_key[0]


def confirm_consignment_sale(conn: Connection, consignment_sale_id: int) -> None:
    """Mark a consignment_sales row as confirmed — the explicit human
    decision that unblocks post_consignment_sale(). This is the only
    function in this module that sets confirmed_at.
    """
    result = conn.execute(
        update(consignment_sales)
        .where(consignment_sales.c.id == consignment_sale_id)
        .where(consignment_sales.c.confirmed_at.is_(None))
        .values(confirmed_at=func.now())
    )
    if result.rowcount == 0:
        existing = conn.execute(
            select(consignment_sales.c.confirmed_at).where(consignment_sales.c.id == consignment_sale_id)
        ).first()
        if existing is None:
            raise ValueError(f"No consignment_sales row with id={consignment_sale_id}")
        # Already confirmed — treat as a no-op rather than an error.


def post_consignment_sale(
    conn: Connection,
    *,
    consignment_sale_id: int,
    ebay_account_id: int,
    entry_date: _dt.date,
    gross_sale_price_usd: Decimal,
    ebay_fee_usd: Decimal,
    kurs_pajak_rate: Decimal,
    category_id: int | None = None,
    ebay_order_ref: str | None = None,
    memo: str | None = None,
) -> int:
    """Post the journal entry for a consignment sale that's already been
    confirmed (see create_consignment_sale / confirm_consignment_sale).

    ``gross_sale_price_usd`` is the full eBay-charged amount (item +
    shipping charged to the buyer) for THIS sale — distinct from the
    consignment_sales row's item_price_usd (excludes shipping, used only
    for the tier lookup/payout math) and shipping_cost_usd (the seller's
    own outbound shipping cost under the experimental model). It's passed
    here rather than stored on consignment_sales because it's a property
    of the underlying eBay transaction, not of the consignor deal terms.

    Raises MissingConfirmedAmountError if the row hasn't been confirmed —
    there is no code path that posts an unconfirmed payout. The schema's
    own CHECK constraint (ck_consignment_sales_no_post_without_confirmation)
    backs this up structurally: it would reject the UPDATE below even if
    this Python-level check were somehow bypassed.
    """
    row = conn.execute(
        select(
            consignment_sales.c.payout_model,
            consignment_sales.c.payout_amount_idr,
            consignment_sales.c.consignor_item_ref,
            consignment_sales.c.confirmed_at,
            consignment_sales.c.journal_entry_id,
        ).where(consignment_sales.c.id == consignment_sale_id)
    ).first()
    if row is None:
        raise ValueError(f"No consignment_sales row with id={consignment_sale_id}")
    if row.confirmed_at is None:
        raise MissingConfirmedAmountError(
            f"consignment_sales id={consignment_sale_id} has not been confirmed "
            "(confirmed_at is NULL) — a consignment payout can never post without "
            "an explicit, already-confirmed amount. Call confirm_consignment_sale() first."
        )
    if row.journal_entry_id is not None:
        raise ValueError(
            f"consignment_sales id={consignment_sale_id} has already been posted "
            f"(journal_entry_id={row.journal_entry_id}) — refusing to post twice."
        )

    for name, val in [
        ("gross_sale_price_usd", gross_sale_price_usd),
        ("ebay_fee_usd", ebay_fee_usd),
        ("kurs_pajak_rate", kurs_pajak_rate),
    ]:
        _require_decimal(val, name)

    payout_idr = row.payout_amount_idr
    consignor_item_ref = row.consignor_item_ref
    payout_model = row.payout_model

    gross_idr = round_idr(gross_sale_price_usd * kurs_pajak_rate)
    fee_idr = round_idr(ebay_fee_usd * kurs_pajak_rate)
    net_wallet_idr = gross_idr - fee_idr

    ebay_wallet_id = get_account_id(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
    ebay_fee_id = _singleton(conn, "EBAY_SELLING_FEES")
    consignor_payable_id = _singleton(conn, "CONSIGNOR_PAYABLE")

    common_kwargs = dict(
        amount_usd_ref=gross_sale_price_usd,
        fx_rate_used=kurs_pajak_rate,
        ebay_order_ref=ebay_order_ref,
        consignor_item_ref=consignor_item_ref,
    )

    lines = [
        debit(ebay_wallet_id, net_wallet_idr, **common_kwargs),
        debit(ebay_fee_id, fee_idr, **common_kwargs),
    ]

    if payout_model == "tier":
        # Consignor is insulated from eBay fees and shipping — both stay
        # with the seller. Everything not owed to the consignor is the
        # seller's commission (this is what makes shipping revenue "stick"
        # to the seller under this model, per CLAUDE.md).
        commission_idr = gross_idr - payout_idr
        if commission_idr <= 0:
            raise ValueError(
                f"Tier-model commission computed as {commission_idr} (<=0) — "
                "confirmed payout exceeds gross sale price; check inputs."
            )
        commission_id = _singleton(conn, "CONSIGNMENT_COMMISSION_INCOME")
        lines.append(credit(consignor_payable_id, payout_idr, **common_kwargs))
        # category_id belongs only on the true Sales Revenue line per
        # CLAUDE.md; Commission Income is a different revenue line, so we
        # deliberately don't tag it with category_id here.
        lines.append(credit(commission_id, commission_idr, **common_kwargs))
    else:  # net_of_fees_and_shipping
        # Consignor absorbs eBay fees and shipping cost (payout is reduced
        # by both). Seller earns $0 explicit commission — see module
        # docstring for the exact recovery-line construction.
        recovered_idr = net_wallet_idr - payout_idr  # = gross_idr - fee_idr - payout_idr
        if recovered_idr < 0:
            raise ValueError(
                f"Experimental-model confirmed payout ({payout_idr}) exceeds net-of-fee "
                f"proceeds ({net_wallet_idr}) — check inputs."
            )
        lines.append(credit(consignor_payable_id, payout_idr, **common_kwargs))
        if fee_idr > 0:
            # Fee recovery: the consignor absorbs the fee, so the seller's
            # net expense from this sale's fee is $0 (fee still visible on
            # both sides for traceability of the gross amount eBay charged).
            lines.append(credit(ebay_fee_id, fee_idr, **common_kwargs))
        if recovered_idr > 0:
            shipping_cost_id = _singleton(conn, "SHIPPING_COST")
            lines.append(credit(shipping_cost_id, recovered_idr, **common_kwargs))
        # Consignment Commission Income is never posted for this model — a
        # $0 line would violate the "exactly one nonzero side" constraint,
        # and $0 commission means no line, not a zero-amount line.

    entry_id = _insert_journal_entry(
        conn, entry_date=entry_date, source_type="consignment_sale", lines=lines, memo=memo
    )

    conn.execute(
        update(consignment_sales)
        .where(consignment_sales.c.id == consignment_sale_id)
        .values(journal_entry_id=entry_id)
    )
    return entry_id


def post_consignor_reimbursement(
    conn: Connection,
    *,
    entry_date: _dt.date,
    amount_idr: Decimal,
    consignor_item_ref: str,
    paying_account_type_code: str = "BCA_MAIN",
    paying_ebay_account_id: int | None = None,
    paying_wallet_group_id: int | None = None,
    memo: str | None = None,
) -> int:
    """Clear (part of) the aggregate Consignor Payable liability by actually
    paying a consignor out. Never touches P&L or equity.
    """
    _require_decimal(amount_idr, "amount_idr")
    if not consignor_item_ref:
        raise ValueError("consignor_item_ref is required (traceability).")

    consignor_payable_id = _singleton(conn, "CONSIGNOR_PAYABLE")
    paying_account_id = get_account_id(
        conn,
        paying_account_type_code,
        ebay_account_id=paying_ebay_account_id,
        wallet_group_id=paying_wallet_group_id,
    )

    lines = [
        debit(consignor_payable_id, amount_idr, consignor_item_ref=consignor_item_ref),
        credit(paying_account_id, amount_idr, consignor_item_ref=consignor_item_ref),
    ]
    return _insert_journal_entry(
        conn, entry_date=entry_date, source_type="consignment_payout", lines=lines, memo=memo
    )


# ---------------------------------------------------------------------------
# Refunds / discount clawbacks (contra-revenue, never netted into revenue)
# ---------------------------------------------------------------------------

def post_refund(
    conn: Connection,
    *,
    entry_date: _dt.date,
    amount_idr: Decimal,
    stage: str,  # 'ebay_wallet' | 'payoneer'
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
    usd_amount: Decimal | None = None,
    kurs_pajak_rate: Decimal | None = None,
    category_id: int | None = None,
    ebay_order_ref: str | None = None,
    memo: str | None = None,
) -> int:
    """A refund/discount clawback deducted by eBay, at either the eBay
    Wallet stage or the Payoneer stage. Posts to Sales Returns & Allowances
    (contra-revenue) — never netted silently into Sales Revenue.
    """
    _require_decimal(amount_idr, "amount_idr")
    if stage == "ebay_wallet":
        if ebay_account_id is None:
            raise ValueError("stage='ebay_wallet' requires ebay_account_id")
        cash_account_id = get_account_id(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
    elif stage == "payoneer":
        if wallet_group_id is None:
            raise ValueError("stage='payoneer' requires wallet_group_id")
        cash_account_id = get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=wallet_group_id)
    else:
        raise ValueError(f"stage must be 'ebay_wallet' or 'payoneer' (got {stage!r})")

    returns_id = _singleton(conn, "SALES_RETURNS_ALLOWANCES")

    lines = [
        debit(
            returns_id,
            amount_idr,
            amount_usd_ref=usd_amount,
            fx_rate_used=kurs_pajak_rate,
            category_id=category_id,
            ebay_order_ref=ebay_order_ref,
        ),
        credit(
            cash_account_id,
            amount_idr,
            amount_usd_ref=usd_amount,
            fx_rate_used=kurs_pajak_rate,
            ebay_order_ref=ebay_order_ref,
        ),
    ]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="ebay_refund", lines=lines, memo=memo)


# ---------------------------------------------------------------------------
# Inter-account transfer — a distinct, non-P&L transaction type.
# ---------------------------------------------------------------------------

def post_inter_account_transfer(
    conn: Connection,
    *,
    entry_date: _dt.date,
    from_account_type_code: str,
    to_account_type_code: str,
    amount_idr: Decimal,
    from_ebay_account_id: int | None = None,
    from_wallet_group_id: int | None = None,
    to_ebay_account_id: int | None = None,
    to_wallet_group_id: int | None = None,
    memo: str | None = None,
) -> int:
    """Move the entity's own money between its own wallet/bank accounts.
    Never touches revenue or expense — both Python (here) and the Postgres
    trg_check_transfer_accounts trigger enforce this account whitelist.
    """
    _require_decimal(amount_idr, "amount_idr")
    for code in (from_account_type_code, to_account_type_code):
        if code not in TRANSFERABLE_ACCOUNT_TYPE_CODES:
            raise InvalidTransferError(
                f"{code} is not a transferable wallet/bank account "
                f"(allowed: {sorted(TRANSFERABLE_ACCOUNT_TYPE_CODES)}). "
                "Inter-account transfers may never touch revenue or expense accounts."
            )

    from_id = get_account_id(
        conn, from_account_type_code, ebay_account_id=from_ebay_account_id, wallet_group_id=from_wallet_group_id
    )
    to_id = get_account_id(
        conn, to_account_type_code, ebay_account_id=to_ebay_account_id, wallet_group_id=to_wallet_group_id
    )

    lines = [debit(to_id, amount_idr), credit(from_id, amount_idr)]
    return _insert_journal_entry(
        conn, entry_date=entry_date, source_type="inter_account_transfer", lines=lines, memo=memo
    )


# ---------------------------------------------------------------------------
# FX: realized (at Payoneer withdrawal) and unrealized (month-end revaluation)
# ---------------------------------------------------------------------------

def post_realized_fx_withdrawal(
    conn: Connection,
    *,
    wallet_group_id: int,
    entry_date: _dt.date,
    gross_usd: Decimal,
    payoneer_fee_usd: Decimal,
    exchange_rate_excl_fee: Decimal,
    booking_rate_used_idr: Decimal,
    memo: str | None = None,
) -> int:
    """Realized FX at the Payoneer withdrawal event — the one point where
    USD actually converts to IDR. Splits into two distinct postings:
    Payout Fee (Payoneer's flat USD fee, an operating expense) and Realized
    FX Gain/Loss (the pure rate-movement effect) — never blended. Both
    Payoneer's fee and its stated exchange-rate-excluding-fee come directly
    from the withdrawal confirmation (never inferred), same for
    booking_rate_used_idr (the Kurs Pajak rate the underlying sale(s) were
    originally booked at). See the module docstring for the exact spread
    formula and why. Also writes a payoneer_withdrawals row so the split
    is traceable back to these exact stated figures, not a memo string.
    """
    for name, val in [
        ("gross_usd", gross_usd),
        ("payoneer_fee_usd", payoneer_fee_usd),
        ("exchange_rate_excl_fee", exchange_rate_excl_fee),
        ("booking_rate_used_idr", booking_rate_used_idr),
    ]:
        _require_decimal(val, name)
    if payoneer_fee_usd > gross_usd:
        raise ValueError("payoneer_fee_usd cannot exceed gross_usd")

    net_usd_converted = gross_usd - payoneer_fee_usd
    net_idr_landed = round_idr(net_usd_converted * exchange_rate_excl_fee)
    payout_fee_idr = round_idr(payoneer_fee_usd * exchange_rate_excl_fee)
    booking_rate_implied_idr = round_idr(gross_usd * booking_rate_used_idr)
    fx_spread_idr = (net_idr_landed + payout_fee_idr) - booking_rate_implied_idr

    payoneer_wallet_id = get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=wallet_group_id)
    bca_bridging_id = get_account_id(conn, "BCA_BRIDGING", wallet_group_id=wallet_group_id)
    payout_fee_id = _singleton(conn, "PAYOUT_FEE")
    realized_fx_id = _singleton(conn, "REALIZED_FX")

    lines = [
        debit(bca_bridging_id, net_idr_landed, amount_usd_ref=net_usd_converted, fx_rate_used=exchange_rate_excl_fee),
        debit(payout_fee_id, payout_fee_idr, amount_usd_ref=payoneer_fee_usd, fx_rate_used=exchange_rate_excl_fee),
        credit(payoneer_wallet_id, booking_rate_implied_idr, amount_usd_ref=gross_usd, fx_rate_used=booking_rate_used_idr),
    ]
    if fx_spread_idr > 0:
        lines.append(credit(realized_fx_id, fx_spread_idr, amount_usd_ref=gross_usd))
    elif fx_spread_idr < 0:
        lines.append(debit(realized_fx_id, -fx_spread_idr, amount_usd_ref=gross_usd))
    # fx_spread_idr == 0: no FX line needed, the other three lines already balance.

    entry_id = _insert_journal_entry(
        conn, entry_date=entry_date, source_type="payoneer_withdrawal", lines=lines, memo=memo
    )

    conn.execute(
        payoneer_withdrawals.insert().values(
            wallet_group_id=wallet_group_id,
            withdrawal_date=entry_date,
            gross_usd=gross_usd,
            payoneer_fee_usd=payoneer_fee_usd,
            exchange_rate_excl_fee=exchange_rate_excl_fee,
            net_idr_landed=net_idr_landed,
            booking_rate_used_idr=booking_rate_used_idr,
            journal_entry_id=entry_id,
        )
    )
    return entry_id


def post_unrealized_fx_revaluation(
    conn: Connection,
    *,
    wallet_group_id: int,
    period_month: _dt.date,
    usd_balance: Decimal,
    current_book_value_idr: Decimal,
    kemenkeu_eom_rate_idr: Decimal,
    memo: str | None = None,
) -> int | None:
    """Month-end revaluation of a USD balance still sitting unwithdrawn in a
    Payoneer wallet, using Kemenkeu's end-of-month Kurs Pajak rate. Posts to
    Unrealized FX Gain/Loss — a distinct account from Realized FX. Also
    writes an fx_revaluations row for traceability back to the exact
    balance/rate used.

    Returns None (posts nothing) if the revalued amount exactly matches the
    current book value — there's genuinely no adjustment to make.
    """
    for name, val in [
        ("usd_balance", usd_balance),
        ("current_book_value_idr", current_book_value_idr),
        ("kemenkeu_eom_rate_idr", kemenkeu_eom_rate_idr),
    ]:
        _require_decimal(val, name)

    revalued_idr = round_idr(usd_balance * kemenkeu_eom_rate_idr)
    diff = revalued_idr - current_book_value_idr
    if diff == 0:
        return None

    payoneer_wallet_id = get_account_id(conn, "PAYONEER_WALLET", wallet_group_id=wallet_group_id)
    unrealized_fx_id = _singleton(conn, "UNREALIZED_FX")

    if diff > 0:
        lines = [
            debit(payoneer_wallet_id, diff, amount_usd_ref=usd_balance, fx_rate_used=kemenkeu_eom_rate_idr),
            credit(unrealized_fx_id, diff, amount_usd_ref=usd_balance, fx_rate_used=kemenkeu_eom_rate_idr),
        ]
    else:
        lines = [
            debit(unrealized_fx_id, -diff, amount_usd_ref=usd_balance, fx_rate_used=kemenkeu_eom_rate_idr),
            credit(payoneer_wallet_id, -diff, amount_usd_ref=usd_balance, fx_rate_used=kemenkeu_eom_rate_idr),
        ]

    entry_id = _insert_journal_entry(
        conn, entry_date=period_month, source_type="fx_revaluation", lines=lines, memo=memo
    )

    conn.execute(
        fx_revaluations.insert().values(
            wallet_group_id=wallet_group_id,
            period_month=period_month,
            usd_balance=usd_balance,
            kemenkeu_eom_rate_idr=kemenkeu_eom_rate_idr,
            revalued_idr=revalued_idr,
            journal_entry_id=entry_id,
        )
    )
    return entry_id


# ---------------------------------------------------------------------------
# Owner contribution / draw — trivial, but part of the source_type enum, so
# implemented for completeness (not exercised by the milestone's required
# test scenarios).
# ---------------------------------------------------------------------------

def post_owner_contribution(
    conn: Connection, *, entry_date: _dt.date, amount_idr: Decimal, memo: str | None = None
) -> int:
    _require_decimal(amount_idr, "amount_idr")
    bca_main_id = _singleton(conn, "BCA_MAIN")
    capital_id = _singleton(conn, "OWNERS_CAPITAL")
    lines = [debit(bca_main_id, amount_idr), credit(capital_id, amount_idr)]
    return _insert_journal_entry(
        conn, entry_date=entry_date, source_type="owner_contribution", lines=lines, memo=memo
    )


def post_owner_draw(conn: Connection, *, entry_date: _dt.date, amount_idr: Decimal, memo: str | None = None) -> int:
    _require_decimal(amount_idr, "amount_idr")
    draw_id = _singleton(conn, "OWNERS_DRAW")
    bca_main_id = _singleton(conn, "BCA_MAIN")
    lines = [debit(draw_id, amount_idr), credit(bca_main_id, amount_idr)]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="owner_draw", lines=lines, memo=memo)
