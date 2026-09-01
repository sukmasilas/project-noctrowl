"""Fixtures for milestone-4 web app tests. Reuses the same disposable
Postgres instance as tests/conftest.py and tests/ingestion/conftest.py
(TEST_DATABASE_URL, falls back to DATABASE_URL).

Unlike the rollback-per-test pattern the other conftests use, web-app
integration tests need COMMITTED seed data: Flask's test client drives real
HTTP requests through webapp/db.py, which opens its own NEW connection per
request from the shared engine — a separate connection can never see an
still-open, uncommitted transaction on another connection, so seed data has
to actually be committed for a request handler to see it. The schema is
still fully dropped and recreated per test (function-scoped ``wengine``),
so nothing leaks between tests despite the commits.
"""
from __future__ import annotations

import datetime as _dt
import os
from decimal import Decimal

import pytest

# Importing these registers milestone-3 and milestone-4's tables on the
# SAME shared SQLAlchemy MetaData object ledger.schema uses, so
# ledger.schema.create_schema() below picks all three milestones up
# together via one metadata.create_all() call.
import ingestion.schema as _ischema  # noqa: F401
import webapp.schema as _wschema  # noqa: F401
from ingestion.kurs_pajak import seed_kurs_pajak_rate
from ingestion.schema import review_queue, source_documents
from ledger.db import get_engine
from ledger.schema import create_schema, drop_schema
from ledger.seed import seed_catalogs, seed_prototype_topology
from webapp import create_app


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL (or DATABASE_URL) is not set — point it at a "
            "disposable PostgreSQL database to run the web app test suite."
        )
    return url


@pytest.fixture()
def wengine():
    eng = get_engine(_test_database_url())
    drop_schema(eng)
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def wconn(wengine):
    """A COMMITTED-seed connection (catalogs only). Closed (not rolled
    back) at teardown — the next test gets a fully fresh schema from
    ``wengine`` regardless.
    """
    with wengine.connect() as connection:
        seed_catalogs(connection)
        connection.commit()
        yield connection


@pytest.fixture()
def wtopology(wconn):
    """catalogs + prototype topology (one eBay account + its wallet-group),
    committed, plus a Kurs Pajak rate covering a representative test month.
    """
    topo = seed_prototype_topology(wconn)
    seed_kurs_pajak_rate(wconn, effective_date=_dt.date(2026, 7, 6), rate_idr=Decimal("16300.0000"))
    wconn.commit()
    return wconn, topo


@pytest.fixture()
def login_env(monkeypatch):
    monkeypatch.setenv("APP_LOGIN_USERNAME", "testuser")
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "testpass")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-not-for-production")
    return "testuser", "testpass"


@pytest.fixture()
def app(wengine, login_env):
    flask_app = create_app(engine=wengine, drive_client=None)
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def logged_in_client(client, login_env):
    username, password = login_env
    resp = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    return client


def make_source_document(conn, *, document_type: str, period_month: _dt.date, ingested: bool = True, **scope) -> int:
    result = conn.execute(
        source_documents.insert().values(
            document_type=document_type,
            period_month=period_month,
            drive_file_name="test-fixture",
            ingested_at=_dt.datetime.now(_dt.timezone.utc) if ingested else None,
            **scope,
        )
    )
    return result.inserted_primary_key[0]


def make_review_queue_row(
    conn,
    *,
    source_document_id: int,
    transaction_date: _dt.date,
    amount_idr: Decimal = Decimal("100000"),
    match_status: str = "needs_review",
    source_type: str = "bank_statement",
    raw_description: str = "TEST LINE",
    **scope,
) -> int:
    result = conn.execute(
        review_queue.insert().values(
            source_type=source_type,
            source_document_id=source_document_id,
            transaction_date=transaction_date,
            amount_idr=amount_idr,
            raw_description=raw_description,
            match_status=match_status,
            **scope,
        )
    )
    return result.inserted_primary_key[0]
