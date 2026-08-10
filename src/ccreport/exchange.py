"""USD/NOK exchange rate lookups using Norges Bank daily spot rates.

Fetches the B.USD.NOK.SP series from the Norges Bank SDMX-JSON API and stores
every observation in SQLite. get_rate then answers one date out of that store,
walking back up to _MAX_WALKBACK_DAYS days when the date itself has no
observation — a weekend or a Norwegian holiday — and marking the rate estimated
only when the walk ran past the newest date the store holds.
"""

from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ccreport import cache_db

OSLO_TZ = ZoneInfo("Europe/Oslo")
# How far get_rate may walk back from a record's date for a rate, and with it
# the read horizon of _load_cached_rates and start_prefetch, the reach
# _resolvable checks, and how far before the earliest wanted date _fetch_span
# starts its span. Sized for the longest run of dates the series can leave
# unobserved, which is a Norwegian holiday cluster — Easter's Thursday-to-Monday
# and the Christmas/New Year stretch, each butting onto a weekend — not the
# three days of a long weekend. Lower it past one of those and get_rate returns
# (None, False) for the dates behind the gap: those records leave the NOK column
# empty and nothing reports an error.
_MAX_WALKBACK_DAYS = 10

# A cached rate is permanent: _fetch_span only asks the API for dates with no
# row, and get_rate's walkback stops at the first date present, sound or not.
# So one bad value (a 0.0, a misparsed field) silently skews every NOK figure
# for that date forever. USD/NOK has stayed within roughly 5-12 for as long as
# the series exists; this band rejects garbage while leaving room for a currency
# move far outside anything on record.
_MIN_PLAUSIBLE_RATE = 5.0
_MAX_PLAUSIBLE_RATE = 20.0

# The B.USD.NOK.SP series carries business days only, so a Saturday or a
# Sunday has no observation to fetch and never will. Treating those dates as
# permanently answered (by get_rate's walkback to the Friday) is what keeps
# them out of the missing set; without it every run asked the API for a span
# reaching back to the oldest weekend in the corpus.
#
# A Norwegian public holiday is the same kind of hole but not derivable from
# the date, so it is learnt instead: a date the API covered and returned
# nothing for is stored with this sentinel rate, meaning "asked, no
# observation exists". _is_plausible_rate rejects it, so it can never collide
# with a real rate, and it is filtered out before any lookup sees it.
_NO_OBSERVATION = 0.0

# How old a date must be before a missing observation counts as final. Norges
# Bank publishes a day's rate that afternoon, so today's and yesterday's
# absence means "not yet", not "never" — negative-caching those would freeze
# the newest days as estimated forever. Comfortably clears a long weekend.
_OBSERVATION_SETTLE_DAYS = 5

# How far back start_prefetch speculates. It runs before the corpus is loaded
# and so cannot know which dates the report needs; on a warm cache the only
# ones that can still be missing are the recent tail, and three weeks covers
# that plus a stretch of days the machine did not run.
_PREFETCH_LOOKBACK_DAYS = 21

_API_BASE = (
    "https://data.norges-bank.no/api/data/EXR/B.USD.NOK.SP"
    "?format=sdmx-json&locale=no"
)


def _is_plausible_rate(rate: float) -> bool:
    """Whether *rate* is a USD/NOK figure worth caching permanently."""
    return math.isfinite(rate) and _MIN_PLAUSIBLE_RATE <= rate <= _MAX_PLAUSIBLE_RATE


def _is_business_day(d: date) -> bool:
    """Whether the series can hold an observation for *d* at all."""
    return d.weekday() < 5


def today_oslo() -> date:
    """Today where the rates are published, which decides what is publishable yet.

    Norges Bank's calendar, not the machine's: a run from another zone would
    otherwise call a date settled, or not, a day early.
    """
    return datetime.now(OSLO_TZ).date()


