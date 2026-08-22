"""The merged spend page: what it totals, what it derives, and what it loads."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import server_fixture as sf
from _narrow import present
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
        assert len(view.chart_days) == dashboard.EMPTY_SPAN_DAYS
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
        assert f'href="/?days={dashboard.ALL_TIME}&by=model&metric=cost"' in body


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
    def _redacted(self, tmp_path, count=2):
        """A machine that opted nothing in: *count* records, all identity stripped."""
        app = create_app(sf.config(tmp_path))
        token = sf.mint_for(app)
        records = [
            sf.record(mid=f"m{n}", dk=f"d{n}", ts=_ts(n + 1), project=None, cwd=None,
                      repo=None, sid=None, utc_offset=_offset())
            for n in range(count)
        ]
        TestClient(app).post("/v1/ingest", json=sf.batch(records), headers=sf.auth(token))
        return app

    def test_the_bucket_keeps_its_numbers(self, tmp_path):
        """The point of the redaction design; the page must not hide it away."""
        rows = _view(self._redacted(tmp_path)).breakdowns["project"]
        assert rows[0]["cost"] > 0
        assert rows[0]["tokens"] > 0

    def test_every_redacted_project_is_one_row_named_for_the_account(self, tmp_path):
        """A row per project would count the private projects and price each."""
        rows = _view(self._redacted(tmp_path)).breakdowns["project"]
        assert [row["key"] for row in rows] == ["me@example.net/aggregated"]
        assert rows[0]["calls"] == 2

    def test_naming_the_account_names_the_bucket(self, tmp_path):
        from ccreport.server import db

        app = self._redacted(tmp_path)
        conn = app.state.db.connect()
        db.set_account_alias(conn, "acct-1", "personal", 1.0)
        conn.commit()
        rows = _view(app).breakdowns["project"]
        assert [row["key"] for row in rows] == ["personal-aggregated"]


class TestTogglesInTheURL:
    """Which breakdown and which chart series the page opens on."""

    def _row(self, body: str, dimension: str) -> str:
        """The clip wrapper's tag for one dimension, hidden attribute and all."""
        tag = re.search(rf'<div class="breakdown-clip" data-dimension="{dimension}"[^>]*>', body)
        assert tag, f"no {dimension} table on the page"
        return tag[0]

    def test_the_default_is_the_first_dimension(self, client):
        body = client.get("/").text
        assert "hidden" not in self._row(body, "model")
        assert "hidden" in self._row(body, "project")

    @pytest.mark.parametrize("dimension", dashboard.DIMENSIONS)
    def test_by_opens_that_table_and_no_other(self, client, dimension):
        body = client.get(f"/?by={dimension}").text
        showing = [d for d in dashboard.DIMENSIONS if "hidden" not in self._row(body, d)]
        assert showing == [dimension]

    def test_by_marks_that_tab(self, client):
        body = client.get("/?by=machine").text
        assert 'class="toggle dimension on" data-dimension="machine"' in body

    def test_metric_marks_that_tab(self, client):
        body = client.get("/?metric=tokens").text
        assert 'class="toggle metric on" data-metric="tokens"' in body

    def test_the_default_metric_is_cost(self, client):
        assert 'class="toggle metric on" data-metric="cost"' in client.get("/").text

    @pytest.mark.parametrize("query", ["?by=nonsense", "?metric=nonsense", ""])
    def test_a_value_the_page_has_no_tab_for_falls_back(self, client, query):
        """A hand-edited URL draws the page it would have drawn with no query."""
        body = client.get(f"/{query}").text
        assert 'class="toggle dimension on" data-dimension="model"' in body
        assert 'class="toggle metric on" data-metric="cost"' in body

    def test_a_range_link_carries_both_toggles(self, client):
        """Switching the range keeps the tab, which is the point of the round trip."""
        body = client.get("/?days=7&by=project&metric=tokens").text
        assert 'href="/?days=90&by=project&metric=tokens"' in body

    def test_a_dimension_link_carries_the_range_and_the_metric(self, client):
        body = client.get("/?days=0&by=project&metric=tokens").text
        assert 'href="/?days=0&by=machine&metric=tokens"' in body

    def test_neither_toggle_costs_a_build(self, app):
        """They pick what shows; the view carries every breakdown and both series."""
        client = TestClient(app)
        client.get("/?by=project&metric=tokens")
        first = dashboard.cached_build(app.state.db, dashboard.DEFAULT_RANGE)
        client.get("/?by=machine&metric=cost")
        assert dashboard.cached_build(app.state.db, dashboard.DEFAULT_RANGE) is first


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

    def test_an_alias_invalidates_it(self, app):
        """No push is behind a rename, so content_stamp has to read the aliases."""
        from ccreport.server import db

        first = dashboard.cached_build(app.state.db, 30, NOW)
        conn = app.state.db.connect()
        db.set_account_alias(conn, "work", "personal", 1.0)
        conn.commit()
        second = dashboard.cached_build(app.state.db, 30, NOW)
        assert second is not first
        assert "personal" in [row.account for row in second.accounts]
        assert "work@example.net" not in [row.account for row in second.accounts]

    def test_clearing_an_alias_invalidates_it_too(self, app):
        from ccreport.server import db

        conn = app.state.db.connect()
        db.set_account_alias(conn, "work", "personal", 1.0)
        conn.commit()
        renamed = dashboard.cached_build(app.state.db, 30, NOW)
        db.set_account_alias(conn, "work", "", 2.0)
        conn.commit()
        back = dashboard.cached_build(app.state.db, 30, NOW)
        assert back is not renamed
        assert "work@example.net" in [row.account for row in back.accounts]

    def test_the_page_serves_the_cached_view(self, app):
        view = dashboard.cached_build(app.state.db, 30, NOW)
        TestClient(app).get("/?days=30")
        assert dashboard.cached_build(app.state.db, 30, NOW) is view


