"""Tests for pricing.py — pure cost calculation and lookup functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _narrow import present

from ccreport import pricing
from ccreport.pricing import (
    MODEL_ALIASES,
    OTHER_FAMILY,
    PRICING_HISTORY,
    ROLLING_WINDOWS,
    SESSION_WINDOW_S,
    TIER_THRESHOLD,
    WEEK_WINDOW_S,
    _bucket_rolling_cost,
    _FileContext,
    _parse_effective,
    _parse_window_starts,
    _rec_cost,
    _rolling_thresholds,
    _scan_jsonl_file,
    _try_cached_file,
    calc_cost,
    extract_assistant_fields,
    find_pricing,
    model_family,
    tiered_cost,
    window_start_epoch,
)


class TestParseEffective:
    def test_date_only(self):
        dt = _parse_effective("2025-01-01")
        assert dt == datetime(2025, 1, 1, tzinfo=UTC)

    def test_date_with_hour(self):
        dt = _parse_effective("2026-03-13T18")
        assert dt == datetime(2026, 3, 13, 18, tzinfo=UTC)

    def test_returns_utc(self):
        dt = _parse_effective("2025-06-15")
        assert dt.tzinfo == UTC


class TestFindPricing:
    def test_exact_model_name(self):
        prices = find_pricing("claude-sonnet-4-20250514")
        assert prices is not None
        assert "input" in prices
        assert "output" in prices

    def test_alias_resolution(self):
        for alias in MODEL_ALIASES:
            prices = find_pricing(alias)
            assert prices is not None, f"Alias {alias!r} returned None"

    def test_unknown_model_returns_none(self):
        assert find_pricing("nonexistent-model-xyz") is None

    def test_historical_lookup_before_first_period(self):
        very_old = datetime(2020, 1, 1, tzinfo=UTC)
        assert find_pricing("claude-sonnet-4-20250514", ts=very_old) is None

    def test_historical_lookup_in_first_period(self):
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        prices = find_pricing("claude-sonnet-4-20250514", ts=ts)
        assert prices is not None
        assert prices["input"] == 3e-06

    def test_pricing_period_transition(self):
        """Opus 4.6 had 200k tier before 2026-03-13T18, flat after."""
        before = datetime(2026, 3, 13, 17, tzinfo=UTC)
        after = datetime(2026, 3, 13, 19, tzinfo=UTC)

        prices_before = find_pricing("claude-opus-4-6", ts=before)
        prices_after = find_pricing("claude-opus-4-6", ts=after)

        assert prices_before is not None
        assert prices_after is not None
        # Before: had 200k tier keys
        assert "input_200k" in prices_before
        # After: flat pricing, no 200k keys
        assert "input_200k" not in prices_after

    def test_substring_matching(self):
        """Models with version suffixes should match via substring."""
        prices = find_pricing("claude-opus-4-6")
        assert prices is not None


class TestTieredCost:
    def test_below_threshold_no_tier(self):
        cost = tiered_cost(100_000, 3e-06, 6e-06)
        assert cost == pytest.approx(100_000 * 3e-06)

    def test_below_threshold_with_none_tier(self):
        cost = tiered_cost(100_000, 3e-06, None)
        assert cost == pytest.approx(100_000 * 3e-06)

    def test_at_threshold_exact(self):
        cost = tiered_cost(TIER_THRESHOLD, 3e-06, 6e-06)
        assert cost == pytest.approx(TIER_THRESHOLD * 3e-06)

    def test_above_threshold_with_tier(self):
        count = TIER_THRESHOLD + 50_000
        cost = tiered_cost(count, 3e-06, 6e-06)
        expected = TIER_THRESHOLD * 3e-06 + 50_000 * 6e-06
        assert cost == pytest.approx(expected)

    def test_above_threshold_none_tier_uses_base(self):
        count = TIER_THRESHOLD + 50_000
        cost = tiered_cost(count, 3e-06, None)
        assert cost == pytest.approx(count * 3e-06)

    def test_zero_tokens(self):
        assert tiered_cost(0, 3e-06, 6e-06) == 0.0

    def test_one_token(self):
        assert tiered_cost(1, 5e-06, 10e-06) == pytest.approx(5e-06)


class TestCalcCost:
    def test_unknown_model_returns_zero(self):
        assert calc_cost(1000, 1000, 1000, 1000, "nonexistent") == 0.0

    def test_basic_cost_calculation(self):
        """Verify cost with known Sonnet 4 pricing (first period)."""
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        cost = calc_cost(
            input_tokens=10_000,
            output_tokens=5_000,
            cache_create_tokens=0,
            cache_read_tokens=0,
            model="claude-sonnet-4-20250514",
            ts=ts,
        )
        # input: 10_000 * 3e-06 = 0.03, output: 5_000 * 15e-06 = 0.075
        assert cost == pytest.approx(0.105)

    def test_all_token_types(self):
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        cost = calc_cost(
            input_tokens=1000,
            output_tokens=1000,
            cache_create_tokens=1000,
            cache_read_tokens=1000,
            model="claude-sonnet-4-20250514",
            ts=ts,
        )
        expected = (
            1000 * 3e-06  # input
            + 1000 * 15e-06  # output
            + 1000 * 3.75e-06  # cache_create
            + 1000 * 0.3e-06  # cache_read
        )
        assert cost == pytest.approx(expected)

    def test_tiered_pricing_kicks_in(self):
        """With 250K input tokens on Sonnet 4, tiered rate should apply."""
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        cost = calc_cost(
            input_tokens=250_000,
            output_tokens=0,
            cache_create_tokens=0,
            cache_read_tokens=0,
            model="claude-sonnet-4-20250514",
            ts=ts,
        )
        expected = TIER_THRESHOLD * 3e-06 + 50_000 * 6e-06
        assert cost == pytest.approx(expected)

    def test_no_tiered_rate_uses_flat(self):
        """Haiku has no 200k tier — flat rate for all tokens."""
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        cost = calc_cost(
            input_tokens=250_000,
            output_tokens=0,
            cache_create_tokens=0,
            cache_read_tokens=0,
            model="claude-haiku-4-5-20251001",
            ts=ts,
        )
        assert cost == pytest.approx(250_000 * 1e-06)

    def test_zero_tokens(self):
        assert calc_cost(0, 0, 0, 0, "claude-sonnet-4-20250514") == 0.0

    def test_flat_pricing_after_transition(self):
        """After 2026-03-13T18, Opus 4.6 uses flat pricing."""
        ts = datetime(2026, 4, 1, tzinfo=UTC)
        cost = calc_cost(
            input_tokens=250_000,
            output_tokens=0,
            cache_create_tokens=0,
            cache_read_tokens=0,
            model="claude-opus-4-6",
            ts=ts,
        )
        # No tiered rate → all at base rate
        assert cost == pytest.approx(250_000 * 5e-06)


class TestWindowStartEpoch:
    NOW = 1_700_000_000.0

    def test_past_reset_is_the_window_start(self):
        past = datetime.fromtimestamp(self.NOW - 600, tz=UTC).isoformat()
        assert window_start_epoch(past, SESSION_WINDOW_S, self.NOW) == self.NOW - 600

    def test_future_reset_subtracts_the_window(self):
        future = datetime.fromtimestamp(self.NOW + 3600, tz=UTC).isoformat()
        assert window_start_epoch(future, SESSION_WINDOW_S, self.NOW) == (
            self.NOW + 3600 - SESSION_WINDOW_S
        )
        assert window_start_epoch(future, WEEK_WINDOW_S, self.NOW) == (
            self.NOW + 3600 - WEEK_WINDOW_S
        )

    def test_naive_reset_counts_as_local_time(self):
        # Claude Code's stdin rate limits reach us as naive local ISO.
        naive = datetime.fromtimestamp(self.NOW + 3600).isoformat()  # noqa: DTZ006
        assert window_start_epoch(naive, SESSION_WINDOW_S, self.NOW) == (
            self.NOW + 3600 - SESSION_WINDOW_S
        )

    @pytest.mark.parametrize("value", [None, "", "not-a-date"])
    def test_unusable_input_returns_none(self, value):
        assert window_start_epoch(value, SESSION_WINDOW_S, self.NOW) is None

    def test_window_lengths(self):
        assert SESSION_WINDOW_S == 5 * 3600
        assert WEEK_WINDOW_S == 7 * 86400


class TestParseWindowStarts:
    def test_no_resets_returns_none_session_and_monday(self):
        session_start, week_start = _parse_window_starts(None, None)
        assert session_start is None
        assert week_start is not None
        # Week start should be a Monday at midnight
        assert week_start.weekday() == 0
        assert week_start.hour == 0
        assert week_start.minute == 0

    def test_past_session_reset_used_directly(self):
        past_iso = "2020-01-01T00:00:00+00:00"
        session_start, _ = _parse_window_starts(past_iso, None)
        assert session_start is not None
        assert session_start.year == 2020

    def test_future_session_reset_subtracts_window(self):
        future_iso = "2099-12-31T23:59:59+00:00"
        session_start, _ = _parse_window_starts(future_iso, None)
        assert session_start is not None
        # Should be 5 hours before the future reset
        expected_year = 2099
        assert session_start.year == expected_year

    def test_invalid_session_reset_returns_none(self):
        session_start, _ = _parse_window_starts("not-a-date", None)
        assert session_start is None

    def test_past_week_reset_used_directly(self):
        past_iso = "2020-01-06T00:00:00+00:00"
        _, week_start = _parse_window_starts(None, past_iso)
        assert week_start.year == 2020

    def test_future_week_reset_subtracts_7_days(self):
        future_iso = "2099-12-31T00:00:00+00:00"
        _, week_start = _parse_window_starts(None, future_iso)
        assert week_start.year == 2099
        assert week_start.day == 24  # 31 - 7

    def test_invalid_week_reset_falls_back_to_monday(self):
        _, week_start = _parse_window_starts(None, "garbage")
        assert week_start.weekday() == 0


class TestRollingHelpers:
    def test_thresholds_has_all_window_names(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        t = _rolling_thresholds(now)
        for w in ROLLING_WINDOWS:
            assert w.name in t

    def test_thresholds_are_ordered_oldest_first(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        t = _rolling_thresholds(now)
        values = [t[w.name] for w in ROLLING_WINDOWS]
        # ROLLING_WINDOWS is longest→shortest, so thresholds should be ascending
        assert values == sorted(values)

    def test_bucket_accumulates_into_all_matching_windows(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        t = _rolling_thresholds(now)
        totals: dict[str, float] = {}
        # Timestamp = now (within all windows)
        _bucket_rolling_cost(1.0, now.timestamp(), t, totals)
        for w in ROLLING_WINDOWS:
            assert totals[w.name] == 1.0

    def test_bucket_skips_windows_outside_range(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        t = _rolling_thresholds(now)
        totals: dict[str, float] = {}
        # Timestamp = 20 days ago (only in thirty_day and seven_day? No, 20 > 7)
        old_ts = (now - timedelta(days=20)).timestamp()
        _bucket_rolling_cost(1.0, old_ts, t, totals)
        assert totals.get("thirty_day") == 1.0
        assert totals.get("seven_day") is None
        assert totals.get("six_hour") is None

    def test_bucket_with_project(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        t = _rolling_thresholds(now)
        totals: dict[str, float] = {}
        proj: dict[str, float] = {}
        _bucket_rolling_cost(2.5, now.timestamp(), t, totals, proj, is_project=True)
        assert totals["six_hour"] == 2.5
        assert proj["six_hour"] == 2.5

    def test_bucket_no_project_when_false(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        t = _rolling_thresholds(now)
        totals: dict[str, float] = {}
        proj: dict[str, float] = {}
        _bucket_rolling_cost(2.5, now.timestamp(), t, totals, proj, is_project=False)
        assert totals["six_hour"] == 2.5
        assert proj.get("six_hour") is None


class TestRecCost:
    def test_returns_stored_cost_if_present(self):
        rec = {"cost": 1.23, "t": [100, 200, 300, 400], "model": "x"}
        assert _rec_cost(rec) == 1.23

    def test_zero_cost_recomputes(self):
        rec = {
            "cost": 0,
            "t": [10_000, 5_000, 0, 0],
            "model": "claude-sonnet-4-20250514",
            "ts": datetime(2025, 6, 1, tzinfo=UTC).timestamp(),
        }
        result = _rec_cost(rec)
        assert result > 0
        assert result == pytest.approx(0.105)  # 10k*3e-6 + 5k*15e-6

    def test_none_cost_recomputes(self):
        rec = {
            "cost": None,
            "t": [1000, 0, 0, 0],
            "model": "claude-sonnet-4-20250514",
            "ts": datetime(2025, 6, 1, tzinfo=UTC).timestamp(),
        }
        assert _rec_cost(rec) == pytest.approx(1000 * 3e-06)

    def test_missing_tokens_returns_zero(self):
        assert _rec_cost({}) == 0.0
        assert _rec_cost({"t": [1, 2]}) == 0.0
        assert _rec_cost({"t": None}) == 0.0

    def test_missing_ts_still_computes(self):
        rec = {"cost": None, "t": [1000, 0, 0, 0], "model": "claude-sonnet-4-20250514"}
        assert _rec_cost(rec) > 0


class TestPricingDataIntegrity:
    def test_pricing_history_chronological(self):
        """PRICING_HISTORY entries should be in chronological order."""
        dates = [_parse_effective(p["effective"]) for p in PRICING_HISTORY]
        for i in range(1, len(dates)):
            assert dates[i] > dates[i - 1], (
                f"Period {i} ({dates[i]}) is not after period {i - 1} ({dates[i - 1]})"
            )

    def test_all_models_have_required_keys(self):
        """Every model pricing must have at least input and output."""
        for period in PRICING_HISTORY:
            for model, prices in period["models"].items():
                assert "input" in prices, f"{model} missing 'input'"
                assert "output" in prices, f"{model} missing 'output'"

    def test_all_aliases_resolve_to_known_model(self):
        """Every alias target must exist in at least one pricing period."""
        all_models = set()
        for period in PRICING_HISTORY:
            all_models.update(period["models"].keys())
        for alias, target in MODEL_ALIASES.items():
            assert target in all_models, (
                f"Alias {alias!r} → {target!r} not in any period"
            )

    def test_tiered_rates_are_higher_than_base(self):
        """200k tier rates should be >= base rates (they're premiums)."""
        for period in PRICING_HISTORY:
            for model, prices in period["models"].items():
                for key in ("input", "output", "cache_create", "cache_read"):
                    tiered_key = f"{key}_200k"
                    if tiered_key in prices:
                        assert prices[tiered_key] >= prices[key], (
                            f"{model}.{tiered_key} ({prices[tiered_key]}) < "
                            f"{model}.{key} ({prices[key]})"
                        )

    def test_tier_threshold_is_200k(self):
        assert TIER_THRESHOLD == 200_000


class TestExtractAssistantFields:
    def _make_rec(self, **overrides):
        rec = {
            "type": "assistant",
            "timestamp": "2025-06-15T12:00:00+00:00",
            "requestId": "req-1",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            },
        }
        rec.update(overrides)
        return rec

    def test_valid_record(self):
        result = extract_assistant_fields(self._make_rec())
        assert result is not None
        _msg, usage, mid, rid, dk, ts = result
        assert mid == "msg-1"
        assert rid == "req-1"
        assert dk == "msg-1:req-1"
        assert usage["input_tokens"] == 100
        assert ts.tzinfo is not None

    def test_non_assistant_returns_none(self):
        assert extract_assistant_fields(self._make_rec(type="human")) is None

    def test_missing_message_returns_none(self):
        rec = self._make_rec()
        rec["message"] = None
        assert extract_assistant_fields(rec) is None

    def test_missing_usage_returns_none(self):
        rec = self._make_rec()
        rec["message"]["usage"] = None
        assert extract_assistant_fields(rec) is None

    def test_invalid_timestamp_returns_none(self):
        assert extract_assistant_fields(self._make_rec(timestamp="nope")) is None

    def test_missing_ids_gives_none_dedup_key(self):
        rec = self._make_rec()
        rec["message"]["id"] = ""
        result = extract_assistant_fields(rec)
        assert result is not None
        assert result[4] is None  # dedup_key

    def test_naive_timestamp_gets_utc(self):
        rec = self._make_rec(timestamp="2025-06-15T12:00:00")
        result = extract_assistant_fields(rec)
        assert result is not None
        assert result[5].tzinfo is not None