def _parse_sdmx(data: dict[str, Any]) -> tuple[dict[str, float], set[str]]:
    """Parse an SDMX-JSON response into ({date_str: rate}, rejected date_strs).

    Implausible rates are dropped rather than returned, and their dates come
    back in the second half: an observation that arrived and failed validation
    must stay uncached — negatively as much as positively — so the next run
    asks for it again.
    """
    rates: dict[str, float] = {}
    rejected: set[str] = set()
    try:
        structure = data["data"]["structure"]
        time_periods = structure["dimensions"]["observation"][0]["values"]
        series = data["data"]["dataSets"][0]["series"]
        # Single series key "0:0:0:0" for USD/NOK spot
        obs = next(iter(series.values()))["observations"]
        for idx_str, values in obs.items():
            idx = int(idx_str)
            if idx < len(time_periods):
                date_str = time_periods[idx]["id"]
                rate = float(values[0])
                if _is_plausible_rate(rate):
                    rates[date_str] = rate
                else:
                    rejected.add(date_str)
    except (KeyError, IndexError, StopIteration, ValueError, TypeError):
        pass
    return rates, rejected


def _fetch_api(start: date, end: date) -> tuple[dict[str, float], set[str]]:
    """Fetch rates from Norges Bank API for a date range.

    An unreachable API answers the same as an empty range, which is why the
    caller may only conclude "no observation exists" from a non-empty result.
    """
    url = f"{_API_BASE}&startPeriod={start}&endPeriod={end}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read())
        return _parse_sdmx(data)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return {}, set()


def _read_cached(since: date) -> tuple[dict[str, float], set[str]]:
    """Cached rates from *since* on, split from the dates known to have none.

    The two live in one table under one key, so they come back in one read;
    everything downstream wants them apart, and nothing but this function may
    hand a _NO_OBSERVATION row on as if it were a rate.
    """
    rows = cache_db.get_exchange_rates(since.isoformat())
    rates = {d: r for d, r in rows.items() if r != _NO_OBSERVATION}
    gaps = {d for d, r in rows.items() if r == _NO_OBSERVATION}
    return rates, gaps


def _load_cached_rates(dates: set[date]) -> tuple[dict[str, float], set[str]]:
    """Load the cached rates any lookup for *dates* could reach, and the gaps.

    get_rate walks back up to _MAX_WALKBACK_DAYS, so the earliest usable rate
    sits that far before the earliest requested date. Everything older is dead
    weight in a table that grows a row per calendar day forever.
    """
    return _read_cached(min(dates) - timedelta(days=_MAX_WALKBACK_DAYS))


def _record_fetch(
    start: date, end: date, rates: dict[str, float], rejected: set[str],
) -> tuple[dict[str, float], set[str]]:
    """Persist a fetch: its rates, plus the gaps it proved are permanent.

    A business day inside a range the API did answer, with no observation in
    the reply, has none to be had — a Norwegian holiday. Storing that as a
    _NO_OBSERVATION row is what stops the next run from asking again, which is
    the whole reason a six-month span was re-requested on every invocation.

    Three things are deliberately not negative-cached: a date the API returned
    an implausible value for (it exists, we just refused it), anything newer
    than _OBSERVATION_SETTLE_DAYS (today's rate may simply not be published
    yet), and the entire range when the reply was empty — an unreachable API
    looks exactly like a range with nothing in it.

    The oldest observation in the reply, not *start*, bounds where the sweep
    begins: a range reaching back past the start of the series would otherwise
    have every business day before it recorded as a permanent hole.
    """
    if not rates:
        return {}, set()
    gaps: dict[str, float] = {}
    settled = min(end, today_oslo() - timedelta(days=_OBSERVATION_SETTLE_DAYS))
    d = max(start, date.fromisoformat(min(rates)))
    while d <= settled:
        key = d.isoformat()
        if _is_business_day(d) and key not in rates and key not in rejected:
            gaps[key] = _NO_OBSERVATION
        d += timedelta(days=1)
    cache_db.save_exchange_rates({**rates, **gaps})
    return rates, set(gaps)


def to_oslo_date(ts: datetime) -> date:
    """Convert a timestamp to its Oslo-local date (canonical FX date)."""
    return ts.astimezone(OSLO_TZ).date()


def _resolvable(d: date, cached: dict[str, float]) -> bool:
    """Whether get_rate can already answer for *d* out of *cached*."""
    return any(
        (d - timedelta(days=i)).isoformat() in cached
        for i in range(_MAX_WALKBACK_DAYS + 1)
    )


