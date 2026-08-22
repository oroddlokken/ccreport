"""What the merged spend page shows: a headline, the tiles, a chart, a table.

Folds the same records `ccreport --server` renders, through the same
`aggregate.py`. What the page adds is the stat tiles and the shapes uPlot
wants. Five of those tiles are about the records themselves; the sixth sets
what they were worth against what the subscription behind them cost, and shows
only where a plan is declared and the page is about the whole of what it
bought.

Everything a restricted machine did not opt in to appears as one row per
account, with its real cost and token counts and no name of its own.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from ccreport import aggregate, pricing, tier_timeline
from ccreport.server import db, reports

ALL_TIME = 0
"""The toggle that starts at the oldest stored record instead of a day count."""

RANGES = (7, 30, 90, ALL_TIME)
"""What the header toggles between, in the order it draws them."""

RANGE_LABELS = {7: "7d", 30: "30d", 90: "90d", ALL_TIME: "All"}

DEFAULT_RANGE = ALL_TIME
"""What a page with no `days` opens on, and what an unknown one falls back to."""

EMPTY_SPAN_DAYS = 30
"""How wide all-time is when there is nothing stored to measure it from."""

DIMENSIONS = ("model", "day", "week", "month", "project", "machine")
"""What the breakdown table switches between. Same columns throughout.

The three periods run day, week, month so a row leads to the page one step
wider than the row above it."""

METRICS = ("cost", "tokens")
"""Which series the chart draws. Both are in the payload either way."""

SCOPES = ("model", "account", *(name for name in DIMENSIONS if name != "model"))
"""What a detail page can be about, in the order that page stacks its tables.
One more than the dashboard's tabs: the accounts have a column of their own
there rather than a table. Model leads, because which model was billed is the
first question a page about one project or one day is opened with."""

PERIODS = ("day", "week", "month")
"""The scopes that are a span of time rather than something spending over one.
Each is its own range: the toggle cannot widen a page that is about one week."""

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

    A database with nothing in it falls back to `EMPTY_SPAN_DAYS`: a zero-day
    axis has no columns to draw and reads as a broken chart rather than an
    empty one.
    """
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if oldest is None:
        return range_bounds(EMPTY_SPAN_DAYS, now)
    start = datetime.fromtimestamp(oldest, tz=now.tzinfo or UTC).astimezone(now.tzinfo)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(start, end - timedelta(days=1)), end


def week_key(day: str) -> str:
    """The Monday that opens the week a calendar day falls in."""
    parsed = date.fromisoformat(day)
    return (parsed - timedelta(days=parsed.weekday())).isoformat()