class TestTryCachedFile:
    """Tests for the cache-hit helper extracted from compute_costs."""

    NOW = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)

    @pytest.fixture
    def thresholds(self):
        return _rolling_thresholds(self.NOW)

    @pytest.fixture
    def accumulators(self):
        return {"rolling": {}, "proj": {}, "seen": set()}

    def _make_ctx(self, **overrides) -> _FileContext:
        defaults = {
            "key": "/tmp/test.jsonl",
            "is_session_file": False,
            "is_project_file": False,
            "in_session_window": False,
            "in_rolling_window": False,
            "file_unchanged": True,
            "ccr_fresh": False,
        }
        defaults.update(overrides)
        return _FileContext(**defaults)

    def _make_cached(self, **overrides) -> dict:
        defaults = {
            "mtime_ns": 100,
            "size": 200,
            "week_cost": 1.5,
            "month_cost": 2.0,
            "all_time_cost": 10.0,
            "session_cost": 0.5,
            "dedup_keys": ["k1", "k2"],
        }
        defaults.update(overrides)
        return defaults

    def test_returns_none_when_file_changed(self, thresholds, accumulators):
        ctx = self._make_ctx(file_unchanged=False)
        result = _try_cached_file(
            ctx,
            self._make_cached(),
            {},
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            accumulators["rolling"],
            accumulators["proj"],
        )
        assert result is None

    def test_returns_none_when_no_cache_entry(self, thresholds, accumulators):
        ctx = self._make_ctx()
        result = _try_cached_file(
            ctx,
            None,
            {},
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            accumulators["rolling"],
            accumulators["proj"],
        )
        assert result is None

    def test_cache_hit_outside_all_windows(self, thresholds, accumulators):
        ctx = self._make_ctx(in_rolling_window=False, in_session_window=False)
        rolling = accumulators["rolling"]
        result = _try_cached_file(
            ctx,
            self._make_cached(),
            {},
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            rolling,
            accumulators["proj"],
        )
        assert result is not None
        assert result.week == 1.5
        assert result.month == 2.0
        assert result.session_window == 0.0
        assert rolling["all_time"] == 10.0
        assert accumulators["seen"] == {"k1", "k2"}

    def test_cache_hit_session_file_returns_session_cost(
        self, thresholds, accumulators
    ):
        ctx = self._make_ctx(is_session_file=True)
        result = _try_cached_file(
            ctx,
            self._make_cached(),
            {},
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            accumulators["rolling"],
            accumulators["proj"],
        )
        assert result is not None
        assert result.session == 0.5

    def test_cache_hit_non_session_file_zero_session(self, thresholds, accumulators):
        ctx = self._make_ctx(is_session_file=False)
        result = _try_cached_file(
            ctx,
            self._make_cached(),
            {},
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            accumulators["rolling"],
            accumulators["proj"],
        )
        assert result is not None
        assert result.session == 0.0

    def test_returns_none_when_in_rolling_and_no_ccr(self, thresholds, accumulators):
        ctx = self._make_ctx(in_rolling_window=True, ccr_fresh=False)
        result = _try_cached_file(
            ctx,
            self._make_cached(),
            {},
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            accumulators["rolling"],
            accumulators["proj"],
        )
        assert result is None

    def test_branch3_ccr_fresh_computes_rolling(self, thresholds, accumulators):
        now_ts = self.NOW.timestamp()
        ctx = self._make_ctx(
            in_rolling_window=True,
            ccr_fresh=True,
            is_project_file=True,
        )
        ccr_records = {
            ctx.key: [
                {
                    "dk": "r1",
                    "ts": now_ts,
                    "cost": 3.0,
                    "t": [1, 1, 0, 0],
                    "model": "x",
                },
            ]
        }
        rolling = accumulators["rolling"]
        proj = accumulators["proj"]
        result = _try_cached_file(
            ctx,
            self._make_cached(),
            ccr_records,
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            rolling,
            proj,
        )
        assert result is not None
        assert result.week == 1.5  # from cache
        assert rolling.get("six_hour", 0.0) == 3.0
        assert proj.get("six_hour", 0.0) == 3.0
        assert "r1" in accumulators["seen"]

    def test_branch3_skips_duplicate_keys(self, thresholds, accumulators):
        now_ts = self.NOW.timestamp()
        ctx = self._make_ctx(in_rolling_window=True, ccr_fresh=True)
        accumulators["seen"].add("dup1")
        ccr_records = {
            ctx.key: [
                {
                    "dk": "dup1",
                    "ts": now_ts,
                    "cost": 5.0,
                    "t": [1, 1, 0, 0],
                    "model": "x",
                },
            ]
        }
        rolling = accumulators["rolling"]
        _try_cached_file(
            ctx,
            self._make_cached(),
            ccr_records,
            accumulators["seen"],
            thresholds,
            None,
            thresholds["thirty_day"],
            rolling,
            accumulators["proj"],
        )
        assert rolling.get("six_hour", 0.0) == 0.0


class TestScanJsonlFile:
    """Tests for the JSONL scanning helper extracted from compute_costs."""

    NOW = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)

    @pytest.fixture
    def thresholds(self):
        return _rolling_thresholds(self.NOW)

    def test_empty_file(self, tmp_path, thresholds):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        result = _scan_jsonl_file(
            f,
            is_session_file=False,
            session_window_start=None,
            week_window_start=self.NOW - timedelta(days=7),
            month_window_start=self.NOW.replace(day=1),
            thresholds=thresholds,
            seen_keys=set(),
        )
        assert result.all_time_cost == 0.0
        assert result.dedup_keys == []

    def test_nonexistent_file(self, tmp_path, thresholds):
        f = tmp_path / "nope.jsonl"
        result = _scan_jsonl_file(
            f,
            is_session_file=False,
            session_window_start=None,
            week_window_start=self.NOW - timedelta(days=7),
            month_window_start=self.NOW.replace(day=1),
            thresholds=thresholds,
            seen_keys=set(),
        )
        assert result.all_time_cost == 0.0


class TestTheRawParseHonoursALoggedCost:
    """A record's own `costUSD` outranks the token calculation, as it does for
    `aggregate.UsageRecord.cost` and for the cached-record readers.
    """

    TS = "2025-06-15T12:00:00+00:00"

    def _file(self, tmp_path, **extra):
        import json

        line = {
            "type": "assistant",
            "timestamp": self.TS,
            "requestId": "req-1",
            "sessionId": "s1",
            "message": {
                "id": "msg-1",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 1000, "output_tokens": 0},
            },
            **extra,
        }
        path = tmp_path / "a.jsonl"
        path.write_text(json.dumps(line) + "\n")
        return path

    def _from_tokens(self) -> float:
        return calc_cost(1000, 0, 0, 0, "claude-opus-5",
                         datetime.fromisoformat(self.TS))

    def test_a_logged_cost_is_taken_as_it_stands(self, tmp_path):
        [(cost, _ts, _dk, _m)] = _iter_live(self._file(tmp_path, costUSD=2.5))
        assert cost == 2.5

    def test_without_one_the_tokens_price_it(self, tmp_path):
        [(cost, _ts, _dk, _m)] = _iter_live(self._file(tmp_path))
        assert cost == self._from_tokens()

    def test_an_unparseable_one_falls_back_to_the_tokens(self, tmp_path):
        [(cost, _ts, _dk, _m)] = _iter_live(self._file(tmp_path, costUSD="lots"))
        assert cost == self._from_tokens()


# ---------------------------------------------------------------------------
# compute_costs — session-window key presence
# ---------------------------------------------------------------------------


