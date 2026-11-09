"""Fast mode and 1-hour cache writes, from the log line to the server's price.

Both are read off `message.usage` and change what a call costs: fast mode by
the model's pricing.FAST_KEY multiple, a 1-hour cache write at twice the input
rate where a 5-minute one is 1.25x. Every path that prices a record from its
tokens has to see them, which is what these follow.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport import cache_db, push, scan
from ccreport import ccreport as ccr
from ccreport.aggregate import TokenCounts, UsageRecord
from ccreport.pricing import (
    _line_cost,
    _rec_cost_from_tokens,
    calc_cost,
    usage_pricing_fields,
)
from ccreport.server import db
from ccreport.server.factory import create_app
from ccreport.server.ingest import IngestRecord

TS = datetime(2026, 11, 9, 12, tzinfo=UTC)


def _usage(*, speed: str = "standard", write_1h: int = 0, write_5m: int = 0, **counts) -> dict:
    return {
        "input_tokens": counts.get("input", 0),
        "output_tokens": counts.get("output", 0),
        "cache_creation_input_tokens": write_1h + write_5m,
        "cache_read_input_tokens": counts.get("read", 0),
        "cache_creation": {
            "ephemeral_1h_input_tokens": write_1h,
            "ephemeral_5m_input_tokens": write_5m,
        },
        "speed": speed,
    }


def _line(usage: dict, *, model: str = "claude-opus-5-5", mid: str = "msg_1") -> dict:
    return {
        "type": "assistant",
        "timestamp": TS.isoformat(),
        "sessionId": "sess",
        "requestId": f"req_{mid}",
        "cwd": "/tmp/live",
        "message": {"id": mid, "model": model, "usage": usage},
    }


class TestCalcCost:
    def test_fast_doubles_a_model_that_documents_it(self):
        standard = calc_cost(1_000, 1_000, 1_000, 1_000, "claude-opus-5-5", TS)
        assert calc_cost(1_000, 1_000, 1_000, 1_000, "claude-opus-5-5", TS, fast=True) == (
            pytest.approx(2 * standard)
        )

    @pytest.mark.parametrize("model", [
        "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5", "claude-fable-5-2", "claude-mythos-5-2",
    ])
    def test_the_documented_fast_models_carry_the_multiple(self, model):
        standard = calc_cost(1_000, 0, 0, 0, model, TS)
        assert calc_cost(1_000, 0, 0, 0, model, TS, fast=True) == pytest.approx(2 * standard)

    def test_fast_on_a_model_with_no_fast_price_is_standard(self):
        standard = calc_cost(1_000, 1_000, 0, 0, "claude-sonnet-5-5", TS)
        assert calc_cost(1_000, 1_000, 0, 0, "claude-sonnet-5-5", TS, fast=True) == standard

    def test_a_1h_write_costs_twice_the_input_rate(self):
        # Opus 5.5: $4 input, so $8 per MTok for a 1-hour write against $5 for 5 minutes.
        assert calc_cost(0, 0, 1_000_000, 0, "claude-opus-5-5", TS, cache_create_1h=1_000_000) == (
            pytest.approx(8.0)
        )
        assert calc_cost(0, 0, 1_000_000, 0, "claude-opus-5-5", TS) == pytest.approx(5.0)

    def test_a_mixed_write_prices_each_share_at_its_own_rate(self):
        cost = calc_cost(0, 0, 1_000_000, 0, "claude-opus-5-5", TS, cache_create_1h=250_000)
        assert cost == pytest.approx(0.25 * 8.0 + 0.75 * 5.0)

    def test_the_1h_count_cannot_exceed_the_writes_it_is_a_share_of(self):
        assert calc_cost(0, 0, 1_000, 0, "claude-opus-5-5", TS, cache_create_1h=5_000) == (
            calc_cost(0, 0, 1_000, 0, "claude-opus-5-5", TS, cache_create_1h=1_000)
        )

    def test_a_1h_write_on_haikus_long_prompt_doubles_the_long_input_rate(self):
        # Over 100k the input rate is $0.50, so a 1-hour write is $1 per MTok.
        cost = calc_cost(0, 0, 1_000_000, 0, "claude-haiku-5-5", TS, cache_create_1h=1_000_000)
        assert cost == pytest.approx(1.0)

    def test_fast_multiplies_the_1h_premium_too(self):
        cost = calc_cost(0, 0, 1_000_000, 0, "claude-opus-5-5", TS, cache_create_1h=1_000_000, fast=True)
        assert cost == pytest.approx(16.0)


class TestUsageFields:
    def test_reads_the_1h_count_and_the_speed(self):
        assert usage_pricing_fields(_usage(speed="fast", write_1h=30, write_5m=70)) == (30, True)

    def test_a_log_before_the_fields_reads_as_none_and_standard(self):
        assert usage_pricing_fields({"input_tokens": 1}) == (0, False)

    @pytest.mark.parametrize("split", [None, "x", {"ephemeral_1h_input_tokens": None}])
    def test_a_malformed_split_reads_as_none(self, split):
        assert usage_pricing_fields({"cache_creation": split}) == (0, False)


class TestLinePath:
    """What the status line's file costs read: one JSONL line at a time."""

    def test_a_fast_line_costs_twice_a_standard_one(self):
        fast = _line_cost(json.dumps(_line(_usage(speed="fast", input=1_000))).encode(), set())
        standard = _line_cost(json.dumps(_line(_usage(input=1_000))).encode(), set())
        assert fast is not None
        assert standard is not None
        assert fast[0] == pytest.approx(2 * standard[0])

    def test_a_1h_write_line_costs_the_1h_rate(self):
        got = _line_cost(json.dumps(_line(_usage(write_1h=1_000_000))).encode(), set())
        assert got is not None
        assert got[0] == pytest.approx(8.0)

    def test_a_cached_record_dict_prices_the_same(self):
        rec = {"model": "claude-opus-5-5", "ts": TS.timestamp(), "t": [0, 0, 1_000_000, 0],
               "cache_create_1h": 1_000_000, "fast": 1}
        assert _rec_cost_from_tokens(rec) == pytest.approx(16.0)


