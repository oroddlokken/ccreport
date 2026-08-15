"""The record model and every report's aggregation, with nothing that renders.

ccreport.py used to fold records into buckets and build rich tables in one
pass. The server has to fold the same records with no terminal to draw on, and
the CLI has to render rows the server folded, so the two halves live apart:
this module answers "what are the rows", ccreport.py answers "what do they
look like".

Importing rich here would defeat the split, so nothing in this module may.

AUDIT: All calculations are documented in docs/calculation-reference.md.
When changing any calculation here, update that document to match.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Self

from ccreport.exchange import get_rate, to_oslo_date
from ccreport.pricing import calc_cost

UNKNOWN_ACCOUNT = "unknown"


@dataclass
class TokenCounts:
    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read

    def __iadd__(self, other: TokenCounts) -> Self:
        self.input += other.input
        self.output += other.output
        self.cache_create += other.cache_create
        self.cache_read += other.cache_read
        return self


@dataclass
class UsageRecord:
    message_id: str
    model: str
    tokens: TokenCounts
    timestamp: datetime
    session_id: str
    project: str
    cost_usd: float | None = None  # pre-calculated cost from Claude Code
    dedup_key: str | None = None  # message_id:request_id for deduplication
    cwd: str | None = None  # original cwd from JSONL; lets future migrations re-derive project
    repo: str | None = None  # normalized git remote captured at parse time (durable identity)
    account: str = UNKNOWN_ACCOUNT
    """Which Claude account this was billed to, assigned by _keep from the
    account_events timeline. Attribution is read-time on purpose: it is not
    parsed from the log (which never names an account) and not written to the
    record cache, so a change log that grows a missing event fixes every past
    report on the next run instead of needing a re-parse."""
    count: int = 1
    """How many API calls this stands for. One, except for the records a rollup
    row deserializes to, which stand for a whole day of a session's calls — the
    Calls column adds this rather than counting records."""
    oslo_date: date | None = None
    """The FX date to convert this record's cost under, when it cannot be
    derived from `timestamp`. A rollup record's timestamp is the newest in its
    group, and a local day can straddle two Oslo dates, so the date the group
    was actually rolled up under travels with it. None everywhere else, where
    to_oslo_date(timestamp) is the answer by construction."""
    _cost: float | None = field(default=None, repr=False, compare=False)
    """Memo for cost(). Deliberately not cost_usd: that field means 'the log gave
    us this' and is what _serialize_records writes to the SQLite cache, so a
    computed value landing there would persist as if it had been logged."""
    _local: datetime | None = field(default=None, repr=False, compare=False)
    _day: str | None = field(default=None, repr=False, compare=False)
    _fx_date: date | None = field(default=None, repr=False, compare=False)
    """Memos for the three date derivations below, on the same reasoning as
    _cost: a default run buckets the same records five to seven times, and
    every pass re-ran the same zone conversion per record. `timestamp` is set
    at construction and never assigned again, so none of these can go stale."""

    def cost(self) -> float:
        """USD cost: the log's own costUSD when present, else computed from tokens.

        Memoized — a default report aggregates the same records six times over,
        and the pricing lookup is the most expensive thing in the run.
        """
        if self._cost is None:
            self._cost = self.cost_usd if self.cost_usd is not None else calc_cost(
                self.tokens.input, self.tokens.output,
                self.tokens.cache_create, self.tokens.cache_read,
                self.model, self.timestamp,
            )
        return self._cost

    def local(self) -> datetime:
        """The timestamp in the machine's zone, which is how days are bucketed."""
        if self._local is None:
            self._local = self.timestamp.astimezone()
        return self._local

    def day_key(self) -> str:
        """Local calendar day as YYYY-MM-DD — the daily report's bucket."""
        if self._day is None:
            self._day = self.local().strftime("%Y-%m-%d")
        return self._day

    def month_key(self) -> str:
        """Local month as YYYY-MM. A prefix of the day, so it costs no second format."""
        return self.day_key()[:7]

    def fx_date(self) -> date:
        """The FX date this converts under: its own, else its timestamp's.

        A rollup record carries the date it was aggregated under, which is the
        only correct answer for it — re-deriving from its timestamp would move
        the whole group onto whichever Oslo date the newest call in it fell on.
        """
        if self._fx_date is None:
            self._fx_date = (
                self.oslo_date if self.oslo_date is not None
                else to_oslo_date(self.timestamp)
            )
        return self._fx_date


