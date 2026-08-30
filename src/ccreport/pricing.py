"""Shared Claude model pricing data, cost calculation, and cost aggregation.

Single source of truth for pricing tables, model aliases, cost formulas,
and cost aggregation. ccreport.py, statusline.py, usage_api.py, ccu.py and
cache_db.py all import from this module.

AUDIT: All calculations are documented in docs/calculation-reference.md.
When changing any calculation, caching, or data format here,
update that document to match.
"""

from __future__ import annotations

import json
import os  # already loaded via pathlib below, so naming it costs the render nothing
import sys
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, BinaryIO, NamedTuple, TypedDict

if TYPE_CHECKING:
    # Both are slow-path only: zoneinfo's package init resolves TZPATH through
    # sysconfig, and project_identity reads the repo-roots config. A fast render
    # imports this module and needs neither, so their call sites import them.
    from zoneinfo import ZoneInfo

    from ccreport.project_identity import Resolver


class UsageData(TypedDict, total=False):
    """Cross-module usage dict flowing through fetch→cache→compute→render.

    All fields are optional since different pipeline stages populate different
    subsets. Use usage.get("key") for safe access.
    """

    # Rate limits (from API fetch)
    session_percent: int
    session_reset: str
    week_percent: int
    week_reset: str
    sonnet_percent: int
    sonnet_reset: str

    # Extra usage (from API fetch)
    extra_percent: int
    extra_spent: float
    extra_limit: float

    # Metadata
    last_updated: str
    _meta: dict[str, Any]
    _stale: bool

    # Cost windows (from compute_costs)
    session_window_cost: float
    week_cost: float
    week_model_costs: dict[str, float]
    session_cost: float
    six_hour_cost: float
    six_hour_project_cost: float
    twelve_hour_cost: float
    twelve_hour_project_cost: float
    twenty_four_hour_cost: float
    twenty_four_hour_project_cost: float
    seven_day_cost: float
    seven_day_project_cost: float
    thirty_day_cost: float
    thirty_day_project_cost: float
    all_time_cost: float
    all_time_project_cost: float
    month_cost: float
    project_cost: float


def _local_tz() -> ZoneInfo:
    """Return the system's local timezone using full zone rules (DST-aware).

    Reading $TZ every call and caching on it: the resolution below is a symlink
    read and a zone-file parse, but $TZ is the one input that moves within a
    process — ccreport's rollup fingerprint covers the zone, and its tests
    change it to prove a zone change rebuilds.
    """
    import os

    return _tz_from_env(os.environ.get("TZ"))


@cache
def _tz_from_env(tz_env: str | None) -> ZoneInfo:
    """Resolve the zone $TZ names, or the system's own when it names none."""
    from zoneinfo import ZoneInfo

    try:
        if tz_env:
            return ZoneInfo(tz_env)
        # On macOS/Linux, read /etc/localtime symlink target
        localtime = Path("/etc/localtime")
        if localtime.is_symlink():
            target = str(localtime.resolve())
            # e.g. /usr/share/zoneinfo/Europe/Oslo → Europe/Oslo
            marker = "/zoneinfo/"
            idx = target.find(marker)
            if idx >= 0:
                return ZoneInfo(target[idx + len(marker):])
        # Fallback: current fixed offset (better than crashing)
        return ZoneInfo("UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")

# Source: https://github.com/BerriAI/litellm model_prices_and_context_window.json
LAST_CHECKED = "2026-08-12"

PRICING_HISTORY: list[dict[str, Any]] = [
    {
        "effective": "2025-01-01",
        "models": {
            "claude-opus-4-5-20251101": {
                "input": 5e-06, "output": 25e-06,
                "cache_create": 6.25e-06, "cache_read": 0.5e-06,
            },
            "claude-sonnet-4-20250514": {
                "input": 3e-06, "output": 15e-06,
                "cache_create": 3.75e-06, "cache_read": 0.3e-06,
                "input_200k": 6e-06, "output_200k": 22.5e-06,
                "cache_create_200k": 7.5e-06, "cache_read_200k": 0.6e-06,
            },
            "claude-haiku-4-5-20251001": {
                "input": 1e-06, "output": 5e-06,
                "cache_create": 1.25e-06, "cache_read": 0.1e-06,
            },
            "claude-sonnet-4-5-20250929": {
                "input": 3e-06, "output": 15e-06,
                "cache_create": 3.75e-06, "cache_read": 0.3e-06,
                "input_200k": 6e-06, "output_200k": 22.5e-06,
                "cache_create_200k": 7.5e-06, "cache_read_200k": 0.6e-06,
            },
        },
    },
    {
        "effective": "2026-02-05",
        "models": {
            "claude-opus-4-6": {
                "input": 5e-06, "output": 25e-06,
                "cache_create": 6.25e-06, "cache_read": 0.5e-06,
                "input_200k": 10e-06, "output_200k": 37.5e-06,
                "cache_create_200k": 12.5e-06, "cache_read_200k": 1e-06,
            },
        },
    },
    {
        "effective": "2026-02-17",
        "models": {
            "claude-sonnet-4-6": {
                "input": 3e-06, "output": 15e-06,
                "cache_create": 3.75e-06, "cache_read": 0.3e-06,
                "input_200k": 6e-06, "output_200k": 22.5e-06,
                "cache_create_200k": 7.5e-06, "cache_read_200k": 0.6e-06,
            },
        },
    },
    {
        # 2026-03-13 18:00 UTC (19:00 Oslo CET): Opus 4.6 and Sonnet 4.6
        # switched to flat pricing — no 200k tier premium.
        "effective": "2026-03-13T18",
        "models": {
            "claude-opus-4-6": {
                "input": 5e-06, "output": 25e-06,
                "cache_create": 6.25e-06, "cache_read": 0.5e-06,
            },
            "claude-sonnet-4-6": {
                "input": 3e-06, "output": 15e-06,
                "cache_create": 3.75e-06, "cache_read": 0.3e-06,
            },
        },
    },
    {
        # Opus 4.7 released 2026-04-16, same pricing as 4.6.
        "effective": "2026-04-16",
        "models": {
            "claude-opus-4-7": {
                "input": 5e-06, "output": 25e-06,
                "cache_create": 6.25e-06, "cache_read": 0.5e-06,
            },
        },
    },
    {
        # Opus 4.8 first seen 2026-05-29, same pricing as 4.7.
        "effective": "2026-05-29",
        "models": {
            "claude-opus-4-8": {
                "input": 5e-06, "output": 25e-06,
                "cache_create": 6.25e-06, "cache_read": 0.5e-06,
            },
        },
    },
    {
        # Sonnet 5, $2/$10 per MTok. Launched as introductory pricing through
        # 2026-08-31; Anthropic made the rate permanent before that expired.
        # Release date unknown; 2026-06-01 covers all current records.
        "effective": "2026-06-01",
        "models": {
            "claude-sonnet-5": {
                "input": 2e-06, "output": 10e-06,
                "cache_create": 2.5e-06, "cache_read": 0.2e-06,
            },
        },
    },
    {
        # Fable 5 first seen 2026-06-08. $10/$50 per MTok, cache write
        # 1.25x input og cache read 0.1x input (standard Anthropic-ratio).
        # Mythos 5 (Project Glasswing only) matches Fable 5 pricing exactly.
        "effective": "2026-06-08",
        "models": {
            "claude-fable-5": {
                "input": 10e-06, "output": 50e-06,
                "cache_create": 12.5e-06, "cache_read": 1e-06,
            },
            "claude-mythos-5": {
                "input": 10e-06, "output": 50e-06,
                "cache_create": 12.5e-06, "cache_read": 1e-06,
            },
        },
    },
    {
        # Opus 5 released 2026-07-24, same pricing as 4.8 ($5/$25 per MTok),
        # 1M context with no long-context premium. Fast mode is 2x base
        # ($10/$50) but rides the same model ID — records carry a separate
        # "speed" field we don't read, so fast-mode usage costs as standard.
        "effective": "2026-07-24",
        "models": {
            "claude-opus-5": {
                "input": 5e-06, "output": 25e-06,
                "cache_create": 6.25e-06, "cache_read": 0.5e-06,
            },
        },
    },
]

MODEL_ALIASES: dict[str, str] = {
    "claude-opus-4-5": "claude-opus-4-5-20251101",
    "claude-sonnet-4": "claude-sonnet-4-20250514",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
}

TIER_THRESHOLD = 200_000

# Families the per-model week split buckets by, matched as substrings so a
# model ID ("claude-fable-5"), an API display name ("Fable") and a statusline
# label all land on the same key.
MODEL_FAMILIES = ("haiku", "sonnet", "opus", "fable")
OTHER_FAMILY = "other"


def model_family(model: str) -> str:
    """Family bucket for *model*, or OTHER_FAMILY for anything unrecognized.

    Both sides of the per-model week lookup go through this: the accumulation
    keys on the record's model ID, the render on the quota's display name.
    Anything outside MODEL_FAMILIES — a local model, a name released after this
    list — shares one bucket, so a scoped quota named after such a model reads
    a total that may include others.
    """
    m = (model or "").lower()
    return next((f for f in MODEL_FAMILIES if f in m), OTHER_FAMILY)


