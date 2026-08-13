"""The merged spend page: what it totals, what it derives, and what it loads."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport import pricing
from ccreport.server import dashboard
from ccreport.server.factory import create_app

NOW = datetime.now(tz=UTC).astimezone()


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


def _ts(days_ago: int, hour: int = 12) -> float:
    return (NOW - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0,
    ).timestamp()


def _offset() -> int:
    offset = NOW.utcoffset()
    return int(offset.total_seconds()) if offset else 0


def _rec(days_ago, account, project="projA", model="claude-haiku-4-5", **over):
    return sf.record(
        mid=f"m{days_ago}{account}{project}{model}", dk=f"d{days_ago}{account}{project}{model}",
        ts=_ts(days_ago), project=project, model=model, utc_offset=_offset(),
        account_uuid=account, account_label=f"{account}@example.net", **over,
    )


@pytest.fixture
def app(tmp_path):
    """Two machines, two accounts, spread over the last two months."""
    app = create_app(sf.config(tmp_path))
    client = TestClient(app)
    for machine, label, records in (
        ("laptop-1", "Laptop", [
            _rec(1, "work"), _rec(3, "work", project="projB"),
            _rec(20, "work", model="claude-sonnet-4-5-20250929"),
            _rec(60, "work"),
        ]),
        ("desk-1", "Desk", [_rec(2, "home"), _rec(40, "home", project="projB")]),
    ):
        token = sf.mint_for(app, machine, label)
        resp = client.post(
            "/v1/ingest",
            json=sf.batch(records, path=f"/p/{machine}.jsonl", label=label),
            headers=sf.auth(token),
        )
        assert all(f["status"] == "accepted" for f in resp.json()["files"]), resp.json()
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _view(app, days=30):
    return dashboard.build(app.state.db.connect(), days, NOW)


class TestRangeBounds:
    @pytest.mark.parametrize("days", [7, 30, 90])
    def test_a_toggle_covers_exactly_its_days(self, days):
        start, end = dashboard.range_bounds(days, NOW)
        assert (end - start).days == days

    def test_the_span_ends_at_the_next_midnight(self):
        """A part-day at the end would read as a collapse in usage."""
        _start, end = dashboard.range_bounds(7, NOW)
        assert (end.hour, end.minute, end.second) == (0, 0, 0)
        assert end > NOW

    def test_each_toggle_bounds_the_query(self, app):
        """The 7-day view must not carry what only the 90-day one covers."""
        assert _view(app, 7).total_cost < _view(app, 30).total_cost
        assert _view(app, 30).total_cost < _view(app, 90).total_cost

    def test_an_unknown_range_falls_back_to_the_default(self, app):
        assert _view(app, 365).days == dashboard.DEFAULT_RANGE


class TestAllTime:
    def test_it_starts_at_the_oldest_record(self, app):
        """60 days back, which the 90-day toggle also covers but 30 does not."""
        view = _view(app, dashboard.ALL_TIME)
        assert view.start == (NOW - timedelta(days=60)).strftime("%Y-%m-%d")
        assert view.total_cost == pytest.approx(_view(app, 90).total_cost)

    def test_the_chart_axis_spans_the_whole_history(self, app):
        view = _view(app, dashboard.ALL_TIME)
        assert len(view.chart_days) == 61
        assert all(len(series.cost) == 61 for series in view.series)
        assert sum(sum(s.cost) for s in view.series) == pytest.approx(view.total_cost)

    def test_an_empty_database_falls_back_to_the_default_span(self, tmp_path):
        """A zero-day axis has no columns and reads as a broken chart."""
        app = create_app(sf.config(tmp_path))
        view = dashboard.build(app.state.db.connect(), dashboard.ALL_TIME, NOW)
        assert len(view.chart_days) == dashboard.DEFAULT_RANGE
        assert view.total_cost == 0.0

    def test_the_oldest_record_is_what_the_database_reports(self, app):
        from ccreport.server import db

        oldest = db.oldest_record_ts(app.state.db.connect())
        assert oldest == pytest.approx(_ts(60))

    def test_an_empty_database_has_no_oldest_record(self, tmp_path):
        from ccreport.server import db

        app = create_app(sf.config(tmp_path))
        assert db.oldest_record_ts(app.state.db.connect()) is None

    def test_the_toggle_reads_all_on_the_page(self, client):
        body = client.get(f"/?days={dashboard.ALL_TIME}").text
        assert ">All</a>" in body
        assert f'href="/?days={dashboard.ALL_TIME}"' in body


class TestTotals:
    def test_the_account_rows_total_to_the_headline(self, app):
        view = _view(app)
        assert sum(row.cost for row in view.accounts) == pytest.approx(view.total_cost)

    def test_the_shares_total_to_one(self, app):
        view = _view(app)
        assert sum(row.share for row in view.accounts) == pytest.approx(1.0)

    def test_the_chart_series_total_to_the_headline(self, app):
        view = _view(app)
        charted = sum(sum(series.cost) for series in view.series)
        assert charted == pytest.approx(view.total_cost)

    @pytest.mark.parametrize("dimension", dashboard.DIMENSIONS)
    def test_every_breakdown_totals_to_the_same_cost(self, app, dimension):
        """The toggle is a change of view, not a change of subject."""
        view = _view(app)
        rows = view.breakdowns[dimension]
        assert sum(row["cost"] for row in rows) == pytest.approx(view.total_cost)

    def test_the_machine_dimension_names_both_machines(self, app):
        """The one breakdown no single-machine report has."""
        keys = {row["key"] for row in _view(app).breakdowns["machine"]}
        assert keys == {"Laptop", "Desk"}

    def test_the_breakdown_comes_priciest_first(self, app):
        rows = _view(app).breakdowns["model"]
        assert [row["cost"] for row in rows] == sorted(
            (row["cost"] for row in rows), reverse=True,
        )


class TestChart:
    def test_the_axis_is_dense(self, app):
        """A quiet weekend is a zero, not a gap the line jumps across."""
        view = _view(app, 30)
        assert len(view.chart_days) == 30
        assert all(len(series.cost) == 30 for series in view.series)

    def test_there_is_one_series_per_account(self, app):
        view = _view(app)
        assert {series.account for series in view.series} == {
            row.account for row in view.accounts
        }

    def test_a_day_with_no_work_is_zero_rather_than_missing(self, app):
        view = _view(app, 30)
        assert any(value == 0.0 for series in view.series for value in series.cost)

    def test_tokens_and_cost_are_both_carried(self, app):
        view = _view(app)
        assert sum(sum(s.tokens) for s in view.series) > 0


class TestTiles:
    def test_there_are_five(self, app):
        assert len(_view(app).tiles) == 5

    def test_each_carries_a_derived_subline(self, app):
        assert all(tile.subline for tile in _view(app).tiles)

    def test_the_cache_read_multiple_matches_the_hand_computed_figure(self, tmp_path):
        """Every cache read priced at what it would have cost as fresh input."""
        app = create_app(sf.config(tmp_path))
        client = TestClient(app)
        token = sf.mint_for(app)
        model = "claude-haiku-4-5"
        record = sf.record(
            mid="m1", dk="d1", ts=_ts(1), model=model, utc_offset=_offset(),
            input_tokens=1000, output_tokens=100, cache_create=0, cache_read=200_000,
        )
        client.post("/v1/ingest", json=sf.batch([record]), headers=sf.auth(token))

        prices = pricing.find_pricing(model, datetime.fromtimestamp(_ts(1), tz=UTC))
        assert prices is not None
        expected_saved = 200_000 * prices["input"]
        actual_cost = pricing.calc_cost(
            1000, 100, 0, 200_000, model, datetime.fromtimestamp(_ts(1), tz=UTC),
        )

        tile = next(t for t in _view(app).tiles if t.label == "Cache reads vs spend")
        assert tile.value == f"{expected_saved / actual_cost:.1f}x"
        assert f"{expected_saved:.2f}" in tile.subline or "$" in tile.subline

    def test_the_cached_share_is_of_observed_input(self, tmp_path):
        app = create_app(sf.config(tmp_path))
        client = TestClient(app)
        token = sf.mint_for(app)
        record = sf.record(
            mid="m1", dk="d1", ts=_ts(1), utc_offset=_offset(),
            input_tokens=250, output_tokens=0, cache_create=250, cache_read=500,
        )
        client.post("/v1/ingest", json=sf.batch([record]), headers=sf.auth(token))
        tile = next(t for t in _view(app).tiles if t.label == "Cached input")
        assert "50% of observed input" in tile.subline

    def test_an_empty_range_still_renders_five_tiles(self, app):
        view = dashboard.build(app.state.db.connect(), 7, NOW - timedelta(days=400))
        assert len(view.tiles) == 5
        assert view.total_cost == 0.0


class TestRedactedProjects:
    def test_a_pseudonymous_project_keeps_its_numbers(self, tmp_path):
        """The point of the redaction design; the page must not hide it away."""
        app = create_app(sf.config(tmp_path))
        client = TestClient(app)
        token = sf.mint_for(app)
        record = sf.record(
            mid="m1", dk="d1", ts=_ts(1), project="a1b2c3d4", cwd=None, repo=None,
            sid="9f8e7d6c5b4a3210", utc_offset=_offset(),
        )
        client.post("/v1/ingest", json=sf.batch([record]), headers=sf.auth(token))
        rows = _view(app).breakdowns["project"]
        assert [row["key"] for row in rows] == ["a1b2c3d4"]
        assert rows[0]["cost"] > 0
        assert rows[0]["tokens"] > 0


class TestCachedBuild:
    """One build per range per push. The page is otherwise the corpus, folded
    again, on every render."""

    def _push(self, app, days_ago=5):
        client = TestClient(app)
        token = sf.mint_for(app, "laptop-1", "Laptop")
        resp = client.post(
            "/v1/ingest",
            json=sf.batch([_rec(days_ago, "work", project="projC")],
                          path=f"/p/extra{days_ago}.jsonl", label="Laptop"),
            headers=sf.auth(token),
        )
        assert resp.json()["files"][0]["status"] == "accepted", resp.json()

    def test_a_second_render_of_the_same_database_reuses_the_first(self, app):
        first = dashboard.cached_build(app.state.db, 30, NOW)
        assert dashboard.cached_build(app.state.db, 30, NOW) is first

    def test_a_push_invalidates_it(self, app):
        first = dashboard.cached_build(app.state.db, 30, NOW)
        self._push(app)
        second = dashboard.cached_build(app.state.db, 30, NOW)
        assert second is not first
        assert second.total_cost > first.total_cost

    def test_a_new_day_invalidates_it(self, app):
        """The ranges end at the next midnight, so yesterday's axis is wrong."""
        first = dashboard.cached_build(app.state.db, 30, NOW)
        second = dashboard.cached_build(app.state.db, 30, NOW + timedelta(days=1))
        assert second is not first
        assert second.chart_days[-1] != first.chart_days[-1]

    def test_each_range_is_cached_apart(self, app):
        week = dashboard.cached_build(app.state.db, 7, NOW)
        month = dashboard.cached_build(app.state.db, 30, NOW)
        assert week is not month
        assert dashboard.cached_build(app.state.db, 7, NOW) is week

    def test_an_unknown_range_shares_the_default_entry(self, app):
        assert dashboard.cached_build(app.state.db, 999, NOW) is dashboard.cached_build(
            app.state.db, dashboard.DEFAULT_RANGE, NOW,
        )

    def test_two_databases_do_not_share_an_entry(self, app, tmp_path):
        """Two empty ones stamp identically, so the path has to be in the key."""
        other = create_app(sf.config(tmp_path / "other"))
        assert dashboard.cached_build(other.state.db, 30, NOW).total_cost == 0.0
        assert dashboard.cached_build(app.state.db, 30, NOW).total_cost > 0.0

    def test_the_page_serves_the_cached_view(self, app):
        view = dashboard.cached_build(app.state.db, 30, NOW)
        TestClient(app).get("/?days=30")
        assert dashboard.cached_build(app.state.db, 30, NOW) is view