class TestComputeCostsSessionWindowKey:
    """session_window_cost must be absent, not 0.0, without a reset time.

    Callers merge the result over existing data, so a placeholder zero would
    overwrite a real total computed by a caller that had the reset.
    """

    @pytest.fixture
    def isolated(self, monkeypatch, tmp_path):
        from ccreport import cache_db, pricing

        projects = tmp_path / "projects"
        projects.mkdir()
        (projects / "chat.jsonl").write_text("")
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [projects])
        monkeypatch.setattr(cache_db, "load_cost_cache", lambda *a, **k: {})
        monkeypatch.setattr(cache_db, "bulk_load_ccreport_cache", lambda: ({}, {}))
        monkeypatch.setattr(cache_db, "bulk_save_file_costs", lambda *a, **k: None)
        written: list[dict] = []
        monkeypatch.setattr(
            cache_db,
            "write_cost_summary",
            lambda costs, cwd=None: written.append(costs),
        )
        return written

    def test_omitted_without_reset(self, isolated):
        from ccreport.pricing import compute_costs

        result = compute_costs()
        assert "session_window_cost" not in result
        assert "week_cost" in result
        assert isolated
        assert "session_window_cost" not in isolated[-1]

    def test_present_with_reset(self, isolated):
        from ccreport.pricing import compute_costs

        reset = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
        result = compute_costs(session_reset_iso=reset)
        assert result["session_window_cost"] == 0.0
        assert "session_window_cost" in isolated[-1]


# ---------------------------------------------------------------------------
# compute_costs — incremental saves
# ---------------------------------------------------------------------------


class TestComputeCostsSavesOnlyChangedFiles:
    """One appended line must not rewrite the whole cost cache."""

    @pytest.fixture
    def projects(self, monkeypatch, tmp_path):
        from ccreport import pricing

        d = tmp_path / "projects" / "-tmp-proj"
        d.mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        return d

    def _append(self, path, mid: str) -> None:
        import json

        with open(path, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "requestId": f"req-{mid}",
                        "sessionId": "s1",
                        "cwd": "/tmp/proj",
                        "message": {
                            "id": mid,
                            "model": "claude-opus-5",
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        },
                    }
                )
                + "\n"
            )

    def _saves(self, monkeypatch) -> list:
        from ccreport import cache_db

        calls: list = []
        real = cache_db.bulk_save_file_costs

        def spy(entries, week_key, month_key, changed=None, **kwargs):
            calls.append(changed)
            real(entries, week_key, month_key, changed=changed, **kwargs)

        monkeypatch.setattr(cache_db, "bulk_save_file_costs", spy)
        return calls

    def test_only_the_grown_file_is_rewritten(self, projects, monkeypatch):
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        a, b = projects / "a.jsonl", projects / "b.jsonl"
        self._append(a, "msg-a")
        self._append(b, "msg-b")
        compute_costs()

        conn = cache_db.get_connection()
        # A key only the table knows about: it survives iff b's row is left alone.
        conn.execute(
            "INSERT INTO dedup_keys (dk, file_path) VALUES ('ghost', ?)", (str(b),)
        )
        conn.commit()

        calls = self._saves(monkeypatch)
        self._append(a, "msg-a2")
        compute_costs()

        assert calls == [{str(a)}]
        keys = dict(
            conn.execute("SELECT file_path, count(*) FROM dedup_keys GROUP BY 1")
        )
        assert keys[str(b)] == 2, "b keeps its own key and the ghost"
        assert keys[str(a)] == 2, "a is rebuilt from its two messages"

    def test_a_departed_file_leaves_the_cache(self, projects, monkeypatch):
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        a, b = projects / "a.jsonl", projects / "b.jsonl"
        self._append(a, "msg-a")
        self._append(b, "msg-b")
        compute_costs()

        b.unlink()
        compute_costs()
        conn = cache_db.get_connection()
        paths = {r[0] for r in conn.execute("SELECT path FROM file_costs")}
        assert paths == {str(a)}
        assert (
            conn.execute(
                "SELECT count(*) FROM dedup_keys WHERE file_path = ?", (str(b),)
            ).fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------------------
# Which records one project's windows are allowed to count
# ---------------------------------------------------------------------------


class TestProjectScoping:
    """A project's cost windows count that project's records and no others.

    The path prefix is the whole defence against a sibling directory whose name
    starts with the same string, and an orphaned record has only the prefix and
    the stored project name left to be found by.
    """

    CWD = "/tmp/proj"
    # cwd → projects-dir name is a "/"→"-" swap, so this sibling project's
    # directory has the target's directory name as a string prefix.
    SIBLING_CWD = "/tmp/proj-other"

    @pytest.fixture
    def projects_dir(self, monkeypatch, tmp_path):
        from ccreport import cache_db, pricing

        d = tmp_path / "projects"
        (d / "-tmp-proj").mkdir(parents=True)
        (d / "-tmp-proj-other").mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        cache_db.init_ccreport_meta(1, "test-hash")
        return d

    def _orphan(
        self, path: str, *, sid: str, cost: float, ts: float | None = None,
        project: str = "proj", cwd: str | None = None,
    ) -> None:
        """Cache a record under *path* with no file on disk to back it.

        *project* and *cwd* are the record's own, which is what an orphan is
        attributed by once its directory is gone.
        """
        from ccreport import cache_db

        cache_db.save_ccreport_file(
            path,
            1,
            1,
            [
                {
                    "mid": "m",
                    "model": "claude-opus-5",
                    "ts": ts if ts is not None else datetime.now(tz=UTC).timestamp(),
                    "sid": sid,
                    "project": project,
                    "cwd": cwd if cwd is not None else self.CWD,
                    "repo": None,
                    "dk": None,
                    "cost": cost,
                    "t": [1, 1, 0, 0],
                }
            ],
        )

    def test_orphaned_project_costs_still_reach_the_totals(self, projects_dir):
        from ccreport.pricing import compute_costs

        self._orphan(str(projects_dir / "-tmp-proj" / "gone.jsonl"), sid="s1", cost=3.0)
        totals = compute_costs(cwd=self.CWD)
        assert totals["all_time_project_cost"] == 3.0
        assert totals["twenty_four_hour_project_cost"] == 3.0

    def test_a_sibling_project_sharing_the_prefix_is_not_counted(self, projects_dir):
        """Each record carries its own project, so the directory is what could
        confuse the two: `-tmp-proj` is a bare string prefix of `-tmp-proj-other`.
        """
        from ccreport.pricing import compute_costs

        self._orphan(str(projects_dir / "-tmp-proj" / "gone.jsonl"), sid="s1", cost=3.0)
        self._orphan(
            str(projects_dir / "-tmp-proj-other" / "gone.jsonl"), sid="s1", cost=99.0,
            project="proj-other", cwd=self.SIBLING_CWD,
        )
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 3.0
        assert compute_costs(cwd=self.SIBLING_CWD)["all_time_project_cost"] == 99.0

    def test_a_live_file_is_not_also_counted_as_an_orphan(self, projects_dir):
        import json

        from ccreport.pricing import compute_costs

        live = projects_dir / "-tmp-proj" / "live.jsonl"
        live.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "requestId": "r1",
                    "sessionId": "s1",
                    "cwd": self.CWD,
                    "message": {
                        "id": "msg-1",
                        "model": "claude-opus-5",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                }
            )
            + "\n"
        )
        self._orphan(str(live), sid="s1", cost=50.0)
        # The cached row is for a path still on disk, so the JSONL scan owns it
        # and the orphan pass must skip it — 50.0 would be the double count.
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] < 1.0

    def test_the_purged_session_fallback_sums_only_that_session(self, projects_dir):
        from ccreport.pricing import compute_session_cost

        self._orphan(str(projects_dir / "-tmp-proj" / "a.jsonl"), sid="s1", cost=3.0)
        self._orphan(str(projects_dir / "-tmp-proj" / "b.jsonl"), sid="s2", cost=7.0)
        assert compute_session_cost("s1", self.CWD) == 3.0
        assert compute_session_cost("s2", self.CWD) == 7.0

    def test_the_purged_session_fallback_stays_inside_the_project(self, projects_dir):
        from ccreport.pricing import compute_session_cost

        # Same session id, other project's directory: the loader is not scoped
        # by path, so compute_session_cost's own prefix filter has to hold.
        self._orphan(str(projects_dir / "-tmp-proj" / "a.jsonl"), sid="s1", cost=3.0)
        self._orphan(
            str(projects_dir / "-tmp-proj-other" / "a.jsonl"), sid="s1", cost=99.0
        )
        assert compute_session_cost("s1", self.CWD) == 3.0

    def test_the_session_fallback_reads_no_other_sessions_rows(
        self,
        projects_dir,
        monkeypatch,
    ):
        from ccreport import cache_db
        from ccreport.pricing import compute_session_cost

        self._orphan(str(projects_dir / "-tmp-proj" / "a.jsonl"), sid="s1", cost=3.0)

        def no_full_scans():
            msg = "a render must not load the whole table"
            raise AssertionError(msg)

        monkeypatch.setattr(cache_db, "bulk_load_ccreport_cache", no_full_scans)
        assert compute_session_cost("s1", self.CWD) == 3.0

    def test_a_mismatched_salt_degrades_to_no_orphans(self, projects_dir):
        from ccreport import cache_db
        from ccreport.pricing import compute_costs, compute_session_cost

        self._orphan(str(projects_dir / "-tmp-proj" / "gone.jsonl"), sid="s1", cost=3.0)
        conn = cache_db.get_connection()
        cache_db._set_meta(conn, "ccreport_schema_salt", "not-the-salt")
        conn.commit()
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 0.0
        assert compute_session_cost("s1", self.CWD) == 0.0
        # Degraded, not repaired: the row a re-parse could never rebuild is intact.
        assert conn.execute("SELECT cost FROM ccreport_records").fetchone()[0] == 3.0


# ---------------------------------------------------------------------------
# One definition of which project a record belongs to
# ---------------------------------------------------------------------------


class TestPathInProject:
    def test_a_sibling_sharing_the_string_prefix_is_excluded(self):
        from pathlib import Path

        from ccreport.pricing import path_in_project, project_path_prefixes

        prefixes = project_path_prefixes("/tmp/proj", [Path("/root")])
        assert prefixes == ["/root/-tmp-proj/"]
        assert path_in_project("/root/-tmp-proj/s.jsonl", prefixes)
        # The separator every prefix ends in is the whole defence here, and it
        # is also what makes cache_db.prefix_range's bounds agree with this.
        assert not path_in_project("/root/-tmp-proj-other/s.jsonl", prefixes)

    def test_no_prefixes_matches_nothing(self):
        from ccreport.pricing import path_in_project

        assert not path_in_project("/root/-tmp-proj/s.jsonl", [])