def _period_dates(dimension: str, key: str) -> tuple[date, date]:
    """The first calendar day one period holds and the first day after it.

    Raises ValueError on a key the period cannot parse. A week is keyed on any
    date it contains and opens on that date's Monday, so the seven URLs of one
    week all draw it.
    """
    if dimension == "month":
        first = date.fromisoformat(f"{key}-01")
        return first, (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    day = date.fromisoformat(key)
    if dimension == "week":
        first = day - timedelta(days=day.weekday())
        return first, first + timedelta(days=7)
    return day, day + timedelta(days=1)


def period_span(dimension: str, key: str) -> tuple[datetime, datetime, list[str]]:
    """One period page's bounds in this server's clock, and the days it covers.

    The days are counted over dates rather than derived from the two bounds: an
    hour of daylight saving inside the period leaves (end - start).days one
    short of the calendar days the period holds, which drops a column and a
    day of spend off the end of the axis.
    """
    first, stop = _period_dates(dimension, key)
    axis = [(first + timedelta(days=offset)).isoformat() for offset in range((stop - first).days)]
    return (datetime.combine(first, time.min).astimezone(),
            datetime.combine(stop, time.min).astimezone(), axis)


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

    @property
    def is_period(self) -> bool:
        """Whether this page is a span of time, which has no range toggle."""
        return self.dimension in PERIODS


@dataclass
class Trace:
    """One line on a chart: a label and a value per bucket on the axis."""

    label: str
    values: Sequence[float | None]
    """None is a bucket nothing was measured in, which uPlot draws as a gap. The
    record folds never produce one — a dense axis makes an idle hour a zero —
    but a fill curve does: a machine that took no reading in a bucket is not a
    machine whose quota emptied."""


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


SLICE_SCOPES = ("model", "project", "machine")
"""Pages whose spend is one part of what the subscription bought.

The multiple is worth showing there — one project outrunning the whole plan by
itself is the interesting case — but the denominator is still the entire
subscription, so the subline has to say the numerator is a slice. Everything
else (the server-wide page, one account, one span of time) compares two things
that cover the same ground and needs no such qualifier.
"""


def _in_nok(usd: float, nok: aggregate.NokCtx, on: date) -> float | None:
    """*usd* in kroner at *on*'s rate, or None where this server has none.

    Under the page's own MVA setting, so a figure derived here agrees with
    every converted figure drawn beside it.
    """
    rate, _ = nok.rate_for(on)
    if rate is None:
        return None
    return usd * rate * (1.25 if nok.mva else 1.0)


def _active_span(merged: list[reports.MergedRecord], start: datetime,
                 end: datetime) -> tuple[datetime, datetime]:
    """The part of *start*..*end* the page's own records actually cover.

    A page about one project is set against the subscription that bought it,
    and the denominator has to cover the same period as the numerator. The
    range bounds do not: on the all-time toggle they run from the server's
    oldest record, so a project first seen in August was priced against every
    month since February and read as barely having paid for itself.

    Whole local days, so a project seen once still spans a day rather than an
    instant, and clipped to the range so a toggle still narrows the page.
    """
    if not merged:
        return start, end
    stamps = [item.record.timestamp.astimezone(start.tzinfo) for item in merged]
    first = min(stamps).replace(hour=0, minute=0, second=0, microsecond=0)
    last = max(stamps).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(first, start), min(last, end)


def _fmt_span(months: float, days: int) -> str:
    """How long a span is, in whichever unit reads as a quantity at that scale.

    "0.3 months" is a fraction a reader has to convert; "8 days" is the same
    span already converted. The cut is just above one month, so a span of
    roughly a month is called one rather than 31 days.
    """
    if months >= 1.05:
        return f"{months:.1f} months"
    return "1 day" if days == 1 else f"{days} days"


def _plan_tile(total_cost: float, plan_usd: float, nok: aggregate.NokCtx,
               on: date, slice_of: str | None = None, span: str = "") -> Tile:
    """The valuation set against what the subscription cost over the same span.

    A multiple as the number, because "44x" is the answer and a dollar figure
    is the evidence. The subline says what is being compared: the valuation
    prices every call at API list rates, so it is what this work would have
    cost through the API and not money anyone was charged.

    *span* is how long the page covers, so a figure that sums several months of
    subscription cannot be read as one month's price — which is what "$934
    plan" was doing. *slice_of* names the dimension when the page is about one
    part of that spend, and the subline then says so. Without it the same sentence would
    read identically on a page about one model and a page about everything,
    which is the reading that turns a true number into a false claim.

    NOK converts the saved amount alone, at the span's own Oslo date and under
    the page's own MVA setting, so it agrees with every other NOK figure drawn
    beside it.
    """
    multiple = (total_cost / plan_usd) if plan_usd > 0 else 0.0
    saved = total_cost - plan_usd
    # A quiet span is worth less than the plan it ran on, and the sentence has
    # to say so: "-$99.93 more than" is a sign printed where a word belongs.
    direction = "more" if saved >= 0 else "less"
    amount = _in_nok(abs(saved), nok, on)
    in_nok = "" if amount is None else f" (kr {amount:,.0f})"
    # "the $140 plan" reads as a plan that costs $140. It is the share of each
    # month's subscription this span covers, prorated by days, so the sentence
    # has to name it as a quantity of subscription rather than as a price.
    alone = f" — this {slice_of} alone" if slice_of else ""
    over = f" across {span}" if span else ""
    return Tile(
        "Value vs plan", f"{multiple:.1f}x",
        f"{_fmt_usd(abs(saved))}{in_nok} {direction} than the {_fmt_usd(plan_usd)} "
        f"of subscription{over}{alone}, valued at API rates",
    )


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


def _covered_axis(axis: list[str], merged: list[reports.MergedRecord]) -> list[str]:
    """*axis* cut back to the first and last day *merged* has a record on.

    A page about one model is charted over the range toggle, which on all-time
    opens at the server's oldest record. A model first seen in July then drew
    four fifths of every plot as a flat zero and squeezed its own shape into
    the right edge.

    Cut on `day_key` rather than on the timestamp `_active_span` reads: that is
    the pushing machine's calendar day, and a machine an hour off this server's
    clock would otherwise keep a record whose instant falls outside the day it
    belongs to, which `_day_position` drops. Records with no day on the axis
    leave it whole -- there is nothing to centre on.
    """
    days = {item.record.day_key() for item in merged}
    covered = [position for position, day in enumerate(axis) if day in days]
    if not covered:
        return axis
    return axis[covered[0]:covered[-1] + 1]


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
    "week": lambda item: week_key(item.record.day_key()),
    "month": lambda item: item.record.day_key()[:7],
    "project": lambda item: item.record.project,
    "machine": lambda item: item.machine,
}
"""What each breakdown groups on. machine is the one no local report has."""


