"""The merged speed page: per-model latency and output rate, across machines.

`ccreport speed` answers this for one machine. Here the same buckets fold every
machine's pushed spans, so a model can be compared against itself on two hosts
and against the others on one page. The span is the client's — derived off the
log's line order at parse time and pushed beside the record — and every figure
is `ccreport.speed.SpeedBucket`'s, so the page and the CLI cannot disagree
about what a median is or which replies are too short to have a rate.

Only files still on a machine's disk carry timing: an archived file is never
pushed, and a row stored before protocol 3 has no span until a `--full` push
replaces its file. The page counts the calls it could not time rather than
leaving them out silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ccreport import speed
from ccreport.server import dashboard, db, reports

MIN_RATE_OUTPUT_TOKENS = speed.MIN_RATE_OUTPUT_TOKENS
"""The floor the page's footnote names, so the template reads the rule it states."""


@dataclass(frozen=True)
class SpeedRow:
    """One (model, machine) cell, or a model's every machine where machine is None."""

    model: str
    machine: str | None
    calls: int
    """Deduped calls in the range, timed or not."""
    requests: int
    """Of those, the ones with a known span."""
    turns: int
    latency_mid: float | None
    latency_p90: float | None
    rate_mid: float | None
    turn_mid: float | None

    @property
    def timed_share(self) -> float | None:
        return self.requests / self.calls if self.calls else None


@dataclass(frozen=True)
class ModelGroup:
    total: SpeedRow
    machines: list[SpeedRow] = field(default_factory=list)
    """Per machine, busiest first. Empty where one machine made every call: its
    row would repeat the total."""


@dataclass(frozen=True)
class SpeedView:
    days: int
    start: str
    end: str
    groups: list[ModelGroup]
    calls: int
    requests: int


class _Cell:
    __slots__ = ("bucket", "calls")

    def __init__(self) -> None:
        self.bucket = speed.SpeedBucket()
        self.calls = 0


def _row(model: str, machine: str | None, cell: _Cell) -> SpeedRow:
    bucket = cell.bucket
    return SpeedRow(
        model=model, machine=machine, calls=cell.calls, requests=bucket.requests,
        turns=len(bucket.turns_ms),
        latency_mid=bucket.latency_mid(), latency_p90=bucket.latency_p90(),
        rate_mid=bucket.rate_mid(), turn_mid=bucket.turn_mid(),
    )


def fold(
    calls: list[reports.SpeedRow], turns: list[tuple[str, str, int]], labels: dict[str, str],
) -> list[ModelGroup]:
    """Fold deduped calls and turns into one group per model, busiest first.

    Every call is counted toward its cell and only a timed one reaches the
    bucket, which is what lets a row say what share of it was measured. Keyed
    on the machine's label rather than its id, so two machines a person gave
    one name draw one row, the way every other page draws them.
    """
    cells: dict[tuple[str, str | None], _Cell] = {}

    def both(model: str, machine_id: str) -> tuple[_Cell, _Cell]:
        label = labels.get(machine_id, machine_id)
        return (cells.setdefault((model, None), _Cell()),
                cells.setdefault((model, label), _Cell()))

    for model, machine_id, output_tokens, start, end in calls:
        if not speed.timed_model(model):
            continue
        for cell in both(model, machine_id):
            cell.calls += 1
            cell.bucket.add_request(output_tokens, start, end)
    for model, machine_id, duration_ms in turns:
        if not speed.timed_model(model):
            continue
        for cell in both(model, machine_id):
            cell.bucket.add_turn(duration_ms)

    groups = []
    for (model, machine), cell in cells.items():
        if machine is not None:
            continue
        machines = sorted(
            (_row(model, name, other) for (other_model, name), other in cells.items()
             if other_model == model and name is not None),
            key=lambda row: (-row.calls, row.machine),
        )
        groups.append(ModelGroup(
            total=_row(model, None, cell), machines=machines if len(machines) > 1 else [],
        ))
    groups.sort(key=lambda group: (-group.total.calls, group.total.model))
    return groups


def build(conn, days: int, now: datetime | None = None) -> SpeedView:
    """The speed table for one range toggle, in whole local days like the dashboard's."""
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in dashboard.RANGES else dashboard.DEFAULT_RANGE
    if days == dashboard.ALL_TIME:
        start, end = dashboard.all_time_bounds(db.oldest_record_ts(conn), now)
    else:
        start, end = dashboard.range_bounds(days, now)
    labels = dict(conn.execute("SELECT machine_id, label FROM machines").fetchall())
    groups = fold(
        reports.load_speed(conn, reports.Filters(since=start, until=end)),
        reports.load_turns(conn, start.timestamp(), end.timestamp()),
        labels,
    )
    return SpeedView(
        days=days,
        start=start.strftime("%Y-%m-%d"),
        end=(end - timedelta(days=1)).strftime("%Y-%m-%d"),
        groups=groups,
        calls=sum(group.total.calls for group in groups),
        requests=sum(group.total.requests for group in groups),
    )


_CACHE: dashboard.StampCache[SpeedView] = dashboard.StampCache()


def cached_build(database: db.Database, days: int, now: datetime | None = None) -> SpeedView:
    """build(), held against the stamp the dashboard's index is held at.

    `db.content_stamp` moves on every file a push replaces, and a file's turns
    are replaced with its records, so neither half can go stale behind it.
    """
    conn = database.connect()
    now = now or datetime.now(tz=UTC).astimezone()
    days = days if days in dashboard.RANGES else dashboard.DEFAULT_RANGE
    return _CACHE.get(
        (str(database.path), days), dashboard.cache_stamp(conn, now),
        lambda: build(conn, days, now),
    )
