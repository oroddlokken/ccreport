"""Where spend is heading, per account, against a ceiling if one is set.

Display only. Nothing here notifies, changes colour or holds a threshold state:
the number is the feature, and a number that also shouts is a number people
turn off.

Three horizons over the same record stream — the calendar month, the billing
month, and the rate-limit windows — because they answer different questions.
The calendar month is what a spreadsheet asks for, the billing month is the
period the money actually settles over, and the windows are what runs out this
afternoon.

No rich here: `ccu` reads this, and it renders its own ANSI.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MIN_ACTIVE_DAYS = 3
"""Below this, nothing is projected. A projection built on two data points is a
guess with a decimal point on it."""

HALF_LIFE_DAYS = 7.0
"""How fast an older day stops counting toward the daily rate.

Dividing the total by the days elapsed lets one heavy day early in the month
decide the whole projection, and a month that started with a migration is not a
month that continues with one."""


@dataclass(frozen=True)
class Forecast:
    """One horizon's projection, and the ceiling it is measured against."""

    spent: float
    """What has been spent inside the period so far."""
    projected: float
    """What the period ends at, at the recent daily rate."""
    elapsed: float
    """Days elapsed, fractional."""
    total: float
    """Days in the period."""
    ceiling: float | None = None
    """The account's limit, or None when it has none set."""

    @property
    def share(self) -> float | None:
        """The projection as a fraction of the ceiling, or None without one."""
        if not self.ceiling:
            return None
        return self.projected / self.ceiling

    @property
    def over(self) -> bool:
        share = self.share
        return share is not None and share > 1.0


def daily_costs(records) -> dict[str, dict[str, float]]:
    """Account -> local day -> USD, over an already-loaded record stream.

    Takes records rather than reading the cache, so the deduplication and the
    account attribution are the ones every report already applied. A forecast
    that counted a synced log twice, or billed the work account for a personal
    week, would be wrong in a way that is hard to see and easy to act on — and
    getting that right a second time here is how the two would drift.
    """
    by_account: dict[str, dict[str, float]] = {}
    for rec in records:
        days = by_account.setdefault(rec.account, {})
        day = rec.day_key()
        days[day] = days.get(day, 0.0) + rec.cost()
    return by_account


def daily_rate(costs: dict[str, float], today: date) -> float | None:
    """The recent daily spend, weighting recent days more heavily.

    None when fewer than MIN_ACTIVE_DAYS carried any spend.

    Weighted rather than averaged: the answer people want is what tomorrow
    costs, and a month that opened with one enormous day has already stopped
    being that month.
    """
    active = {day: cost for day, cost in costs.items() if cost > 0}
    if len(active) < MIN_ACTIVE_DAYS:
        return None
    weighted = total_weight = 0.0
    for day, cost in active.items():
        age = (today - date.fromisoformat(day)).days
        weight = math.exp(-max(age, 0) * math.log(2) / HALF_LIFE_DAYS)
        weighted += weight * cost
        total_weight += weight
    return weighted / total_weight if total_weight else None


def _period_forecast(
    costs: dict[str, float], start: date, end: date, now: datetime,
    ceiling: float | None,
) -> Forecast | None:
    """Spend inside [start, end) projected to its end, or None to say nothing."""
    inside = {
        day: cost for day, cost in costs.items()
        if start <= date.fromisoformat(day) < end
    }
    rate = daily_rate(inside, now.date())
    if rate is None:
        return None
    spent = sum(inside.values())
    elapsed = (now - datetime.combine(start, datetime.min.time(), now.tzinfo)).days + 1
    total = (end - start).days
    remaining = max(total - elapsed, 0)
    return Forecast(
        spent=spent,
        projected=spent + rate * remaining,
        elapsed=float(elapsed),
        total=float(total),
        ceiling=ceiling,
    )


def month_forecast(
    costs: dict[str, float], now: datetime, ceiling: float | None = None,
) -> Forecast | None:
    """The calendar month, month-to-date extrapolated to month end."""
    start = now.date().replace(day=1)
    days = calendar.monthrange(now.year, now.month)[1]
    return _period_forecast(costs, start, start + timedelta(days=days), now, ceiling)


def billing_start(now: date, renewal_day: int) -> date:
    """The start of the billing period *now* falls in.

    A renewal day past the end of a short month lands on its last day: a
    subscription renewing on the 31st renews on the 28th in February, and there
    is no 31st to wait for.
    """
    def clamp(year: int, month: int) -> date:
        return date(year, month, min(renewal_day, calendar.monthrange(year, month)[1]))

    this_month = clamp(now.year, now.month)
    if now >= this_month:
        return this_month
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return clamp(year, month)


def billing_forecast(
    costs: dict[str, float], now: datetime, renewal_day: int,
    ceiling: float | None = None,
) -> Forecast | None:
    """The subscription period, anchored to the renewal day rather than the 1st.

    The usage API's response carries no renewal date — nothing in it names one —
    so the day is configured per account with `ccreport budget set`.
    """
    start = billing_start(now.date(), renewal_day)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = date(year, month, min(renewal_day, calendar.monthrange(year, month)[1]))
    return _period_forecast(costs, start, end, now, ceiling)


@dataclass(frozen=True)
class WindowForecast:
    """What a rate-limit window ends up costing at the rate it is spending."""

    name: str
    spent: float
    projected: float
    resets_at: float


def window_forecast(
    name: str, spent: float, window_start: float, window_span: float, now: float,
) -> WindowForecast | None:
    """Project one rolling window's cost to its reset.

    The money counterpart of burn.py, which projects the quota percentage. The
    two answer different questions and neither replaces the other: a window can
    fill on cheap calls or on expensive ones.

    None until the window has run long enough to have a rate — a window two
    minutes old projects its first call across five hours.
    """
    elapsed = now - window_start
    if elapsed <= 0 or elapsed >= window_span or spent <= 0:
        return None
    if elapsed < window_span * 0.05:
        return None
    return WindowForecast(
        name=name,
        spent=spent,
        projected=spent / elapsed * window_span,
        resets_at=window_start + window_span,
    )
