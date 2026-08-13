"""The read side: merged records in, the same rows a local report has out.

Every number here goes through `aggregate.py`, the module the CLI renders from,
so a merged report and a single-machine one cannot disagree about what a week
cost. What this adds is the part only a merged report has: which machine and
which account each row came from.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ccreport import aggregate, exchange
from ccreport.aggregate import NokCtx, ReportRows, Row, UsageRecord
from ccreport.server.db import REC_COLS


@dataclass(frozen=True)
class Filters:
    """What a report may narrow itself to. Every field is optional."""

    since: datetime | None = None
    until: datetime | None = None
    project: str | None = None
    account: str | None = None
    machine: str | None = None


@dataclass
class MergedRecord:
    """A record with the two things a local one has no room for."""

    record: UsageRecord
    machine: str
    account: str


_SELECT = ", ".join(REC_COLS)


def _where(filters: Filters) -> tuple[str, list]:
    """The filter clause and its parameters, or an empty clause.

    ts bounds rather than day bounds: `day` is the machine's own calendar day
    and two machines can disagree about which day an instant falls in, so a
    range that means the same thing to both has to be stated in instants.
    """
    clauses, params = [], []
    if filters.since is not None:
        clauses.append("ts >= ?")
        params.append(filters.since.timestamp())
    if filters.until is not None:
        clauses.append("ts < ?")
        params.append(filters.until.timestamp())
    if filters.project is not None:
        clauses.append("project = ?")
        params.append(filters.project)
    if filters.account is not None:
        clauses.append("(account_label = ? OR account_uuid = ?)")
        params += [filters.account, filters.account]
    if filters.machine is not None:
        clauses.append("machine_id = ?")
        params.append(filters.machine)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def load(conn: sqlite3.Connection, filters: Filters | None = None) -> list[MergedRecord]:
    """Every stored record the filters admit, deduplicated, oldest first.

    Two machines sharing a synced home directory both stored their copy of the
    same call. Here they collapse to one, keyed by account and dedup key, and
    the copy that survives is the one with the lowest id — the machine that
    reported it first. So a synced log neither double-counts nor silently
    erases one machine's contribution.

    A record with no dedup key is kept as it stands: the log carried nothing to
    match it on, and dropping it would lose a real call.
    """
    filters = filters or Filters()
    clause, params = _where(filters)
    rows = conn.execute(
        f"SELECT machine_id, {_SELECT} FROM server_records{clause} ORDER BY id",  # noqa: S608
        params,
    ).fetchall()

    labels = dict(conn.execute("SELECT machine_id, label FROM machines").fetchall())
    seen: set[tuple[str, str]] = set()
    merged: list[MergedRecord] = []
    for row in rows:
        machine_id, rec = row[0], dict(zip(REC_COLS, row[1:], strict=True))
        key = (rec["account_uuid"], rec["dk"])
        if rec["dk"] and key in seen:
            continue
        seen.add(key)
        merged.append(_as_merged(rec, labels.get(machine_id, machine_id)))
    merged.sort(key=lambda m: m.record.timestamp)
    return merged


def _as_merged(rec: dict, machine_label: str) -> MergedRecord:
    """One stored row as the record the aggregation understands.

    day and oslo_date travel on the record rather than being re-derived: they
    were computed against the machine's own zone at ingest, and the server's is
    nobody's working day.
    """
    account = rec["account_label"] or rec["account_uuid"]
    record = UsageRecord(
        message_id=rec["mid"] or "",
        model=rec["model"],
        tokens=aggregate.TokenCounts(
            input=rec["input_tokens"], output=rec["output_tokens"],
            cache_create=rec["cache_create"], cache_read=rec["cache_read"],
        ),
        timestamp=datetime.fromtimestamp(rec["ts"], tz=UTC),
        session_id=rec["sid"] or "",
        project=rec["project"] or "(redacted)",
        # The server's own price, always. cost_usd is the field the aggregation
        # reads as "already known", which here it is: the server computed it at
        # ingest with its own pricing.py and stored it NOT NULL.
        cost_usd=rec["cost"],
        dedup_key=rec["dk"],
        account=account,
        oslo_date=date.fromisoformat(rec["oslo_date"]),
    )
    # The stored day is the machine's calendar day. Priming the memo is what
    # makes the daily and monthly reports bucket by it rather than by the
    # server's midnight.
    record._day = rec["day"]  # noqa: SLF001 - the memo is the point
    return MergedRecord(record=record, machine=machine_label, account=account)


def nok_context(merged: list[MergedRecord], *, mva: bool = True) -> NokCtx:
    """A NokCtx over the rates this server holds for the dates in *merged*.

    The server converts, so two clients cannot disagree about the same week.
    Each record converts at its own Oslo date, which travelled with it from
    ingest — not at the date the report was asked for.
    """
    if not merged:
        return NokCtx(mva=mva)
    earliest = min(m.record.fx_date() for m in merged)
    rates = exchange.read_rates_since(earliest)
    if not rates:
        return NokCtx(mva=mva)
    return NokCtx(rates, max(rates), mva)


def _attribute(rows: list[Row], merged: list[MergedRecord], key_fn) -> None:
    """Fill each row's machine and account split.

    The same bucketing the rows came from, run once more over the pair of
    labels: a row's key decides which records belong to it, and each record
    knows which machine stored it and which account paid.
    """
    by_key: dict[str, list[MergedRecord]] = {}
    for item in merged:
        by_key.setdefault(str(key_fn(item)), []).append(item)
    for row in rows:
        for item in by_key.get(row.key, ()):
            cost = item.record.cost()
            row.machines[item.machine] = row.machines.get(item.machine, 0.0) + cost
            row.accounts[item.account] = row.accounts.get(item.account, 0.0) + cost


_KEY_FNS = {
    "day": lambda m: m.record.day_key(),
    "month": lambda m: m.record.month_key(),
    "project": lambda m: m.record.project,
    "session": lambda m: m.record.session_id,
    "account": lambda m: m.record.account,
}

KINDS = tuple(_KEY_FNS)


def build(
    merged: list[MergedRecord], kind: str, nok: NokCtx, *,
    limit: int | None = None, breakdown: bool = False,
) -> ReportRows:
    """One report's rows, with the machine and account split filled in.

    Raises:
        KeyError: *kind* is not one of KINDS.
    """
    key_fn = _KEY_FNS[kind]
    records = [m.record for m in merged]
    if kind == "day":
        report = aggregate.daily_rows(records, nok, breakdown=breakdown)
    elif kind == "month":
        report = aggregate.monthly_rows(records, nok)
    elif kind == "project":
        report = aggregate.project_rows(records, nok, limit=limit)
    elif kind == "session":
        report = aggregate.session_rows(records, nok, limit=limit)
    else:
        report = aggregate.account_rows(records, nok)
    _attribute(report.rows, merged, key_fn)
    return report
