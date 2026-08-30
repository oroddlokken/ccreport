"""Rate-limit window instances: what the samples say, and what they cost.

The samples the status line writes are a percentage and a reset time, so what a
window cost has to come from the record corpus covering the span it filled
over. Both halves live here rather than in ccreport.py, because the merged
server reports the same windows over records it holds itself and cannot import
a module that draws rich tables.

Nothing here renders. `ccreport.py` turns these into tables and
`server/limits.py` into pages; a formatted string in this module would be one
of them deciding for the other.

AUDIT: the maths is documented in docs/calculation-reference.md section 9.7.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ccreport import pricing

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ccreport.aggregate import UsageRecord

# aggregate is a type-checking import alone: exchange.py imports cache_db, and
# cache_db reads rl_window_key from here, so a runtime import of aggregate would
# close that ring. Nothing below needs more of a record than cost(), tokens,
# timestamp and model.

# Claude Code has been seen sending resets_at = 9999999999 on stdin (a year-2286
# placeholder), and rows written before this check carry it permanently. The
# day of slack over seven is for a span quoted generously, not for a placeholder.
#
# Both ends read it from here: statusline._rl_sample refuses to store one, and
# every report drops the ones already stored. A reader that tolerated what the
# writer rejects would render a window resetting in 2286.
RL_MAX_LOOKAHEAD_S = 8 * 86_400


def rl_window_key(resets_at: float) -> float:
    """*resets_at* rounded to the whole minute — one window instance's identity.

    The usage API returns a float that drifts by up to a second between fetches
    of the same window (observed: 80 scoped rows spanning 1786305599.03 to
    1786305600.95, all of one reset at 1786305600). Both ends of that table
    treat resets_at as the window's identity — the write gate to decide whether
    a reading belongs to the window it already stored, the reports to group
    samples into one fill curve — so that drift made every render look like a
    fresh window: the whole-percent gate never applied, and one week of scoped
    history became 80 single-sample instances.

    A minute because real resets land on one; anything finer is fetch latency.
    Rounded rather than truncated, else a reset at :00 splits across two buckets
    depending on which side of it the jitter fell.

    The writer normalizes before storing. The reader applies it again on read,
    because the rows written before this existed keep their jitter forever.
    """
    return round(resets_at / 60.0) * 60.0


def implausible_reset(sample: dict) -> bool:
    """Whether *sample*'s reset time is too far out to be a window.

    The writer refuses these now (statusline._rl_sample), but the sample table
    is permanent history and rows written before that check carry Claude Code's
    9999999999 placeholder. Reported as-is they are one window per placeholder,
    resetting in 2286, with a fill time in decades.
    """
    return sample["resets_at"] - sample["ts"] > RL_MAX_LOOKAHEAD_S


# A window the tables have never heard of is still reported, under its raw name
# and after these — the writer's list of windows lives in statusline._rl_samples,
# and a report over permanent history is the wrong place to lose a row or raise
# over one because the two lists drifted.
LIMIT_WINDOWS = ("session", "week", "sonnet", "scoped")

LIMIT_WINDOW_LABELS = {
    "session": "Session (5h)",
    "week": "Week (7d)",
    "sonnet": "Sonnet (7d)",
    "scoped": "Scoped model (7d)",
}

# How long each window runs, so its reset time says when it opened. pricing owns
# both spans; the three 7-day quotas differ in what they count, not in how long
# they run. A window type not listed here — one the writer added since — has no
# derivable start, so its note names the opening reading and no lag.
LIMIT_WINDOW_SPAN_S = {
    "session": float(pricing.SESSION_WINDOW_S),
    "week": float(pricing.WEEK_WINDOW_S),
    "sonnet": float(pricing.WEEK_WINDOW_S),
    "scoped": float(pricing.WEEK_WINDOW_S),
}

# Points a window may already carry when first sampled before the report calls
# it partial. Capture starts at a render, so a point or two of lag is ordinary;
# past this the peak counts a rise the spend columns never priced.
PARTIAL_OPENING_PP = 5.0

InstanceKey = tuple[str, str | None, float, int]
"""What identifies one fill curve in a report: WindowInstance.key's shape."""

# How far a reading must fall within one reset time before it reads as the quota
# being rebased rather than a rounding step: a plan change restarts the
# percentage against the new allowance without moving resets_at, so the samples
# either side of it measure different quotas. Every drop in the stored history is
# exactly one point, the step the write gate's whole-percent rule leaves room
# for, bar a ten-point fall to zero on the day a plan changed.
REBASE_DROP_PP = 5.0


