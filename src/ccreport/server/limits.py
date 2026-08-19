"""The merged rate-limit window pages: which windows filled, and what filled them.

A quota belongs to an account, not to a machine, so two laptops drawing on one
account push readings of the same window instance. Here they are unioned into
one fill curve — the thing the client's own `ccreport limits` cannot show,
because it only ever saw the part its own renders caught.

Everything the rows say about a window comes from `windows.py`, the module the
CLI's tables read. What this adds is the merge and the shapes uPlot wants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ccreport import pricing, windows
from ccreport.server import dashboard, db, reports

BUCKET_SWITCH_S = 6 * 3600
"""Window span above which the charts bucket by the hour instead of by five
minutes. A 5-hour window drawn hourly is five columns; a 7-day one drawn every
five minutes is two thousand."""

FINE_BUCKET_S = 300
COARSE_BUCKET_S = 3600


@dataclass
class WindowRow:
    """One window instance as the table prints it."""

    instance: windows.WindowInstance
    account: str
    spend: windows.WindowSpend
    machines: list[str]
    """Which machines reported a reading of it, in the order they first did."""

    @property
    def key(self) -> str:
        """The path segment that opens this window's own page."""
        return f"{int(self.instance.resets_at)}"


@dataclass
class WindowGroup:
    """One window type's rows, under the label the CLI prints for it."""

    window: str
    label: str
    rows: list[WindowRow]

    @property
    def scoped(self) -> bool:
        """Whether the rows name a model of their own."""
        return any(row.instance.model for row in self.rows)

    @property
    def hits(self) -> int:
        return sum(1 for row in self.rows if row.instance.hit_limit)

    @property
    def cache_hit(self) -> float | None:
        return windows.group_cache_hit(
            [row.instance for row in self.rows],
            {row.instance.key: row.spend for row in self.rows},
        )


@dataclass
class LimitsView:
    """Everything the window list page shows."""

    days: int
    start: str
    end: str
    groups: list[WindowGroup] = field(default_factory=list)


@dataclass
class WindowView:
    """Everything one window instance's page shows."""

    row: WindowRow
    label: str
    started: str
    resets: str
    tiles: list[dashboard.Tile] = field(default_factory=list)
    charts: list[dashboard.Chart] = field(default_factory=list)
    breakdown: list[dict] = field(default_factory=list)


def _account_of(sample: dict, aliases: dict[str, str]) -> str:
    return reports.account_display(
        sample["account_uuid"], sample["account_label"], aliases,
    )


def _merged_instances(
    samples: list[dict], aliases: dict[str, str],
) -> list[tuple[str, windows.WindowInstance, list[str]]]:
    """Group *samples* into one instance per (account, window, model, reset).

    The account leads because the quota is the account's: two machines signed
    into one account watched the same window fill, and their readings belong on
    one curve. Two accounts that happen to reset at the same minute do not.

    Samples arrive in ts order, so each instance's own list is already in fill
    order however many machines contributed to it, and the machine list is in
    the order each was first heard from.
    """
    by_key: dict[tuple, tuple[windows.WindowInstance, list[str]]] = {}
    for sample in samples:
        if windows.implausible_reset(sample):
            continue
        account = _account_of(sample, aliases)
        key = (account, sample["window"], sample["model"],
               windows.rl_window_key(sample["resets_at"]))
        entry = by_key.get(key)
        if entry is None:
            entry = by_key[key] = (
                windows.WindowInstance(key[1], key[2], key[3], []), [],
            )
        instance, machines = entry
        instance.samples.append(sample)
        if sample["machine"] not in machines:
            machines.append(sample["machine"])
    return [(key[0], inst, machines) for key, (inst, machines) in by_key.items()]


def _spend_indexes(
    conn, instances: list[tuple[str, windows.WindowInstance, list[str]]],
) -> dict[str, windows.SpendIndex]:
    """One record index per account, bounded to the span the windows cover.

    Per account because a window is priced against the records billed to it,
    and one index over every account would price a shared machine's windows
    with somebody else's work. Bounded because a page about last week has no
    use for two years of records.

    The full record path, not the grouped one: a 5-hour window is priced over
    hours, and a grouped row has folded the hour away. Dedup is what makes the
    number an answer — summing the rows raw double-counts every call a synced
    log stored twice.
    """
    if not instances:
        return {}
    since = datetime.fromtimestamp(min(i.first_ts for _a, i, _m in instances), tz=UTC)
    until = datetime.fromtimestamp(max(i.peak_ts for _a, i, _m in instances), tz=UTC)
    merged = reports.load(
        conn, reports.Filters(since=since, until=until + timedelta(seconds=1)),
    )
    per_account: dict[str, list] = {}
    for item in merged:
        per_account.setdefault(item.account, []).append(item.record)
    return {account: windows.SpendIndex(recs) for account, recs in per_account.items()}