@dataclass
class AggBucket:
    tokens: TokenCounts = field(default_factory=TokenCounts)
    cost: float = 0.0
    cost_nok: float = 0.0
    nok_estimated: bool = False
    models: dict[str, float] = field(default_factory=dict)
    """Model name → its USD cost within this bucket; the Models column shows both."""
    count: int = 0

    def __iadd__(self, other: AggBucket) -> Self:
        """Fold another bucket in — how every report builds its TOTAL row."""
        self.tokens += other.tokens
        self.cost += other.cost
        self.cost_nok += other.cost_nok
        self.nok_estimated = self.nok_estimated or other.nok_estimated
        for model, cost in other.models.items():
            self.models[model] = self.models.get(model, 0.0) + cost
        self.count += other.count
        return self


def record_cost(rec: UsageRecord) -> float:
    """Return cost for a record: use pre-calculated costUSD if available, else compute."""
    return rec.cost()


@dataclass(frozen=True)
class NokCtx:
    """Everything the NOK column needs, as one value instead of four parameters.

    ``enabled`` is derived rather than stored: a separate has_nok flag could
    disagree with the rates it is supposed to describe.
    """

    rates: dict[str, float] = field(default_factory=dict)
    max_rate_date: str | None = None
    mva: bool = True
    _rate_memo: dict[date, tuple[float | None, bool]] = field(
        default_factory=dict, repr=False, compare=False,
    )
    """Oslo date → get_rate result. A corpus of any size spans only a few hundred
    Oslo dates, and get_rate walks back over weekends and holidays on every miss."""

    @property
    def enabled(self) -> bool:
        return bool(self.rates)

    @property
    def label(self) -> str:
        return "NOK+MVA" if self.mva else "NOK"

    def rate_for(self, oslo_date: date) -> tuple[float | None, bool]:
        """(rate, estimated) for an Oslo date, memoized across the whole run."""
        hit = self._rate_memo.get(oslo_date)
        if hit is None:
            hit = self._rate_memo[oslo_date] = get_rate(
                self.rates, oslo_date, _max_date=self.max_rate_date,
            )
        return hit


_REMOTE_RATES = MappingProxyType({"converted-by-the-server": 1.0})
"""A stand-in that makes NokCtx.enabled true without naming a rate.

Rows fetched from a server carry cost_nok already, converted there at each
record's own Oslo date. The renderers ask a NokCtx only whether the column
exists and what to head it — never for a rate — so the context a remote render
needs has no rates to hold, and a real-looking one here would be a rate nothing
converted anything at.
"""


def display_nok(*, enabled: bool, mva: bool = True) -> NokCtx:
    """A context for rendering rows something else already converted."""
    return NokCtx(dict(_REMOTE_RATES) if enabled else {}, None, mva)


def record_oslo_date(rec: UsageRecord) -> date:
    """The FX date a record converts under; see UsageRecord.fx_date."""
    return rec.fx_date()


def record_cost_nok(rec: UsageRecord, cost_usd: float, nok: NokCtx) -> tuple[float | None, bool]:
    """Convert a record's USD cost to NOK using its day's exchange rate.

    With nok.mva (the default), applies 25% Norwegian VAT (MVA) on top.
    Returns (nok_amount, estimated) where estimated is True only at the
    trailing edge of rate data (the true rate is not yet known).
    """
    rate, estimated = nok.rate_for(record_oslo_date(rec))
    if rate is None:
        return None, False
    multiplier = 1.25 if nok.mva else 1.0
    return cost_usd * rate * multiplier, estimated


def _accum_nok(bucket: AggBucket, rec: UsageRecord, cost_usd: float, nok: NokCtx) -> None:
    amount, estimated = record_cost_nok(rec, cost_usd, nok)
    if amount is not None:
        bucket.cost_nok += amount
        if estimated:
            bucket.nok_estimated = True


def bucket_by(
    records: list[UsageRecord],
    key_fn: Callable[[UsageRecord], Any],
    nok: NokCtx,
) -> dict[Any, AggBucket]:
    """Aggregate *records* into buckets keyed by ``key_fn(rec)``.

    Every report differs only in that key: a date, a month, a project, a
    session, or a (date, model) pair for the breakdown rows.
    """
    buckets: dict[Any, AggBucket] = defaultdict(AggBucket)
    for rec in records:
        b = buckets[key_fn(rec)]
        b.tokens += rec.tokens
        cost = record_cost(rec)
        b.cost += cost
        if nok.enabled:
            _accum_nok(b, rec, cost, nok)
        if rec.model != "<synthetic>":
            b.models[rec.model] = b.models.get(rec.model, 0.0) + cost
        b.count += rec.count
    return buckets