class TestMergedProjectsShareTheirCostWindows:
    """`ccreport merge` has to mean one project for the statusline too.

    A merge used to regroup the reports and leave the per-project cost windows
    split, because pricing matched on a path prefix and a cwd basename and knew
    nothing about the override table.
    """

    CWD = "/tmp/proj"
    OTHER_CWD = "/tmp/other"

    @pytest.fixture
    def projects_dir(self, monkeypatch, tmp_path):
        from ccreport import cache_db, pricing

        d = tmp_path / "projects"
        (d / "-tmp-proj").mkdir(parents=True)
        (d / "-tmp-other").mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        cache_db.init_ccreport_meta(1, "test-hash")
        return d

    @staticmethod
    def _merge(monkeypatch, source: str, target: str, kind: str = "name") -> None:
        from ccreport import cache_db

        monkeypatch.setattr(
            cache_db,
            "get_project_overrides",
            lambda: [
                {"id": 1, "match_kind": kind, "match_value": source, "target": target},
            ],
        )

    @staticmethod
    def _orphan(path: str, *, project: str, cwd: str | None, cost: float) -> None:
        from ccreport import cache_db

        cache_db.save_ccreport_file(
            path,
            1,
            1,
            [
                {
                    "mid": "m",
                    "model": "claude-opus-5",
                    "ts": datetime.now(tz=UTC).timestamp(),
                    "sid": "s1",
                    "project": project,
                    "cwd": cwd,
                    "repo": None,
                    # Distinct per project: dedup is global, so a shared key would
                    # drop the second record and hide whether the scope found it.
                    "dk": f"m:{project}",
                    "cost": cost,
                    "t": [1, 1, 0, 0],
                }
            ],
        )

    def _both_projects(self, projects_dir) -> None:
        self._orphan(
            str(projects_dir / "-tmp-proj" / "gone.jsonl"),
            project="proj",
            cwd=self.CWD,
            cost=3.0,
        )
        self._orphan(
            str(projects_dir / "-tmp-other" / "gone.jsonl"),
            project="other",
            cwd=self.OTHER_CWD,
            cost=99.0,
        )

    def test_a_merged_projects_costs_reach_the_targets_window(
        self,
        projects_dir,
        monkeypatch,
    ):
        from ccreport.pricing import compute_costs

        self._both_projects(projects_dir)
        self._merge(monkeypatch, "other", "proj")
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 102.0

    def test_the_merged_side_lands_on_the_target_too(self, projects_dir, monkeypatch):
        """Standing in the directory that was merged away sees the same total."""
        from ccreport.pricing import compute_costs

        self._both_projects(projects_dir)
        self._merge(monkeypatch, "other", "proj")
        assert compute_costs(cwd=self.OTHER_CWD)["all_time_project_cost"] == 102.0

    def test_without_a_rule_the_two_projects_stay_apart(
        self, projects_dir, monkeypatch
    ):
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        self._both_projects(projects_dir)
        monkeypatch.setattr(cache_db, "get_project_overrides", list)
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 3.0
        assert compute_costs(cwd=self.OTHER_CWD)["all_time_project_cost"] == 99.0

    def test_an_unrelated_project_is_not_dragged_in(self, projects_dir, monkeypatch):
        from ccreport.pricing import compute_costs

        self._both_projects(projects_dir)
        (projects_dir / "-tmp-third").mkdir()
        self._orphan(
            str(projects_dir / "-tmp-third" / "gone.jsonl"),
            project="third",
            cwd="/tmp/third",
            cost=7.0,
        )
        self._merge(monkeypatch, "other", "proj")
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 102.0

    def test_a_purged_record_reaches_the_target_by_name_alone(
        self,
        projects_dir,
        monkeypatch,
    ):
        """No cwd, no repo, and a path outside every project dir — name only."""
        from ccreport.pricing import compute_costs

        self._orphan("/gone.jsonl", project="other", cwd=None, cost=42.0)
        self._merge(monkeypatch, "other", "proj")
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 42.0

    def test_the_cwds_own_name_is_resolved_before_the_comparison(
        self,
        projects_dir,
        monkeypatch,
    ):
        """Both sides go through the rules, so a merge of the cwd itself holds."""
        from ccreport.pricing import compute_costs

        self._orphan("/gone.jsonl", project="archive", cwd=None, cost=5.0)
        # The cwd's own project is what gets merged away this time.
        self._merge(monkeypatch, "proj", "archive")
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 5.0

    def test_the_table_is_read_once_per_computation(self, projects_dir, monkeypatch):
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        self._both_projects(projects_dir)
        reads = []
        rules = [
            {"id": 1, "match_kind": "name", "match_value": "other", "target": "proj"}
        ]

        def counted():
            reads.append(1)
            return rules

        monkeypatch.setattr(cache_db, "get_project_overrides", counted)
        compute_costs(cwd=self.CWD)
        assert reads == [1], "one read per compute, not one per record"


class TestTheResolvedScopeIsCachedPerCwd:
    """Resolving a merged scope reads every cached file's identity.

    That GROUP BY was 0.020s of an 0.085s statusline call, repeated on every
    render for an answer that only moves when a rule or a record does. These
    tests pin both halves: the second render skips the scan, and a change to
    either input still reaches it.
    """

    CWD = "/tmp/proj"

    @pytest.fixture
    def merged(self, monkeypatch, tmp_path):
        """One record in a second project, merged into the cwd's by name."""
        from ccreport import cache_db, pricing

        d = tmp_path / "projects"
        (d / "-tmp-proj").mkdir(parents=True)
        (d / "-tmp-other").mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        cache_db.init_ccreport_meta(1, "test-hash")
        cache_db.save_ccreport_file(
            str(d / "-tmp-other" / "gone.jsonl"),
            1,
            1,
            [
                {
                    "mid": "m",
                    "model": "claude-opus-5",
                    "ts": 1.5,
                    "sid": "s1",
                    "project": "other",
                    "cwd": "/tmp/other",
                    "repo": None,
                    "dk": "dk1",
                    "cost": 1.0,
                    "t": [1, 1, 0, 0],
                }
            ],
        )
        cache_db.add_project_override("name", "other", "proj")
        return d

    @staticmethod
    def _merged_prefix(projects_dir) -> str:
        return str(projects_dir / "-tmp-other") + "/"

    @staticmethod
    def _scope_rows() -> int:
        from ccreport import cache_db

        return (
            cache_db.get_connection()
            .execute("SELECT COUNT(*) FROM project_scopes")
            .fetchone()[0]
        )

    def test_a_second_call_answers_without_rescanning_the_identities(
        self,
        merged,
        monkeypatch,
    ):
        from ccreport import pricing

        scans = []
        real = pricing._file_identities
        monkeypatch.setattr(
            pricing,
            "_file_identities",
            lambda: (scans.append(1), real())[1],
        )
        first = pricing.project_scope(self.CWD, [merged])
        second = pricing.project_scope(self.CWD, [merged])
        assert scans == [1], "the identities are scanned once, not once per call"
        assert (second.name, second.prefixes) == (first.name, first.prefixes)
        # The merged directory is the part only the scan could have found, so
        # its survival is what says the cached answer is the real one.
        assert self._merged_prefix(merged) in second.prefixes

    def test_a_rule_change_rederives_the_scope(self, merged):
        from ccreport import cache_db, pricing

        assert pricing.project_scope(self.CWD, [merged]).name == "proj"
        cache_db.delete_project_override("other")
        cache_db.add_project_override("name", "proj", "archive")
        again = pricing.project_scope(self.CWD, [merged])
        assert again.name == "archive"
        assert self._merged_prefix(merged) not in again.prefixes

    def test_a_newly_cached_record_rederives_the_scope(self, merged, tmp_path):
        from ccreport import cache_db, pricing

        (tmp_path / "projects" / "-tmp-third").mkdir()
        assert (
            self._merged_prefix(merged)
            in pricing.project_scope(self.CWD, [merged]).prefixes
        )
        cache_db.add_project_override("name", "third", "proj")
        cache_db.save_ccreport_file(
            str(merged / "-tmp-third" / "gone.jsonl"),
            1,
            1,
            [
                {
                    "mid": "m2",
                    "model": "claude-opus-5",
                    "ts": 1.5,
                    "sid": "s2",
                    "project": "third",
                    "cwd": "/tmp/third",
                    "repo": None,
                    "dk": "dk2",
                    "cost": 1.0,
                    "t": [1, 1, 0, 0],
                }
            ],
        )
        assert (
            str(merged / "-tmp-third") + "/"
            in pricing.project_scope(self.CWD, [merged]).prefixes
        )

    def test_with_no_rules_nothing_is_cached(self, merged):
        from ccreport import cache_db, pricing

        cache_db.delete_project_override("other")
        assert pricing.project_scope(self.CWD, [merged]).name == "proj"
        # Without rules the scope is the cwd's own directory and costs one
        # table read; a row here would be an invalidation liability for nothing.
        assert self._scope_rows() == 0

    def test_a_scope_that_predates_a_projects_dir_is_rederived(
        self,
        merged,
        tmp_path,
    ):
        from ccreport import pricing

        pricing.project_scope(self.CWD, [merged])
        second = tmp_path / "projects2"
        (second / "-tmp-proj").mkdir(parents=True)
        scope = pricing.project_scope(self.CWD, [merged, second])
        assert str(second / "-tmp-proj") + "/" in scope.prefixes

    def test_a_failing_cache_write_still_yields_the_scope(self, merged, monkeypatch):
        import sqlite3

        from ccreport import cache_db, pricing

        def locked(*_args, **_kw):
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(cache_db, "save_project_scope", locked)
        scope = pricing.project_scope(self.CWD, [merged])
        assert scope.name == "proj"
        assert self._merged_prefix(merged) in scope.prefixes


# ---------------------------------------------------------------------------
# dk-NULL records still have to dedupe
# ---------------------------------------------------------------------------


class TestFallbackDedupIdentity:
    """dk is NULL when the source log carried no message id or requestId.

    Read-time dedup used to skip those records entirely, so a duplicate row
    among them counted twice into every cost total it reached.
    """

    CWD = "/tmp/proj"

    @pytest.fixture
    def projects_dir(self, monkeypatch, tmp_path):
        from ccreport import cache_db, pricing

        d = tmp_path / "projects"
        (d / "-tmp-proj").mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        monkeypatch.setattr(cache_db, "get_project_overrides", list)
        cache_db.init_ccreport_meta(1, "test-hash")
        return d

    def _rows(self, projects_dir, *rows: dict) -> None:
        from ccreport import cache_db

        ts = datetime.now(tz=UTC).timestamp()
        cache_db.save_ccreport_file(
            str(projects_dir / "-tmp-proj" / "gone.jsonl"),
            1,
            1,
            [
                {
                    "mid": "m",
                    "model": "claude-opus-5",
                    "ts": ts,
                    "sid": "s1",
                    "project": "proj",
                    "cwd": self.CWD,
                    "repo": None,
                    "dk": None,
                    "cost": 1.0,
                    "t": [1, 1, 0, 0],
                    **r,
                }
                for r in rows
            ],
        )

    def test_two_identical_dk_null_rows_count_once(self, projects_dir):
        from ccreport.pricing import compute_costs

        self._rows(projects_dir, {}, {})
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 1.0

    def test_one_differing_token_count_keeps_them_apart(self, projects_dir):
        """A streaming message's chunks differ exactly here."""
        from ccreport.pricing import compute_costs

        self._rows(projects_dir, {"t": [1, 1, 0, 0]}, {"t": [1, 2, 0, 0]})
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 2.0

    def test_a_different_session_keeps_them_apart(self, projects_dir):
        from ccreport.pricing import compute_costs

        self._rows(projects_dir, {}, {"sid": "s2"})
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 2.0

    def test_rows_carrying_a_dedup_key_are_untouched(self, projects_dir):
        from ccreport.pricing import compute_costs

        self._rows(projects_dir, {"dk": "m:r1"}, {"dk": "m:r2"})
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 2.0

    def test_the_same_dedup_key_still_collapses(self, projects_dir):
        from ccreport.pricing import compute_costs

        self._rows(projects_dir, {"dk": "m:r1"}, {"dk": "m:r1"})
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 1.0

    def test_the_purged_session_fallback_dedupes_the_same_way(self, projects_dir):
        from ccreport.pricing import compute_session_cost

        self._rows(projects_dir, {}, {})
        assert compute_session_cost("s1", self.CWD) == 1.0

    def test_a_live_jsonl_scan_dedupes_the_same_way(self, projects_dir):
        import json

        from ccreport.pricing import compute_costs

        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "sessionId": "s1",
                "cwd": self.CWD,
                "message": {
                    "id": "msg-1",
                    "model": "claude-opus-5",
                    "usage": {"input_tokens": 1000, "output_tokens": 0},
                },
            }
        )
        live = projects_dir / "-tmp-proj" / "live.jsonl"
        # No requestId, so extract_assistant_fields leaves dk NULL.
        live.write_text(line + "\n" + line + "\n")
        once = 1000 * 5e-06
        assert compute_costs(cwd=self.CWD)[
            "all_time_project_cost"
        ] == round(once, 4)


# ---------------------------------------------------------------------------
# An unchanged live file is priced from the cache, not re-read
# ---------------------------------------------------------------------------


