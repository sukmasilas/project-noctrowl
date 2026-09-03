"""Pure, I/O-free date logic backing every scheduled job in this package.

No DB connection, no Drive client, no implicit ``datetime.date.today()``
call buried inside any of these functions — every function here takes
``today`` as an explicit parameter. That's deliberate: it's what makes the
window/boundary logic (a real source of off-by-one bugs — month-end
rollovers, leap years, "is day 15 itself inside or outside the window")
unit-testable against fixed dates without mocking a clock anywhere. The
callers in ``scheduling.routine_sync`` / ``scheduling.fx_revaluation`` /
``scheduling.drive_provisioning`` are the only places that ever read the
real clock (via ``today: date | None = None`` defaulting to
``date.today()``), and only at the very top of each run.

CLAUDE.md's "Scheduling & triggers" section, quoted for reference:

    "Routine sync job ... runs daily, but only within an active window
    around each month's close: from 15 days before month-end through 7
    days after month-end (rolling into the next month, to allow
    late-arriving revisions/corrections)."

    "Month-end FX revaluation runs as its own scheduled job at period
    close, distinct from the routine sync. It must always use that
    specific period's own end-of-month Kurs Pajak rate — even though the
    job may actually execute a few days later during the H+7 revision
    window, it revalues using the correct closed period's rate, never a
    different month's."

    "Monthly Drive folder provisioning: ahead of each new reporting cycle,
    the backend automatically creates next month's upload folders
    (including a new year folder when the year rolls over)."
"""
from __future__ import annotations

import calendar
import datetime as _dt

# H-15 / H+7, per CLAUDE.md's Scheduling & triggers section.
ROUTINE_SYNC_LOOKAHEAD_DAYS = 15
ROUTINE_SYNC_TAIL_DAYS = 7


def month_end(d: _dt.date) -> _dt.date:
    """The last calendar day of ``d``'s own month."""
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=last_day)


def previous_month_end(d: _dt.date) -> _dt.date:
    """The last calendar day of the month BEFORE ``d``'s own month.
    Correctly rolls the year backward for a January date (e.g.
    previous_month_end(2027-01-15) == 2026-12-31).
    """
    return d.replace(day=1) - _dt.timedelta(days=1)


def next_month_start(d: _dt.date) -> _dt.date:
    """The first calendar day of the month AFTER ``d``'s own month.
    Correctly rolls the year forward for a December date (e.g.
    next_month_start(2026-12-15) == 2027-01-01).
    """
    first = d.replace(day=1)
    if first.month == 12:
        return first.replace(year=first.year + 1, month=1)
    return first.replace(month=first.month + 1)


def is_pre_close_window_active(today: _dt.date) -> bool:
    """Condition (a) from CLAUDE.md: ``today`` is within 15 days before
    the end of the CURRENT calendar month. Inclusive of the day-15
    boundary itself ("within 15 days" reads as <= 15, not < 15).
    """
    return (month_end(today) - today).days <= ROUTINE_SYNC_LOOKAHEAD_DAYS


def is_post_close_tail_active(today: _dt.date) -> bool:
    """Condition (b) from CLAUDE.md: ``today`` is within 7 days after the
    end of the PREVIOUS calendar month. Inclusive of the day-7 boundary.
    """
    return (today - previous_month_end(today)).days <= ROUTINE_SYNC_TAIL_DAYS


def is_routine_sync_active(today: _dt.date) -> bool:
    """Whether the routine sync job should do any work at all today —
    condition (a) OR condition (b), per CLAUDE.md.
    """
    return is_pre_close_window_active(today) or is_post_close_tail_active(today)


def periods_to_sync(today: _dt.date) -> list[_dt.date]:
    """Which period_month(s) (first-of-month dates) the routine sync job
    should process for ``today``. Empty list when the job is outside its
    active window entirely — the caller should no-op cleanly in that case,
    never fabricate a period to sync anyway.

    When active at all, the CURRENT month is always included. The
    PREVIOUS month is additionally included specifically when in the H+7
    tail (condition (b)) — "rolling into the next month, to allow
    late-arriving revisions/corrections" per CLAUDE.md — so a correction
    uploaded in the first week of a new month still gets matched/posted
    against the period it actually belongs to, not just whatever period
    today's calendar date happens to fall in.
    """
    if not is_routine_sync_active(today):
        return []

    periods = [today.replace(day=1)]
    if is_post_close_tail_active(today):
        periods.append(previous_month_end(today).replace(day=1))

    # De-dupe while preserving order. Only matters if (a) and (b) both
    # apply to the exact same period on the exact same day, which can't
    # happen for any real Gregorian month length (the shortest possible
    # gap between the two conditions is Feb-in-a-non-leap-year, and even
    # then they land on different periods) — kept anyway so this is
    # correct by construction rather than "correct because no real
    # calendar month is short enough to break it".
    seen: set[_dt.date] = set()
    ordered: list[_dt.date] = []
    for p in periods:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def closed_period_for(today: _dt.date) -> _dt.date:
    """The calendar month that most recently closed relative to ``today``
    — i.e. the previous calendar month's first-of-month date. Used by the
    month-end FX revaluation job: even though that job may actually run a
    few days into the H+7 tail, it must always revalue the period that
    JUST closed, never "whatever month today happens to be in".
    """
    return previous_month_end(today).replace(day=1)
