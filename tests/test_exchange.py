"""Tests for exchange.py — bounded rate loading and rate validation.

The Norges Bank API is never called: every test either patches
``exchange._fetch_api`` or the urlopen underneath it.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from typing import Any

import pytest

from ccreport import cache_db, exchange
from ccreport.cache_db import get_exchange_rates, save_exchange_rates
from ccreport.exchange import _NO_OBSERVATION, _parse_sdmx, get_rate, load_rates, today_oslo


def _sdmx(rates: dict[str, float]) -> dict[str, Any]:
    """Build an SDMX-JSON payload shaped like the Norges Bank EXR response."""
    dates = list(rates)
    return {
        "data": {
            "structure": {
                "dimensions": {"observation": [{"values": [{"id": d} for d in dates]}]}
            },
            "dataSets": [
                {
                    "series": {
                        "0:0:0:0": {
                            "observations": {
                                str(i): [rates[d]] for i, d in enumerate(dates)
                            }
                        }
                    }
                }
            ],
        }
    }


@pytest.fixture
def fake_api(monkeypatch):
    """Serve a canned SDMX payload in place of the live API.

    Patches urlopen rather than _fetch_api so the parse/validate path — the
    thing under test — still runs.
    """

    def install(rates: dict[str, float]) -> None:
        # BytesIO is already the context manager urlopen's caller expects:
        # __enter__ hands back the stream, __exit__ closes it and swallows
        # nothing. What it lacks is a way to be constructed from the body.
        def _urlopen(req, timeout=None):
            return io.BytesIO(json.dumps(_sdmx(rates)).encode())

        monkeypatch.setattr(exchange.urllib.request, "urlopen", _urlopen)

    return install


@pytest.fixture
def no_api(monkeypatch):
    """Make every fetch return nothing, so only cached rates are in play."""
    monkeypatch.setattr(exchange, "_fetch_api", lambda start, end: ({}, set()))


@pytest.fixture
def spy_api(monkeypatch):
    """Record every range asked for, serving a canned set of rates.

    The ranges are what most of these tests are about: the bug was not a wrong
    rate but a six-month span re-requested on every run.
    """
    calls: list[tuple[date, date]] = []

    def install(rates: dict[str, float]):
        def _fetch(start, end):
            calls.append((start, end))
            return (
                {
                    d: r
                    for d, r in rates.items()
                    if start.isoformat() <= d <= end.isoformat()
                },
                set(),
            )

        monkeypatch.setattr(exchange, "_fetch_api", _fetch)
        return calls

    return install


# ---------------------------------------------------------------------------
# DAL
# ---------------------------------------------------------------------------


def test_dal_round_trip():
    save_exchange_rates({"2026-01-05": 10.25, "2026-01-06": 10.5})
    assert get_exchange_rates("2026-01-01") == {
        "2026-01-05": 10.25,
        "2026-01-06": 10.5,
    }


def test_dal_save_empty_is_a_noop():
    save_exchange_rates({})
    assert get_exchange_rates("2000-01-01") == {}


def test_dal_save_upserts_existing_date():
    save_exchange_rates({"2026-01-05": 10.0})
    save_exchange_rates({"2026-01-05": 11.0})
    assert get_exchange_rates("2026-01-05") == {"2026-01-05": 11.0}


def test_dal_since_date_is_inclusive_and_excludes_older():
    save_exchange_rates({"2025-12-20": 9.0, "2026-01-05": 10.0, "2026-01-10": 11.0})
    assert get_exchange_rates("2026-01-05") == {"2026-01-05": 10.0, "2026-01-10": 11.0}


# ---------------------------------------------------------------------------
# Bounded read
# ---------------------------------------------------------------------------


def test_load_rates_stops_at_the_walkback_horizon(no_api):
    save_exchange_rates({"2025-06-01": 9.0, "2026-01-05": 10.0})
    rates = load_rates({date(2026, 1, 15)})
    assert "2025-06-01" not in rates, "read reached past the walkback horizon"
    assert "2026-01-05" in rates


def test_walkback_finds_a_rate_at_the_query_boundary(no_api):
    # 2026-01-05 is exactly _MAX_WALKBACK_DAYS before the requested date: the
    # oldest row the bounded read must still return.
    save_exchange_rates({"2026-01-05": 10.0})
    rates = load_rates({date(2026, 1, 15)})
    rate, _ = get_rate(rates, date(2026, 1, 15))
    assert rate == 10.0


def test_horizon_is_measured_from_the_earliest_requested_date(no_api):
    save_exchange_rates({"2026-01-05": 10.0, "2026-03-02": 11.0})
    rates = load_rates({date(2026, 1, 15), date(2026, 3, 2)})
    assert rates == {"2026-01-05": 10.0, "2026-03-02": 11.0}


def test_load_rates_without_dates_touches_nothing(no_api):
    assert load_rates(set()) == {}


# ---------------------------------------------------------------------------
# Rate validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [0.0, -10.0, float("nan"), float("inf"), float("-inf"), 1e9, 4.99, 20.01]
)
def test_parse_drops_implausible_rates(bad):
    assert _parse_sdmx(_sdmx({"2026-01-05": bad})) == ({}, {"2026-01-05"})


@pytest.mark.parametrize("good", [5.0, 10.4321, 20.0])
def test_parse_keeps_plausible_rates(good):
    assert _parse_sdmx(_sdmx({"2026-01-05": good})) == ({"2026-01-05": good}, set())


def test_parse_reports_nothing_for_a_malformed_payload():
    assert _parse_sdmx({"data": {}}) == ({}, set())


def test_bad_rate_is_not_saved_and_date_stays_missing(fake_api):
    fake_api({"2026-03-02": 0.0})
    rates = load_rates({date(2026, 3, 2)})
    assert "2026-03-02" not in rates
    assert get_exchange_rates("2000-01-01") == {}, "a bad rate reached the cache"


def test_a_bad_rate_does_not_discard_its_good_neighbours(fake_api):
    fake_api({"2026-03-02": 10.5, "2026-03-03": 0.0})
    load_rates({date(2026, 3, 2), date(2026, 3, 3)})
    assert get_exchange_rates("2000-01-01") == {"2026-03-02": 10.5}


def test_a_rejected_date_is_re_requested_next_run(fake_api):
    fake_api({"2026-03-02": 0.0})
    load_rates({date(2026, 3, 2)})
    fake_api({"2026-03-02": 10.5})
    rates = load_rates({date(2026, 3, 2)})
    assert rates["2026-03-02"] == 10.5


# ---------------------------------------------------------------------------
# Gap handling: the series is business-day only (macsetup-50u0)
# ---------------------------------------------------------------------------


def test_a_weekend_is_never_requested(spy_api):
    """No observation exists for a Saturday, so asking for one is pure latency."""
    calls = spy_api({})
    # Mon 2026-01-05 through Fri 2026-01-09, then the weekend that follows.
    save_exchange_rates(
        {(date(2026, 1, 5) + timedelta(days=i)).isoformat(): 10.0 for i in range(5)}
    )
    load_rates({date(2026, 1, 10), date(2026, 1, 11)})
    assert calls == []


def test_a_holiday_is_negative_cached_and_not_re_requested(spy_api):
    """Thu 2026-01-01: a business day the series has no rate for."""
    calls = spy_api({"2025-12-31": 10.0, "2026-01-02": 10.5})
    wanted = {date(2026, 1, 1), date(2026, 1, 2)}

    load_rates(wanted)
    assert len(calls) == 1
    assert get_exchange_rates("2026-01-01")["2026-01-01"] == _NO_OBSERVATION

    load_rates(wanted)
    assert len(calls) == 1, "the holiday was requested a second time"


def test_a_settled_corpus_stops_asking_the_api(spy_api):
    """The regression: weekend and holiday gaps kept every run refetching.

    _find_missing_range counted every date with no row as missing, and no
    weekend ever gets one, so the span ran from the corpus's oldest Saturday to
    today on every single invocation.
    """
    fortnight = [date(2026, 1, 5) + timedelta(days=i) for i in range(14)]
    calls = spy_api(
        {
            d.isoformat(): 10.0
            for d in fortnight
            # Thu 2026-01-08 stands in for a public holiday.
            if d.weekday() < 5 and d != date(2026, 1, 8)
        }
    )

    load_rates(set(fortnight))
    assert len(calls) == 1
    load_rates(set(fortnight))
    assert len(calls) == 1, "a settled span was requested again"


def test_a_recent_business_day_is_not_negative_cached(spy_api):
    """Today's rate is published this afternoon; absence is 'not yet', not 'never'."""
    recent = today_oslo() - timedelta(days=1)
    while recent.weekday() >= 5:
        recent -= timedelta(days=1)
    calls = spy_api({(recent - timedelta(days=7)).isoformat(): 10.0})

    load_rates({recent})
    assert recent.isoformat() not in get_exchange_rates("2000-01-01"), (
        "a date the API could still publish was negative-cached"
    )
    load_rates({recent})
    assert len(calls) == 2, "the unpublished date was not retried"