class TestLiveFilesPricedFromTheCcreportCache:
    """A file whose (mtime_ns, size) still matches is summed from its cached
    records instead of re-read, which only holds while both paths total the
    same. `TestTryCachedFile` covers the branches; these run the whole
    computation twice over one corpus.
    """

    CWD = "/tmp/proj"

    @pytest.fixture
    def projects_dir(self, monkeypatch, tmp_path):
        from ccreport import cache_db, pricing

        d = tmp_path / "projects"
        (d / "-tmp-proj").mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        monkeypatch.setattr(cache_db, "get_project_overrides", list)
        cache_db.init_ccreport_meta(1, "test-hash")
        return d

    @staticmethod
    def _record(mid: str, **kw) -> dict:
        """One cached record, in the shape cache_db stores and pricing reads.

        `cost` is None as scan.py leaves it for a log with no `costUSD`, which is
        almost every log; a test that wants one sets it and _line emits it.
        """
        return {
            "mid": mid,
            "model": "claude-opus-5",
            "ts": datetime.now(tz=UTC).timestamp(),
            "sid": "s1",
            "project": "proj",
            "cwd": "/tmp/proj",
            "repo": None,
            "dk": f"{mid}:req-1",
            "cost": None,
            "t": [1000, 500, 0, 0],
            **kw,
        }

    @staticmethod
    def _line(rec: dict) -> str:
        """The JSONL line a raw parse must read *rec* back out of."""
        import json

        line: dict = {
            "type": "assistant",
            "timestamp": datetime.fromtimestamp(rec["ts"], tz=UTC).isoformat(),
            "sessionId": rec["sid"],
            "cwd": rec["cwd"],
            "message": {
                "id": rec["mid"],
                "model": rec["model"],
                "usage": {
                    "input_tokens": rec["t"][0],
                    "output_tokens": rec["t"][1],
                    "cache_creation_input_tokens": rec["t"][2],
                    "cache_read_input_tokens": rec["t"][3],
                },
            },
        }
        if rec["dk"]:
            line["requestId"] = rec["dk"].split(":", 1)[1]
        if rec["cost"] is not None:
            line["costUSD"] = rec["cost"]
        return json.dumps(line)

    def _file(
        self,
        projects_dir,
        name: str,
        records: list[dict],
        *,
        cached: bool = True,
        fresh: bool = True,
    ):
        """Write *records* as a JSONL file, optionally caching them for it."""
        from ccreport import cache_db

        path = projects_dir / "-tmp-proj" / name
        path.write_text("".join(self._line(r) + "\n" for r in records))
        if cached:
            st = path.stat()
            fp = (st.st_mtime_ns, st.st_size) if fresh else (1, 1)
            cache_db.save_ccreport_file(str(path), *fp, records)
        return path

    @staticmethod
    def _expected(*records: dict) -> float:
        """What both paths owe: every record priced from its own tokens."""
        from ccreport.pricing import _rec_cost_from_tokens

        return round(sum(_rec_cost_from_tokens(r) for r in records), 4)

    def _mixed_project(self, projects_dir) -> list[dict]:
        """Three files: cached and fresh, uncached, cached but stale."""
        fresh = [self._record("msg-a"), self._record("msg-b")]
        uncached = [self._record("msg-c")]
        stale = [self._record("msg-d")]
        self._file(projects_dir, "a.jsonl", fresh)
        self._file(projects_dir, "b.jsonl", uncached, cached=False)
        self._file(projects_dir, "c.jsonl", stale, fresh=False)
        return [*fresh, *uncached, *stale]

    def test_the_warm_caches_total_what_the_cold_ones_did(self, projects_dir):
        """The first call fills file_costs; the second answers from it."""
        from ccreport.pricing import compute_costs

        records = self._mixed_project(projects_dir)
        cold = compute_costs(cwd=self.CWD)
        assert cold == compute_costs(cwd=self.CWD)
        assert cold["all_time_project_cost"] == self._expected(*records)

    def test_a_logged_cost_is_what_both_paths_total(self, projects_dir):
        """A `costUSD` in the log is the record's cost on either path.

        The parse used to price from tokens while the cached record kept the
        logged figure, so one warm window read higher than all_time.
        """
        from ccreport.pricing import compute_costs

        self._file(projects_dir, "a.jsonl", [self._record("msg-a", cost=2.5)])
        cold = compute_costs(cwd=self.CWD)
        assert cold["all_time_project_cost"] == 2.5
        assert cold["twenty_four_hour_project_cost"] == 2.5
        assert compute_costs(cwd=self.CWD) == cold

    @pytest.mark.parametrize("cached_first", [True, False])
    def test_one_message_in_two_files_counts_once_across_the_two_paths(
        self,
        projects_dir,
        cached_first,
    ):
        """seen_keys is shared, so which path reads the twin cannot matter."""
        from ccreport.pricing import compute_costs

        rec = self._record("msg-a")
        first, second = (
            ("a.jsonl", "b.jsonl") if cached_first else ("b.jsonl", "a.jsonl")
        )
        self._file(projects_dir, first, [rec])
        self._file(projects_dir, second, [rec], cached=False)
        assert compute_costs(cwd=self.CWD)[
            "all_time_project_cost"
        ] == self._expected(rec)

    def test_an_orphans_stored_cost_still_wins(self, projects_dir):
        """No JSONL left to re-price from, so the stored cost is the only truth."""
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        rec = self._record("msg-a", cost=99.0)
        cache_db.save_ccreport_file(
            str(projects_dir / "-tmp-proj" / "gone.jsonl"), 1, 1, [rec]
        )
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 99.0

    def test_a_file_modified_after_caching_is_reparsed(self, projects_dir):
        from ccreport.pricing import compute_costs

        rec = self._record("msg-a")
        path = self._file(projects_dir, "a.jsonl", [rec])
        # Same path, different content: the cached records now describe a file
        # that no longer exists, and their fingerprint is what says so.
        rewritten = [self._record("msg-b"), self._record("msg-c")]
        path.write_text("".join(self._line(r) + "\n" for r in rewritten))
        assert compute_costs(cwd=self.CWD)[
            "all_time_project_cost"
        ] == self._expected(*rewritten)

    def test_only_the_files_the_cache_cannot_vouch_for_are_read(
        self,
        projects_dir,
        monkeypatch,
    ):
        """A file with cached records and a matching fingerprint is not opened.

        b.jsonl has no cached records, so its per-record timestamps can only
        come from the file however fresh file_costs is; c.jsonl's fingerprint
        misses.
        """
        from pathlib import Path

        from ccreport import pricing

        self._mixed_project(projects_dir)
        pricing.compute_costs(cwd=self.CWD)  # fills file_costs for all three
        parsed: list[str] = []
        real = pricing._scan_jsonl_file
        monkeypatch.setattr(
            pricing,
            "_scan_jsonl_file",
            lambda p, *a, **kw: parsed.append(Path(p).name) or real(p, *a, **kw),
        )
        pricing.compute_costs(cwd=self.CWD)
        assert parsed == ["b.jsonl", "c.jsonl"]


# ---------------------------------------------------------------------------
# Per-model week split — the cost beside the scoped rate-limit segment
# ---------------------------------------------------------------------------


class TestModelFamily:
    """One derivation for both sides: the record's model ID, the quota's name."""

    @pytest.mark.parametrize(
        ("model", "family"),
        [
            ("claude-fable-5", "fable"),
            ("Fable", "fable"),
            ("claude-opus-5[1m]", "opus"),
            ("claude-sonnet-4-5-20250929", "sonnet"),
            ("claude-haiku-4-5", "haiku"),
        ],
    )
    def test_ids_and_display_names_land_on_one_key(self, model, family):
        assert model_family(model) == family

    @pytest.mark.parametrize("model", ["", "qwen3:32b", "some-other-model"])
    def test_anything_unrecognized_shares_the_fallback(self, model):
        assert model_family(model) == OTHER_FAMILY


class TestWeekCostByModel:
    """week_cost split by model family, for the per-model weekly quota.

    Every path that can produce a file's week_cost owes the split too: a file
    served from the cost cache contributes a total the scan never sees, and a
    record whose JSONL is gone contributes one no file carries at all.
    """

    CWD = "/tmp/proj"

    @pytest.fixture
    def projects_dir(self, monkeypatch, tmp_path):
        from ccreport import cache_db, pricing

        d = tmp_path / "projects"
        (d / "-tmp-proj").mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        monkeypatch.setattr(cache_db, "get_project_overrides", list)
        cache_db.init_ccreport_meta(1, "test-hash")
        return d

    NOW = datetime.now(tz=UTC)
    # Window start lands 4 days back, so "now" is inside it and 30 days is not.
    WEEK_RESET = (NOW + timedelta(days=3)).isoformat()
    TOKENS = (1000, 1000, 0, 0)

    def _cost(self, model: str) -> float:
        return calc_cost(*self.TOKENS, model, self.NOW)

    def _write(self, path, *records: tuple[str, str, datetime]) -> None:
        import json

        path.write_text(
            "".join(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": ts.isoformat(),
                        "requestId": f"req-{mid}",
                        "sessionId": "s1",
                        "cwd": self.CWD,
                        "message": {
                            "id": mid,
                            "model": model,
                            "usage": {
                                "input_tokens": self.TOKENS[0],
                                "output_tokens": self.TOKENS[1],
                            },
                        },
                    }
                )
                + "\n"
                for mid, model, ts in records
            )
        )

    def _corpus(self, projects_dir):
        self._write(
            projects_dir / "-tmp-proj" / "a.jsonl",
            ("m1", "claude-fable-5", self.NOW - timedelta(hours=1)),
            ("m2", "claude-opus-5", self.NOW - timedelta(hours=2)),
            ("m3", "claude-fable-5", self.NOW - timedelta(days=30)),
        )

    def _costs(self):
        from ccreport.pricing import compute_costs

        return compute_costs(cwd=self.CWD, week_reset_iso=self.WEEK_RESET)

    def test_a_scan_splits_the_window_by_family(self, projects_dir):
        self._corpus(projects_dir)
        result = self._costs()
        assert result["week_model_costs"] == {
            "fable": pytest.approx(self._cost("claude-fable-5")),
            "opus": pytest.approx(self._cost("claude-opus-5")),
        }

    def test_the_split_adds_up_to_the_week_total(self, projects_dir):
        self._corpus(projects_dir)
        result = self._costs()
        assert sum(result["week_model_costs"].values()) == pytest.approx(
            result["week_cost"]
        )

    def test_a_record_outside_the_window_is_left_out(self, projects_dir):
        """The third fable record is 30 days old — only the in-window one counts."""
        self._corpus(projects_dir)
        result = self._costs()
        assert result["week_model_costs"]["fable"] < result["all_time_cost"]

    def test_a_fully_cached_file_still_reports_its_split(
        self,
        projects_dir,
        monkeypatch,
    ):
        """A file too old to re-scan answers from the cost cache alone.

        Its mtime puts it outside every rolling window, so the second run takes
        the branch that returns cached totals without opening the file.
        """
        import os

        from ccreport import pricing

        path = projects_dir / "-tmp-proj" / "a.jsonl"
        self._corpus(projects_dir)
        aged = (self.NOW - timedelta(days=40)).timestamp()
        os.utime(path, (aged, aged))

        first = self._costs()
        opened: list[str] = []
        real = pricing._iter_jsonl_costs
        monkeypatch.setattr(
            pricing,
            "_iter_jsonl_costs",
            lambda p, seen: opened.append(str(p)) or real(p, seen),
        )
        second = self._costs()

        assert opened == [], "the file was re-parsed, so the cache is not what answered"
        assert second["week_model_costs"] == first["week_model_costs"]

    def test_an_orphaned_record_reaches_the_split(self, projects_dir):
        """The JSONL is gone; the cached record is the only thing left of it."""
        from ccreport import cache_db

        cache_db.save_ccreport_file(
            str(projects_dir / "-tmp-proj" / "gone.jsonl"),
            1,
            1,
            [
                {
                    "mid": "m9",
                    "model": "claude-fable-5",
                    "ts": (self.NOW - timedelta(hours=3)).timestamp(),
                    "sid": "s9",
                    "project": "proj",
                    "cwd": self.CWD,
                    "repo": None,
                    "dk": "m9:r9",
                    "cost": 2.5,
                    "t": list(self.TOKENS),
                }
            ],
        )
        assert self._costs()["week_model_costs"] == {"fable": pytest.approx(2.5)}