@dataclass
class WindowInstance:
    """One rate-limit window's life, as the samples of it that were taken.

    A window instance is one 5-hour or 7-day span: the samples that share a
    resets_at are readings of the same quota filling up, which is what makes a
    peak and a fill time mean anything. *samples* are in ts order, as the
    sample readers return them.
    """

    window: str
    model: str | None
    resets_at: float
    samples: list[dict]
    stretch: int = 0
    """Which fill curve under this reset time, 0 for the one that opened it."""

    @property
    def peak(self) -> float:
        """The fullest this window was ever seen. Raw float, as stored."""
        return max(s["used_pct"] for s in self.samples)

    @property
    def first_ts(self) -> float:
        return self.samples[0]["ts"]

    @property
    def peak_ts(self) -> float:
        """When the peak was first reached, not the last sample that matched it.

        A window that sits at its peak for hours filled once; the later samples
        are the plateau, and counting them as fill time would report the idle
        stretch as part of how fast it got there.
        """
        peak = self.peak
        return next(s["ts"] for s in self.samples if s["used_pct"] == peak)

    @property
    def fill_s(self) -> float:
        """Seconds from the first sample to the peak.

        A floor, not the truth: the window may already have been filling before
        the first render that saw it, and 0 means the peak was already there.
        """
        return self.peak_ts - self.first_ts

    @property
    def hit_limit(self) -> bool:
        """Whether this window filled.

        Rounded to match the write gate: it only lets a reading through when the
        whole percent moves, so 99.6 is the last sample a full window can leave
        behind and treating it as short of the limit would undercount.
        """
        return round(self.peak) >= 100

    @property
    def key(self) -> InstanceKey:
        """What window_instances grouped on, and so unique across a report.

        The stretch is in it because a rebase puts two of these under one reset
        time, and a caller keying spend on the first three would price one of
        them over the other's span.
        """
        return (self.window, self.model, self.resets_at, self.stretch)

    @property
    def rebased(self) -> bool:
        """Whether an earlier stretch filled a different allowance under this reset."""
        return self.stretch > 0

    @property
    def last_ts(self) -> float:
        return self.samples[-1]["ts"]

    @property
    def opening_pct(self) -> float:
        """The first reading taken of this window, which is rarely 0.

        Capture starts when a render happens, not when the window opens, so a
        window seen first at 77% had already spent 77 points nobody watched.
        Every rate below is measured from here, and the reports name it, so the
        number is read as "since we started looking" and not as the window's own
        history.
        """
        return self.samples[0]["used_pct"]

    @property
    def latest_pct(self) -> float:
        """The newest reading — where the window stands, if it is still open."""
        return self.samples[-1]["used_pct"]

    @property
    def rise(self) -> float:
        """Points gained between the first sample and the peak."""
        return self.peak - self.opening_pct

    @property
    def burn_pph(self) -> float | None:
        """Points per hour over the fill span, or None when there is no span.

        Wall-clock, not active-hours: an overnight gap between two renders
        counts as time the window took to fill. That makes it the rate to
        project a reset time with (idle hours will happen again before this
        window closes) and the wrong one to answer how fast a working hour
        spends the quota.

        None where the arithmetic has no meaning — one sample, or a peak
        already there when the first render saw it — rather than 0, which
        would read as "this window is not filling".
        """
        if self.fill_s <= 0 or self.rise <= 0:
            return None
        return self.rise / (self.fill_s / 3600)

    @property
    def started_at(self) -> float | None:
        """When the window opened, or None where its length is unknown."""
        span = LIMIT_WINDOW_SPAN_S.get(self.window)
        return None if span is None else self.resets_at - span

    @property
    def unseen_s(self) -> float | None:
        """Seconds the window ran before the first sample of it was taken."""
        start = self.started_at
        return None if start is None else max(0.0, self.first_ts - start)

    @property
    def partial(self) -> bool:
        """Whether the window had filled measurably before capture began.

        The gap is opening_pct — what the first render found already spent —
        and not the hours before that render, which cost nothing while nobody
        was working. A partial instance still reports a true peak, but its
        Spend and $/pp price the sampled span alone, so the two columns answer
        different stretches of the same window.
        """
        return self.opening_pct >= PARTIAL_OPENING_PP

    def is_open(self, now: float) -> bool:
        """Whether the window has yet to reset."""
        return self.resets_at > now

    def projected_pct(self, now: float) -> float | None:
        """Where the latest reading lands by reset time at the current rate.

        Extrapolated from the last sample rather than from *now*, which is only
        used to decide whether the window is still open: both ends of the line
        are then readings, and a machine that has not rendered in six hours
        does not get those hours counted twice — once as idle time inside the
        rate, once as time still to burn.

        None for a closed window (its outcome is the peak, not a projection)
        and for one with no measurable rate. Uncapped: a projection over 100%
        is the useful reading, since it says the limit arrives before the reset
        does.
        """
        rate = self.burn_pph
        if rate is None or not self.is_open(now):
            return None
        return self.latest_pct + rate * (self.resets_at - self.last_ts) / 3600


