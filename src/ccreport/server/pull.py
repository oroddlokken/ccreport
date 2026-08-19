"""The remainder one machine is missing: what every *other* machine spent.

A person on two machines reads a status-line cost line that is short by whatever
the other machine spent, and this server already holds both halves. This module
computes the half the asking machine does not have, and the ingest response
carries it home.

Two grains, because neither derives the other. A rolling window is sub-day and
cannot be summed out of daily rows; a daily table cannot be summed out of
windows that overlap. So the remainder travels as per-minute cost buckets, which
answer any rolling window the client asks for, and as one row per (machine, day,
project) for the reports.

The exclusion is by dedup identity, not by machine id alone. Dropping the asking
machine's own rows is not enough: two machines sharing a synced home directory
both pushed the same call, so what is left would still hold a copy of work the
client is about to add itself.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ccreport import pricing
from ccreport.server import db

BUCKET_S = 60
"""Seconds per cost bucket in the window half of the remainder.

A minute, not a second: 30 days of minutes is 43k buckets per machine whatever
the corpus, where seconds would grow with the call count. The cost is that a
rolling window's trailing edge lands on the minute rather than on the second,
which is a minute of one other machine's spend on a window measured in hours.
"""


@dataclass
class MachineRemainder:
    """One contributing machine's half of the remainder."""

    machine_id: str
    label: str
    last_seen: float
    """When that machine last pushed. What the staleness marker reads: a
    machine that stopped pushing is not a machine that stopped spending."""
    buckets: list[tuple[float, float]] = field(default_factory=list)
    """(bucket start epoch, cost) in ts order, for the rolling windows."""
    days: list[tuple[str, str, float, int, int, int, int, int]] = field(
        default_factory=list,
    )
    """(day, project, cost, input, output, cache_create, cache_read, calls).

    project is "" where the machine that pushed it redacted the name, which is
    what a restricted machine sends for a project outside its allow list."""


_EXCLUDE = """
    a.account_uuid = ? AND a.machine_id != ?
    AND (a.dk IS NULL OR a.dk = '' OR a.dk NOT IN (
        SELECT dk FROM server_records
         WHERE account_uuid = ? AND machine_id = ? AND dk IS NOT NULL AND dk != ''
    ))
    AND (a.dk IS NULL OR a.dk = '' OR a.id IN (
        SELECT MIN(id) FROM server_records
         WHERE account_uuid = ? AND machine_id != ?
         GROUP BY account_uuid, dk
    ))
"""
"""Which rows are genuinely elsewhere, for account ? and asking machine ?.

Three conditions and each earns its place. The first drops the asking machine's
own rows. The second drops any remaining copy of a call that machine also
pushed — machine-id exclusion alone would double-count a session log present on
two machines, because the server dedups across the set it folds and a client
adding its local total to that set's leftovers cannot see the overlap. The third
is the ordinary dedup among what is left, so two *other* machines sharing a log
contribute it once.

A record with no dedup key is kept as it stands, as every other reader here
keeps it: the log carried nothing to match it on.
"""


def _params(account_uuid: str, machine_id: str) -> list:
    return [account_uuid, machine_id] * 3


def bucket_floor(now: float) -> float:
    """The oldest instant the cost buckets cover, for a pull taken at *now*.

    The longest rolling window, and no further: the buckets exist to answer
    those, and unbounded they would grow with the history rather than with the
    window list. Everything older is in the daily rows, which keep every day.
    """
    span = max(w.delta.total_seconds() for w in pricing.ROLLING_WINDOWS)
    return now - span


def remainder(
    conn: sqlite3.Connection, account_uuid: str, machine_id: str, floor: float,
) -> list[MachineRemainder]:
    """What every machine but *machine_id* spent on *account_uuid*.

    Two grouped passes over the same exclusion rather than one pass in Python:
    a merged history of half a million calls is a few thousand buckets and a
    few thousand day rows, and neither reaches this process as records.
    """
    machines = {
        row[0]: MachineRemainder(machine_id=row[0], label=row[1], last_seen=row[2])
        for row in conn.execute(
            "SELECT machine_id, label, last_seen FROM machines WHERE machine_id != ?",
            (machine_id,),
        )
    }
    for mid, bucket, cost in conn.execute(
        f"SELECT a.machine_id, CAST(a.ts / {BUCKET_S} AS INTEGER) * {BUCKET_S}, "  # noqa: S608
        f"SUM(a.cost) FROM server_records a WHERE a.ts >= ? AND {_EXCLUDE} "
        "GROUP BY 1, 2 ORDER BY 2",
        [floor, *_params(account_uuid, machine_id)],
    ):
        entry = machines.get(mid)
        if entry is not None:
            entry.buckets.append((float(bucket), cost))
    for row in conn.execute(
        "SELECT a.machine_id, a.day, COALESCE(a.project, ''), SUM(a.cost), "  # noqa: S608
        "SUM(a.input_tokens), SUM(a.output_tokens), SUM(a.cache_create), "
        f"SUM(a.cache_read), COUNT(*) FROM server_records a WHERE {_EXCLUDE} "
        "GROUP BY 1, 2, 3",
        _params(account_uuid, machine_id),
    ):
        entry = machines.get(row[0])
        if entry is not None:
            entry.days.append(tuple(row[1:]))  # type: ignore[arg-type]
    return [m for m in machines.values() if m.buckets or m.days]


_memo: dict[tuple[str, str, float], tuple[tuple, list[MachineRemainder]]] = {}
"""(account, asking machine, bucket floor) -> (content stamp, its remainder).

Held per key rather than per request because a pull between pushes asks the same
question and the stamp is what says the answer moved. Without it every pull runs
the fold above on a token-authed endpoint reachable from anywhere.
"""


def cached_remainder(
    conn: sqlite3.Connection, account_uuid: str, machine_id: str, now: float,
) -> list[MachineRemainder]:
    """remainder(), recomputed only when the server's content or the day moved.

    The bucket floor rides in the key alongside the content stamp. It is the
    only part of the answer that depends on *now*, and rounding it to the hour
    is what lets a pull between pushes cost a stamp read: the client sums the
    buckets it is handed against its own instant, so an hour of extra buckets
    at the far end changes no window it asks for.
    """
    floor = bucket_floor(now)
    key = (account_uuid, machine_id, floor - floor % 3600)
    stamp = db.content_stamp(conn)
    hit = _memo.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    rows = remainder(conn, account_uuid, machine_id, key[2])
    _memo[key] = (stamp, rows)
    return rows


def clear_memo() -> None:
    """Forget every memoized remainder. For tests, and for a restarted app."""
    _memo.clear()
