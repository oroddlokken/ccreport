"""The merged reports: what the server aggregates, and what the CLI renders.

The seeded database holds two machines and two accounts, which is the whole
point of merging — every assertion here is about a number no single machine
could have produced on its own.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient
from rich.console import Console

from ccreport import aggregate
from ccreport import ccreport as ccr
from ccreport.server import reports
from ccreport.server.factory import create_app


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


def _ts(day: int, hour: int = 12) -> float:
    return datetime(2026, 3, day, hour, 0, tzinfo=UTC).timestamp()


LAPTOP_WORK = [
    sf.record(mid="a1", dk="a1:r", ts=_ts(2), sid="s-alpha", project="projA",
              account_uuid="u-work", account_label="me@work.example",
              model="claude-opus-4-5-20251101", utc_offset=0),
    sf.record(mid="a2", dk="a2:r", ts=_ts(3), sid="s-alpha", project="projA",
              account_uuid="u-work", account_label="me@work.example",
              model="claude-sonnet-4-5-20250929", utc_offset=0),
]
DESK_HOME = [
    sf.record(mid="b1", dk="b1:r", ts=_ts(3), sid="s-beta", project="projB",
              account_uuid="u-home", account_label="me@home.example",
              model="claude-haiku-4-5", utc_offset=0),
    # The same call as the laptop's a2: a synced home directory, stored twice.
    sf.record(mid="a2", dk="a2:r", ts=_ts(3), sid="s-alpha", project="projA",
              account_uuid="u-work", account_label="me@work.example",
              model="claude-sonnet-4-5-20250929", utc_offset=0),
]


@pytest.fixture
def app(tmp_path):
    app = create_app(sf.config(tmp_path))
    client = TestClient(app)
    for machine, label, records, path in (
        ("laptop-1", "Laptop", LAPTOP_WORK, "/p/a.jsonl"),
        ("desk-1", "Desk", DESK_HOME, "/p/b.jsonl"),
    ):
        token = sf.mint_for(app, machine, label)
        resp = client.post(
            "/v1/ingest",
            json=sf.batch(records, path=path, label=label),
            headers=sf.auth(token),
        )
        assert resp.json()["files"][0]["status"] == "accepted", resp.json()
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _report(client, kind, **params):
    resp = client.get(f"/v1/report/{kind}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestLoading:
    def test_a_synced_log_stored_twice_counts_once(self, app):
        """Collapsed by (account, dedup key), so neither machine is double-counted."""
        merged = reports.load(app.state.db.connect())
        assert [m.record.message_id for m in merged] == ["a1", "a2", "b1"]

    def test_the_surviving_copy_is_the_machine_that_reported_it_first(self, app):
        merged = {m.record.message_id: m.machine for m in reports.load(app.state.db.connect())}
        assert merged["a2"] == "Laptop"

    def test_a_record_with_no_dedup_key_is_kept(self, tmp_path):
        """The log carried nothing to match it on; dropping it loses a real call."""
        app = create_app(sf.config(tmp_path))
        client = TestClient(app)
        token = sf.mint_for(app)
        client.post(
            "/v1/ingest",
            json=sf.batch([sf.record(mid="x1", dk=None), sf.record(mid="x2", dk=None)]),
            headers=sf.auth(token),
        )
        assert len(reports.load(app.state.db.connect())) == 2

    def test_records_come_back_oldest_first(self, app):
        merged = reports.load(app.state.db.connect())
        assert [m.record.timestamp for m in merged] == sorted(m.record.timestamp for m in merged)

    @pytest.mark.parametrize(
        ("field", "value", "kept"),
        [
            ("project", "projA", ["a1", "a2"]),
            ("account", "me@home.example", ["b1"]),
            ("machine", "desk-1", ["b1", "a2"]),
        ],
    )
    def test_each_filter_narrows_to_what_it_names(self, app, field, value, kept):
        filters = reports.Filters(**{field: value})
        merged = reports.load(app.state.db.connect(), filters)
        assert [m.record.message_id for m in merged] == kept

    def test_a_date_range_is_bounded_in_instants(self, app):
        """Two machines can disagree about which day an instant falls in."""
        merged = reports.load(app.state.db.connect(), reports.Filters(
            since=datetime(2026, 3, 3, tzinfo=UTC),
        ))
        assert [m.record.message_id for m in merged] == ["a2", "b1"]

    def test_the_machines_calendar_day_travels_with_the_record(self, app):
        """Stamped at ingest from the offset the client sent, not re-derived."""
        merged = reports.load(app.state.db.connect())
        assert {m.record.day_key() for m in merged} == {"2026-03-02", "2026-03-03"}


def _totals(merged):
    """What a grouped load has to agree with load() about, to the cent."""
    return (
        round(sum(m.record.cost() for m in merged), 9),
        sum(m.record.tokens.total for m in merged),
        sum(m.record.count for m in merged),
        sorted({(m.machine, m.account, m.record.day_key()) for m in merged}),
    )


class TestGroupedLoading:
    """The dashboard's loader: SQL folds the corpus, and the sums must not move."""

    def test_it_totals_what_the_record_loader_does(self, app):
        conn = app.state.db.connect()
        assert _totals(reports.load_grouped(conn)) == _totals(reports.load(conn))

    def test_a_synced_log_stored_twice_counts_once(self, app):
        """The same collapse load() does, stated in SQL instead of Python."""
        merged = reports.load_grouped(app.state.db.connect())
        assert sum(m.record.count for m in merged) == 3

    def test_the_surviving_copy_is_the_machine_that_reported_it_first(self, app):
        merged = reports.load_grouped(app.state.db.connect())
        assert {m.machine for m in merged if m.record.project == "projA"} == {"Laptop"}

    def test_a_record_with_no_dedup_key_is_kept(self, tmp_path):
        app = create_app(sf.config(tmp_path))
        client = TestClient(app)
        token = sf.mint_for(app)
        client.post(
            "/v1/ingest",
            json=sf.batch([sf.record(mid="x1", dk=None), sf.record(mid="x2", dk=None)]),
            headers=sf.auth(token),
        )
        merged = reports.load_grouped(app.state.db.connect())
        assert sum(m.record.count for m in merged) == 2

    def test_one_group_carries_every_call_in_it(self, app):
        """projA is two calls on two days and two models, so nothing may merge."""
        merged = reports.load_grouped(app.state.db.connect(), reports.Filters(project="projA"))
        assert sorted(m.record.count for m in merged) == [1, 1]

    @pytest.mark.parametrize(
        ("field", "value", "calls"),
        [("project", "projA", 2), ("account", "me@home.example", 1), ("machine", "desk-1", 2)],
    )
    def test_each_filter_narrows_to_what_it_names(self, app, field, value, calls):
        """machine=desk-1 keeps its own copy of the synced call: the dedup
        subquery repeats the filter, so the laptop's copy is not in the set
        whose lowest id wins."""
        merged = reports.load_grouped(app.state.db.connect(), reports.Filters(**{field: value}))
        assert sum(m.record.count for m in merged) == calls

    def test_a_date_range_is_bounded_in_instants(self, app):
        merged = reports.load_grouped(app.state.db.connect(), reports.Filters(
            since=datetime(2026, 3, 3, tzinfo=UTC),
        ))
        assert sum(m.record.count for m in merged) == 2

    def test_groups_come_back_oldest_first(self, app):
        merged = reports.load_grouped(app.state.db.connect())
        assert [m.record.timestamp for m in merged] == sorted(m.record.timestamp for m in merged)

    def test_the_machines_calendar_day_travels_with_the_group(self, app):
        merged = reports.load_grouped(app.state.db.connect())
        assert {m.record.day_key() for m in merged} == {"2026-03-02", "2026-03-03"}

    def test_a_group_prices_at_its_earliest_instant(self, app):
        """What the savings tile reads. A group is one model on one day, so the
        first call's instant prices every call in it."""
        merged = reports.load_grouped(app.state.db.connect(), reports.Filters(project="projB"))
        assert [m.record.timestamp for m in merged] == [datetime(2026, 3, 3, 12, tzinfo=UTC)]


