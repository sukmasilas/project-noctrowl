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
    opening_balances,
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

    lines = [
        debit(
            ebay_wallet_id,
            net_wallet_idr,
            amount_usd_ref=gross_sale_price_usd,
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
    # BUG FIX (2026-09, found running real May 2026 eBay data through the
    # pipeline): a real order (22-14606-65529) has $0 in every itemized fee
    # column — no Final Value Fee, no regulatory/international/deposit fee
    # at all (a real, legitimate zero, not a parsing gap — confirmed against
    # the CSV's own fee columns). ``debit()``/``credit()`` both reject a
    # zero/negative amount as meaningless, so unconditionally posting a fee
    # line here crashed on this real row. Fixed by only posting the fee line
    # when there actually IS a fee — when there isn't, the wallet debit
    # equals the full gross (net_wallet_idr == gross_idr) and the entry
    # still balances with just the two lines above, which is the accounting
    # -correct representation of "eBay charged no fee this time", not a
    # workaround.
    if fee_idr > 0:
        ebay_fee_id = _singleton(conn, "EBAY_SELLING_FEES")
        lines.append(
            debit(
                ebay_fee_id,
                fee_idr,
                amount_usd_ref=ebay_fee_usd,
                fx_rate_used=kurs_pajak_rate,
                ebay_order_ref=ebay_order_ref,
            )
        )

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
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """Clear (part of) the aggregate Consignor Payable liability by actually
    paying a consignor out. Never touches P&L or equity.

    ``amount_usd_ref``/``fx_rate_used`` (added 2026-09, QA-found bug fix —
    see ``scheduling/fx_revaluation.py``'s ``compute_payoneer_wallet_
    balance`` docstring for the concrete money-math impact of NOT
    threading these through: a Payoneer-wallet-paid reimbursement with no
    USD reference silently corrupts the wallet-group's computed USD
    balance, which then feeds a fictitious month-end Unrealized FX
    Gain/Loss straight onto the consolidated P&L) are OPTIONAL and only
    meaningful when ``paying_account_type_code`` is a USD-denominated
    account (i.e. ``PAYONEER_WALLET`` — a reimbursement paid directly out
    of a Payoneer Wallet, per ``ingestion.matching._paying_account_for_
    row``'s ``payoneer_csv`` case). Tagged on BOTH lines, matching this
    module's existing convention (``post_ebay_sale``, ``post_refund``,
    ``post_ebay_wallet_operating_expense`` — the USD figure is a reference
    on the whole TRANSACTION, not something only the USD-currency side of
    it carries). Both default None, same optional-kwarg pattern already
    used by ``post_inter_account_transfer``/``post_refund`` — existing
    callers that don't pass these are unaffected.
    """
    _require_decimal(amount_idr, "amount_idr")
    if not consignor_item_ref:
        raise ValueError("consignor_item_ref is required (traceability).")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")

    consignor_payable_id = _singleton(conn, "CONSIGNOR_PAYABLE")
    paying_account_id = get_account_id(
        conn,
        paying_account_type_code,
        ebay_account_id=paying_ebay_account_id,
        wallet_group_id=paying_wallet_group_id,
    )

    common_kwargs = dict(
        consignor_item_ref=consignor_item_ref, amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used
    )
    lines = [
        debit(consignor_payable_id, amount_idr, **common_kwargs),
        credit(paying_account_id, amount_idr, **common_kwargs),
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
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """Move the entity's own money between its own wallet/bank accounts.
    Never touches revenue or expense — both Python (here) and the Postgres
    trg_check_transfer_accounts trigger enforce this account whitelist.

    ``amount_usd_ref``/``fx_rate_used`` (added 2026-08-31, milestone 3,
    approved by Main-agent as an additive/backward-compatible change) are
    OPTIONAL and only meaningful for a USD-denominated leg of the transfer
    graph — specifically the eBay Wallet -> Payoneer Wallet leg (both USD
    accounts), where CLAUDE.md's ledger design still requires an IDR
    valuation (IDR is the single ledger currency) even though no actual
    currency conversion happens at that step. Existing callers (e.g. the
    IDR-only BCA Bridging -> BCA Main leg) are unaffected — both default to
    None, same as every other posting function's optional USD-reference
    kwargs.
    """
    _require_decimal(amount_idr, "amount_idr")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")
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

    lines = [
        debit(to_id, amount_idr, amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used),
        credit(from_id, amount_idr, amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used),
    ]
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


# ---------------------------------------------------------------------------
# Milestone 3 additions (2026-08-31) — new functions only, nothing above this
# line is modified except post_inter_account_transfer's additive optional
# kwargs (see its docstring). These back the review-queue / eBay-CSV
# postings designed in docs/design/milestone-3-ingestion-design.md and
# approved by Main-agent (see that doc's §0 resolutions).
# ---------------------------------------------------------------------------

def post_operating_expense(
    conn: Connection,
    *,
    entry_date: _dt.date,
    expense_account_type_code: str,
    amount_idr: Decimal,
    paying_account_type_code: str = "BCA_MAIN",
    paying_ebay_account_id: int | None = None,
    paying_wallet_group_id: int | None = None,
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """Generic "debit an operating-expense account, credit whatever paid it"
    posting — backs review-queue rule (e) keyword-matched lines (Payroll,
    General Opex, bank admin fees, etc. — see the design doc's §6). Reuses
    'bank_other' as source_type, the same catch-all precedent already used
    by post_shipping_cost_purchase for anything without its own dedicated
    journal_entries.source_type value.

    ``amount_usd_ref``/``fx_rate_used`` (added 2026-09, QA-found bug fix —
    see ``post_consignor_reimbursement``'s docstring above for the full
    explanation; the same gap applied here for a Payoneer-wallet-paid
    operating expense, e.g. a Payoneer-charged fee classified via
    review-queue rule (e)) are OPTIONAL, tagged on BOTH lines (matching
    this module's established convention), and only meaningful when
    ``paying_account_type_code`` is ``PAYONEER_WALLET``. Both default
    None; existing callers that don't pass these are unaffected.
    """
    _require_decimal(amount_idr, "amount_idr")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")
    expense_id = _singleton(conn, expense_account_type_code)
    paying_id = get_account_id(
        conn,
        paying_account_type_code,
        ebay_account_id=paying_ebay_account_id,
        wallet_group_id=paying_wallet_group_id,
    )
    common_kwargs = dict(amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used)
    lines = [debit(expense_id, amount_idr, **common_kwargs), credit(paying_id, amount_idr, **common_kwargs)]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="bank_other", lines=lines, memo=memo)


def post_ebay_wallet_operating_expense(
    conn: Connection,
    *,
    ebay_account_id: int,
    entry_date: _dt.date,
    amount_idr: Decimal,
    expense_account_type_code: str = "GENERAL_OPEX",
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    ebay_order_ref: str | None = None,
    memo: str | None = None,
) -> int:
    """A real debit taken directly from an eBay Wallet balance that isn't the
    per-sale Final Value Fee (post_ebay_sale already handles that) —
    specifically eBay's "Other fee" transaction rows (Promoted Listings ad
    fee, Store subscription fee). Per Main-agent's 2026-08-31 decision (see
    CLAUDE.md's Chart of accounts, "Other eBay Wallet debits" note), these
    post to GENERAL_OPEX by default — no dedicated account. Distinct from
    post_operating_expense above only in which asset account is credited
    (EBAY_WALLET here, vs. an explicit paying_account_type_code there).
    """
    _require_decimal(amount_idr, "amount_idr")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")
    expense_id = _singleton(conn, expense_account_type_code)
    ebay_wallet_id = get_account_id(conn, "EBAY_WALLET", ebay_account_id=ebay_account_id)
    common_kwargs = dict(amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used, ebay_order_ref=ebay_order_ref)
    lines = [
        debit(expense_id, amount_idr, **common_kwargs),
        credit(ebay_wallet_id, amount_idr, **common_kwargs),
    ]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="bank_other", lines=lines, memo=memo)


def post_refund_fee_credit(
    conn: Connection,
    *,
    entry_date: _dt.date,
    amount_idr: Decimal,
    stage: str,  # 'ebay_wallet' | 'payoneer' — same two stages as post_refund
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    ebay_order_ref: str | None = None,
    memo: str | None = None,
) -> int:
    """The Final Value Fee credited back to the seller as part of a refund
    event — a distinct, separate posting from post_refund's contra-revenue
    line (see docs/design/milestone-3-ingestion-design.md §2: post_refund's
    existing 2-line shape has no slot for this, so it's a new function
    rather than a change to post_refund's signature/behavior). Debits the
    same cash account the refund itself drew from (eBay Wallet or Payoneer
    Wallet) and credits EBAY_SELLING_FEES back down, mirroring how the fee
    was originally debited to that account in post_ebay_sale.
    """
    _require_decimal(amount_idr, "amount_idr")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")
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

    ebay_fee_id = _singleton(conn, "EBAY_SELLING_FEES")
    common_kwargs = dict(amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used, ebay_order_ref=ebay_order_ref)
    lines = [
        debit(cash_account_id, amount_idr, **common_kwargs),
        credit(ebay_fee_id, amount_idr, **common_kwargs),
    ]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="ebay_refund", lines=lines, memo=memo)


def post_interest_income(
    conn: Connection,
    *,
    entry_date: _dt.date,
    gross_interest_idr: Decimal,
    tax_withheld_idr: Decimal,
    memo: str | None = None,
) -> int:
    """BUNGA (bank-credited interest) on the BCA Main Account, booked NET of
    the small PAJAK BUNGA withholding tax deducted at source — per
    Main-agent's 2026-08-31 decision (both figures immaterial; netting
    avoids a rounding-level opex line for the tax). Debits BCA_MAIN for the
    net amount actually received, credits INTEREST_INCOME for the same net
    amount — a plain 2-line entry, no separate tax-expense line.
    """
    _require_decimal(gross_interest_idr, "gross_interest_idr")
    _require_decimal(tax_withheld_idr, "tax_withheld_idr")
    if tax_withheld_idr > gross_interest_idr:
        raise ValueError("tax_withheld_idr cannot exceed gross_interest_idr")
    net_idr = gross_interest_idr - tax_withheld_idr

    bca_main_id = _singleton(conn, "BCA_MAIN")
    interest_income_id = _singleton(conn, "INTEREST_INCOME")
    lines = [debit(bca_main_id, net_idr), credit(interest_income_id, net_idr)]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="bank_other", lines=lines, memo=memo)


