"""Fixtures for milestone-3 ingestion tests. Reuses the same disposable
Postgres instance as tests/conftest.py (TEST_DATABASE_URL, falls back to
DATABASE_URL) — see that file's docstring for how to stand one up.
"""
from __future__ import annotations

import datetime as _dt
import os
from decimal import Decimal

import pytest

# Importing ingestion.schema registers milestone-3's tables on the SAME
# SQLAlchemy MetaData object ledger.schema uses, so ledger.schema's own
# create_schema()/drop_schema() (called below) pick them up automatically.
import ingestion.schema as ischema  # noqa: F401
from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.schema import source_documents
from ledger.db import get_engine
from ledger.schema import create_schema, drop_schema
from ledger.seed import seed_catalogs, seed_prototype_topology


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL (or DATABASE_URL) is not set — point it at a "
            "disposable PostgreSQL database to run the ingestion test suite."
        )
    return url


@pytest.fixture()
def iengine():
    eng = get_engine(_test_database_url())
    drop_schema(eng)
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def iconn(iengine):
    with iengine.connect() as connection:
        seed_catalogs(connection)
        yield connection
        connection.rollback()


@pytest.fixture()
def iprototype(iconn):
    """catalogs + prototype topology (one eBay account + its wallet-group)
    + a Kurs Pajak rate covering the real eBay sample's July 2026 dates and
    the following payout-settlement window into August 2026.
    """
    topo = seed_prototype_topology(iconn)
    seed_kurs_pajak_rate(iconn, effective_date=_dt.date(2026, 6, 29), rate_idr=Decimal("16250.0000"))
    seed_kurs_pajak_rate(iconn, effective_date=_dt.date(2026, 7, 6), rate_idr=Decimal("16300.0000"))
    seed_kurs_pajak_rate(iconn, effective_date=_dt.date(2026, 7, 13), rate_idr=Decimal("16280.0000"))
    seed_kurs_pajak_rate(iconn, effective_date=_dt.date(2026, 7, 20), rate_idr=Decimal("16310.0000"))
    seed_kurs_pajak_rate(iconn, effective_date=_dt.date(2026, 7, 27), rate_idr=Decimal("16350.0000"))
    seed_kurs_pajak_rate(iconn, effective_date=_dt.date(2026, 8, 3), rate_idr=Decimal("16400.0000"))
    return iconn, topo


def make_source_document(conn, *, document_type: str, period_month: _dt.date, **scope) -> int:
    result = conn.execute(
        source_documents.insert().values(
            document_type=document_type,
            period_month=period_month,
            drive_file_name="test-fixture",
            **scope,
        )
    )
    return result.inserted_primary_key[0]
