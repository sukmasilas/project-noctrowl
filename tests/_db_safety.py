"""Shared safety guard for resolving the disposable test database URL.

Exists to prevent a repeat of the 2026-09 incident: `tests/conftest.py`'s
`_test_database_url()` fell back to `DATABASE_URL` when `TEST_DATABASE_URL`
wasn't explicitly set, and the `engine` fixture unconditionally called
`drop_schema()`/`create_schema()` on whatever database that resolved to.
`DATABASE_URL` was later pointed at the real, persistent `noctrowl`
database for live validation work, and a subsequent `pytest` run (that
didn't think to set `TEST_DATABASE_URL`) silently dropped and recreated
that database's schema, destroying 880 real posted journal entries.

Two independent guards, both required (belt and suspenders):

1. `TEST_DATABASE_URL` must be set explicitly. There is deliberately NO
   fallback to `DATABASE_URL` here — `DATABASE_URL` is allowed to point at
   a real, persistent database (e.g. the droplet's `noctrowl` DB) and must
   never be touched by the test suite's schema drop/recreate.
2. The resolved database name must look like a disposable test database
   (contains "test", case-insensitive). A database literally named
   `noctrowl` is refused even if it somehow arrives via `TEST_DATABASE_URL`
   — a misconfigured environment variable is exactly the kind of mistake
   that caused the original incident.

Guard #2 is also enforced structurally inside `ledger.schema.drop_schema()`
itself (so it protects any caller, not just this module), but it's
duplicated here so misconfiguration is caught before the fixture even
builds an engine, with a message pointed at the right file.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when the resolved test database doesn't look disposable."""


def resolve_test_database_url() -> str:
    """Return TEST_DATABASE_URL, or raise loudly. Never falls back to
    DATABASE_URL — see module docstring.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is not set. The test suite drops and recreates "
            "its schema against this database on every run, so it must NEVER "
            "fall back to DATABASE_URL (which may point at a real, persistent "
            "database). Set TEST_DATABASE_URL explicitly to a disposable "
            "Postgres instance, e.g.:\n\n"
            "    docker run -d --name noctrowl-test-pg \\\n"
            "        -e POSTGRES_USER=noctrowl -e POSTGRES_PASSWORD=testpass \\\n"
            "        -e POSTGRES_DB=noctrowl_test -p 55432:5432 postgres:16-alpine\n\n"
            "    export TEST_DATABASE_URL=postgresql+psycopg2://noctrowl:testpass@localhost:55432/noctrowl_test\n"
            "    pytest\n"
        )

    db_name = (urlsplit(url).path or "").lstrip("/")
    if "test" not in db_name.lower():
        raise UnsafeTestDatabaseError(
            f"Refusing to run the test suite against database {db_name!r} "
            "(resolved from TEST_DATABASE_URL). The test suite drops and "
            "recreates its schema on every run, which is destructive if this "
            "is actually a real/persistent database. The database name must "
            "contain 'test' (e.g. 'noctrowl_test') to proceed. If this really "
            "is meant to be disposable, rename it or point TEST_DATABASE_URL "
            "at one that follows this convention."
        )

    return url
