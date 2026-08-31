"""Database engine/connection management.

The Postgres connection string comes ONLY from the DATABASE_URL environment
variable (see .env.example at the repo root). Never hardcode a connection
string, host, username, or password here or anywhere else in this package.

Tech stack is confirmed PostgreSQL (see CLAUDE.md). We deliberately do not
provide a silent SQLite fallback: the invariant that makes an unbalanced
double-entry posting impossible to persist is implemented as a Postgres
deferred constraint trigger (see ledger/schema.py). A different database
engine would silently lose that structural guarantee, which is exactly the
kind of money-math correctness gap this project can't afford.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


class MissingDatabaseUrlError(RuntimeError):
    """Raised when DATABASE_URL is not set in the environment."""


class UnsupportedDatabaseError(RuntimeError):
    """Raised when DATABASE_URL points at a non-Postgres database."""


def get_engine(database_url: str | None = None, **engine_kwargs) -> Engine:
    """Build a SQLAlchemy engine from DATABASE_URL (or an explicit override).

    ``database_url`` is only ever meant to be passed explicitly in tests
    (e.g. pointing at a disposable local test database) — application code
    should always call ``get_engine()`` with no arguments and rely on the
    environment variable, so no connection string is ever hardcoded.
    """
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise MissingDatabaseUrlError(
            "DATABASE_URL is not set. Copy .env.example to .env and set a "
            "real PostgreSQL connection string there (never hardcode "
            "credentials in code)."
        )
    if not (url.startswith("postgresql://") or url.startswith("postgresql+")):
        raise UnsupportedDatabaseError(
            f"DATABASE_URL scheme {url.split(':', 1)[0]!r} is not supported. "
            "Project-Noctrowl's ledger requires PostgreSQL (confirmed in "
            "CLAUDE.md) — the balance-enforcement trigger in ledger/schema.py "
            "depends on Postgres-specific features."
        )
    return create_engine(url, future=True, **engine_kwargs)