def post_income_line(
    conn: Connection,
    *,
    entry_date: _dt.date,
    income_account_type_code: str,
    amount_idr: Decimal,
    paying_account_type_code: str = "BCA_MAIN",
    paying_ebay_account_id: int | None = None,
    paying_wallet_group_id: int | None = None,
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """Generic sign-aware "post a single bank/Payoneer-statement line against
    an Other-Income/Expense-section INCOME account" posting.

    Added 2026-09-05 as the GENERALIZED form of what was originally
    ``post_interest_income_line`` (hardcoded to INTEREST_INCOME) —
    ``post_interest_income_line`` now delegates here with
    ``income_account_type_code="INTEREST_INCOME"`` and is otherwise
    unchanged (same signature, same behavior, every existing caller
    unaffected). This mirrors ``post_operating_expense``'s own established
    "one generic posting function + an ``..._account_type_code`` parameter"
    reuse pattern (see that function and ``post_ebay_wallet_operating_
    expense``), just applied on the income side.

    Why sign-aware rather than a plain always-inflow posting: an
    Other-Income/Expense-section account can legitimately see either
    direction on different real bank lines — an inflow crediting it (BUNGA
    interest; a genuine, unclassifiable-but-real 'other' inflow — see
    ingestion.matching._post_one_row's 'other' branch and the OTHER_INCOME
    account added alongside this function) or an outflow debiting it back
    down (PAJAK BUNGA withholding tax on that same interest). See the
    original ``post_interest_income_line`` design note (2026-09-02) this
    behavior was first built for.

    - amount_idr > 0 (an inflow): debit the paying/receiving account, credit
      ``income_account_type_code``.
    - amount_idr < 0 (an outflow against that same income-account balance):
      debit ``income_account_type_code``, credit the paying account.

    ``paying_account_type_code``/``paying_ebay_account_id``/
    ``paying_wallet_group_id`` mirror ``post_operating_expense``'s generic
    asset-account resolution (default BCA_MAIN — not hardcoded, any
    wallet/bank account can pay or receive).

    ``amount_usd_ref``/``fx_rate_used`` (same QA-found-bug-fix convention as
    ``post_consignor_reimbursement``/``post_operating_expense`` above) are
    OPTIONAL, tagged on BOTH lines, and only meaningful when
    ``paying_account_type_code`` is ``PAYONEER_WALLET``. Both default None;
    existing callers are unaffected.
    """
    _require_decimal(amount_idr, "amount_idr")
    if amount_idr == 0:
        raise ValueError("amount_idr must be non-zero (a journal line amount must be > 0).")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")

    income_id = _singleton(conn, income_account_type_code)
    paying_id = get_account_id(
        conn,
        paying_account_type_code,
        ebay_account_id=paying_ebay_account_id,
        wallet_group_id=paying_wallet_group_id,
    )
    magnitude = abs(amount_idr)
    common_kwargs = dict(amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used)
    if amount_idr > 0:
        lines = [debit(paying_id, magnitude, **common_kwargs), credit(income_id, magnitude, **common_kwargs)]
    else:
        lines = [debit(income_id, magnitude, **common_kwargs), credit(paying_id, magnitude, **common_kwargs)]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="bank_other", lines=lines, memo=memo)