class TestEndpoints:
    def test_the_daily_report_merges_both_machines(self, client):
        body = _report(client, "day")
        assert [row["key"] for row in body["rows"]] == ["2026-03-02", "2026-03-03"]
        assert body["n_all"] == 2

    def test_every_row_says_which_machine_it_came_from(self, client):
        rows = {row["key"]: row for row in _report(client, "day")["rows"]}
        assert set(rows["2026-03-03"]["machines"]) == {"Laptop", "Desk"}
        assert set(rows["2026-03-02"]["machines"]) == {"Laptop"}

    def test_every_row_says_which_account_paid(self, client):
        rows = {row["key"]: row for row in _report(client, "day")["rows"]}
        assert set(rows["2026-03-03"]["accounts"]) == {"me@work.example", "me@home.example"}

    def test_the_machine_split_adds_up_to_the_rows_cost(self, client):
        for row in _report(client, "day")["rows"]:
            assert sum(row["machines"].values()) == pytest.approx(row["agg"]["cost"])
            assert sum(row["accounts"].values()) == pytest.approx(row["agg"]["cost"])

    def test_the_account_report_splits_across_both_logins(self, client):
        body = _report(client, "account")
        assert sorted(row["key"] for row in body["rows"]) == [
            "me@home.example", "me@work.example",
        ]

    def test_the_project_report_covers_both_machines_projects(self, client):
        body = _report(client, "project")
        assert sorted(row["key"] for row in body["rows"]) == ["projA", "projB"]

    def test_the_session_report_names_the_project_and_last_activity(self, client):
        rows = {row["key"]: row for row in _report(client, "session")["rows"]}
        assert rows["s-alpha"]["project"] == "projA"
        assert rows["s-alpha"]["last"].startswith("2026-03-03")

    def test_the_monthly_report_carries_its_projection_slot(self, client):
        body = _report(client, "month")
        assert [row["key"] for row in body["rows"]] == ["2026-03"]
        assert "projection" in body

    def test_the_response_names_every_machine_and_account_it_covered(self, client):
        body = _report(client, "day")
        assert body["machines"] == ["Desk", "Laptop"]
        assert body["accounts"] == ["me@home.example", "me@work.example"]

    def test_a_limit_cuts_the_rows_and_keeps_the_count(self, client):
        body = _report(client, "project", limit=1)
        assert len(body["rows"]) == 1
        assert body["n_all"] == 2

    def test_a_breakdown_splits_a_day_by_model(self, client):
        rows = {r["key"]: r for r in _report(client, "day", breakdown="true")["rows"]}
        assert [sub["key"] for sub in rows["2026-03-02"]["breakdown"]] == [
            "claude-opus-4-5-20251101",
        ]

    def test_an_unknown_report_is_404_and_names_the_ones_there_are(self, client):
        resp = client.get("/v1/report/weekly")
        assert resp.status_code == 404
        assert "day" in resp.json()["detail"]

    def test_an_unparseable_date_is_400_rather_than_a_silent_full_range(self, client):
        """A report over the wrong range looks exactly like a report."""
        resp = client.get("/v1/report/day", params={"since": "last-tuesday"})
        assert resp.status_code == 400

    def test_a_report_is_behind_the_network_allowlist(self, tmp_path):
        gated = create_app(sf.config(tmp_path, networks=sf.ELSEWHERE))
        assert TestClient(gated).get("/v1/report/day").status_code == 403


