"""Reproducibly apply ledger.migrations.run_migrations() against a real,
already-provisioned database (never drops/recreates anything — safe against
the live ``noctrowl`` database, unlike the test suite's drop_schema()).

WHY THIS EXISTS (2026-09-02): the ``drive_folder_name`` fix was first applied
to the real ``noctrowl`` database by hand (a one-off ALTER TABLE + UPDATE run
directly in a REPL) — QA flagged that as unreproducible and undocumented,
since nothing in the codebase records that it happened or lets it be redone
against a different/future database. ``ledger.migrations`` is the real,
idempotent mechanism this script drives; this script is just its explicit,
logged, standalone entrypoint for "apply this to an existing running
database" (as opposed to the automatic call inside
``ledger.schema.create_schema()``, which only ever runs as part of a full
create-from-scratch — the path every test fixture uses, but NOT the path
that's ever been used against the real droplet database, which was
provisioned once, long ago, and has been running continuously since).

USAGE:
    python3 scripts/run_migrations.py

Reads DATABASE_URL from the environment (via python-dotenv, same as
wsgi.py) — never hardcodes a connection string. Prints one line per
migration step: "already_present" (no change needed), "applied" (this run
just made the change), "ensured" (a constraint-widening step — always
re-run, DROP+ADD is safe regardless of prior state), or
"skipped_table_missing" (this step's table isn't provisioned at this layer
yet — expected/harmless on a milestone-2-only database).

Imports ingestion.schema and webapp.schema before running (registers their
tables on the shared metadata object) so ``ledger.schema.create_schema``'s
own ``metadata.create_all()`` call — a checkfirst no-op against an already-
provisioned database, never destructive — also has the full current shape
to compare against; this does NOT create anything that isn't already there,
it's the same idempotent create_all() every test fixture already relies on.
"""
from __future__ import annotations

import os
import sys

# Allow this script to be run directly (e.g. `python3 scripts/run_migrations.py`
# from the project root, as documented in USAGE above) — same fix as
# scripts/authorize_google_drive.py: sys.path[0] is otherwise this file's own
# directory (scripts/), not the project root, so the top-level `ingestion`/
# `ledger`/`webapp` packages wouldn't be importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import ingestion.schema  # noqa: E402,F401 - registers milestone-3 tables on the shared metadata
import webapp.schema  # noqa: E402,F401 - registers milestone-4 tables on the shared metadata
from ledger.db import get_engine  # noqa: E402
from ledger.migrations import run_migrations  # noqa: E402
from ledger.schema import metadata  # noqa: E402


def main() -> None:
    engine = get_engine()
    print(f"Connecting to database: {engine.url.database!r} (host hidden)")

    # checkfirst create_all: only creates tables that don't exist yet, never
    # touches an existing one — the same call ledger.schema.create_schema()
    # already makes, exposed here explicitly so this script can run
    # standalone against a database that was never provisioned via
    # create_schema() in the first place.
    metadata.create_all(engine)

    results = run_migrations(engine)
    if not results:
        print("No migrations ran (non-Postgres engine, or nothing registered).")
        return

    print(f"\n{len(results)} migration step(s) evaluated:\n")
    for r in results:
        print(f"  [{r.status:>22}] {r.id}")
    print()

    applied = [r for r in results if r.status == "applied"]
    if applied:
        print(f"{len(applied)} step(s) made a real change just now:")
        for r in applied:
            print(f"  - {r.id}: {r.detail}")
    else:
        print("No step made a new change — database was already fully up to date.")


if __name__ == "__main__":
    main()