# ---------------------------------------------------------------------------
# The bisected pricing-period index
# ---------------------------------------------------------------------------


class TestPricingPeriodIndex:
    """The bisect must answer exactly what the reverse walk it replaced did."""

    MODELS = (
        *sorted({m for p in PRICING_HISTORY for m in p["models"]}),
        *sorted(MODEL_ALIASES),
        "claude-opus-4-6",
        "claude-sonnet-5-20260601",
        "nonexistent-model-xyz",
        "llama3:8b",
        "",
    )

    @staticmethod
    def _walk(model, ts):
        """find_pricing as it was before the index: newest matching period wins."""
        from ccreport.pricing import _FREE_PRICING

        resolved = str(MODEL_ALIASES.get(model, model))
        if ":" in resolved:
            return _FREE_PRICING
        for period in reversed(PRICING_HISTORY):
            if ts is not None and _parse_effective(period["effective"]) > ts:
                continue
            models = period["models"]
            if resolved in models:
                return models[resolved]
            for key, prices in models.items():
                if key in resolved or resolved in key:
                    return prices
        return None

    @staticmethod
    def _stamps():
        """None, plus the microsecond either side of every period boundary."""
        stamps = [None]
        for period in PRICING_HISTORY:
            eff = _parse_effective(period["effective"])
            stamps += [
                eff - timedelta(microseconds=1),
                eff,
                eff + timedelta(microseconds=1),
            ]
        return stamps

    def test_every_model_at_every_boundary_matches_the_walk(self):
        for model in self.MODELS:
            for ts in self._stamps():
                assert find_pricing(model, ts) == self._walk(model, ts), (
                    f"{model!r} at {ts}"
                )

    def test_a_period_takes_effect_at_its_own_timestamp_not_after(self):
        """bisect_right, not bisect_left: the effective instant is inside."""
        flat = _parse_effective("2026-03-13T18")
        assert "input_200k" in present(find_pricing(
            "claude-opus-4-6", flat - timedelta(microseconds=1)
        ))
        assert "input_200k" not in present(find_pricing("claude-opus-4-6", flat))

    def test_a_table_out_of_chronological_order_still_prices_by_date(self, monkeypatch):
        """The index sorts, so an appended back-dated period cannot mislead it."""
        from ccreport import pricing

        monkeypatch.setattr(
            pricing,
            "PRICING_HISTORY",
            [
                {"effective": "2026-05-01", "models": {"m-x": {"input": 2e-06}}},
                {"effective": "2025-01-01", "models": {"m-x": {"input": 1e-06}}},
            ],
        )
        monkeypatch.setattr(pricing, "_PERIOD_INDEX", None)
        pricing._pricing_in_effect.cache_clear()
        try:
            old = present(pricing.find_pricing("m-x", datetime(2025, 6, 1, tzinfo=UTC)))
            new = present(pricing.find_pricing("m-x", datetime(2026, 6, 1, tzinfo=UTC)))
            assert old["input"] == 1e-06
            assert new["input"] == 2e-06
        finally:
            pricing._pricing_in_effect.cache_clear()

    def test_the_resolution_is_cached_per_model_and_period(self):
        from ccreport import pricing

        pricing._pricing_in_effect.cache_clear()
        ts = datetime(2026, 8, 1, tzinfo=UTC)
        for _ in range(50):
            find_pricing("claude-opus-5", ts)
            find_pricing("claude-opus-5", ts + timedelta(seconds=1))
        info = pricing._pricing_in_effect.cache_info()
        assert info.currsize == 1, (
            "distinct timestamps in one period must share an entry"
        )
        assert info.misses == 1


# ---------------------------------------------------------------------------
# Incremental session cost
# ---------------------------------------------------------------------------


class TestIncrementalSessionCost:
    """A growing session log is parsed once, not once per render per line."""

    CWD = "/tmp/proj"
    TS = datetime(2026, 8, 1, 12, tzinfo=UTC)
    ONE = 1000 * 5e-06  # 1000 input tokens of claude-opus-5

    @pytest.fixture
    def proj(self, monkeypatch, tmp_path):
        from ccreport import pricing

        d = tmp_path / "projects" / "-tmp-proj"
        d.mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d.parent])
        return d

    def _line(
        self, mid: str, rid: str, tokens: int = 1000, sid: str = "s1",
        model: str = "claude-opus-5",
    ) -> str:
        import json

        return (
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": self.TS.isoformat(),
                    "requestId": rid,
                    "sessionId": sid,
                    "cwd": self.CWD,
                    "message": {
                        "id": mid,
                        "model": model,
                        "usage": {"input_tokens": tokens},
                    },
                }
            )
            + "\n"
        )

    def _cost(self, sid: str = "s1") -> float:
        from ccreport.pricing import compute_session_cost

        return compute_session_cost(sid, self.CWD)

    def _parsed(self, monkeypatch) -> list[bytes]:
        """Every line handed to the record parser from here on."""
        from ccreport import pricing

        lines: list[bytes] = []
        real = pricing._line_cost

        def counting(line, seen_keys):
            lines.append(line)
            return real(line, seen_keys)

        monkeypatch.setattr(pricing, "_line_cost", counting)
        return lines

    def test_an_append_parses_only_the_appended_bytes(self, proj, monkeypatch):
        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1") + self._line("m2", "r2"))
        assert self._cost() == pytest.approx(2 * self.ONE)

        parsed = self._parsed(monkeypatch)
        with f.open("a") as fh:
            fh.write(self._line("m3", "r3"))
        assert self._cost() == pytest.approx(3 * self.ONE)
        assert len(parsed) == 1, "the whole file was re-read, not just the append"

    def test_an_unchanged_session_is_not_opened_at_all(self, proj, monkeypatch):
        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1"))
        assert self._cost() == pytest.approx(self.ONE)

        parsed = self._parsed(monkeypatch)
        assert self._cost() == pytest.approx(self.ONE)
        assert parsed == []

    def test_a_key_repeated_in_an_append_still_counts_once(self, proj):
        """The dedup set has to survive between renders, not just within one."""
        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1"))
        assert self._cost() == pytest.approx(self.ONE)

        with f.open("a") as fh:
            fh.write(self._line("m1", "r1"))
        assert self._cost() == pytest.approx(self.ONE)

    def test_a_rewritten_file_is_counted_from_the_start(self, proj, monkeypatch):
        """Same byte length, different content: only the tail digest catches it."""
        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1") + self._line("m2", "r2"))
        assert self._cost() == pytest.approx(2 * self.ONE)

        parsed = self._parsed(monkeypatch)
        rewritten = self._line("m3", "r3", tokens=2000) + self._line(
            "m4", "r4", tokens=2000
        )
        assert len(rewritten) == len(self._line("m1", "r1") + self._line("m2", "r2"))
        f.write_text(rewritten)

        assert self._cost() == pytest.approx(4 * self.ONE)
        assert len(parsed) == 2, "the rewritten file was resumed instead of recounted"

    def test_a_truncated_file_is_counted_from_the_start(self, proj):
        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1") + self._line("m2", "r2"))
        assert self._cost() == pytest.approx(2 * self.ONE)

        f.write_text(self._line("m1", "r1"))
        assert self._cost() == pytest.approx(self.ONE)

    def test_a_file_that_disappears_leaves_the_total(self, proj):
        """Its share is not separable from the accumulated number, so: recount."""
        (proj / "s1.jsonl").write_text(self._line("m1", "r1"))
        sub = proj / "s1"
        sub.mkdir()
        (sub / "side.jsonl").write_text(self._line("m2", "r2"))
        assert self._cost() == pytest.approx(2 * self.ONE)

        (sub / "side.jsonl").unlink()
        assert self._cost() == pytest.approx(self.ONE)

    def test_a_file_appearing_later_is_added(self, proj):
        (proj / "s1.jsonl").write_text(self._line("m1", "r1"))
        assert self._cost() == pytest.approx(self.ONE)

        sub = proj / "s1"
        sub.mkdir()
        (sub / "side.jsonl").write_text(self._line("m2", "r2"))
        assert self._cost() == pytest.approx(2 * self.ONE)

    def test_a_partial_trailing_line_waits_for_its_newline(self, proj):
        """A writer caught mid-append must not have its half-record counted."""
        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1") + self._line("m2", "r2").rstrip("\n"))
        assert self._cost() == pytest.approx(self.ONE)

        with f.open("a") as fh:
            fh.write("\n")
        assert self._cost() == pytest.approx(2 * self.ONE)

    def test_a_legacy_fingerprint_reparses_once_then_migrates(self, proj, monkeypatch):
        import json

        from ccreport import cache_db, pricing

        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1") + self._line("m2", "r2"))
        # What the pre-incremental cache wrote: an md5 hexdigest and a total.
        cache_db.write_session_cost("s1", "d41d8cd98f00b204e9800998ecf8427e", 99.0)

        assert self._cost() == pytest.approx(2 * self.ONE)
        stored = present(cache_db.read_session_cost("s1"))
        assert json.loads(stored[0])["v"] == pricing._SESSION_STATE_VERSION

        parsed = self._parsed(monkeypatch)
        with f.open("a") as fh:
            fh.write(self._line("m3", "r3"))
        assert self._cost() == pytest.approx(3 * self.ONE)
        assert len(parsed) == 1, "the migrated state was not usable"

    def test_a_corrupt_fingerprint_reparses_rather_than_raises(self, proj):
        from ccreport import cache_db

        (proj / "s1.jsonl").write_text(self._line("m1", "r1"))
        cache_db.write_session_cost("s1", '{"v":2,"f":{"/x":[1]},"k":[]}', 99.0)
        assert self._cost() == pytest.approx(self.ONE)

    def test_the_stored_state_carries_one_key_per_message(self, proj):
        import json

        from ccreport import cache_db

        (proj / "s1.jsonl").write_text(self._line("m1", "r1") + self._line("m2", "r2"))
        self._cost()
        keys = json.loads(present(cache_db.read_session_cost("s1"))[0])["k"]
        assert len(keys) == 2
        assert all(len(k) == 16 for k in keys), "keys are stored digested, not raw"

    def _families(self, sid: str = "s1") -> frozenset[str]:
        from ccreport.pricing import compute_session_usage

        return compute_session_usage(sid, self.CWD).families

    def test_every_model_the_session_logged_lands_in_the_family_set(self, proj):
        """A subagent's model is in the session's own log, so the scan sees it."""
        (proj / "s1.jsonl").write_text(
            self._line("m1", "r1") + self._line("m2", "r2", model="claude-fable-5")
        )
        assert self._families() == {"opus", "fable"}

    def test_a_subdirectory_file_contributes_its_family(self, proj):
        (proj / "s1.jsonl").write_text(self._line("m1", "r1"))
        sub = proj / "s1"
        sub.mkdir()
        (sub / "agent.jsonl").write_text(self._line("m2", "r2", model="claude-fable-5"))
        assert self._families() == {"opus", "fable"}

    def test_a_resumed_scan_keeps_the_families_from_before_the_cursor(self, proj, monkeypatch):
        f = proj / "s1.jsonl"
        f.write_text(self._line("m1", "r1", model="claude-fable-5"))
        assert self._families() == {"fable"}

        parsed = self._parsed(monkeypatch)
        with f.open("a") as fh:
            fh.write(self._line("m2", "r2"))
        assert self._families() == {"fable", "opus"}
        assert len(parsed) == 1, "the append was not resumed"

    def test_an_unchanged_session_returns_the_stored_families(self, proj):
        (proj / "s1.jsonl").write_text(self._line("m1", "r1", model="claude-fable-5"))
        self._families()
        assert self._families() == {"fable"}, "the no-op resume path dropped them"

    def test_a_purged_session_takes_its_families_from_the_cached_records(self, proj, monkeypatch):
        """The JSONL is gone, so the record cache answers for both cost and family."""
        from ccreport import pricing
        from ccreport.pricing import compute_session_usage

        recs = {
            str(proj / "s1.jsonl"): [
                {"model": "claude-fable-5", "cost": 0.25, "ts": self.TS.timestamp()},
            ]
        }
        monkeypatch.setattr(
            "ccreport.cache_db.load_ccreport_records_for_session", lambda sid: recs
        )
        monkeypatch.setattr(pricing, "_find_session_files", lambda *a, **k: set())
        usage = compute_session_usage("s1", self.CWD)
        assert usage.cost == pytest.approx(0.25)
        assert usage.families == {"fable"}