def window_instances(samples: list[dict]) -> list[WindowInstance]:
    """Group *samples* into window instances, oldest instance first.

    Keyed on (window, model, resets_at) rather than resets_at alone: the scoped
    limit follows whichever model it is scoped to, and two models' weekly
    windows reset together. *samples* must be in ts order — insertion order then
    carries both the instances and the samples within one.

    The reset time is bucketed to the minute through rl_window_key, and the
    bucket is what the instance reports. Rows written before the writer
    normalized carry the API's jitter permanently, and grouping them on the exact
    float turned one scoped week into 80 single-sample instances. The samples
    keep the float they were stored with; only the instance's identity is
    rounded, so nothing here rewrites what was recorded.
    """
    by_key: dict[tuple[str, str | None, float], list[dict]] = {}
    for s in samples:
        resets = rl_window_key(s["resets_at"])
        by_key.setdefault((s["window"], s["model"], resets), []).append(s)
    return [
        WindowInstance(window, model, resets, stretch, i)
        for (window, model, resets), grouped in by_key.items()
        for i, stretch in enumerate(rebase_stretches(grouped))
    ]


def rebase_stretches(samples: list[dict]) -> list[list[dict]]:
    """*samples* cut wherever the reading fell far enough to be a new quota.

    One reset time can cover two fill curves: a plan change restarts the
    percentage against the new allowance and leaves resets_at where it was, so
    a peak, a fill span or a rate taken across the fall describes neither
    quota. The cut is by drop size alone, since nothing in a sample says which
    plan it was read under — see REBASE_DROP_PP for what separates one from the
    rounding steps.

    *samples* must be in ts order. Always at least one stretch, so a caller can
    enumerate the result without checking for the ordinary window.
    """
    stretches: list[list[dict]] = [[]]
    for s in samples:
        prev = stretches[-1][-1]["used_pct"] if stretches[-1] else None
        if prev is not None and prev - s["used_pct"] > REBASE_DROP_PP:
            stretches.append([])
        stretches[-1].append(s)
    return stretches


def instance_order(inst: WindowInstance) -> tuple[int, str, float, str, int]:
    """Sort key: window type as printed, then chronological, model breaking ties.

    Applied once, before a table and the JSON split, so the two agree on the
    order — the model tiebreak is what makes it total, since two scoped models'
    weekly windows reset at the same moment. An unlabelled window sorts after
    all four and by name, which is also the order window_types prints them.

    The stretch comes last, so a rebased window prints under the one it
    restarted: the two share every field before it.
    """
    known = inst.window in LIMIT_WINDOWS
    rank = LIMIT_WINDOWS.index(inst.window) if known else len(LIMIT_WINDOWS)
    return (
        rank, "" if known else inst.window, inst.resets_at, inst.model or "", inst.stretch,
    )


def window_types(instances: list[WindowInstance]) -> list[str]:
    """The window types present, the four known ones in order and the rest after."""
    present = {i.window for i in instances}
    return [w for w in LIMIT_WINDOWS if w in present] + sorted(present - set(LIMIT_WINDOWS))


SPEND_ALL = "*"
"""The SpendIndex series covering every model, whatever family it belongs to."""

# Window types whose quota counts one model family, where the samples do not
# name it. The scoped window carries its model in the sample; these do not.
_WINDOW_FAMILY = {"sonnet": "sonnet"}


def window_family(inst: WindowInstance) -> str | None:
    """Which model family's spend fills *inst*, or None for all of them.

    The scoped window follows whichever model it is scoped to and names it in
    the sample; the Sonnet window is scoped by its own definition. Session and
    week count everything, so they get no filter. pricing.model_family maps a
    sample's display name ("Fable") and a record's model ID ("claude-fable-5")
    onto the same key, which is what lets the two be compared at all.
    """
    if inst.model:
        return pricing.model_family(inst.model)
    return _WINDOW_FAMILY.get(inst.window)


# The cumulative series SpendIndex keeps per model family, in the order it
# stores them. Cost is what the spend columns read; the two token counts are
# the cache-hit share's numerator and denominator, kept raw so every reader
# divides them the same way.
_SERIES = ("usd", "cache_read", "observed_input")
_USD, _CACHE_READ, _OBSERVED = range(len(_SERIES))