_EMPTY_INDEX = windows.SpendIndex([])


def _rows(conn, samples: list[dict], now: float) -> list[WindowRow]:
    """Every window the samples describe, priced, in the CLI's printed order."""
    aliases = db.account_aliases(conn)
    instances = _merged_instances(samples, aliases)
    indexes = _spend_indexes(conn, instances)
    rows = [
        WindowRow(
            instance=instance,
            account=account,
            # The server has no Extra-usage series: that is a reading of one
            # machine's status line, and nothing pushes it.
            spend=windows.instance_spend(
                instance, indexes.get(account, _EMPTY_INDEX), now,
            ),
            machines=machines,
        )
        for account, instance, machines in instances
    ]
    rows.sort(key=lambda row: (windows.instance_order(row.instance), row.account))
    return rows


def _bounds(days: int, now: datetime) -> tuple[datetime, datetime]:
    """The span a range toggle selects, in whole local days like the dashboard's."""
    if days == dashboard.ALL_TIME:
        return dashboard.all_time_bounds(None, now)
    return dashboard.range_bounds(days, now)


def build(conn, days: int, now: datetime | None = None) -> LimitsView:
    """The window list for one range toggle, one table per window type."""
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in dashboard.RANGES else dashboard.DEFAULT_RANGE
    if days == dashboard.ALL_TIME:
        oldest = db.oldest_sample_ts(conn)
        start, end = dashboard.all_time_bounds(oldest, now)
    else:
        start, end = dashboard.range_bounds(days, now)
    samples = db.load_rate_limit_samples(conn, start.timestamp(), end.timestamp())
    rows = _rows(conn, samples, now.timestamp())
    return LimitsView(
        days=days,
        start=start.strftime("%Y-%m-%d"),
        end=(end - timedelta(days=1)).strftime("%Y-%m-%d"),
        groups=[
            WindowGroup(
                window=window,
                label=windows.LIMIT_WINDOW_LABELS.get(window, window),
                rows=[row for row in rows if row.instance.window == window],
            )
            for window in windows.window_types([row.instance for row in rows])
        ],
    )


def _stamp(when: float) -> str:
    """An instant as the local minute stamp the charts plot on."""
    return datetime.fromtimestamp(when, tz=UTC).astimezone().strftime("%Y-%m-%dT%H:%M")


