"""The aggregation split out of ccreport.py: its rows, and the output it feeds.

Three things are asserted here. The row builders return the numbers they should
when called directly. The tables they feed render exactly what the pre-split
code rendered — tests/golden/*.txt were captured from HEAD before the split, so
a diff there means the split changed output. And the rollup path and the full
record path produce the same rows, which the golden fixtures cannot show
because a fixture exercises one path only.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import pytest
import report_fixture as fx

from ccreport import aggregate
from ccreport import ccreport as ccr

GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.fixture
def utc_clock(monkeypatch):
    """A fixed zone and a fixed now, which the rendered dates both depend on."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setattr(aggregate, "datetime", fx.FrozenDatetime)
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def corpus():
    return fx.build_records(aggregate)


@pytest.fixture
def nok():
    return aggregate.NokCtx(dict(fx.RATES), max(fx.RATES), True)


class TestGoldenOutput:
    """Byte-identical to what ccreport.py printed before the aggregation moved."""

    @pytest.mark.parametrize(("name", "width"), [("wide", fx.WIDE), ("narrow", fx.NARROW)])
    def test_every_report_renders_what_it_rendered_before_the_split(
        self, corpus, nok, utc_clock, name, width,
    ):
        expected = (GOLDEN_DIR / f"reports-{name}.txt").read_text()
        assert fx.render_all(ccr, corpus, nok, width) == expected


class TestDailyRows:
    def test_one_row_per_local_day_oldest_first(self, corpus, nok):
        report = aggregate.daily_rows(corpus, nok)
        assert [row.key for row in report.rows] == [
            "2026-02-03", "2026-02-17", "2026-02-24",
            "2026-03-02", "2026-03-09", "2026-03-12", "2026-03-14", "2026-03-15",
        ]
        assert report.n_all == 8

    def test_a_days_row_sums_the_calls_it_covers(self, corpus, nok):
        row = aggregate.daily_rows(corpus, nok).rows[0]
        assert row.agg.cost == pytest.approx(5.25)
        assert row.agg.count == 2
        assert row.agg.tokens.input == 20000

    def test_the_calls_column_adds_count_rather_than_counting_rows(self, corpus, nok):
        """A rollup record stands for a whole day of a session's calls."""
        rows = {row.key: row for row in aggregate.daily_rows(corpus, nok).rows}
        assert rows["2026-02-24"].agg.count == 37

    def test_there_is_no_breakdown_unless_asked_for(self, corpus, nok):
        assert all(not row.breakdown for row in aggregate.daily_rows(corpus, nok).rows)

    def test_the_breakdown_splits_a_day_by_model_priciest_first(self, corpus, nok):
        rows = {r.key: r for r in aggregate.daily_rows(corpus, nok, breakdown=True).rows}
        day = rows["2026-02-03"]
        assert [sub.key for sub in day.breakdown] == [
            "claude-opus-4-5-20260101", "claude-sonnet-4-5-20260101",
        ]
        assert sum(sub.agg.cost for sub in day.breakdown) == pytest.approx(day.agg.cost)

    def test_a_synthetic_model_is_counted_but_not_named(self, corpus, nok):
        """It has no price and naming it in the Models column says nothing."""
        rows = {row.key: row for row in aggregate.daily_rows(corpus, nok).rows}
        assert rows["2026-03-12"].agg.count == 1
        assert rows["2026-03-12"].agg.models == {}

    def test_the_total_covers_every_row(self, corpus, nok):
        report = aggregate.daily_rows(corpus, nok)
        assert report.total.cost == pytest.approx(sum(r.agg.cost for r in report.rows))
        assert report.all_total is report.total


class TestMonthlyRows:
    def test_one_row_per_local_month_oldest_first(self, corpus, nok):
        report = aggregate.monthly_rows(corpus, nok)
        assert [row.key for row in report.rows] == ["2026-02", "2026-03"]

    def test_a_month_sums_its_days(self, corpus, nok):
        report = aggregate.monthly_rows(corpus, nok)
        assert report.rows[0].agg.cost == pytest.approx(4.5 + 0.75 + 0.04 + 23.5)