class TestCachedDetail:
    """One build per entity per push, and a bound the index cache does not need."""

    @pytest.fixture(autouse=True)
    def empty_cache(self):
        dashboard._DETAIL_CACHE.clear()
        yield
        dashboard._DETAIL_CACHE.clear()

    def _scope(self, key="projA", dimension="project"):
        return dashboard.Scope(dimension=dimension, key=key)

    def test_a_second_render_of_the_same_entity_reuses_the_first(self, app):
        first = dashboard.cached_detail(app.state.db, 30, self._scope(), NOW)
        assert dashboard.cached_detail(app.state.db, 30, self._scope(), NOW) is first

    def test_each_entity_is_cached_apart(self, app):
        a = dashboard.cached_detail(app.state.db, 30, self._scope("projA"), NOW)
        b = dashboard.cached_detail(app.state.db, 30, self._scope("projB"), NOW)
        assert a is not b
        assert dashboard.cached_detail(app.state.db, 30, self._scope("projA"), NOW) is a

    def test_each_range_of_one_entity_is_cached_apart(self, app):
        week = dashboard.cached_detail(app.state.db, 7, self._scope(), NOW)
        month = dashboard.cached_detail(app.state.db, 30, self._scope(), NOW)
        assert week is not month

    def test_a_push_invalidates_it(self, app):
        first = dashboard.cached_detail(app.state.db, 30, self._scope(), NOW)
        client = TestClient(app)
        token = sf.mint_for(app, "laptop-1", "Laptop")
        resp = client.post(
            "/v1/ingest",
            json=sf.batch([_rec(5, "work")], path="/p/extra5.jsonl", label="Laptop"),
            headers=sf.auth(token),
        )
        assert resp.json()["files"][0]["status"] == "accepted", resp.json()
        second = dashboard.cached_detail(app.state.db, 30, self._scope(), NOW)
        assert second is not first
        assert second.total_cost > first.total_cost

    def test_a_new_day_invalidates_it(self, app):
        first = dashboard.cached_detail(app.state.db, 30, self._scope(), NOW)
        second = dashboard.cached_detail(
            app.state.db, 30, self._scope(), NOW + timedelta(days=1),
        )
        assert second is not first

    def test_the_oldest_entry_goes_at_the_bound(self, app, monkeypatch):
        """Unbounded, the cache is one entry per day this server ever saw."""
        monkeypatch.setattr(dashboard._DETAIL_CACHE, "limit", 3)
        first = dashboard.cached_detail(app.state.db, 30, self._scope("projA"), NOW)
        for key in ("projB", "projC", "projD"):
            dashboard.cached_detail(app.state.db, 30, self._scope(key), NOW)
        assert len(dashboard._DETAIL_CACHE) == 3
        assert dashboard.cached_detail(
            app.state.db, 30, self._scope("projA"), NOW) is not first
        assert dashboard.cached_detail(
            app.state.db, 30, self._scope("projD"), NOW) is dashboard.cached_detail(
            app.state.db, 30, self._scope("projD"), NOW)

    def test_serving_an_entry_keeps_it_from_being_evicted(self, app, monkeypatch):
        monkeypatch.setattr(dashboard, "DETAIL_CACHE_MAX", 2)
        first = dashboard.cached_detail(app.state.db, 30, self._scope("projA"), NOW)
        dashboard.cached_detail(app.state.db, 30, self._scope("projB"), NOW)
        assert dashboard.cached_detail(app.state.db, 30, self._scope("projA"), NOW) is first
        dashboard.cached_detail(app.state.db, 30, self._scope("projC"), NOW)
        assert dashboard.cached_detail(app.state.db, 30, self._scope("projA"), NOW) is first

    def test_two_databases_do_not_share_an_entry(self, app, tmp_path):
        other = create_app(sf.config(tmp_path / "other"))
        assert dashboard.cached_detail(
            other.state.db, 30, self._scope(), NOW).total_cost == 0.0
        assert dashboard.cached_detail(
            app.state.db, 30, self._scope(), NOW).total_cost > 0.0

    def test_a_period_key_that_is_not_a_date_still_raises(self, app):
        """Nothing is stored for it; the page turns it into a 404."""
        with pytest.raises(ValueError):
            dashboard.cached_detail(app.state.db, 30, self._scope("2026-13", "month"), NOW)
        assert not dashboard._DETAIL_CACHE

    def test_the_page_serves_the_cached_view(self, app):
        view = dashboard.cached_detail(
            app.state.db, dashboard.DEFAULT_RANGE, self._scope(), NOW)
        assert TestClient(app).get("/project/projA").status_code == 200
        assert dashboard.cached_detail(
            app.state.db, dashboard.DEFAULT_RANGE, self._scope(), NOW) is view


