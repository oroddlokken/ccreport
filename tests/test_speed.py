"""Tests for request timing: the span derivation, its storage and `ccreport speed`.

The logs are written as real JSONL in the shape Claude Code writes them — one
line per content block, every block of a reply sharing its message id and
request id, the request opened by the user line before it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccreport import cache_db, scan, speed
from ccreport import ccreport as ccr
from ccreport.aggregate import TokenCounts, UsageRecord
from ccreport.speed import MIN_RATE_OUTPUT_TOKENS, SpeedBucket, SpeedSums

T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC).timestamp()
"""Behind any archive cutoff the tests set."""


def _iso(offset_s: float) -> str:
    return datetime.fromtimestamp(T0 + offset_s, tz=UTC).isoformat().replace("+00:00", "Z")


def _user(offset_s: float, *, tool_result: bool = False, sid: str = "s1") -> dict:
    content = [{"type": "tool_result", "content": "ok"}] if tool_result else "hi"
    return {"type": "user", "timestamp": _iso(offset_s), "sessionId": sid,
            "cwd": "/tmp/live", "message": {"role": "user", "content": content}}


def _block(
    offset_s: float, mid: str, *, output: int, kind: str = "text",
    model: str = "claude-opus-5", sid: str = "s1",
) -> dict:
    return {
        "type": "assistant", "timestamp": _iso(offset_s), "sessionId": sid,
        "cwd": "/tmp/live", "requestId": "req-" + mid,
        "message": {
            "id": mid, "model": model, "content": [{"type": kind}],
            "usage": {"input_tokens": 10, "output_tokens": output,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    }


def _turn(offset_s: float, duration_ms: int, *, uuid: str = "turn-1", sid: str = "s1") -> dict:
    return {"type": "system", "subtype": "turn_duration", "durationMs": duration_ms,
            "timestamp": _iso(offset_s), "uuid": uuid, "sessionId": sid}


def _write(path: Path, lines: list[dict]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _session(path: Path) -> Path:
    """A prompt, a three-block reply, a tool_result, a one-block reply, a turn."""
    return _write(path, [
        _user(0),
        _block(2, "m1", output=400, kind="thinking"),
        _block(3, "m1", output=400, kind="tool_use"),
        _block(4, "m1", output=400, kind="tool_use"),
        _user(5, tool_result=True),
        # A tool_result for the second tool_use, logged after the first; the
        # request still starts at the later one.
        _user(6, tool_result=True),
        _block(10, "m2", output=50),
        _turn(11, 11_000),
    ])


@pytest.fixture
def loader(tmp_path, monkeypatch):
    """load_all_records wired to a temp DB and a temp project tree."""
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cache_db, "_conn", None)
    projects_root = tmp_path / "projects"
    projects = projects_root / "-tmp-live"
    projects.mkdir(parents=True)
    monkeypatch.setattr(ccr, "discover_jsonl_files", lambda: sorted(projects.glob("*.jsonl")))
    monkeypatch.setattr(ccr, "_ensure_cache_valid", lambda _live_paths: None)
    monkeypatch.setattr(ccr, "_get_projects_dirs", lambda: [projects_root])
    monkeypatch.setattr(cache_db, "get_project_overrides", list)
    cache_db.init_ccreport_meta(ccr.CACHE_VERSION, "test-hash")
    yield projects
    cache_db.get_connection().close()
    cache_db._conn = None


class TestRequestClock:
    def test_a_reply_spans_the_user_line_before_it_to_its_last_block(self):
        clock = speed.RequestClock()
        clock.user(T0)
        clock.block("a", T0 + 2)
        clock.block("a", T0 + 4)
        clock.block("a", T0 + 3)
        assert clock.span("a") == (T0, T0 + 4)

    def test_a_user_line_between_two_blocks_does_not_restart_the_reply(self):
        clock = speed.RequestClock()
        clock.user(T0)
        clock.block("a", T0 + 1)
        clock.user(T0 + 2)
        clock.block("a", T0 + 3)
        assert clock.span("a") == (T0, T0 + 3)

    def test_a_reply_with_no_user_line_before_it_has_no_start(self):
        clock = speed.RequestClock()
        clock.block("a", T0)
        assert clock.span("a") == (None, T0)
        assert speed.latency(*clock.span("a")) is None

    def test_a_user_line_stamped_after_the_block_is_not_its_start(self):
        clock = speed.RequestClock()
        clock.user(T0 + 5)
        clock.block("a", T0)
        assert clock.span("a") == (None, T0)

    def test_an_unknown_reply_has_no_span(self):
        assert speed.RequestClock().span("nope") == (None, None)


class TestThreshold:
    def test_a_reply_below_the_floor_has_a_latency_and_no_rate(self):
        assert speed.latency(T0, T0 + 2) == 2
        assert speed.output_rate(MIN_RATE_OUTPUT_TOKENS - 1, T0, T0 + 2) is None

    def test_a_reply_at_the_floor_has_a_rate(self):
        assert speed.output_rate(MIN_RATE_OUTPUT_TOKENS, T0, T0 + 2) == MIN_RATE_OUTPUT_TOKENS / 2

    def test_the_sums_count_every_timed_request_and_rate_only_the_long_ones(self):
        sums = SpeedSums()
        sums.add(MIN_RATE_OUTPUT_TOKENS * 2, T0, T0 + 4)
        sums.add(5, T0, T0 + 1)
        sums.add(500, None, T0)  # untimed: not counted at all
        assert sums.as_tuple() == (2, 5.0, MIN_RATE_OUTPUT_TOKENS * 2, 4.0)


class TestParse:
    def test_every_block_of_a_reply_carries_the_replys_span(self, tmp_path):
        records = scan.parse_jsonl(_session(tmp_path / "s1.jsonl")).records
        spans = {(r.message_id, r.req_start, r.req_end) for r in records}
        assert spans == {("m1", T0, T0 + 4), ("m2", T0 + 6, T0 + 10)}
        assert len(records) == 4

    def test_a_tool_result_starts_the_request_that_answers_it(self, tmp_path):
        records = scan.parse_jsonl(_session(tmp_path / "s1.jsonl")).records
        (m2,) = [r for r in records if r.message_id == "m2"]
        assert speed.latency(m2.req_start, m2.req_end) == 4

    def test_a_turn_takes_the_model_of_the_reply_before_it(self, tmp_path):
        turns = scan.parse_jsonl(_session(tmp_path / "s1.jsonl")).turns
        assert turns == [scan.Turn("turn-1", T0 + 11, "s1", "claude-opus-5", 11_000)]

    def test_a_turn_before_any_reply_is_dropped(self, tmp_path):
        path = _write(tmp_path / "s.jsonl", [_user(0), _turn(1, 1000)])
        assert scan.parse_jsonl(path).turns == []

    def test_a_synthetic_reply_is_not_timed_and_names_no_turn(self, tmp_path):
        path = _write(tmp_path / "s.jsonl", [
            _user(0), _block(1, "m1", output=0, model="<synthetic>"), _turn(2, 2000),
        ])
        parsed = scan.parse_jsonl(path)
        assert (parsed.records[0].req_start, parsed.records[0].req_end) == (None, None)
        assert parsed.turns == []

    def test_the_span_round_trips_through_the_cache(self, loader):
        _session(loader / "s1.jsonl")
        records = ccr.load_all_records()
        assert {(r.message_id, r.req_start, r.req_end) for r in records} == {
            ("m1", T0, T0 + 4), ("m2", T0 + 6, T0 + 10),
        }
        # Second read comes off the cache rather than the parse.
        again = ccr.load_all_records()
        assert {(r.message_id, r.req_start, r.req_end) for r in again} == {
            ("m1", T0, T0 + 4), ("m2", T0 + 6, T0 + 10),
        }

    def test_turns_are_saved_with_the_file_and_replaced_with_it(self, loader):
        path = _session(loader / "s1.jsonl")
        ccr.load_all_records()
        assert cache_db.load_ccreport_turns(None, None) == [
            ("turn-1", T0 + 11, "s1", "claude-opus-5", 11_000),
        ]
        _write(path, [_user(0), _block(1, "m9", output=10), _turn(2, 2000, uuid="turn-2")])
        ccr.load_all_records()
        assert [row[0] for row in cache_db.load_ccreport_turns(None, None)] == ["turn-2"]


class TestBucket:
    def test_a_live_cell_answers_medians(self):
        cell = SpeedBucket()
        for secs in (1, 2, 3, 4, 10):
            cell.add_request(MIN_RATE_OUTPUT_TOKENS * secs * 2, T0, T0 + secs)
        assert cell.requests == 5
        assert cell.latency_mid() == 3
        assert cell.latency_p90() == pytest.approx(7.6)
        assert cell.rate_mid() == MIN_RATE_OUTPUT_TOKENS * 2

    def test_an_archived_day_turns_the_cell_to_means_weighted_by_requests(self):
        cell = SpeedBucket()
        cell.add_request(MIN_RATE_OUTPUT_TOKENS, T0, T0 + 1)
        cell.add_archived(SpeedSums(timed_n=3, latency_s=9.0, rate_output=900, rate_s=3.0))
        assert cell.archived
        assert cell.requests == 4
        assert cell.latency_mid() == 10 / 4
        assert cell.latency_p90() is None
        assert cell.rate_mid() == (900 + MIN_RATE_OUTPUT_TOKENS) / 4

    def test_an_archived_row_with_no_timing_leaves_the_cell_alone(self):
        cell = SpeedBucket()
        cell.add_archived(SpeedSums())
        assert not cell.archived
        assert cell.requests == 0

    @pytest.mark.parametrize(("by", "key"), [
        ("day", "2026-01-15"), ("week", "2026-W03"), ("month", "2026-01"), (None, ""),
    ])
    def test_period_keys(self, by, key):
        assert speed.period_key("2026-01-15", by) == key

    def test_an_iso_week_crosses_the_year_on_its_monday(self):
        assert speed.period_key("2027-01-01", "week") == "2026-W53"


class TestArchive:
    def test_the_fold_keeps_the_sums_and_the_report_reads_them_as_means(self, loader):
        path = _session(loader / "s1.jsonl")
        ccr.load_all_records()
        path.unlink()
        ccr.cmd_archive(argparse.Namespace(dry_run=False, min_age_days=30))

        (row,) = cache_db.load_ccreport_archive()
        # m1: 4s and 400 tokens, above the floor; m2: 4s and 50 tokens, below it.
        assert row[16:] == (2, 8.0, 400, 4.0)

        records = ccr.load_all_records()
        (rec,) = records
        assert rec.speed == SpeedSums(2, 8.0, 400, 4.0)
        cells = speed.fold(records, ccr._speed_turns(records, None, None), "day")
        cell = cells[("2026-01-15", "claude-opus-5")]
        assert cell.archived
        assert (cell.requests, cell.latency_mid(), cell.rate_mid()) == (2, 4.0, 100.0)
        # The turn rows outlive the archive, so the turn median survives it.
        assert cell.turn_mid() == 11.0

    def test_archive_rows_folded_before_the_columns_read_as_untimed(self):
        rec = UsageRecord(
            message_id="", model="claude-opus-5", tokens=TokenCounts(output=500),
            timestamp=datetime.fromtimestamp(T0, tz=UTC), session_id="s", project="p",
            speed=SpeedSums(),
        )
        assert speed.fold([rec], [], "day") == {}


class TestReport:
    def test_json_has_one_entry_per_cell_and_one_per_model_total(self, loader, capsys):
        _session(loader / "s1.jsonl")
        _write(loader / "s2.jsonl", [
            _user(100, sid="s2"),
            _block(103, "h1", output=300, model="claude-haiku-5-5", sid="s2"),
        ])
        ccr.cmd_speed(argparse.Namespace(
            since=None, until=None, project=None, account=None, json=True, by="day",
        ))
        entries = json.loads(capsys.readouterr().out)
        assert entries == [
            {"period": "2026-01-15", "model": "claude-haiku-5-5", "requests": 1, "turns": 0,
             "estimate": "median", "latency_s": 3.0, "latency_p90_s": 3.0,
             "turn_s": None, "output_tok_s": 100.0},
            {"period": "2026-01-15", "model": "claude-opus-5", "requests": 2, "turns": 1,
             "estimate": "median", "latency_s": 4.0, "latency_p90_s": 4.0,
             "turn_s": 11.0, "output_tok_s": 100.0},
            {"period": None, "model": "claude-haiku-5-5", "requests": 1, "turns": 0,
             "estimate": "median", "latency_s": 3.0, "latency_p90_s": 3.0,
             "turn_s": None, "output_tok_s": 100.0},
            {"period": None, "model": "claude-opus-5", "requests": 2, "turns": 1,
             "estimate": "median", "latency_s": 4.0, "latency_p90_s": 4.0,
             "turn_s": 11.0, "output_tok_s": 100.0},
        ]

    def test_a_project_filter_reaches_the_turns_through_their_session(self, loader, capsys):
        _session(loader / "s1.jsonl")
        ccr.cmd_speed(argparse.Namespace(
            since=None, until=None, project="nothing-matches", account=None,
            json=True, by="week",
        ))
        assert json.loads(capsys.readouterr().out) == []

    def test_a_copied_turn_counts_once(self, loader):
        _session(loader / "s1.jsonl")
        _write(loader / "s1-resumed.jsonl", [_block(20, "m3", output=10), _turn(11, 11_000)])
        records = ccr.load_all_records()
        assert ccr._speed_turns(records, None, None) == [("2026-01-15", "claude-opus-5", 11_000)]

    def test_the_table_names_each_model_and_flags_archived_cells(self, loader, capsys):
        path = _session(loader / "s1.jsonl")
        ccr.load_all_records()
        path.unlink()
        ccr.cmd_archive(argparse.Namespace(dry_run=False, min_age_days=30))
        capsys.readouterr()
        ccr.cmd_speed(argparse.Namespace(
            since=None, until=None, project=None, account=None, json=False, by="month",
        ))
        out = capsys.readouterr().out
        assert "Speed by month" in out
        assert "opus-5" in out
        assert "4.0s†" in out
        assert "includes archived days" in out

    def test_no_timed_requests_says_so(self, loader, capsys):
        ccr.report_speed([], [], "week")
        assert "No timed requests" in capsys.readouterr().out


class TestMigration:
    def test_a_db_before_the_step_gains_the_columns_and_keeps_its_rows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "cache.db"
        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        conn.execute(
            "INSERT INTO ccreport_archive (day, oslo_date, sid, project, model, cwd, repo, "
            "dir_prefix, min_ts, max_ts, input_tokens, output_tokens, cache_create, "
            "cache_read, cost, n) VALUES ('2026-01-15', '2026-01-15', 's', 'p', "
            "'claude-opus-5', '', '', '', 1, 2, 3, 4, 5, 6, 7.5, 8)"
        )
        # Back to the shape version 15 left: no timing columns, no turns table.
        for col in ("req_start", "req_end"):
            conn.execute(f"ALTER TABLE ccreport_records DROP COLUMN {col}")
        for col in ("timed_n", "latency_s", "rate_output", "rate_s"):
            conn.execute(f"ALTER TABLE ccreport_archive DROP COLUMN {col}")
        conn.execute("DROP TABLE ccreport_turns")
        conn.execute("DELETE FROM schema_migrations WHERE version = 16")
        conn.execute("PRAGMA user_version = 15")
        conn.commit()
        conn.close()
        cache_db._conn = None

        conn = cache_db.get_connection()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ccreport_records)")}
            assert {"req_start", "req_end"} <= cols
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert {"ccreport_turns", "idx_ccturn_file"} <= tables
            cache_db.init_ccreport_meta(1, "h")
            (row,) = cache_db.load_ccreport_archive()
            assert row[14:] == (7.5, 8, 0, 0.0, 0, 0.0)
        finally:
            conn.close()
            cache_db._conn = None

    def test_the_step_is_idempotent_on_a_fresh_db(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "x.db")
        conn.executescript(cache_db._SCHEMA_SQL)
        cache_db._migrate_request_timing(conn)
        cache_db._migrate_request_timing(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ccreport_archive)")}
        assert {"timed_n", "latency_s", "rate_output", "rate_s"} <= cols


class TestReplyTracker:
    def test_a_reply_is_finished_by_the_next_one_and_rated_like_the_parse(self, tmp_path):
        tracker = speed.ReplyTracker()
        tracker.user(T0)
        tracker.block("a", T0 + 2, 400)
        tracker.block("a", T0 + 4, 400)
        assert tracker.last() is None, "a reply still open can still move its end"
        tracker.user(T0 + 5)
        tracker.block("b", T0 + 6, 50)
        assert tracker.last() == (T0 + 4, 100.0)

    def test_a_turn_end_finishes_the_open_reply(self):
        tracker = speed.ReplyTracker()
        tracker.user(T0)
        tracker.block("a", T0 + 2, 200)
        tracker.turn_end()
        assert tracker.last() == (T0 + 2, 100.0)

    def test_a_reply_below_the_floor_keeps_the_previous_rate(self):
        tracker = speed.ReplyTracker()
        tracker.user(T0)
        tracker.block("a", T0 + 2, 200)
        tracker.block("b", T0 + 3, 10)
        tracker.turn_end()
        assert tracker.last() == (T0 + 2, 100.0)

    def test_a_user_line_inside_a_reply_does_not_split_it(self):
        tracker = speed.ReplyTracker()
        tracker.user(T0)
        tracker.block("a", T0 + 1, 400)
        tracker.user(T0 + 2)
        tracker.block("a", T0 + 4, 400)
        tracker.turn_end()
        assert tracker.last() == (T0 + 4, 100.0)

    def test_it_survives_a_dump_and_load_mid_reply(self):
        tracker = speed.ReplyTracker()
        tracker.user(T0)
        tracker.block("a", T0 + 2, 400)
        resumed = speed.ReplyTracker.load(json.loads(json.dumps(tracker.dump())))
        resumed.block("a", T0 + 4, 400)
        resumed.turn_end()
        assert resumed.last() == (T0 + 4, 100.0)

    def test_it_agrees_with_the_parse_on_a_whole_log(self, tmp_path):
        path = _session(tmp_path / "s1.jsonl")
        records = scan.parse_jsonl(path).records
        (m1,) = {(r.tokens.output, r.req_start, r.req_end) for r in records if r.message_id == "m1"}
        tracker = speed.ReplyTracker()
        tracker.user(T0)
        for offset in (2, 3, 4):
            tracker.block("m1:req-m1", T0 + offset, 400)
        tracker.turn_end()
        assert tracker.last() == (m1[2], speed.output_rate(*m1))


class TestSessionRate:
    """compute_session_usage's last_rate, the status line's source."""

    CWD = "/tmp/live"

    @pytest.fixture
    def proj(self, monkeypatch, tmp_path):
        from ccreport import pricing

        d = tmp_path / "projects" / "-tmp-live"
        d.mkdir(parents=True)
        monkeypatch.setattr(pricing, "_get_projects_dirs", lambda: [d.parent])
        return d

    def _rate(self) -> float | None:
        from ccreport.pricing import compute_session_usage

        return compute_session_usage("s1", self.CWD).last_rate

    def test_a_session_with_no_finished_reply_has_no_rate(self, proj):
        _write(proj / "s1.jsonl", [_user(0), _block(2, "m1", output=400)])
        assert self._rate() is None

    def test_the_last_finished_reply_is_the_rate(self, proj):
        _session(proj / "s1.jsonl")
        # m2 is below the floor, so m1 — 400 tokens over 4 s — is the last rated.
        assert self._rate() == 100.0

    def test_a_reply_straddling_two_renders_is_rated_whole(self, proj):
        path = _write(proj / "s1.jsonl", [_user(0), _block(2, "m1", output=800, kind="thinking")])
        assert self._rate() is None
        with path.open("a") as fh:
            for line in (_block(8, "m1", output=800), _turn(9, 9000)):
                fh.write(json.dumps(line) + "\n")
        assert self._rate() == 100.0

    def test_the_newest_reply_across_files_wins(self, proj):
        _session(proj / "s1.jsonl")
        sub = proj / "s1" / "subagents"
        sub.mkdir(parents=True)
        _write(sub / "agent-1.jsonl", [
            _user(20), _block(22, "x1", output=600), _turn(23, 3000),
        ])
        assert self._rate() == 300.0


class TestStatusLineSegment:
    def _render(self, last_rate):
        from ccreport import statusline as sl

        return sl._render_session("Opus 5", "", False, None, 0, 0, 0, 0, "", last_rate)

    def test_off_by_default(self):
        assert "tok/s" not in self._render(87.4)

    def test_on_renders_the_rate(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_TOK_S", "1")
        assert "\033[0;90m87 tok/s\033[0m" in self._render(87.4)

    def test_on_with_no_timed_reply_renders_nothing(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_TOK_S", "1")
        assert "tok/s" not in self._render(None)

    def test_the_rate_rides_the_fast_cache(self, monkeypatch, tmp_path):
        from ccreport import statusline as sl

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        fetched = sl._Fetched(
            git=sl.GitInfo("", "", "", "", 0, 0), battery={}, dsp=False, dcat={},
            usage={}, chat_cost=0.0, chat_families=[], last_rate=42.5, cums=(0, 0, 0),
            total_in=0, sandbox="", sessions="", account="", update="",
        )
        sl._save_fetched("sid-1", "/cwd", 1000.0, fetched)
        loaded = sl._load_fetched("sid-1", "/cwd", 1005.0)
        assert loaded is not None
        assert loaded[0].last_rate == 42.5