def _axis(instance: windows.WindowInstance, now: float) -> tuple[list[str], float, float]:
    """The window's own span as chart buckets, plus its first instant and step.

    The span is the window's, not the sampled part of it: an hour nobody
    rendered in is an hour the quota was running, and drawing only what was
    watched would put a gap at the start of every window capture began late in.
    """
    start = instance.started_at
    if start is None:
        start = instance.first_ts
    end = min(instance.resets_at, max(now, instance.last_ts))
    step = FINE_BUCKET_S if (end - start) <= BUCKET_SWITCH_S else COARSE_BUCKET_S
    first = start - start % step
    count = max(int((end - first) // step) + 1, 1)
    return [_stamp(first + i * step) for i in range(count)], first, step


def _fill_traces(instance: windows.WindowInstance, first: float, step: float,
                 count: int) -> list[dashboard.Trace]:
    """The fill curve, one trace per machine that reported a reading.

    A bucket that machine took no reading in is None rather than 0, so the
    chart draws a gap where nobody was looking instead of a quota that emptied
    and refilled. Where two of its renders land in one bucket the later reading
    wins: the curve rises, and the newest reading is where the window stood.
    """
    traces: dict[str, list[float | None]] = {}
    for sample in instance.samples:
        machine = sample.get("machine") or "this machine"
        position = int((sample["ts"] - first) // step)
        if not 0 <= position < count:
            continue
        values = traces.setdefault(machine, [None] * count)
        values[position] = sample["used_pct"]
    return [dashboard.Trace(label=machine, values=values)
            for machine, values in traces.items()]


def _window_records(conn, row: WindowRow) -> list[reports.MergedRecord]:
    """The records that filled this window: its account, its span, its family.

    The family filter is the same one the Spend column applies — a scoped
    window is filled by the model it names and the Sonnet window by its own
    definition, while session and week count everything.
    """
    instance = row.instance
    start = instance.started_at if instance.started_at is not None else instance.first_ts
    merged = reports.load(conn, reports.Filters(
        since=datetime.fromtimestamp(start, tz=UTC),
        until=datetime.fromtimestamp(instance.resets_at, tz=UTC),
        account=row.account,
    ))
    family = windows.window_family(instance)
    if family is None:
        return merged
    return [m for m in merged if pricing.model_family(m.record.model) == family]


def _tiles(row: WindowRow) -> list[dashboard.Tile]:
    """The five numbers the table row carries, each read against its own line."""
    instance, spend = row.instance, row.spend
    fill = instance.fill_s / 3600
    rate = instance.burn_pph
    share = spend.cache_hit
    return [
        dashboard.Tile(
            "Peak", f"{instance.peak:.1f}%",
            f"opened at {instance.opening_pct:.1f}%, {len(instance.samples)} readings",
        ),
        dashboard.Tile(
            "Fill", f"{fill:.1f}h",
            "—" if rate is None else f"{rate:.2f} points an hour",
        ),
        dashboard.Tile(
            "Spend", "—" if spend.usd is None else f"${spend.usd:,.2f}",
            "over the fill span, at API prices",
        ),
        dashboard.Tile(
            "$/pp", "—" if spend.per_pp is None else f"${spend.per_pp:,.2f}",
            f"{instance.rise:.1f} points gained",
        ),
        dashboard.Tile(
            "Cache", "—" if share is None else f"{share * 100:.0f}%",
            "of the context shown was already paid for",
        ),
    ]


def _charts(merged: list[reports.MergedRecord], row: WindowRow,
            axis: list[str], first: float, step: float) -> list[dashboard.Chart]:
    """The fill curve and the three plots of what filled it.

    Four rather than one with a toggle, and one scale each: percent, dollars,
    tokens and calls answer different questions and cannot share an axis.
    """
    def position(item: reports.MergedRecord) -> int | None:
        at = int((item.record.timestamp.timestamp() - first) // step)
        return at if 0 <= at < len(axis) else None

    return [
        dashboard.Chart(
            key="fill", title="Fill", unit="percent", axis=axis,
            traces=_fill_traces(row.instance, first, step, len(axis)),
        ),
        dashboard.Chart(
            key="cost-model", title="Cost by model", unit="usd", axis=axis,
            traces=dashboard.traced(
                merged, axis, position,
                lambda item: [(item.record.model, item.record.cost())]),
        ),
        dashboard.Chart(
            key="tokens-kind", title="Tokens by kind", unit="tokens", axis=axis,
            traces=dashboard.traced(merged, axis, position, dashboard.token_pairs),
        ),
        dashboard.Chart(
            key="calls", title="Calls", unit="calls", axis=axis,
            traces=dashboard.traced(
                merged, axis, position,
                lambda item: [("Calls", float(item.record.count))]),
        ),
    ]


def build_window(
    conn, window: str, resets_at: float, model: str | None, account: str,
    now: datetime | None = None,
) -> WindowView:
    """One window instance's page.

    Raises:
        LookupError: no stored sample describes that window. The URL named a
            window this server has never been pushed a reading of, and an empty
            page would read as a window nobody used.
    """
    now = now or datetime.now(tz=UTC).astimezone()
    reset = windows.rl_window_key(resets_at)
    span = windows.LIMIT_WINDOW_SPAN_S.get(window, float(pricing.WEEK_WINDOW_S))
    samples = db.load_rate_limit_samples(conn, reset - span, reset + 1)
    aliases = db.account_aliases(conn)
    wanted = [
        s for s in samples
        if s["window"] == window
        and s["model"] == model
        and windows.rl_window_key(s["resets_at"]) == reset
        and _account_of(s, aliases) == account
    ]
    if not wanted:
        raise LookupError(window)
    [row] = _rows(conn, wanted, now.timestamp())
    axis, first, step = _axis(row.instance, now.timestamp())
    merged = _window_records(conn, row)
    total = sum(item.record.cost() for item in merged)
    started = row.instance.started_at
    return WindowView(
        row=row,
        label=windows.LIMIT_WINDOW_LABELS.get(window, window),
        started=_stamp(started if started is not None else row.instance.first_ts),
        resets=_stamp(reset),
        tiles=_tiles(row),
        charts=_charts(merged, row, axis, first, step),
        breakdown=dashboard.breakdown(merged, "model", total),
    )