def short_model(model: str) -> str:
    m = model.replace("claude-", "")
    # Strip -YYYYMMDD date suffix
    if len(m) > 9 and m[-9] == "-" and m[-8:].isdigit():
        m = m[:-9]
    return m


def by_cost_desc(model: str, cost: float) -> tuple[float, str]:
    """Sort key: priciest model first, name breaking ties so output is stable."""
    return (-cost, short_model(model))


# --- Rows ---

TRAILING_WINDOW_DAYS = 14
"""How far back the monthly report's second projection averages over.

Bounded by ccreport.ROLLUP_WINDOW_DAYS and equal to it: past the rollup cutoff
a record stands for a whole day, which this window cannot split. The two are
stated separately rather than imported one from the other, which would point
this module back at the one it was split out of; test_reports asserts they
have not drifted.
"""


@dataclass
class Row:
    """One line of a report, before anything decides how it looks.

    *key* is the bucket: a day, a month, a project name, an account label, a
    session id. The optional fields carry what one report needs and the others
    have no use for, rather than a dict of extras nothing can type-check.
    """

    key: str
    agg: AggBucket
    breakdown: list[Row] = field(default_factory=list)
    """The daily report's per-model sub-rows, priciest first. Empty elsewhere."""
    project: str | None = None
    """The session report's project column."""
    last: datetime | None = None
    """When the session was last active, which is the date it shows."""
    machines: dict[str, float] = field(default_factory=dict)
    """Machine label → its share of this row's USD cost. Empty on a local
    report, which has one machine and nothing to attribute. The merged reports
    fill it, because which machine a number came from is the whole reason for
    merging at all."""
    accounts: dict[str, float] = field(default_factory=dict)
    """Account label → its share, on the same terms as *machines*."""


@dataclass
class ReportRows:
    """A whole report as data: its rows in display order and its summary lines."""

    rows: list[Row]
    total: AggBucket
    """Summed over the rows shown, which is what the TOTAL line reports."""
    n_all: int
    """How many buckets there were before a limit cut any. Equals len(rows)
    for the reports that take no limit."""
    all_total: AggBucket
    """Summed over every bucket, limit or no limit. The project and session
    reports print an average across all of them under the top-N average, and
    the two differ by exactly this."""


@dataclass(frozen=True)
class Projection:
    cost: float
    cost_nok: float
    nok_estimated: bool


@dataclass(frozen=True)
class MonthProjection:
    """What the monthly report extrapolates for a month still running.

    Absent — the function returns None — when the newest month in the corpus is
    not the current one, which is the only case where the report shows no
    projection block at all.
    """

    days_elapsed: int
    days_in_month: int
    month_name: str
    window_days: int
    """Length of the trailing window, in days."""
    month_to_date: Projection | None
    """This month's spend per elapsed day, over a full month. None on the last
    day of the month, where there is nothing left to project onto."""
    trailing: Projection | None
    """The same, from the trailing window's daily average instead. None when
    that window spent nothing, and always None when month_to_date is."""


def _fold(buckets: dict[Any, AggBucket], keys) -> AggBucket:
    """Sum the named buckets into one, for a TOTAL or an AVERAGE line."""
    total = AggBucket()
    for key in keys:
        total += buckets[key]
    return total


# --- Rows over the wire ---
#
# The server aggregates and the CLI renders, so the rows cross a network in
# between. Both ends read these functions rather than each keeping a shape the
# other has to match.


def _agg_json(agg: AggBucket) -> dict:
    t = agg.tokens
    return {
        "tokens": [t.input, t.output, t.cache_create, t.cache_read],
        "cost": agg.cost,
        "cost_nok": agg.cost_nok,
        "nok_estimated": agg.nok_estimated,
        "models": agg.models,
        "count": agg.count,
    }


def _agg_from_json(payload: dict) -> AggBucket:
    tin, tout, tcc, tcr = payload["tokens"]
    return AggBucket(
        tokens=TokenCounts(input=tin, output=tout, cache_create=tcc, cache_read=tcr),
        cost=payload["cost"],
        cost_nok=payload.get("cost_nok", 0.0),
        nok_estimated=payload.get("nok_estimated", False),
        models=dict(payload.get("models", {})),
        count=payload.get("count", 0),
    )