# ---------------------------------------------------------------------------
# Slow-path-only imports and lazy module state
# ---------------------------------------------------------------------------


class TestDeferredImports:
    def test_importing_pricing_touches_neither_zoneinfo_nor_the_repo_roots_config(self):
        """A fast render imports this module and needs none of it."""
        import subprocess
        import sys
        from pathlib import Path

        src_dir = Path(__file__).resolve().parent.parent / "src"
        probe = (
            "import sys;"
            "from ccreport import pricing, project_identity as pi;"
            "assert 'zoneinfo' not in sys.modules, 'zoneinfo';"
            "assert 'tomllib' not in sys.modules, 'tomllib';"
            "assert 'hashlib' not in sys.modules, 'hashlib';"
            "assert pricing._PERIOD_INDEX is None, 'pricing period index';"
            "assert pi.repo_roots.cache_info().currsize == 0, 'repo roots'"
        )
        subprocess.run([sys.executable, "-c", probe], cwd=src_dir, check=True)

    def test_the_local_zone_still_follows_the_tz_env_var(self, monkeypatch):
        """Ccreport's rollup fingerprint holds the zone; caching must not freeze it."""
        from ccreport.pricing import _local_tz

        monkeypatch.setenv("TZ", "Pacific/Kiritimati")
        assert str(_local_tz()) == "Pacific/Kiritimati"
        monkeypatch.setenv("TZ", "Europe/Oslo")
        assert str(_local_tz()) == "Europe/Oslo"

    def test_an_unknown_zone_falls_back_to_utc(self, monkeypatch):
        from ccreport.pricing import _local_tz

        monkeypatch.setenv("TZ", "Mars/Olympus_Mons")
        assert str(_local_tz()) == "UTC"

    def test_the_repo_roots_constant_still_resolves(self):
        """REPO_ROOTS was a module attribute before repo_roots() replaced it."""
        from ccreport import project_identity

        assert project_identity.repo_roots() == project_identity.REPO_ROOTS

    def test_an_unknown_module_attribute_still_raises(self):
        from ccreport import project_identity

        with pytest.raises(AttributeError):
            _ = project_identity.NO_SUCH_NAME


# ---------------------------------------------------------------------------
# Cost computation bounded to a window, all_time excepted
# ---------------------------------------------------------------------------

# Priced since 2025-01-01, so a record 200 days old still has a price to
# look up — the newest models are only priced from their release on.
OLD_MODEL = "claude-sonnet-4-20250514"


class TestBoundedCostWindows:
    """A render reads one window's records, never the whole logged history.

    all_time is the bucket with no window to be bounded by, so it is answered
    from pre-summed rows instead: file_costs for the files still on disk,
    ccreport_orphan_costs for the ones Claude Code has purged. The tests here
    pin what those rows have to keep getting right — an orphan counted at all,
    a row whose stored cost is NULL priced from its tokens, a duplicate counted
    once — because a sum that is wrong is indistinguishable from one that is
    right until somebody re-derives it.
    """

    CWD = "/tmp/proj"
    OLD_TS = 200 * 86400  # comfortably outside every window, including month

    @pytest.fixture
    def projects_dir(self, monkeypatch, tmp_path):
        from ccreport import cache_db, pricing

        d = tmp_path / "projects"
        (d / "-tmp-proj").mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d])
        cache_db.init_ccreport_meta(1, "test-hash")
        return d

    def _orphan(
        self,
        path,
        *,
        dk=None,
        cost=None,
        age_s=OLD_TS,
        tokens=(1000, 1000, 0, 0),
        project="proj",
        cwd=CWD,
    ) -> None:
        """Cache one record under *path*, with no file on disk behind it."""
        from ccreport import cache_db

        cache_db.save_ccreport_file(
            str(path),
            1,
            1,
            [
                {
                    "mid": "m",
                    "model": OLD_MODEL,
                    "ts": datetime.now(tz=UTC).timestamp() - age_s,
                    "sid": "s1",
                    "project": project,
                    "cwd": cwd,
                    "repo": None,
                    "dk": dk,
                    "cost": cost,
                    "t": list(tokens),
                }
            ],
        )

    def _live(self, path, mid: str, *, age_s: float = 0.0) -> None:
        import json

        ts = datetime.now(tz=UTC) - timedelta(seconds=age_s)
        with open(path, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": ts.isoformat(),
                        "requestId": f"req-{mid}",
                        "sessionId": "s1",
                        "cwd": self.CWD,
                        "message": {
                            "id": mid,
                            "model": OLD_MODEL,
                            "usage": {"input_tokens": 1000, "output_tokens": 1000},
                        },
                    }
                )
                + "\n"
            )

    def _cache_live(self, path) -> None:
        """Cache *path*'s record under its real fingerprint, as ccreport would.

        A file ccreport never saw does not join the orphan set when it is
        deleted — there is nothing cached to orphan.
        """
        from ccreport import cache_db

        st = path.stat()
        cache_db.save_ccreport_file(
            str(path),
            st.st_mtime_ns,
            st.st_size,
            [
                {
                    "mid": "msg-live",
                    "model": OLD_MODEL,
                    "ts": datetime.now(tz=UTC).timestamp(),
                    "sid": "s1",
                    "project": "proj",
                    "cwd": self.CWD,
                    "repo": None,
                    "dk": "msg-live:req-msg-live",
                    "cost": None,
                    "t": [1000, 1000, 0, 0],
                }
            ],
        )

    def test_a_purged_records_all_time_survives_the_window_bound(self, projects_dir):
        from ccreport.pricing import compute_costs

        self._orphan(projects_dir / "-tmp-proj" / "gone.jsonl", cost=3.0)
        result = compute_costs(cwd=self.CWD)
        assert result["all_time_cost"] == 3.0
        assert result["all_time_project_cost"] == 3.0
        # Old enough that no bounded window may claim it — which is exactly why
        # a bounded read cannot be where all_time comes from.
        assert result["thirty_day_cost"] == 0.0
        assert result["month_cost"] == 0.0

    def test_a_null_cost_row_is_priced_from_its_tokens(self, projects_dir):
        """invalidate_ccreport NULLs a live file's costs; a SUM(cost) reads 0."""
        from ccreport import cache_db
        from ccreport.pricing import _rec_cost_from_tokens, compute_costs

        self._orphan(projects_dir / "-tmp-proj" / "gone.jsonl", cost=None)
        conn = cache_db.get_connection()
        assert conn.execute("SELECT cost FROM ccreport_records").fetchone()[0] is None

        expected = _rec_cost_from_tokens(
            {
                "t": [1000, 1000, 0, 0],
                "model": OLD_MODEL,
                "ts": datetime.now(tz=UTC).timestamp() - self.OLD_TS,
            }
        )
        assert expected > 0
        assert compute_costs(cwd=self.CWD)["all_time_cost"] == pytest.approx(expected)

    def test_a_duplicated_purged_record_counts_once(self, projects_dir):
        from ccreport.pricing import compute_costs

        # One message id in two purged logs — a resumed or copied transcript.
        self._orphan(projects_dir / "-tmp-proj" / "a.jsonl", dk="msg-1:req-1", cost=5.0)
        self._orphan(projects_dir / "-tmp-proj" / "b.jsonl", dk="msg-1:req-1", cost=5.0)
        assert compute_costs(cwd=self.CWD)["all_time_cost"] == 5.0

    def test_the_bounded_read_agrees_with_an_unbounded_derivation(self, projects_dir):
        """The whole point, stated once: same numbers, less read.

        The reference sums every cached record and every live line by hand,
        with no window bound and no stored aggregate anywhere in it.
        """
        from ccreport.pricing import _rec_cost, compute_costs

        d = projects_dir / "-tmp-proj"
        for i, age in enumerate((self.OLD_TS, 40 * 86400, 3 * 86400, 60)):
            self._orphan(d / f"gone-{i}.jsonl", dk=f"m{i}:r{i}", cost=None, age_s=age)
        live = d / "live.jsonl"
        self._live(live, "msg-live-old", age_s=45 * 86400)
        self._live(live, "msg-live-new")

        from ccreport import cache_db

        expected = sum(
            _rec_cost(rec)
            for recs in cache_db.bulk_load_ccreport_cache()[1].values()
            for rec in recs
        )
        expected += sum(cost for cost, _ts, _dk, _m in _iter_live(live))

        result = compute_costs(cwd=self.CWD)
        assert result["all_time_cost"] == pytest.approx(round(expected, 4), abs=1e-4)
        assert result["all_time_project_cost"] == pytest.approx(
            round(expected, 4), abs=1e-4
        )

    def test_the_stored_total_is_reused_until_the_orphan_set_moves(
        self,
        projects_dir,
        monkeypatch,
    ):
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        d = projects_dir / "-tmp-proj"
        self._orphan(d / "gone.jsonl", cost=3.0)
        live = d / "live.jsonl"
        self._live(live, "msg-live")
        self._cache_live(live)
        assert compute_costs(cwd=self.CWD)["all_time_cost"] > 3.0

        rebuilds = []
        real = cache_db.load_ccreport_records_for_paths

        def spy(paths):
            rebuilds.append(set(paths))
            return real(paths)

        monkeypatch.setattr(cache_db, "load_ccreport_records_for_paths", spy)

        compute_costs(cwd=self.CWD)
        assert rebuilds == [], "an unchanged orphan set must not be re-summed"

        live.unlink()
        compute_costs(cwd=self.CWD)
        assert rebuilds == [{str(d / "gone.jsonl"), str(live)}]

    def test_re_parsing_a_live_file_does_not_re_sum_the_purged_ones(
        self,
        projects_dir,
        monkeypatch,
    ):
        """The common write, and the one a whole-table stamp would trip on."""
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        d = projects_dir / "-tmp-proj"
        self._orphan(d / "gone.jsonl", cost=3.0)
        live = d / "live.jsonl"
        self._live(live, "msg-live")
        compute_costs(cwd=self.CWD)

        rebuilds = []
        monkeypatch.setattr(
            cache_db,
            "load_ccreport_records_for_paths",
            lambda paths: rebuilds.append(set(paths)) or {},
        )
        # What `ccreport` does to a session log that grew: delete, re-insert.
        st = live.stat()
        cache_db.save_ccreport_file(
            str(live),
            st.st_mtime_ns,
            st.st_size,
            [
                {
                    "mid": "m2",
                    "model": "claude-opus-5",
                    "ts": datetime.now(tz=UTC).timestamp(),
                    "sid": "s1",
                    "project": "proj",
                    "cwd": self.CWD,
                    "repo": None,
                    "dk": "m2:r2",
                    "cost": 1.0,
                    "t": [1, 1, 0, 0],
                }
            ],
        )
        assert compute_costs(cwd=self.CWD)["all_time_cost"] > 3.0
        assert rebuilds == []

    def test_a_render_never_loads_the_whole_record_table(
        self, projects_dir, monkeypatch
    ):
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        self._orphan(projects_dir / "-tmp-proj" / "gone.jsonl", cost=3.0)

        def no_full_scans():
            msg = "a render must not load the whole table"
            raise AssertionError(msg)

        monkeypatch.setattr(cache_db, "bulk_load_ccreport_cache", no_full_scans)
        assert compute_costs(cwd=self.CWD)["all_time_cost"] == 3.0

    def test_a_merge_regroups_the_stored_totals_with_no_re_sum(
        self,
        projects_dir,
        monkeypatch,
    ):
        """The rows keep the raw identity, so a rule change costs no rebuild."""
        from ccreport import cache_db
        from ccreport.pricing import compute_costs

        # Another project's directory, so only the stored identity can hand it
        # to this cwd — the path prefix never will.
        self._orphan(
            projects_dir / "-tmp-other" / "gone.jsonl",
            cost=4.0,
            project="other",
            cwd="/tmp/other",
        )
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 0.0

        rebuilds = []
        real = cache_db.load_ccreport_records_for_paths
        monkeypatch.setattr(
            cache_db,
            "load_ccreport_records_for_paths",
            lambda paths: rebuilds.append(1) or real(paths),
        )
        cache_db.add_project_override("name", "other", "proj")
        cache_db.add_project_override("name", "proj", "proj")
        assert compute_costs(cwd=self.CWD)["all_time_project_cost"] == 4.0
        assert rebuilds == []


