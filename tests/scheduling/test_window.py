"""Pure date-logic tests for scheduling.window — no DB, no fixtures. Per
this milestone's brief: boundary days (exactly day 15/day 7), a Dec 31 ->
Jan 1 year rollover, and a leap-year February.
"""
from __future__ import annotations

import datetime as dt

import pytest

from scheduling.window import (
    closed_period_for,
    is_post_close_tail_active,
    is_pre_close_window_active,
    is_routine_sync_active,
    month_end,
    next_month_start,
    periods_to_sync,
    previous_month_end,
)


# ---------------------------------------------------------------------------
# month_end / previous_month_end / next_month_start
# ---------------------------------------------------------------------------


def test_month_end_31_day_month():
    assert month_end(dt.date(2026, 8, 15)) == dt.date(2026, 8, 31)


def test_month_end_leap_year_february():
    assert month_end(dt.date(2028, 2, 10)) == dt.date(2028, 2, 29)


def test_month_end_non_leap_year_february():
    assert month_end(dt.date(2026, 2, 10)) == dt.date(2026, 2, 28)


def test_previous_month_end_rolls_year_backward():
    assert previous_month_end(dt.date(2027, 1, 15)) == dt.date(2026, 12, 31)


def test_next_month_start_rolls_year_forward():
    assert next_month_start(dt.date(2026, 12, 15)) == dt.date(2027, 1, 1)


def test_next_month_start_ordinary_month():
    assert next_month_start(dt.date(2026, 8, 5)) == dt.date(2026, 9, 1)


# ---------------------------------------------------------------------------
# Window (a): 15 days before month-end, current month
# ---------------------------------------------------------------------------


def test_window_a_active_exactly_at_day_15_boundary():
    # August has 31 days; Aug 31 - 15 = Aug 16.
    assert is_pre_close_window_active(dt.date(2026, 8, 16)) is True


def test_window_a_inactive_one_day_before_day_15_boundary():
    assert is_pre_close_window_active(dt.date(2026, 8, 15)) is False


def test_window_a_active_on_month_end_itself():
    assert is_pre_close_window_active(dt.date(2026, 8, 31)) is True


def test_window_a_active_short_february_non_leap():
    # Feb 2026 has 28 days; 28-15=13 -> Feb 13 is the boundary.
    assert is_pre_close_window_active(dt.date(2026, 2, 13)) is True
    assert is_pre_close_window_active(dt.date(2026, 2, 12)) is False


def test_window_a_active_leap_year_february():
    # Feb 2028 has 29 days; 29-15=14 -> Feb 14 is the boundary.
    assert is_pre_close_window_active(dt.date(2028, 2, 14)) is True
    assert is_pre_close_window_active(dt.date(2028, 2, 13)) is False


# ---------------------------------------------------------------------------
# Window (b): 7 days after the end of the PREVIOUS month
# ---------------------------------------------------------------------------


def test_window_b_active_exactly_at_day_7_boundary():
    # July ends July 31; July31 + 7 = Aug 7.
    assert is_post_close_tail_active(dt.date(2026, 8, 7)) is True


def test_window_b_inactive_one_day_after_day_7_boundary():
    assert is_post_close_tail_active(dt.date(2026, 8, 8)) is False


def test_window_b_active_on_first_day_of_month():
    assert is_post_close_tail_active(dt.date(2026, 8, 1)) is True


def test_window_b_active_across_year_rollover():
    # Dec 31 -> Jan 1 rollover: Jan 1-7 should be active via window (b).
    assert is_post_close_tail_active(dt.date(2027, 1, 1)) is True
    assert is_post_close_tail_active(dt.date(2027, 1, 7)) is True
    assert is_post_close_tail_active(dt.date(2027, 1, 8)) is False


# ---------------------------------------------------------------------------
# Mid-month idle gap: neither window active
# ---------------------------------------------------------------------------


def test_mid_month_idle_gap_is_inactive():
    # August 2026: window (b) covers Aug 1-7, window (a) covers Aug 16-31.
    # Aug 8-15 should be fully idle.
    for day in range(8, 16):
        d = dt.date(2026, 8, day)
        assert is_routine_sync_active(d) is False, f"expected {d} to be idle"


def test_windows_bracket_the_idle_gap_correctly():
    for day in range(1, 8):
        assert is_routine_sync_active(dt.date(2026, 8, day)) is True
    for day in range(16, 32):
        assert is_routine_sync_active(dt.date(2026, 8, day)) is True


# ---------------------------------------------------------------------------
# periods_to_sync
# ---------------------------------------------------------------------------


def test_periods_to_sync_empty_outside_window():
    assert periods_to_sync(dt.date(2026, 8, 10)) == []


def test_periods_to_sync_current_month_only_in_window_a():
    assert periods_to_sync(dt.date(2026, 8, 20)) == [dt.date(2026, 8, 1)]


def test_periods_to_sync_current_and_previous_month_in_window_b():
    assert periods_to_sync(dt.date(2026, 9, 3)) == [dt.date(2026, 9, 1), dt.date(2026, 8, 1)]


def test_periods_to_sync_year_rollover_in_window_b():
    assert periods_to_sync(dt.date(2027, 1, 3)) == [dt.date(2027, 1, 1), dt.date(2026, 12, 1)]


def test_periods_to_sync_leap_year_february_boundary():
    # Feb 2028 (leap): window (a) starts Feb 14; window (b) covers Jan 1-7.
    assert periods_to_sync(dt.date(2028, 2, 14)) == [dt.date(2028, 2, 1)]
    assert periods_to_sync(dt.date(2028, 2, 1)) == [dt.date(2028, 2, 1), dt.date(2028, 1, 1)]


# ---------------------------------------------------------------------------
# closed_period_for (month-end FX revaluation job's period selection)
# ---------------------------------------------------------------------------


def test_closed_period_for_ordinary_month():
    assert closed_period_for(dt.date(2026, 9, 3)) == dt.date(2026, 8, 1)


def test_closed_period_for_year_rollover():
    assert closed_period_for(dt.date(2027, 1, 5)) == dt.date(2026, 12, 1)


def test_closed_period_for_stays_the_same_throughout_the_h7_tail():
    # CLAUDE.md: the job "may actually execute a few days later during the
    # H+7 revision window" but must always resolve to the SAME closed
    # period regardless of which day within that tail it actually runs.
    expected = dt.date(2026, 8, 1)
    for day in range(1, 8):
        assert closed_period_for(dt.date(2026, 9, day)) == expected


@pytest.mark.parametrize("year", [2024, 2026, 2028, 2032])
def test_window_boundaries_never_crash_across_several_years(year):
    # Smoke test across a range of years (including leap and non-leap) to
    # make sure nothing here ever raises for a plain valid calendar date.
    d = dt.date(year, 1, 1)
    one_year_later = dt.date(year + 1, 1, 1)
    step = dt.timedelta(days=17)  # coprime-ish stride, hits varied days-of-month
    while d < one_year_later:
        is_routine_sync_active(d)
        periods_to_sync(d)
        closed_period_for(d)
        next_month_start(d)
        d += step