class TestPage:
    def test_it_renders_the_headline_and_the_footnote(self, client):
        body = client.get("/").text
        assert "Tokens priced at full API rates." in body
        assert "Accounts" in body
        assert "Breakdown" in body

    def test_the_range_toggles_are_links(self, client):
        body = client.get("/").text
        for days in dashboard.RANGES:
            assert f'href="/?days={days}&by=model&metric=cost"' in body

    def test_a_page_with_no_range_opens_on_all_time(self, client):
        body = client.get("/").text
        assert dashboard.DEFAULT_RANGE == dashboard.ALL_TIME
        assert ('class="toggle on"\n       aria-current="true"\n'
                f'       href="/?days={dashboard.ALL_TIME}&by=model&metric=cost"') in body

    def test_the_header_dates_open_their_day_pages(self, client):
        view = dashboard.cached_build(client.app.state.db, dashboard.DEFAULT_RANGE)
        body = client.get("/").text
        assert f'<a href="/day/{view.start}" data-day="{view.start}">' in body
        assert f'<a href="/day/{view.end}" data-day="{view.end}">' in body

    def test_the_selected_range_is_marked(self, client):
        body = client.get("/?days=7").text
        assert ('class="toggle on"\n       aria-current="true"\n'
                '       href="/?days=7&by=model&metric=cost"') in body

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


class TestSortableTables:
    """Every page's tables re-order on a click; the script is what does it."""

    @pytest.mark.parametrize("path", ["/", "/settings/machines", "/settings/accounts",
                                      "/model/claude-haiku-4-5"])
    def test_the_page_loads_the_sort_script(self, client, path):
        assert "/static/sort.js" in client.get(path).text

    def test_it_loads_after_format_js(self, client):
        """The listener registered second runs second, on the text format.js left."""
        body = client.get("/").text
        assert body.index("/static/format.js") < body.index("/static/sort.js")

    def test_the_dashboard_breakdown_is_not_clipped(self, client):
        """One table is on that page at a time, so the window scrolls it."""
        assert "table-clip" not in client.get("/").text


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


