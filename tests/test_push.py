"""The push client: what it sends, what it records, and when it runs at all."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport import cache_db, push
from ccreport.server.factory import create_app

TS = datetime(2026, 3, 2, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


def _write_config(tmp_path, url="https://ccr.example.net", token="tok", **extra) -> Path:
    path = tmp_path / "push.toml"
    body = [f'[server."{url}"]', f'token = "{token}"', 'label = "Laptop"']
    body += [f"{k} = {json.dumps(v)}" for k, v in extra.items()]
    path.write_text("\n".join(body) + "\n")
    return path


def _cached_file(path="/p/a.jsonl", mtime_ns=1, size=100, records=None) -> None:
    """Put one file's records in the local cache, the way ccreport would."""
    records = records or [{
        "mid": "msg_1", "model": "claude-sonnet-4-5-20250929", "ts": TS,
        "sid": "sess-1", "project": "ccr-projA", "cwd": "/tmp/ccr-projA",
        "repo": "github.com/o/p", "dk": "msg_1:req_1", "cost": None,
        "t": [1000, 200, 5000, 30000],
    }]
    cache_db.save_ccreport_files([(path, mtime_ns, size, records)])


class TestConfig:
    def test_no_file_is_no_push_and_no_error(self, tmp_path):
        """The ordinary state of a machine nobody has connected to anything."""
        assert push.load_config(tmp_path / "absent.toml") == []
        assert not push.configured(tmp_path / "absent.toml")

    def test_a_broken_file_reads_the_same_as_no_file(self, tmp_path):
        path = tmp_path / "push.toml"
        path.write_text("this is not = = toml")
        assert push.load_config(path) == []

    def test_an_entry_with_no_token_is_skipped(self, tmp_path):
        path = tmp_path / "push.toml"
        path.write_text('[server."https://ccr.example.net"]\nlabel = "Laptop"\n')
        assert push.load_config(path) == []

    def test_each_table_is_one_server(self, tmp_path):
        path = tmp_path / "push.toml"
        path.write_text(
            '[server."https://a.example"]\ntoken = "t1"\nlabel = "A"\n'
            '[server."https://b.example"]\ntoken = "t2"\nlabel = "B"\n',
        )
        assert [s.url for s in push.load_config(path)] == [
            "https://a.example", "https://b.example",
        ]

    def test_the_body_limit_can_be_set_per_server(self, tmp_path):
        assert push.load_config(_write_config(tmp_path, max_body=1234))[0].max_body == 1234


class TestChangedFiles:
    def test_an_acknowledged_file_is_not_offered_again(self, tmp_path):
        _cached_file()
        conn = push._read_only(cache_db.DB_PATH)
        assert push.changed_files(conn, {}) == [("/p/a.jsonl", 1, 100)]
        assert push.changed_files(conn, {"/p/a.jsonl": (1, 100)}) == []
        conn.close()

    def test_a_changed_file_is_offered_again(self, tmp_path):
        _cached_file(mtime_ns=2, size=200)
        conn = push._read_only(cache_db.DB_PATH)
        assert push.changed_files(conn, {"/p/a.jsonl": (1, 100)}) == [("/p/a.jsonl", 2, 200)]
        conn.close()

    def test_the_cache_is_opened_read_only(self, tmp_path):
        """A render must never wait on this process for a write lock."""
        _cached_file()
        conn = push._read_only(cache_db.DB_PATH)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM ccreport_files")
        conn.close()