def test_an_unreachable_api_records_no_gaps(no_api):
    """An empty reply and a dead network are indistinguishable, so neither teaches."""
    save_exchange_rates({"2026-01-02": 10.0})
    load_rates({date(2026, 1, 5)})
    assert get_exchange_rates("2000-01-01") == {"2026-01-02": 10.0}


def test_a_rejected_observation_is_not_negative_cached(fake_api):
    """It exists and we refused it — the opposite of 'there is nothing here'."""
    fake_api({"2026-01-02": 10.0, "2026-01-05": 0.0})
    load_rates({date(2026, 1, 2), date(2026, 1, 5)})
    assert "2026-01-05" not in get_exchange_rates("2000-01-01")


def test_a_gap_row_never_reaches_a_lookup(spy_api):
    """A sentinel read back as a rate would price a whole day at zero."""
    spy_api({"2025-12-31": 10.0, "2026-01-02": 10.5})
    load_rates({date(2026, 1, 1), date(2026, 1, 2)})
    rates = load_rates({date(2026, 1, 1), date(2026, 1, 2)})
    assert _NO_OBSERVATION not in rates.values()
    assert get_rate(rates, date(2026, 1, 1)) == (10.0, False)


# ---------------------------------------------------------------------------
# Prefetch
# ---------------------------------------------------------------------------