class TestPlanColumn:
    """A month's spend, beside what the plan behind it cost."""

    def _declare(self, app, account, *entries):
        from ccreport import tier_timeline
        from ccreport.server import db

        conn = app.state.db.connect()
        db.set_account_tiers(conn, account, [
            tier_timeline.Entry(
                ts=ts, account=account, organization_rate_limit_tier=tier,
            )
            for ts, tier in entries
        ], 1.0)
        conn.commit()

    def _months(self, app, days=90):
        return {row["key"]: row for row in _view(app, days).breakdowns["month"]}

    def _long_ago(self):
        return datetime(2020, 1, 1, tzinfo=UTC).timestamp()

    def test_a_month_with_no_declared_plan_carries_no_figure(self, app):
        """Not a zero, which beside real spend reads as a month that was free."""
        rows = self._months(app)
        assert rows
        assert all("plan_usd" not in row for row in rows.values())

    def test_a_declared_plan_prices_every_month_on_the_page(self, app):
        self._declare(app, "work", (self._long_ago(), "default_claude_max_5x"))
        rows = self._months(app)
        assert all(row["plan_usd"] == pytest.approx(100.0) for row in rows.values())

    def test_two_accounts_sum_into_one_figure(self, app):
        """The whole-server page sets one plan cost against one valuation."""
        self._declare(app, "work", (self._long_ago(), "default_claude_max_5x"))
        self._declare(app, "home", (self._long_ago(), "default_claude_pro"))
        rows = self._months(app)
        assert all(row["plan_usd"] == pytest.approx(120.0) for row in rows.values())

    def test_an_unpriced_tier_leaves_the_month_unpriced(self, app):
        self._declare(app, "work", (self._long_ago(), "default_raven"))
        assert all("plan_usd" not in row for row in self._months(app).values())

    def test_only_the_month_breakdown_gains_the_figure(self, app):
        """A model has no subscription; a day is shorter than a billing cycle."""
        self._declare(app, "work", (self._long_ago(), "default_claude_max_5x"))
        view = _view(app)
        for name, rows in view.breakdowns.items():
            if name != "month":
                assert all("plan_usd" not in row for row in rows)

    def test_the_page_draws_the_column(self, app, client):
        self._declare(app, "work", (self._long_ago(), "default_claude_max_5x"))
        body = client.get("/?days=90&by=month").text
        assert ">Plan</th>" in body
        assert "$100.00" in body

    def test_the_column_is_absent_from_the_other_tables(self, app, client):
        body = client.get("/?days=90&by=model").text
        assert body.count(">Plan</th>") == 1


