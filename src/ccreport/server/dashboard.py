"""What the merged spend page shows: a headline, five tiles, a chart, a table.

Everything here folds the same records `ccreport --server` renders, through the
same `aggregate.py`. What the page adds is the five stat tiles, which no CLI
table carries, and the shapes uPlot wants.

A project a restricted machine did not opt in to appears under its pseudonym
with its real cost and token counts. That is the point of the redaction design,
and the page does not special-case it away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ccreport import aggregate, pricing
from ccreport.server import reports

RANGES = (7, 30, 90)
"""The day counts the header toggles between."""

DEFAULT_RANGE = 30

DIMENSIONS = ("model", "day", "project", "machine")
"""What the breakdown table switches between. Same columns throughout."""


def range_bounds(days: int, now: datetime) -> tuple[datetime, datetime]:
    """The half-open span a range toggle selects, ending at the next midnight.

    Whole local days: a "7 days" that ended at the current minute would move
    every row a little on every refresh, and the chart's last column would be a
    part-day the eye reads as a collapse in usage.
    """
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return end - timedelta(days=days), end


@dataclass
class Tile:
    """One stat tile: a number, and the derived line that makes it worth reading."""

    label: str
    value: str
    subline: str


@dataclass
class AccountRow:
    """The left column: one row per account, with its share of the total."""

    account: str
    cost: float
    tokens: int
    share: float
    """Fraction of the total cost, for the bar and the percentage beside it."""


@dataclass
class Series:
    """One account's daily values, aligned to the chart's day axis."""

    account: str
    cost: list[float] = field(default_factory=list)
    tokens: list[float] = field(default_factory=list)


@dataclass
class Dashboard:
    """Everything one render of the page needs."""

    days: int
    start: str
    end: str
    total_cost: float
    total_cost_nok: float
    nok_enabled: bool
    accounts: list[AccountRow]
    tiles: list[Tile]
    chart_days: list[str]
    series: list[Series]
    breakdowns: dict[str, list[dict]]
    machines: list[str]


def _fmt_tokens(n: float) -> str:
    """A token count at the scale a person reads it: 1.2M, 940K, 812."""
    for suffix, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return f"{n:.0f}"


def _fmt_usd(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 10:
        return f"${value:.0f}"
    return f"${value:.2f}"


def _uncached_input_rate(model: str, when: datetime) -> float:
    """What one input token costs uncached, for the savings tile.

    None of the pricing tables is consulted twice: this is the same
    find_pricing every cost on the page went through.
    """
    prices = pricing.find_pricing(model, when)
    return prices.get("input", 0.0) if prices else 0.0


def _tiles(merged: list[reports.MergedRecord], total_cost: float) -> list[Tile]:
    """The five tiles, each with the subline that makes the number mean something.

    Cache savings is the one worth explaining: it prices every cache read at
    what that token would have cost as fresh input, which is what the read
    replaced. Expressed as a multiple of what was actually paid, because "12x"
    answers "is the caching working" and a dollar figure does not.
    """
    tokens = aggregate.TokenCounts()
    read_at_input_price = 0.0
    days: set[str] = set()
    for item in merged:
        rec = item.record
        tokens += rec.tokens
        days.add(rec.day_key())
        rate = _uncached_input_rate(rec.model, rec.timestamp)
        read_at_input_price += rec.tokens.cache_read * rate

    active = max(len(days), 1)
    observed_input = tokens.input + tokens.cache_create + tokens.cache_read
    cached_share = (tokens.cache_read / observed_input * 100) if observed_input else 0.0
    savings_multiple = (read_at_input_price / total_cost) if total_cost > 0 else 0.0

    return [
        Tile("Processed tokens", _fmt_tokens(tokens.total),
             f"{_fmt_tokens(tokens.total / active)} per active day, {active} days"),
        Tile("Cached input", _fmt_tokens(tokens.cache_read),
             f"{cached_share:.0f}% of observed input"),
        Tile("Uncached input", _fmt_tokens(tokens.input + tokens.cache_create),
             f"{_fmt_tokens(tokens.cache_create)} written to cache"),
        Tile("Output", _fmt_tokens(tokens.output),
             f"{_fmt_tokens(tokens.output / active)} per active day"),
        Tile("Cache savings", f"{savings_multiple:.1f}x",
             f"{_fmt_usd(read_at_input_price)} if those reads had been fresh input"),
    ]


def _account_rows(report, total_cost: float) -> list[AccountRow]:
    return [
        AccountRow(
            account=row.key,
            cost=row.agg.cost,
            tokens=row.agg.tokens.total,
            share=(row.agg.cost / total_cost) if total_cost > 0 else 0.0,
        )
        for row in report.rows
    ]


def _chart(merged: list[reports.MergedRecord], start: datetime, days: int,
           accounts: list[str]) -> tuple[list[str], list[Series]]:
    """One series per account over a dense day axis.

    Dense, so a day nobody worked is a zero rather than a gap the line jumps
    across — which would read as a straight climb through a quiet weekend.
    """
    axis = [
        (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days)
    ]
    index = {day: position for position, day in enumerate(axis)}
    series = {account: Series(account=account, cost=[0.0] * days, tokens=[0.0] * days)
              for account in accounts}
    for item in merged:
        position = index.get(item.record.day_key())
        if position is None or item.account not in series:
            continue
        series[item.account].cost[position] += item.record.cost()
        series[item.account].tokens[position] += item.record.tokens.total
    return axis, [series[account] for account in accounts]


_DIMENSION_KEYS = {
    "model": lambda item: item.record.model,
    "day": lambda item: item.record.day_key(),
    "project": lambda item: item.record.project,
    "machine": lambda item: item.machine,
}
"""What each breakdown groups on. machine is the one no local report has, and
the reason the merge exists at all."""


def _breakdown(merged: list[reports.MergedRecord], dimension: str,
               total_cost: float) -> list[dict]:
    """One dimension's rows: cost, share and tokens, priciest first.

    Every dimension folds the same records, so all four total to the same
    overall cost — which is what makes the toggle a change of view rather than
    a change of subject.
    """
    key_fn = _DIMENSION_KEYS[dimension]
    folded: dict[str, aggregate.AggBucket] = {}
    for item in merged:
        bucket = folded.setdefault(str(key_fn(item)), aggregate.AggBucket())
        bucket.tokens += item.record.tokens
        bucket.cost += item.record.cost()
        bucket.count += item.record.count
    rows = sorted(folded.items(), key=lambda pair: -pair[1].cost)
    return [
        {
            "key": key,
            "cost": bucket.cost,
            "tokens": bucket.tokens.total,
            "share": (bucket.cost / total_cost) if total_cost > 0 else 0.0,
            "calls": bucket.count,
        }
        for key, bucket in rows
    ]


def build(conn, days: int, now: datetime | None = None) -> Dashboard:
    """Everything the page shows, for one range toggle."""
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in RANGES else DEFAULT_RANGE
    start, end = range_bounds(days, now)
    merged = reports.load(conn, reports.Filters(since=start, until=end))
    nok = reports.nok_context(merged)

    account_report = reports.build(merged, "account", nok)
    total_cost = account_report.total.cost
    accounts = [row.key for row in account_report.rows]
    axis, series = _chart(merged, start, days, accounts)

    return Dashboard(
        days=days,
        start=start.strftime("%Y-%m-%d"),
        end=(end - timedelta(days=1)).strftime("%Y-%m-%d"),
        total_cost=total_cost,
        total_cost_nok=account_report.total.cost_nok,
        nok_enabled=nok.enabled,
        accounts=_account_rows(account_report, total_cost),
        tiles=_tiles(merged, total_cost),
        chart_days=axis,
        series=series,
        breakdowns={
            dimension: _breakdown(merged, dimension, total_cost)
            for dimension in DIMENSIONS
        },
        machines=sorted({item.machine for item in merged}),
    )
