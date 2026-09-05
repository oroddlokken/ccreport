"""The spend forecast: the daily rate, the three horizons, and the ceilings."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime

import pytest
from rich.console import Console

from ccreport import aggregate, cache_db, forecast
from ccreport import ccreport as ccr

NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC).astimezone()


class _FrozenClock(datetime):
    """`datetime` with `now` pinned to NOW, for the command tests.

    The calendar-month projection wants three active days inside the month, so
    a run seeded backwards from the wall clock projects nothing on the 2nd.
    """

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW


def _costs(pairs) -> dict[str, float]:
    """(day of March, USD) pairs as the mapping the forecast reads."""
    return {f"2026-03-{day:02d}": cost for day, cost in pairs}


class TestDailyRate:
    def test_a_steady_month_rates_at_its_daily_spend(self):
        rate = forecast.daily_rate(_costs([(10, 5.0), (11, 5.0), (12, 5.0)]), NOW.date())
        assert rate == pytest.approx(5.0)

    def test_two_active_days_project_nothing(self):
        """A projection built on two data points is a guess with a decimal point."""
        assert forecast.daily_rate(_costs([(10, 5.0), (11, 5.0)]), NOW.date()) is None

    def test_a_day_with_no_spend_does_not_count_as_active(self):
        assert forecast.daily_rate(
            _costs([(10, 5.0), (11, 0.0), (12, 5.0)]), NOW.date(),
        ) is None

    def test_nothing_at_all_projects_nothing(self):
        assert forecast.daily_rate({}, NOW.date()) is None

    def test_recent_days_weigh_more_than_old_ones(self):
        """One heavy day early in the month must not decide the whole projection."""
        front_loaded = forecast.daily_rate(
            _costs([(1, 90.0), (13, 1.0), (14, 1.0), (15, 1.0)]), NOW.date())
        even = forecast.daily_rate(
            _costs([(1, 23.25), (13, 23.25), (14, 23.25), (15, 23.25)]), NOW.date())
        assert front_loaded is not None
        assert even is not None
        assert front_loaded < even, "the old spike should not carry the rate"

    def test_a_spike_today_moves_it_up(self):
        calm = forecast.daily_rate(_costs([(13, 5.0), (14, 5.0), (15, 5.0)]), NOW.date())
        spike = forecast.daily_rate(_costs([(13, 5.0), (14, 5.0), (15, 50.0)]), NOW.date())
        assert calm is not None
        assert spike is not None
        assert spike > calm * 2


class TestMonthForecast:
    def test_a_steady_month_projects_close_to_the_arithmetic(self):
        """5/day over a 31-day March, 15 elapsed: 15 spent plus 16 to come."""
        costs = _costs([(day, 5.0) for day in range(1, 16)])
        result = forecast.month_forecast(costs, NOW)
        assert result is not None
        assert result.spent == pytest.approx(75.0)
        assert result.projected == pytest.approx(75.0 + 5.0 * 16, rel=0.02)
        assert (result.elapsed, result.total) == (15.0, 31.0)

    def test_a_sparse_month_projects_nothing(self):
        assert forecast.month_forecast(_costs([(10, 5.0), (11, 5.0)]), NOW) is None

    def test_last_months_spend_stays_out_of_it(self):
        costs = {"2026-02-20": 500.0, **_costs([(13, 1.0), (14, 1.0), (15, 1.0)])}
        result = forecast.month_forecast(costs, NOW)
        assert result is not None
        assert result.spent == pytest.approx(3.0)

    def test_a_ceiling_gives_a_share(self):
        costs = _costs([(day, 5.0) for day in range(1, 16)])
        result = forecast.month_forecast(costs, NOW, ceiling=200.0)
        assert result is not None
        assert result.share == pytest.approx(result.projected / 200.0)
        assert not result.over

    def test_no_ceiling_is_the_projection_alone(self):
        result = forecast.month_forecast(_costs([(13, 5.0), (14, 5.0), (15, 5.0)]), NOW)
        assert result is not None
        assert result.share is None
        assert not result.over

    def test_a_projection_past_the_ceiling_says_so(self):
        costs = _costs([(day, 50.0) for day in range(1, 16)])
        result = forecast.month_forecast(costs, NOW, ceiling=100.0)
        assert result is not None
        assert result.over


class TestBillingMonth:
    @pytest.mark.parametrize(("today", "renewal", "expected"), [
        (date(2026, 3, 15), 8, date(2026, 3, 8)),
        (date(2026, 3, 5), 8, date(2026, 2, 8)),
        (date(2026, 1, 5), 8, date(2025, 12, 8)),
        (date(2026, 3, 8), 8, date(2026, 3, 8)),
    ])
    def test_the_period_is_anchored_to_the_renewal_day(self, today, renewal, expected):
        assert forecast.billing_start(today, renewal) == expected

    def test_a_renewal_day_past_the_month_end_lands_on_the_last_day(self):
        """A subscription renewing on the 31st renews on the 28th in February."""
        assert forecast.billing_start(date(2026, 3, 1), 31) == date(2026, 2, 28)

    def test_a_short_month_does_not_end_the_period_early(self):
        """On Feb 20 with a renewal on the 31st, the period started Jan 31."""
        assert forecast.billing_start(date(2026, 2, 20), 31) == date(2026, 1, 31)

    def test_it_projects_over_the_period_not_the_calendar_month(self):
        costs = _costs([(day, 5.0) for day in range(8, 16)])
        result = forecast.billing_forecast(costs, NOW, renewal_day=8)
        assert result is not None
        assert result.spent == pytest.approx(40.0)
        assert result.total == pytest.approx(31.0)

    def test_spend_before_the_renewal_stays_out(self):
        costs = {"2026-03-01": 500.0, **_costs([(13, 1.0), (14, 1.0), (15, 1.0)])}
        result = forecast.billing_forecast(costs, NOW, renewal_day=8)
        assert result is not None
        assert result.spent == pytest.approx(3.0)

    def test_a_sparse_period_projects_nothing(self):
        assert forecast.billing_forecast(_costs([(14, 5.0)]), NOW, renewal_day=8) is None


class TestWindowForecast:
    SPAN = 5 * 3600

    def test_it_projects_the_window_to_its_reset(self):
        now = 1_800_000_000.0
        result = forecast.window_forecast("session", 10.0, now - self.SPAN / 2, self.SPAN, now)
        assert result is not None
        assert result.projected == pytest.approx(20.0)
        assert result.resets_at == pytest.approx(now + self.SPAN / 2)

    def test_a_window_barely_started_projects_nothing(self):
        """Two minutes in, one call would be extrapolated across five hours."""
        now = 1_800_000_000.0
        assert forecast.window_forecast("session", 5.0, now - 120, self.SPAN, now) is None

    def test_a_window_with_no_spend_projects_nothing(self):
        now = 1_800_000_000.0
        assert forecast.window_forecast("session", 0.0, now - 3600, self.SPAN, now) is None

    def test_an_expired_window_projects_nothing(self):
        now = 1_800_000_000.0
        assert forecast.window_forecast(
            "session", 5.0, now - self.SPAN - 1, self.SPAN, now,
        ) is None


class TestDailyCosts:
    def _record(self, day, cost, account="me@work.example", sid="s"):
        return aggregate.UsageRecord(
            message_id="m", model="claude-haiku-4-5",
            tokens=aggregate.TokenCounts(input=1, output=1),
            timestamp=datetime(2026, 3, day, 12, 0, tzinfo=UTC),
            session_id=sid, project="p", cost_usd=cost, account=account,
        )

    def test_it_buckets_by_account_and_local_day(self):
        costs = forecast.daily_costs([self._record(15, 2.5), self._record(15, 1.5)])
        assert costs == {"me@work.example": {"2026-03-15": pytest.approx(4.0)}}

    def test_two_accounts_stay_apart(self):
        """Personal and work are separate money, and separate projections."""
        costs = forecast.daily_costs([
            self._record(15, 2.5), self._record(15, 1.0, account="me@home.example"),
        ])
        assert set(costs) == {"me@work.example", "me@home.example"}

    def test_an_empty_stream_is_an_empty_answer(self):
        assert forecast.daily_costs([]) == {}

    def test_it_uses_the_records_own_cost(self):
        """Logged where the log carried one, computed where it did not."""
        computed = forecast.daily_costs([self._record(15, None)])
        assert sum(computed["me@work.example"].values()) > 0


class TestBudgetsTable:
    def test_it_starts_empty(self):
        assert cache_db.load_budgets() == {}

    def test_a_ceiling_round_trips(self):
        cache_db.save_budget("me@work.example", 200.0, None, 1.0)
        assert cache_db.load_budgets() == {"me@work.example": (200.0, None)}

    def test_setting_a_renewal_day_keeps_the_ceiling(self):
        """Two settings, one row: neither may quietly drop the other."""
        cache_db.save_budget("me@work.example", 200.0, None, 1.0)
        cache_db.save_budget("me@work.example", None, 8, 2.0)
        assert cache_db.load_budgets() == {"me@work.example": (200.0, 8)}

    def test_each_account_carries_its_own(self):
        """Personal and work are separate money."""
        cache_db.save_budget("work", 200.0, None, 1.0)
        cache_db.save_budget("home", 50.0, None, 1.0)
        assert cache_db.load_budgets() == {"home": (50.0, None), "work": (200.0, None)}

    def test_clearing_removes_it(self):
        cache_db.save_budget("work", 200.0, None, 1.0)
        assert cache_db.clear_budget("work")
        assert cache_db.load_budgets() == {}

    def test_clearing_nothing_says_so(self):
        assert not cache_db.clear_budget("never-set")


class TestBudgetCommand:
    @pytest.fixture(autouse=True)
    def isolated_corpus(self, tmp_path, monkeypatch):
        """Keep the command off this machine's real ~/.claude/projects.

        `ccreport budget` loads records the way every report does, and without
        this the suite would scan and price the developer's whole corpus — 60
        seconds, and assertions against whatever they happened to spend.
        """
        projects = tmp_path / "projects"
        projects.mkdir()
        monkeypatch.setattr(ccr, "discover_jsonl_files", list)
        monkeypatch.setattr(ccr, "_ensure_cache_valid", lambda _live_paths: None)
        cache_db.init_ccreport_meta(ccr.CACHE_VERSION, ccr._script_hash())
        monkeypatch.setattr(ccr, "datetime", _FrozenClock)

    def _run(self, monkeypatch, argv) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport", *argv])
        ccr.main()
        return buf.getvalue()

    def _seed(self, days=5, cost=5.0):
        now = NOW
        cache_db.record_account_event(
            {"accountUuid": "u", "emailAddress": "me@work.example", "organizationName": "Org"},
            now=now.timestamp() - 40 * 86400,
        )
        records = [
            {"mid": f"m{i}", "model": "claude-haiku-4-5",
             "ts": (now.timestamp() - i * 86400), "sid": "s", "project": "p",
             "cwd": None, "repo": None, "dk": f"m{i}:r", "cost": cost,
             "t": [1, 1, 0, 0]}
            for i in range(days)
        ]
        cache_db.save_ccreport_files([("/p/a.jsonl", 1, 10, records)])

    def test_set_stores_the_ceiling(self, monkeypatch):
        self._run(monkeypatch, ["budget", "set", "me@work.example", "200"])
        assert cache_db.load_budgets()["me@work.example"][0] == 200.0

    def test_set_stores_a_renewal_day(self, monkeypatch):
        self._run(monkeypatch, ["budget", "set", "me@work.example", "--renewal-day", "8"])
        assert cache_db.load_budgets()["me@work.example"][1] == 8

    def test_clear_removes_it(self, monkeypatch):
        cache_db.save_budget("me@work.example", 200.0, None, 1.0)
        self._run(monkeypatch, ["budget", "clear", "me@work.example"])
        assert cache_db.load_budgets() == {}

    def test_clearing_an_unset_account_exits_non_zero(self, monkeypatch):
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, ["budget", "clear", "never-set"])
        assert exit_info.value.code == 1

    def test_an_empty_corpus_says_so(self, monkeypatch):
        assert "No records" in self._run(monkeypatch, ["budget"])

    def test_it_lists_the_projection_per_account(self, monkeypatch):
        self._seed()
        out = self._run(monkeypatch, ["budget"])
        assert "me@work.example" in out
        assert "calendar month" in out
        assert "projected" in out

    def test_an_account_with_no_ceiling_shows_the_projection_alone(self, monkeypatch):
        self._seed()
        out = self._run(monkeypatch, ["budget"])
        assert "% of" not in out

    def test_a_ceiling_is_shown_beside_it(self, monkeypatch):
        self._seed()
        cache_db.save_budget("me@work.example", 500.0, None, 1.0)
        assert "% of" in self._run(monkeypatch, ["budget"])

    def test_a_renewal_day_adds_the_billing_row(self, monkeypatch):
        self._seed()
        cache_db.save_budget("me@work.example", None, 8, 1.0)
        assert "billing (d8)" in self._run(monkeypatch, ["budget"])

    def test_a_sparse_account_says_it_cannot_project(self, monkeypatch):
        self._seed(days=2)
        assert "too few active days" in self._run(monkeypatch, ["budget"])