class TestPlanTile:
    """What the spend was worth against the subscription that bought it."""

    def _declare(self, app, account="work", tier="default_claude_max_5x"):
        from ccreport import tier_timeline
        from ccreport.server import db

        conn = app.state.db.connect()
        db.set_account_tiers(conn, account, [tier_timeline.Entry(
            ts=datetime(2020, 1, 1, tzinfo=UTC).timestamp(), account=account,
            organization_rate_limit_tier=tier,
        )], 1.0)
        conn.commit()

    def _tile(self, view):
        return next((t for t in view.tiles if t.label == "Value vs plan"), None)

    def test_no_declared_plan_shows_no_tile(self, app):
        """Not a zero and not an infinite multiple: there is nothing to divide by."""
        assert self._tile(_view(app)) is None

    def test_it_shows_the_multiple_over_the_plan(self, app):
        self._declare(app)
        view = _view(app)
        tile = present(self._tile(view))
        plan = dashboard._Plans(
            app.state.db.connect(), {row.account for row in view.accounts},
        ).cost(*dashboard.range_bounds(30, NOW))
        assert tile.value == f"{view.total_cost / present(plan):.1f}x"

    def test_the_subline_names_a_quantity_of_subscription_not_a_price(self, app):
        """"the $140 plan" reads as a plan costing $140; it is a share of months."""
        self._declare(app)
        tile = present(self._tile(_view(app)))
        assert "of subscription across" in tile.subline
        assert "valued at API rates" in tile.subline
        assert "30 days" in tile.subline

    def test_a_span_worth_more_than_its_plan_says_more(self, app, monkeypatch):
        """The fixture corpus is cents, so the plan has to be cheaper still."""
        monkeypatch.setattr(pricing, "PLAN_PRICES", [
            {"effective": "2020-01-01", "plans": {"default_claude_pro": 0.01}},
        ])
        monkeypatch.setattr(pricing, "_PLAN_INDEX", None)
        self._declare(app, tier="default_claude_pro")
        assert "more than the" in present(self._tile(_view(app))).subline

    def test_it_sits_after_the_five_that_were_there(self, app):
        self._declare(app)
        assert [t.label for t in _view(app).tiles][-1] == "Value vs plan"

    @pytest.mark.parametrize("dimension", ["day", "week", "month", "account"])
    def test_a_page_about_a_whole_span_or_one_account_says_nothing_extra(
        self, app, dimension,
    ):
        """Both sides cover the same ground, so no qualifier is needed."""
        self._declare(app)
        assert dimension not in dashboard.SLICE_SCOPES
        base = _view(app)
        # The whole-server page has no account table; accounts are a column.
        key = (base.accounts[0].account if dimension == "account"
               else base.breakdowns[dimension][0]["key"])
        view = dashboard.build(
            app.state.db.connect(), 30, NOW, dashboard.Scope(dimension, key),
        )
        tile = self._tile(view)
        if tile is not None:
            assert " alone" not in tile.subline

    @pytest.mark.parametrize("dimension", ["model", "project", "machine"])
    def test_a_slice_of_spend_shows_it_and_says_it_is_a_slice(self, app, dimension):
        """One project outrunning the plan by itself is the interesting case."""
        self._declare(app)
        assert dimension in dashboard.SLICE_SCOPES
        view = dashboard.build(
            app.state.db.connect(), 30, NOW,
            dashboard.Scope(dimension, _view(app).breakdowns[dimension][0]["key"]),
        )
        tile = present(self._tile(view))
        assert f"this {dimension} alone" in tile.subline

    def test_an_account_page_prices_that_accounts_plan_alone(self, app):
        self._declare(app, "work")
        self._declare(app, "home", "default_claude_pro")
        whole = present(dashboard._Plans(
            app.state.db.connect(), {"work@example.net", "home@example.net"},
        ).cost(*dashboard.range_bounds(30, NOW)))
        just_work = present(dashboard._Plans(
            app.state.db.connect(), {"work@example.net"},
        ).cost(*dashboard.range_bounds(30, NOW)))
        assert just_work < whole

    def test_a_renamed_account_still_finds_its_plan(self, app):
        """_Plans matches display names, and an alias replaces the pushed label."""
        from ccreport.server import db

        self._declare(app, "work")
        conn = app.state.db.connect()
        db.set_account_alias(conn, "work", "personal", 1.0)
        conn.commit()
        priced = dashboard._Plans(conn, {"personal"}).cost(*dashboard.range_bounds(30, NOW))
        assert priced is not None
        assert dashboard._Plans(
            conn, {"work@example.net"}).cost(*dashboard.range_bounds(30, NOW)) is None

    def test_the_page_draws_it(self, app, client):
        self._declare(app)
        assert "Value vs plan" in client.get("/?days=30").text

    def test_a_month_row_carries_the_multiple_and_what_it_saved(self, app):
        self._declare(app)
        rows = _view(app, 90).breakdowns["month"]
        for row in rows:
            assert row["plan_multiple"] == pytest.approx(row["cost"] / row["plan_usd"])
            assert row["plan_saved"] == pytest.approx(row["cost"] - row["plan_usd"])

    def test_the_month_table_heads_the_column(self, app, client):
        self._declare(app)
        assert ">vs plan</th>" in client.get("/?days=90&by=month").text

    def test_the_saved_amount_converts_to_nok(self, app, monkeypatch):
        """At the span's own Oslo date, under the page's MVA setting."""
        from ccreport import exchange

        monkeypatch.setattr(
            exchange, "read_rates_since",
            lambda since: {NOW.date().isoformat(): 10.0},
        )
        self._declare(app)
        tile = present(self._tile(_view(app)))
        assert "kr " in tile.subline

    def test_no_rate_leaves_the_nok_out_rather_than_guessing(self, app):
        self._declare(app)
        assert "kr " not in present(self._tile(_view(app))).subline

    def test_a_span_worth_less_than_its_plan_says_less(self, app):
        """Not "-$99.93 more than", which prints a sign where a word belongs."""
        self._declare(app)
        tile = present(self._tile(_view(app)))
        assert "less than the" in tile.subline
        assert "-$" not in tile.subline

    def test_a_month_row_carries_its_saved_amount_in_kroner(self, app, monkeypatch):
        """At the month's own last day, not at today's rate."""
        from ccreport import exchange

        monkeypatch.setattr(
            exchange, "read_rates_since", lambda since: {NOW.date().isoformat(): 10.0},
        )
        self._declare(app)
        rows = [r for r in _view(app, 90).breakdowns["month"] if r.get("plan_saved_nok")]
        assert rows
        for row in rows:
            assert row["plan_saved_nok"] == pytest.approx(row["plan_saved"] * 10.0 * 1.25)

    def test_no_rate_leaves_a_month_row_without_kroner(self, app):
        rows = _view(app, 90).breakdowns["month"]
        assert rows
        assert all(row.get("plan_saved_nok") is None for row in rows)

    @pytest.mark.parametrize(
        ("months", "days", "expected"),
        [(6.7, 203, "6.7 months"), (1.05, 32, "1.1 months"),
         (1.0, 31, "31 days"), (0.26, 8, "8 days")],
    )
    def test_the_span_reads_in_whichever_unit_is_a_quantity(self, months, days, expected):
        """"0.3 months" is a fraction to convert; "8 days" is already converted."""
        assert dashboard._fmt_span(months, days) == expected


