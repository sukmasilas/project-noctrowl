"""Jinja template filters shared across screens (currency/date formatting).

Pure presentation helpers — no money math happens here, only formatting of
values already computed by webapp/reporting.py or webapp/finalization.py.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from flask import Flask


def format_idr(value) -> str:
    if value is None:
        return "—"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    sign = "-" if value < 0 else ""
    whole = abs(int(value))
    grouped = f"{whole:,}".replace(",", ".")
    return f"{sign}Rp {grouped}"


def format_period(value: _dt.date | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%B %Y")


def format_period_short(value: _dt.date | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m")


def register_template_filters(app: Flask) -> None:
    app.jinja_env.filters["idr"] = format_idr
    app.jinja_env.filters["period"] = format_period
    app.jinja_env.filters["period_short"] = format_period_short