# Local models (Ollama et al.) use "name:tag" identifiers and have no
# per-token cost. Claude model IDs never contain a colon — if a paid model
# ever adopts colon-form IDs, this heuristic must be revisited.
_FREE_PRICING: Mapping[str, float] = MappingProxyType({
    "input": 0.0, "output": 0.0, "cache_create": 0.0, "cache_read": 0.0,
})


@cache
def _parse_effective(date_str: str) -> datetime:
    """Parse an effective date string to a timezone-aware datetime.

    Accepts 'YYYY-MM-DD' (midnight UTC) or 'YYYY-MM-DDTHH' (hour-level UTC).

    Cached: the inputs are the handful of literals in PRICING_HISTORY, but
    find_pricing re-reads them per record — a full report otherwise spends
    seconds in strptime re-parsing the same dozen strings.
    """
    if "T" in date_str:
        return datetime.strptime(date_str, "%Y-%m-%dT%H").replace(tzinfo=UTC)
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)


_PERIOD_INDEX: tuple[tuple[datetime, ...], tuple[int, ...]] | None = None


def _period_index() -> tuple[tuple[datetime, ...], tuple[int, ...]]:
    """PRICING_HISTORY's effective dates in order, and the periods they belong to.

    Built on first pricing lookup rather than at import: a fast render imports
    this module and prices nothing.

    Sorted rather than assumed sorted — the table is maintained in chronological
    order, but bisecting one that an edit left out of order would price against
    the wrong period silently, where the old reverse walk only ever picked a
    later match.
    """
    global _PERIOD_INDEX
    if _PERIOD_INDEX is None:
        order = sorted(
            range(len(PRICING_HISTORY)),
            key=lambda i: _parse_effective(PRICING_HISTORY[i]["effective"]),
        )
        starts = tuple(_parse_effective(PRICING_HISTORY[i]["effective"]) for i in order)
        _PERIOD_INDEX = (starts, tuple(order))
    return _PERIOD_INDEX


def find_pricing(model: str, ts: datetime | None = None) -> Mapping[str, float] | None:
    """Find pricing for a model at a given timestamp.

    Bisects the effective dates for how many periods were in effect at *ts*,
    then resolves the model against that prefix — a lookup cached on the pair,
    since a corpus of records prices against a handful of models and as many
    periods as `PRICING_HISTORY` has, however many records it holds.
    """
    resolved = MODEL_ALIASES.get(model, model)

    if ":" in resolved:
        return _FREE_PRICING

    starts, _ = _period_index()
    periods = len(starts) if ts is None else bisect_right(starts, ts)
    return _pricing_in_effect(resolved, periods)


@cache
def _pricing_in_effect(resolved: str, periods: int) -> Mapping[str, float] | None:
    """Prices from the newest of the first *periods* pricing periods naming *resolved*.

    Matched exactly, then as a substring either way: the table keys some models
    by dated ID and some by family, and a record's model ID carries whichever
    suffix the API sent.
    """
    _, order = _period_index()
    for i in reversed(order[:periods]):
        models = PRICING_HISTORY[i]["models"]
        if resolved in models:
            return models[resolved]
        for key, prices in models.items():
            if key in resolved or resolved in key:
                return prices
    return None


PLAN_PRICES: list[dict[str, Any]] = [
    {
        "effective": "2025-01-01",
        "plans": {
            # The $20 plan under both its names: `default_claude_ai` is what the
            # oauthAccount blob reported on 2026-08-30 beside organizationType
            # `claude_pro`, where a declared timeline spells it
            # `default_claude_pro`. Named in this shipped period rather than a new
            # one because it priced to None at every date before, so nothing
            # already computed moves.
            "default_claude_pro": 20.0,
            "default_claude_ai": 20.0,
            "default_claude_max_5x": 100.0,
            "default_claude_max_20x": 200.0,
        },
        "seats": {
            # The Claude Code premium seat on Team, at the annual rate. A
            # standard seat is $20 and cannot run Claude Code at all, so a seat
            # that shows up in a session log is this one. Billed monthly it is
            # $125, which is a second entry here rather than a guess.
            "team_tier_1": 100.0,
        },
    },
]
"""What a subscription costs per month, in USD, by seat and by rate-limit tier.

The list price, tax excluded. That figure is the same wherever the payer lives
and a VAT rate is not, so a report that wants the money actually charged is the
place to apply one.

Periods like PRICING_HISTORY's, for the same reason: a price rise is a new
period, and pricing a month against the rate in force then keeps working after
it. A tier absent from every period has no price, which is not a price of zero
— a month priced against one has to read as unpriced.

Two maps because a seat and a plan are priced by different fields. An account
on a team seat still carries a rate-limit tier — `default_claude_max_5x` is a
seat's bucket as readily as a personal Max plan's — so pricing off that field
alone would charge a $100 seat at whatever the personal plan of the same limit
costs. `seats` is checked first for that reason.
"""

_PLAN_INDEX: tuple[tuple[datetime, ...], tuple[int, ...]] | None = None


def _plan_index() -> tuple[tuple[datetime, ...], tuple[int, ...]]:
    """PLAN_PRICES' effective dates in order, and the periods they belong to.

    `_period_index` for the plan table, sorted for the same reason: bisecting a
    table an edit left out of order prices against the wrong period silently.
    """
    global _PLAN_INDEX
    if _PLAN_INDEX is None:
        order = sorted(
            range(len(PLAN_PRICES)),
            key=lambda i: _parse_effective(PLAN_PRICES[i]["effective"]),
        )
        starts = tuple(_parse_effective(PLAN_PRICES[i]["effective"]) for i in order)
        _PLAN_INDEX = (starts, tuple(order))
    return _PLAN_INDEX


def _priced_at(kind: str, key: str | None, when: datetime | None) -> float | None:
    """The newest price *kind* names for *key* among the periods live at *when*."""
    if not key:
        return None
    starts, order = _plan_index()
    periods = len(starts) if when is None else bisect_right(starts, when)
    for i in reversed(order[:periods]):
        price = PLAN_PRICES[i].get(kind, {}).get(key)
        if price is not None:
            return price
    return None


def plan_price(tier: str | None, when: datetime | None = None) -> float | None:
    """The monthly list price of rate-limit tier *tier* at *when*, or None.

    None covers three cases a caller must not turn into zero: no tier declared
    for that stretch, a tier this table has never named, and a tier priced only
    by a period that had not started yet.
    """
    return _priced_at("plans", tier, when)


def seat_price(seat_tier: str | None, when: datetime | None = None) -> float | None:
    """The monthly list price of team seat *seat_tier* at *when*, or None."""
    return _priced_at("seats", seat_tier, when)


def tier_set_price(tiers: Mapping[str, str | None], when: datetime | None) -> float | None:
    """What one declared tier set cost per month: the seat, else the plan.

    The seat wins because it is the thing bought. A seat carries a rate-limit
    tier too, and it is often a personal plan's — pricing off that would charge
    a team seat whatever the personal plan of the same limit costs, which is a
    different number and not one anybody was billed.
    """
    return (
        seat_price(tiers.get("seat_tier"), when)
        or plan_price(tier_from(tiers), when)
    )


def tier_from(tiers: Mapping[str, str | None]) -> str | None:
    """The rate-limit bucket a tier set draws against: per-user, then org pool.

    `cache_db.effective_limit_tier` and `tier_timeline.effective_tier` spelled
    once more, because this module may import neither: the status line defers
    cache_db, and tier_timeline holds nothing but the stdlib on purpose.
    """
    return tiers.get("user_rate_limit_tier") or tiers.get("organization_rate_limit_tier")


def _next_month(when: datetime) -> datetime:
    """Midnight UTC on the first of the month after *when*'s."""
    year, month = when.year + (when.month == 12), (when.month % 12) + 1
    return datetime(year, month, 1, tzinfo=UTC)


def _month_shares(from_ts: float, to_ts: float) -> Iterator[float]:
    """One stretch's share of each calendar month it touches, as fractions.

    A subscription is billed by the calendar month, so a stretch is worth its
    length over the length of the month it falls in — not over the length of
    whatever span a caller happens to be asking about. Two weeks of a 31-day
    month is 14/31 of a monthly rate whether the question was about that month,
    that quarter or the whole year.

    A stretch crossing a boundary is split at it, so each part is measured
    against its own month. February and August are different lengths, and a
    fortnight is a larger share of one than of the other.
    """
    at = datetime.fromtimestamp(from_ts, tz=UTC)
    while at.timestamp() < to_ts:
        opens = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        closes = _next_month(opens)
        part = min(to_ts, closes.timestamp()) - at.timestamp()
        yield part / (closes.timestamp() - opens.timestamp())
        at = closes


def months_in_span(start: datetime, end: datetime) -> float:
    """How many calendar months *start*..*end* is worth, as a fraction.

    The same measure prorated_plan_cost bills against, so a figure it produced
    and the month count printed beside it cannot disagree about how long the
    span was. February and August contribute differently: a fortnight is half
    of one and under half of the other.
    """
    if end <= start:
        return 0.0
    return sum(_month_shares(start.timestamp(), end.timestamp()))


