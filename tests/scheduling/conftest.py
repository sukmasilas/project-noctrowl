"""Fixtures for milestone-5 scheduling tests.

Unlike most of this project's other test suites (which share one open,
uncommitted transaction per test — see tests/conftest.py's note), the
scheduling jobs under test here (``run_routine_sync``, ``run_fx_
revaluation``, ``run_drive_folder_provisioning``) all take an ``Engine``
and open their OWN fresh connection internally (``with engine.connect()
as conn:``), exactly as they will when invoked for real from a cron
script. A separate connection can never see another connection's
still-open, uncommitted transaction, so any setup data these tests need
must be genuinely COMMITTED first — same reasoning, and same pattern
(wengine/wconn/wtopology), as tests/webapp/conftest.py.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

# Registers milestone-3's tables (kurs_pajak_rates, review_queue, etc.) on
# the SAME shared SQLAlchemy MetaData object ledger.schema uses.
import ingestion.schema as _ischema  # noqa: F401
from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.seed import seed_bank_keyword_rules
from ledger.db import get_engine
from ledger.schema import create_schema, drop_schema
from ledger.seed import seed_catalogs, seed_full_topology, seed_prototype_topology
from tests._db_safety import resolve_test_database_url


@pytest.fixture()
def sengine():
    eng = get_engine(resolve_test_database_url())
    drop_schema(eng)
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def sconn(sengine):
    """A COMMITTED-seed connection (catalogs + bank keyword rules). Closed
    (not rolled back) at teardown — the next test gets a fully fresh
    schema from ``sengine`` regardless.
    """
    with sengine.connect() as connection:
        seed_catalogs(connection)
        seed_bank_keyword_rules(connection)
        connection.commit()
        yield connection


@pytest.fixture()
def stopology(sconn):
    """catalogs + prototype topology (one eBay account + its wallet-group),
    committed, plus a handful of Kurs Pajak rates covering a representative
    test window (July-September 2026).
    """
    topo = seed_prototype_topology(sconn)
    seed_kurs_pajak_rate(sconn, effective_date=_dt.date(2026, 7, 6), rate_idr=Decimal("16300.0000"))
    seed_kurs_pajak_rate(sconn, effective_date=_dt.date(2026, 7, 27), rate_idr=Decimal("16350.0000"))
    seed_kurs_pajak_rate(sconn, effective_date=_dt.date(2026, 8, 3), rate_idr=Decimal("16400.0000"))
    seed_kurs_pajak_rate(sconn, effective_date=_dt.date(2026, 8, 31), rate_idr=Decimal("16420.0000"))
    seed_kurs_pajak_rate(sconn, effective_date=_dt.date(2026, 9, 7), rate_idr=Decimal("16450.0000"))
    seed_kurs_pajak_rate(sconn, effective_date=_dt.date(2026, 9, 30), rate_idr=Decimal("16480.0000"))
    sconn.commit()
    return sconn, topo


@pytest.fixture()
def sfull_topology(sconn):
    """catalogs + the business's full structural shape (3 eBay accounts
    across 2 wallet-groups, one shared) — committed. Used by tests proving
    a job scopes generically across every active wallet-group rather than
    hardcoding "there's exactly one".
    """
    topo = seed_full_topology(sconn)
    sconn.commit()
    return sconn, topo