class TestPlanSpanOnScopedPages:
    """The denominator has to cover the same period as the numerator."""

    def _declare(self, app):
        from ccreport import tier_timeline
        from ccreport.server import db

        conn = app.state.db.connect()
        db.set_account_tiers(conn, "work", [tier_timeline.Entry(
            ts=datetime(2020, 1, 1, tzinfo=UTC).timestamp(), account="work",
            organization_rate_limit_tier="default_claude_max_5x",
        )], 1.0)
        conn.commit()

    def _tile(self, view):
        return next((t for t in view.tiles if t.label == "Value vs plan"), None)

    def _scoped(self, app, dimension, key, days=90):
        return dashboard.build(
            app.state.db.connect(), days, NOW, dashboard.Scope(dimension, key),
        )

    def test_a_project_prices_the_span_it_ran_over_not_the_range(self, app):
        """projB's records cover part of the 90 days the toggle reaches back."""
        self._declare(app)
        start, end = dashboard.range_bounds(90, NOW)
        merged = dashboard.reports.load_grouped(
            app.state.db.connect(), dashboard.reports.Filters(since=start, until=end),
        )
        first, last = dashboard._active_span(
            [m for m in merged if m.record.project == "projB"], start, end,
        )
        assert (first, last) != (start, end)
        months = pricing.months_in_span(first, last)
        scoped = present(self._tile(self._scoped(app, "project", "projB")))
        assert dashboard._fmt_span(months, (last - first).days) in scoped.subline

    def test_the_scoped_figure_is_smaller_than_the_whole_ranges(self, app):
        self._declare(app)
        plans = dashboard._Plans(app.state.db.connect(), {"work@example.net"})
        start, end = dashboard.range_bounds(90, NOW)
        merged = dashboard.reports.load_grouped(
            app.state.db.connect(), dashboard.reports.Filters(since=start, until=end),
        )
        narrow = dashboard._active_span(
            [m for m in merged if m.record.project == "projB"], start, end,
        )
        assert present(plans.cost(*narrow)) < present(plans.cost(start, end))

    def test_a_month_page_still_charges_the_whole_month(self, app):
        """The month was paid for whether or not every day of it was worked."""
        self._declare(app)
        key = _view(app, 90).breakdowns["month"][0]["key"]
        tile = present(self._tile(self._scoped(app, "month", key)))
        opens, closes = dashboard._month_bounds(key)
        plans = dashboard._Plans(app.state.db.connect(), {"work@example.net"})
        expected = present(plans.cost(opens, closes))
        assert f"${expected:,.0f}" in tile.subline.replace("$100.00", "$100")

    def test_the_whole_server_page_prices_its_whole_range(self, app):
        """No scope, so the range is the question and the bound is not applied."""
        self._declare(app)
        start, end = dashboard.range_bounds(90, NOW)
        expected = dashboard._fmt_span(
            pricing.months_in_span(start, end), (end - start).days,
        )
        assert expected in present(self._tile(_view(app, 90))).subline

    def test_an_empty_page_falls_back_to_the_range(self, app):
        start, end = dashboard.range_bounds(90, NOW)
        assert dashboard._active_span([], start, end) == (start, end)

    def test_the_bound_never_widens_past_the_range(self, app):
        self._declare(app)
        start, end = dashboard.range_bounds(7, NOW)
        merged = dashboard.reports.load_grouped(app.state.db.connect())
        first, last = dashboard._active_span(merged, start, end)
        assert first >= start
        assert last <= end


