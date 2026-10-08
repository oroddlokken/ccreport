"""The merged speed page: per-model latency and output rate across machines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport.server import speed
from ccreport.server.factory import create_app

NOW = datetime.now(tz=UTC).astimezone()
MODEL = "claude-sonnet-4-5-20250929"


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    from ccreport import exchange

    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})
    speed._CACHE.clear()
    yield
    speed._CACHE.clear()


def _ts(hours_ago: float) -> float:
    return (NOW - timedelta(hours=hours_ago)).timestamp()


def _timed(mid: str, secs: float | None, output: int = 1000, **over) -> dict:
    end = _ts(2.0)
    return sf.record(
        mid=mid, dk=f"dk-{mid}", ts=end, output_tokens=output,
        req_start=None if secs is None else end - secs, req_end=None if secs is None else end,
        **over,
    )


def _push(app, client, machine: str, label: str, records: list[dict], turns=()) -> None:
    token = sf.mint_for(app, machine, label)
    body = sf.batch(records, path=f"/p/{machine}.jsonl", label=label)
    body["files"][0]["turns"] = [
        {"uuid": uuid, "ts": _ts(1.0), "model": MODEL, "duration_ms": ms,
         "account_uuid": "acct-1"}
        for uuid, ms in turns
    ]
    assert client.post("/v1/ingest", json=body, headers=sf.auth(token)).status_code == 200


@pytest.fixture
def app(tmp_path):
    app = create_app(sf.config(tmp_path))
    client = TestClient(app)
    _push(app, client, "laptop-1", "Laptop",
          [_timed("a", 10.0), _timed("b", 20.0), _timed("shared", 30.0), _timed("old", None)],
          turns=[("t1", 40_000), ("t-shared", 60_000)])
    _push(app, client, "desk-1", "Desk",
          [_timed("c", 5.0), _timed("shared", 30.0)],
          turns=[("t-shared", 60_000)])
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestBuild:
    def test_a_call_two_machines_pushed_is_timed_once(self, app):
        view = speed.build(app.state.db.connect(), 7, NOW)
        (group,) = view.groups
        assert group.total.model == MODEL
        # a, b, shared, old and c: the desk's copy of `shared` loses the dedup.
        assert group.total.calls == 5
        assert group.total.requests == 4
        assert group.total.latency_mid == pytest.approx(15.0)

    def test_the_rate_is_the_median_of_the_replies_over_the_floor(self, app):
        (group,) = speed.build(app.state.db.connect(), 7, NOW).groups
        # 1000 tokens over 5, 10, 20 and 30 seconds.
        assert group.total.rate_mid == pytest.approx((100 + 50) / 2)

    def test_a_turn_copied_into_two_logs_counts_once(self, app):
        (group,) = speed.build(app.state.db.connect(), 7, NOW).groups
        assert group.total.turns == 2
        assert group.total.turn_mid == pytest.approx(50.0)

    def test_each_machine_gets_a_row_under_the_model(self, app):
        (group,) = speed.build(app.state.db.connect(), 7, NOW).groups
        assert [(row.machine, row.calls, row.requests) for row in group.machines] == [
            ("Laptop", 4, 3), ("Desk", 1, 1),
        ]

    def test_a_single_machine_draws_no_row_repeating_the_total(self):
        groups = speed.fold([(MODEL, "m1", 500, 0.0, 5.0)], [], {"m1": "Laptop"})
        assert groups[0].machines == []

    def test_a_pseudo_model_has_no_speed(self):
        assert speed.fold([("<synthetic>", "m1", 500, 0.0, 5.0)], [], {}) == []


class TestPage:
    def test_the_page_lists_the_model_and_its_machines(self, client):
        html = client.get("/speed").text
        assert MODEL in html
        assert "Laptop" in html
        assert "Desk" in html
        assert "4 of 5 calls timed" in html

    def test_the_page_says_where_untimed_calls_come_from(self, client):
        assert "ccreport server push --full" in client.get("/speed").text

    def test_the_nav_marks_the_page(self, client):
        assert '<a href="/speed" aria-current="page">Speed</a>' in client.get("/speed").text

    def test_an_empty_server_says_so(self, tmp_path):
        client = TestClient(create_app(sf.config(tmp_path / "empty")))
        assert "No machine has pushed a call" in client.get("/speed").text

    def test_the_page_is_behind_the_network_gate(self, tmp_path):
        client = TestClient(create_app(sf.config(tmp_path / "gated", networks=sf.ELSEWHERE)))
        assert client.get("/speed").status_code == 403