def _iter_live(path):
    """pricing._iter_jsonl_costs over a file, with a dedup set of its own."""
    from ccreport.pricing import _iter_jsonl_costs

    return list(_iter_jsonl_costs(path, set()))


class TestPlanPrice:
    """What a subscription cost per month, by rate-limit tier."""

    def _at(self, iso="2026-06-01"):
        return datetime.fromisoformat(iso).replace(tzinfo=UTC)

    @pytest.mark.parametrize(
        ("tier", "usd"),
        [
            ("default_claude_pro", 20.0),
            ("default_claude_max_5x", 100.0),
            ("default_claude_max_20x", 200.0),
        ],
    )
    def test_it_prices_each_plan_at_its_list_price(self, tier, usd):
        assert pricing.plan_price(tier, self._at()) == usd

    def test_a_tier_nothing_prices_is_none_not_zero(self):
        """A team seat is an org's contract, not a list price."""
        assert pricing.plan_price("default_raven", self._at()) is None

    def test_no_tier_is_none(self):
        assert pricing.plan_price(None, self._at()) is None
        assert pricing.plan_price("", self._at()) is None

    def test_an_instant_before_every_period_has_no_price(self):
        assert pricing.plan_price("default_claude_pro", self._at("2024-01-01")) is None

    def test_no_instant_takes_the_newest_period(self):
        assert pricing.plan_price("default_claude_max_5x") == 100.0

    def test_a_later_period_overrides_an_earlier_one(self, monkeypatch):
        monkeypatch.setattr(pricing, "PLAN_PRICES", [
            {"effective": "2025-01-01", "plans": {"p": 20.0}},
            {"effective": "2026-05-01", "plans": {"p": 30.0}},
        ])
        monkeypatch.setattr(pricing, "_PLAN_INDEX", None)
        assert pricing.plan_price("p", self._at("2026-04-01")) == 20.0
        assert pricing.plan_price("p", self._at("2026-06-01")) == 30.0

    def test_a_table_left_out_of_order_still_prices_correctly(self, monkeypatch):
        """Bisecting an unsorted table would pick the wrong period silently."""
        monkeypatch.setattr(pricing, "PLAN_PRICES", [
            {"effective": "2026-05-01", "plans": {"p": 30.0}},
            {"effective": "2025-01-01", "plans": {"p": 20.0}},
        ])
        monkeypatch.setattr(pricing, "_PLAN_INDEX", None)
        assert pricing.plan_price("p", self._at("2026-04-01")) == 20.0


class TestProratedPlanCost:
    """A subscription is billed by the calendar month, so a span is priced by it."""

    FEB = datetime(2026, 2, 1, tzinfo=UTC)

    def _spans(self, *pairs, opening=None):
        """(days, tier) pairs laid end to end from *opening*, default 1 February.

        The tier is the org pool field, which is where a personal plan's lands.
        """
        at = (opening or self.FEB).timestamp()
        out = []
        for days, tier in pairs:
            tiers = None if tier is None else {"organization_rate_limit_tier": tier}
            out.append((at, at + days * 86400, tiers))
            at += days * 86400
        return out

    SEAT_BUCKET = {"user_rate_limit_tier": "default_claude_max_5x"}

    def _seat(self, days, seat):
        at = self.FEB.timestamp()
        return [(at, at + days * 86400, {"seat_tier": seat, **self.SEAT_BUCKET})]

    def test_a_whole_month_on_one_plan_costs_its_monthly_rate(self):
        cost = pricing.prorated_plan_cost(self._spans((28, "default_claude_max_5x")))
        assert cost == pytest.approx(100.0)

    def test_a_split_month_weights_the_two_rates_by_days(self):
        """14 days of each in a 28-day month: half of $20 plus half of $100."""
        cost = pricing.prorated_plan_cost(
            self._spans((14, "default_claude_pro"), (14, "default_claude_max_20x")),
        )
        assert cost == pytest.approx(110.0)

    def test_a_week_costs_a_week_not_a_month(self):
        """Over the span rather than over the month is the defect this catches."""
        cost = pricing.prorated_plan_cost(self._spans((7, "default_claude_max_5x")))
        assert cost == pytest.approx(100.0 * 7 / 28)

    def test_half_a_year_costs_six_months_not_one(self):
        cost = pricing.prorated_plan_cost(
            self._spans((181, "default_claude_max_5x"), opening=datetime(2026, 1, 1, tzinfo=UTC)),
        )
        assert cost == pytest.approx(600.0)

    def test_a_fortnight_is_a_bigger_share_of_february_than_of_august(self):
        """Each part is measured against its own month's length."""
        feb = pricing.prorated_plan_cost(self._spans((14, "default_claude_max_5x")))
        aug = pricing.prorated_plan_cost(self._spans(
            (14, "default_claude_max_5x"), opening=datetime(2026, 8, 1, tzinfo=UTC),
        ))
        assert present(feb) > present(aug)
        assert feb == pytest.approx(100.0 * 14 / 28)
        assert aug == pytest.approx(100.0 * 14 / 31)

    def test_a_stretch_crossing_a_month_boundary_is_split_at_it(self):
        """The last week of February plus the first of March, each on its own."""
        cost = pricing.prorated_plan_cost(self._spans(
            (14, "default_claude_max_5x"), opening=datetime(2026, 2, 22, tzinfo=UTC),
        ))
        assert cost == pytest.approx(100.0 * 7 / 28 + 100.0 * 7 / 31)

    def test_an_unpriced_stretch_contributes_nothing_but_does_not_void_the_span(self):
        cost = pricing.prorated_plan_cost(
            self._spans((14, None), (14, "default_claude_max_20x")),
        )
        assert cost == pytest.approx(100.0)

    def test_nothing_priced_is_none_not_zero(self):
        """A zero beside real spend reads as a span that was free."""
        assert pricing.prorated_plan_cost(self._spans((28, None))) is None
        assert pricing.prorated_plan_cost([]) is None

    def test_an_unpriced_tier_does_not_make_a_span_free(self):
        assert pricing.prorated_plan_cost(self._spans((28, "default_raven"))) is None

    def test_a_team_seat_prices_as_the_seat_not_its_limit_bucket(self):
        """The seat carries a personal plan's bucket and costs its own money."""
        assert pricing.prorated_plan_cost(
            self._seat(28, "team_tier_1"),
        ) == pytest.approx(100.0)

    def test_a_seat_nothing_prices_falls_back_to_its_bucket(self):
        """Better the limit bucket's price than no figure at all."""
        assert pricing.prorated_plan_cost(
            self._seat(28, "team_tier_9"),
        ) == pytest.approx(100.0)

    def test_a_backwards_stretch_contributes_nothing(self):
        at = self.FEB.timestamp()
        tiers = {"organization_rate_limit_tier": "default_claude_pro"}
        assert pricing.prorated_plan_cost([(at + 86400, at, tiers)]) is None

    def test_each_stretch_pays_the_rate_in_force_when_it_started(self, monkeypatch):
        """A span crossing a price rise pays the old rate up to it."""
        monkeypatch.setattr(pricing, "PLAN_PRICES", [
            {"effective": "2025-01-01", "plans": {"p": 20.0}},
            {"effective": "2026-02-15", "plans": {"p": 40.0}},
        ])
        monkeypatch.setattr(pricing, "_PLAN_INDEX", None)
        cost = pricing.prorated_plan_cost(self._spans((14, "p"), (14, "p")))
        assert cost == pytest.approx(30.0)


class TestSeatPrice:
    """A team seat is priced by the seat, not by the limits it happens to get."""

    def _at(self):
        return datetime(2026, 6, 1, tzinfo=UTC)

    def test_the_claude_code_premium_seat_is_priced(self):
        assert pricing.seat_price("team_tier_1", self._at()) == 100.0

    def test_a_seat_nothing_names_is_none(self):
        assert pricing.seat_price("team_tier_9", self._at()) is None

    def test_no_seat_is_none(self):
        assert pricing.seat_price(None, self._at()) is None

    def test_the_seat_wins_over_the_rate_limit_tier(self):
        assert pricing.tier_set_price({
            "seat_tier": "team_tier_1",
            "user_rate_limit_tier": "default_claude_max_20x",
        }, self._at()) == 100.0

    def test_a_personal_plan_has_no_seat_and_prices_by_its_tier(self):
        assert pricing.tier_set_price({
            "seat_tier": None,
            "organization_rate_limit_tier": "default_claude_max_20x",
        }, self._at()) == 200.0

    def test_a_tier_set_nothing_prices_is_none(self):
        assert pricing.tier_set_price({"organization_rate_limit_tier": "default_raven"},
                                      self._at()) is None


class TestTierFrom:
    """The bucket a tier set drew against, spelled here so pricing imports neither."""

    def test_the_user_tier_wins(self):
        assert pricing.tier_from({
            "user_rate_limit_tier": "u", "organization_rate_limit_tier": "o",
        }) == "u"

    def test_the_org_pool_answers_a_personal_plan(self):
        assert pricing.tier_from({"organization_rate_limit_tier": "o"}) == "o"

    def test_neither_is_none(self):
        assert pricing.tier_from({"seat_tier": "team_tier_1"}) is None

    def test_it_agrees_with_the_other_two_spellings(self):
        from ccreport import cache_db, tier_timeline

        row = {"user_rate_limit_tier": None, "organization_rate_limit_tier": "o"}
        assert pricing.tier_from(row) == cache_db.effective_limit_tier(row)
        assert pricing.tier_from(row) == tier_timeline.effective_tier(row)


class TestMonthsInSpan:
    """How long a span is, in the unit the subscription is billed by."""

    def test_a_whole_month_is_one(self):
        assert pricing.months_in_span(
            datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC),
        ) == pytest.approx(1.0)

    def test_a_fortnight_is_a_bigger_fraction_of_february_than_of_august(self):
        feb = pricing.months_in_span(
            datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 15, tzinfo=UTC),
        )
        aug = pricing.months_in_span(
            datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 15, tzinfo=UTC),
        )
        assert feb == pytest.approx(14 / 28)
        assert aug == pytest.approx(14 / 31)

    def test_it_sums_across_a_boundary(self):
        assert pricing.months_in_span(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC),
        ) == pytest.approx(2.0)

    def test_an_empty_or_backwards_span_is_zero(self):
        one = datetime(2026, 2, 1, tzinfo=UTC)
        assert pricing.months_in_span(one, one) == 0.0
        assert pricing.months_in_span(datetime(2026, 3, 1, tzinfo=UTC), one) == 0.0

    def test_it_agrees_with_what_the_pricing_bills(self):
        """A count printed beside a cost must measure the span the same way."""
        start, end = datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)
        spans = [(start.timestamp(), end.timestamp(),
                  {"organization_rate_limit_tier": "default_claude_max_5x"})]
        cost = present(pricing.prorated_plan_cost(spans))
        assert cost == pytest.approx(100.0 * pricing.months_in_span(start, end))