def prorated_plan_cost(
    stretches: Sequence[tuple[float, float, Mapping[str, str | None] | None]],
) -> float | None:
    """What the plans in *stretches* cost, prorated by calendar month.

    A plan changed mid-cycle is billed prorated, so pricing a month as whichever
    plan happened to be running on the 1st would misprice five of the seven
    months a year of receipts can hold. Each stretch pays its share of a monthly
    rate, measured against the month it falls in.

    Against the month and never against the caller's span, which is the one
    thing this must not do: a week priced over a week would charge a full
    month's rate for seven days, and half a year priced over half a year would
    charge one month's rate for six.

    None when nothing has a price. Not zero — an unpriced span and a free one
    are different answers, and a zero in a column beside real money reads as
    the second.

    Priced at each stretch's own start, so a stretch that begins after a price
    rise pays the new rate and one before it pays the old.
    """
    total, priced = 0.0, False
    for from_ts, to_ts, tiers in stretches:
        if tiers is None or to_ts <= from_ts:
            continue
        rate = tier_set_price(tiers, datetime.fromtimestamp(from_ts, tz=UTC))
        if rate is None:
            continue
        priced = True
        total += rate * sum(_month_shares(from_ts, to_ts))
    return total if priced else None


def tiered_cost(count: int, base_rate: float, tiered_rate: float | None) -> float:
    """Calculate cost for a single token type with per-type 200K tiering."""
    if count > TIER_THRESHOLD and tiered_rate is not None:
        return TIER_THRESHOLD * base_rate + (count - TIER_THRESHOLD) * tiered_rate
    return count * base_rate


def calc_cost(
    input_tokens: int,
    output_tokens: int,
    cache_create_tokens: int,
    cache_read_tokens: int,
    model: str,
    ts: datetime | None = None,
) -> float:
    """Calculate total cost for a set of token counts using model-specific pricing.

    The 200K tier is applied per token type independently: each type's count
    is checked against the threshold separately.
    """
    prices = find_pricing(model, ts)
    if not prices:
        if model and not model.startswith("<"):
            print(f"Warning: no pricing found for model '{model}'", file=sys.stderr)
        return 0.0
    return (
        tiered_cost(input_tokens, prices.get("input", 0.0), prices.get("input_200k"))
        + tiered_cost(output_tokens, prices.get("output", 0.0), prices.get("output_200k"))
        + tiered_cost(cache_create_tokens, prices.get("cache_create", 0.0), prices.get("cache_create_200k"))
        + tiered_cost(cache_read_tokens, prices.get("cache_read", 0.0), prices.get("cache_read_200k"))
    )


# ---------------------------------------------------------------------------
# Cost aggregation — shared JSONL parsing and windowed cost computation
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude"
SESSION_WINDOW_HOURS = 5
SESSION_WINDOW_S = SESSION_WINDOW_HOURS * 3600
WEEK_WINDOW_S = 7 * 86400


class RollingWindow(NamedTuple):
    """One rolling cost window: key prefix, span, and the label a report prints.

    The label is carried rather than derived: `timedelta(hours=24)` is a day as
    far as timedelta is concerned, but the segment says 24H.
    """

    name: str
    delta: timedelta
    label: str


# Rolling cost window definitions.
# Order matters — must be longest→shortest so the bucket cascade works
# with early-exit optimisation (if ts < longest, skip all).
ROLLING_WINDOWS: list[RollingWindow] = [
    RollingWindow("thirty_day", timedelta(days=30), "30D"),
    RollingWindow("seven_day", timedelta(days=7), "7D"),
    RollingWindow("twenty_four_hour", timedelta(hours=24), "24H"),
    RollingWindow("twelve_hour", timedelta(hours=12), "12H"),
    RollingWindow("six_hour", timedelta(hours=6), "6H"),
]

# Every bucket compute_costs() keys by window prefix, including the untimed one.
ROLLING_COST_NAMES: list[str] = [w.name for w in ROLLING_WINDOWS] + ["all_time"]


def rolling_cost_keys() -> list[str]:
    """The `<window>_cost` / `<window>_project_cost` pair for every window.

    The usage table columns, cache_db's field list and the statusline's merge
    list all read this instead of restating the six window names.
    """
    return [f"{name}{suffix}"
            for name in ROLLING_COST_NAMES
            for suffix in ("_cost", "_project_cost")]


def _rolling_thresholds(now_local: datetime) -> dict[str, float]:
    return {w.name: (now_local - w.delta).timestamp() for w in ROLLING_WINDOWS}


def _bucket_rolling_cost(
    cost: float,
    ts_epoch: float,
    thresholds: dict[str, float],
    totals: dict[str, float],
    proj_totals: dict[str, float] | None = None,
    is_project: bool = False,
) -> None:
    """Accumulate *cost* into the appropriate rolling window buckets.

    Mutates *totals* (and optionally *proj_totals*) in place.
    *thresholds* maps window name → epoch cutoff.
    """
    for w in ROLLING_WINDOWS:
        if ts_epoch >= thresholds[w.name]:
            totals[w.name] = totals.get(w.name, 0.0) + cost
            if is_project and proj_totals is not None:
                proj_totals[w.name] = proj_totals.get(w.name, 0.0) + cost


def _add_model_costs(totals: dict[str, float], part: Mapping[str, float]) -> None:
    """Fold one file's or one record set's per-family week costs into *totals*."""
    for fam, cost in part.items():
        totals[fam] = totals.get(fam, 0.0) + cost


def extract_assistant_fields(
    rec: dict,
) -> tuple[dict, dict, str, str, str | None, datetime] | None:
    """Extract and validate common fields from a parsed JSONL assistant record.

    Returns (message, usage, message_id, request_id, dedup_key, timestamp)
    or None if the record is invalid/incomplete.

    Shared by _iter_jsonl_costs (pricing) and parse_jsonl_file (scan).
    """
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not msg or not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not usage or not isinstance(usage, dict):
        return None

    message_id = msg.get("id", "")
    request_id = rec.get("requestId", "")
    dk: str | None = None
    if message_id and request_id:
        dk = f"{message_id}:{request_id}"

    ts_str = rec.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None

    return msg, usage, message_id, request_id, dk, ts


def dedup_identity(
    dk: str | None,
    mid: str,
    sid: str,
    ts_epoch: float,
    model: str,
    tokens: Sequence[int],
) -> str | None:
    """The key two records must share to be one and the same logged message.

    Normally the log's own message id plus request id (*dk*). A log missing
    either one leaves dk NULL — a small minority of cached rows — and
    read-time dedup used to wave those through, so a row stored twice counted
    twice everywhere it was read. The fallback stands in for dk: same session,
    same timestamp to the microsecond, same message id, same model, same four
    token counts.

    Deliberately narrow. Progressive chunks of one streaming message share a
    message id but differ in their token counts, so the fallback keeps them
    apart — collapsing those is dk's job and they do carry one. And None,
    meaning never a duplicate, for a record with neither a message id nor a
    single token: that leaves only the session and the timestamp to tell two
    rows apart, which is not enough to delete one on. No stored row is in that
    state today; the <synthetic> rows that come closest all carry a message id.
    """
    if dk:
        return dk
    if not mid and not any(tokens):
        return None
    # Leading separator: no "message_id:request_id" can collide with this.
    return "\0".join(("", mid or "", sid or "", f"{ts_epoch:.6f}",
                      model or "", *(str(t or 0) for t in tokens)))


def record_is_duplicate(rec: dict, seen_keys: set[str]) -> bool:
    """Whether cached record *rec* repeats one already counted.

    Marks it seen when it does not. Every reader of the cached records goes
    through this one copy, so none of them can quietly keep a dedup rule the
    others dropped.
    """
    key = dedup_identity(
        rec.get("dk"), rec.get("mid") or "", rec.get("sid") or "",
        rec.get("ts") or 0.0, rec.get("model") or "", rec.get("t") or (),
    )
    if key is None:
        return False
    if key in seen_keys:
        return True
    seen_keys.add(key)
    return False


def project_key(cwd: str) -> str:
    """Claude Code's projects-dir name for a working directory.

    One spelling of the encoding: it names directories under
    ~/.claude/projects/ and scopes cache_db's cost-summary keys, where a
    writer/reader disagreement would be a permanent silent cache miss rather
    than an error. ccreport's reverse decoding is a separate problem — it has
    to resolve the "-" ambiguity — and stays separate.
    """
    return cwd.replace("/", "-")


def project_path_prefixes(cwd: str, projects_dirs: list[Path]) -> list[str]:
    """Path prefixes that mark a cached file as belonging to *cwd*.

    Each ends in a separator, which is what keeps a sibling project out: the
    directory name is the cwd with its slashes swapped, so /tmp/proj-other
    lands as -tmp-proj-other and has -tmp-proj as a bare string prefix. The
    same trailing separator makes cache_db.prefix_range's half-open bounds
    select exactly this directory, so the SQL and the Python agree.
    """
    key = project_key(cwd)
    return [str(d / key) + "/" for d in projects_dirs]


def path_in_project(path: str, prefixes: Sequence[str]) -> bool:
    """Whether a cached file path sits under any of *prefixes*."""
    return any(path.startswith(p) for p in prefixes)


