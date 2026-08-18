"""One entity's page: what it scopes to, what it charts, and what it links to."""

from __future__ import annotations

import calendar
import json
import re
from datetime import UTC, date, datetime, timedelta

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport.server import dashboard
from ccreport.server.factory import create_app

NOW = datetime.now(tz=UTC).astimezone()
TODAY = NOW.strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


def _ts(days_ago: int, hour: int) -> float:
    return (NOW - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0,
    ).timestamp()


def _offset() -> int:
    offset = NOW.utcoffset()
    return int(offset.total_seconds()) if offset else 0


def _rec(days_ago, hour, account, project, model):
    return sf.record(
        mid=f"m{days_ago}{hour}{account}{project}{model}",
        dk=f"d{days_ago}{hour}{account}{project}{model}",
        ts=_ts(days_ago, hour), project=project, model=model, utc_offset=_offset(),
        account_uuid=account, account_label=f"{account}@example.net",
    )


@pytest.fixture
def app(tmp_path):
    """One machine, two accounts, two projects, two models, three hours a day."""
    app = create_app(sf.config(tmp_path))
    client = TestClient(app)
    records = [
        _rec(days_ago, hour, account, project, model)
        for days_ago in (0, 2, 9)
        for hour in (9, 14, 20)
        for account in ("work", "home")
        for project in ("infrastructure", "ccreport")
        for model in ("claude-opus-4-5-20251101", "claude-haiku-4-5")
    ]
    token = sf.mint_for(app, "neo", "neo")
    resp = client.post(
        "/v1/ingest", json=sf.batch(records, path="/p/neo.jsonl", label="neo"),
        headers=sf.auth(token),
    )
    assert all(f["status"] == "accepted" for f in resp.json()["files"]), resp.json()
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _charts(body: str) -> list[dict]:
    payload = re.search(
        r'<script id="chart-data" type="application/json">(.*?)</script>', body, re.DOTALL,
    )
    assert payload, "the page carries no chart payload"
    return json.loads(payload[1])


PAGES = {
    "account": "work@example.net",
    "model": "claude-haiku-4-5",
    "day": TODAY,
    "project": "infrastructure",
    "machine": "neo",
}


class TestRouting:
    @pytest.mark.parametrize(("dimension", "key"), PAGES.items())
    def test_every_dimension_has_a_page(self, client, dimension, key):
        resp = client.get(f"/{dimension}/{key}")
        assert resp.status_code == 200
        assert key in resp.text

    def test_a_dimension_this_server_does_not_break_down_is_a_404(self, client):
        """A typed URL, not an idle month — which is what an empty page would read as."""
        assert client.get("/session/abc").status_code == 404

    def test_the_static_files_still_win_their_path(self, client):
        """The catch-all would answer 404 for every asset if it were matched first."""
        assert client.get("/static/app.css").status_code == 200

    def test_a_settings_page_still_wins_its_path(self, client):
        assert client.get("/settings/machines").status_code == 200

    def test_a_key_with_a_slash_survives_the_round_trip(self, client):
        resp = client.get("/project/one/two")
        assert resp.status_code == 200
        assert "one/two" in resp.text

    def test_an_entity_with_no_records_is_an_empty_page(self, client):
        body = client.get("/machine/never-pushed").text
        assert "Nothing in this range." in body
        assert "$0.00" in body

    def test_the_page_is_behind_the_network_gate(self, tmp_path):
        gated = TestClient(create_app(sf.config(tmp_path, networks=["10.0.0.0/8"])))
        assert gated.get("/project/infrastructure").status_code == 403


class TestScoping:
    def test_the_total_matches_that_row_on_the_dashboard(self, app, client):
        row = next(
            r for r in dashboard.cached_build(app.state.db, 30).breakdowns["project"]
            if r["key"] == "infrastructure"
        )
        assert f"${row['cost']:,.2f}" in client.get("/project/infrastructure").text

    def test_a_model_page_carries_that_model_alone(self, client):
        body = client.get("/model/claude-haiku-4-5").text
        assert "claude-opus-4-5-20251101" not in body

    def test_the_scoped_dimension_has_no_table_of_its_own(self, client):
        """It would be one row, restating the headline."""
        body = client.get("/project/infrastructure").text
        assert "By project" not in body
        assert "By machine" in body

    def test_the_range_toggle_narrows_the_page(self, app):
        client = TestClient(app)
        week = client.get("/project/infrastructure?days=7").text
        month = client.get("/project/infrastructure?days=30").text
        assert 'class="toggle on"' in week
        assert _total(week) < _total(month)

    def test_a_scoped_build_does_not_enter_the_shared_cache(self, app):
        """That cache is keyed on (database, range) and holds the whole server."""
        client = TestClient(app)
        first = dashboard.cached_build(app.state.db, 30)
        client.get("/project/infrastructure")
        assert dashboard.cached_build(app.state.db, 30) is first