class TestCurrency:
    def _with_rates(self, app, rates):
        store = app.state.db.connect()
        store.executemany(
            "INSERT OR REPLACE INTO exchange_rates (date, rate) VALUES (?, ?)",
            list(rates.items()),
        )
        store.commit()

    def test_each_record_converts_at_its_own_oslo_date(self, app, client):
        """A rate per date, so a record on the wrong one lands visibly wrong."""
        self._with_rates(app, {"2026-03-02": 10.0, "2026-03-03": 20.0})
        rows = {row["key"]: row for row in _report(client, "day")["rows"]}
        for key, rate in (("2026-03-02", 10.0), ("2026-03-03", 20.0)):
            agg = rows[key]["agg"]
            assert agg["cost_nok"] == pytest.approx(agg["cost"] * rate * 1.25)

    def test_the_column_is_off_when_the_server_holds_no_rates(self, client):
        body = _report(client, "day")
        assert body["nok"]["enabled"] is False
        assert all(row["agg"]["cost_nok"] == 0.0 for row in body["rows"])

    def test_the_column_turns_on_once_it_does(self, app, client):
        self._with_rates(app, {"2026-03-02": 10.0, "2026-03-03": 10.0})
        assert _report(client, "day")["nok"]["enabled"] is True


class TestJsonRoundTrip:
    def test_rows_survive_the_wire(self, app):
        merged = reports.load(app.state.db.connect())
        nok = aggregate.NokCtx({"2026-03-02": 10.0, "2026-03-03": 10.0}, "2026-03-03", True)
        built = reports.build(merged, "day", nok, breakdown=True)
        back = aggregate.rows_from_json(json.loads(json.dumps(aggregate.rows_to_json(built))))
        assert [r.key for r in back.rows] == [r.key for r in built.rows]
        assert back.total.cost == pytest.approx(built.total.cost)
        assert back.rows[0].machines == built.rows[0].machines
        assert [s.key for s in back.rows[0].breakdown] == [
            s.key for s in built.rows[0].breakdown
        ]

    def test_a_projection_survives_it_too(self):
        proj = aggregate.MonthProjection(
            days_elapsed=15, days_in_month=31, month_name="March", window_days=14,
            month_to_date=aggregate.Projection(cost=1.0, cost_nok=11.0, nok_estimated=False),
            trailing=None,
        )
        payload = json.loads(json.dumps(aggregate.month_projection_to_json(proj)))
        assert aggregate.month_projection_from_json(payload) == proj

    def test_no_projection_stays_no_projection(self):
        assert aggregate.month_projection_from_json(None) is None