def _get_projects_dirs() -> list[Path]:
    """Return existing Claude project directories."""
    dirs: list[Path] = []
    for d in [CLAUDE_DIR / "projects", Path.home() / ".config" / "claude" / "projects"]:
        if d.is_dir():
            dirs.append(d)
    return dirs


def _project_dir_prefix(path: str, projects_dirs: list[Path]) -> str | None:
    """The `<projects dir>/<project>/` prefix *path* sits under, if any."""
    for d in projects_dirs:
        root = str(d) + "/"
        if path.startswith(root):
            head = path[len(root):].split("/", 1)[0]
            if head:
                return root + head + "/"
    return None


class ProjectScope(NamedTuple):
    """How a cost computation decides whether a record is the cwd's own.

    Two tests rather than one because they answer for different populations: a
    live file is placed by its path, while an orphaned record whose directory
    is gone has only the *name* its parse froze into it. *resolve* is carried
    so the orphan pass applies the same override rules that produced *name*.
    """

    name: str
    prefixes: list[str]
    resolve: Resolver | None = None


def project_scope(cwd: str, projects_dirs: list[Path]) -> ProjectScope:
    """Resolve the project *cwd* belongs to, following any `ccreport merge`.

    With no merge rules this is the cwd's own directory and the name a record
    logged from it carries — one table read, and nothing else. With rules, the
    name becomes the merge target and the prefixes grow to cover every other
    project directory that resolves to that same target: a merge has to mean
    "these are one project" for the statusline's cost windows as much as for
    the reports.

    The override table is read once per call; the per-file identities only on a
    miss in the per-cwd scope cache, since scanning them is a quarter of a
    render. A cached scope is only used while it still covers the cwd's own
    directories — a projects dir that appeared after the row was written is
    otherwise invisible until something invalidates the table. A cache that
    cannot be read degrades to the unmerged scope, which is what every render
    did before merges existed.
    """
    from ccreport.project_identity import build_override_fn, name_for_cwd

    own = project_path_prefixes(cwd, projects_dirs)
    unmerged = ProjectScope(name_for_cwd(cwd), own)
    try:
        resolve = build_override_fn()
        if resolve is None:
            return unmerged
        cached = _load_cached_scope(cwd)
        if cached is not None and set(own) <= set(cached[1]):
            return ProjectScope(cached[0], cached[1], resolve)
        identities = _file_identities()
    except Exception:  # noqa: BLE001
        return unmerged

    # The cwd's own cached logs carry the identity parse_jsonl_file derived for
    # them, including the git remote this module will not shell out for.
    signals = next(
        ((repo, rec_cwd or cwd, project or name_for_cwd(cwd))
         for path, repo, rec_cwd, project in identities
         if path_in_project(path, own)),
        (None, cwd, name_for_cwd(cwd)),
    )
    name = resolve(*signals)

    prefixes = list(own)
    seen = set(own)
    for path, repo, rec_cwd, project in identities:
        if path_in_project(path, own):
            continue
        if resolve(repo, rec_cwd, project) != name:
            continue
        merged = _project_dir_prefix(path, projects_dirs)
        if merged and merged not in seen:
            seen.add(merged)
            prefixes.append(merged)
    _cache_scope(cwd, name, prefixes)
    return ProjectScope(name, prefixes, resolve)


def _file_identities() -> list[tuple[str, str | None, str | None, str]]:
    """cache_db.load_ccreport_file_identities, imported at call time."""
    from ccreport.cache_db import load_ccreport_file_identities

    return load_ccreport_file_identities()


def _load_cached_scope(cwd: str) -> tuple[str, list[str]] | None:
    """cache_db.load_project_scope, imported at call time."""
    from ccreport.cache_db import load_project_scope

    return load_project_scope(cwd)


def _cache_scope(cwd: str, name: str, prefixes: list[str]) -> None:
    """Store the derived scope, best-effort.

    A render is a reader that happens to have computed something worth keeping;
    a DB busy on the write leaves it with the answer it already has.
    """
    try:
        from ccreport.cache_db import save_project_scope

        save_project_scope(cwd, name, prefixes)
    except Exception:  # noqa: BLE001
        pass


def _find_session_files(
    session_id: str,
    cwd: str,
    projects_dirs: list[Path] | None = None,
) -> set[str]:
    """Find JSONL files belonging to a session."""
    if projects_dirs is None:
        projects_dirs = _get_projects_dirs()
    key = project_key(cwd)
    files: set[str] = set()
    for d in projects_dirs:
        base = d / key
        main = base / f"{session_id}.jsonl"
        if main.exists():
            files.add(str(main))
        sub = base / session_id
        if sub.is_dir():
            for f in sub.rglob("*.jsonl"):
                files.add(str(f))
    return files


def _line_cost(
    line: bytes,
    seen_keys: set[str],
) -> tuple[float, datetime, str | None, str] | None:
    """(cost, timestamp, dedup_key, model) for one JSONL line, or None to skip it.

    Skipped: anything that is not a well-formed assistant record, and any record
    whose dedup_identity is already in *seen_keys* — which this adds to, so one
    logged message counts once however many files or appended chunks carry it.

    Only the log's own dedup key is returned: the fallback identity is a
    read-time device, and callers persist what they get as durable dedup keys.

    A logged `costUSD` is the cost, as it is for `aggregate.UsageRecord.cost`
    and for the cached-record readers here — pricing from tokens instead made a
    window disagree with the reports and with a warm cache.
    """
    if b'"assistant"' not in line:
        return None
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    fields = extract_assistant_fields(rec)
    if fields is None:
        return None
    msg, usage, mid, _rid, dk, ts = fields

    tokens = (
        usage.get("input_tokens") or 0,
        usage.get("output_tokens") or 0,
        usage.get("cache_creation_input_tokens") or 0,
        usage.get("cache_read_input_tokens") or 0,
    )
    model = msg.get("model") or ""
    key = dedup_identity(
        dk, mid, rec.get("sessionId") or "", ts.timestamp(), model, tokens,
    )
    if key is not None:
        if key in seen_keys:
            return None
        seen_keys.add(key)

    logged = rec.get("costUSD")
    if logged is not None:
        try:
            return float(logged), ts, dk, model
        except (TypeError, ValueError):
            pass
    return calc_cost(*tokens, model, ts), ts, dk, model


def _iter_jsonl_costs(
    path: str | Path,
    seen_keys: set[str],
) -> Iterator[tuple[float, datetime, str | None, str]]:
    """Yield (cost, timestamp, dedup_key, model) for each unique assistant record.

    Deduplicates via *seen_keys* (modified in-place), by dedup_identity — so a
    record whose log carried no message id or requestId is still matched
    against its twin, on tokens and timestamp.

    Read as bytes so this and the session scanner, which needs byte offsets to
    resume at, split lines identically.
    """
    try:
        with open(path, "rb") as f:
            for line in f:
                got = _line_cost(line, seen_keys)
                if got is not None:
                    yield got
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Session cost — incremental over appended bytes
# ---------------------------------------------------------------------------

# How much of the already-counted bytes is re-read to prove a file was appended
# to rather than rewritten in place. Enough to span the end of the last record
# counted, which is the part a rewrite changes first.
_TAIL_BYTES = 256

_SESSION_STATE_VERSION = 3


def _digest(data: bytes) -> str:
    """A short stable digest. Neither caller is a security boundary.

    hashlib is imported here rather than at module scope: it pulls in OpenSSL
    bindings, and a render that reads a cached session cost hashes nothing.
    """
    from hashlib import blake2b

    return blake2b(data, digest_size=8).hexdigest()


class _DigestKeys(set):
    """A dedup-key set holding an 8-byte digest of every key put into it.

    The session cache persists this set between renders — one entry per logged
    message — so it stores what is only ever compared for equality at its
    smallest: 16 hex characters against the ~60 bytes of a message-id/request-id
    pair, or rather more for a content fallback key. Iteration yields the
    digests, which is what the stored blob holds and what `load` takes back.
    """

    @classmethod
    def load(cls, digests: list[str]) -> _DigestKeys:
        keys = cls()
        set.update(keys, digests)
        return keys

    def add(self, key: str) -> None:
        super().add(_digest(key.encode()))

    def __contains__(self, key: object) -> bool:
        return super().__contains__(_digest(str(key).encode()))


class _FileCursor(NamedTuple):
    """How much of one session file the stored session cost already accounts for.

    *tail* digests the last _TAIL_BYTES before *offset*; *mtime_ns* and *size*
    are the cheap "nothing moved" test that avoids opening the file at all.
    """

    mtime_ns: int
    size: int
    offset: int
    tail: str


class _SessionCostState(NamedTuple):
    """A session's accumulated cost and everything needed to extend it."""

    files: dict[str, _FileCursor]
    cost: float
    keys: _DigestKeys
    families: set[str]