def _cache_recent_days(n: int) -> None:
    today = today_oslo()
    save_exchange_rates(
        {(today - timedelta(days=i)).isoformat(): 10.0 for i in range(n + 1)}
    )


def test_prefetch_is_skipped_when_the_recent_tail_is_cached(spy_api):
    calls = spy_api({})
    _cache_recent_days(exchange._PREFETCH_LOOKBACK_DAYS + exchange._MAX_WALKBACK_DAYS)
    assert exchange.start_prefetch() is None
    assert calls == []


def test_prefetch_spares_load_rates_a_second_call(spy_api):
    today = today_oslo()
    calls = spy_api(
        {(today - timedelta(days=i)).isoformat(): 10.0 for i in range(1, 40)}
    )
    pending = exchange.start_prefetch()
    assert pending is not None

    rates = load_rates({today - timedelta(days=1)}, pending)
    assert len(calls) == 1, "load_rates refetched what the prefetch already had"
    assert rates[(today - timedelta(days=1)).isoformat()] == 10.0


def test_prefetch_that_covers_nothing_leaves_load_rates_to_fetch(spy_api):
    """An old corpus is outside the speculative window; it still gets its rates."""
    today = today_oslo()
    calls = spy_api(
        {(today - timedelta(days=i)).isoformat(): 10.0 for i in range(1, 40)}
        | {"2026-01-02": 10.5}
    )
    pending = exchange.start_prefetch()
    assert pending is not None

    rates = load_rates({date(2026, 1, 2)}, pending)
    assert len(calls) == 2
    assert rates["2026-01-02"] == 10.5


def test_exchange_holds_no_sql():
    """The DAL owns the SQL; exchange.py talks to it through cache_db."""
    src = exchange.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "sqlite3" not in text
    assert "get_connection" not in text
    assert "SELECT" not in text
    assert cache_db.get_exchange_rates is exchange.cache_db.get_exchange_rates
