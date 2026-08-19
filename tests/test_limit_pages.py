"""The merged rate-limit window pages: what they list, and what one window shows."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport.server import limits
from ccreport.server.factory import create_app

NOW = datetime.now(tz=UTC).astimezone()


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


def _ts(hours_ago: float) -> float:
    return (NOW - timedelta(hours=hours_ago)).timestamp()


RESET = (NOW + timedelta(hours=1)).replace(second=0, microsecond=0).timestamp()
"""One session window, opening four hours ago and resetting in an hour."""


def _offset() -> int:
    offset = NOW.utcoffset()
    return int(offset.total_seconds()) if offset else 0


@pytest.fixture
def app(tmp_path):
    """One window filled by two machines, over records billed to one account."""
    app = create_app(sf.config(tmp_path))
    client = TestClient(app)
    for machine, label, pcts in (
        ("laptop-1", "Laptop", [(4.0, 5.0), (2.0, 40.0)]),
        ("desk-1", "Desk", [(3.0, 22.0), (1.0, 61.0)]),
    ):
        token = sf.mint_for(app, machine, label)
        client.post("/v1/ingest", headers=sf.auth(token), json={
            **sf.sample_batch([
                sf.sample(ts=_ts(ago), used_pct=pct, resets_at=RESET)
                for ago, pct in pcts
            ]),
            "label": label,
            "files": [{
                "path": f"/p/{machine}.jsonl", "mtime_ns": 1, "size": 10,
                "records": [sf.record(
                    mid=f"m{machine}", dk=f"d{machine}", ts=_ts(3.0),
                    utc_offset=_offset(),
                )],
            }],
        })
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _window_url(body: str) -> str:
    match = re.search(r'href="(/limits/session/[^"]+)"', body)
    assert match, body
    return match.group(1).replace("&amp;", "&")


class TestTheWindowList:
    def test_the_page_lists_the_window_under_its_type(self, client):
        body = client.get("/limits").text
        assert "Session (5h)" in body
        assert "61.0%" in body

    def test_two_machines_reporting_one_window_are_one_row(self, client):
        body = client.get("/limits").text
        assert body.count("<tbody>") == 1
        assert "Desk, Laptop" in body or "Laptop, Desk" in body

    def test_the_row_carries_a_cache_share(self, client):
        """5000 written and 30000 read of 36000 shown is 83%."""
        assert "83%" in client.get("/limits").text

    def test_an_empty_server_says_why_it_is_empty(self, tmp_path):
        empty = TestClient(create_app(sf.config(tmp_path / "other")))
        body = empty.get("/limits").text
        assert "No machine has pushed" in body

    def test_the_range_toggle_narrows_the_span(self, client):
        assert client.get("/limits?days=7").status_code == 200

    def test_the_nav_marks_the_page(self, client):
        assert 'href="/limits" aria-current="page"' in client.get("/limits").text

    def test_the_list_is_behind_the_network_gate(self, tmp_path):
        gated = TestClient(create_app(sf.config(tmp_path, networks=sf.ELSEWHERE)))
        assert gated.get("/limits").status_code == 403


class TestOneWindowsPage:
    def test_the_row_links_to_the_window(self, client):
        resp = client.get(_window_url(client.get("/limits").text))
        assert resp.status_code == 200
        assert "61.0%" in resp.text

    def test_the_page_draws_a_fill_curve_per_machine(self, client):
        body = client.get(_window_url(client.get("/limits").text)).text
        assert '"key": "fill"' in body
        assert '"label": "Laptop"' in body
        assert '"label": "Desk"' in body

    def test_a_bucket_nobody_sampled_is_a_gap(self, client):
        """0 would draw a quota that emptied and refilled."""
        body = client.get(_window_url(client.get("/limits").text)).text
        assert "null" in body

    def test_the_page_breaks_the_span_down_by_model(self, client):
        body = client.get(_window_url(client.get("/limits").text)).text
        assert "claude-sonnet-4-5-20250929" in body

    def test_a_window_nothing_was_pushed_for_is_a_404(self, client):
        assert client.get("/limits/session/1?account=nobody").status_code == 404

    def test_a_window_of_another_account_is_a_404(self, client):
        url = _window_url(client.get("/limits").text)
        assert client.get(url.replace("account=", "account=x")).status_code == 404


class TestTheMerge:
    def test_two_accounts_resetting_together_stay_apart(self, app):
        """The quota is the account's; a shared minute is not a shared window."""
        client = TestClient(app)
        token = sf.mint_for(app, "laptop-2", "Other")
        client.post("/v1/ingest", headers=sf.auth(token), json=sf.sample_batch([
            sf.sample(ts=_ts(2.0), used_pct=9.0, resets_at=RESET,
                      account_uuid="acct-2", account_label="other@example.net"),
        ]))
        view = limits.build(app.state.db.connect(), 0)
        assert sum(len(group.rows) for group in view.groups) == 2

    def test_a_placeholder_reset_is_left_out(self, app):
        client = TestClient(app)
        token = sf.mint_for(app, "laptop-3", "Third")
        client.post("/v1/ingest", headers=sf.auth(token), json=sf.sample_batch([
            sf.sample(ts=_ts(2.0), used_pct=9.0, resets_at=9_999_999_999.0),
        ]))
        view = limits.build(app.state.db.connect(), 0)
        assert all(
            row.instance.resets_at != 9_999_999_999.0
            for group in view.groups for row in group.rows
        )