class TestCacheRefresh:
    """A push parses its own records, since only a parse writes the table it reads."""

    @pytest.fixture
    def logged(self):
        """One session log on disk that nothing has parsed yet."""
        from ccreport import scan

        root = scan._PROJECT_ROOTS[0] / "-tmp-projA"
        root.mkdir(parents=True)
        path = root / "sess-1.jsonl"
        path.write_text(json.dumps({
            "type": "assistant",
            "timestamp": "2026-03-02T12:00:00Z",
            "sessionId": "sess-1",
            "cwd": "/tmp/projA",
            "requestId": "req-1",
            "message": {
                "id": "msg_1", "model": "claude-opus-5",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        }) + "\n")
        return path

    def _sent(self, monkeypatch) -> list[list[str]]:
        """The paths each push_to call found pending, without a server."""
        sent: list[list[str]] = []

        def record(server, full=False, db_path=None):
            conn = push._read_only(cache_db.DB_PATH)
            sent.append([path for path, _m, _s in push.changed_files(conn, {})])
            conn.close()
            return push.PushResult(server=server.url)

        monkeypatch.setattr(push, "push_to", record)
        return sent

    def test_a_run_sends_a_session_no_report_has_read(self, tmp_path, logged, monkeypatch):
        """The bug: a machine whose CLI nobody runs pushed nothing and called it a success."""
        sent = self._sent(monkeypatch)
        push.run_once(config_path=_write_config(tmp_path), force=True)
        assert sent == [[str(logged)]]

    def test_a_run_that_is_not_due_parses_nothing(self, tmp_path, logged, monkeypatch):
        cache_db.write_push_attempt("https://ccr.example.net", time.time(), 0)
        push.run_once(config_path=_write_config(tmp_path))
        conn = push._read_only(cache_db.DB_PATH)
        assert push.changed_files(conn, {}) == []
        conn.close()

    def test_a_blocked_run_parses_nothing(self, tmp_path, logged, monkeypatch):
        """Off-network is the state a laptop spends its evenings in."""
        monkeypatch.setattr(push, "on_allowed_network", lambda networks: False)
        config = _write_config(tmp_path, networks=["10.0.0.0/8"])
        assert push.run_once(config_path=config, force=True)[0].blocked
        conn = push._read_only(cache_db.DB_PATH)
        assert push.changed_files(conn, {}) == []
        conn.close()

    def test_two_servers_parse_once(self, tmp_path, logged, monkeypatch):
        path = tmp_path / "push.toml"
        path.write_text(
            '[server."https://a.example"]\ntoken = "t1"\n'
            '[server."https://b.example"]\ntoken = "t2"\n',
        )
        calls = []
        monkeypatch.setattr(push, "push_to",
                            lambda server, full=False, db_path=None: push.PushResult(server.url))
        monkeypatch.setattr(push, "refresh_cache", lambda: calls.append(1))
        push.run_once(config_path=path, force=True)
        assert calls == [1]

    def test_the_client_still_imports_no_rich(self):
        """What the parse moved to scan.py for: a detached spawn pays for its imports."""
        loaded = subprocess.run(
            [sys.executable, "-c",
             "import sys, ccreport.push, ccreport.scan; print('rich' in sys.modules)"],
            capture_output=True, text=True, check=True,
        )
        assert loaded.stdout.strip() == "False"

    def test_a_locked_database_costs_the_records_and_not_the_push(self, tmp_path, monkeypatch):
        """What is already cached still goes out; the fresh records wait a run."""
        from ccreport import scan

        def locked():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(scan, "refresh_cache", locked)
        sent = self._sent(monkeypatch)
        _cached_file()
        push.run_once(config_path=_write_config(tmp_path), force=True)
        assert sent == [["/p/a.jsonl"]]


class TestPayload:
    def _built(self, monkeypatch, override=None, events=()):
        from ccreport.accounts import AccountTimeline

        for event in events:
            cache_db.record_account_event(event["identity"], now=event["ts"])
        timeline = AccountTimeline(cache_db.load_account_events())
        conn = push._read_only(cache_db.DB_PATH)
        try:
            files = push.build_files(conn, [("/p/a.jsonl", 1, 100)], timeline, override)
        finally:
            conn.close()
        return files[0]["records"][0]

    def test_a_file_travels_whole(self, tmp_path):
        _cached_file()
        conn = push._read_only(cache_db.DB_PATH)
        files = push.build_files(conn, [("/p/a.jsonl", 1, 100)], _NoAccounts(), None)
        conn.close()
        assert files[0]["path"] == "/p/a.jsonl"
        assert files[0]["mtime_ns"] == 1
        assert len(files[0]["records"]) == 1

    def test_the_account_is_stamped_from_the_change_log(self, tmp_path):
        _cached_file()
        rec = self._built(None, events=[{
            "ts": TS - 86400,
            "identity": {"accountUuid": "u-work", "emailAddress": "me@work.example",
                         "organizationName": "Org"},
        }])
        assert rec["account_uuid"] == "u-work"
        assert rec["account_label"] == "me@work.example"

    def test_history_older_than_the_log_is_sent_as_unknown(self, tmp_path):
        """The log starts when capture was switched on; before it, nothing knows."""
        _cached_file()
        rec = self._built(None, events=[{
            "ts": TS + 86400,
            "identity": {"accountUuid": "u-work", "emailAddress": "me@work.example",
                         "organizationName": "Org"},
        }])
        assert rec["account_uuid"] == "unknown"

    def test_the_project_is_resolved_through_this_machines_rules(self, tmp_path):
        """The server holds no merge rules and treats what arrives as final."""
        _cached_file()
        rec = self._built(None, override=lambda repo, cwd, project: "merged-target")
        assert rec["project"] == "merged-target"

    def test_only_a_cost_the_log_carried_is_sent(self, tmp_path):
        _cached_file()
        assert self._built(None)["cost"] is None
        _cached_file(records=[{
            "mid": "m", "model": "claude-haiku-4-5", "ts": TS, "sid": "s",
            "project": "p", "cwd": None, "repo": None, "dk": None, "cost": 0.5,
            "t": [1, 2, 3, 4],
        }])
        assert self._built(None)["cost"] == 0.5

    def test_the_machines_utc_offset_travels_with_each_record(self, tmp_path):
        """So the server buckets the call under the machine's day, not its own."""
        _cached_file()
        expected = datetime.fromtimestamp(TS, tz=UTC).astimezone().utcoffset()
        assert expected is not None
        assert self._built(None)["utc_offset"] == int(expected.total_seconds())

    def test_the_batch_never_names_its_machine(self, tmp_path):
        """The server takes that from the token."""
        _cached_file()
        conn = push._read_only(cache_db.DB_PATH)
        files = push.build_files(conn, [("/p/a.jsonl", 1, 100)], _NoAccounts(), None)
        conn.close()
        batch = push.pack_batches(files, "Laptop", push.DEFAULT_MAX_BODY)[0]
        assert "machine_id" not in batch
        assert batch["label"] == "Laptop"


class _NoAccounts:
    """A timeline for a machine whose change log starts after the records."""

    def uuid_at(self, when):
        return None

    def label_at(self, when):
        return "unknown"


class TestBatching:
    def _files(self, n: int, weight: int = 500) -> list[dict]:
        return [
            {"path": f"/p/{i}.jsonl", "mtime_ns": i, "size": weight,
             "records": [{"pad": "x" * weight}]}
            for i in range(n)
        ]

    def test_everything_fits_in_one_request_when_it_can(self):
        batches = push.pack_batches(self._files(3), "Laptop", 1_000_000)
        assert len(batches) == 1
        assert len(batches[0]["files"]) == 3

    def test_a_file_is_never_split_across_requests(self):
        """Whole files only: it is what makes the server's replace one transaction."""
        batches = push.pack_batches(self._files(4), "Laptop", 1200)
        assert sum(len(b["files"]) for b in batches) == 4
        assert all(b["files"] for b in batches)

    def test_a_single_oversized_file_still_goes_on_its_own(self):
        """The server answers 413 and names the limit, which its owner can act on."""
        batches = push.pack_batches(self._files(1, weight=5000), "Laptop", 100)
        assert len(batches) == 1
        assert len(batches[0]["files"]) == 1

    def test_nothing_to_send_is_no_request(self):
        assert push.pack_batches([], "Laptop", 1000) == []


class TestInterval:
    def test_a_fresh_machine_is_due_at_once(self):
        assert push.due(0.0, 0, time.time())

    def test_inside_the_interval_it_is_not(self):
        now = 10_000.0
        assert not push.due(now - 60, 0, now)

    def test_the_interval_widens_with_consecutive_failures(self):
        now = 1_000_000.0
        one_base_ago = now - push.BASE_INTERVAL_S - 1
        assert push.due(one_base_ago, 0, now)
        assert not push.due(one_base_ago, 1, now), "one failure should have doubled it"
        assert not push.due(one_base_ago, 4, now)

    def test_it_stops_widening_at_the_cap(self):
        now = 1_000_000.0
        assert push.due(now - push.MAX_INTERVAL_S, 99, now)

    def test_one_success_resets_it(self):
        now = 1_000_000.0
        one_base_ago = now - push.BASE_INTERVAL_S - 1
        assert not push.due(one_base_ago, 3, now)
        assert push.due(one_base_ago, 0, now), "a success should be back to the base"


class TestConfiguredInterval:
    """`interval_minutes` is the machine's, not the server's: a wired desktop
    keeps the merged view minutes fresh where a metered laptop pushes rarely.
    """

    def test_it_is_read_as_seconds(self, tmp_path):
        assert push.load_config(_write_config(tmp_path, interval_minutes=5))[0].interval_s == 300

    def test_an_entry_without_it_takes_the_default(self, tmp_path):
        assert push.load_config(_write_config(tmp_path))[0].interval_s == push.BASE_INTERVAL_S

    @pytest.mark.parametrize("value", [0, -5, "soon", 5.5, True, ""])
    def test_an_unusable_value_takes_the_default(self, tmp_path, value):
        conf = _write_config(tmp_path, interval_minutes=value)
        assert push.load_config(conf)[0].interval_s == push.BASE_INTERVAL_S

    def test_a_quoted_number_still_counts(self, tmp_path):
        """A hand-edited 0600 file is the input, so "5" is a typo worth honouring."""
        assert push.load_config(_write_config(tmp_path, interval_minutes="5"))[0].interval_s == 300

    def test_due_widens_from_the_configured_base(self):
        now = 1_000_000.0
        base = 5 * 60
        assert push.due(now - base - 1, 0, now, base)
        assert not push.due(now - base - 1, 1, now, base), "one failure doubles 5 min"
        assert push.due(now - 2 * base - 1, 1, now, base)

    def test_the_cap_never_shortens_a_configured_base(self):
        base = push.MAX_INTERVAL_S * 2
        assert push.attempt_interval(0, base) == base
        assert push.attempt_interval(3, base) == base

    def test_the_cap_still_bounds_the_widening_from_the_default(self):
        assert push.attempt_interval(99) == push.MAX_INTERVAL_S


class TestWatermarkState:
    def test_it_starts_empty_and_records_what_was_acknowledged(self):
        url = "https://ccr.example.net"
        assert cache_db.load_push_state(url) == {}
        cache_db.save_push_state(url, [("/p/a.jsonl", 1, 100)], 500.0)
        assert cache_db.load_push_state(url) == {"/p/a.jsonl": (1, 100)}

    def test_each_server_has_its_own(self):
        cache_db.save_push_state("https://a.example", [("/p/a.jsonl", 1, 100)], 500.0)
        assert cache_db.load_push_state("https://b.example") == {}

    def test_full_forgets_it(self):
        url = "https://ccr.example.net"
        cache_db.save_push_state(url, [("/p/a.jsonl", 1, 100)], 500.0)
        cache_db.clear_push_state(url)
        assert cache_db.load_push_state(url) == {}

    def test_the_attempt_stamp_round_trips(self):
        url = "https://ccr.example.net"
        assert cache_db.read_push_attempt(url) == (0.0, 0, False)
        cache_db.write_push_attempt(url, 500.0, 3, stopped=True)
        assert cache_db.read_push_attempt(url) == (500.0, 3, True)

    def test_the_outcome_round_trips(self):
        url = "https://ccr.example.net"
        assert cache_db.read_push_outcome(url) == (0.0, "")
        cache_db.write_push_attempt(url, 500.0, 0, succeeded=True)
        assert cache_db.read_push_outcome(url) == (500.0, "")
        cache_db.write_push_attempt(url, 900.0, 1, reason="refused")
        assert cache_db.read_push_outcome(url) == (500.0, "refused")

    def test_a_success_clears_the_previous_reason(self):
        url = "https://ccr.example.net"
        cache_db.write_push_attempt(url, 500.0, 1, reason="refused")
        cache_db.write_push_attempt(url, 900.0, 0, succeeded=True)
        assert cache_db.read_push_outcome(url) == (900.0, "")

    def test_an_off_network_run_does_not_date_a_push(self):
        """It sent nothing, so it clears the count without claiming a success."""
        url = "https://ccr.example.net"
        cache_db.write_push_attempt(url, 500.0, 0)
        assert cache_db.read_push_outcome(url)[0] == 0.0


class TestAgainstAServer:
    """The whole path, against the real ingest endpoint."""

    @pytest.fixture
    def server(self, tmp_path):
        app = create_app(sf.config(tmp_path / "server"))
        return app, TestClient(app)

    @pytest.fixture
    def wired(self, server, tmp_path, monkeypatch):
        """push.py pointed at the TestClient, with a token that server minted."""
        app, client = server
        token = sf.mint_for(app, "laptop-1", "Laptop")
        config = push.ServerConfig(
            url="http://testserver", token=token, label="Laptop", machine_id="",
        )

        def post(server_config, batch):
            resp = client.post(
                "/v1/ingest",
                json={**batch, "client_version": "test"},
                headers={"Authorization": f"Bearer {server_config.token}"},
            )
            if resp.status_code != 200:
                raise push.PushError(
                    f"{server_config.url}: {resp.status_code}", terminal=resp.status_code == 401,
                )
            return resp.json()

        monkeypatch.setattr(push, "post_batch", post)
        return app, client, config

    def test_a_first_push_sends_everything_and_records_it(self, wired):
        app, _client, config = wired
        _cached_file()
        result = push.push_to(config)
        assert result.accepted == ["/p/a.jsonl"]
        assert result.records == 1
        assert cache_db.load_push_state(config.url) == {"/p/a.jsonl": (1, 100)}
        assert len(sf.stored(app, "laptop-1")) == 1

    def test_a_second_push_of_the_same_file_sends_nothing(self, wired):
        _app, _client, config = wired
        _cached_file()
        push.push_to(config)
        result = push.push_to(config)
        assert result.accepted == []
        assert result.skipped == []
        assert not result.rejected

    def test_a_changed_file_is_resent(self, wired):
        app, _client, config = wired
        _cached_file()
        push.push_to(config)
        _cached_file(mtime_ns=2, size=200, records=[
            {"mid": "msg_1", "model": "claude-haiku-4-5", "ts": TS, "sid": "s",
             "project": "p", "cwd": None, "repo": None, "dk": "d1", "cost": None,
             "t": [1, 2, 3, 4]},
            {"mid": "msg_2", "model": "claude-haiku-4-5", "ts": TS, "sid": "s",
             "project": "p", "cwd": None, "repo": None, "dk": "d2", "cost": None,
             "t": [1, 2, 3, 4]},
        ])
        result = push.push_to(config)
        assert result.accepted == ["/p/a.jsonl"]
        assert len(sf.stored(app, "laptop-1")) == 2

    def test_a_rejected_file_is_left_out_of_the_watermark_and_retried(self, wired):
        """The watermark is what the server stored, never what was hoped."""
        _app, _client, config = wired
        _cached_file(records=[{
            "mid": "m", "model": "gpt-9-ultra", "ts": TS, "sid": "s", "project": "p",
            "cwd": None, "repo": None, "dk": None, "cost": None, "t": [1, 2, 3, 4],
        }])
        result = push.push_to(config)
        assert [path for path, _ in result.rejected] == ["/p/a.jsonl"]
        assert cache_db.load_push_state(config.url) == {}
        assert push.push_to(config).rejected, "the next run must offer it again"

    def test_full_re_stores_every_file(self, wired):
        """The recovery command: it repairs the server's copy, not just the watermark."""
        _app, _client, config = wired
        _cached_file()
        push.push_to(config)
        result = push.push_to(config, full=True)
        assert result.accepted == ["/p/a.jsonl"]
        assert cache_db.load_push_state(config.url) == {"/p/a.jsonl": (1, 100)}

    def _seed_samples(self, *readings):
        """Write utilization samples the way a status line render would."""
        for i, (pct, resets) in enumerate(readings):
            cache_db.record_rate_limit_snapshots(
                [cache_db.RateLimitSample("session", pct, resets, None, "stdin")],
                now=TS + i * 3600,
            )

    def test_a_push_carries_the_utilization_samples(self, wired):
        from ccreport.server import db

        app, _client, config = wired
        self._seed_samples((5.0, TS + 18000), (40.0, TS + 18000))
        result = push.push_to(config)
        assert result.samples == 2
        rows = db.load_rate_limit_samples(app.state.db.connect())
        assert [row["used_pct"] for row in rows] == [5.0, 40.0]

    def test_a_machine_with_no_changed_log_still_sends_its_samples(self, wired):
        """A quiet machine still renders, and its windows still moved."""
        _app, _client, config = wired
        _cached_file()
        push.push_to(config)
        self._seed_samples((5.0, TS + 18000))
        assert push.push_to(config).samples == 1

    def test_a_sample_already_stored_is_not_offered_again(self, wired):
        _app, _client, config = wired
        self._seed_samples((5.0, TS + 18000))
        push.push_to(config)
        assert push.push_to(config).samples == 0

    def test_full_offers_every_sample_again(self, wired):
        """The recovery command repairs both halves of what the server holds."""
        _app, _client, config = wired
        self._seed_samples((5.0, TS + 18000))
        push.push_to(config)
        assert push.push_to(config, full=True).samples == 1

    def test_a_restricted_machine_sends_them_unchanged(self, wired):
        """A sample carries no project and no session; there is nothing to strip."""
        from dataclasses import replace

        app, _client, config = wired
        self._seed_samples((5.0, TS + 18000))
        push.push_to(replace(config, restricted=True, allow=()))
        from ccreport.server import db

        [row] = db.load_rate_limit_samples(app.state.db.connect())
        assert (row["window"], row["used_pct"]) == ("session", 5.0)

    def test_a_revoked_token_is_terminal(self, wired):
        from ccreport.server import db, tokens

        app, _client, config = wired
        _cached_file()
        push.push_to(config)
        conn = app.state.db.connect()
        db.revoke_token(conn, tokens.token_hash(config.token), time.time())
        conn.commit()
        _cached_file(mtime_ns=3, size=300)
        with pytest.raises(push.PushError) as exc:
            push.push_to(config)
        assert exc.value.terminal


class TestRunOnce:
    @pytest.fixture
    def config_path(self, tmp_path):
        return _write_config(tmp_path)

    def test_a_failure_still_stamps_the_attempt(self, config_path, monkeypatch):
        """Else an unreachable server is probed once per render."""
        def boom(server, full=False, db_path=None):
            raise push.PushError("refused")

        monkeypatch.setattr(push, "push_to", boom)
        push.run_once(config_path=config_path, force=True)
        attempt, failures, stopped = cache_db.read_push_attempt("https://ccr.example.net")
        assert attempt > 0
        assert failures == 1
        assert not stopped

    def test_a_failure_keeps_its_reason(self, config_path, monkeypatch):
        """A count alone cannot tell connection-refused from a 500."""
        def boom(server, full=False, db_path=None):
            raise push.PushError("https://ccr.example.net: refused")

        monkeypatch.setattr(push, "push_to", boom)
        push.run_once(config_path=config_path, force=True)
        assert cache_db.read_push_outcome("https://ccr.example.net")[1].endswith("refused")

    def test_consecutive_failures_accumulate(self, config_path, monkeypatch):
        monkeypatch.setattr(
            push, "push_to",
            lambda server, full=False, db_path=None: (_ for _ in ()).throw(push.PushError("no")),
        )
        push.run_once(config_path=config_path, force=True)
        push.run_once(config_path=config_path, force=True)
        assert cache_db.read_push_attempt("https://ccr.example.net")[1] == 2

    def test_a_success_resets_the_count(self, config_path, monkeypatch):
        url = "https://ccr.example.net"
        cache_db.write_push_attempt(url, 100.0, 4)
        monkeypatch.setattr(
            push, "push_to",
            lambda server, full=False, db_path=None: push.PushResult(server=server.url),
        )
        push.run_once(config_path=config_path, force=True)
        assert cache_db.read_push_attempt(url)[1] == 0
        assert cache_db.read_push_outcome(url)[0] > 0, "the success is what dates the last push"

    def test_a_401_stops_further_attempts(self, config_path, monkeypatch):
        url = "https://ccr.example.net"
        calls = []

        def refuse(server, full=False, db_path=None):
            calls.append(server.url)
            raise push.PushError("token refused", terminal=True)

        monkeypatch.setattr(push, "push_to", refuse)
        push.run_once(config_path=config_path, force=True)
        assert cache_db.read_push_attempt(url)[2] is True
        push.run_once(config_path=config_path)
        assert len(calls) == 1, "a revoked token must not keep knocking"

    def test_the_interval_holds_off_a_run_that_is_not_due(self, config_path, monkeypatch):
        calls = []
        cache_db.write_push_attempt("https://ccr.example.net", time.time(), 0)
        monkeypatch.setattr(
            push, "push_to",
            lambda server, full=False, db_path=None: calls.append(1),
        )
        push.run_once(config_path=config_path)
        assert calls == []

    def test_a_configured_interval_is_what_run_once_waits_out(self, tmp_path, monkeypatch):
        url = "https://ccr.example.net"
        path = _write_config(tmp_path, interval_minutes=5)
        calls = []
        monkeypatch.setattr(
            push, "push_to",
            lambda server, full=False, db_path=None: calls.append(1) or push.PushResult("x"),
        )
        cache_db.write_push_attempt(url, time.time() - 4 * 60, 0)
        push.run_once(config_path=path)
        assert calls == [], "four minutes in, a five-minute machine is not due"
        cache_db.write_push_attempt(url, time.time() - 6 * 60, 0)
        push.run_once(config_path=path)
        assert calls == [1]

    def test_the_manual_command_ignores_the_interval(self, config_path, monkeypatch):
        """Someone who typed it is watching; waiting out an invisible backoff reads as broken."""
        calls = []
        cache_db.write_push_attempt("https://ccr.example.net", time.time(), 0)
        monkeypatch.setattr(
            push, "push_to",
            lambda server, full=False, db_path=None: calls.append(1) or push.PushResult("x"),
        )
        push.run_once(config_path=config_path, force=True)
        assert calls == [1]

    def test_only_narrows_to_one_server(self, tmp_path, monkeypatch):
        path = tmp_path / "push.toml"
        path.write_text(
            '[server."https://a.example"]\ntoken = "t1"\n'
            '[server."https://b.example"]\ntoken = "t2"\n',
        )
        seen = []
        monkeypatch.setattr(
            push, "push_to",
            lambda server, full=False, db_path=None: seen.append(server.url) or push.PushResult("x"),
        )
        push.run_once(config_path=path, only="https://b.example", force=True)
        assert seen == ["https://b.example"]


class TestSpawnGate:
    def test_the_next_attempt_is_a_full_interval_after_a_success(self, tmp_path):
        path = _write_config(tmp_path)
        cache_db.write_push_attempt("https://ccr.example.net", 1000.0, 0)
        assert push.next_attempt_at(1000.0, path) == 1000.0 + push.BASE_INTERVAL_S

    def test_a_failure_pushes_it_further_out(self, tmp_path):
        path = _write_config(tmp_path)
        cache_db.write_push_attempt("https://ccr.example.net", 1000.0, 2)
        assert push.next_attempt_at(1000.0, path) == 1000.0 + push.BASE_INTERVAL_S * 4

    def test_the_configured_interval_decides_the_next_attempt(self, tmp_path):
        path = _write_config(tmp_path, interval_minutes=5)
        cache_db.write_push_attempt("https://ccr.example.net", 1000.0, 0)
        assert push.next_attempt_at(1000.0, path) == 1300.0

    def test_a_failure_doubles_the_configured_one(self, tmp_path):
        path = _write_config(tmp_path, interval_minutes=5)
        cache_db.write_push_attempt("https://ccr.example.net", 1000.0, 1)
        assert push.next_attempt_at(1000.0, path) == 1600.0

    def test_a_stopped_server_contributes_nothing(self, tmp_path):
        path = _write_config(tmp_path)
        cache_db.write_push_attempt("https://ccr.example.net", 1000.0, 0, stopped=True)
        assert push.next_attempt_at(1000.0, path) == 1000.0 + push.MAX_INTERVAL_S

    def test_the_soonest_server_decides(self, tmp_path):
        path = tmp_path / "push.toml"
        path.write_text(
            '[server."https://a.example"]\ntoken = "t1"\n'
            '[server."https://b.example"]\ntoken = "t2"\n',
        )
        cache_db.write_push_attempt("https://a.example", 5000.0, 0)
        cache_db.write_push_attempt("https://b.example", 1000.0, 0)
        assert push.next_attempt_at(5000.0, path) == 1000.0 + push.BASE_INTERVAL_S

    def test_the_stamp_round_trips_through_the_cache(self):
        assert cache_db.read_push_next_attempt() == 0.0
        cache_db.write_push_next_attempt(1234.5)
        assert cache_db.read_push_next_attempt() == 1234.5


class TestStatuslineSpawn:
    """The render path: one stat, one meta read, and a spawn only when due."""

    @pytest.fixture
    def spawns(self, monkeypatch):
        from ccreport import statusline as sl

        seen = []
        monkeypatch.setattr(sl, "_refresh_env", dict)
        monkeypatch.setattr(
            sl.subprocess if hasattr(sl, "subprocess") else sl, "__name__", sl.__name__,
        )
        import subprocess

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: seen.append(a))
        return sl, seen

    def test_no_push_toml_spawns_nothing(self, spawns, tmp_path):
        """A machine that has not opted in pays nothing, not even a database read."""
        sl, seen = spawns
        sl._PUSH_CONFIG = tmp_path / "absent.toml"
        sl._spawn_push(time.time())
        assert seen == []

    def test_a_due_machine_spawns(self, spawns, tmp_path):
        sl, seen = spawns
        sl._PUSH_CONFIG = _write_config(tmp_path)
        cache_db.write_push_next_attempt(0.0)
        sl._spawn_push(time.time())
        assert len(seen) == 1

    def test_a_machine_that_is_not_due_spawns_nothing(self, spawns, tmp_path):
        sl, seen = spawns
        sl._PUSH_CONFIG = _write_config(tmp_path)
        cache_db.write_push_next_attempt(time.time() + 3600)
        sl._spawn_push(time.time())
        assert seen == []

    def test_the_env_var_turns_it_off(self, spawns, tmp_path, monkeypatch):
        sl, seen = spawns
        sl._PUSH_CONFIG = _write_config(tmp_path)
        cache_db.write_push_next_attempt(0.0)
        monkeypatch.setenv("CLAUDE_STATUSLINE_PUSH", "0")
        sl._spawn_push(time.time())
        assert seen == []

    def test_the_statusline_does_not_import_the_pusher(self):
        """It spawns push.py the way it spawns usage_api.py, and never imports it."""
        source = (
            __import__("pathlib").Path(__import__("ccreport").__file__).parent / "statusline.py"
        ).read_text()
        assert "import push" not in source
        assert "from ccreport import push" not in source
