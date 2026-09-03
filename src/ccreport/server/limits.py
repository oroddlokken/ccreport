"""The merged rate-limit window pages: which windows filled, and what filled them.

A quota belongs to an account, not to a machine, so two laptops drawing on one
account push readings of the same window instance. Here they are unioned into
one fill curve — the thing the client's own `ccreport limits` cannot show,
because it only ever saw the part its own renders caught.

The Extra column is the one thing that is not unioned. It reads cumulative
account spend, so every machine reports the same dollars rather than a share of
them, and one machine's series answers for the window.

Everything the rows say about a window comes from `windows.py`, the module the
CLI's tables read. What this adds is the merge and the shapes uPlot wants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ccreport import pricing, tier_timeline, windows
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

    @property
    def stretch(self) -> int:
        """Which fill curve under that reset, for the query string beside it.

        In the query rather than the path so a link written before a rebase
        existed still opens the stretch that opened the window, which is the
        one it named.
        """
        return self.instance.stretch


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


def _tier_changes(conn) -> dict[str, list[float]]:
    """When each account's declared plan moved, oldest first, keyed by uuid.

    A window is only cut where a plan change explains the fall, and the server's
    tier history is declared rather than pushed: an account nobody typed a
    timeline for has no changes and so is never cut. The first entry establishes
    a tier rather than moving one — there is nothing before it to have moved
    from.
    """
    moved: dict[str, list[float]] = {}
    previous: dict[str, str | None] = {}
    for entry in db.account_tiers(conn):
        tier = tier_timeline.effective_tier(entry.tiers())
        if entry.account in previous and tier != previous[entry.account]:
            moved.setdefault(entry.account, []).append(entry.ts)
        previous[entry.account] = tier
    return moved


def _merged_instances(
    samples: list[dict], aliases: dict[str, str], changes: dict[str, list[float]],
) -> list[tuple[str, windows.WindowInstance, list[str]]]:
    """Group *samples* into one instance per (account, window, model, reset).

    The account leads because the quota is the account's: two machines signed
    into one account watched the same window fill, and their readings belong on
    one curve. Two accounts that happen to reset at the same minute do not.

    Samples arrive in ts order, so each instance's own list is already in fill
    order however many machines contributed to it, and the machine list is in
    the order each was first heard from.

    A group is then cut into stretches the way `windows.window_instances` cuts
    one machine's: a plan change restarts the percentage under an unchanged
    reset time, and a curve drawn across that fall belongs to neither quota. The
    machines are counted per stretch rather than per group, because the column
    says who reported a reading of the row it sits on.
    """
    by_key: dict[tuple, list[dict]] = {}
    uuids: dict[tuple, set[str]] = {}
    for sample in samples:
        if windows.implausible_reset(sample):
            continue
        account = _account_of(sample, aliases)
        key = (account, sample["window"], sample["model"],
               windows.rl_window_key(sample["resets_at"]))
        by_key.setdefault(key, []).append(sample)
        uuids.setdefault(key, set()).add(sample["account_uuid"])
    merged = []
    for key, grouped in by_key.items():
        account, window, model, reset = key
        # Keyed by display name, which an alias can point two uuids at, so the
        # changes of every account behind this row are what may cut it.
        moved = sorted(
            ts for uuid in uuids[key] for ts in changes.get(uuid, ())
        )
        for i, (change, stretch) in enumerate(windows.rebase_cuts(grouped, moved)):
            machines: list[str] = []
            for sample in stretch:
                if sample["machine"] not in machines:
                    machines.append(sample["machine"])
            merged.append((
                account,
                windows.WindowInstance(window, model, reset, stretch, i, change),
                machines,
            ))
    return merged


def _spend_indexes(
    conn, instances: list[tuple[str, windows.WindowInstance, list[str]]],
) -> dict[str, windows.SpendIndex]:
    """One record index per account, bounded to the span the windows cover.

    Per account because a window is priced against the records billed to it,
    and one index over every account would price a shared machine's windows
    with somebody else's work. Bounded because a page about last week has no
    use for two years of records.

    Per call, not per bucket: a 5-hour window is priced over hours, and a
    grouped row has folded the hour away. Not a coarser grain either --
    bucketing by (model, minute) moved a window's spend by 9.7%, and by five
    minutes 37%, because a fill span of minutes is most of one bucket. Dedup is
    what makes the number an answer: summing the rows raw double-counts every
    call a synced log stored twice.

    Per call is not the same as per record, though, and `reports.load_spend`
    reads the five columns an index keeps instead of building a UsageRecord for
    each — the identity on one is not something a window is priced by.

    Narrowed to the one account where the instances name one, which is every
    window page: the list page draws whatever accounts pushed a reading and
    cannot say a single name, so it reads the span whole.
    """
    if not instances:
        return {}
    since = datetime.fromtimestamp(min(i.first_ts for _a, i, _m in instances), tz=UTC)
    until = datetime.fromtimestamp(max(i.peak_ts for _a, i, _m in instances), tz=UTC)
    named = {account for account, _i, _m in instances}
    per_account = reports.load_spend(conn, reports.Filters(
        since=since, until=until + timedelta(seconds=1),
        account=next(iter(named)) if len(named) == 1 else None,
    ))
    return {account: windows.SpendIndex.from_rows(rows) for account, rows in per_account.items()}


_EMPTY_INDEX = windows.SpendIndex([])


def _extra_series(
    conn, aliases: dict[str, str],
) -> dict[str, dict[str, list[tuple[float, float]]]]:
    """Account -> machine -> its Extra readings, oldest first.

    Unbounded in time on purpose. A span needs a reading at or before it to
    subtract from, and on a series sampled a few times a day that reading can
    be older than any bound a page would set; the table holds one row per
    reading per machine, not one per call.

    Keyed on the displayed account, as the window rows are, so an alias typed on
    /settings/accounts selects the same readings the email does.
    """
    per_account: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for (uuid, label, machine), series in db.load_extra_samples(conn).items():
        account = reports.account_display(uuid, label, aliases)
        per_account.setdefault(account, {})[machine] = series
    return per_account


def _instance_extra(
    instance: windows.WindowInstance,
    machines: dict[str, list[tuple[float, float]]],
    now: float,
) -> float | None:
    """Extra billed while *instance* ran, from one machine's readings alone.

    Every machine on the account reports the *same* cumulative dollars, so the
    answer is one machine's, not a sum and not a merged series: a machine whose
    fetch lagged would report a lower figure after a higher one, and a drop in
    this series is what says the billing month rolled over.

    Which machine is whichever has the most readings inside the span, among
    those that can answer at all — the one that watched the window most closely.
    Ties go to the lower machine name, so the page does not change under a
    reader who reloads it.
    """
    start = instance.started_at if instance.started_at is not None else instance.first_ts
    end = min(instance.resets_at, now)
    best: tuple[int, str] | None = None
    answer: float | None = None
    for machine, series in sorted(machines.items()):
        spent = windows.ExtraIndex(series).spent_between(start, end)
        if spent is None:
            continue
        inside = sum(1 for ts, _s in series if start <= ts <= end)
        if best is None or inside > best[0]:
            best = (inside, machine)
            answer = spent
    return answer


def _row_order(row: WindowRow) -> tuple[int, str, float, int, str, str]:
    """`windows.instance_order` with the reset and the stretch reversed, then the account.

    The stretch reverses with the reset it sits under, for the reason the reset
    does: where a plan change split one window in two, the curve filling now is
    the one a page is opened for.
    """
    rank, name, resets_at, model, stretch = windows.instance_order(row.instance)
    return (rank, name, -resets_at, -stretch, model, row.account)


def _rows(conn, samples: list[dict], now: float) -> list[WindowRow]:
    """Every window the samples describe, priced, newest reset first.

    The reverse of the CLI's printed order, which `windows.instance_order`
    still sets: a page is opened for the window that is filling now, and an
    all-time toggle puts hundreds of finished ones above it. The rest of that
    key is kept, so two windows resetting at one instant still order.
    """
    aliases = db.account_aliases(conn)
    instances = _merged_instances(samples, aliases, _tier_changes(conn))
    indexes = _spend_indexes(conn, instances)
    extra = _extra_series(conn, aliases)
    rows = [
        WindowRow(
            instance=instance,
            account=account,
            spend=windows.instance_spend(
                instance, indexes.get(account, _EMPTY_INDEX), now,
                _instance_extra(instance, extra.get(account, {}), now),
            ),
            machines=machines,
        )
        for account, instance, machines in instances
    ]
    rows.sort(key=_row_order)
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


WINDOW_CACHE_MAX = 128
"""How many window pages `cached_window` holds before the oldest goes.

