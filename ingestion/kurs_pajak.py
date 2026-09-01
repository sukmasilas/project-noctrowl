"""Kurs Pajak (Indonesia's weekly tax reference exchange rate) lookup.

Per Main-agent's 2026-08-31 resolution (design doc §0, question 3): the
prototype sources this from a plain, manually-seeded reference table
(``ingestion.schema.kurs_pajak_rates``), not an automated Kemenkeu scraper.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ingestion.schema import kurs_pajak_rates


class NoKursPajakRateError(Exception):
    """Raised when no seeded rate covers a requested date — never silently
    fall back to a fabricated rate for money-math correctness.
    """


def seed_kurs_pajak_rate(conn: Connection, *, effective_date: _dt.date, rate_idr: Decimal) -> int:
    """Insert one weekly rate. Idempotent-friendly: callers should upsert
    (delete-then-insert or ON CONFLICT) if re-seeding the same date; this
    function itself just inserts, matching ledger/seed.py's plain-insert
    style for other catalogs.
    """
    if not isinstance(rate_idr, Decimal):
        raise TypeError("rate_idr must be a decimal.Decimal, never a float")
    result = conn.execute(
        kurs_pajak_rates.insert().values(effective_date=effective_date, rate_idr=rate_idr)
    )
    return result.inserted_primary_key[0]


def lookup_kurs_pajak_rate(conn: Connection, entry_date: _dt.date) -> Decimal:
    """The most recently published rate as of ``entry_date`` (Kemenkeu
    publishes weekly, effective from a given date until superseded) — i.e.
    the row with the largest ``effective_date <= entry_date``.

    Raises NoKursPajakRateError if no seeded rate covers this date, rather
    than guessing (e.g. falling back to the nearest rate on the wrong side,
    or a default of 1). A booking that can't find its rate should stop, not
    post a wrong number.
    """
    row = conn.execute(
        select(kurs_pajak_rates.c.rate_idr)
        .where(kurs_pajak_rates.c.effective_date <= entry_date)
        .order_by(kurs_pajak_rates.c.effective_date.desc())
        .limit(1)
    ).first()
    if row is None:
        raise NoKursPajakRateError(
            f"No kurs_pajak_rates row with effective_date <= {entry_date} — seed a rate "
            "covering this date before ingesting transactions for it."
        )
    return row.rate_idr


def lookup_most_recent_rate_as_of(conn: Connection, as_of_date: _dt.date) -> Decimal:
    """Alias for ``lookup_kurs_pajak_rate`` used specifically for the
    Payoneer-withdrawal ``booking_rate_used_idr`` approximation (Main-agent's
    2026-08-31 resolution to design doc §0 question 4: most recent week's
    rate as of the withdrawal date, not a weighted-average-of-original
    -booking-rates approach). Named separately from the plain lookup purely
    for call-site clarity about which of the two purposes it's serving —
    the underlying logic is identical.
    """
    return lookup_kurs_pajak_rate(conn, as_of_date)
