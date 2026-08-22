"""`ccreport server pull` and `-A`: the spend this machine did not record itself.

The remainder is what makes the two halves addable. A machine-id exclusion alone
would double-count a session log present on two machines, so the tests here push
the same call twice under different machines and check it lands once.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport import cache_db, push, tier_timeline
from ccreport import ccreport as ccr
from ccreport.server import pull
from ccreport.server.factory import create_app

TS = time.time() - 3600
"""An hour ago, so the pushed calls land inside every rolling window.

The server bounds the cost buckets to the longest of them, measured from the
instant the pull is answered, so a fixed epoch in the past would come back as
day rows alone."""

URL = "https://ccr.example.net"


@pytest.fixture(autouse=True)
def _no_memo():
    pull.clear_memo()
    yield
    pull.clear_memo()


@pytest.fixture
def server(tmp_path):
    """A server holding one call from the laptop and one from the desk."""
    app = create_app(sf.config(tmp_path / "srv"))
    client = TestClient(app)
    for machine, label, mid in (("laptop-1", "Laptop", "m-laptop"),
                                ("desk-1", "Desk", "m-desk")):
        token = sf.mint_for(app, machine, label)
        client.post("/v1/ingest", headers=sf.auth(token), json={
            "label": label,
            "files": [{
                "path": f"/p/{machine}.jsonl", "mtime_ns": 1, "size": 10,
                "records": [sf.record(mid=mid, dk=mid, ts=TS)],
            }],
        })
    return app


def _remainder(app, machine_id: str):
    return pull.remainder(
        app.state.db.connect(), "acct-1", machine_id, pull.bucket_floor(TS) - 86400,
    )


class TestTheRemainder:
    def test_the_asking_machines_own_rows_are_left_out(self, server):
        [rest] = _remainder(server, "laptop-1")
        assert rest.machine_id == "desk-1"

    def test_a_synced_call_both_machines_pushed_is_left_out(self, tmp_path):
        """Machine-id exclusion alone would send it back and double it."""
        app = create_app(sf.config(tmp_path / "srv"))
        client = TestClient(app)
        for machine, label in (("laptop-1", "Laptop"), ("desk-1", "Desk")):
            token = sf.mint_for(app, machine, label)
            client.post("/v1/ingest", headers=sf.auth(token), json={
                "label": label,
                "files": [{
                    "path": "/p/shared.jsonl", "mtime_ns": 1, "size": 10,
                    "records": [sf.record(mid="m-shared", dk="d-shared", ts=TS)],
                }],
            })
        assert _remainder(app, "laptop-1") == []

    def test_two_other_machines_sharing_a_log_contribute_it_once(self, tmp_path):
        app = create_app(sf.config(tmp_path / "srv"))
        client = TestClient(app)
        for machine, label in (("desk-1", "Desk"), ("nas-1", "NAS")):
            token = sf.mint_for(app, machine, label)
            client.post("/v1/ingest", headers=sf.auth(token), json={
                "label": label,
                "files": [{
                    "path": "/p/shared.jsonl", "mtime_ns": 1, "size": 10,
                    "records": [sf.record(mid="m-shared", dk="d-shared", ts=TS)],
                }],
            })
        rest = _remainder(app, "laptop-1")
        assert sum(row[7] for m in rest for row in m.days) == 1

    def test_the_reply_names_the_machine_and_its_last_push(self, server):
        [rest] = _remainder(server, "laptop-1")
        assert rest.label == "Desk"
        assert rest.last_seen > 0

    def test_the_memo_holds_until_the_content_moves(self, server):
        conn = server.state.db.connect()
        first = pull.cached_remainder(conn, "acct-1", "laptop-1", TS)
        assert pull.cached_remainder(conn, "acct-1", "laptop-1", TS) is first

    def test_a_new_push_moves_the_memo(self, server):
        conn = server.state.db.connect()
        first = pull.cached_remainder(conn, "acct-1", "laptop-1", TS)
        token = sf.mint_for(server, "nas-1", "NAS")
        TestClient(server).post("/v1/ingest", headers=sf.auth(token), json={
            "label": "NAS",
            "files": [{
                "path": "/p/nas.jsonl", "mtime_ns": 1, "size": 10,
                "records": [sf.record(mid="m-nas", dk="d-nas", ts=TS)],
            }],
        })
        assert pull.cached_remainder(conn, "acct-1", "laptop-1", TS) is not first


@pytest.fixture
def client_cache(tmp_path, monkeypatch):
    """A local cache signed in to the account the server fixture pushes under."""
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cache_db, "_conn", None)
    conn = cache_db.get_connection()
    cache_db.init_ccreport_meta(ccr.CACHE_VERSION, "test-hash")
    conn.execute(
        "INSERT INTO account_events (ts, account_uuid, email) VALUES (?, ?, ?)",
        (1.0, "acct-1", "me@example.net"),
    )
    conn.commit()
    yield conn
    cache_db.close_connection()


def _reply(app, machine_id: str = "laptop-1") -> dict:
    """What the ingest endpoint answers a pull-only batch with."""
    token = sf.mint_for(app, machine_id, "Laptop")
    return TestClient(app).post("/v1/ingest", headers=sf.auth(token), json={
        "label": "Laptop", "files": [], "samples": [], "extra": [],
        "pull": {"account_uuid": "acct-1"},
    }).json()


class TestStoringAPull:
    def test_the_windows_and_the_days_both_land(self, server, client_cache):
        assert push.store_pull(URL, _reply(server), TS) == 1
        windows = cache_db.load_remote_window_costs("acct-1")
        assert {row["window"] for row in windows} >= {"all_time", "seven_day"}
        assert len(cache_db.load_remote_day_costs("acct-1")) == 1

    def test_the_window_total_matches_the_day_total(self, server, client_cache):
        push.store_pull(URL, _reply(server), TS)
        totals, _oldest = cache_db.load_remote_window_totals("acct-1")
        days = cache_db.load_remote_day_costs("acct-1")
        assert totals["all_time"] == pytest.approx(sum(d["cost"] for d in days))

    def test_a_second_pull_replaces_rather_than_adds(self, server, client_cache):
        push.store_pull(URL, _reply(server), TS)
        push.store_pull(URL, _reply(server), TS)
        assert len(cache_db.load_remote_day_costs("acct-1")) == 1

    def test_another_accounts_rows_are_never_selected(self, server, client_cache):
        push.store_pull(URL, _reply(server), TS)
        assert cache_db.load_remote_day_costs("acct-2") == []

    def test_a_reply_with_no_pull_section_stores_nothing(self, client_cache):
        assert push.store_pull(URL, {"machine_id": "laptop-1"}, TS) == 0


class TestTheDeclaredTimeline:
    """The server is the one source; the machines take their copy from it."""

    def _declare(self, app, account="acct-1", *entries):
        from ccreport.server import db as server_db

        conn = app.state.db.connect()
        server_db.set_account_tiers(conn, account, [
            tier_timeline.Entry(
                ts=ts, account=account, organization_rate_limit_tier=tier,
            )
            for ts, tier in (entries or ((100.0, "default_claude_max_5x"),))
        ], 1.0)
        conn.commit()

    def _declared(self):
        return [
            e for e in cache_db.load_account_events()
            if e["source"] == cache_db.SOURCE_BACKFILL
        ]

    def test_the_reply_carries_the_accounts_timeline(self, server):
        self._declare(server, "acct-1", (100.0, "default_claude_pro"), (200.0, "max_20x"))
        tiers = _reply(server)["pull"]["tiers"]
        assert [(t["ts"], t["organization_rate_limit_tier"]) for t in tiers] == [
            (100.0, "default_claude_pro"), (200.0, "max_20x"),
        ]

    def test_a_server_that_declares_nothing_sends_an_empty_section(self, server):
        assert _reply(server)["pull"]["tiers"] == []

    def test_another_accounts_entries_never_reach_this_machine(self, server):
        self._declare(server, "acct-2", (100.0, "max_20x"))
        assert _reply(server)["pull"]["tiers"] == []

    def test_a_pull_writes_them_as_backfilled_rows(self, server, client_cache):
        self._declare(server, "acct-1", (100.0, "default_claude_pro"))
        assert push.store_tiers(_reply(server)) == 1
        [row] = self._declared()
        assert row["organization_rate_limit_tier"] == "default_claude_pro"
        assert (row["account_uuid"], row["email"]) == ("acct-1", "me@example.net")

    def test_the_capture_it_copied_its_identity_from_is_untouched(self, server, client_cache):
        self._declare(server, "acct-1")
        before = [e for e in cache_db.load_account_events()
                  if e["source"] == cache_db.SOURCE_CAPTURE]
        push.store_tiers(_reply(server))
        assert [e for e in cache_db.load_account_events()
                if e["source"] == cache_db.SOURCE_CAPTURE] == before

    def test_a_second_pull_with_nothing_changed_writes_nothing_new(self, server, client_cache):
        self._declare(server, "acct-1", (100.0, "default_claude_pro"), (200.0, "max_20x"))
        push.store_tiers(_reply(server))
        after_first = cache_db.load_account_events()
        push.store_tiers(_reply(server))
        assert cache_db.load_account_events() == after_first

    def test_an_entry_deleted_on_the_server_goes_here_too(self, server, client_cache):
        """Server-wins: the timeline is a document, not rows to merge into."""
        self._declare(server, "acct-1", (100.0, "default_claude_pro"), (200.0, "max_20x"))
        push.store_tiers(_reply(server))
        assert len(self._declared()) == 2
        self._declare(server, "acct-1", (100.0, "default_claude_pro"))
        push.store_tiers(_reply(server))
        assert [(e["ts"], e["organization_rate_limit_tier"]) for e in self._declared()] == [
            (100.0, "default_claude_pro"),
        ]

    def test_a_server_declaring_nothing_leaves_a_local_declaration_standing(
        self, server, client_cache,
    ):
        """An empty section is a server nobody typed a timeline into."""
        cache_db.backfill_account_events([
            {"ts": 5.0, "account_uuid": "acct-1", "email": "me@example.net",
             "organization_rate_limit_tier": "max_20x"},
        ])
        assert push.store_tiers(_reply(server)) == 0
        assert len(self._declared()) == 1

    def test_an_account_this_machine_has_never_seen_writes_nothing(
        self, server, client_cache,
    ):
        client_cache.execute("DELETE FROM account_events")
        client_cache.commit()
        self._declare(server, "acct-1")
        assert push.store_tiers(_reply(server)) == 0
        assert self._declared() == []

    def test_another_accounts_declared_rows_are_left_alone(self, server, client_cache):
        """A person on two accounts pulls for one of them at a time."""
        cache_db.backfill_account_events([
            {"ts": 300.0, "account_uuid": "acct-2", "email": "other@example.net",
             "organization_rate_limit_tier": "max_20x"},
        ])
        self._declare(server, "acct-1", (100.0, "default_claude_pro"))
        push.store_tiers(_reply(server))
        assert {(e["ts"], e["account_uuid"]) for e in self._declared()} == {
            (100.0, "acct-1"), (300.0, "acct-2"),
        }

    def test_an_entry_on_a_captures_own_instant_is_dropped_not_written(
        self, server, client_cache,
    ):
        """ts is the whole primary key; a write there would take the capture's place,
        and a capture is the only record that anyone was signed in at that moment."""
        self._declare(server, "acct-1", (1.0, "max_20x"), (100.0, "default_claude_pro"))
        assert push.store_tiers(_reply(server)) == 1
        capture = [e for e in cache_db.load_account_events() if e["ts"] == 1.0]
        assert [e["source"] for e in capture] == [cache_db.SOURCE_CAPTURE]
        assert [e["ts"] for e in self._declared()] == [100.0]

    def test_a_reply_with_no_pull_section_declares_nothing(self, client_cache):
        assert push.store_tiers({"machine_id": "laptop-1"}) == 0

    def test_a_pull_run_reports_what_it_declared(self, server, client_cache,
                                                 tmp_path, monkeypatch):
        self._declare(server, "acct-1", (100.0, "default_claude_pro"))
        token = sf.mint_for(server, "laptop-1", "Laptop")
        config = tmp_path / "push.toml"
        config.write_text(f'[server."{URL}"]\ntoken = "{token}"\nlabel = "Laptop"\n')
        client = TestClient(server)
        monkeypatch.setattr(push, "post_batch", lambda _s, batch: client.post(
            "/v1/ingest", headers=sf.auth(token), json=batch).json())
        [server_config] = push.load_config(config)
        assert push.pull_from(server_config).declared == 1
        assert len(self._declared()) == 1


class TestTheMergedReport:
    @pytest.fixture
    def pulled(self, server, client_cache):
        push.store_pull(URL, _reply(server), TS)
        return client_cache

    def test_the_pulled_day_reads_back_as_a_record(self, pulled):
        [rec] = ccr._remote_records(None, None, None, None).records
        assert rec.model == ccr.REMOTE_MODEL
        assert rec.account == "me@example.net"
        assert rec.cost() > 0

    def test_a_date_filter_bounds_it(self, pulled):
        far = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=365)
        assert ccr._remote_records(far, None, None, None).records == []

    def test_the_note_names_the_spend_and_the_flag(self, pulled):
        note = ccr._remote_note(ccr._remote_records(None, None, None, None))
        assert "ccreport -A" in note
        assert "other machine" in note

    def test_no_pull_means_no_note(self, client_cache):
        assert ccr._remote_note(ccr.RemoteSpend([], set())) == ""

    def test_the_note_counts_machines_and_not_project_names(self, pulled):
        """One machine with 48 projects read as 48 machines."""
        spend = ccr._remote_records(None, None, None, None)
        assert spend.machines == {"Desk"}
        assert "1 other machine." in ccr._remote_note(spend)

    def test_a_login_switch_hides_the_previous_accounts_rows(self, pulled):
        pulled.execute(
            "INSERT INTO account_events (ts, account_uuid, email) VALUES (?, ?, ?)",
            (2.0, "acct-9", "other@example.net"),
        )
        pulled.commit()
        assert ccr._remote_records(None, None, None, None).records == []
        assert cache_db.load_remote_day_costs("acct-1"), "the rows stay for a switch back"


class TestTheCommands:
    """Both spellings exist so each half is testable on its own."""

    @pytest.fixture
    def wired(self, server, client_cache, tmp_path, monkeypatch):
        """push.toml pointing at the in-process server, requests short-circuited."""
        token = sf.mint_for(server, "laptop-1", "Laptop")
        config = tmp_path / "push.toml"
        config.write_text(
            f'[server."{URL}"]\ntoken = "{token}"\nlabel = "Laptop"\n'
        )
        client = TestClient(server)

        def post(_server, batch):
            return client.post(
                "/v1/ingest", headers=sf.auth(token), json=batch,
            ).json()

        monkeypatch.setattr(push, "post_batch", post)
        return config

    def test_pull_alone_stores_the_remainder_and_sends_nothing(self, wired):
        [server_config] = push.load_config(wired)
        result = push.pull_from(server_config)
        assert result.pulled == 1
        assert cache_db.load_remote_day_costs("acct-1")

    def test_a_plain_push_asks_for_no_remainder(self, wired, monkeypatch):
        monkeypatch.setattr(push, "refresh_cache", lambda: None)
        [result] = push.run_once(config_path=wired, force=True, pull=False)
        assert result.pulled == 0
        assert cache_db.load_remote_day_costs("acct-1") == []

    def test_sync_pushes_and_pulls_in_one_run(self, wired, monkeypatch):
        monkeypatch.setattr(push, "refresh_cache", lambda: None)
        [result] = push.run_once(config_path=wired, force=True, pull=True)
        assert result.pulled == 1

    def test_a_machine_signed_in_to_nothing_pulls_nothing(self, wired, client_cache):
        client_cache.execute("DELETE FROM account_events")
        client_cache.commit()
        [server_config] = push.load_config(wired)
        assert push.pull_from(server_config).pulled == 0


class TestTheStatusLineMerge:
    def test_the_window_cost_gains_the_other_machines(self, server, client_cache):
        from ccreport import statusline

        push.store_pull(URL, _reply(server), TS)
        usage = {"seven_day_cost": "1.0", "seven_day_project_cost": "0.5"}
        statusline._merge_remote_costs(usage, TS)
        assert float(usage["seven_day_cost"]) > 1.0
        assert usage["seven_day_project_cost"] == "0.5", "the split stays local"

    def test_a_machine_that_stopped_pushing_is_marked_stale(self, server, client_cache):
        from ccreport import statusline

        push.store_pull(URL, _reply(server), TS)
        usage = {"seven_day_cost": "1.0"}
        statusline._merge_remote_costs(usage, TS + 10 * 86400)
        assert usage["remote_stale"] is True

    def test_nothing_pulled_leaves_the_window_alone(self, client_cache):
        from ccreport import statusline

        usage = {"seven_day_cost": "1.0"}
        statusline._merge_remote_costs(usage, TS)
        assert usage == {"seven_day_cost": "1.0"}