class TestStampCache:
    """The one mechanism behind every held view: the index, an entity page, a
    window list, a window and the two /settings tables."""

    def test_a_second_get_under_one_stamp_reuses_the_first(self):
        cache: dashboard.StampCache[object] = dashboard.StampCache()
        first = cache.get(("k",), ("s",), object)
        assert cache.get(("k",), ("s",), object) is first

    def test_a_moved_stamp_rebuilds(self):
        cache: dashboard.StampCache[object] = dashboard.StampCache()
        first = cache.get(("k",), ("s",), object)
        assert cache.get(("k",), ("t",), object) is not first

    def test_each_key_is_held_apart(self):
        cache: dashboard.StampCache[object] = dashboard.StampCache()
        a = cache.get(("a",), ("s",), object)
        b = cache.get(("b",), ("s",), object)
        assert a is not b
        assert cache.get(("a",), ("s",), object) is a

    def test_no_limit_holds_everything(self):
        cache: dashboard.StampCache[object] = dashboard.StampCache()
        for n in range(50):
            cache.get((n,), ("s",), object)
        assert len(cache) == 50

    def test_the_least_recently_served_goes_at_the_limit(self):
        cache: dashboard.StampCache[object] = dashboard.StampCache(2)
        first = cache.get(("a",), ("s",), object)
        cache.get(("b",), ("s",), object)
        cache.get(("a",), ("s",), object)
        cache.get(("c",), ("s",), object)
        assert len(cache) == 2
        assert cache.get(("a",), ("s",), object) is first

    def test_a_build_that_raises_stores_nothing(self):
        """A 404 on a mistyped URL has to be a 404 on the next request too."""
        cache: dashboard.StampCache[object] = dashboard.StampCache()

        def boom():
            raise LookupError("nope")

        with pytest.raises(LookupError):
            cache.get(("k",), ("s",), boom)
        assert len(cache) == 0

    def test_clear_empties_it(self):
        cache: dashboard.StampCache[object] = dashboard.StampCache()
        cache.get(("k",), ("s",), object)
        cache.clear()
        assert len(cache) == 0


class TestCacheStamp:
    def test_a_push_moves_it(self, app):
        conn = app.state.db.connect()
        before = dashboard.cache_stamp(conn, NOW)
        client = TestClient(app)
        token = sf.mint_for(app, "laptop-1", "Laptop")
        client.post(
            "/v1/ingest",
            json=sf.batch([_rec(4, "work", project="projD")], path="/p/stamp.jsonl"),
            headers=sf.auth(token),
        )
        assert dashboard.cache_stamp(conn, NOW) != before

    def test_a_new_day_moves_it(self, app):
        conn = app.state.db.connect()
        assert dashboard.cache_stamp(conn, NOW) != dashboard.cache_stamp(
            conn, NOW + timedelta(days=1),
        )