class TestMonthProjection:
    def _projection(self, corpus, nok, now):
        report = aggregate.monthly_rows(corpus, nok)
        return aggregate.month_projection(corpus, report, nok, now=now)

    def test_the_month_to_date_figure_is_the_daily_rate_over_a_full_month(self, corpus, nok):
        proj = self._projection(corpus, nok, fx.FROZEN_NOW)
        assert proj is not None
        assert (proj.days_elapsed, proj.days_in_month, proj.month_name) == (15, 31, "March")
        march = aggregate.monthly_rows(corpus, nok).rows[-1].agg
        assert proj.month_to_date is not None
        assert proj.month_to_date.cost == pytest.approx(march.cost / 15 * 31)

    def test_the_trailing_figure_averages_the_window_instead(self, corpus, nok):
        proj = self._projection(corpus, nok, fx.FROZEN_NOW)
        assert proj is not None
        assert proj.trailing is not None
        assert proj.window_days == aggregate.TRAILING_WINDOW_DAYS
        # The window ends at midnight, so the 15th's own call is outside it.
        window = 9.96 + 0.42 + 0.09 + 0.0 + 6.2
        assert proj.trailing.cost == pytest.approx(window / 14 * 31)

    def test_a_month_already_over_projects_nothing(self, corpus, nok):
        """The last day of the month has nothing left to project onto."""
        proj = self._projection(corpus, nok, dt.datetime(2026, 3, 31, 12, 0, tzinfo=dt.UTC))
        assert proj is not None
        assert (proj.month_to_date, proj.trailing) == (None, None)

    def test_a_corpus_that_stops_before_this_month_has_no_projection(self, corpus, nok):
        older = [r for r in corpus if r.timestamp.month == 2]
        report = aggregate.monthly_rows(older, nok)
        assert aggregate.month_projection(older, report, nok, now=fx.FROZEN_NOW) is None

    def test_no_records_project_nothing_rather_than_raising(self, nok):
        report = aggregate.monthly_rows([], nok)
        assert aggregate.month_projection([], report, nok, now=fx.FROZEN_NOW) is None

    def test_the_trailing_window_stays_inside_the_rollup_window(self):
        """Past the cutoff a record stands for a whole day, which it cannot split."""
        assert aggregate.TRAILING_WINDOW_DAYS == ccr.ROLLUP_WINDOW_DAYS


class TestProjectRows:
    def test_rows_come_priciest_first(self, corpus, nok):
        report = aggregate.project_rows(corpus, nok, limit=None)
        assert [row.key for row in report.rows] == [
            "ccr-projB", "ccr-projA", "ccr-projD", "ccr-projC",
        ]

    def test_a_limit_cuts_the_rows_and_the_total_with_them(self, corpus, nok):
        report = aggregate.project_rows(corpus, nok, limit=2)
        assert [row.key for row in report.rows] == ["ccr-projB", "ccr-projA"]
        assert report.total.cost == pytest.approx(23.54 + 15.63)

    def test_the_all_total_survives_the_cut(self, corpus, nok):
        """The report prints an average across every project under the top-N one."""
        report = aggregate.project_rows(corpus, nok, limit=2)
        assert report.n_all == 4
        assert report.all_total.cost == pytest.approx(sum(r.cost_usd or 0 for r in corpus))

    def test_no_limit_shows_everything(self, corpus, nok):
        report = aggregate.project_rows(corpus, nok, limit=None)
        assert len(report.rows) == report.n_all == 4
        assert report.total.cost == pytest.approx(report.all_total.cost)


class TestAccountRows:
    def test_rows_come_priciest_first_and_nothing_is_cut(self, corpus, nok):
        report = aggregate.account_rows(corpus, nok)
        assert [row.key for row in report.rows] == [
            "me@work.example", "me@home.example", "unknown",
        ]
        assert len(report.rows) == report.n_all == 3

    def test_worth_showing_needs_two_named_accounts(self, corpus):
        assert aggregate.accounts_worth_showing(corpus)

    def test_one_account_beside_its_own_unknown_history_is_not_two(self, corpus):
        one = [r for r in corpus if r.account in {"me@work.example", "unknown"}]
        assert not aggregate.accounts_worth_showing(one)


class TestSessionRows:
    def test_rows_come_priciest_first_with_their_project_and_last_activity(self, corpus, nok):
        report = aggregate.session_rows(corpus, nok, limit=None)
        assert [row.key for row in report.rows] == [
            "sess-beta", "sess-gamma", "sess-epsilon", "sess-alpha", "sess-delta",
        ]
        top = report.rows[0]
        assert top.project == "ccr-projB"
        assert top.last == dt.datetime(2026, 2, 24, 16, 0, tzinfo=dt.UTC)

    def test_the_project_is_the_one_the_session_started_in(self, corpus, nok):
        """A session can move between projects; the first record names it."""
        rows = {row.key: row for row in aggregate.session_rows(corpus, nok, limit=None).rows}
        assert rows["sess-alpha"].project == "ccr-projA"

    def test_a_limit_keeps_the_count_of_everything(self, corpus, nok):
        report = aggregate.session_rows(corpus, nok, limit=2)
        assert len(report.rows) == 2
        assert report.n_all == 5
        assert report.all_total.cost > report.total.cost
