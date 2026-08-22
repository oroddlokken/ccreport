"""The merged rate-limit window pages: what they list, and what one window shows."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport.server import dashboard, limits
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


class TestTheExtraColumn:
    """Real billed credits, the one figure that is not an API-price valuation."""

    @staticmethod
    def _push_extra(app, machine: str, label: str, readings: list[tuple[float, float]]):
        token = sf.mint_for(app, machine, label)
        TestClient(app).post("/v1/ingest", headers=sf.auth(token), json={
            "label": label, "files": [], "samples": [],
            "extra": [sf.extra(ts=ts, spent=spent) for ts, spent in readings],
        })

    def test_a_bounded_window_prints_what_it_billed(self, app, client):
        self._push_extra(app, "laptop-1", "Laptop",
                         [(_ts(5.0), 2.0), (_ts(0.5), 9.5)])
        assert "$7.50" in client.get("/limits").text

    def test_no_reading_before_the_window_reads_as_absent(self, app, client):
        """A dash, never $0.00: a billed window that reads as a free one."""
        self._push_extra(app, "laptop-1", "Laptop", [(_ts(0.5), 9.5)])
        body = client.get("/limits").text
        assert "$9.50" not in body
        assert "Extra" in body

    def test_two_machines_on_one_account_do_not_double_the_figure(self, app, client):
        """Both read the same cumulative dollars, so the answer is one machine's."""
        self._push_extra(app, "laptop-1", "Laptop",
                         [(_ts(5.0), 2.0), (_ts(3.0), 6.0), (_ts(0.5), 9.5)])
        self._push_extra(app, "desk-1", "Desk", [(_ts(5.1), 2.0), (_ts(0.6), 9.5)])
        body = client.get("/limits").text
        assert "$7.50" in body
        assert "$15.00" not in body

    def test_the_machine_that_watched_most_closely_answers(self, app):
        """A lagging second machine cannot be read as the monthly reset."""
        self._push_extra(app, "laptop-1", "Laptop",
                         [(_ts(5.0), 2.0), (_ts(3.0), 6.0), (_ts(0.5), 9.5)])
        self._push_extra(app, "desk-1", "Desk", [(_ts(5.1), 2.0), (_ts(0.6), 4.0)])
        view = limits.build(app.state.db.connect(), 30, NOW)
        [row] = view.groups[0].rows
        assert row.spend.extra_usd == pytest.approx(7.5)

    def test_the_window_page_carries_the_tile(self, app, client):
        self._push_extra(app, "laptop-1", "Laptop",
                         [(_ts(5.0), 2.0), (_ts(0.5), 9.5)])
        body = client.get(_window_url(client.get("/limits").text)).text
        assert "billed as credits while the window ran" in body

    def test_the_page_says_a_dash_is_retention_and_not_a_free_window(self, client):
        assert "Extra is the only" in client.get("/limits").text


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


class TestCachedWindows:
    """One build per range and one per window instance, until a push lands."""

    @pytest.fixture(autouse=True)
    def empty_caches(self):
        limits._LIST_CACHE.clear()
        limits._WINDOW_CACHE.clear()
        yield
        limits._LIST_CACHE.clear()
        limits._WINDOW_CACHE.clear()

    @staticmethod
    def _push_sample(app, used_pct: float):
        token = sf.mint_for(app, "laptop-1", "Laptop")
        TestClient(app).post("/v1/ingest", headers=sf.auth(token), json=sf.sample_batch([
            sf.sample(ts=_ts(0.25), used_pct=used_pct, resets_at=RESET),
        ]))

    def test_a_second_render_of_a_range_reuses_the_first(self, app):
        first = limits.cached_build(app.state.db, 30, NOW)
        assert limits.cached_build(app.state.db, 30, NOW) is first

    def test_each_range_is_held_apart(self, app):
        week = limits.cached_build(app.state.db, 7, NOW)
        month = limits.cached_build(app.state.db, 30, NOW)
        assert week is not month
        assert limits.cached_build(app.state.db, 7, NOW) is week

    def test_an_unknown_range_shares_the_default_entry(self, app):
        assert limits.cached_build(app.state.db, 999, NOW) is limits.cached_build(
            app.state.db, dashboard.DEFAULT_RANGE, NOW,
        )

    def test_a_pushed_sample_invalidates_the_list(self, app):
        first = limits.cached_build(app.state.db, 30, NOW)
        self._push_sample(app, 88.0)
        second = limits.cached_build(app.state.db, 30, NOW)
        assert second is not first
        assert second.groups[0].rows[0].instance.peak == 88.0

    def test_a_new_day_invalidates_the_list(self, app):
        first = limits.cached_build(app.state.db, 30, NOW)
        assert limits.cached_build(app.state.db, 30, NOW + timedelta(days=1)) is not first

    def test_two_databases_do_not_share_an_entry(self, app, tmp_path):
        other = create_app(sf.config(tmp_path / "other"))
        assert limits.cached_build(other.state.db, 30, NOW).groups == []
        assert limits.cached_build(app.state.db, 30, NOW).groups != []

    def test_the_list_page_serves_the_cached_view(self, app):
        view = limits.cached_build(app.state.db, dashboard.DEFAULT_RANGE)
        TestClient(app).get("/limits")
        assert limits.cached_build(app.state.db, dashboard.DEFAULT_RANGE) is view

    def test_a_second_render_of_one_window_reuses_the_first(self, app):
        first = limits.cached_window(app.state.db, "session", RESET, None, "me@example.net", NOW)
        assert limits.cached_window(
            app.state.db, "session", RESET, None, "me@example.net", NOW,
        ) is first

    def test_a_reset_a_rounding_apart_is_the_same_page(self, app):
        """Stored jitter would otherwise build the window once per link."""
        first = limits.cached_window(app.state.db, "session", RESET, None, "me@example.net", NOW)
        assert limits.cached_window(
            app.state.db, "session", RESET + 20, None, "me@example.net", NOW,
        ) is first

    def test_a_window_nobody_pushed_stores_nothing(self, app):
        with pytest.raises(LookupError):
            limits.cached_window(app.state.db, "session", 1.0, None, "nobody", NOW)
        assert len(limits._WINDOW_CACHE) == 0

    def test_the_oldest_window_goes_at_the_bound(self, app, monkeypatch):
        """One entry per instance, and five session windows arrive every day."""
        token = sf.mint_for(app, "laptop-2", "Other")
        TestClient(app).post("/v1/ingest", headers=sf.auth(token), json=sf.sample_batch([
            sf.sample(ts=_ts(2.0), used_pct=9.0, resets_at=RESET,
                      account_uuid="acct-2", account_label="other@example.net"),
        ]))
        monkeypatch.setattr(limits._WINDOW_CACHE, "limit", 1)
        first = limits.cached_window(app.state.db, "session", RESET, None, "me@example.net", NOW)
        limits.cached_window(app.state.db, "session", RESET, None, "other@example.net", NOW)
        assert len(limits._WINDOW_CACHE) == 1
        assert limits.cached_window(
            app.state.db, "session", RESET, None, "me@example.net", NOW,
        ) is not first

    def test_the_window_page_serves_the_cached_view(self, app, client):
        view = limits.cached_window(app.state.db, "session", RESET, None, "me@example.net")
        client.get(_window_url(client.get("/limits").text))
        assert limits.cached_window(
            app.state.db, "session", RESET, None, "me@example.net",
        ) is view
