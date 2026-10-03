"""Review Queue / Wallet / Journal Entries behavior for the SYSTEM-ONLY
'legacy_ebay_account_payout' category (added 2026-10-03)."""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from sqlalchemy import select, update

from ingestion.matching import post_pending_rows
from ingestion.schema import review_queue
from tests.webapp.conftest import make_review_queue_row, make_source_document
from webapp.review_queue_bp import CATEGORY_OPTIONS, SYSTEM_CATEGORY_LABELS

PERIOD = _dt.date(2026, 7, 1)
DAY = _dt.date(2026, 7, 5)
CAT = "legacy_ebay_account_payout"


def _master_row(conn):
    src = make_source_document(conn, document_type="bank_statement_master", period_month=PERIOD)
    return make_review_queue_row(
        conn, source_document_id=src, transaction_date=DAY, amount_idr=Decimal("48643836"),
        raw_description="KR OTOMATIS LLG-MANDIRI 0938 / NUSA SATU INTI ART",
    )


def test_not_in_dropdown_options():
    assert CAT not in [c for c, _ in CATEGORY_OPTIONS]
    assert CAT in SYSTEM_CATEGORY_LABELS


def test_crafted_post_with_system_category_is_rejected(client, wtopology):
    conn, _ = wtopology
    row_id = _master_row(conn)
    conn.commit()
    for headers in ({}, {"X-Requested-With": "XMLHttpRequest"}):
        resp = client.post(
            f"/review-queue/{row_id}",
            data={"category": CAT, "consignor_item_ref": "", "period": PERIOD.isoformat()},
            headers=headers,
        )
        if headers:
            assert resp.status_code == 400
    row = conn.execute(select(review_queue).where(review_queue.c.id == row_id)).one()
    assert row.category is None and row.labeled_at is None


def test_posted_row_renders_everywhere(client, wtopology):
    conn, topo = wtopology
    row_id = _master_row(conn)
    conn.execute(update(review_queue).where(review_queue.c.id == row_id).values(category=CAT))
    assert post_pending_rows(conn).posted == 1
    conn.commit()

    body = client.get(f"/review-queue/?period={PERIOD.isoformat()[:7]}").get_data(as_text=True)
    assert "Legacy eBay Account Payout (system-applied)" in body
    assert f">{CAT}<" not in body

    from webapp.wallet_bp import list_wallet_options, wallet_register

    bca_main_opt = next(o for o in list_wallet_options(conn) if o.account_type_code == "BCA_MAIN")
    txns = wallet_register(conn, bca_main_opt, period_month=PERIOD)
    assert [t.category_label for t in txns] == ["Legacy eBay Account Payout (system-applied)"]
    wbody = client.get(f"/wallet/?period=2026-07&account_id={bca_main_opt.account_id}").get_data(as_text=True)
    assert "Legacy eBay Account Payout (system-applied)" in wbody

    resp = client.get("/journal-entries/?period=2026-07")
    assert resp.status_code == 200
    assert "NET of" in resp.get_data(as_text=True)