def _hour_axis(day: str) -> list[str]:
    """The 24 local hours of one day, as the stamps the chart plots on."""
    return [f"{day}T{hour:02d}:00" for hour in range(24)]


def traced(merged: list[reports.MergedRecord], axis: list[str],
           position_of, pairs_of) -> list[Trace]:
    """One trace per key, dense over *axis*, fattest first.

    Public because the window pages fold the same records over an axis of their
    own; the three helpers below it are shared for the same reason.

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


def token_pairs(item: reports.MergedRecord) -> list[tuple[str, float]]:
    """One record's tokens as the four kinds they were billed as."""
    tokens = item.record.tokens
    return [
        ("Input", float(tokens.input)),
        ("Output", float(tokens.output)),
        ("Cache write", float(tokens.cache_create)),
        ("Cache read", float(tokens.cache_read)),
    ]


def _charts(merged: list[reports.MergedRecord], axis: list[str], position_of) -> list[Chart]:
    """The plots a detail page draws over one axis, at most four.

    Four rather than one with a toggle: cost, what it went on, what shape the
    tokens were and how many calls carried them answer different questions, and
    a reader comparing them wants them on screen together. One scale each — a
    dollar axis and a token axis on one plot would be two charts drawn over
    each other.

    A cost chart with one trace is dropped: it redraws the headline figure
    under a second title. That covers the page split by what it is already
    about — an account page's cost by account — and the project worked on by
    one account, which is the same chart from the other side. Calls is one
    trace by design and is not a cost split.
    """
    charts = [
        Chart(key="cost-account", title="Cost by account", unit="usd", axis=axis,
              traces=traced(merged, axis, position_of,
                             lambda item: [(item.account, item.record.cost())])),
        Chart(key="cost-model", title="Cost by model", unit="usd", axis=axis,
              traces=traced(merged, axis, position_of,
                             lambda item: [(item.record.model, item.record.cost())])),
        Chart(key="tokens-kind", title="Tokens by kind", unit="tokens", axis=axis,
              traces=traced(merged, axis, position_of, token_pairs)),
        Chart(key="calls", title="Calls", unit="calls", axis=axis,
              traces=traced(merged, axis, position_of,
                             lambda item: [("Calls", float(item.record.count))])),
    ]
    return [chart for chart in charts
            if not (chart.key.startswith("cost-") and len(chart.traces) == 1)]