SpendRow = tuple[float, float, float, float, str]
"""One call as `SpendIndex` reads it: instant, USD, cache reads, observed input
and model family. Observed input is what the API was shown — fresh input, plus
what was written to the cache and read back from it."""


def spend_row(rec: UsageRecord) -> SpendRow:
    """One record as the five numbers an index keeps of it."""
    tokens = rec.tokens
    return (
        rec.timestamp.timestamp(),
        rec.cost(),
        float(tokens.cache_read),
        float(tokens.input + tokens.cache_create + tokens.cache_read),
        pricing.model_family(rec.model),
    )


class SpendIndex:
    """Deduplicated record cost and token counts, summable over a time range.

    Built once per run and queried once per window instance, because instances
    overlap — every session window sits inside a week window, and summing the
    corpus per instance is quadratic once a year of history has accumulated.

    Each family keeps its own timestamps and running totals rather than a column
    in one array: a query is then one bisect per series and one pass per record,
    instead of a per-family pass over every record.

    *records* must be in timestamp order, which is what every loader returns.
    """

    def __init__(self, records: list[UsageRecord]) -> None:
        self._ts: dict[str, list[float]] = {}
        self._cum: dict[str, list[list[float]]] = {}
        self._absorb(spend_row(rec) for rec in records)

    @classmethod
    def from_rows(cls, rows: Iterable[SpendRow]) -> SpendIndex:
        """An index over rows a caller read without building records first.

        Five numbers per call is what this class reads; a UsageRecord carries a
        project, a session, an account and three date derivations besides, and
        the server was building 83,250 of them per window page to sum three
        columns. Same order requirement as *records*.
        """
        index = cls([])
        index._absorb(rows)
        return index

    def _absorb(self, rows: Iterable[SpendRow]) -> None:
        for when, usd, cache_read, observed, family in rows:
            values = (usd, cache_read, observed)
            for key in (SPEND_ALL, family):
                self._ts.setdefault(key, []).append(when)
                cum = self._cum.setdefault(key, [[0.0] for _ in _SERIES])
                for series, value in zip(cum, values, strict=True):
                    series.append(series[-1] + value)

    @property
    def empty(self) -> bool:
        """Whether there is no corpus at all behind this index.

        The reports ask, because $0.00 of spend against a window that visibly
        filled is a missing corpus, not a free window, and rendering it as a
        number would state the wrong one.
        """
        return not self._ts

    def _span(self, series: int, start: float, end: float, family: str | None) -> float:
        """One series summed over [*start*, *end*], on *family* alone when given.

        Both bounds inclusive, matching the window instance they come from — a
        record written in the same second as the first sample belongs to the
        window that sample opened.
        """
        key = family or SPEND_ALL
        stamps = self._ts.get(key)
        if not stamps:
            return 0.0
        cum = self._cum[key][series]
        return (cum[bisect.bisect_right(stamps, end)]
                - cum[bisect.bisect_left(stamps, start)])

    def total(self, start: float, end: float, family: str | None = None) -> float:
        """USD spent in [*start*, *end*], on *family* alone when given."""
        return self._span(_USD, start, end, family)

    def cache_tokens(
        self, start: float, end: float, family: str | None = None,
    ) -> tuple[int, int]:
        """(cache reads, observed input) over the same span the cost covers.

        Observed input is what the API was shown: fresh input, tokens written to
        the cache, and tokens read back from it. Output is left out — it is the
        model's answer rather than context that was or was not already paid for.
        """
        return (
            round(self._span(_CACHE_READ, start, end, family)),
            round(self._span(_OBSERVED, start, end, family)),
        )