class TestTheTableStack:
    def test_model_comes_before_account(self, client):
        """Which model billed it is the first question a project page is opened with."""
        body = client.get("/project/infrastructure").text
        assert body.index("By model") < body.index("By account")

    def test_each_table_is_clipped(self, client):
        """Four stack here, and an all-time day table would otherwise be the page."""
        body = client.get("/project/infrastructure").text
        assert body.count('class="table-clip"') == len(dashboard.SCOPES) - 1


def _total(body: str) -> float:
    figure = re.search(r'<div class="figure"[^>]*>\$([\d,.]+)</div>', body)
    assert figure, "the page carries no headline figure"
    return float(figure[1].replace(",", ""))


class TestCharts:
    def test_there_are_four_and_they_do_not_share_a_scale(self, client):
        charts = _charts(client.get("/project/infrastructure").text)
        assert [c["key"] for c in charts] == [
            "cost-account", "cost-model", "tokens-kind", "calls",
        ]
        assert {c["unit"] for c in charts} == {"usd", "tokens", "calls"}

    def test_a_trace_carries_one_value_per_bucket(self, client):
        for chart in _charts(client.get("/project/infrastructure").text):
            assert all(len(t["values"]) == len(chart["axis"]) for t in chart["traces"])

    def test_the_token_chart_splits_by_kind(self, client):
        charts = {c["key"]: c for c in _charts(client.get("/machine/neo").text)}
        assert {t["label"] for t in charts["tokens-kind"]["traces"]} == {
            "Input", "Output", "Cache write", "Cache read",
        }

    def test_the_traces_total_to_the_headline(self, client):
        body = client.get("/project/infrastructure").text
        charts = {c["key"]: c for c in _charts(body)}
        charted = sum(sum(t["values"]) for t in charts["cost-account"]["traces"])
        assert charted == pytest.approx(_total(body), abs=0.005)

    def test_the_dashboard_itself_has_no_chart_payload_of_this_shape(self, client):
        """One chart and a toggle there; the four are the detail page's own."""
        assert isinstance(_charts(client.get("/").text), dict)

    def test_past_the_limit_the_rest_fold_into_one_trace(self, tmp_path):
        """A legend of thirty models is a legend, not a chart."""
        app = create_app(sf.config(tmp_path))
        client = TestClient(app)
        models = [
            "claude-opus-4-5-20251101", "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5-20250929", "claude-opus-4-6", "claude-sonnet-4-6",
            "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-5",
        ]
        assert len(models) > dashboard.TRACE_LIMIT
        records = [
            sf.record(mid=f"m{n}", dk=f"d{n}", ts=_ts(1, 12), project="p",
                      model=model, utc_offset=_offset(),
                      account_uuid="work", account_label="work@example.net")
            for n, model in enumerate(models)
        ]
        token = sf.mint_for(app, "neo", "neo")
        client.post("/v1/ingest", json=sf.batch(records, path="/p/n.jsonl", label="neo"),
                    headers=sf.auth(token))
        charts = {c["key"]: c for c in _charts(client.get("/machine/neo").text)}
        labels = [t["label"] for t in charts["cost-model"]["traces"]]
        assert len(labels) == dashboard.TRACE_LIMIT + 1
        assert labels[-1] == "Other"


class TestTheDayPage:
    def test_it_charts_the_hours_of_that_day(self, client):
        charts = _charts(client.get(f"/day/{TODAY}").text)
        for chart in charts:
            assert len(chart["axis"]) == 24
            assert chart["axis"][0] == f"{TODAY}T00:00"
            assert chart["axis"][-1] == f"{TODAY}T23:00"

    def test_the_work_lands_in_the_hours_it_was_done(self, client):
        charts = {c["key"]: c for c in _charts(client.get(f"/day/{TODAY}").text)}
        calls = charts["calls"]["traces"][0]["values"]
        assert [hour for hour, value in enumerate(calls) if value] == [9, 14, 20]

    def test_a_day_with_nothing_in_it_still_draws_its_axis(self, client):
        charts = _charts(client.get("/day/2000-01-01").text)
        assert all(len(chart["axis"]) == 24 for chart in charts)

    def test_it_has_no_range_toggle(self, client):
        """The toggle cannot widen a page that is about one day."""
        assert 'class="toggle' not in client.get(f"/day/{TODAY}").text

    def test_its_own_date_is_not_a_link(self, client):
        """Both dates are the day the page is already on."""
        assert f'href="/day/{TODAY}"' not in client.get(f"/day/{TODAY}").text