def _encode_session_state(state: _SessionCostState) -> str:
    """Serialise the cursors and dedup keys into the fingerprint column.

    The column is opaque TEXT to cache_db, so the state rides in it as one JSON
    blob rather than needing a schema of its own. The cost is not in here — it
    has a column already, and one home keeps the two from disagreeing.

    The keys go out in set order. Sorting them would make the blob reproducible
    and nothing reads it that way, at a per-render cost that grows with the
    session. The families are sorted because there are at most a handful.
    """
    return json.dumps(
        {
            "v": _SESSION_STATE_VERSION,
            "f": {p: list(c) for p, c in state.files.items()},
            "k": list(state.keys),
            "m": sorted(state.families),
        },
        separators=(",", ":"),
    )


def _decode_session_state(blob: str, cost: float) -> _SessionCostState | None:
    """Parse a stored fingerprint, or None for one this build cannot extend.

    None covers the pre-incremental md5 fingerprint as much as a truncated or
    future-version blob: the caller then reparses in full and stores the current
    format, so a migration costs one render.
    """
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != _SESSION_STATE_VERSION:
        return None
    try:
        files = {
            str(path): _FileCursor(int(c[0]), int(c[1]), int(c[2]), str(c[3]))
            for path, c in data["f"].items()
        }
        keys = _DigestKeys.load([str(k) for k in data["k"]])
        families = {str(m) for m in data["m"]}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    return _SessionCostState(files, cost, keys, families)


def _tail_digest(f: BinaryIO, offset: int) -> str:
    """Digest the bytes just before *offset*, empty for the start of a file."""
    if not offset:
        return ""
    start = max(0, offset - _TAIL_BYTES)
    f.seek(start)
    return _digest(f.read(offset - start))


def _scan_session_file(
    path: str,
    seen_keys: set[str],
    cursor: _FileCursor | None,
    families: set[str],
) -> tuple[float, int, str] | None:
    """Cost of the records after *cursor*, with the offset and tail it reached.

    Adds every scanned record's model family to *families* — mutated in place,
    and only reliable when the caller keeps it for as long as it keeps the
    cursor: a resume scans appended bytes alone, so the families of the records
    before the cursor are the stored set's to remember.

    None when the file cannot be read, or cannot be resumed: the bytes before
    the cursor's offset are re-digested first, so a log rewritten in place fails
    the check and has to be counted from the start again. That check covers the
    last record only — an edit further back in a file whose size never changed
    would be missed, which no Claude Code writer does.

    Only whole lines move the offset. A writer caught mid-append leaves a
    partial last line, and the next render sees it complete.
    """
    offset = cursor.offset if cursor else 0
    try:
        with open(path, "rb") as f:
            if offset:
                if _tail_digest(f, offset) != cursor.tail:  # type: ignore[union-attr]
                    return None
                f.seek(offset)
            total = 0.0
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                got = _line_cost(raw, seen_keys)
                if got is not None:
                    total += got[0]
                    families.add(model_family(got[3]))
            return total, offset, _tail_digest(f, offset)
    except OSError:
        return None


def _resume_session_cost(
    stats: dict[str, tuple[int, int]],
    state: _SessionCostState,
) -> _SessionCostState | None:
    """Extend *state* with what has been appended to the session since it was written.

    Returns *state* itself when no file moved. Returns None when the stored
    total cannot be extended — a counted file gone, truncated, or rewritten —
    because one file's share of a single accumulated number is not separable
    from it. A file that has appeared since is only added, so it needs none of
    that.
    """
    if set(state.files) - set(stats):
        return None

    files = dict(state.files)
    total = state.cost
    families = set(state.families)
    changed = False
    for path, (mtime_ns, size) in stats.items():
        cursor = files.get(path)
        if cursor is not None:
            if cursor.mtime_ns == mtime_ns and cursor.size == size:
                continue
            if size < cursor.offset:
                return None
        scanned = _scan_session_file(path, state.keys, cursor, families)
        if scanned is None:
            if cursor is not None:
                return None
            # A file that has never been counted and cannot be read now: leave
            # it out and let a later render pick it up whole.
            continue
        cost, offset, tail = scanned
        total += cost
        files[path] = _FileCursor(mtime_ns, size, offset, tail)
        changed = True

    return _SessionCostState(files, total, state.keys, families) if changed else state


def _reparse_session_cost(stats: dict[str, tuple[int, int]]) -> _SessionCostState:
    """Count every session file from its first byte, discarding any subtotal."""
    files: dict[str, _FileCursor] = {}
    keys = _DigestKeys()
    families: set[str] = set()
    total = 0.0
    for path, (mtime_ns, size) in stats.items():
        scanned = _scan_session_file(path, keys, None, families)
        if scanned is None:
            continue
        cost, offset, tail = scanned
        total += cost
        files[path] = _FileCursor(mtime_ns, size, offset, tail)
    return _SessionCostState(files, total, keys, families)


class SessionUsage(NamedTuple):
    """A session's cost and the model families its records were billed to.

    The families cover the whole session log, subagent records included, so a
    main model of one family and a Task subagent of another both land here.
    """

    cost: float
    families: frozenset[str]


def _purged_session_usage(session_id: str, cwd: str) -> SessionUsage:
    """Cost and model families of a session whose JSONL files are gone, from cached records.

    Scoped by project path: the loader's predicate is the session id alone, and
    two projects' sessions can share one.
    """
    try:
        from ccreport.cache_db import load_ccreport_records_for_session

        ccr_records_by_file = load_ccreport_records_for_session(session_id)
        project_prefixes = project_path_prefixes(cwd, _get_projects_dirs())
        total = 0.0
        families: set[str] = set()
        seen: set[str] = set()
        for fp, recs in ccr_records_by_file.items():
            if not path_in_project(fp, project_prefixes):
                continue
            for rec in recs:
                if record_is_duplicate(rec, seen):
                    continue
                families.add(model_family(rec.get("model", "")))
                cost = _rec_cost(rec)
                if cost:
                    total += cost
        return SessionUsage(total, frozenset(families))
    except Exception:  # noqa: BLE001
        return SessionUsage(0.0, frozenset())


def compute_session_cost(session_id: str, cwd: str) -> float:
    """Total cost for a single session — compute_session_usage's cost alone."""
    return compute_session_usage(session_id, cwd).cost


def compute_session_usage(session_id: str, cwd: str) -> SessionUsage:
    """Compute total cost and model families for a single session from its JSONL files.

    Incremental: the cache stores how far into each of the session's files the
    stored total has counted, so an appended log costs a parse of the appended
    bytes. Fingerprinting the whole file made every render of a live session
    reparse it from line 1, and a session's own growth then made that
    quadratic in its length.

    Falls back to a full reparse whenever the stored total can no longer be
    trusted as a subtotal, and to cached ccreport_records when the JSONL files
    have been purged.
    """
    if not session_id or not cwd:
        return SessionUsage(0.0, frozenset())

    files = _find_session_files(session_id, cwd)
    if not files:
        return _purged_session_usage(session_id, cwd)

    # Sorted, so the dedup order a full reparse applies is the order the
    # incremental passes built up in.
    stats: dict[str, tuple[int, int]] = {}
    for path in sorted(files):
        try:
            st = Path(path).stat()
        except OSError:
            continue
        stats[path] = (st.st_mtime_ns, st.st_size)

    if not stats:
        return SessionUsage(0.0, frozenset())

    from ccreport.cache_db import read_session_cost, write_session_cost

    stored = read_session_cost(session_id)
    state = _decode_session_state(*stored) if stored is not None else None
    updated = _resume_session_cost(stats, state) if state is not None else None
    if updated is None:
        updated = _reparse_session_cost(stats)
    elif state is not None and updated is state:
        return SessionUsage(state.cost, frozenset(state.families))

    write_session_cost(session_id, _encode_session_state(updated), updated.cost)
    return SessionUsage(updated.cost, frozenset(updated.families))


def _accumulate_orphaned_costs(
    ccr_records_by_file: dict[str, list[dict]],
    live_paths: set[str],
    seen_keys: set[str],
    thresholds: dict[str, float],
    totals: dict[str, float],
    proj_totals: dict[str, float],
    project_name: str,
    path_prefixes: list[str] | None = None,
    extra_thresholds: dict[str, float] | None = None,
    extra_totals: dict[str, float] | None = None,
    week_model_totals: dict[str, float] | None = None,
    resolve: Resolver | None = None,
) -> None:
    """Accumulate costs from orphaned ccreport records (deleted JSONL files).

    Mutates *totals*, *proj_totals*, *extra_totals* and *week_model_totals* in
    place. *extra_thresholds*/*extra_totals* handle non-rolling windows (week,
    month, session); *week_model_totals* splits the week window by model family.

    A record counts toward the project either by the directory it was cached
    under or by *project_name*, which is the cwd's project with any merge rule
    already applied — *resolve* puts the record through the same rules, so both
    sides of the comparison land on the merge target before it is made.

    The untimed bucket is left alone: the caller is handed only the records
    inside its widest window, and their all-time share is in the stored orphan
    totals (_orphan_alltime_totals), which cover every orphaned record rather
    than the recent ones. Adding it here too would count it twice.
    """
    from ccreport.project_identity import record_project

    week_ts = (extra_thresholds or {}).get("week")
    for fp, recs in ccr_records_by_file.items():
        if fp in live_paths:
            continue
        is_ours = path_prefixes is not None and path_in_project(fp, path_prefixes)
        for rec in recs:
            if record_is_duplicate(rec, seen_keys):
                continue
            cost = _rec_cost(rec)
            if not cost:
                continue
            ts_epoch = rec.get("ts", 0)
            # The path test is what places a record from a project-scoped read;
            # the name is the only handle left on one whose directory is gone.
            is_project = is_ours or bool(
                project_name and record_project(rec, resolve) == project_name
            )
            if ts_epoch:
                _bucket_rolling_cost(cost, ts_epoch, thresholds, totals,
                                     proj_totals, is_project)
                if extra_thresholds and extra_totals:
                    for key, thresh in extra_thresholds.items():
                        if ts_epoch >= thresh:
                            extra_totals[key] = extra_totals.get(key, 0.0) + cost
                if week_model_totals is not None and week_ts and ts_epoch >= week_ts:
                    fam = model_family(rec.get("model", ""))
                    week_model_totals[fam] = week_model_totals.get(fam, 0.0) + cost


