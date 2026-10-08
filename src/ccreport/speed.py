"""How long a model took to answer, read off the timestamps a session log carries.

The log has no latency field. What it has is a timestamp per line: every content
block of a reply — thinking, text, each tool_use — is a line of its own, stamped
when that block finished, and every line of one reply shares its message id and
request id. The request began at the `user` line before its first block, which
is either the prompt or the tool_result the model is answering. So a request's
span is that user line to its last block, and every number here is derived
from it.

All of it is an approximation and is reported as one. The span includes
queueing and prefill, and a block is written when it completes rather than when
it starts, so a long thinking block hides when output began — there is no true
time to first token in the log.

No ccreport import, and no dataclasses or statistics either: pricing.py
imports this for the status line's session scan, and those two cost a render
6.7 ms between them. scan.py derives the span through this at parse time, the
archive fold and `ccreport speed` fold it, and the status line tracks the same
span a line at a time.

AUDIT: documented in docs/calculation-reference.md section 10.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ccreport.aggregate import UsageRecord

MIN_RATE_OUTPUT_TOKENS = 100
"""A reply shorter than this has no output rate.

Below it the span is mostly prefill and queueing, so tokens over seconds says
how long the request waited rather than how fast the model wrote. Chosen off a
21k-request corpus: from 100 up, the median rate per model moves under 5% as
the floor rises, where at 0 a model answering mostly in short tool calls read
at an eighth of its rate above the floor. Latency counts every reply; only the
rate takes the floor."""


class RequestClock:
    """Pairs each reply with the user line that started it, in log order.

    Feed it every user line and every reply block of one file as they come.
    A reply's start is fixed on its first block, so a tool_result logged
    between two blocks of the same reply cannot move it.
    """

    __slots__ = ("_last_user", "_spans")

    def __init__(self) -> None:
        self._last_user: float | None = None
        self._spans: dict[str, list[float | None]] = {}

    def user(self, ts: float) -> None:
        """A user line — a prompt or a tool_result — at epoch *ts*."""
        self._last_user = ts

    def block(self, key: str, ts: float) -> None:
        """One content block of the reply *key* (its dedup key), finished at *ts*."""
        span = self._spans.get(key)
        if span is None:
            start = self._last_user
            # A user line stamped after the block it supposedly started is a
            # clock that went backwards, not a request; leave it unstarted.
            self._spans[key] = [start if start is not None and start <= ts else None, ts]
        elif span[1] is None or ts > span[1]:
            span[1] = ts

    def span(self, key: str) -> tuple[float | None, float | None]:
        """(start, end) of the reply *key*, None where the log could not say."""
        span = self._spans.get(key)
        if span is None:
            return None, None
        return span[0], span[1]


def latency(start: float | None, end: float | None) -> float | None:
    """Seconds from a request's start to its last block, or None if unknown."""
    if start is None or end is None or end <= start:
        return None
    return end - start


def output_rate(output_tokens: int, start: float | None, end: float | None) -> float | None:
    """Effective output tokens per second, or None below MIN_RATE_OUTPUT_TOKENS."""
    secs = latency(start, end)
    if secs is None or output_tokens < MIN_RATE_OUTPUT_TOKENS:
        return None
    return output_tokens / secs


def timed_model(model: str) -> bool:
    """Whether *model* is a real one. `<synthetic>` replies were never requested."""
    return not model.startswith("<")