def _row_json(row: Row) -> dict:
    return {
        "key": row.key,
        "agg": _agg_json(row.agg),
        "breakdown": [_row_json(sub) for sub in row.breakdown],
        "project": row.project,
        "last": row.last.isoformat() if row.last else None,
        "machines": row.machines,
        "accounts": row.accounts,
    }


def _row_from_json(payload: dict) -> Row:
    last = payload.get("last")
    return Row(
        key=payload["key"],
        agg=_agg_from_json(payload["agg"]),
        breakdown=[_row_from_json(sub) for sub in payload.get("breakdown", ())],
        project=payload.get("project"),
        last=datetime.fromisoformat(last) if last else None,
        machines=dict(payload.get("machines", {})),
        accounts=dict(payload.get("accounts", {})),
    )


def rows_to_json(report: ReportRows) -> dict:
    """A whole report as the object the server sends."""
    return {
        "rows": [_row_json(row) for row in report.rows],
        "total": _agg_json(report.total),
        "n_all": report.n_all,
        "all_total": _agg_json(report.all_total),
    }


def rows_from_json(payload: dict) -> ReportRows:
    """The inverse, for a client about to render what a server aggregated."""
    return ReportRows(
        rows=[_row_from_json(row) for row in payload["rows"]],
        total=_agg_from_json(payload["total"]),
        n_all=payload["n_all"],
        all_total=_agg_from_json(payload["all_total"]),
    )


def _projection_json(proj: Projection | None) -> dict | None:
    if proj is None:
        return None
    return {"cost": proj.cost, "cost_nok": proj.cost_nok, "nok_estimated": proj.nok_estimated}


def month_projection_to_json(proj: MonthProjection | None) -> dict | None:
    if proj is None:
        return None
    return {
        "days_elapsed": proj.days_elapsed,
        "days_in_month": proj.days_in_month,
        "month_name": proj.month_name,
        "window_days": proj.window_days,
        "month_to_date": _projection_json(proj.month_to_date),
        "trailing": _projection_json(proj.trailing),
    }


def month_projection_from_json(payload: dict | None) -> MonthProjection | None:
    if payload is None:
        return None
    def part(key: str) -> Projection | None:
        value = payload.get(key)
        return None if value is None else Projection(**value)

    return MonthProjection(
        days_elapsed=payload["days_elapsed"],
        days_in_month=payload["days_in_month"],
        month_name=payload["month_name"],
        window_days=payload["window_days"],
        month_to_date=part("month_to_date"),
        trailing=part("trailing"),
    )


def daily_rows(records: list[UsageRecord], nok: NokCtx, *, breakdown: bool = False) -> ReportRows:
    """Cost per local day, oldest first, optionally split by model within a day."""
    buckets = bucket_by(records, UsageRecord.day_key, nok)
    # Breakdown rows are the same aggregation one level finer, so they come from
    # the same helper keyed by (day, model) rather than a nested copy of it.
    model_buckets = bucket_by(records, lambda r: (r.day_key(), r.model), nok) if breakdown else {}
    models_per_day: dict[str, list[str]] = defaultdict(list)
    for day, model in model_buckets:
        models_per_day[day].append(model)

    rows = []
    for day in sorted(buckets):
        sub = sorted(
            (Row(key=m, agg=model_buckets[day, m]) for m in models_per_day[day]),
            key=lambda row: by_cost_desc(row.key, row.agg.cost),
        )
        rows.append(Row(key=day, agg=buckets[day], breakdown=sub))
    total = _fold(buckets, sorted(buckets))
    return ReportRows(rows=rows, total=total, n_all=len(buckets), all_total=total)


def monthly_rows(records: list[UsageRecord], nok: NokCtx) -> ReportRows:
    """Cost per local month, oldest first."""
    buckets = bucket_by(records, UsageRecord.month_key, nok)
    rows = [Row(key=month, agg=buckets[month]) for month in sorted(buckets)]
    total = _fold(buckets, sorted(buckets))
    return ReportRows(rows=rows, total=total, n_all=len(buckets), all_total=total)


