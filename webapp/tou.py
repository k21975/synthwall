"""tou.py — time-of-use peak/off-peak classification for charging charts.

The defaults match a common time-of-use tariff: peak is **1PM-9PM every
day**, off-peak the rest of the clock. Weekends and holidays are peak like any
other day, but both can be carved out from the settings page for tariffs that
exempt them.

Everything is configurable (see settings.py "tou") so a tariff change — or a
different utility entirely — needs no code change.

Hours are wall-clock hours in the house timezone; the window is [start, end),
so 13-21 means "peak from 13:00:00 up to 20:59:59".
"""

from datetime import date, datetime, timedelta

# Holidays a tariff would name as off-peak, if holiday_offpeak is turned on.
_FIXED = ((1, 1), (7, 4), (12, 25))          # New Year's, Independence, Christmas
MON, THU = 0, 3

DEFAULTS = {
    "peak": [13, 21],          # local hours [start, end)
    "weekend_offpeak": False,  # tariff has no weekend carve-out
    "holiday_offpeak": False,  # ...nor a holiday one
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Date of the nth (1-based) `weekday` in a month, e.g. 1st Monday of Sep."""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Date of the last `weekday` in a month, e.g. last Monday of May."""
    d = date(year, month, 1) + timedelta(days=31)
    d = d.replace(day=1) - timedelta(days=1)          # last day of the month
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def holidays(year: int) -> set:
    """The observed holidays for a calendar year (used only when opted in).

    A holiday landing on Sunday is observed the following Monday, the usual
    tariff language.
    """
    days = {date(year, m, d) for m, d in _FIXED}
    days.add(_last_weekday(year, 5, MON))             # Memorial Day
    days.add(_nth_weekday(year, 9, MON, 1))           # Labor Day
    days.add(_nth_weekday(year, 11, THU, 4))          # Thanksgiving
    return days | {d + timedelta(days=1) for d in days if d.weekday() == 6}


def is_peak(dt: datetime, cfg: dict) -> bool:
    """Is this local wall-clock moment inside the peak window?"""
    if cfg.get("weekend_offpeak") and dt.weekday() >= 5:
        return False
    if cfg.get("holiday_offpeak") and dt.date() in holidays(dt.year):
        return False
    start, end = cfg.get("peak", DEFAULTS["peak"])
    return start <= dt.hour < end