class TestPage:
    def test_it_renders_the_headline_and_the_footnote(self, client):
        body = client.get("/").text
        assert "Tokens priced at full API rates." in body
        assert "Accounts" in body
        assert "Breakdown" in body

    def test_the_range_toggles_are_links(self, client):
        body = client.get("/").text
        for days in dashboard.RANGES:
            assert f'href="/?days={days}"' in body

    def test_the_selected_range_is_marked(self, client):
        body = client.get("/?days=7").text
        assert 'class="toggle on" href="/?days=7"' in body

    def test_every_breakdown_dimension_has_a_table(self, client):
        body = client.get("/").text
        for dimension in dashboard.DIMENSIONS:
            assert f'data-dimension="{dimension}"' in body

    def test_the_chart_data_is_embedded(self, client):
        body = client.get("/").text
        assert 'id="chart-data"' in body
        assert '"series"' in body

    def test_it_is_behind_the_network_allowlist(self, tmp_path):
        gated = create_app(sf.config(tmp_path, networks=sf.ELSEWHERE))
        assert TestClient(gated).get("/").status_code == 403


class TestNoExternalOrigin:
    """The page has to draw on a laptop with no internet."""

    ASSETS = Path(__file__).resolve().parents[1] / "src" / "ccreport" / "server"

    def _sources(self) -> list[Path]:
        return [
            *(self.ASSETS / "templates").glob("*.html"),
            *(self.ASSETS / "static").glob("*.css"),
            *(self.ASSETS / "static").glob("*.js"),
        ]

    def test_no_template_or_asset_references_a_remote_origin(self):
        pattern = re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""")
        offenders = [
            path.name for path in self._sources() if pattern.search(path.read_text())
        ]
        assert offenders == []

    def test_the_chart_library_is_vendored(self):
        vendor = self.ASSETS / "static" / "vendor"
        assert (vendor / "uPlot.iife.min.js").exists()
        assert (vendor / "uPlot.min.css").exists()

    def test_the_vendored_copy_records_where_it_came_from(self):
        """Nothing updates it automatically, so the version has to be written down."""
        readme = (self.ASSETS / "static" / "vendor" / "README.md").read_text()
        assert "uPlot" in readme
        assert "1.6.32" in readme

    def test_the_page_loads_the_vendored_copy(self, client):
        body = client.get("/").text
        assert "/static/vendor/uPlot.iife.min.js" in body
        assert "cdn." not in body