@pytest.fixture
def loader(tmp_path, monkeypatch):
    """load_all_records over a temp DB and one temp project directory."""
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cache_db, "_conn", None)
    projects = tmp_path / "projects" / "-tmp-live"
    projects.mkdir(parents=True)
    monkeypatch.setattr(ccr, "discover_jsonl_files", lambda: sorted(projects.glob("*.jsonl")))
    monkeypatch.setattr(ccr, "_ensure_cache_valid", lambda _live_paths: None)
    cache_db.init_ccreport_meta(ccr.CACHE_VERSION, "test-hash")
    monkeypatch.setattr(cache_db, "get_project_overrides", list)
    yield projects
    cache_db.get_connection().close()
    cache_db._conn = None


class TestScanAndCache:
    def test_the_parse_carries_both_fields(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(json.dumps(_line(_usage(speed="fast", write_1h=40, write_5m=60))) + "\n")
        (rec,) = scan.parse_jsonl_file(path)
        assert rec.fast is True
        assert (rec.tokens.cache_create, rec.tokens.cache_create_1h) == (100, 40)

    def test_both_survive_the_record_cache(self, loader):
        (loader / "s.jsonl").write_text(
            json.dumps(_line(_usage(speed="fast", write_1h=1_000_000))) + "\n"
        )
        first = ccr.load_all_records()
        _meta, by_file = cache_db.bulk_load_ccreport_cache()
        (cached,) = by_file[str(loader / "s.jsonl")]
        assert (cached["cache_create_1h"], cached["fast"]) == (1_000_000, 1)
        (rec,) = first
        assert rec.cost() == pytest.approx(16.0)

    def test_a_standard_record_keeps_the_cache_dict_small(self, loader):
        (loader / "s.jsonl").write_text(json.dumps(_line(_usage(input=5))) + "\n")
        ccr.load_all_records()
        _meta, by_file = cache_db.bulk_load_ccreport_cache()
        (cached,) = by_file[str(loader / "s.jsonl")]
        assert "fast" not in cached
        assert "cache_create_1h" not in cached


def _record(*, fast: bool, write_1h: int = 0) -> UsageRecord:
    return UsageRecord(
        message_id="m", model="claude-opus-5-5",
        tokens=TokenCounts(cache_create=write_1h, cache_create_1h=write_1h),
        timestamp=TS, session_id="s", project="p", fast=fast,
    )


class TestArchive:
    def test_the_fold_keeps_fast_and_standard_apart(self):
        rows: dict = {}
        ccr._fold_archive_row(rows, _record(fast=True, write_1h=10), "")
        ccr._fold_archive_row(rows, _record(fast=False, write_1h=5), "")
        ccr._fold_archive_row(rows, _record(fast=False, write_1h=7), "")
        assert sorted((key[8], row[9]) for key, row in rows.items()) == [(0, 12), (1, 10)]


_V18_ARCHIVE_DDL = """
CREATE TABLE ccreport_archive (
    day TEXT NOT NULL, oslo_date TEXT NOT NULL, sid TEXT NOT NULL, project TEXT NOT NULL,
    model TEXT NOT NULL, cwd TEXT NOT NULL, repo TEXT NOT NULL, dir_prefix TEXT NOT NULL,
    min_ts REAL NOT NULL, max_ts REAL NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_create INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    cost REAL NOT NULL, n INTEGER NOT NULL,
    timed_n INTEGER NOT NULL DEFAULT 0, latency_s REAL NOT NULL DEFAULT 0,
    rate_output INTEGER NOT NULL DEFAULT 0, rate_s REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, oslo_date, sid, project, model, cwd, repo, dir_prefix)
) WITHOUT ROWID
"""


class TestClientMigration:
    def test_a_v18_db_gains_the_columns_and_keeps_its_archive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        for col in ("cache_create_1h", "fast"):
            conn.execute(f"ALTER TABLE ccreport_records DROP COLUMN {col}")
        conn.execute("DROP TABLE ccreport_archive")
        conn.execute(_V18_ARCHIVE_DDL)
        conn.execute(
            "INSERT INTO ccreport_archive (day, oslo_date, sid, project, model, cwd, repo, "
            "dir_prefix, min_ts, max_ts, input_tokens, output_tokens, cache_create, "
            "cache_read, cost, n, timed_n, latency_s, rate_output, rate_s) VALUES "
            "('2026-01-15', '2026-01-15', 's', 'p', 'claude-opus-5', '', '', '', "
            "1, 2, 3, 4, 5, 6, 7.5, 8, 1, 2.0, 3, 4.0)"
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = 19")
        conn.execute("PRAGMA user_version = 18")
        conn.commit()
        conn.close()
        cache_db._conn = None

        conn = cache_db.get_connection()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ccreport_records)")}
            assert {"cache_create_1h", "fast"} <= cols
            pk = [r[1] for r in conn.execute("PRAGMA table_info(ccreport_archive)") if r[5]]
            assert "fast" in pk
            cache_db.init_ccreport_meta(1, "h")
            (row,) = cache_db.load_ccreport_archive()
            assert row[14:] == (7.5, 8, 1, 2.0, 3, 4.0, 0, 0)
            assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'ccreport_archive_v18'"
                                ).fetchone() is None
        finally:
            conn.close()
            cache_db._conn = None

    def test_the_step_is_idempotent_on_a_fresh_db(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "x.db")
        conn.executescript(cache_db._SCHEMA_SQL)
        cache_db._migrate_fast_and_1h_writes(conn)
        cache_db._migrate_fast_and_1h_writes(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ccreport_archive)")}
        assert {"cache_create_1h", "fast"} <= cols