Sized against the list they are clicked from: a 30-day toggle drew 89 window
instances on the server this was measured on, where a page pickles at 10 KiB
and a week window's 169-bucket charts at 33. The key space grows by about five
session windows a day, so it needs the bound the list cache does not.
"""

_LIST_CACHE: dashboard.StampCache[LimitsView] = dashboard.StampCache()
_WINDOW_CACHE: dashboard.StampCache[WindowView] = dashboard.StampCache(WINDOW_CACHE_MAX)


def cached_build(database: db.Database, days: int, now: datetime | None = None) -> LimitsView:
    """build(), held against the stamp `dashboard.cached_build` holds the index at.

    One entry per (database, range toggle), as the index cache is: what a
    window cost is priced over the whole span the listed windows cover, and
    that answer moves only when a push lands or the day rolls over.
    """
    conn = database.connect()
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in dashboard.RANGES else dashboard.DEFAULT_RANGE
    return _LIST_CACHE.get(
        (str(database.path), days), dashboard.cache_stamp(conn, now),
        lambda: build(conn, days, now),
    )


def cached_window(database: db.Database, window: str, resets_at: float, model: str | None,
                  account: str, stretch: int = 0,
                  now: datetime | None = None) -> WindowView:
    """build_window(), held the way `dashboard.cached_detail` holds an entity page.

    Keyed on the reset `windows.rl_window_key` rounds to rather than on the
    number in the URL, so the second of two links a rounding apart is served
    the page the first built.

    Raises the LookupError build_window raises, and stores nothing for it.
    """
    conn = database.connect()
    now = now or datetime.now(tz=UTC).astimezone()
    key = (str(database.path), window, windows.rl_window_key(resets_at), model, account,
           stretch)
    return _WINDOW_CACHE.get(
        key, dashboard.cache_stamp(conn, now),
        lambda: build_window(conn, window, resets_at, model, account, stretch, now),
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


def _window_records(
    conn, row: WindowRow, first: float, step: float,
) -> list[reports.MergedRecord]:
    """The records that filled this window: its account, its span, its family.

    The family filter is the same one the Spend column applies — a scoped
    window is filled by the model it names and the Sonnet window by its own
    definition, while session and week count everything.

    Folded to the axis the charts plot on, which is what every caller does with
    them anyway: three charts sum into those buckets and the breakdown sums the
    lot. A week window drew 36,846 records to fill 169 hourly columns.

    Not what prices the window — that is `_spend_indexes`, per call, because a
    fill span can be shorter than one of these buckets.
    """
    instance = row.instance
    start = instance.started_at if instance.started_at is not None else instance.first_ts
    merged = reports.load_bucketed(conn, reports.Filters(
        since=datetime.fromtimestamp(start, tz=UTC),
        until=datetime.fromtimestamp(instance.resets_at, tz=UTC),
        account=row.account,
    ), origin=first, step=step)
    family = windows.window_family(instance)
    if family is None:
        return merged
    return [m for m in merged if pricing.model_family(m.record.model) == family]


def _tiles(row: WindowRow) -> list[dashboard.Tile]:
    """The six numbers the table row carries, each read against its own line."""
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
            "Extra", "—" if spend.extra_usd is None else f"${spend.extra_usd:,.2f}",
            "billed as credits while the window ran"
            if spend.extra_usd is not None
            else "no reading bounds this window",
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
    stretch: int = 0, now: datetime | None = None,
) -> WindowView:
    """One window instance's page.

    *stretch* picks the fill curve where a plan change split the reset time in
    two; 0 is the one that opened the window, which is what a link written
    before the split named.

    Raises:
        LookupError: no stored sample describes that window, or none describes
            that stretch of it. The URL named a window this server has never
            been pushed a reading of, and an empty page would read as a window
            nobody used.
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
    rows = [r for r in _rows(conn, wanted, now.timestamp()) if r.instance.stretch == stretch]
    if not rows:
        raise LookupError(window)
    [row] = rows
    axis, first, step = _axis(row.instance, now.timestamp())
    merged = _window_records(conn, row, first, step)
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