class ReplyTracker:
    """The newest finished reply of one log, extended a line at a time.

    RequestClock's rule, kept as a handful of scalars so the status line can
    store it between renders and resume on the appended bytes alone. A reply is
    finished once the next reply starts or a turn ends: until then a later
    block can still move its end, and a rate read off half a reply is too high.
    A user line does not finish one, for the reason RequestClock does not let
    it restart one — a tool_result can land between two blocks of a reply.
    The output count is the first block's, which is the line the dedup keeps.
    """

    __slots__ = ("_closed", "_last_user", "_open")

    def __init__(self) -> None:
        self._last_user: float | None = None
        self._open: list | None = None  # [key, start, end, output_tokens]
        self._closed: tuple[float, float] | None = None  # (end, tok/s)

    def user(self, ts: float) -> None:
        self._last_user = ts

    def block(self, key: str, ts: float, output_tokens: int) -> None:
        if self._open is not None and self._open[0] == key:
            self._open[2] = max(self._open[2], ts)
            return
        self.turn_end()
        start = self._last_user
        self._open = [key, start if start is not None and start <= ts else None, ts,
                      output_tokens]

    def turn_end(self) -> None:
        """Finish the open reply, if any. Also what a new reply does to the last."""
        if self._open is None:
            return
        _key, start, end, output = self._open
        self._open = None
        rate = output_rate(output, start, end)
        if rate is not None:
            self._closed = (end, rate)

    def last(self) -> tuple[float, float] | None:
        """(end, tok/s) of the newest finished reply that has a rate."""
        return self._closed

    def dump(self) -> list:
        return [self._last_user, self._open, list(self._closed) if self._closed else None]

    @classmethod
    def load(cls, data: list) -> ReplyTracker:
        """The inverse of dump. Raises on a malformed blob; the caller reparses."""
        tracker = cls()
        last_user, open_, closed = data
        tracker._last_user = None if last_user is None else float(last_user)
        if open_ is not None:
            key, start, end, output = open_
            tracker._open = [str(key), None if start is None else float(start),
                             float(end), int(output)]
        if closed is not None:
            tracker._closed = (float(closed[0]), float(closed[1]))
        return tracker


class SpeedSums:
    """What survives of a day's timing once `ccreport archive` folds it.

    A median cannot be folded, so an archived day keeps sums alone: the mean
    latency is latency_s / timed_n and the rate is rate_output / rate_s, the
    second weighted by tokens rather than averaging per-request rates. Sums of
    sums are still sums, so a week of archived days weighs each by its
    requests rather than averaging the daily means.
    """

    __slots__ = ("latency_s", "rate_output", "rate_s", "timed_n")

    def __init__(
        self, timed_n: int = 0, latency_s: float = 0.0, rate_output: int = 0, rate_s: float = 0.0,
    ) -> None:
        self.timed_n = timed_n
        """Requests with a known span."""
        self.latency_s = latency_s
        """Their spans, summed."""
        self.rate_output = rate_output
        """Output tokens of the requests that cleared MIN_RATE_OUTPUT_TOKENS."""
        self.rate_s = rate_s
        """Those same requests' spans, summed."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SpeedSums) and self.as_tuple() == other.as_tuple()

    __hash__ = None  # type: ignore[assignment]  # mutable, like the dataclass it was

    def __repr__(self) -> str:
        return f"SpeedSums{self.as_tuple()!r}"

    def add(self, output_tokens: int, start: float | None, end: float | None) -> None:
        """Count one request in."""
        secs = latency(start, end)
        if secs is None:
            return
        self.timed_n += 1
        self.latency_s += secs
        if output_tokens >= MIN_RATE_OUTPUT_TOKENS:
            self.rate_output += output_tokens
            self.rate_s += secs

    def merge(self, other: SpeedSums) -> None:
        self.timed_n += other.timed_n
        self.latency_s += other.latency_s
        self.rate_output += other.rate_output
        self.rate_s += other.rate_s

    def as_tuple(self) -> tuple[int, float, int, float]:
        """In the archive's column order: timed_n, latency_s, rate_output, rate_s."""
        return self.timed_n, self.latency_s, self.rate_output, self.rate_s