def _fetch_span(
    needed: set[date], cached: dict[str, float], gaps: set[str],
) -> tuple[date, date] | None:
    """The range worth asking the API about, or None when nothing is.

    Two kinds of date go in. A business day with no rate and no gap row is one
    we have never had an answer for. A date get_rate cannot reach at all, even
    walking back, is the other — that one is worth fetching whatever weekday it
    falls on, because without it the record converts at no rate.

    Everything else stays out, and that is the point: a weekend, or a holiday
    already known to have no observation, is answered by the walkback and asking
    again can only ever return the same nothing.
    """
    wanted = {
        d for d in needed
        if (_is_business_day(d) and d.isoformat() not in cached
            and d.isoformat() not in gaps)
        or not _resolvable(d, cached)
    }
    if not wanted:
        return None
    # Reaching a walkback before the earliest wanted date is what lets a gap at
    # the start of the range fall back to a rate rather than to nothing.
    return min(wanted) - timedelta(days=_MAX_WALKBACK_DAYS), max(wanted)


class RateFetch:
    """A Norges Bank request running on its own thread.

    Which dates a report needs is known only once its records are loaded, but
    the API call does not have to wait for that: on a warm cache the only dates
    that can still be missing are the recent tail, so start_prefetch asks for
    that up front and the corpus load runs against it. load_rates joins here
    and still fetches, blocking, whatever this did not cover.

    Only the request itself runs off-thread. cache_db hands out one connection
    owned by the thread that opened it, so both the read that decides what to
    ask for and the write that stores the answer stay on the caller's side.
    """

    def __init__(self, start: date, end: date) -> None:
        self.start = start
        self.end = end
        self._rates: dict[str, float] = {}
        self._rejected: set[str] = set()
        # Daemon so a run that exits before consuming the rates (no records,
        # or a failure) is not held open by a request still in its timeout.
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._rates, self._rejected = _fetch_api(self.start, self.end)

    def collect(self) -> tuple[dict[str, float], set[str]]:
        """Wait for the request and store what it returned. Caller's thread."""
        self._thread.join()
        return _record_fetch(self.start, self.end, self._rates, self._rejected)


def start_prefetch() -> RateFetch | None:
    """Begin fetching the recent rate tail, or None if the cache has it already.

    Returning None on a warm cache is what keeps this from adding a request to
    runs that need none — the common case once weekends and holidays stopped
    counting as missing.
    """
    today = today_oslo()
    start = today - timedelta(days=_PREFETCH_LOOKBACK_DAYS)
    cached, gaps = _read_cached(start - timedelta(days=_MAX_WALKBACK_DAYS))
    window = {start + timedelta(days=i) for i in range((today - start).days + 1)}
    span = _fetch_span(window, cached, gaps)
    return RateFetch(*span) if span else None


def load_rates(
    dates: set[date], prefetch: RateFetch | None = None,
) -> dict[str, float]:
    """Load exchange rates for a set of dates, fetching missing ones from API.

    Returns {date_str: rate} from the walkback horizon of the earliest
    requested date onward. Dates on weekends/holidays won't have entries —
    use get_rate() for fallback.

    *prefetch* is a request start_prefetch already put in flight; it is joined
    and folded in before the missing set is worked out, so anything it covered
    costs no second call.
    """
    if not dates:
        return {}
    cached, gaps = _load_cached_rates(dates)
    if prefetch is not None:
        early, early_gaps = prefetch.collect()
        cached.update(early)
        gaps |= early_gaps
    span = _fetch_span(dates, cached, gaps)
    if span:
        fetched, rejected = _fetch_api(*span)
        cached.update(_record_fetch(*span, fetched, rejected)[0])
    return cached


def get_rate(rates: dict[str, float], d: date, _max_date: str | None = None) -> tuple[float | None, bool]:
    """Look up the rate for a date, walking back for weekends/holidays.

    Returns (rate, estimated) where estimated is True only when the rate
    is at the trailing edge of our data — i.e., we walked back and there
    is no later rate in the dataset. Gaps between two known rates (e.g.,
    weekends) are NOT estimated since the prior business day rate is the
    definitive rate for those dates.

    Returns (None, False) if no rate found within the walkback window.

    *_max_date* is the newest key in *rates*, which is what the trailing edge is
    measured against. Pass it precomputed: without it every call scans the whole
    dict for max(rates), and this runs once per record.
    """
    for i in range(_MAX_WALKBACK_DAYS + 1):
        key = (d - timedelta(days=i)).isoformat()
        if key in rates:
            if i == 0:
                return rates[key], False
            latest = _max_date if _max_date is not None else max(rates)
            return rates[key], d.isoformat() >= latest
    return None, False
