"""Per-request database connection handling.

Deliberately thin: this module does NOT define a second engine/connection
concept. It calls ``ledger.db.get_engine()`` (the existing, single
DATABASE_URL reader — see that module's docstring for why there's no
SQLite fallback) once at app-startup and hands out one connection per
Flask request via ``flask.g``, closed in a ``teardown_appcontext`` hook.

Write routes must call ``conn.commit()`` explicitly after a successful
write (SQLAlchemy 2.0 "future" connections auto-begin a transaction on
first use and never auto-commit) — read-only routes don't need to, since
``conn.close()`` on teardown implicitly rolls back any still-open
transaction, which is harmless for a read-only request.
"""
from __future__ import annotations

from flask import Flask, g
from sqlalchemy.engine import Connection, Engine

from ledger.db import get_engine


def init_app(app: Flask, *, engine: Engine | None = None) -> None:
    app.config["DB_ENGINE"] = engine or get_engine()

    @app.teardown_appcontext
    def _close_db(exception: BaseException | None) -> None:  # noqa: ARG001
        conn: Connection | None = g.pop("db_conn", None)
        if conn is not None:
            conn.close()


def get_db() -> Connection:
    """Return this request's connection, opening one on first use."""
    from flask import current_app

    if "db_conn" not in g:
        engine: Engine = current_app.config["DB_ENGINE"]
        g.db_conn = engine.connect()
    return g.db_conn