class TestPush:
    def test_the_payload_carries_both_fields(self):
        rec: dict = dict.fromkeys(cache_db._CCR_COLS)
        rec.update({"model": "claude-opus-5-5", "ts": TS.timestamp(), "project": "p",
                    "input_tokens": 0, "output_tokens": 0, "cache_create": 9,
                    "cache_read": 0, "cache_create_1h": 9, "fast": 1, "source": "claude"})

        class _Timeline:
            def uuid_at(self, _when):
                return "acct"

            def label_at(self, _when):
                return "me"

        payload = push._payload_record(rec, _Timeline(), None)
        assert payload is not None
        assert (payload["cache_create_1h"], payload["fast"]) == (9, True)

    def test_a_record_shape_change_moves_the_policy_digest(self, monkeypatch):
        server = push.ServerConfig(url="https://s", token="t", label="L", machine_id="m")
        before = push.policy_hash(server)
        monkeypatch.setattr(push, "RECORD_SHAPE", "something-else")
        assert push.policy_hash(server) != before


class TestServer:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        from ccreport import exchange

        monkeypatch.setattr(exchange, "_store", exchange._store)
        monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})

    def test_ingest_prices_fast_and_1h_writes(self, tmp_path):
        app = create_app(sf.config(tmp_path))
        client = TestClient(app)
        token = sf.mint_for(app)
        common = {"model": "claude-opus-5-5", "ts": TS.timestamp(), "input_tokens": 0,
                  "output_tokens": 0, "cache_create": 1_000_000, "cache_read": 0}
        records = [
            sf.record(**common, mid="a", dk="a:1", cache_create_1h=1_000_000, fast=True),
            sf.record(**common, mid="b", dk="b:1"),
        ]
        resp = client.post("/v1/ingest", json=sf.batch(records), headers=sf.auth(token))
        assert resp.status_code == 200, resp.text
        by_mid = {r["mid"]: r for r in sf.stored(app, "laptop-1")}
        assert by_mid["a"]["cost"] == pytest.approx(16.0)
        assert (by_mid["a"]["cache_create_1h"], by_mid["a"]["fast"]) == (1_000_000, 1)
        assert by_mid["b"]["cost"] == pytest.approx(5.0)

    def test_a_1h_count_above_the_writes_is_refused(self):
        with pytest.raises(ValueError, match="cache_create_1h"):
            IngestRecord(model="m", ts=1.0, account_uuid="a", cache_create=1, cache_create_1h=2)

    def test_the_server_step_is_idempotent(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "s.db")
        conn.executescript(db._SCHEMA_SQL)
        db._add_fast_and_1h_writes(conn)
        db._add_fast_and_1h_writes(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(server_records)")}
        assert {"cache_create_1h", "fast"} <= cols