class SpeedBucket:
    """One (period, model) cell of `ccreport speed`.

    Requests from the record path keep their samples, so the cell can answer a
    median. Archived days add sums alone, and a cell holding any of them
    answers with means for the whole cell — a median over the live half beside
    a mean of the archived half would be a number that is neither.
    """

    __slots__ = ("archived", "latencies", "rates", "sums", "turns_ms")

    def __init__(self) -> None:
        self.latencies: list[float] = []
        self.rates: list[float] = []
        self.turns_ms: list[int] = []
        self.sums = SpeedSums()
        self.archived = False
        """True once an archived day's sums are in: the cell then reports means."""

    def add_request(self, output_tokens: int, start: float | None, end: float | None) -> None:
        secs = latency(start, end)
        if secs is None:
            return
        self.sums.add(output_tokens, start, end)
        self.latencies.append(secs)
        rate = output_rate(output_tokens, start, end)
        if rate is not None:
            self.rates.append(rate)

    def add_archived(self, sums: SpeedSums) -> None:
        if not sums.timed_n:
            return
        self.sums.merge(sums)
        self.archived = True

    def add_turn(self, duration_ms: int) -> None:
        self.turns_ms.append(duration_ms)

    @property
    def requests(self) -> int:
        return self.sums.timed_n

    def latency_mid(self) -> float | None:
        """Median request latency, or the mean once an archived day is in."""
        if self.archived:
            return self.sums.latency_s / self.sums.timed_n if self.sums.timed_n else None
        return _quantile(self.latencies, 0.5) if self.latencies else None

    def latency_p90(self) -> float | None:
        """90th percentile latency. None once an archived day is in: no samples."""
        if self.archived or not self.latencies:
            return None
        return _quantile(self.latencies, 0.9)

    def rate_mid(self) -> float | None:
        """Median output tok/s, or Σtokens/Σseconds once an archived day is in."""
        if self.archived:
            return self.sums.rate_output / self.sums.rate_s if self.sums.rate_s else None
        return _quantile(self.rates, 0.5) if self.rates else None

    def turn_mid(self) -> float | None:
        """Median turn duration in seconds. Turns are never folded, so always a median."""
        return _quantile(self.turns_ms, 0.5) / 1000 if self.turns_ms else None


def _quantile(values: list[float] | list[int], q: float) -> float:
    """The *q* quantile, interpolated between the two nearest ranks.

    statistics.quantiles(method="inclusive") and statistics.median agree with
    this; that module is not imported for it, for the reason in the docstring.
    """
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    if lo + 1 >= len(ordered):
        return float(ordered[lo])
    return ordered[lo] + (ordered[lo + 1] - ordered[lo]) * (pos - lo)


PERIODS = ("day", "week", "month")
"""What `ccreport speed --by` cuts the corpus into."""

TOTAL = ""
"""The period key of the whole-range fold, period_key's answer for by=None."""


def period_key(day: str, by: str | None) -> str:
    """The *by* bucket a local YYYY-MM-DD falls in.

    A week is the ISO week, opening on Monday as the server's week period does,
    and keyed YYYY-Www rather than on its Monday so a key reads as a week.
    """
    if by is None:
        return TOTAL
    if by == "day":
        return day
    if by == "month":
        return day[:7]
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def fold(
    records: Iterable[UsageRecord],
    turns: Iterable[tuple[str, str, int]],
    by: str | None,
) -> dict[tuple[str, str], SpeedBucket]:
    """Fold deduped *records* and (day, model, duration_ms) *turns* into cells.

    Keyed (period, model). A record carrying `speed` is an archived day and adds
    its sums; any other adds its own span. A cell no request and no turn landed
    in is not there, so a model whose every reply was untimed draws no row.
    """
    buckets: dict[tuple[str, str], SpeedBucket] = {}
    for rec in records:
        if not timed_model(rec.model):
            continue
        key = (period_key(rec.day_key(), by), rec.model)
        if rec.speed is not None:
            if rec.speed.timed_n:
                buckets.setdefault(key, SpeedBucket()).add_archived(rec.speed)
        elif rec.req_start is not None and rec.req_end is not None:
            buckets.setdefault(key, SpeedBucket()).add_request(
                rec.tokens.output, rec.req_start, rec.req_end,
            )
    for day, model, duration_ms in turns:
        buckets.setdefault((period_key(day, by), model), SpeedBucket()).add_turn(duration_ms)
    return buckets
