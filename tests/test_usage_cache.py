"""Cache-DB behaviour the statusline render depends on.

Kept out of test_cache_db.py deliberately: these are about what a render sees
when the database is contended or a writer omits a column, not about the schema.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime

import pytest
from _narrow import present

from ccreport import cache_db


def _iso(offset_s: float = 0.0) -> str:
    return (
        datetime.fromtimestamp(
            time.time() + offset_s,
            tz=UTC,
        )
        .astimezone()
        .isoformat()
    )


@pytest.fixture
def blocking_writer():
    """A second connection holding the one WAL writer slot via BEGIN IMMEDIATE."""
    cache_db.get_connection()  # create and bootstrap the file first
    other = sqlite3.connect(str(cache_db.DB_PATH), timeout=5)
    other.execute("BEGIN IMMEDIATE")
    yield other
    other.rollback()
    other.close()


class TestDbTimeout:
    """The wait is configurable so a render can refuse to wait (macsetup-6dp1)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0.25", 0.25),
            ("30", 30.0),
            ("3600", 3600.0),
        ],
    )
    def test_valid_values_are_honoured(self, monkeypatch, raw, expected):
        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", raw)
        assert cache_db._db_timeout() == expected

    @pytest.mark.parametrize("raw", ["", "soon", "0", "-5", "nan", "inf", "1e9"])
    def test_garbage_falls_back_to_the_default(self, monkeypatch, raw):
        """Including values that would silently disable the wait entirely."""
        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", raw)
        assert cache_db._db_timeout() == 10.0

    def test_unset_is_the_default(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CACHE_DB_TIMEOUT", raising=False)
        assert cache_db._db_timeout() == 10.0

    def test_a_short_timeout_gives_up_instead_of_blocking(
        self,
        monkeypatch,
        blocking_writer,
    ):
        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", "0.1")
        cache_db.close_connection()  # reopen so the new timeout applies
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            cache_db.accumulate_cache_stats("sid", 10, 1, 1, 1)
        assert time.monotonic() - started < 5  # not the 10 s default


class TestAccumulateCacheStats:
    """The addition happens in SQL, so interleaved renders cannot lose one."""

    def test_deltas_sum_across_calls(self):
        assert cache_db.accumulate_cache_stats("sid", 100, 1, 2, 3) == (1, 2, 3)
        assert cache_db.accumulate_cache_stats("sid", 200, 10, 20, 30) == (11, 22, 33)

    def test_an_unchanged_change_key_is_a_no_op(self):
        cache_db.accumulate_cache_stats("sid", 100, 1, 2, 3)
        assert cache_db.accumulate_cache_stats("sid", 100, 9, 9, 9) == (1, 2, 3)
        assert cache_db.read_cache_stats("sid") == (100, 1, 2, 3)

    def test_a_shrinking_context_still_accumulates(self):
        """/compact lowers the total; the key is change, not growth."""
        cache_db.accumulate_cache_stats("sid", 400_000, 5, 5, 5)
        assert cache_db.accumulate_cache_stats("sid", 12_000, 1, 1, 1) == (6, 6, 6)

    def test_sessions_are_independent(self):
        cache_db.accumulate_cache_stats("a", 10, 1, 1, 1)
        cache_db.accumulate_cache_stats("b", 10, 5, 5, 5)
        assert cache_db.accumulate_cache_stats("a", 20, 1, 1, 1) == (2, 2, 2)

    def test_a_read_modify_write_race_would_have_lost_this(self):
        """Both callers read the same stored totals before either writes.

        The old code did the addition in Python, so the second write clobbered
        the first with old+delta. Simulated here by computing both deltas up
        front — the point is that neither call is told what the other found.
        """
        cache_db.accumulate_cache_stats("sid", 1, 100, 0, 0)
        cache_db.accumulate_cache_stats("sid", 2, 7, 0, 0)
        cache_db.accumulate_cache_stats("sid", 3, 7, 0, 0)
        assert present(cache_db.read_cache_stats("sid"))[1] == 114


class TestWriteUsageCachePartial:
    """A write dict names what it knows; the rest of the row is not its business."""

    def test_absent_cost_keys_keep_their_values(self):
        """compute_costs failing must not blank the cost segments (macsetup-29bl)."""
        cache_db.write_usage_cache(
            {
                "session_percent": 10,
                "last_updated": _iso(),
                "seven_day_cost": 12.5,
                "thirty_day_cost": 40.0,
            }
        )
        cache_db.write_usage_cache({"session_percent": 20, "last_updated": _iso()})
        row = present(cache_db.read_usage_stale())
        assert row["seven_day_cost"] == 12.5
        assert row["thirty_day_cost"] == 40.0
        assert row["session_percent"] == 20

    def test_an_explicit_none_clears_the_column(self):
        """How a caller says the quota no longer applies, as usage_api does."""
        cache_db.write_usage_cache(
            {
                "scoped_percent": 40,
                "scoped_model": "Fable",
                "last_updated": _iso(),
            }
        )
        cache_db.write_usage_cache({"scoped_percent": None, "scoped_model": None})
        row = present(cache_db.read_usage_stale())
        assert "scoped_percent" not in row
        assert "scoped_model" not in row

    def test_meta_blobs_survive_a_write_that_carries_none(self):
        cache_db.write_usage_cache(
            {
                "session_percent": 1,
                "_meta": {"method": "api"},
                "last_updated": _iso(),
            }
        )
        cache_db.write_usage_cache({"session_percent": 2})
        assert present(cache_db.read_usage_stale())["_meta"] == {"method": "api"}

    def test_an_empty_write_leaves_the_row_alone(self):
        cache_db.write_usage_cache({"session_percent": 7, "last_updated": _iso()})
        cache_db.write_usage_cache({})
        assert present(cache_db.read_usage_stale())["session_percent"] == 7

    def test_extra_spent_is_still_snapshotted(self):
        cache_db.write_usage_cache({"extra_spent": 4.0, "last_updated": _iso()})
        conn = cache_db.get_connection()
        rows = conn.execute("SELECT spent FROM extra_usage_snapshots").fetchall()
        assert [r[0] for r in rows] == [4.0]


class TestUsageReadsAreOneQuery:
    def test_freshness_costs_no_second_select(self):
        cache_db.write_usage_cache({"session_percent": 5, "last_updated": _iso()})
        conn = cache_db.get_connection()
        seen: list[str] = []
        conn.set_trace_callback(seen.append)
        try:
            assert cache_db.read_usage_cache(600) is not None
        finally:
            conn.set_trace_callback(None)
        assert sum("FROM usage WHERE id = 1" in s for s in seen) == 1

    def test_expired_row_reads_as_a_miss(self):
        cache_db.write_usage_cache(
            {
                "session_percent": 5,
                "last_updated": _iso(-4000),
            }
        )
        assert cache_db.read_usage_cache(600) is None
        assert present(cache_db.read_usage_stale())["session_percent"] == 5


class TestFetchBlockedBatching:
    def test_lock_and_backoff_come_back_together(self):
        conn = cache_db.get_connection()
        seen: list[str] = []
        conn.set_trace_callback(seen.append)
        try:
            assert cache_db.is_fetch_blocked() is False
        finally:
            conn.set_trace_callback(None)
        assert sum("FROM meta" in s for s in seen) == 1

    def test_a_held_fetch_lock_blocks(self):
        assert cache_db.try_acquire_fetch_lock() is True
        assert cache_db.is_fetch_blocked() is True
        cache_db.release_fetch_lock()
        assert cache_db.is_fetch_blocked() is False

    def test_recorded_failures_block(self):
        cache_db.record_fetch_failure()
        assert cache_db.is_fetch_blocked() is True
        cache_db.clear_fetch_failures()
        assert cache_db.is_fetch_blocked() is False