class TestClientRendering:
    """`ccreport --server URL` draws server rows through the local builders."""

    def _rendered(self, monkeypatch, payloads, argv, width=200) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=width, no_color=True))
        monkeypatch.setattr(
            ccr.sys, "argv", ["ccreport", "--server", "https://ccr.example.net", *argv],
        )
        monkeypatch.setattr(
            "ccreport.remote.fetch_report",
            lambda base, kind, **params: payloads[kind],
        )
        ccr.main()
        return buf.getvalue()

    def test_a_merged_report_renders_like_a_local_one(self, client, monkeypatch):
        payload = _report(client, "day")
        out = self._rendered(monkeypatch, {"day": payload}, ["daily"])
        assert "Daily Usage (2 days)" in out
        assert "2026-03-02" in out
        assert "TOTAL" in out

    def test_the_nok_column_follows_what_the_server_said(self, client, monkeypatch, app):
        conn = app.state.db.connect()
        conn.executemany(
            "INSERT OR REPLACE INTO exchange_rates (date, rate) VALUES (?, ?)",
            [("2026-03-02", 10.0), ("2026-03-03", 10.0)],
        )
        conn.commit()
        payload = _report(client, "day")
        out = self._rendered(monkeypatch, {"day": payload}, ["daily"])
        assert "NOK+MVA" in out
        assert "kr " in out

    def test_no_rates_means_no_nok_column(self, client, monkeypatch):
        out = self._rendered(monkeypatch, {"day": _report(client, "day")}, ["daily"])
        assert "NOK" not in out

    def test_every_report_kind_renders(self, client, monkeypatch):
        payloads = {kind: _report(client, kind) for kind in reports.KINDS}
        out = self._rendered(monkeypatch, payloads, [])
        for title in ("Daily Usage", "Monthly Usage", "Projects", "Sessions", "Accounts"):
            assert title in out

    def test_json_prints_what_the_server_sent(self, client, monkeypatch, capsys):
        payload = _report(client, "day")
        self._rendered(monkeypatch, {"day": payload}, ["daily", "--json"])
        assert json.loads(capsys.readouterr().out) == payload

    def test_an_unreachable_server_exits_non_zero_and_says_what_it_tried(
        self, monkeypatch, capsys,
    ):
        """Never a quiet fall back to the local cache: that is the other report."""
        from ccreport.remote import RemoteError

        def boom(base, kind, **params):
            raise RemoteError(f"{base}/v1/report/{kind} could not be reached: refused")

        monkeypatch.setattr("ccreport.remote.fetch_report", boom)
        monkeypatch.setattr(
            ccr.sys, "argv", ["ccreport", "--server", "https://ccr.example.net", "daily"],
        )
        with pytest.raises(SystemExit) as exit_info:
            ccr.main()
        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        assert "https://ccr.example.net/v1/report/day" in err
        assert "refused" in err


class TestRemoteFetch:
    def test_the_url_carries_only_the_filters_that_were_set(self):
        from ccreport.remote import _url

        url = _url("https://ccr.example.net/", "day", {
            "since": "20260301", "until": None, "project": "", "breakdown": False, "limit": 5,
        })
        assert url == "https://ccr.example.net/v1/report/day?since=20260301&limit=5"

    def test_an_unreachable_host_becomes_a_remote_error_naming_the_url(self, monkeypatch):
        import urllib.error

        from ccreport import remote

        def refuse(request, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(remote.urllib.request, "urlopen", refuse)
        with pytest.raises(remote.RemoteError) as exc:
            remote.fetch_report("https://ccr.example.net", "day")
        assert "https://ccr.example.net/v1/report/day" in str(exc.value)
        assert "Connection refused" in str(exc.value)

    def test_a_server_error_names_the_status(self, monkeypatch):
        import urllib.error
        from email.message import Message

        from ccreport import remote

        def fail(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", Message(), None)

        monkeypatch.setattr(remote.urllib.request, "urlopen", fail)
        with pytest.raises(remote.RemoteError, match="403"):
            remote.fetch_report("https://ccr.example.net", "day")