class ExtraIndex:
    """Extra-usage spend over a time range, from the stored snapshot series.

    The series is cumulative dollars within a billing month, sampled by the
    status line on slow renders alone, so it is coarse and it restarts at 0
    every month. A range is therefore walked rather than subtracted end to end:
    a reading below the one before it is the monthly reset, and the whole of
    that reading is spend since it.

    *snapshots* are `(ts, spent)` in ts order, as cache_db.load_extra_snapshots
    returns them for this machine and db.load_extra_samples does per machine
    on the server. One machine's readings, never two merged: the figure is
    cumulative account spend, so a second machine reporting a slightly older
    one after a fresher one is a drop, and a drop here means the month rolled
    over.
    """

    def __init__(self, snapshots: list[tuple[float, float]]) -> None:
        self._ts = [ts for ts, _spent in snapshots]
        self._spent = [spent for _ts, spent in snapshots]

    def spent_between(self, start: float, end: float) -> float | None:
        """Dollars accrued in (*start*, *end*], or None where nothing bounds it.

        Needs a reading at or before *start* to subtract from and one inside the
        range to subtract it from. Missing either, the answer is unknown and not
        $0.00 — the series is pruned at 31 days and skipped by every costs-only
        refresh, so an absent reading says nothing about what was spent.

        One exception, and it is what makes a week reconcile with the sessions
        inside it: a reading of $0.00 has nothing behind it, so where the series
        begins inside the range at zero it is a baseline of its own. Without it
        the oldest window of every type is unknown however much it billed, since
        the series can only start after it opened.
        """
        base = bisect.bisect_right(self._ts, start) - 1
        last = bisect.bisect_right(self._ts, end)
        if base < 0:
            opening = bisect.bisect_left(self._ts, start)
            if opening >= last or self._spent[opening] != 0.0:
                return None
            base = opening
        if last <= base + 1:
            return None
        total = 0.0
        prev = self._spent[base]
        for spent in self._spent[base + 1:last]:
            total += spent - prev if spent >= prev else spent
            prev = spent
        return total


@dataclass(frozen=True)
class WindowSpend:
    """What one window instance's observed rise cost, in API-priced dollars.

    An exchange rate, not an identity: the rate limit meters something Anthropic
    does not publish, and this divides what the same work would have cost at API
    prices by the points it consumed. It answers "what is the rest of this
    window worth" in the only unit this tool has.

    Measured over the fill span (first sample → peak), the same span
    WindowInstance.rise and .burn_pph are measured over, so the three describe
    one stretch of time and not three.

    *extra_usd* is the exception, and the only real money here: dollars Anthropic
    actually billed, over the window's whole life rather than over its fill span.
    """

    usd: float | None
    """Spend over the fill span."""
    per_pp: float | None
    """USD per point gained."""
    headroom_usd: float | None
    """What the points left are worth at that rate; None for a closed window,
    whose points are gone rather than left."""
    extra_usd: float | None = None
    """Extra usage billed while the window ran; None where unknown."""
    cache_read: int | None = None
    """Tokens read back from the prompt cache over the fill span."""
    observed_input: int | None = None
    """Fresh input, cache writes and cache reads over the same span — every
    token the window was charged context for, which is what cache_read is a
    share of."""

    @property
    def cache_hit(self) -> float | None:
        """Cache reads over observed input, or None where nothing priced the span.

        The reading is "how much of this window's context was already paid
        for". A window with no records behind it has no share rather than a 0%
        one — the same distinction the Spend column draws — and so does one
        whose span carried output alone.
        """
        if not self.observed_input:
            return None
        return (self.cache_read or 0) / self.observed_input


def instance_spend(
    inst: WindowInstance, index: SpendIndex, now: float, extra_usd: float | None = None,
) -> WindowSpend:
    """Price *inst*'s rise, and what is left of it, against the record corpus.

    A window that never rose while it was watched prices as nothing at all
    rather than as $0.00: its fill span is a single instant, and the spend of
    an instant is a number nobody asked for wearing the answer to "was this
    window free". The cache-hit share goes with it, for the same reason.

    *extra_usd* survives that early return: it is metered by the clock rather
    than by the rise, so a window nobody watched rising still billed what it
    billed. Both ends pass it — the CLI off this machine's snapshot table, the
    server off the readings one machine on the account pushed — and None where
    no reading bounds the span.
    """
    if index.empty or inst.rise <= 0:
        return WindowSpend(None, None, None, extra_usd)
    family = window_family(inst)
    usd = index.total(inst.first_ts, inst.peak_ts, family)
    cache_read, observed = index.cache_tokens(inst.first_ts, inst.peak_ts, family)
    per_pp = usd / inst.rise
    headroom = (
        max(100.0 - inst.latest_pct, 0.0) * per_pp if inst.is_open(now) else None
    )
    return WindowSpend(usd, per_pp, headroom, extra_usd, cache_read, observed)


def group_cache_hit(
    instances: list[WindowInstance], spends: dict[InstanceKey, WindowSpend],
) -> float | None:
    """A group's own cache-hit share: its total reads over its total input.

    Not the mean of the per-window shares — a window that ran one call would
    weigh as much as a week that ran forty thousand.
    """
    reads = sum(s.cache_read or 0 for s in (spends[i.key] for i in instances))
    observed = sum(s.observed_input or 0 for s in (spends[i.key] for i in instances))
    return (reads / observed) if observed else None