def _orphan_alltime_fingerprint(orphan_paths: set[str]) -> str:
    """Digest of every input the stored orphan all-time rows froze an answer to.

    Any mismatch rebuilds them, so a part missing here is silently wrong
    numbers — the same contract as ccreport's rollup fingerprint, and the same
    two halves: what the DB holds (cache_db.orphan_alltime_stamp) and what
    prices it. pricing.py is hashed because the rows store a cost, and almost
    no cached record carries a stored cost at all — they are priced from their
    tokens on every read, so a price edit moves the total with no record change
    to notice.

    The orphan set itself is the third part: a file being purged moves its
    records from the live half of the corpus to this one, and moves them to
    the back of the dedup order with it.

    Not in it, deliberately: the override rules. The rows store the raw
    (project, cwd, repo) identity and _apply_orphan_alltime resolves it per
    call, so a `ccreport merge` re-groups the same totals without a rebuild.
    """
    # hashlib at call time, not import time: this runs at most once per render
    # and test_importing_pricing_touches_neither_zoneinfo_nor_the_repo_roots_config
    # holds the module's import cost to what every render actually needs.
    from hashlib import sha256

    from ccreport.cache_db import archive_stamp, orphan_alltime_stamp

    h = sha256()
    h.update(orphan_alltime_stamp(orphan_paths).encode())
    h.update(b"\n")
    # The archive is a second source of the same total, and one an orphan path
    # set cannot see: a file it folded away is still in ccreport_files with the
    # fingerprint it always had.
    h.update(archive_stamp().encode())
    h.update(b"\n")
    try:
        h.update(Path(__file__).read_bytes())
    except OSError:
        pass  # unreadable source is not a reason to serve a stale total
    for path in sorted(orphan_paths):
        h.update(path.encode())
        h.update(b"\0")
    return h.hexdigest()


def _build_orphan_alltime(
    orphan_paths: set[str], projects_dirs: list[Path],
) -> list[tuple[str, str, str, str, float]]:
    """Sum every orphaned record's cost into (dir_prefix, identity) buckets.

    The one full pass over the orphaned corpus, run only when the fingerprint
    says the last one no longer describes it. Deliberately self-contained: it
    dedups the orphaned records against each other and against nothing else,
    where the live pass that precedes it in compute_costs would have had the
    first claim on a shared key. Nothing on a real machine is in that state —
    a message id belongs to one session log — and a stored total cannot depend
    on which files happened to be on disk the day it was built.
    """
    from ccreport.cache_db import load_ccreport_archive, load_ccreport_records_for_paths

    buckets: dict[tuple[str, str, str, str], float] = {}
    seen_keys: set[str] = set()
    for fp, recs in load_ccreport_records_for_paths(orphan_paths).items():
        prefix = _project_dir_prefix(fp, projects_dirs) or ""
        for rec in recs:
            if record_is_duplicate(rec, seen_keys):
                continue
            cost = _rec_cost(rec)
            if not cost:
                continue
            key = (prefix, rec.get("project") or "",
                   rec.get("cwd") or "", rec.get("repo") or "")
            buckets[key] = buckets.get(key, 0.0) + cost
    # The archive holds the same kind of history one fold coarser: purged files
    # whose records are gone. Its rows are already deduped and already priced,
    # and they carry the bucket key outright, so they are added rather than
    # walked. Without this an archive run reads as a machine that never spent.
    for row in load_ccreport_archive():
        _day, _oslo, _sid, project, _model, cwd, repo, prefix = row[:8]
        cost = row[14]
        if not cost:
            continue
        key = (prefix, project, cwd, repo)
        buckets[key] = buckets.get(key, 0.0) + cost
    return [(*key, cost) for key, cost in buckets.items()]


def _orphan_alltime_totals(
    orphan_paths: set[str], projects_dirs: list[Path],
) -> list[tuple[str, str, str, str, float]]:
    """The orphan all-time buckets, from the cache when it still applies.

    Best-effort throughout: every failure returns the empty aggregate, which
    is what an unreadable record cache already degraded compute_costs to
    before any of this existed — an all_time total missing its purged history
    rather than a render that raises.
    """
    from ccreport.cache_db import load_orphan_alltime, save_orphan_alltime

    try:
        fingerprint = _orphan_alltime_fingerprint(orphan_paths)
        rows = load_orphan_alltime(fingerprint)
        if rows:
            return rows
        rows = _build_orphan_alltime(orphan_paths, projects_dirs)
    except Exception:  # noqa: BLE001
        return []
    try:
        save_orphan_alltime(rows, fingerprint)
    except Exception:  # noqa: BLE001
        pass  # a busy DB costs the next render the same rebuild, not the total
    return rows


def _apply_orphan_alltime(
    rows: list[tuple[str, str, str, str, float]],
    totals: dict[str, float],
    proj_totals: dict[str, float] | None,
    project_name: str,
    path_prefixes: list[str] | None,
    resolve: Resolver | None,
) -> None:
    """Fold the stored orphan buckets into the untimed totals.

    The two project tests _accumulate_orphaned_costs runs per record, run here
    per bucket: the directory prefix a bucket was summed under is exactly what
    path_in_project compares, and the identity is what record_project resolves.
    """
    from ccreport.project_identity import record_project

    prefixes = path_prefixes or []
    for dir_prefix, project, cwd, repo, cost in rows:
        totals["all_time"] = totals.get("all_time", 0.0) + cost
        if proj_totals is None:
            continue
        rec = {"project": project, "cwd": cwd or None, "repo": repo or None}
        is_project = (bool(dir_prefix) and dir_prefix in prefixes) or bool(
            project_name and record_project(rec, resolve) == project_name
        )
        if is_project:
            proj_totals["all_time"] = proj_totals.get("all_time", 0.0) + cost


def pace_days() -> int:
    """The pace window in days from CLAUDE_CODE_PACE_DAYS, default 7.

    Lives here rather than in either reader because the status line's pace
    segment and `ccu`'s pace line answer to the same variable, and a user who
    sets it to 5 is entitled to have both of them mean it. Out-of-range and
    unparseable both fall back to 7 — a pace of 0 would divide by zero and a
    pace above 7 is slower than the window it measures.
    """
    try:
        d = int(os.environ.get("CLAUDE_CODE_PACE_DAYS", "7"))
    except ValueError:
        return 7
    return d if 1 <= d <= 7 else 7


def window_start_epoch(
    reset_iso: str | None, window_seconds: int, now: float,
) -> float | None:
    """Epoch start of the rate-limit window that resets at *reset_iso*.

    A reset time already in the past IS the start of the window we are in — it
    just rolled over. A reset in the future means we are inside the window, so
    its length comes off. None when the string is missing or unparseable.

    Naive strings count as local time: Claude Code's stdin rate limits arrive as
    epoch seconds and reach us as naive local ISO, while the usage API sends
    offset-aware strings.
    """
    if not reset_iso:
        return None
    try:
        epoch = datetime.fromisoformat(str(reset_iso)).timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return None
    return epoch if epoch <= now else epoch - window_seconds


def quota_rebase_epoch(window_start: float | None, now: float) -> float | None:
    """When a plan change restarted the quota that opened at *window_start*.

    A change of rate-limit tier rebases the percentage without moving the reset
    time, so from that instant the reading counts a different allowance. None
    where the account change log records no change inside the span, which is
    every window that ran on one plan.

    This opens the cache, so a caller on the status line's render path takes the
    instant from its fetch rather than calling here per frame.
    """
    if window_start is None:
        return None
    from ccreport.accounts import AccountTimeline
    from ccreport.cache_db import load_account_events

    return AccountTimeline(load_account_events()).rebase_within(window_start, now)