def breakdown(merged: list[reports.MergedRecord], dimension: str,
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


def _month_bounds(key: str) -> tuple[datetime, datetime]:
    """A YYYY-MM key as the UTC instants its month opens and closes on.

    UTC rather than the server's zone, because the timeline's own dates are
    read as UTC. Both ends move together, so a month is a whole month wherever
    the server sits — what it must not be is 31 days on one clock and 31 days
    minus an offset on the other, which is what mixing the two would give.
    """
    year, month = (int(part) for part in key.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=UTC)
    return start, end


class _Plans:
    """One page's declared timelines, ready to price any span the page shows.

    Built once per render: the timeline is one query however many spans are
    priced against it, and a page prices one per month plus one for its own
    range.
    """

    def __init__(self, conn, accounts: set[str]) -> None:
        entries = db.account_tiers(conn)
        self._timeline = tier_timeline.TierTimeline(entries)
        aliases = db.account_aliases(conn)
        # Display names, because that is what a folded record carries and what
        # a breakdown row groups on. The timeline is keyed by uuid, so the two
        # meet here and nowhere else. account_names rather than
        # account_overview: the spend beside a name there costs a dedup scan of
        # every record, and a plan tile throws it away.
        self._uuids = [
            row["account_uuid"] for row in reports.account_names(conn)
            if reports.account_display(row["account_uuid"], row["label"], aliases) in accounts
        ] if entries else []

    def cost(self, start: datetime, end: datetime) -> float | None:
        """What the plans behind this page's accounts cost over *start*..*end*.

        Summed across them, so the whole-server page sets one figure against
        one valuation and an account page sets that account's.

        Prorated by calendar month rather than over the span, so a seven-day
        range costs a quarter of a month and half a year costs six of them.

        None where no account has a declared plan covering the span, which a
        caller must not turn into zero: $0.00 beside real spend reads as a span
        that was free, and a multiple over it reads as infinite.
        """
        if end <= start:
            return None
        total, priced = 0.0, False
        for uuid in self._uuids:
            spans = self._timeline.stretches(uuid, start.timestamp(), end.timestamp())
            usd = pricing.prorated_plan_cost(spans)
            if usd is not None:
                total, priced = total + usd, True
        return total if priced else None


_CACHE_LOCK = threading.Lock()


def cache_stamp(conn, now: datetime) -> tuple:
    """What a held view is checked against: what has been pushed, and the day.

    `db.content_stamp` covers every write and every name typed into /settings.
    The local date is beside it because a range ends at the next midnight, and
    a view held across one keeps drawing yesterday's axis.
    """
    return db.content_stamp(conn), now.strftime("%Y-%m-%d")


class StampCache[T]:
    """Built views, each held until the stamp it was built from moves.

    *limit* evicts the least recently served entry and belongs to a key space
    that grows — one entry per entity, per window instance, per day. A key
    space of one entry per range toggle passes None and needs no bound.

    Two threads missing on one key both build. That costs a duplicate query,
    where holding the lock across a build queues every other key behind one.
    """

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit
        self._held: OrderedDict[tuple, tuple[tuple, T]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._held)

    def clear(self) -> None:
        self._held.clear()

    def get(self, key: tuple, stamp: tuple, build: Callable[[], T]) -> T:
        """The view held for *key* under *stamp*, or what *build* returns.

        Whatever *build* raises propagates and nothing is stored for it: a URL
        naming a window nobody pushed is a 404 on every request rather than a
        held one.
        """
        with _CACHE_LOCK:
            held = self._held.get(key)
            if held is not None and held[0] == stamp:
                self._held.move_to_end(key)
                return held[1]
        view = build()
        with _CACHE_LOCK:
            self._held[key] = (stamp, view)
            self._held.move_to_end(key)
            while self.limit is not None and len(self._held) > self.limit:
                self._held.popitem(last=False)
        return view


_CACHE: StampCache[Dashboard] = StampCache()


def cached_build(database: db.Database, days: int, now: datetime | None = None,
                 hide_redacted: bool = False) -> Dashboard:
    """build(), reusing the last one until a push or a new day invalidates it.

    One entry per (database, range toggle, preference), so the whole cache is
    twice as many entries as RANGES has per server.

    Keyed on the database path and not on the range alone, because a process
    can hold more than one — every server test does — and two empty databases
    have the same stamp. The preference is in the key because it changes every
    figure the view holds, and one browser's cookie must not answer another's
    request.
    """
    conn = database.connect()
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in RANGES else DEFAULT_RANGE
    return _CACHE.get(
        (str(database.path), days, hide_redacted), cache_stamp(conn, now),
        lambda: build(conn, days, now, hide_redacted=hide_redacted),
    )


DETAIL_CACHE_MAX = 96
"""How many detail pages `cached_detail` holds before the oldest goes.

Chosen against what one weighs rather than picked round: an all-time entry on a
three-year corpus is its four charts, seven series each over a day per column,
plus the breakdown rows behind them -- a few hundred kilobytes at the top end.
Ninety-six of those is tens of megabytes held against a server whose database
is hundreds, and it covers every model, machine and account a server has with
room for the months people click through.
"""

_DETAIL_CACHE: StampCache[Dashboard] = StampCache(DETAIL_CACHE_MAX)


def cached_detail(database: db.Database, days: int, scope: Scope,
                  now: datetime | None = None, hide_redacted: bool = False) -> Dashboard:
    """One entity's page, held the way `cached_build` holds the index.

    Bounded, where `cached_build` needs no bound. Its key space is one entry
    per range toggle per server; this one is one per model, project, machine,
    account, day, week and month the server holds, and a new day arrives every
    day.

    Raises whatever `build` raises -- a period key that is not a date is a
    ValueError, and nothing is stored for it.
    """
    conn = database.connect()
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in RANGES else DEFAULT_RANGE
    return _DETAIL_CACHE.get(
        (str(database.path), days, scope.dimension, scope.key, hide_redacted),
        cache_stamp(conn, now),
        lambda: build(conn, days, now, scope=scope, hide_redacted=hide_redacted),
    )


def _period_records(conn, start: datetime, end: datetime, days: set[str],
                    hourly: bool) -> list[reports.MergedRecord]:
    """One period's records: the ones whose own calendar day the period holds.

    The ts window is a day wider at each end and the day is matched afterwards:
    `day` is the pushing machine's calendar day, and a machine an hour off this
    server's clock keeps records whose instant falls outside the local day they
    belong to.

    A day page loads records one at a time because the hour is what it plots.
    Anything wider plots by day and takes the grouped path, which folds the
    hour away and hands back a few thousand rows instead of the corpus.
    """
    filters = reports.Filters(since=start - timedelta(days=1), until=end + timedelta(days=1))
    load = reports.load if hourly else reports.load_grouped
    return [item for item in load(conn, filters) if item.record.day_key() in days]


def _visible(merged: list[reports.MergedRecord], hide_redacted: bool) -> list[reports.MergedRecord]:
    """*merged*, less the records whose project name their machine stripped.

    Dropped here rather than in SQL so the fold, the axis and every breakdown
    see one set of records: a page that charted a day the filter had emptied
    would draw a gap the table below it does not have.
    """
    return [item for item in merged if not item.redacted] if hide_redacted else merged


def build(conn, days: int, now: datetime | None = None,
          scope: Scope | None = None, hide_redacted: bool = False) -> Dashboard:
    """Everything the page shows, for one range toggle and one scope.

    With a *scope* the same fold runs over the records that match it alone, and
    the page gains the four charts. A period scope is its own range: the toggle
    cannot widen a page that is about one day, one week or one month.

    *hide_redacted* leaves out every record a restricted machine stripped the
    project from, which the totals, the account rows and the charts all lose
    with it. A call two machines pushed, where the stripped copy is the one the
    dedup kept, goes with them rather than falling back to the named copy.

    Raises ValueError where a period scope's key is not a date that period can
    be keyed on.
    """
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in RANGES else DEFAULT_RANGE
    if scope is not None and scope.is_period:
        hourly = scope.dimension == "day"
        start, end, day_axis = period_span(scope.dimension, scope.key)
        merged = _visible(_period_records(conn, start, end, set(day_axis), hourly), hide_redacted)
        axis = _hour_axis(scope.key) if hourly else day_axis
        position_of = _hour_position(start) if hourly else _day_position(axis)
    else:
        if days == ALL_TIME:
            start, end = all_time_bounds(db.oldest_record_ts(conn), now)
        else:
            start, end = range_bounds(days, now)
        merged = _visible(
            reports.load_grouped(conn, reports.Filters(since=start, until=end)), hide_redacted,
        )
        if scope is not None:
            key_of = _DIMENSION_KEYS[scope.dimension]
            merged = [item for item in merged if str(key_of(item)) == scope.key]
        axis = _day_axis(start, (end - start).days)
        if scope is not None:
            axis = _covered_axis(axis, merged)
        position_of = _day_position(axis)

    nok = reports.nok_context(merged)
    account_report = reports.build(merged, "account", nok)
    total_cost = account_report.total.cost
    accounts = [row.key for row in account_report.rows]
    breakdowns = {
        dimension: breakdown(merged, dimension, total_cost)
        for dimension in (SCOPES if scope is not None else DIMENSIONS)
    }
    # Attached after the fold rather than inside it: a plan cost is not a
    # property of any record, and breakdown() sees neither the connection the
    # timeline is read from nor which account a folded row belongs to.
    plans = _Plans(conn, set(accounts))
    for row in breakdowns.get("month", []):
        opens, closes = _month_bounds(row["key"])
        usd = plans.cost(opens, closes)
        if usd is not None:
            row["plan_usd"] = usd
            row["plan_multiple"] = (row["cost"] / usd) if usd > 0 else 0.0
            row["plan_saved"] = row["cost"] - usd
            # The month's own last day, as its records converted at their own
            # Oslo dates: a row's kroner must not be read off today's rate.
            row["plan_saved_nok"] = _in_nok(
                row["plan_saved"], nok, (closes - timedelta(days=1)).date(),
            )

    tiles = _tiles(merged, total_cost)
    # A period page charges its whole period: the month was paid for whether or
    # not every day of it was worked, and shortening it to the days with
    # records would flatter every quiet month. Every other scope is narrowed to
    # what it covers.
    priced_from, priced_to = (
        (start, end) if scope is None or scope.is_period
        else _active_span(merged, start, end)
    )
    plan_usd = plans.cost(priced_from, priced_to)
    if plan_usd is not None:
        # First in the row, though it is built last: it needs the span the page
        # covers, which is only known once the records are folded.
        tiles.insert(0, _plan_tile(
            total_cost, plan_usd, nok, (priced_to - timedelta(days=1)).date(),
            scope.dimension if scope and scope.dimension in SLICE_SCOPES else None,
            _fmt_span(pricing.months_in_span(priced_from, priced_to),
                      (priced_to - priced_from).days),
        ))

    return Dashboard(
        days=days,
        start=start.strftime("%Y-%m-%d"),
        end=(end - timedelta(days=1)).strftime("%Y-%m-%d"),
        total_cost=total_cost,
        total_cost_nok=account_report.total.cost_nok,
        nok_enabled=nok.enabled,
        accounts=_account_rows(account_report, total_cost),
        tiles=tiles,
        chart_days=[] if scope is not None else axis,
        series=[] if scope is not None else _chart(merged, axis, accounts),
        breakdowns=breakdowns,
        machines=sorted({item.machine for item in merged}),
        scope=scope,
        charts=_charts(merged, axis, position_of) if scope is not None else [],
    )
