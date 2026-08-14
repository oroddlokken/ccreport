"""What the merged spend page shows: a headline, five tiles, a chart, a table.

Folds the same records `ccreport --server` renders, through the same
`aggregate.py`. What the page adds is the five stat tiles and the shapes uPlot
wants.

Everything a restricted machine did not opt in to appears as one row per
account, with its real cost and token counts and no name of its own.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ccreport import aggregate, pricing
from ccreport.server import db, reports

ALL_TIME = 0
"""The toggle that starts at the oldest stored record instead of a day count."""

RANGES = (7, 30, 90, ALL_TIME)
"""What the header toggles between, in the order it draws them."""

RANGE_LABELS = {7: "7d", 30: "30d", 90: "90d", ALL_TIME: "All"}

DEFAULT_RANGE = 30

DIMENSIONS = ("model", "day", "project", "machine")
"""What the breakdown table switches between. Same columns throughout."""

METRICS = ("cost", "tokens")
"""Which series the chart draws. Both are in the payload either way."""

SCOPES = ("account", *DIMENSIONS)
"""What a detail page can be about. One more than the dashboard's tabs: the
accounts have a column of their own there rather than a table."""

TRACE_LIMIT = 6
"""Series per chart before the rest fold into one. Past this the eye is reading
a legend, not a chart, and the palette has run out of hues that stay apart."""


def range_bounds(days: int, now: datetime) -> tuple[datetime, datetime]:
    """The half-open span a range toggle selects, ending at the next midnight.

    Whole local days: a span ending at the current minute makes the chart's
    last column a part-day that reads as a collapse in usage.
    """
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return end - timedelta(days=days), end


def all_time_bounds(oldest: float | None, now: datetime) -> tuple[datetime, datetime]:
    """The span from the oldest record's local day to the same next midnight.

    A database with nothing in it falls back to the default range: a zero-day
    axis has no columns to draw and reads as a broken chart rather than an
    empty one.
    """
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if oldest is None:
        return range_bounds(DEFAULT_RANGE, now)
    start = datetime.fromtimestamp(oldest, tz=now.tzinfo or UTC).astimezone(now.tzinfo)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(start, end - timedelta(days=1)), end


@dataclass
class Tile:
    """One stat tile: a number and the line derived under it."""

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


@dataclass(frozen=True)
class Scope:
    """What one detail page is about, as the breakdown row spells it.

    The key is matched against the string the table shows rather than against a
    stored column, so an account alias, a machine label and a redacted project
    bucket all lead to the rows they stand for.
    """

    dimension: str
    key: str


@dataclass
class Trace:
    """One line on a chart: a label and a value per bucket on the axis."""

    label: str
    values: list[float]


@dataclass
class Chart:
    """One plot: its axis, its lines, and what the numbers on them are."""

    key: str
    title: str
    unit: str
    """usd, tokens or calls. Picks the tick format, and says why two charts
    that look alike are not one chart with two scales."""
    axis: list[str]
    traces: list[Trace]


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
    scope: Scope | None = None
    charts: list[Chart] = field(default_factory=list)
    """Empty on the whole-server page, which has one chart and a toggle."""


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
    """What one input token costs uncached, for the cache-reads tile."""
    prices = pricing.find_pricing(model, when)
    return prices.get("input", 0.0) if prices else 0.0


def _tiles(merged: list[reports.MergedRecord], total_cost: float) -> list[Tile]:
    """The five tiles, each with the subline its number is read against.

    Cache reads vs spend prices every cache read as fresh input, then divides by
    the whole bill. The multiple answers "is the caching working"; a dollar
    figure does not. It is not what caching saved: the denominator covers output
    and uncached input too, and the reads themselves were not free.
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
    read_multiple = (read_at_input_price / total_cost) if total_cost > 0 else 0.0

    return [
        Tile("Processed tokens", _fmt_tokens(tokens.total),
             f"{_fmt_tokens(tokens.total / active)} per active day, {active} days"),
        Tile("Cached input", _fmt_tokens(tokens.cache_read),
             f"{cached_share:.0f}% of observed input"),
        Tile("Uncached input", _fmt_tokens(tokens.input + tokens.cache_create),
             f"{_fmt_tokens(tokens.cache_create)} written to cache"),
        Tile("Output", _fmt_tokens(tokens.output),
             f"{_fmt_tokens(tokens.output / active)} per active day"),
        Tile("Cache reads vs spend", f"{read_multiple:.1f}x",
             f"{_fmt_usd(read_at_input_price)} if those reads had been fresh input, "
             f"against {_fmt_usd(total_cost)} paid"),
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


def _day_axis(start: datetime, days: int) -> list[str]:
    """The calendar days a range covers, in order."""
    return [(start + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days)]


def _day_position(axis: list[str]):
    """Where a record lands on a day axis, or None if it falls outside it."""
    index = {day: position for position, day in enumerate(axis)}
    return lambda item: index.get(item.record.day_key())


def _hour_position(midnight: datetime):
    """Where a record lands on one day's 24 hours, in this server's clock."""
    def position(item: reports.MergedRecord) -> int | None:
        elapsed = item.record.timestamp.astimezone() - midnight
        hour = int(elapsed.total_seconds() // 3600)
        return hour if 0 <= hour < 24 else None
    return position


def _chart(merged: list[reports.MergedRecord], axis: list[str],
           accounts: list[str]) -> list[Series]:
    """One series per account over a dense day axis.

    Dense, so a day nobody worked is a zero rather than a gap the line jumps
    across.
    """
    position_of = _day_position(axis)
    series = {account: Series(account=account, cost=[0.0] * len(axis), tokens=[0.0] * len(axis))
              for account in accounts}
    for item in merged:
        position = position_of(item)
        if position is None or item.account not in series:
            continue
        series[item.account].cost[position] += item.record.cost()
        series[item.account].tokens[position] += item.record.tokens.total
    return [series[account] for account in accounts]


_DIMENSION_KEYS = {
    "account": lambda item: item.account,
    "model": lambda item: item.record.model,
    "day": lambda item: item.record.day_key(),
    "project": lambda item: item.record.project,
    "machine": lambda item: item.machine,
}
"""What each breakdown groups on. machine is the one no local report has."""


def _hour_axis(day: str) -> list[str]:
    """The 24 local hours of one day, as the stamps the chart plots on."""
    return [f"{day}T{hour:02d}:00" for hour in range(24)]


def _traced(merged: list[reports.MergedRecord], axis: list[str],
            position_of, pairs_of) -> list[Trace]:
    """One trace per key, dense over *axis*, fattest first.

    *pairs_of* turns one record into the (label, value) pairs it contributes:
    one for a cost split by account, four for a split by token kind. Dense
    again, so an idle hour is a zero and not a gap.
    """
    traces: dict[str, list[float]] = {}
    for item in merged:
        position = position_of(item)
        if position is None:
            continue
        for label, value in pairs_of(item):
            traces.setdefault(label, [0.0] * len(axis))[position] += value
    ordered = sorted(traces.items(), key=lambda pair: -sum(pair[1]))
    if len(ordered) > TRACE_LIMIT:
        rest = [sum(values) for values in zip(*(v for _, v in ordered[TRACE_LIMIT:]), strict=True)]
        ordered = [*ordered[:TRACE_LIMIT], ("Other", rest)]
    return [Trace(label=label, values=values) for label, values in ordered]


def _token_pairs(item: reports.MergedRecord) -> list[tuple[str, float]]:
    """One record's tokens as the four kinds they were billed as."""
    tokens = item.record.tokens
    return [
        ("Input", float(tokens.input)),
        ("Output", float(tokens.output)),
        ("Cache write", float(tokens.cache_create)),
        ("Cache read", float(tokens.cache_read)),
    ]


def _charts(merged: list[reports.MergedRecord], axis: list[str], position_of) -> list[Chart]:
    """The four plots a detail page draws over one axis.

    Four rather than one with a toggle: cost, what it went on, what shape the
    tokens were and how many calls carried them answer different questions, and
    a reader comparing them wants them on screen together. One scale each — a
    dollar axis and a token axis on one plot would be two charts drawn over
    each other.
    """
    return [
        Chart(key="cost-account", title="Cost by account", unit="usd", axis=axis,
              traces=_traced(merged, axis, position_of,
                             lambda item: [(item.account, item.record.cost())])),
        Chart(key="cost-model", title="Cost by model", unit="usd", axis=axis,
              traces=_traced(merged, axis, position_of,
                             lambda item: [(item.record.model, item.record.cost())])),
        Chart(key="tokens-kind", title="Tokens by kind", unit="tokens", axis=axis,
              traces=_traced(merged, axis, position_of, _token_pairs)),
        Chart(key="calls", title="Calls", unit="calls", axis=axis,
              traces=_traced(merged, axis, position_of,
                             lambda item: [("Calls", float(item.record.count))])),
    ]


def _breakdown(merged: list[reports.MergedRecord], dimension: str,
               total_cost: float) -> list[dict]:
    """One dimension's rows: cost, share and tokens, priciest first.

    Every dimension folds the same records, so all four total the same cost.
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


_CACHE: dict[tuple, tuple[tuple, Dashboard]] = {}
_CACHE_LOCK = threading.Lock()


def cached_build(database: db.Database, days: int, now: datetime | None = None) -> Dashboard:
    """build(), reusing the last one until a push or a new day invalidates it.

    One entry per (database, range toggle), so the whole cache is as many
    entries as RANGES has per server. Each is held against `db.content_stamp`
    and the local date the render fell on: the stamp covers what was pushed,
    the date covers the ranges that end at the next midnight and would
    otherwise keep yesterday's axis.

    Keyed on the database path and not on the range alone, because a process
    can hold more than one — every server test does — and two empty databases
    have the same stamp.

    Two threads can miss on the same range at once and both build. That costs a
    duplicate query and nothing else, which is cheaper than holding the lock
    across a build and serializing every other range behind it.
    """
    conn = database.connect()
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in RANGES else DEFAULT_RANGE
    key = (str(database.path), days)
    stamp = (db.content_stamp(conn), now.strftime("%Y-%m-%d"))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    view = build(conn, days, now)
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, view)
    return view


def _day_records(conn, key: str) -> tuple[list[reports.MergedRecord], datetime]:
    """One calendar day's records, ungrouped, and the local midnight they open.

    Ungrouped because the hour is what this page plots and `load_grouped` folds
    it away. The ts window is a day wider at each end and the day itself is
    matched afterwards: `day` is the machine's own calendar day, and a machine
    an hour off this server's clock keeps records whose instant falls outside
    the local day they belong to.
    """
    midnight = datetime.strptime(key, "%Y-%m-%d").astimezone()
    merged = reports.load(conn, reports.Filters(
        since=midnight - timedelta(days=1), until=midnight + timedelta(days=2),
    ))
    return [item for item in merged if item.record.day_key() == key], midnight


def build(conn, days: int, now: datetime | None = None,
          scope: Scope | None = None) -> Dashboard:
    """Everything the page shows, for one range toggle and one scope.

    With a *scope* the same fold runs over the records that match it alone, and
    the page gains the four charts. A day scope is its own range: the toggle
    cannot widen a page that is about one day.
    """
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in RANGES else DEFAULT_RANGE
    hourly = scope is not None and scope.dimension == "day"
    if hourly:
        merged, start = _day_records(conn, scope.key)  # type: ignore[union-attr]
        end = start + timedelta(days=1)
        axis = _hour_axis(scope.key)  # type: ignore[union-attr]
        position_of = _hour_position(start)
    else:
        if days == ALL_TIME:
            start, end = all_time_bounds(db.oldest_record_ts(conn), now)
        else:
            start, end = range_bounds(days, now)
        merged = reports.load_grouped(conn, reports.Filters(since=start, until=end))
        if scope is not None:
            key_of = _DIMENSION_KEYS[scope.dimension]
            merged = [item for item in merged if str(key_of(item)) == scope.key]
        axis = _day_axis(start, (end - start).days)
        position_of = _day_position(axis)

    nok = reports.nok_context(merged)
    account_report = reports.build(merged, "account", nok)
    total_cost = account_report.total.cost
    accounts = [row.key for row in account_report.rows]

    return Dashboard(
        days=days,
        start=start.strftime("%Y-%m-%d"),
        end=(end - timedelta(days=1)).strftime("%Y-%m-%d"),
        total_cost=total_cost,
        total_cost_nok=account_report.total.cost_nok,
        nok_enabled=nok.enabled,
        accounts=_account_rows(account_report, total_cost),
        tiles=_tiles(merged, total_cost),
        chart_days=[] if scope is not None else axis,
        series=[] if scope is not None else _chart(merged, axis, accounts),
        breakdowns={
            dimension: _breakdown(merged, dimension, total_cost)
            for dimension in (SCOPES if scope is not None else DIMENSIONS)
        },
        machines=sorted({item.machine for item in merged}),
        scope=scope,
        charts=_charts(merged, axis, position_of) if scope is not None else [],
    )