def _parse_window_starts(
    session_reset_iso: str | None,
    week_reset_iso: str | None,
) -> tuple[datetime | None, datetime]:
    """Derive rate-limit window start times from reset ISO strings.

    Returns (session_window_start, week_window_start).
    session_window_start is None if the reset time is unavailable.
    week_window_start falls back to Monday 00:00 local time.

    A plan change inside the week window moves its start to the change: the
    percentage restarted there, so the dollars beside it have to value the same
    span. That also moves week_key, which re-scans the per-file cost entries
    the previous bound had cached.
    """
    tz = _local_tz()
    now = datetime.now(tz=tz)

    def as_local(epoch: float | None) -> datetime | None:
        return datetime.fromtimestamp(epoch, tz=tz) if epoch is not None else None

    session_window_start = as_local(
        window_start_epoch(session_reset_iso, SESSION_WINDOW_S, now.timestamp()),
    )
    week_window_start = as_local(
        window_start_epoch(week_reset_iso, WEEK_WINDOW_S, now.timestamp()),
    ) or (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    try:
        rebase = quota_rebase_epoch(week_window_start.timestamp(), now.timestamp())
    except Exception:  # noqa: BLE001 — an unreadable log costs the cut, never the costs
        rebase = None
    if rebase is not None:
        week_window_start = as_local(rebase) or week_window_start

    return session_window_start, week_window_start


def _rec_cost_from_tokens(rec: dict) -> float:
    """Cost of a ccreport record priced from its own tokens, stored cost ignored.

    What _rec_cost falls back to for a record whose log gave no `costUSD`, which
    is almost all of them.
    """
    t = rec.get("t")
    if not t or len(t) < 4:
        return 0.0
    ts_epoch = rec.get("ts", 0)
    ts_dt = None
    if ts_epoch:
        try:
            ts_dt = datetime.fromtimestamp(ts_epoch, tz=UTC)
        except (ValueError, OSError):
            pass
    return calc_cost(t[0], t[1], t[2], t[3], rec.get("model", ""), ts_dt)


def _rec_cost(rec: dict) -> float:
    """Compute cost for a ccreport record dict, recomputing from tokens if needed."""
    cost = rec.get("cost")
    if cost is not None and cost != 0:
        return cost
    return _rec_cost_from_tokens(rec)


class _FileContext(NamedTuple):
    """Immutable per-file facts used by compute_costs helpers."""

    key: str
    is_session_file: bool
    is_project_file: bool
    in_session_window: bool
    in_rolling_window: bool
    file_unchanged: bool
    ccr_fresh: bool


class _CacheResult(NamedTuple):
    """Return type for _try_cached_file — contributions from a cache hit."""

    week: float
    month: float
    session: float
    session_window: float
    week_model: dict[str, float]
    entry: dict[str, Any]


class _ScanResult(NamedTuple):
    """Return type for _scan_jsonl_file — raw totals from parsing JSONL."""

    week_cost: float
    month_cost: float
    session_cost: float
    sw_cost: float
    all_time_cost: float
    week_model_costs: dict[str, float]
    file_rolling: dict[str, float]
    dedup_keys: list[str]


def _try_cached_file(
    ctx: _FileContext,
    cached_entry: dict[str, Any] | None,
    ccr_records_by_file: dict[str, list[dict]],
    seen_keys: set[str],
    thresholds: dict[str, float],
    sw_ts: float | None,
    td_ts: float,
    rolling_totals: dict[str, float],
    rolling_proj: dict[str, float],
) -> _CacheResult | None:
    """Try to handle a file entirely from caches (branches 1-3).

    Returns a _CacheResult if the file was fully handled, None if a JSONL
    parse is needed. Mutates *seen_keys*, *rolling_totals*, *rolling_proj*.
    """
    if not ctx.file_unchanged or cached_entry is None:
        return None

    # Branches 1+2: unchanged, outside rolling/session windows
    if not ctx.in_session_window and not ctx.in_rolling_window:
        c = cached_entry.get("all_time_cost", 0.0)
        rolling_totals["all_time"] = rolling_totals.get("all_time", 0.0) + c
        if ctx.is_project_file:
            rolling_proj["all_time"] = rolling_proj.get("all_time", 0.0) + c
        seen_keys.update(cached_entry.get("dedup_keys", []))
        return _CacheResult(
            week=cached_entry.get("week_cost", 0.0),
            month=cached_entry.get("month_cost", 0.0),
            session=cached_entry.get("session_cost", 0.0) if ctx.is_session_file else 0.0,
            session_window=0.0,
            week_model=cached_entry.get("week_model_costs", {}),
            entry=cached_entry,
        )

    # Branch 3: unchanged + ccreport records available for rolling costs
    if not ctx.ccr_fresh:
        return None

    c = cached_entry.get("all_time_cost", 0.0)
    rolling_totals["all_time"] = rolling_totals.get("all_time", 0.0) + c
    if ctx.is_project_file:
        rolling_proj["all_time"] = rolling_proj.get("all_time", 0.0) + c
    session = cached_entry.get("session_cost", 0.0) if ctx.is_session_file else 0.0
    sw = 0.0

    for rec in ccr_records_by_file.get(ctx.key, []):
        if record_is_duplicate(rec, seen_keys):
            continue
        ts_e = rec.get("ts", 0)
        if not ts_e or ts_e < td_ts:
            if not (ctx.in_session_window and sw_ts and ts_e >= sw_ts):
                continue
        cost = _rec_cost(rec)
        if not cost:
            continue
        _bucket_rolling_cost(cost, ts_e, thresholds,
                             rolling_totals, rolling_proj, ctx.is_project_file)
        if sw_ts and ts_e >= sw_ts:
            sw += cost

    # Merge dedup keys from file_costs cache not seen via ccreport
    seen_keys.update(cached_entry.get("dedup_keys", []))
    return _CacheResult(
        week=cached_entry.get("week_cost", 0.0),
        month=cached_entry.get("month_cost", 0.0),
        session=session,
        session_window=sw,
        week_model=cached_entry.get("week_model_costs", {}),
        entry=cached_entry,
    )


def _scan_jsonl_file(
    jsonl_path: Path,
    is_session_file: bool,
    session_window_start: datetime | None,
    week_window_start: datetime,
    month_window_start: datetime,
    thresholds: dict[str, float],
    seen_keys: set[str],
) -> _ScanResult:
    """Parse a JSONL file and compute cost totals for all windows.

    Mutates *seen_keys* in place (via _iter_jsonl_costs).
    """
    w_cost = 0.0
    s_cost = 0.0
    sw_cost = 0.0
    m_cost = 0.0
    a_cost = 0.0
    w_by_model: dict[str, float] = {}
    file_rolling: dict[str, float] = {}
    file_dedup_keys: list[str] = []

    for cost, ts, dk, model in _iter_jsonl_costs(jsonl_path, seen_keys):
        if dk:
            file_dedup_keys.append(dk)
        a_cost += cost
        if ts >= month_window_start:
            m_cost += cost
        if ts >= week_window_start:
            w_cost += cost
            fam = model_family(model)
            w_by_model[fam] = w_by_model.get(fam, 0.0) + cost
        _bucket_rolling_cost(cost, ts.timestamp(), thresholds, file_rolling)
        if is_session_file:
            s_cost += cost
        if session_window_start and ts >= session_window_start:
            sw_cost += cost

    return _ScanResult(
        week_cost=w_cost,
        month_cost=m_cost,
        session_cost=s_cost,
        sw_cost=sw_cost,
        all_time_cost=a_cost,
        week_model_costs=w_by_model,
        file_rolling=file_rolling,
        dedup_keys=file_dedup_keys,
    )


def compute_costs(
    session_id: str | None = None,
    cwd: str | None = None,
    session_reset_iso: str | None = None,
    week_reset_iso: str | None = None,
) -> dict[str, Any]:
    """Compute per-chat, session-window, week-window, and rolling costs.

    Uses ccreport_records cache when available (fast path, ~0.7s) with
    JSONL fallback for uncached files. Falls back to full JSONL scan
    if ccreport cache is empty.

    Cost buckets:
      session_cost          – total cost for the target chat (all time)
      session_window_cost   – cost across ALL chats within the current
                              rate-limit session window (~5 h)
      week_cost             – cost across ALL chats within the current
                              rate-limit week window
      week_model_costs      – that week total split by model family, which is
                              what a per-model weekly quota is spent against
      month_cost            – cost since first of current calendar month
      six_hour_cost         – rolling 6-hour cost
      twelve_hour_cost      – rolling 12-hour cost
      twenty_four_hour_cost – rolling 24-hour cost
      seven_day_cost        – rolling 7-day cost
      thirty_day_cost       – rolling 30-day cost
      all_time_cost         – all records, no time filter
      <name>_project_cost   – each of the six rolling buckets again, scoped to
                              the cwd's project and whatever a `ccreport merge`
                              grouped into it: six_hour_project_cost through
                              all_time_project_cost

    session_cost, week_cost, and month_cost use per-file caching (mtime/size).
    session_window_cost and rolling costs are computed fresh.

    session_window_cost is absent from the dict, not zeroed, when no session
    reset is known: callers merge this over existing data, and a 0.0 there is
    indistinguishable from "not computed" — it would overwrite a real total
    with an empty window.

    Writes as well as returns: the result goes through
    write_cost_summary(result, cwd), which is the row the status line reads.

    Every window above is bounded, so the cached records this reads are too:
    one window's worth, not the whole table. all_time is the exception it is
    worth reading the rest of this function for — it has no cutoff to be
    bounded by, and its two halves are pre-summed elsewhere instead. Live
    files' shares sit in file_costs, keyed by (mtime_ns, size); purged files'
    in ccreport_orphan_costs, keyed by a fingerprint over the orphan set.
    """
    from ccreport.cache_db import (
        bulk_save_file_costs,
        load_ccreport_file_meta,
        load_ccreport_records_since,
        load_cost_cache,
    )

    projects_dirs = _get_projects_dirs()

    session_window_start, week_window_start = _parse_window_starts(
        session_reset_iso, week_reset_iso,
    )
    week_key = week_window_start.strftime("%Y-%m-%dT%H")

    now_local = datetime.now(tz=_local_tz())
    month_window_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_key = month_window_start.strftime("%Y-%m")

    thresholds = _rolling_thresholds(now_local)

    file_cache = load_cost_cache(week_key, month_key)

    session_files: set[str] = set()
    if session_id and cwd:
        session_files = _find_session_files(session_id, cwd, projects_dirs)

    # Which files and which orphaned records belong to the current cwd's
    # project — merge rules included, so the report and the statusline group
    # the same way.
    scope = ProjectScope("", [])
    if cwd:
        scope = project_scope(cwd, projects_dirs)

    week_total = 0.0
    session_total = 0.0
    sw_total = 0.0
    month_total = 0.0
    week_model_totals: dict[str, float] = {}
    rolling_totals: dict[str, float] = {}
    rolling_proj: dict[str, float] = {}
    seen_keys: set[str] = set()
    new_entries: dict[str, Any] = {}
    # Paths whose entry was rebuilt by a scan. Every other entry in
    # new_entries is the cached dict verbatim, so the save can leave those
    # rows — and their dedup keys — untouched.
    changed_files: set[str] = set()

    sw_ts = session_window_start.timestamp() if session_window_start else None
    mw_ts = month_window_start.timestamp()
    ww_ts = week_window_start.timestamp()
    td_ts = thresholds["thirty_day"]

    # The start of the widest window any bucket below is filled from. Every
    # cached record older than this reaches exactly one total, all_time, and
    # that one is answered per file (file_costs.all_time_cost) or per orphan
    # bucket (ccreport_orphan_costs) — so reading those rows back deserializes
    # most of the table to skip it. Not simply thirty_day: the month window is
    # the wider of the two for the last day of a 31-day month, since it starts
    # at midnight and the rolling one trails `now` by the time of day.
    record_cutoff = min(td_ts, mw_ts, ww_ts, sw_ts if sw_ts is not None else td_ts)

    # Both halves of the ccreport cache, each read at its own grain: the
    # fingerprints for every cached file, the records for one window's worth.
    ccr_file_meta = load_ccreport_file_meta()
    ccr_records_by_file = load_ccreport_records_since(record_cutoff)

    # Every JSONL still on disk, across all projects: this function owes a
    # global total as well as the project's, so its walk is the whole corpus.
    corpus_live_paths: set[str] = set()

    for projects_dir in projects_dirs:
        for jsonl_path in sorted(projects_dir.rglob("*.jsonl")):
            key = str(jsonl_path)
            corpus_live_paths.add(key)
            try:
                st = jsonl_path.stat()
            except OSError:
                continue

            cached_entry = file_cache.get(key)
            ccr_meta = ccr_file_meta.get(key)

            ctx = _FileContext(
                key=key,
                is_session_file=key in session_files,
                is_project_file=path_in_project(key, scope.prefixes),
                in_session_window=sw_ts is not None and st.st_mtime >= sw_ts,
                in_rolling_window=st.st_mtime >= td_ts,
                file_unchanged=bool(
                    cached_entry
                    and cached_entry.get("mtime_ns") == st.st_mtime_ns
                    and cached_entry.get("size") == st.st_size
                ),
                ccr_fresh=bool(
                    ccr_meta is not None
                    and ccr_meta[0] == st.st_mtime_ns
                    and ccr_meta[1] == st.st_size
                ),
            )

            hit = _try_cached_file(
                ctx, cached_entry, ccr_records_by_file, seen_keys,
                thresholds, sw_ts, td_ts,
                rolling_totals, rolling_proj,
            )
            if hit is not None:
                week_total += hit.week
                month_total += hit.month
                session_total += hit.session
                sw_total += hit.session_window
                _add_model_costs(week_model_totals, hit.week_model)
                new_entries[key] = hit.entry
                continue

            scan = _scan_jsonl_file(
                jsonl_path, ctx.is_session_file, session_window_start,
                week_window_start, month_window_start, thresholds, seen_keys,
            )

            if ctx.file_unchanged:
                assert cached_entry is not None  # noqa: S101 - file_unchanged implies cache hit
                week_total += cached_entry.get("week_cost", 0.0)
                month_total += cached_entry.get("month_cost", 0.0)
                _add_model_costs(
                    week_model_totals, cached_entry.get("week_model_costs", {}))
                c = cached_entry.get("all_time_cost", 0.0)
                rolling_totals["all_time"] = rolling_totals.get("all_time", 0.0) + c
                if ctx.is_project_file:
                    rolling_proj["all_time"] = rolling_proj.get("all_time", 0.0) + c
                if ctx.is_session_file:
                    session_total += cached_entry.get("session_cost", 0.0)
                new_entries[key] = cached_entry
            else:
                entry: dict[str, Any] = {
                    "mtime_ns": st.st_mtime_ns,
                    "size": st.st_size,
                    "week_cost": round(scan.week_cost, 6),
                    "month_cost": round(scan.month_cost, 6),
                    "all_time_cost": round(scan.all_time_cost, 6),
                    "week_model_costs": {
                        fam: round(c, 6) for fam, c in scan.week_model_costs.items()
                    },
                    "dedup_keys": scan.dedup_keys,
                }
                if ctx.is_session_file:
                    entry["session_cost"] = round(scan.session_cost, 6)
                new_entries[key] = entry
                week_total += scan.week_cost
                month_total += scan.month_cost
                _add_model_costs(week_model_totals, scan.week_model_costs)
                rolling_totals["all_time"] = rolling_totals.get("all_time", 0.0) + scan.all_time_cost
                if ctx.is_project_file:
                    rolling_proj["all_time"] = rolling_proj.get("all_time", 0.0) + scan.all_time_cost
                session_total += scan.session_cost
                changed_files.add(key)

            sw_total += scan.sw_cost
            for w in ROLLING_WINDOWS:
                fc = scan.file_rolling.get(w.name, 0.0)
                rolling_totals[w.name] = rolling_totals.get(w.name, 0.0) + fc
                if ctx.is_project_file:
                    rolling_proj[w.name] = rolling_proj.get(w.name, 0.0) + fc

    if changed_files or set(new_entries) != set(file_cache):
        try:
            bulk_save_file_costs(
                new_entries, week_key, month_key, changed=changed_files,
                # Dedup keys only change a total inside a window that still
                # counts the file, so the widest of the two bounded windows is
                # where they stop being worth storing.
                dedup_cutoff_ns=int(min(mw_ts, td_ts) * 1_000_000_000),
            )
        except OSError:
            pass

    # Include orphaned records (from deleted JSONL files cached by ccreport).
    # In two parts, because the two halves of an orphaned record's contribution
    # are bounded differently: the windowed buckets come from the records this
    # call actually read, and all_time from a stored sum over every orphaned
    # record there has ever been.
    extra_thresholds = {"month": mw_ts, "week": ww_ts}
    if sw_ts:
        extra_thresholds["session_window"] = sw_ts
    extra_totals: dict[str, float] = {}
    _accumulate_orphaned_costs(
        ccr_records_by_file, corpus_live_paths, seen_keys, thresholds,
        rolling_totals, rolling_proj, scope.name,
        path_prefixes=scope.prefixes,
        extra_thresholds=extra_thresholds, extra_totals=extra_totals,
        week_model_totals=week_model_totals,
        resolve=scope.resolve,
    )
    _apply_orphan_alltime(
        _orphan_alltime_totals(set(ccr_file_meta) - corpus_live_paths, projects_dirs),
        rolling_totals, rolling_proj, scope.name, scope.prefixes, scope.resolve,
    )
    month_total += extra_totals.get("month", 0.0)
    week_total += extra_totals.get("week", 0.0)
    sw_total += extra_totals.get("session_window", 0.0)

    result: dict[str, Any] = {
        "session_cost": round(session_total, 4),
        "week_cost": round(week_total, 4),
        "month_cost": round(month_total, 4),
        "week_model_costs": {
            fam: round(c, 4) for fam, c in week_model_totals.items()
        },
    }
    if session_window_start is not None:
        # Omitted, not zeroed, without a reset time: callers merge this dict over
        # existing data, and a 0.0 here is indistinguishable from "not computed"
        # — it would overwrite a real total with an empty window.
        result["session_window_cost"] = round(sw_total, 4)
    for name in ROLLING_COST_NAMES:
        result[f"{name}_cost"] = round(rolling_totals.get(name, 0.0), 4)
        result[f"{name}_project_cost"] = round(rolling_proj.get(name, 0.0), 4)

    try:
        from ccreport.cache_db import write_cost_summary
        write_cost_summary(result, cwd=cwd)
    except Exception:  # noqa: BLE001
        pass

    return result