class TestLinks:
    def test_a_breakdown_row_opens_that_entity(self, client):
        body = client.get("/?by=project").text
        assert f'href="/project/infrastructure?days={dashboard.DEFAULT_RANGE}"' in body

    def test_an_account_bar_opens_its_account(self, client):
        body = client.get("/").text
        assert f'href="/account/work%40example.net?days={dashboard.DEFAULT_RANGE}"' in body

    def test_the_header_dates_open_their_day_pages(self, client):
        body = client.get("/project/infrastructure?days=7").text
        view = dashboard.build(client.app.state.db.connect(), 7,
                               scope=dashboard.Scope("project", "infrastructure"))
        assert f'<a href="/day/{view.start}" data-day="{view.start}">' in body
        assert f'<a href="/day/{view.end}" data-day="{view.end}">' in body

    def test_a_row_carries_the_range_it_was_clicked_from(self, client):
        assert 'href="/model/claude-haiku-4-5?days=7"' in client.get("/?days=7&by=model").text

    def test_a_day_row_rolls_up_to_its_week_and_month(self, client):
        body = client.get("/project/infrastructure").text
        days = dashboard.DEFAULT_RANGE
        assert f'href="/week/{dashboard.week_key(TODAY)}?days={days}"' in body
        assert f'href="/month/{TODAY[:7]}?days={days}"' in body

    def test_a_detail_row_opens_the_next_entity(self, client):
        body = client.get("/project/infrastructure").text
        assert f'href="/machine/neo?days={dashboard.DEFAULT_RANGE}"' in body


class TestThePeriodPages:
    def test_a_week_charts_seven_days_from_its_monday(self, client):
        monday = date.fromisoformat(dashboard.week_key(TODAY))
        for chart in _charts(client.get(f"/week/{TODAY}").text):
            assert chart["axis"] == [(monday + timedelta(days=n)).isoformat() for n in range(7)]

    def test_a_month_charts_every_calendar_day_it_holds(self, client):
        month = TODAY[:7]
        for chart in _charts(client.get(f"/month/{month}").text):
            assert len(chart["axis"]) == calendar.monthrange(NOW.year, NOW.month)[1]
            assert chart["axis"][0] == f"{month}-01"

    def test_every_date_in_a_week_draws_the_same_week(self, client):
        """The URL renders in place, so the seven dates are one page."""
        monday = date.fromisoformat(dashboard.week_key(TODAY))
        bodies = {client.get(f"/week/{monday + timedelta(days=n)}").text for n in range(7)}
        assert len(bodies) == 1

    def test_a_week_totals_the_days_it_holds(self, client):
        """Which is also what says the day nine back is outside it."""
        axis = _charts(client.get(f"/week/{TODAY}").text)[0]["axis"]
        days = sum(_total(client.get(f"/day/{day}").text) for day in axis)
        assert days == pytest.approx(_total(client.get(f"/week/{TODAY}").text), abs=0.005)

    def test_a_month_totals_its_own_day_rows(self, app):
        view = dashboard.build(app.state.db.connect(), dashboard.DEFAULT_RANGE,
                               scope=dashboard.Scope("month", TODAY[:7]))
        assert sum(row["cost"] for row in view.breakdowns["day"]) == pytest.approx(
            view.total_cost, abs=0.005)

    def test_a_period_a_key_cannot_name_is_a_404(self, client):
        """A mistyped URL, where an empty page would read as an idle month."""
        for path in ("/month/2026-13", "/month/nope", "/week/2026-07-32", "/day/2026-02-30"):
            assert client.get(path).status_code == 404, path

    def test_neither_has_a_range_toggle(self, client):
        for path in (f"/week/{TODAY}", f"/month/{TODAY[:7]}"):
            assert 'class="toggle' not in client.get(path).text, path

    def test_a_period_page_prints_its_span_once(self, client):
        body = client.get(f"/day/{TODAY}").text
        assert body.count("<h1") == 1
        assert body.count(f'data-day="{TODAY}"') == 1
        assert f"<h1>{TODAY}</h1>" not in body

    def test_a_page_about_a_thing_keeps_its_name_as_the_heading(self, client):
        body = client.get("/project/infrastructure").text
        assert body.count("<h1") == 1
        assert "<h1>infrastructure</h1>" in body


class TestPeriodKeys:
    def test_a_monday_is_its_own_week(self):
        assert dashboard.week_key("2026-07-27") == "2026-07-27"

    def test_a_sunday_belongs_to_the_monday_before_it(self):
        assert dashboard.week_key("2026-08-02") == "2026-07-27"

    def test_a_month_over_a_clock_change_keeps_every_day(self):
        """(end - start).days is one short where an hour went missing."""
        for month, length in (("2026-03", 31), ("2026-10", 31), ("2028-02", 29)):
            _, _, axis = dashboard.period_span("month", month)
            assert len(axis) == length, month
            assert axis[-1] == f"{month}-{length:02d}"
