"""Test fixtures.

Tests run against a real PostgreSQL database — this is deliberate: the
balance-enforcement and transfer-whitelist invariants are implemented as
Postgres triggers (see ledger/schema.py), and float-vs-Decimal or
constraint-timing bugs don't reliably surface against a different engine.

Point TEST_DATABASE_URL at a disposable Postgres instance before running
the suite, e.g.:

    docker run -d --name noctrowl-test-pg \\
        -e POSTGRES_USER=noctrowl -e POSTGRES_PASSWORD=testpass \\
        -e POSTGRES_DB=noctrowl_test -p 55432:5432 postgres:16-alpine

    export TEST_DATABASE_URL=postgresql+psycopg2://noctrowl:testpass@localhost:55432/noctrowl_test
    pytest

Never point this at a real/production database — the schema is dropped and
recreated for every test. There is deliberately NO fallback to
DATABASE_URL (see tests/_db_safety.py) — a 2026-09 incident lost 880 real
posted journal entries exactly that way, when DATABASE_URL was pointed at
the real `noctrowl` database and TEST_DATABASE_URL wasn't set. Both
TEST_DATABASE_URL being required AND the resolved database name needing to
contain "test" are enforced in tests/_db_safety.py and, as a second
independent layer, inside ledger.schema.drop_schema() itself.

Note on transactions: most tests share one open (uncommitted) connection
transaction per test, which is enough because ledger/posting.py's own
Python-side balance check runs *before* any row is written — bad data never
reaches the DB in the normal path. The one test that deliberately bypasses
posting.py to prove the Postgres deferred-constraint trigger is a real,
independent backstop (test_schema.py) forces an immediate constraint check
with ``SET CONSTRAINTS ALL IMMEDIATE`` rather than relying on this fixture.
"""
from __future__ import annotations

import pytest

from ledger.db import get_engine
from ledger.schema import create_schema, drop_schema
from ledger.seed import seed_catalogs, seed_full_topology, seed_prototype_topology
from tests._db_safety import resolve_test_database_url


@pytest.fixture()
def engine():
    eng = get_engine(resolve_test_database_url())
    drop_schema(eng)
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def conn(engine):
    """A connection with the global catalogs (account types, categories,
    consignor payout tiers) already seeded. Wraps the test in one open
    transaction; teardown rolls it back (the next test gets a fresh schema
    from the `engine` fixture regardless).
    """
    with engine.connect() as connection:
        seed_catalogs(connection)
        yield connection
        connection.rollback()


@pytest.fixture()
def prototype(conn):
    """A conn with the catalogs seeded plus the milestone's prototype
    topology: one eBay account + its own wallet-group.
    """
    topo = seed_prototype_topology(conn)
    return conn, topo


@pytest.fixture()
def full_topology(conn):
    """A conn with the catalogs seeded plus the business's full structural
    shape: 3 eBay accounts across 2 wallet-groups (one shared).
    """
    topo = seed_full_topology(conn)
    return conn, topo