def month_projection(
    records: list[UsageRecord], report: ReportRows, nok: NokCtx, now: datetime | None = None,
) -> MonthProjection | None:
    """Extrapolate the newest month, two ways, or None if it has already ended.

    Month-to-date divides this month's spend by the days elapsed. The trailing
    figure divides the last *window_days* of spend instead, which answers the
    same question for someone whose month started at a different pace than it
    is running at now.
    """
    if not report.rows:
        return None
    today = now or datetime.now().astimezone()
    latest = report.rows[-1]
    if latest.key != today.strftime("%Y-%m"):
        return None

    days_elapsed = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    window = TRAILING_WINDOW_DAYS
    if days_elapsed >= days_in_month:
        return MonthProjection(
            days_elapsed=days_elapsed, days_in_month=days_in_month,
            month_name=today.strftime("%B"), window_days=window,
            month_to_date=None, trailing=None,
        )

    month_to_date = Projection(
        cost=latest.agg.cost / days_elapsed * days_in_month,
        cost_nok=latest.agg.cost_nok / days_elapsed * days_in_month if nok.enabled else 0.0,
        nok_estimated=latest.agg.nok_estimated if nok.enabled else False,
    )

    # The window sits entirely on the live-record side of the rollup cutoff,
    # computed the same way: a rollup record stands for a whole day, so it is
    # never in `recent` and never has to be split across the boundary.
    # Widening the window past the rollup window would break that.
    end = today.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=window)
    recent = [r for r in records if start <= r.local() < end]
    agg = bucket_by(recent, lambda _r: "window", nok).get("window", AggBucket())
    trailing = None
    if agg.cost > 0:
        trailing = Projection(
            cost=agg.cost / window * days_in_month,
            cost_nok=(agg.cost_nok / window) * days_in_month if nok.enabled else 0.0,
            nok_estimated=agg.nok_estimated,
        )
    return MonthProjection(
        days_elapsed=days_elapsed, days_in_month=days_in_month,
        month_name=today.strftime("%B"), window_days=window,
        month_to_date=month_to_date, trailing=trailing,
    )


def _ranked_rows(
    buckets: dict[str, AggBucket], limit: int | None, make_row: Callable[[str], Row],
) -> ReportRows:
    """The costliest buckets first, cut to *limit*, with both totals kept.

    The average across every bucket is a line the project and session reports
    print under the average across the ones shown, so all_total is folded
    before the cut rather than reconstructed after it.
    """
    ranked = sorted(buckets, key=lambda k: buckets[k].cost, reverse=True)
    shown = ranked[:limit] if limit else ranked
    return ReportRows(
        rows=[make_row(key) for key in shown],
        total=_fold(buckets, shown),
        n_all=len(ranked),
        all_total=_fold(buckets, buckets),
    )


def project_rows(records: list[UsageRecord], nok: NokCtx, limit: int | None = 20) -> ReportRows:
    """Cost per project, priciest first."""
    buckets = bucket_by(records, lambda r: r.project, nok)
    return _ranked_rows(buckets, limit, lambda key: Row(key=key, agg=buckets[key]))


def account_rows(records: list[UsageRecord], nok: NokCtx) -> ReportRows:
    """Cost per Claude account, priciest first.

    No limit, unlike the project report: an account is a login, so a machine
    has two or three and there is nothing to cut off.
    """
    buckets = bucket_by(records, lambda r: r.account, nok)
    return _ranked_rows(buckets, None, lambda key: Row(key=key, agg=buckets[key]))


def session_rows(records: list[UsageRecord], nok: NokCtx, limit: int | None = 20) -> ReportRows:
    """Cost per session, priciest first, each with its project and last activity."""
    buckets = bucket_by(records, lambda r: r.session_id, nok)
    meta: dict[str, tuple[str, datetime]] = {}
    for rec in records:
        known = meta.get(rec.session_id)
        if known is None:
            meta[rec.session_id] = (rec.project, rec.timestamp)
        elif rec.timestamp > known[1]:
            meta[rec.session_id] = (known[0], rec.timestamp)
    return _ranked_rows(
        buckets, limit,
        lambda key: Row(key=key, agg=buckets[key], project=meta[key][0], last=meta[key][1]),
    )


def accounts_worth_showing(records: list[UsageRecord]) -> bool:
    """Whether the default run should append the per-account table.

    Two or more real accounts means the split says something no other table
    does. One says only what the TOTAL row of every other table already said,
    and none says less than that. UNKNOWN_ACCOUNT does not count towards the
    two: a single account beside its own pre-capture history is one account's
    costs drawn twice, and `ccreport adopt` exists to merge exactly that pair.

    Only about what an unasked-for run volunteers — `ccreport account` prints
    regardless, which is where someone goes to see the unknown split.
    """
    return len({r.account for r in records if r.account != UNKNOWN_ACCOUNT}) > 1