def post_interest_income_line(
    conn: Connection,
    *,
    entry_date: _dt.date,
    amount_idr: Decimal,
    paying_account_type_code: str = "BCA_MAIN",
    paying_ebay_account_id: int | None = None,
    paying_wallet_group_id: int | None = None,
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """Additive sibling to ``post_interest_income`` (2026-09-02) — posts a
    SINGLE bank-statement line (BUNGA credited interest OR PAJAK BUNGA
    withheld tax on it) to INTEREST_INCOME, direction chosen by the sign of
    ``amount_idr``, rather than requiring both figures known/paired together
    up front the way ``post_interest_income`` does.

    Why this exists instead of reusing ``post_interest_income``: the review
    -queue pipeline posts one journal entry per bank-statement LINE (see
    ``ingestion.matching.post_pending_rows``) — BUNGA and PAJAK BUNGA arrive
    as two separate real bank lines, staged and classified independently,
    often without ever being paired programmatically. Per Main-agent's brief
    (2026-09-02): "two separate real bank lines, two separate traceable
    postings, but the account's own balance nets to the true net interest
    received" — i.e. netting happens naturally because both postings hit the
    SAME INTEREST_INCOME account, not by combining them into one entry.
    ``post_interest_income`` (unchanged, still usable if a caller ever DOES
    have both figures together) is left exactly as it was; nothing here
    replaces it.

    IMPLEMENTATION (2026-09-05): this is now a thin wrapper around the
    generalized ``post_income_line`` (``income_account_type_code=
    "INTEREST_INCOME"``) — see that function's docstring for the full
    sign-aware behavior description. Signature and behavior for every
    existing caller are unchanged.
    """
    return post_income_line(
        conn,
        entry_date=entry_date,
        income_account_type_code="INTEREST_INCOME",
        amount_idr=amount_idr,
        paying_account_type_code=paying_account_type_code,
        paying_ebay_account_id=paying_ebay_account_id,
        paying_wallet_group_id=paying_wallet_group_id,
        amount_usd_ref=amount_usd_ref,
        fx_rate_used=fx_rate_used,
        memo=memo,
    )


# ---------------------------------------------------------------------------
# Opening balance (added 2026-09-03) — a one-time entry recording a wallet/
# bank account's real balance as of just before ledger-tracking began
# (2026-05-01, the earliest posted entry in the real database). See
# CLAUDE.md's Definition of done, the negative-Payoneer-balance gap note,
# and scripts/post_opening_balance.py for the real one-off use of this
# against wallet-group 1's Payoneer Wallet.
# ---------------------------------------------------------------------------

def post_opening_balance(
    conn: Connection,
    *,
    account_type_code: str,
    entry_date: _dt.date,
    amount_idr: Decimal,
    ebay_account_id: int | None = None,
    wallet_group_id: int | None = None,
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """Record a wallet/bank account's real balance as of just before ledger
    -tracking began, as a debit to that SPECIFIC target account (any account
    type/instance — generic and reusable, not hardcoded to Payoneer Wallet
    or any one wallet-group) and a credit to Owner's Capital. Per the user's
    2026-09-03 confirmation: a pre-existing balance predating ledger
    tracking is the owner's own money that was already there, so it books
    to OWNERS_CAPITAL — deliberately NOT ``post_owner_contribution``, which
    hardcodes debiting BCA_MAIN and represents an ongoing capital-injection
    EVENT, not a one-time correction for a balance that already existed
    before tracking began.

    ``amount_usd_ref``/``fx_rate_used`` follow this module's established
    optional-USD-reference convention (positive magnitude, direction
    encoded structurally by debit/credit side — the exact convention
    Milestone 5 fixed a real bug around, see ``post_consignor_reimbursement``
    's docstring) and are tagged on BOTH lines, same as every other function
    here. Only meaningful for a USD-denominated target account (e.g.
    PAYONEER_WALLET or EBAY_WALLET).

    ``source_type='opening_balance'`` is deliberately distinct from
    'owner_contribution' (see above) and is NOT excluded from
    ``scheduling.fx_revaluation.compute_payoneer_wallet_balance``'s USD sum
    (unlike 'fx_revaluation' rows) — this falls out correctly from that
    function's existing logic without any special-casing, because an
    opening balance genuinely represents real USD sitting in the wallet (not
    a restatement of an existing balance at a new rate, which is the only
    reason 'fx_revaluation' rows are excluded there).

    IDEMPOTENCY: this function does NOT itself guard against being called
    twice for the same account — the real backstop is the DB-level unique
    index ``ux_opening_balances_account_id`` on ``opening_balances.
    account_id`` (see ledger/schema.py). A second call for an account that
    already has an opening balance fails at INSERT time with an
    IntegrityError, matching this project's established "DB constraint is
    the real backstop" pattern (see e.g. ``fx_revaluations``' own unique
    index) rather than an app-layer SELECT-before-INSERT that could race.
    """
    _require_decimal(amount_idr, "amount_idr")
    if amount_idr <= 0:
        raise ValueError(
            "amount_idr must be > 0 — an opening balance records a real, nonzero pre-existing balance."
        )
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")

    target_account_id = get_account_id(
        conn, account_type_code, ebay_account_id=ebay_account_id, wallet_group_id=wallet_group_id
    )
    capital_id = _singleton(conn, "OWNERS_CAPITAL")

    common_kwargs = dict(amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used)
    lines = [
        debit(target_account_id, amount_idr, **common_kwargs),
        credit(capital_id, amount_idr, **common_kwargs),
    ]
    entry_id = _insert_journal_entry(
        conn, entry_date=entry_date, source_type="opening_balance", lines=lines, memo=memo
    )

    conn.execute(
        opening_balances.insert().values(
            account_id=target_account_id,
            entry_date=entry_date,
            amount_idr=amount_idr,
            amount_usd_ref=amount_usd_ref,
            fx_rate_used=fx_rate_used,
            journal_entry_id=entry_id,
        )
    )
    return entry_id


# ---------------------------------------------------------------------------
# Reversal (added 2026-09-05) — a DELIBERATE, NARROW, one-time exception to
# "corrections to posted transactions are out of scope for the prototype"
# (see CLAUDE.md's Bank transaction classification section and Definition of
# done). This is NOT a general reopen/reverse workflow: it exists to close
# out exactly one real historical bad posting (journal_entry_id=917 /
# review_queue.id=321 — a real +Rp 50,000 inflow wrongly labeled
# 'cogs_purchase' and posted with its direction flipped), via
# scripts/correct_journal_entry_917.py, and is not wired into any
# app-reachable path (webapp/review_queue_bp.py has no "reverse" action; the
# 2026-08-31 decision that corrections to already-posted rows are out of
# scope for the prototype still stands for the general case).
#
# ``journal_entries.reversed_by_id`` (see ledger/schema.py) was added in
# milestone 2 specifically as a documented placeholder for this future need
# ("the column exists so a future reversal flow is additive, not a
# migration") — using it here, now that a real one-off correction is
# actually needed and explicitly authorized, is that placeholder's intended
# use, not a new ad-hoc mechanism invented to route around the "no
# corrections" rule.
# ---------------------------------------------------------------------------


def post_reversal_entry(
    conn: Connection,
    *,
    original_journal_entry_id: int,
    entry_date: _dt.date,
    memo: str,
) -> int:
    """Post a new journal entry that exactly mirrors
    ``original_journal_entry_id``'s lines with debit/credit swapped on every
    line (same accounts, same amounts, same USD/category/order/consignor
    references) — the standard double-entry-bookkeeping definition of a
    reversal, net effect zero on every account touched. Marks the ORIGINAL
    entry's ``reversed_by_id`` to point at this new entry (so it's visibly,
    permanently flagged as reversed and by what) and refuses to reverse an
    entry that's already been reversed (``reversed_by_id`` already set) —
    never a double-reversal.

    ``memo`` is REQUIRED (unlike every other posting function here, where
    it's optional) — a reversal with no stated reason defeats the whole
    point of this being an audited, deliberate, traceable exception rather
    than a silent undo. Callers should state exactly what's being corrected
    and why (see scripts/correct_journal_entry_917.py's usage).

    Reuses the original entry's own ``source_type`` (the reversal is the
    same class of real-world event, just corrected — this needs no new
    ``ck_journal_entries_source_type`` value).
    """
    if not memo:
        raise ValueError("post_reversal_entry requires a non-empty memo stating what is being corrected and why.")

    original = conn.execute(
        select(journal_entries.c.id, journal_entries.c.source_type, journal_entries.c.reversed_by_id).where(
            journal_entries.c.id == original_journal_entry_id
        )
    ).first()
    if original is None:
        raise ValueError(f"No journal_entries row with id={original_journal_entry_id}")
    if original.reversed_by_id is not None:
        raise ValueError(
            f"journal_entries id={original_journal_entry_id} has already been reversed "
            f"(reversed_by_id={original.reversed_by_id}) — refusing to reverse it twice."
        )

    original_lines = conn.execute(
        select(
            journal_lines.c.account_id,
            journal_lines.c.debit_amount_idr,
            journal_lines.c.credit_amount_idr,
            journal_lines.c.amount_usd_ref,
            journal_lines.c.fx_rate_used,
            journal_lines.c.category_id,
            journal_lines.c.ebay_order_ref,
            journal_lines.c.consignor_item_ref,
        ).where(journal_lines.c.journal_entry_id == original_journal_entry_id)
    ).all()
    if not original_lines:
        raise ValueError(f"journal_entries id={original_journal_entry_id} has no journal_lines to reverse.")

    reversal_lines = [
        Line(
            l.account_id,
            l.credit_amount_idr,  # swapped: original's credit becomes this line's debit
            l.debit_amount_idr,  # swapped: original's debit becomes this line's credit
            amount_usd_ref=l.amount_usd_ref,
            fx_rate_used=l.fx_rate_used,
            category_id=l.category_id,
            ebay_order_ref=l.ebay_order_ref,
            consignor_item_ref=l.consignor_item_ref,
        )
        for l in original_lines
    ]

    reversal_id = _insert_journal_entry(
        conn, entry_date=entry_date, source_type=original.source_type, lines=reversal_lines, memo=memo
    )

    conn.execute(
        update(journal_entries).where(journal_entries.c.id == original_journal_entry_id).values(reversed_by_id=reversal_id)
    )
    return reversal_id


# ---------------------------------------------------------------------------
# Employee loans (added 2026-09-10) — no-interest loans the company gives
# employees, repaid via a salary deduction. Tracked as ONE aggregate asset
# account (EMPLOYEE_LOAN_RECEIVABLE, see ledger/chart_of_accounts.py), same
# "one aggregate account + per-transaction reference" pattern already used
# for CONSIGNOR_PAYABLE — the employee reference reuses the existing,
# already-generic ``consignor_item_ref`` Line field (never touches P&L or
# equity, only ever this asset account and whatever paid/received it).
# ---------------------------------------------------------------------------


def post_employee_loan_disbursement(
    conn: Connection,
    *,
    entry_date: _dt.date,
    amount_idr: Decimal,
    employee_ref: str,
    paying_account_type_code: str = "BCA_MAIN",
    paying_ebay_account_id: int | None = None,
    paying_wallet_group_id: int | None = None,
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """A one-off loan disbursement to an employee — debits
    EMPLOYEE_LOAN_RECEIVABLE (the company is now owed this money back, via
    payroll deduction) and credits whatever account actually paid it out
    (default BCA_MAIN, same generic asset-account resolution as
    ``post_operating_expense``). Never touches P&L or equity.

    ``employee_ref`` is REQUIRED (unlike most optional reference kwargs
    elsewhere in this module) — the whole point of retaining it is
    traceability for an aggregate account with no per-employee sub-ledger
    (see CLAUDE.md's Core accounting rules, Consignor Payable's identical
    reasoning); an unlabeled disbursement would defeat that.

    ``amount_usd_ref``/``fx_rate_used`` follow this module's established
    optional-USD-reference convention (see ``post_consignor_reimbursement``'s
    docstring) — only meaningful when ``paying_account_type_code`` is a
    USD-denominated account (i.e. ``PAYONEER_WALLET``); both default None.
    """
    _require_decimal(amount_idr, "amount_idr")
    if amount_idr <= 0:
        raise ValueError("amount_idr must be > 0 — a disbursement is a real, positive amount lent out.")
    if not employee_ref:
        raise ValueError("employee_ref is required (traceability, same pattern as consignor_item_ref).")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")

    receivable_id = _singleton(conn, "EMPLOYEE_LOAN_RECEIVABLE")
    paying_id = get_account_id(
        conn,
        paying_account_type_code,
        ebay_account_id=paying_ebay_account_id,
        wallet_group_id=paying_wallet_group_id,
    )
    common_kwargs = dict(
        consignor_item_ref=employee_ref, amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used
    )
    lines = [
        debit(receivable_id, amount_idr, **common_kwargs),
        credit(paying_id, amount_idr, **common_kwargs),
    ]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="bank_other", lines=lines, memo=memo)


def post_payroll_with_loan_repayment(
    conn: Connection,
    *,
    entry_date: _dt.date,
    net_transfer_idr: Decimal,
    loan_repayment_idr: Decimal,
    employee_ref: str,
    paying_account_type_code: str = "BCA_MAIN",
    paying_ebay_account_id: int | None = None,
    paying_wallet_group_id: int | None = None,
    amount_usd_ref: Decimal | None = None,
    fx_rate_used: Decimal | None = None,
    memo: str | None = None,
) -> int:
    """A payroll bank transaction that has an employee-loan installment
    deducted from it before the transfer went out — the real, common case
    this exists for is a payroll line whose transferred amount is LESS than
    the employee's normal salary because part of it was withheld to repay a
    loan (see CLAUDE.md's Core accounting rules / the real Fariz Pradana
    loan example). There is no separate payroll record system this can be
    derived from — a human reviewing the transaction supplies
    ``loan_repayment_idr`` directly (see webapp/review_queue_bp.py and
    ingestion.matching._post_one_row's 'payroll' branch).

    Posts a 3-line entry, per Main-agent's brief exactly:
      debit  PAYROLL for the full GROSS amount (net_transfer_idr + loan_repayment_idr)
      credit EMPLOYEE_LOAN_RECEIVABLE for loan_repayment_idr
      credit the paying account for the actual net amount transferred

    This correctly shows the employee's full gross salary cost as a real
    Payroll expense (not understated by the deduction) while the loan
    balance draws down by exactly the installment amount — never touches
    P&L via EMPLOYEE_LOAN_RECEIVABLE (only the PAYROLL line does, same as
    any other payroll cost).

    ``employee_ref`` is REQUIRED — same traceability reasoning as
    ``post_employee_loan_disbursement`` above; there is no other way to know
    which employee's loan balance this repayment should draw down against
    an aggregate receivable account.

    ``amount_usd_ref``/``fx_rate_used`` (added 2026-09-10, QA-found gap — the
    same bug class Milestone 5 already found and fixed elsewhere in this
    module, see ``post_consignor_reimbursement``'s docstring: a posting
    function missing this threading silently corrupts
    ``scheduling.fx_revaluation.compute_payoneer_wallet_balance``'s USD sum
    for any line that touches a Payoneer Wallet) follow this module's
    established optional-USD-reference convention — positive magnitude,
    tagged on ALL THREE lines (matching the "reference on the whole
    TRANSACTION, not just the USD-currency side" convention already used
    elsewhere), only meaningful when ``paying_account_type_code`` is
    ``PAYONEER_WALLET``. Both default None; existing callers unaffected.
    """
    _require_decimal(net_transfer_idr, "net_transfer_idr")
    _require_decimal(loan_repayment_idr, "loan_repayment_idr")
    if net_transfer_idr <= 0:
        raise ValueError("net_transfer_idr must be > 0 — the real amount actually transferred to the employee.")
    if loan_repayment_idr <= 0:
        raise ValueError(
            "loan_repayment_idr must be > 0 — call post_operating_expense(expense_account_type_code="
            "'PAYROLL', ...) instead for a plain payroll line with no embedded loan repayment."
        )
    if not employee_ref:
        raise ValueError("employee_ref is required (traceability, same pattern as consignor_item_ref).")
    if amount_usd_ref is not None:
        _require_decimal(amount_usd_ref, "amount_usd_ref")
    if fx_rate_used is not None:
        _require_decimal(fx_rate_used, "fx_rate_used")

    gross_idr = net_transfer_idr + loan_repayment_idr
    payroll_id = _singleton(conn, "PAYROLL")
    receivable_id = _singleton(conn, "EMPLOYEE_LOAN_RECEIVABLE")
    paying_id = get_account_id(
        conn,
        paying_account_type_code,
        ebay_account_id=paying_ebay_account_id,
        wallet_group_id=paying_wallet_group_id,
    )

    common_kwargs = dict(
        consignor_item_ref=employee_ref, amount_usd_ref=amount_usd_ref, fx_rate_used=fx_rate_used
    )
    lines = [
        debit(payroll_id, gross_idr, **common_kwargs),
        credit(receivable_id, loan_repayment_idr, **common_kwargs),
        credit(paying_id, net_transfer_idr, **common_kwargs),
    ]
    return _insert_journal_entry(conn, entry_date=entry_date, source_type="bank_other", lines=lines, memo=memo)
