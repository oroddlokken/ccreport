"""Tests for cache_db.py — schema migration, locks, backoff, ccreport rows.

Every test runs against a throwaway DB; nothing here touches the real cache.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from _narrow import present

from ccreport import cache_db, migrations
from ccreport.cache_db import (
    _CCR_COLS,
    RateLimitSample,
    bulk_load_ccreport_cache,
    bulk_save_file_costs,
    check_fetch_backoff,
    clear_fetch_failures,
    invalidate_ccreport,
    load_cost_cache,
    record_account_event,
    record_fetch_failure,
    record_rate_limit_snapshots,
    release_costs_lock,
    release_fetch_lock,
    save_ccreport_file,
    save_ccreport_files,
    try_acquire_costs_lock,
    try_acquire_fetch_lock,
)
from ccreport.pricing import rolling_cost_keys

# A DB as it looked before the added columns existed, to migrate forward.
_PRE_MIGRATION_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE usage (id INTEGER PRIMARY KEY CHECK (id = 1), session_percent INTEGER);
CREATE TABLE ccreport_files (
    path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL);
CREATE TABLE ccreport_records (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL REFERENCES ccreport_files(path) ON DELETE CASCADE,
    mid TEXT, model TEXT NOT NULL, ts REAL NOT NULL, sid TEXT NOT NULL,
    project TEXT NOT NULL, dk TEXT, cost REAL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_create INTEGER NOT NULL, cache_read INTEGER NOT NULL);
"""

# account_events as it looked before the tier columns, holding one capture. The
# schema script's CREATE TABLE IF NOT EXISTS sees the table and leaves it alone,
# so only the ALTERs in _ADDED_COLUMNS can widen this.
_PRE_TIER_ACCOUNT_EVENTS_SQL = """
CREATE TABLE account_events (
    ts                REAL PRIMARY KEY,
    account_uuid      TEXT NOT NULL,
    email             TEXT,
    organization_uuid TEXT,
    organization_name TEXT
) WITHOUT ROWID;
INSERT INTO account_events VALUES
    (100.0, 'uuid-old', 'old@work.example', 'org-old', 'Old AS');
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A connection to an empty DB at a temp path, with snapshots disabled.

    The ccreport schema salt is stamped up front because every ccreport reader
    refuses to return rows without it, and in production ccreport stamps it
    before it writes its first record. Tests about the missing/mismatched salt
    overwrite it themselves.
    """
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cache_db, "_conn", None)
    monkeypatch.setattr(cache_db, "_lock_owners", {})
    conn = cache_db.get_connection()
    cache_db.init_ccreport_meta(1, "test-hash")
    yield conn
    conn.close()
    cache_db._conn = None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _count_commits(conn: sqlite3.Connection):
    """Install a trace callback; the returned fn reports COMMITs since then.

    Both the explicit `COMMIT` statements and the ones `conn.commit()` issues
    reach the trace callback, so this counts whole write transactions.
    """
    return _count_statements(conn, "COMMIT")


def _count_statements(conn: sqlite3.Connection, fragment: str):
    """Install a trace callback counting statements containing *fragment*.

    The callback fires once per row of an executemany, so this measures rows
    written, not calls made.
    """
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    return lambda: sum(1 for s in seen if fragment in s)


class _FailingCommit:
    """A connection whose COMMIT ends the transaction and then fails.

    Reproduces the case that made an unconditional ROLLBACK in the handler
    raise "cannot rollback" over the real error.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("COMMIT"):
            self._conn.execute("ROLLBACK")
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


_INSERT_RECORD = (
    "INSERT INTO ccreport_records "
    "(file_path, mid, model, ts, sid, project, cost, "
    " input_tokens, output_tokens, cache_create, cache_read) "
    "VALUES (?, ?, ?, ?, 's1', 'proj', ?, 0, 0, 0, 0)"
)


# One rollup row in _CCR_ROLLUP_COLS order: the six-part key, the ts span, the
# four token sums, cost, count.
_ROLLUP_ROW = (
    "2026-06-15",
    "2026-06-15",
    "sess-1",
    "proj",
    "claude-sonnet-5",
    "me@work.example",
    100.0,
    900.0,
    10,
    20,
    30,
    40,
    1.5,
    7,
)


def _seed_ccreport(conn, rows, *, path="/tmp/proj/a.jsonl") -> None:
    """Insert (mid, model, ts, cost) rows under one ccreport_files parent."""
    conn.execute(
        "INSERT OR REPLACE INTO ccreport_files (path, mtime_ns, size) VALUES (?, 1, 1)",
        (path,),
    )
    conn.executemany(_INSERT_RECORD, [(path, *r) for r in rows])
    conn.commit()


class TestSchemaColumns:
    def test_new_db_has_every_added_column(self, db):
        for table, col, _type in cache_db._ADDED_COLUMNS:
            assert col in _columns(db, table), f"{table}.{col}"

    def test_pre_migration_db_is_brought_forward(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        # Migrations run here, which runs the sanity check — point it at an
        # empty dir so it can't compare against this machine's real snapshots.
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.executescript(_PRE_MIGRATION_SQL)
        old.commit()
        old.close()

        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        try:
            assert set(rolling_cost_keys()) <= _columns(conn, "usage")
            assert {"scoped_percent", "scoped_model", "scoped_reset"} <= _columns(
                conn, "usage"
            )
            assert {"cwd", "repo"} <= _columns(conn, "ccreport_records")
            # Tables added after a DB was created arrive through the schema
            # script, which only runs because SCHEMA_VERSION moved.
            assert {"account_events", "rate_limit_snapshots"} <= _tables(conn)
        finally:
            conn.close()
            cache_db._conn = None

    def test_a_pre_tier_account_log_keeps_its_rows_and_gains_the_columns(
        self,
        tmp_path,
        monkeypatch,
    ):
        """CREATE TABLE IF NOT EXISTS cannot widen a table that already exists."""
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.executescript(_PRE_MIGRATION_SQL + _PRE_TIER_ACCOUNT_EVENTS_SQL)
        old.commit()
        old.close()

        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        try:
            assert set(cache_db._ACCOUNT_TIER_COLS) <= _columns(conn, "account_events")
            # The capture is intact and its tiers read as unrecorded, which is
            # what they are — nothing was watching them when it was written.
            assert cache_db.load_account_events() == [
                {
                    "ts": 100.0,
                    "account_uuid": "uuid-old",
                    "email": "old@work.example",
                    "organization_uuid": "org-old",
                    "organization_name": "Old AS",
                    "seat_tier": None,
                    "user_rate_limit_tier": None,
                    "organization_rate_limit_tier": None,
                }
            ]
        finally:
            conn.close()
            cache_db._conn = None


class TestSchemaVersionGate:
    """PRAGMA user_version gates the bootstrap every process pays on first touch.

    These lean on the autouse isolate_cache_db fixture for DB_PATH; reopening
    the same file with _conn cleared is what a second process sees.
    """

    @staticmethod
    def _reopen() -> sqlite3.Connection:
        cache_db.close_connection()
        return cache_db.get_connection()

    def test_the_first_open_stamps_the_current_version(self):
        conn = cache_db.get_connection()
        assert cache_db._user_version(conn) == cache_db.SCHEMA_VERSION

    def test_a_stamped_db_skips_the_schema_script(self):
        conn = cache_db.get_connection()
        conn.execute("DROP TABLE exchange_rates")
        conn = self._reopen()
        assert "exchange_rates" not in _tables(conn)

    def test_a_stamped_db_skips_the_alters_and_the_migrations(self, monkeypatch):
        cache_db.get_connection()
        calls: list[str] = []
        monkeypatch.setattr(cache_db, "_add_column", lambda *a: calls.append("alter"))
        monkeypatch.setattr(
            cache_db, "_run_migrations", lambda c: calls.append("migrate")
        )
        self._reopen()
        assert calls == []

    def test_a_stale_version_takes_the_slow_path_once(self, monkeypatch):
        conn = cache_db.get_connection()
        conn.execute("DROP TABLE exchange_rates")
        conn.execute(f"PRAGMA user_version = {cache_db.SCHEMA_VERSION - 1}")
        conn = self._reopen()
        assert "exchange_rates" in _tables(conn), "the bump re-ran the schema script"
        assert cache_db._user_version(conn) == cache_db.SCHEMA_VERSION
        calls: list[str] = []
        monkeypatch.setattr(
            cache_db, "_run_migrations", lambda c: calls.append("migrate")
        )
        self._reopen()
        assert calls == [], "and the DB is back on the fast path"


class TestMigrationChain:
    """The numbered steps that pick up where the frozen bootstrap stops."""

    _LEGACY_FLAGS = (
        "migrated_flat_pricing_2026_03_13",
        "migrated_flat_pricing_ccreport",
        "migrated_flat_pricing_ccreport_variants",
        "migrated_rename_fingerprint",
        "migrated_dedup_keys_pk_order",
    )

    def _seed(self, tmp_path, monkeypatch):
        """A DB that ran every pre-baseline repair, stamped before the baseline."""
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "cache.db"
        seed = sqlite3.connect(path)
        seed.executescript(cache_db._SCHEMA_SQL)
        _seed_ccreport(seed, [("m1", "claude-opus-4-6", 1773424801.0, 5.0)])
        seed.executemany(
            "INSERT INTO meta (key, value) VALUES (?, '1')",
            [(flag,) for flag in self._LEGACY_FLAGS],
        )
        seed.execute("PRAGMA user_version = 0")
        seed.commit()
        seed.close()
        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        return path

    def test_the_flagged_repairs_do_not_run_again(self, tmp_path, monkeypatch):
        """Their flags are the record, and the chain starts above them. Re-running
        the flat-pricing repair would null a cost the DB has already recomputed."""
        self._seed(tmp_path, monkeypatch)
        conn = cache_db.get_connection()
        try:
            assert conn.execute("SELECT cost FROM ccreport_records").fetchone()[0] == 5.0
            assert cache_db._user_version(conn) == cache_db.SCHEMA_VERSION
        finally:
            cache_db.close_connection()

    def test_a_step_reaches_a_database_the_schema_script_cannot_widen(
        self, tmp_path, monkeypatch,
    ):
        """The whole point of the chain: ccreport_records exists already, so only
        a step can add a column to it."""
        self._seed(tmp_path, monkeypatch)

        def add_lane(conn: sqlite3.Connection) -> None:
            conn.execute("ALTER TABLE ccreport_records ADD COLUMN lane TEXT")

        step = migrations.Step(cache_db.MIGRATION_BASELINE + 1, "record lane", add_lane)
        monkeypatch.setattr(cache_db, "MIGRATION_CHAIN", (step,))
        monkeypatch.setattr(cache_db, "SCHEMA_VERSION", step.version)

        conn = cache_db.get_connection()
        try:
            assert "lane" in _columns(conn, "ccreport_records")
            assert cache_db._user_version(conn) == step.version
            assert conn.execute(
                "SELECT name FROM schema_migrations WHERE version = ?", (step.version,),
            ).fetchone()[0] == "record lane"
        finally:
            cache_db.close_connection()


class TestBootstrapFailure:
    """A bootstrap that raises must not leave a usable-looking singleton."""

    def test_a_failure_leaves_no_connection_and_the_next_call_retries(
        self, monkeypatch
    ):
        attempts: list[sqlite3.Connection] = []

        def flaky(conn):
            attempts.append(conn)
            if len(attempts) == 1:
                msg = "migration blew up"
                raise RuntimeError(msg)
            return False

        monkeypatch.setattr(cache_db, "_run_migrations", flaky)
        with pytest.raises(RuntimeError, match="migration blew up"):
            cache_db.get_connection()
        assert cache_db._conn is None
        conn = cache_db.get_connection()
        assert len(attempts) == 2
        assert conn.execute("SELECT 1").fetchone() == (1,)

    def test_the_half_built_connection_is_closed(self, monkeypatch):
        attempts: list[sqlite3.Connection] = []

        def boom(conn):
            attempts.append(conn)
            msg = "migration blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr(cache_db, "_run_migrations", boom)
        with pytest.raises(RuntimeError):
            cache_db.get_connection()
        with pytest.raises(sqlite3.ProgrammingError):
            attempts[0].execute("SELECT 1")

    def test_a_failed_bootstrap_does_not_stamp_the_version(self, monkeypatch):
        def boom(conn):
            msg = "migration blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr(cache_db, "_run_migrations", boom)
        with pytest.raises(RuntimeError):
            cache_db.get_connection()
        # An unstamped DB is what puts the next process back on the slow path.
        stale = sqlite3.connect(cache_db.DB_PATH)
        try:
            assert stale.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            stale.close()


class _FailingRename:
    """A connection whose RENAME COLUMN fails the way a contended cache.db does."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql, *args):
        if "RENAME COLUMN" in sql:
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestRenameFingerprintMigration:
    """Migration 3 must never mark itself done over a table it did not rename."""

    _EARLIER_FLAGS = [
        "migrated_flat_pricing_2026_03_13",
        "migrated_flat_pricing_ccreport",
        "migrated_flat_pricing_ccreport_variants",
    ]
    _LEGACY = (
        "CREATE TABLE session_costs (session_id TEXT PRIMARY KEY, file_size INTEGER)"
    )

    def _db(self, tmp_path, session_costs_sql: str | None) -> sqlite3.Connection:
        """A DB where migration 3 is the only one with work left to do."""
        conn = sqlite3.connect(tmp_path / "m3.db")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if session_costs_sql:
            conn.execute(session_costs_sql)
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, '1')",
            [(k,) for k in self._EARLIER_FLAGS],
        )
        conn.commit()
        return conn

    @staticmethod
    def _flag(conn) -> str | None:
        return cache_db._get_meta(conn, "migrated_rename_fingerprint")

    def test_a_legacy_file_size_column_is_renamed(self, tmp_path):
        conn = self._db(tmp_path, self._LEGACY)
        assert cache_db._run_migrations(conn) is True
        assert "fingerprint" in cache_db._table_columns(conn, "session_costs")
        assert self._flag(conn) == "1"

    def test_an_already_renamed_column_still_sets_the_flag(self, tmp_path):
        conn = self._db(
            tmp_path,
            "CREATE TABLE session_costs (session_id TEXT PRIMARY KEY, fingerprint INTEGER)",
        )
        assert cache_db._run_migrations(conn) is True
        assert self._flag(conn) == "1"

    def test_a_table_with_neither_column_leaves_the_flag_unset(self, tmp_path):
        conn = self._db(
            tmp_path, "CREATE TABLE session_costs (session_id TEXT PRIMARY KEY)"
        )
        with pytest.raises(sqlite3.OperationalError, match="no fingerprint column"):
            cache_db._run_migrations(conn)
        assert self._flag(conn) is None

    def test_a_missing_table_propagates_instead_of_being_swallowed(self, tmp_path):
        conn = self._db(tmp_path, None)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            cache_db._run_migrations(conn)
        assert self._flag(conn) is None

    def test_a_locked_db_is_retried_rather_than_marked_done(self, tmp_path):
        conn = self._db(tmp_path, self._LEGACY)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            cache_db._run_migrations(cast("sqlite3.Connection", _FailingRename(conn)))
        assert self._flag(conn) is None
        assert cache_db._run_migrations(conn) is True
        assert "fingerprint" in cache_db._table_columns(conn, "session_costs")
        assert self._flag(conn) == "1"


def _hold_lock(conn: sqlite3.Connection, prefix: str, *, age: float) -> None:
    """Plant a ``{prefix}_lock`` acquired *age* seconds ago."""
    cache_db._set_meta(conn, f"{prefix}_lock_time", str(time.time() - age))
    conn.commit()


class TestLocks:
    def test_held_lock_blocks_a_second_acquire(self, db):
        assert try_acquire_fetch_lock() is True
        assert try_acquire_fetch_lock() is False
        release_fetch_lock()
        assert try_acquire_fetch_lock() is True

    def test_release_clears_both_meta_keys(self, db):
        try_acquire_fetch_lock()
        release_fetch_lock()
        assert cache_db._get_meta(db, "fetch_lock_time") is None
        assert cache_db._get_meta(db, "fetch_lock_owner") is None

    def test_the_two_locks_are_independent(self, db):
        assert try_acquire_fetch_lock() is True
        assert try_acquire_costs_lock() is True
        release_costs_lock()
        assert cache_db._get_meta(db, "fetch_lock_time") is not None

    @pytest.mark.parametrize(
        ("prefix", "acquire", "stale_timeout"),
        [
            ("fetch", try_acquire_fetch_lock, cache_db.FETCH_LOCK_STALE_TIMEOUT),
            ("costs", try_acquire_costs_lock, cache_db._LOCK_STALE_TIMEOUT),
        ],
    )
    def test_each_lock_is_abandoned_at_its_own_timeout(
        self,
        db,
        prefix,
        acquire,
        stale_timeout,
    ):
        """One TTL cannot serve both: the fetch hold is 80 s, the costs hold 30 s."""
        _hold_lock(db, prefix, age=stale_timeout - 1)
        assert acquire() is False
        _hold_lock(db, prefix, age=stale_timeout + 1)
        assert acquire() is True

    @pytest.mark.parametrize(
        ("prefix", "acquire"),
        [("fetch", try_acquire_fetch_lock), ("costs", try_acquire_costs_lock)],
    )
    def test_corrupt_timestamp_is_stale_on_both_locks(self, db, prefix, acquire):
        cache_db._set_meta(db, f"{prefix}_lock_time", "not-a-number")
        db.commit()
        assert acquire() is True

    def test_release_leaves_a_lock_another_process_took(self, db):
        try_acquire_fetch_lock()
        cache_db._set_meta(db, "fetch_lock_owner", "someone-else")
        db.commit()
        release_fetch_lock()
        assert cache_db._get_meta(db, "fetch_lock_owner") == "someone-else"
        assert cache_db._get_meta(db, "fetch_lock_time") is not None

    def test_a_busy_database_is_a_refusal_not_a_traceback(self, db):
        """Callers run with stderr at /dev/null; raising here loses the refresh."""
        db.execute("PRAGMA busy_timeout = 50")  # don't sit out the real 10 s
        other = sqlite3.connect(str(cache_db.DB_PATH), timeout=0.05)
        other.execute("BEGIN IMMEDIATE")
        try:
            assert try_acquire_fetch_lock() is False
            assert try_acquire_costs_lock() is False
        finally:
            other.execute("ROLLBACK")
            other.close()
        assert try_acquire_fetch_lock() is True

    def test_a_failed_commit_is_a_refusal_not_a_rollback_error(self, db, monkeypatch):
        monkeypatch.setattr(cache_db, "get_connection", lambda: _FailingCommit(db))
        assert try_acquire_fetch_lock() is False


class TestFetchBlockedGate:
    """What the statusline asks before spawning any refresh at all.

    Both spawns end in a _try_acquire_lock, so a lock either of them would fail
    on has to read as blocked here. A leader holding the costs lock across a
    multi-second compute_costs was invisible to this gate, so every slow render
    in that window spawned a detached interpreter that acquired nothing and
    exited.
    """

    def test_a_held_costs_lock_blocks(self, db):
        assert try_acquire_costs_lock() is True
        assert cache_db.is_fetch_blocked() is True
        release_costs_lock()
        assert cache_db.is_fetch_blocked() is False

    def test_a_held_fetch_lock_blocks(self, db):
        assert try_acquire_fetch_lock() is True
        assert cache_db.is_fetch_blocked() is True
        release_fetch_lock()
        assert cache_db.is_fetch_blocked() is False

    @pytest.mark.parametrize(
        ("prefix", "stale_timeout"),
        [
            ("fetch", cache_db.FETCH_LOCK_STALE_TIMEOUT),
            ("costs", cache_db._LOCK_STALE_TIMEOUT),
        ],
    )
    def test_a_lock_the_acquire_would_take_over_does_not_block(
        self,
        db,
        prefix,
        stale_timeout,
    ):
        """Staleness has to mean the same thing here as in _try_acquire_lock."""
        _hold_lock(db, prefix, age=stale_timeout + 1)
        assert cache_db.is_fetch_blocked() is False

    def test_a_fetch_lock_inside_its_longer_budget_still_blocks(self, db):
        """The holder can spend a slow keychain and a retrying API under it.

        Judged by the costs lock's 30 s this reads as abandoned, and the render
        spawns a second fetch that try_acquire_fetch_lock then refuses — the
        duplicate this gate exists to prevent.
        """
        _hold_lock(db, "fetch", age=cache_db._LOCK_STALE_TIMEOUT + 10)
        assert cache_db.is_fetch_blocked() is True
        assert try_acquire_fetch_lock() is False, "the gate and the acquire agree"

    def test_a_costs_lock_keeps_the_short_budget(self, db):
        """Its hold is a JSONL rescan, so the fetch lock's 80 s is not its own."""
        _hold_lock(db, "costs", age=cache_db._LOCK_STALE_TIMEOUT + 10)
        assert cache_db.is_fetch_blocked() is False
        assert try_acquire_costs_lock() is True

    @pytest.mark.parametrize("prefix", ["fetch", "costs"])
    def test_a_corrupt_lock_time_does_not_block(self, db, prefix):
        cache_db._set_meta(db, f"{prefix}_lock_time", "not-a-number")
        db.commit()
        assert cache_db.is_fetch_blocked() is False

    def test_the_second_lock_costs_no_second_round_trip(self, db):
        """A render asks this every time its cached row has expired."""
        selects = _count_statements(db, "FROM meta")
        assert cache_db.is_fetch_blocked() is False
        assert selects() == 1


class TestIsCostsRefreshBlocked:
    """The costs-only spawn's own gate: the costs lock and nothing else.

    A render with a fresh usage row asks this when its project's cost summary
    has expired, and the answer has to agree with try_acquire_costs_lock — which
    skips the API backoff, since a failing API says nothing about whether local
    JSONL can be rescanned.
    """

    def test_a_held_costs_lock_blocks(self, db):
        assert try_acquire_costs_lock() is True
        assert cache_db.is_costs_refresh_blocked() is True
        release_costs_lock()
        assert cache_db.is_costs_refresh_blocked() is False

    def test_a_held_fetch_lock_does_not(self, db):
        """The two locks are separate; a fetch never writes the cost summary."""
        assert try_acquire_fetch_lock() is True
        assert cache_db.is_costs_refresh_blocked() is False
        release_fetch_lock()

    def test_the_api_backoff_does_not(self, db):
        record_fetch_failure()
        assert cache_db.is_costs_refresh_blocked() is False
        assert try_acquire_costs_lock() is True, "the gate and the acquire agree"

    def test_a_lock_the_acquire_would_take_over_does_not_block(self, db):
        _hold_lock(db, "costs", age=cache_db._LOCK_STALE_TIMEOUT + 1)
        assert cache_db.is_costs_refresh_blocked() is False

    def test_a_corrupt_lock_time_does_not_block(self, db):
        cache_db._set_meta(db, "costs_lock_time", "not-a-number")
        db.commit()
        assert cache_db.is_costs_refresh_blocked() is False


class TestBackoff:
    def test_no_failures_means_no_backoff(self, db):
        assert check_fetch_backoff() is False

    def test_a_failure_blocks_fetching_and_the_fetch_lock(self, db):
        record_fetch_failure()
        assert check_fetch_backoff() is True
        assert try_acquire_fetch_lock() is False

    def test_costs_lock_ignores_api_backoff(self, db):
        record_fetch_failure()
        assert try_acquire_costs_lock() is True

    def test_clearing_failures_ends_the_backoff(self, db):
        record_fetch_failure()
        clear_fetch_failures()
        assert check_fetch_backoff() is False
        assert try_acquire_fetch_lock() is True

    def test_corrupt_fail_time_is_not_a_backoff(self, db):
        cache_db._set_meta(db, "fetch_fail_count", "2")
        cache_db._set_meta(db, "fetch_fail_time", "whenever")
        db.commit()
        assert check_fetch_backoff() is False


def _cost_entry(
    mtime: int = 1,
    size: int = 1,
    dks: tuple[str, ...] = (),
    week_model: dict | None = None,
) -> dict:
    return {
        "mtime_ns": mtime,
        "size": size,
        "week_cost": 1.0,
        "month_cost": 2.0,
        "all_time_cost": 3.0,
        "week_model_costs": week_model or {},
        "dedup_keys": list(dks),
    }


class TestBulkSaveFileCosts:
    """Saving one changed file must not churn the rest of the corpus."""

    WEEK = "2026-08-03T00"
    MONTH = "2026-08"

    def _save(self, entries, changed=None) -> None:
        bulk_save_file_costs(entries, self.WEEK, self.MONTH, changed=changed)

    def _dks(self, conn, path: str) -> set[str]:
        return {
            r[0]
            for r in conn.execute(
                "SELECT dk FROM dedup_keys WHERE file_path = ?", (path,)
            )
        }

    def test_a_save_round_trips_through_load(self, db):
        self._save({"/a": _cost_entry(mtime=7, size=8, dks=("k1", "k2"))})
        loaded = load_cost_cache(self.WEEK, self.MONTH)["/a"]
        assert (loaded["mtime_ns"], loaded["size"]) == (7, 8)
        assert sorted(loaded["dedup_keys"]) == ["k1", "k2"]

    def test_an_unchanged_files_dedup_keys_survive(self, db):
        entries = {"/a": _cost_entry(dks=("a1",)), "/b": _cost_entry(dks=("b1",))}
        self._save(entries)
        # A key only the table knows about: it disappears if the save rewrites
        # /b's keys, and stays if the save leaves that row alone.
        db.execute("INSERT INTO dedup_keys (dk, file_path) VALUES ('ghost', '/b')")
        db.commit()

        entries["/a"] = _cost_entry(mtime=2, dks=("a2",))
        self._save(entries, changed={"/a"})

        assert self._dks(db, "/a") == {"a2"}
        assert self._dks(db, "/b") == {"b1", "ghost"}

    def test_a_changed_files_row_is_replaced(self, db):
        self._save({"/a": _cost_entry(mtime=1, size=1, dks=("old",))})
        self._save({"/a": _cost_entry(mtime=9, size=9, dks=("new",))}, changed={"/a"})
        loaded = load_cost_cache(self.WEEK, self.MONTH)["/a"]
        assert (loaded["mtime_ns"], loaded["size"]) == (9, 9)
        assert loaded["dedup_keys"] == ["new"]

    def test_a_departed_path_takes_its_dedup_keys_with_it(self, db):
        self._save({"/a": _cost_entry(dks=("a1",)), "/b": _cost_entry(dks=("b1",))})
        self._save({"/a": _cost_entry(dks=("a1",))}, changed=set())
        assert set(load_cost_cache(self.WEEK, self.MONTH)) == {"/a"}
        assert self._dks(db, "/b") == set()

    def test_no_changed_set_means_rewrite_everything(self, db):
        self._save({"/a": _cost_entry(dks=("a1",))})
        db.execute("INSERT INTO dedup_keys (dk, file_path) VALUES ('ghost', '/a')")
        db.commit()
        self._save({"/a": _cost_entry(dks=("a1",))})
        assert self._dks(db, "/a") == {"a1"}

    def test_one_changed_file_writes_one_row(self, db):
        entries = {f"/f{i}": _cost_entry(dks=(f"k{i}",)) for i in range(50)}
        self._save(entries)
        writes = _count_statements(db, "INTO file_costs")

        entries["/f7"] = _cost_entry(mtime=2, dks=("k7",))
        self._save(entries, changed={"/f7"})
        assert writes() == 1

    def test_a_failed_commit_reports_its_own_error(self, db, monkeypatch):
        monkeypatch.setattr(cache_db, "get_connection", lambda: _FailingCommit(db))
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            self._save({"/a": _cost_entry()})

    def test_the_per_model_week_split_round_trips(self, db):
        split = {"fable": 1.25, "opus": 0.5}
        self._save({"/a": _cost_entry(week_model=split)})
        assert load_cost_cache(self.WEEK, self.MONTH)["/a"]["week_model_costs"] == split

    def test_a_file_with_nothing_in_the_week_stores_no_json(self, db):
        self._save({"/a": _cost_entry()})
        assert (
            db.execute(
                "SELECT week_model_json FROM file_costs WHERE path = '/a'"
            ).fetchone()[0]
            is None
        )
        assert load_cost_cache(self.WEEK, self.MONTH)["/a"]["week_model_costs"] == {}


class TestCostEntrySchemaGate:
    """A stored entry shape older than the code's must re-scan, not read empty.

    An entry predating a field still matches on mtime and size, so nothing else
    would ever invalidate it and the missing field would total as zero.
    """

    WEEK = "2026-08-03T00"
    MONTH = "2026-08"

    def _seed(self, db) -> None:
        bulk_save_file_costs(
            {"/a": _cost_entry(week_model={"fable": 2.0})}, self.WEEK, self.MONTH
        )

    def test_a_matching_schema_keeps_the_rows(self, db):
        self._seed(db)
        assert set(load_cost_cache(self.WEEK, self.MONTH)) == {"/a"}

    def test_an_older_schema_truncates_the_cache(self, db):
        self._seed(db)
        db.execute("UPDATE meta SET value = '1' WHERE key = 'cost_schema'")
        db.commit()
        assert load_cost_cache(self.WEEK, self.MONTH) == {}

    def test_the_truncating_load_stamps_the_current_schema(self, db):
        self._seed(db)
        db.execute("DELETE FROM meta WHERE key = 'cost_schema'")
        db.commit()
        load_cost_cache(self.WEEK, self.MONTH)
        assert cache_db._get_meta(db, "cost_schema") == cache_db._COST_ENTRY_SCHEMA
        # And the next load, having nothing to invalidate, keeps what it stored.
        self._seed(db)
        assert set(load_cost_cache(self.WEEK, self.MONTH)) == {"/a"}


class TestDedupKeyPruning:
    """Keys stop being stored once no bounded window can still count the file.

    The table gains a row per assistant message and nothing ever removed one.
    Costs from an aged-out file survive in file_costs; only its dedup keys go.
    """

    WEEK = "2026-08-03T00"
    MONTH = "2026-08"
    CUTOFF = 1_000_000
    OLD = CUTOFF - 1
    FRESH = CUTOFF + 1

    def _save(self, entries, changed=None, cutoff: int | None = CUTOFF) -> None:
        bulk_save_file_costs(
            entries,
            self.WEEK,
            self.MONTH,
            changed=changed,
            dedup_cutoff_ns=cutoff,
        )

    def test_an_aged_out_files_keys_are_never_written(self, db):
        self._save({"/old": _cost_entry(mtime=self.OLD, dks=("k1",))})
        assert load_cost_cache(self.WEEK, self.MONTH)["/old"]["dedup_keys"] == []

    def test_a_fresh_files_keys_survive(self, db):
        self._save({"/new": _cost_entry(mtime=self.FRESH, dks=("k1", "k2"))})
        loaded = load_cost_cache(self.WEEK, self.MONTH)["/new"]
        assert sorted(loaded["dedup_keys"]) == ["k1", "k2"]

    def test_keys_written_in_window_are_pruned_once_the_file_ages_out(self, db):
        entry = _cost_entry(mtime=self.FRESH, dks=("k1",))
        self._save({"/f": entry})
        # The window moves rather than the file: the same mtime is now behind
        # the cutoff, which is what a day passing does to every stored file.
        self._save({"/f": entry}, changed=set(), cutoff=self.FRESH + 1)
        assert load_cost_cache(self.WEEK, self.MONTH)["/f"]["dedup_keys"] == []

    def test_the_aged_out_files_costs_stay(self, db):
        self._save({"/old": _cost_entry(mtime=self.OLD, dks=("k1",))})
        loaded = load_cost_cache(self.WEEK, self.MONTH)["/old"]
        assert (loaded["week_cost"], loaded["all_time_cost"]) == (1.0, 3.0)

    def test_no_cutoff_keeps_everything(self, db):
        self._save({"/old": _cost_entry(mtime=self.OLD, dks=("k1",))}, cutoff=None)
        assert load_cost_cache(self.WEEK, self.MONTH)["/old"]["dedup_keys"] == ["k1"]

    def test_the_grouped_load_keys_every_file_separately(self, db):
        entries = {
            f"/f{i}": _cost_entry(mtime=self.FRESH, dks=(f"k{i}a", f"k{i}b"))
            for i in range(5)
        }
        self._save(entries)
        loaded = load_cost_cache(self.WEEK, self.MONTH)
        assert {p: sorted(e["dedup_keys"]) for p, e in loaded.items()} == {
            f"/f{i}": [f"k{i}a", f"k{i}b"] for i in range(5)
        }


class TestCcreportRows:
    RECORD = {
        "mid": "m1",
        "model": "claude-opus-5",
        "ts": 1.5,
        "sid": "s1",
        "project": "proj",
        "cwd": "/tmp/proj",
        "repo": "gh/x",
        "dk": "dk1",
        "cost": 0.25,
        "t": [1, 2, 3, 4],
    }
    PATH = "/tmp/proj/a.jsonl"

    def test_a_saved_record_round_trips_field_for_field(self, db):
        """Every column the INSERT names comes back the way it went in.

        The insert list, its placeholders and the mapping back are all derived
        from _CCR_COLS, so this is the test that would catch a column added to
        the table and left out of that tuple.
        """
        save_ccreport_file(self.PATH, 111, 222, [self.RECORD])
        file_meta, by_file = bulk_load_ccreport_cache()
        assert by_file == {self.PATH: [self.RECORD]}
        assert file_meta == {self.PATH: (111, 222)}

    def test_a_batch_save_writes_every_file(self, db):
        other = {**self.RECORD, "mid": "m2"}
        save_ccreport_files(
            [
                (self.PATH, 111, 222, [self.RECORD]),
                ("/tmp/proj/b.jsonl", 333, 444, [other]),
            ]
        )
        file_meta, by_file = bulk_load_ccreport_cache()
        assert file_meta == {self.PATH: (111, 222), "/tmp/proj/b.jsonl": (333, 444)}
        assert by_file == {self.PATH: [self.RECORD], "/tmp/proj/b.jsonl": [other]}

    def test_a_batch_save_replaces_a_files_records(self, db):
        save_ccreport_files([(self.PATH, 111, 222, [self.RECORD])])
        replacement = {**self.RECORD, "mid": "m9"}
        save_ccreport_files([(self.PATH, 555, 666, [replacement])])
        file_meta, by_file = bulk_load_ccreport_cache()
        assert file_meta == {self.PATH: (555, 666)}
        assert by_file == {self.PATH: [replacement]}

    def test_an_empty_batch_writes_nothing(self, db):
        save_ccreport_files([])
        assert bulk_load_ccreport_cache() == ({}, {})

    def test_a_batch_is_one_transaction(self, db):
        commits = _count_commits(db)
        save_ccreport_files(
            [(f"/tmp/proj/{i}.jsonl", i, i, [self.RECORD]) for i in range(20)]
        )
        assert commits() == 1

    def test_column_constant_matches_the_table(self, db):
        assert set(_CCR_COLS) <= _columns(db, "ccreport_records")

    def test_every_record_column_is_written_and_read_back(self, db):
        """No column of _CCR_COLS is silently NULLed on the way through.

        A column present in the SELECT but missing from the INSERT reads back
        as None, which a round-trip of one hand-written record only catches if
        that record happens to carry a truthy value for it — so assert the
        stored row directly.
        """
        save_ccreport_file(self.PATH, 111, 222, [self.RECORD])
        stored = db.execute(
            f"SELECT {', '.join(_CCR_COLS)} FROM ccreport_records"
        ).fetchone()
        assert all(v is not None for v in stored)

    def test_the_record_dict_matches_the_column_tuple(self, db):
        """_group_by_file indexes the row by position; this is what names it.

        Every cached read passes the whole corpus through _group_by_file, and
        walking _CCR_COLS by name cost a slice, a zipped dict, a comprehension
        and a list per row, so the record dict is one literal indexed straight
        off the row. Nothing at runtime ties those indices back to the tuple:
        a column added to _CCR_COLS and not to the literal lands here.
        """
        row = ("/p/a.jsonl", *range(len(_CCR_COLS)))
        record = cache_db._group_by_file([row])["/p/a.jsonl"][0]
        expected: dict[str, int | list[int]] = {
            name: i for i, name in enumerate(cache_db._CCR_FIELD_COLS)
        }
        expected["t"] = [
            len(cache_db._CCR_FIELD_COLS) + i
            for i in range(len(cache_db._CCR_TOKEN_COLS))
        ]
        assert record == expected


class TestScopedCcreportLoaders:
    """The scoped loaders must equal a full load filtered in Python.

    That equality is the whole safety argument for scoping the read in SQL at
    all: the statusline used to load every row in ccreport_records per render
    and throw away everything outside one project.
    """

    PROJ = "/p/-tmp-proj/"
    # Shares PROJ as a string prefix but is a different directory, which is
    # exactly what the half-open range has to exclude and a bare `>=` would not.
    SIBLING = "/p/-tmp-proj-other/"
    OTHER = "/p/-tmp-elsewhere/"

    @pytest.fixture
    def seeded(self, db):
        def rec(**kw):
            return {
                "mid": "m",
                "model": "claude-opus-5",
                "ts": 1.5,
                "sid": "s1",
                "project": "proj",
                "cwd": "/tmp/proj",
                "repo": "gh/x",
                "dk": None,
                "cost": 0.25,
                "t": [1, 2, 3, 4],
                **kw,
            }

        save_ccreport_files(
            [
                (self.PROJ + "a.jsonl", 1, 1, [rec(mid="a1"), rec(mid="a2", sid="s2")]),
                (self.PROJ + "sub/b.jsonl", 2, 2, [rec(mid="b1", sid="s2")]),
                (self.SIBLING + "c.jsonl", 3, 3, [rec(mid="c1")]),
                (self.OTHER + "d.jsonl", 4, 4, [rec(mid="d1", sid="s2")]),
            ]
        )
        return db

    def test_a_prefix_load_equals_a_full_load_filtered_by_that_prefix(self, seeded):
        _, everything = bulk_load_ccreport_cache()
        expected = {
            path: recs
            for path, recs in everything.items()
            if path.startswith(self.PROJ)
        }
        assert cache_db.load_ccreport_records_under(self.PROJ) == expected

    def test_a_sibling_sharing_the_prefix_string_is_excluded(self, seeded):
        loaded = cache_db.load_ccreport_records_under(self.PROJ)
        assert set(loaded) == {self.PROJ + "a.jsonl", self.PROJ + "sub/b.jsonl"}
        assert self.SIBLING + "c.jsonl" not in loaded

    def test_a_session_load_equals_a_full_load_filtered_by_sid(self, seeded):
        _, everything = bulk_load_ccreport_cache()
        expected = {}
        for path, recs in everything.items():
            hits = [r for r in recs if r["sid"] == "s2"]
            if hits:
                expected[path] = hits
        assert cache_db.load_ccreport_records_for_session("s2") == expected

    def test_a_session_load_reaches_across_projects(self, seeded):
        loaded = cache_db.load_ccreport_records_for_session("s2")
        # Scoping to one project is the caller's job, so the loader itself must
        # not do it — compute_session_cost applies the prefixes afterwards.
        assert set(loaded) == {
            self.PROJ + "a.jsonl",
            self.PROJ + "sub/b.jsonl",
            self.OTHER + "d.jsonl",
        }

    def test_an_unmatched_prefix_loads_nothing(self, seeded):
        assert cache_db.load_ccreport_records_under("/p/-tmp-nothing/") == {}

    def test_a_prefix_meta_load_equals_a_full_load_filtered_by_that_prefix(
        self,
        seeded,
    ):
        everything, _ = bulk_load_ccreport_cache()
        expected = {
            path: fp for path, fp in everything.items() if path.startswith(self.PROJ)
        }
        assert cache_db.load_ccreport_file_meta_under(self.PROJ) == expected
        # The fingerprints themselves, not just the paths: a render compares
        # them against a stat() and would serve stale records if they drifted.
        assert expected == {
            self.PROJ + "a.jsonl": (1, 1),
            self.PROJ + "sub/b.jsonl": (2, 2),
        }

    def test_a_sibling_sharing_the_prefix_string_has_no_fingerprint_either(
        self,
        seeded,
    ):
        assert self.SIBLING + "c.jsonl" not in cache_db.load_ccreport_file_meta_under(
            self.PROJ
        )

    def test_an_unmatched_prefix_has_no_fingerprints(self, seeded):
        assert cache_db.load_ccreport_file_meta_under("/p/-tmp-nothing/") == {}

    @pytest.mark.parametrize(
        ("prefix", "expected"),
        [("/a/b/", ("/a/b/", "/a/b0")), ("xy", ("xy", "xz"))],
    )
    def test_the_upper_bound_steps_the_last_character(self, prefix, expected):
        assert cache_db.prefix_range(prefix) == expected

    def test_an_empty_prefix_is_refused(self):
        # Silently scanning the whole table is the bug this exists to prevent.
        with pytest.raises(ValueError):
            cache_db.prefix_range("")


class TestCcreportSaltGate:
    """A reader that cannot vouch for the row format degrades; it never repairs.

    Invalidation belongs to ccreport's _ensure_cache_valid, which re-parses
    what it clears. A statusline render doing it would destroy the orphan
    records no re-parse can rebuild.
    """

    PATH = "/p/-tmp-proj/a.jsonl"
    RECORD = {
        "mid": "m1",
        "model": "claude-opus-5",
        "ts": 1.5,
        "sid": "s1",
        "project": "proj",
        "cwd": "/tmp/proj",
        "repo": "gh/x",
        "dk": "dk1",
        "cost": 0.25,
        "t": [1, 2, 3, 4],
    }

    @pytest.fixture
    def mismatched(self, db):
        save_ccreport_file(self.PATH, 111, 222, [self.RECORD])
        cache_db.save_ccreport_rollups([_ROLLUP_ROW], "fp-1")
        # After the file, which clears the scopes as any record write does.
        cache_db.save_project_scope("/tmp/proj", "proj", ["/p/-tmp-proj/"])
        cache_db._set_meta(db, "ccreport_schema_salt", "not-the-salt")
        db.commit()
        return db

    @pytest.mark.parametrize(
        "read",
        [
            pytest.param(bulk_load_ccreport_cache, id="bulk"),
            pytest.param(
                lambda: cache_db.load_ccreport_records_under("/p/-tmp-proj/"),
                id="prefix",
            ),
            pytest.param(
                lambda: cache_db.load_ccreport_records_for_session("s1"), id="session"
            ),
            pytest.param(
                lambda: cache_db.load_ccreport_file_meta_under("/p/-tmp-proj/"),
                id="prefix-meta",
            ),
            pytest.param(
                lambda: cache_db.load_project_scope("/tmp/proj"), id="project-scope"
            ),
            pytest.param(cache_db.load_ccreport_file_meta, id="all-meta"),
            pytest.param(lambda: cache_db.load_ccreport_records_since(0.0), id="since"),
            pytest.param(
                lambda: cache_db.load_ccreport_records_in_range(0.0, 9e9), id="ts-range"
            ),
            pytest.param(
                lambda: cache_db.load_ccreport_file_meta_before(9e9), id="meta-before"
            ),
            pytest.param(cache_db.load_ccreport_rollups, id="rollups"),
            pytest.param(
                cache_db.read_ccreport_rollup_fingerprint, id="rollup-fp"
            ),
        ],
    )
    def test_a_mismatched_salt_reads_as_empty(self, mismatched, read):
        # A tuple is a result in its own right (bulk returns a pair, a cached
        # scope a name and its prefixes), so emptiness is asked of its parts.
        result = read()
        assert not any(result) if isinstance(result, tuple) else not result

    def test_a_missing_salt_reads_as_empty(self, db):
        save_ccreport_file(self.PATH, 111, 222, [self.RECORD])
        db.execute("DELETE FROM meta WHERE key = 'ccreport_schema_salt'")
        db.commit()
        assert cache_db.load_ccreport_records_under("/p/-tmp-proj/") == {}

    def test_the_degraded_read_deletes_nothing(self, mismatched):
        bulk_load_ccreport_cache()
        cache_db.load_ccreport_records_under("/p/-tmp-proj/")
        cache_db.load_ccreport_records_for_session("s1")
        cache_db.load_project_scope("/tmp/proj")
        assert (
            mismatched.execute("SELECT COUNT(*) FROM ccreport_records").fetchone()[0]
            == 1
        )
        assert (
            mismatched.execute("SELECT COUNT(*) FROM project_scopes").fetchone()[0] == 1
        )
        assert (
            mismatched.execute("SELECT COUNT(*) FROM ccreport_files").fetchone()[0] == 1
        )
        # The cost above all: an orphan's came from a JSONL that is gone.
        assert (
            mismatched.execute("SELECT cost FROM ccreport_records").fetchone()[0]
            == 0.25
        )

    def test_restamping_the_salt_brings_the_rows_back(self, mismatched):
        cache_db.init_ccreport_meta(1, "test-hash")
        assert cache_db.load_ccreport_records_under("/p/-tmp-proj/") == {
            self.PATH: [self.RECORD]
        }


class TestInvalidateCcreport:
    """An orphan's cost came from a JSONL that's gone; it must survive."""

    LIVE = "/tmp/proj/live.jsonl"
    GONE = "/tmp/proj/purged.jsonl"
    RECORD = {
        "mid": "m1",
        "model": "claude-opus-5",
        "ts": 1.5,
        "sid": "s1",
        "project": "proj",
        "cwd": "/tmp/proj",
        "repo": "gh/x",
        "dk": "dk1",
        "cost": 0.25,
        "t": [1, 2, 3, 4],
    }

    @pytest.fixture
    def two_files(self, db):
        save_ccreport_file(self.LIVE, 111, 222, [dict(self.RECORD)])
        save_ccreport_file(self.GONE, 333, 444, [dict(self.RECORD)])
        return db

    @staticmethod
    def _cost(conn, path):
        """The one record's cached cost, read past the salt gate.

        invalidate_ccreport drops the salt, and every loader returns nothing
        without it, so these assertions go to the table directly.
        """
        return conn.execute(
            "SELECT cost FROM ccreport_records WHERE file_path = ?", (path,)
        ).fetchone()[0]

    def test_only_live_records_lose_their_cached_cost(self, two_files):
        invalidate_ccreport({self.LIVE})
        assert self._cost(two_files, self.LIVE) is None
        assert self._cost(two_files, self.GONE) == 0.25

    def test_only_live_files_lose_their_fingerprint(self, two_files):
        invalidate_ccreport({self.LIVE})
        # invalidate_ccreport drops the salt; ccreport's _ensure_cache_valid
        # restamps it in the next breath, and the readers below need it back.
        cache_db.init_ccreport_meta(1, "test-hash")
        file_meta, _ = bulk_load_ccreport_cache()
        assert file_meta[self.LIVE] == (0, 0)
        assert file_meta[self.GONE] == (333, 444)

    def test_with_no_live_files_it_only_clears_the_meta_keys(self, two_files):
        cache_db.init_ccreport_meta(9, "hash")
        invalidate_ccreport(set())
        assert cache_db.check_ccreport_valid(9, "hash") is False
        assert self._cost(two_files, self.GONE) == 0.25
        assert self._cost(two_files, self.LIVE) == 0.25

    @pytest.fixture
    def many_files(self, db):
        """Two full chunks and one row over, so the chunking is visible."""
        paths = [
            f"/tmp/proj/{i:04d}.jsonl"
            for i in range(cache_db._INVALIDATE_CHUNK * 2 + 1)
        ]
        save_ccreport_files([(p, 1, 1, [dict(self.RECORD)]) for p in paths])
        return set(paths)

    def test_the_updates_are_chunked_into_bounded_transactions(self, db, many_files):
        """A render gives up on the write lock in 0.25 s, the refresh in 10 s.

        One transaction across the whole ~98k-row UPDATE is what they used to
        wait behind.
        """
        commits = _count_commits(db)
        invalidate_ccreport(many_files)
        # The meta keys go in their own transaction, then one per path chunk.
        assert commits() == 1 + 3

    def test_chunking_still_invalidates_every_file(self, db, many_files):
        invalidate_ccreport(many_files)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM ccreport_records WHERE cost IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM ccreport_files WHERE mtime_ns != 0 OR size != 0"
            ).fetchone()[0]
            == 0
        )

    def test_the_meta_keys_go_first(self, db, many_files):
        """A crash mid-chunk must leave a cache that re-invalidates.

        Clearing them last would leave check_ccreport_valid saying yes over a
        corpus only half re-parsed.
        """
        cache_db.init_ccreport_meta(9, "hash")
        order: list[str] = []
        db.set_trace_callback(order.append)
        invalidate_ccreport(many_files)
        db.set_trace_callback(None)
        first_update = next(i for i, s in enumerate(order) if "UPDATE" in s)
        salt_delete = next(
            i for i, s in enumerate(order) if "ccreport_schema_salt" in s
        )
        assert salt_delete < first_update


class TestCcreportRollups:
    """Per-day aggregates for the days past ccreport's cutoff.

    The rows are derived data — every one is rebuildable from
    ccreport_records — so what these guard is not the rows but the pairing
    between them and the fingerprint that vouches for them.
    """

    def test_a_row_round_trips_in_column_order(self, db):
        cache_db.save_ccreport_rollups([_ROLLUP_ROW], "fp-1")
        assert cache_db.load_ccreport_rollups() == [_ROLLUP_ROW]
        assert cache_db.read_ccreport_rollup_fingerprint() == "fp-1"

    def test_a_save_replaces_the_whole_table(self, db):
        """The cutoff moves daily, so a rebuild is a new set, not a delta."""
        cache_db.save_ccreport_rollups([_ROLLUP_ROW], "fp-1")
        other = ("2026-06-16", *_ROLLUP_ROW[1:])
        cache_db.save_ccreport_rollups([other], "fp-2")
        assert cache_db.load_ccreport_rollups() == [other]
        assert cache_db.read_ccreport_rollup_fingerprint() == "fp-2"

    def test_an_empty_build_still_stamps_its_fingerprint(self, db):
        """A corpus with nothing old enough must not rebuild on every run."""
        cache_db.save_ccreport_rollups([], "fp-empty")
        assert cache_db.load_ccreport_rollups() == []
        assert cache_db.read_ccreport_rollup_fingerprint() == "fp-empty"

    def test_a_failed_commit_leaves_neither_the_rows_nor_the_fingerprint(
        self,
        db,
        monkeypatch,
    ):
        """A fingerprint outliving its rows is the one failure that reads as valid."""
        cache_db.save_ccreport_rollups([_ROLLUP_ROW], "fp-1")
        monkeypatch.setattr(cache_db, "get_connection", lambda: _FailingCommit(db))
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            cache_db.save_ccreport_rollups([("2026-06-16", *_ROLLUP_ROW[1:])], "fp-2")
        # Back to the real connection by patching over the wrapper, not by
        # undoing: an undo would also drop the fixture's DB_PATH redirect and
        # read the machine's actual cache.
        monkeypatch.setattr(cache_db, "get_connection", lambda: db)
        assert cache_db.load_ccreport_rollups() == [_ROLLUP_ROW]
        assert cache_db.read_ccreport_rollup_fingerprint() == "fp-1"

    def test_invalidating_the_record_cache_drops_the_fingerprint(self, db):
        """It NULLs the very costs the rollups froze."""
        save_ccreport_file("/tmp/proj/a.jsonl", 111, 222, [])
        cache_db.save_ccreport_rollups([_ROLLUP_ROW], "fp-1")
        invalidate_ccreport({"/tmp/proj/a.jsonl"})
        cache_db.init_ccreport_meta(1, "test-hash")  # as _ensure_cache_valid does
        assert cache_db.read_ccreport_rollup_fingerprint() is None


class TestCcreportRollupReadPath:
    """The two queries that let a run skip the pre-cutoff records."""

    LIVE = "/tmp/proj/live.jsonl"
    GONE = "/tmp/proj/purged.jsonl"

    def _rec(self, mid: str, ts: float) -> dict:
        return {
            "mid": mid,
            "model": "claude-opus-5",
            "ts": ts,
            "sid": "s1",
            "project": "proj",
            "cwd": "/tmp/proj",
            "repo": "gh/x",
            "dk": mid,
            "cost": 0.25,
            "t": [1, 2, 3, 4],
        }

    @pytest.fixture
    def corpus(self, db):
        save_ccreport_file(
            self.LIVE, 111, 222, [self._rec("old", 100.0), self._rec("new", 900.0)]
        )
        save_ccreport_file(self.GONE, 333, 444, [self._rec("orphan-new", 950.0)])
        return db

    def test_only_records_at_or_after_the_cutoff_come_back(self, corpus):
        by_file = cache_db.load_ccreport_records_since(900.0)
        assert [r["mid"] for r in by_file[self.LIVE]] == ["new"]

    def test_one_query_covers_live_and_purged_files_alike(self, corpus):
        """No join through ccreport_files, so an orphan is not a second query."""
        by_file = cache_db.load_ccreport_records_since(500.0)
        assert set(by_file) == {self.LIVE, self.GONE}

    def test_the_row_order_is_insert_order(self, corpus):
        """Dedup keeps the first occurrence, so the order decides the winner."""
        by_file = cache_db.load_ccreport_records_since(0.0)
        assert [r["mid"] for r in by_file[self.LIVE]] == ["old", "new"]

    def test_only_files_holding_an_older_record_are_fingerprinted(self, corpus):
        assert cache_db.load_ccreport_file_meta_before(900.0) == [
            (self.LIVE, 111, 222),
        ]
        assert cache_db.load_ccreport_file_meta_before(0.0) == []

    def test_the_meta_carries_the_same_fingerprint_the_record_cache_uses(self, corpus):
        assert cache_db.load_ccreport_file_meta() == {
            self.LIVE: (111, 222),
            self.GONE: (333, 444),
        }


class TestCcreportRecordsInRange:
    """The window a filtered report reads, instead of the whole corpus."""

    LIVE = "/tmp/proj/live.jsonl"
    GONE = "/tmp/proj/purged.jsonl"

    def _rec(self, mid: str, ts: float) -> dict:
        return {
            "mid": mid,
            "model": "claude-opus-5",
            "ts": ts,
            "sid": "s1",
            "project": "proj",
            "cwd": "/tmp/proj",
            "repo": "gh/x",
            "dk": mid,
            "cost": 0.25,
            "t": [1, 2, 3, 4],
        }

    @pytest.fixture
    def corpus(self, db):
        save_ccreport_file(
            self.LIVE,
            111,
            222,
            [
                self._rec("a", 100.0),
                self._rec("b", 200.0),
                self._rec("c", 300.0),
            ],
        )
        save_ccreport_file(self.GONE, 333, 444, [self._rec("orphan", 250.0)])
        return db

    def _mids(self, since, until) -> list[str]:
        by_file = cache_db.load_ccreport_records_in_range(since, until)
        return [r["mid"] for recs in by_file.values() for r in recs]

    def test_both_bounds_narrow_the_window(self, corpus):
        assert self._mids(200.0, 250.0) == ["b", "orphan"]

    def test_the_bounds_are_inclusive_as_keep_is(self, corpus):
        """_keep drops on `ts < since` and `ts > until`, so the ends are kept."""
        assert self._mids(100.0, 100.0) == ["a"]
        assert self._mids(300.0, 300.0) == ["c"]

    def test_either_bound_may_be_open(self, corpus):
        assert self._mids(260.0, None) == ["c"]
        assert self._mids(None, 150.0) == ["a"]

    def test_no_bounds_is_every_row(self, corpus):
        assert self._mids(None, None) == ["a", "b", "c", "orphan"]

    def test_one_query_covers_live_and_purged_files_alike(self, corpus):
        assert set(cache_db.load_ccreport_records_in_range(None, None)) == {
            self.LIVE,
            self.GONE,
        }

    def test_the_row_order_is_insert_order(self, corpus):
        """Dedup keeps the first occurrence, so the order decides the winner."""
        by_file = cache_db.load_ccreport_records_in_range(None, None)
        assert [r["mid"] for r in by_file[self.LIVE]] == ["a", "b", "c"]

    def test_the_window_agrees_with_the_one_sided_loader(self, corpus):
        assert cache_db.load_ccreport_records_in_range(200.0, None) == (
            cache_db.load_ccreport_records_since(200.0)
        )


class TestProjectScopeCache:
    """A stored scope is trusted without a fingerprint, so its inputs must clear it.

    The row is a pure function of project_overrides and the cached record
    identities; nothing on it says which version of those it came from. What
    keeps it honest is that every writer of either empties the table.
    """

    CWD = "/tmp/proj"
    PREFIXES = ["/p/-tmp-proj/", "/p/-tmp-other/"]
    RECORD = {
        "mid": "m1",
        "model": "claude-opus-5",
        "ts": 1.5,
        "sid": "s1",
        "project": "proj",
        "cwd": CWD,
        "repo": "gh/x",
        "dk": "dk1",
        "cost": 0.25,
        "t": [1, 2, 3, 4],
    }

    @pytest.fixture
    def cached(self, db):
        cache_db.save_project_scope(self.CWD, "proj", self.PREFIXES)
        return db

    def test_a_saved_scope_comes_back_as_it_went_in(self, cached):
        assert cache_db.load_project_scope(self.CWD) == ("proj", self.PREFIXES)

    def test_an_uncached_cwd_reads_as_a_miss(self, cached):
        assert cache_db.load_project_scope("/tmp/elsewhere") is None

    def test_a_second_save_replaces_the_first(self, cached):
        cache_db.save_project_scope(self.CWD, "renamed", ["/p/-tmp-proj/"])
        assert cache_db.load_project_scope(self.CWD) == ("renamed", ["/p/-tmp-proj/"])

    def test_adding_a_rule_clears_the_scopes(self, cached):
        cache_db.add_project_override("name", "other", "proj")
        assert cache_db.load_project_scope(self.CWD) is None

    def test_deleting_a_rule_clears_the_scopes(self, cached):
        cache_db.add_project_override("name", "other", "proj")
        cache_db.save_project_scope(self.CWD, "proj", self.PREFIXES)
        assert cache_db.delete_project_override("other") == 1
        assert cache_db.load_project_scope(self.CWD) is None

    def test_caching_a_record_clears_the_scopes(self, cached):
        # A newly parsed file can be the one that joins another directory to
        # this project, so its identity has to reach the next render.
        save_ccreport_files([("/p/-tmp-other/a.jsonl", 1, 1, [dict(self.RECORD)])])
        assert cache_db.load_project_scope(self.CWD) is None

    def test_invalidating_the_ccreport_cache_clears_the_scopes(self, cached):
        invalidate_ccreport({"/p/-tmp-proj/a.jsonl"})
        cache_db.init_ccreport_meta(1, "test-hash")
        assert cache_db.load_project_scope(self.CWD) is None


class TestProjectScopeSurvivesAnOrdinarySave:
    """A save that moves no identity must leave the scopes alone.

    Truncating the table on every batch made an ordinary ccreport run cost every
    open session a full scope derivation — a quarter of a render — on its next
    slow render, for records that said exactly what the cached ones said.
    """

    CWD = TestProjectScopeCache.CWD
    PREFIXES = TestProjectScopeCache.PREFIXES
    RECORD = TestProjectScopeCache.RECORD
    DIR = "/p/-tmp-proj/"

    @pytest.fixture
    def seeded(self, db):
        """One cached log, and a scope cached after it — the steady state."""
        save_ccreport_files([(self.DIR + "b.jsonl", 1, 1, [dict(self.RECORD)])])
        cache_db.save_project_scope(self.CWD, "proj", self.PREFIXES)
        return db

    def _scope(self):
        return cache_db.load_project_scope(self.CWD)

    def test_reparsing_a_log_that_grew_keeps_the_scopes(self, seeded):
        save_ccreport_files(
            [
                (self.DIR + "b.jsonl", 2, 2, [dict(self.RECORD), dict(self.RECORD)]),
            ]
        )
        assert self._scope() == ("proj", self.PREFIXES)

    def test_a_new_log_behind_one_saying_the_same_thing_keeps_the_scopes(self, seeded):
        """Its directory already resolves however it resolves."""
        save_ccreport_files([(self.DIR + "c.jsonl", 1, 1, [dict(self.RECORD)])])
        assert self._scope() == ("proj", self.PREFIXES)

    def test_a_batch_of_unchanged_logs_keeps_the_scopes(self, seeded):
        save_ccreport_files(
            [
                (self.DIR + "b.jsonl", 2, 2, [dict(self.RECORD)]),
                (self.DIR + "c.jsonl", 1, 1, [dict(self.RECORD)]),
                (self.DIR + "d.jsonl", 1, 1, [dict(self.RECORD)]),
            ]
        )
        assert self._scope() == ("proj", self.PREFIXES)

    @pytest.mark.parametrize(
        "moved",
        [
            {"repo": "gh/renamed"},
            {"cwd": "/tmp/elsewhere"},
            {"project": "other"},
        ],
    )
    def test_a_changed_identity_clears_the_scopes(self, seeded, moved):
        """Every signal resolve() reads, one at a time."""
        save_ccreport_files(
            [
                (self.DIR + "b.jsonl", 2, 2, [{**self.RECORD, **moved}]),
            ]
        )
        assert self._scope() is None

    def test_a_log_in_a_directory_nothing_is_cached_for_clears_the_scopes(self, seeded):
        """It can be the directory that joins another project to this one."""
        save_ccreport_files([("/p/-tmp-new/a.jsonl", 1, 1, [dict(self.RECORD)])])
        assert self._scope() is None

    def test_a_log_sorting_before_every_sibling_clears_the_scopes(self, seeded):
        """The scope's name is read from the first log in path order."""
        save_ccreport_files([(self.DIR + "a.jsonl", 1, 1, [dict(self.RECORD)])])
        assert self._scope() is None

    def test_a_log_that_now_parses_to_nothing_clears_the_scopes(self, seeded):
        """It takes an identity away; only a rederivation says what is left."""
        save_ccreport_files([(self.DIR + "b.jsonl", 2, 2, [])])
        assert self._scope() is None

    def test_an_unchanged_save_still_writes_its_records(self, seeded):
        """The check reads the pre-write state; it must not skip the write."""
        grown = [dict(self.RECORD), {**self.RECORD, "mid": "m2"}]
        save_ccreport_files([(self.DIR + "b.jsonl", 2, 2, grown)])
        file_meta, by_file = bulk_load_ccreport_cache()
        assert file_meta == {self.DIR + "b.jsonl": (2, 2)}
        assert by_file == {self.DIR + "b.jsonl": grown}


class TestFlatPricingVariantsMigration:
    """Migration 2 matched models by equality; find_pricing matches substrings."""

    CUTOFF = 1773424800.0  # 2026-03-13T18:00 UTC
    ROWS = [
        # Columns: mid, model, ts, cost.
        ("bracketed", "claude-opus-4-6[1m]", CUTOFF + 1, 5.0),
        ("bracketed-old", "claude-opus-4-6[1m]", CUTOFF - 1, 5.0),
        ("dated", "claude-sonnet-4-6-20260210", CUTOFF + 1, 5.0),
        ("exact", "claude-opus-4-6", CUTOFF + 1, 5.0),
        ("unrelated", "claude-opus-5", CUTOFF + 1, 5.0),
    ]

    @pytest.fixture
    def migrated(self, tmp_path, monkeypatch):
        """A DB carrying the pre-existing migration flags, then reopened."""
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "cache.db"
        seed = sqlite3.connect(path)
        seed.executescript(cache_db._SCHEMA_SQL)
        _seed_ccreport(seed, self.ROWS)
        seed.execute(
            "INSERT INTO session_costs (session_id, fingerprint, cost) VALUES ('s1', 1, 3.0)"
        )
        seed.execute("INSERT INTO meta (key, value) VALUES ('cost_summary', '{}')")
        # Every real DB already ran migrations 1-3, so only the new one fires.
        seed.executemany(
            "INSERT INTO meta (key, value) VALUES (?, '1')",
            [
                ("migrated_flat_pricing_2026_03_13",),
                ("migrated_flat_pricing_ccreport",),
                ("migrated_rename_fingerprint",),
            ],
        )
        seed.commit()
        seed.close()

        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        yield conn
        cache_db.close_connection()

    @staticmethod
    def _costs(conn) -> dict[str, float | None]:
        return dict(conn.execute("SELECT mid, cost FROM ccreport_records"))

    def test_missed_variants_past_the_cutoff_are_nulled(self, migrated):
        costs = self._costs(migrated)
        assert costs["bracketed"] is None
        assert costs["dated"] is None
        assert costs["exact"] is None

    def test_pre_cutoff_and_unrelated_models_keep_their_cost(self, migrated):
        costs = self._costs(migrated)
        assert costs["bracketed-old"] == 5.0
        assert costs["unrelated"] == 5.0

    def test_derived_cost_caches_are_dropped(self, migrated):
        assert migrated.execute("SELECT COUNT(*) FROM session_costs").fetchone()[0] == 0
        assert cache_db._get_meta(migrated, "cost_summary") is None

    def test_the_flag_stops_it_running_twice(self, migrated):
        assert (
            cache_db._get_meta(migrated, "migrated_flat_pricing_ccreport_variants")
            == "1"
        )
        migrated.execute("UPDATE ccreport_records SET cost = 7.0")
        migrated.commit()
        assert cache_db._run_migrations(migrated) is False
        assert self._costs(migrated)["bracketed"] == 7.0


# The index and dedup_keys shapes as they stood before idx_ccr_file_ts and
# idx_ccr_sid replaced the single-column idx_ccr_file and idx_ccr_ts, and before
# file_path took the lead of the dedup_keys primary key and left idx_dedup_file
# with nothing to serve.
_OLD_INDEX_SHAPE_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE file_costs (
    path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
    week_cost REAL NOT NULL DEFAULT 0, month_cost REAL NOT NULL DEFAULT 0,
    all_time_cost REAL NOT NULL DEFAULT 0, session_cost REAL) WITHOUT ROWID;
CREATE TABLE dedup_keys (
    dk TEXT NOT NULL,
    file_path TEXT NOT NULL REFERENCES file_costs(path) ON DELETE CASCADE,
    PRIMARY KEY (dk, file_path)) WITHOUT ROWID;
CREATE INDEX idx_dedup_file ON dedup_keys(file_path);
CREATE TABLE ccreport_files (
    path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL);
CREATE TABLE ccreport_records (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL REFERENCES ccreport_files(path) ON DELETE CASCADE,
    mid TEXT, model TEXT NOT NULL, ts REAL NOT NULL, sid TEXT NOT NULL,
    project TEXT NOT NULL, dk TEXT, cost REAL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_create INTEGER NOT NULL, cache_read INTEGER NOT NULL);
CREATE INDEX idx_ccr_file ON ccreport_records(file_path);
CREATE INDEX idx_ccr_ts ON ccreport_records(ts);
"""

_WANTED_CCR_INDEXES = {"idx_ccr_file_ts", "idx_ccr_sid"}
_DEAD_INDEXES = {"idx_ccr_file", "idx_ccr_ts", "idx_dedup_file"}


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def _index_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]


class TestIndexShape:
    """A fresh DB and a migrated one must end up with the same indexes.

    _SCHEMA_SQL only ever creates, so without migration 4 an existing DB would
    keep maintaining the replaced indexes on every insert forever.
    """

    @pytest.fixture
    def old_shaped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "old.db"
        seed = sqlite3.connect(path)
        seed.executescript(_OLD_INDEX_SHAPE_SQL)
        seed.execute(
            "INSERT INTO file_costs (path, mtime_ns, size) VALUES ('/f', 1, 1)"
        )
        seed.executemany(
            "INSERT INTO dedup_keys (dk, file_path) VALUES (?, '/f')",
            [("k1",), ("k2",)],
        )
        seed.commit()
        seed.close()
        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        yield conn
        cache_db.close_connection()

    def test_a_fresh_db_has_the_new_indexes_and_none_of_the_dead_ones(self, db):
        assert _index_names(db, "ccreport_records") >= _WANTED_CCR_INDEXES
        assert _index_names(db, "ccreport_records") & _DEAD_INDEXES == set()
        assert _index_names(db, "dedup_keys") & _DEAD_INDEXES == set()

    def test_migration_brings_an_old_db_to_the_same_shape(self, old_shaped):
        assert _index_names(old_shaped, "ccreport_records") >= _WANTED_CCR_INDEXES
        assert _index_names(old_shaped, "ccreport_records") & _DEAD_INDEXES == set()
        assert _index_names(old_shaped, "dedup_keys") & _DEAD_INDEXES == set()

    def test_the_composite_index_leads_with_file_path(self, db):
        assert _index_columns(db, "idx_ccr_file_ts") == ["file_path", "ts"]

    @pytest.mark.parametrize("fixture", ["db", "old_shaped"])
    def test_dedup_keys_is_keyed_file_path_first(self, fixture, request):
        conn = request.getfixturevalue(fixture)
        assert cache_db._dedup_keys_pk_order(conn) == ["file_path", "dk"]

    def test_the_rebuild_keeps_every_dedup_row(self, old_shaped):
        rows = set(old_shaped.execute("SELECT dk, file_path FROM dedup_keys"))
        assert rows == {("k1", "/f"), ("k2", "/f")}

    def test_the_rebuilt_table_still_cascades_from_file_costs(self, old_shaped):
        old_shaped.execute("DELETE FROM file_costs WHERE path = '/f'")
        old_shaped.commit()
        assert old_shaped.execute("SELECT COUNT(*) FROM dedup_keys").fetchone()[0] == 0

    def test_the_flags_stop_both_migrations_running_twice(self, old_shaped):
        assert cache_db._get_meta(old_shaped, "migrated_drop_dead_indexes") == "1"
        assert cache_db._get_meta(old_shaped, "migrated_dedup_keys_pk_order") == "1"
        assert cache_db._run_migrations(old_shaped) is False


class TestDedupKeysRebuildWithOrphans:
    """A parentless dedup key must not take the whole migration down.

    The old table was written with foreign_keys OFF for years, so real DBs carry
    rows whose file_costs parent is gone. Copying those into the new table under
    foreign_keys ON raised IntegrityError before the migration's own
    foreign_key_check could report it, and every ccreport run died in
    get_connection.
    """

    @pytest.fixture
    def orphaned(self, tmp_path, monkeypatch):
        """An old-shaped DB with one live dedup key and one orphan.

        The pk-order flag is seeded so the open itself is quiet; the test clears
        it and runs the migration by hand.
        """
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "orphaned.db"
        seed = sqlite3.connect(path)
        seed.executescript(_OLD_INDEX_SHAPE_SQL)
        seed.execute(
            "INSERT INTO file_costs (path, mtime_ns, size) VALUES ('/live', 1, 1)"
        )
        seed.executemany(
            "INSERT INTO dedup_keys (dk, file_path) VALUES (?, ?)",
            [("kept", "/live"), ("lost", "/gone")],
        )
        seed.execute(
            "INSERT INTO meta (key, value) VALUES ('migrated_dedup_keys_pk_order', '1')"
        )
        seed.commit()
        seed.close()
        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        yield conn
        cache_db.close_connection()

    @staticmethod
    def _migrate(conn) -> None:
        conn.execute("DELETE FROM meta WHERE key = 'migrated_dedup_keys_pk_order'")
        conn.commit()
        assert cache_db._run_migrations(conn) is True
        assert cache_db._get_meta(conn, "migrated_dedup_keys_pk_order") == "1"

    def test_the_rebuild_keeps_the_live_key_and_drops_the_orphan(self, orphaned):
        self._migrate(orphaned)
        rows = set(orphaned.execute("SELECT dk, file_path FROM dedup_keys"))
        assert rows == {("kept", "/live")}

    def test_the_rebuild_still_reorders_the_primary_key(self, orphaned):
        self._migrate(orphaned)
        assert cache_db._dedup_keys_pk_order(orphaned) == ["file_path", "dk"]


class TestPurgeCostSummaries:
    """write_cost_summary scopes its keys by project, so the literals miss.

    Migrations 1, 2 and 2b deleted 'cost_summary'/'cost_summary_time' and
    cleared nothing at all, because the only caller of write_cost_summary
    always passes a cwd.
    """

    def test_it_removes_the_project_scoped_keys(self, db):
        cache_db.write_cost_summary({"week_cost": 1.0}, cwd="/tmp/proj")
        cache_db.write_cost_summary({"week_cost": 2.0}, cwd="/tmp/other")
        cache_db._purge_cost_summaries(db)
        db.commit()
        assert cache_db.read_cost_summary(cwd="/tmp/proj") is None
        assert cache_db.read_cost_summary(cwd="/tmp/other") is None

    def test_it_removes_the_bare_keys_too(self, db):
        cache_db.write_cost_summary({"week_cost": 1.0})
        cache_db._purge_cost_summaries(db)
        db.commit()
        assert cache_db.read_cost_summary() is None

    def test_it_leaves_everything_else_alone(self, db):
        cache_db._set_meta(db, "ccreport_version", "9")
        cache_db.write_cost_summary({"week_cost": 1.0}, cwd="/tmp/proj")
        cache_db._purge_cost_summaries(db)
        db.commit()
        assert cache_db._get_meta(db, "ccreport_version") == "9"

    def test_a_migration_clears_a_scoped_summary(self, tmp_path, monkeypatch):
        """The regression itself: migration 2b ran and left the real key behind."""
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        path = tmp_path / "cache.db"
        seed = sqlite3.connect(path)
        seed.executescript(cache_db._SCHEMA_SQL)
        _seed_ccreport(seed, [("m", "claude-opus-4-6", 1773424801.0, 5.0)])
        seed.executemany(
            "INSERT INTO meta (key, value) VALUES (?, '1')",
            [
                ("migrated_flat_pricing_2026_03_13",),
                ("migrated_flat_pricing_ccreport",),
                ("migrated_rename_fingerprint",),
            ],
        )
        seed.execute(
            "INSERT INTO meta (key, value) VALUES ('cost_summary:-tmp-proj', '{}')"
        )
        seed.commit()
        seed.close()

        monkeypatch.setattr(cache_db, "DB_PATH", path)
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        try:
            assert cache_db._get_meta(conn, "cost_summary:-tmp-proj") is None
        finally:
            cache_db.close_connection()


class TestSanityCheck:
    """Warns on lost rows, lost costs, or records orphaned from their file."""

    @pytest.fixture
    def snapshotted(self, tmp_path, monkeypatch):
        """A DB with 200 costed records and yesterday's snapshot beside it."""
        snaps = tmp_path / "snaps"
        snaps.mkdir()
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(snaps))
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.delenv("CLAUDE_CACHE_SANITY_DISABLE", raising=False)
        monkeypatch.delenv("CLAUDE_CACHE_SANITY_ABORT", raising=False)
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
        monkeypatch.setattr(cache_db, "_conn", None)
        conn = cache_db.get_connection()
        _seed_ccreport(conn, [(f"m{i}", "claude-opus-5", 1.0, 1.0) for i in range(200)])
        yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        dst = sqlite3.connect(str(snaps / f"{yesterday}.db"))
        conn.backup(dst)
        dst.close()
        yield conn
        cache_db.close_connection()

    def test_an_intact_db_says_nothing(self, snapshotted, capsys):
        cache_db._sanity_check(snapshotted)
        assert capsys.readouterr().err == ""

    def test_a_compressed_snapshot_is_never_the_one_it_opens(self, snapshotted, capsys):
        """Two COUNT(*) queries do not justify decompressing a whole snapshot."""
        snaps = Path(os.environ["CLAUDE_CACHE_SNAPSHOT_DIR"])
        (snaps / "2020-01-01.db.xz").write_bytes(b"not a database")
        snapshotted.execute("DELETE FROM ccreport_records")
        snapshotted.commit()
        cache_db._sanity_check(snapshotted)
        err = capsys.readouterr().err
        assert ".db.xz" not in err
        assert "Restore with:   cp " in err

    def test_a_cost_wipe_warns_though_the_row_count_holds(self, snapshotted, capsys):
        snapshotted.execute("UPDATE ccreport_records SET cost = NULL")
        snapshotted.commit()
        cache_db._sanity_check(snapshotted)
        err = capsys.readouterr().err
        assert "lost ccreport_records costs" in err
        assert "lost ccreport_records rows" not in err

    def test_deleted_rows_still_warn(self, snapshotted, capsys):
        snapshotted.execute("DELETE FROM ccreport_records WHERE mid LIKE 'm1%'")
        snapshotted.commit()
        cache_db._sanity_check(snapshotted)
        assert "lost ccreport_records rows" in capsys.readouterr().err

    def test_abort_env_raises_instead_of_warning(self, snapshotted, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SANITY_ABORT", "1")
        snapshotted.execute("UPDATE ccreport_records SET cost = NULL")
        snapshotted.commit()
        with pytest.raises(RuntimeError, match="lost ccreport_records costs"):
            cache_db._sanity_check(snapshotted)

    def test_parentless_records_are_reported(self, snapshotted, capsys):
        snapshotted.execute("PRAGMA foreign_keys = OFF")
        snapshotted.execute("DELETE FROM ccreport_files")
        snapshotted.commit()
        snapshotted.execute("PRAGMA foreign_keys = ON")
        cache_db._sanity_check(snapshotted)
        assert "no ccreport_files parent" in capsys.readouterr().err

    def test_the_disable_env_silences_everything(
        self, snapshotted, capsys, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CACHE_SANITY_DISABLE", "1")
        snapshotted.execute("DELETE FROM ccreport_records")
        snapshotted.commit()
        cache_db._sanity_check(snapshotted)
        assert capsys.readouterr().err == ""

    def test_a_deferring_process_leaves_the_check_to_whoever_snapshots(
        self,
        tmp_path,
        monkeypatch,
    ):
        """The check rides the snapshot, so deferring one defers the other."""
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
        monkeypatch.setattr(cache_db, "_conn", None)
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        cache_db.get_connection()
        cache_db.close_connection()
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DISABLE")

        calls: list = []
        monkeypatch.setattr(cache_db, "_sanity_check", calls.append)
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DEFER", "1")
        cache_db.get_connection()
        cache_db.close_connection()
        assert calls == []

    def test_it_runs_once_a_day_off_the_snapshot_not_off_migrations(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DEFER", raising=False)
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
        monkeypatch.setattr(cache_db, "_conn", None)
        # Create the DB with snapshots off, so today's is still unwritten and
        # every migration flag is already set — the old trigger is exhausted.
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        cache_db.get_connection()
        cache_db.close_connection()
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DISABLE")

        calls: list = []
        monkeypatch.setattr(cache_db, "_sanity_check", calls.append)
        cache_db.get_connection()
        cache_db.close_connection()
        assert len(calls) == 1, "the run that writes the day's snapshot checks"
        cache_db.get_connection()
        cache_db.close_connection()
        assert len(calls) == 1, "later runs the same day do not"


class _BackupRecorder:
    """Stands in for a connection, recording how conn.backup was called."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def backup(self, dst, **kwargs) -> None:
        self.calls.append(kwargs)


class _SteppedBackup:
    """Stands in for a connection, driving the progress callback as SQLite does.

    *remaining* is the remaining-page count each step reports. A value that goes
    back up is what a restart looks like from the callback's side — SQLite hands
    the copy the whole page count again and returns SQLITE_OK, which is why the
    callback is the only place a restart can be seen at all.
    """

    def __init__(self, remaining: list[int]) -> None:
        self.remaining = remaining
        self.steps = 0

    def backup(self, dst, *, pages=None, sleep=None, progress=None) -> None:
        for rem in self.remaining:
            self.steps += 1
            if progress is not None:
                progress(0, rem, 100)


class TestDailySnapshot:
    """A full copy of the DB is nothing a statusline render should be doing.

    The daily one is deferred out of the render and into its detached refresh
    subprocess; the one taken before a migration is not deferrable at all.
    """

    @pytest.fixture
    def snaps(self, tmp_path, monkeypatch):
        """An existing DB with 200 records, and today's snapshot still unwritten.

        Created with snapshots off, because get_connection only snapshots a DB
        that was already there — a first-ever open has nothing to back up.
        """
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.setenv("CLAUDE_CACHE_SANITY_DISABLE", "1")
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DEFER", raising=False)
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
        monkeypatch.setattr(cache_db, "_conn", None)
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        conn = cache_db.get_connection()
        _seed_ccreport(conn, [(f"m{i}", "claude-opus-5", 1.0, 1.0) for i in range(200)])
        cache_db.close_connection()
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DISABLE")
        yield tmp_path / "snaps"
        cache_db.close_connection()

    @staticmethod
    def _today(snaps):
        return snaps / f"{datetime.now(tz=UTC).strftime('%Y-%m-%d')}.db"

    @staticmethod
    def _open() -> None:
        cache_db.get_connection()
        cache_db.close_connection()

    def test_a_render_defers_the_daily_copy(self, snaps, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DEFER", "1")
        self._open()
        assert not self._today(snaps).exists()

    def test_a_process_that_does_not_defer_takes_it(self, snaps):
        self._open()
        assert self._today(snaps).exists()

    def test_a_pending_bootstrap_snapshots_even_under_defer(self, snaps, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DEFER", "1")
        stale = sqlite3.connect(cache_db.DB_PATH)
        stale.execute(f"PRAGMA user_version = {cache_db.SCHEMA_VERSION - 1}")
        stale.close()
        self._open()
        assert self._today(snaps).exists(), "a migration's pre-image is not optional"

    def test_the_snapshot_is_a_complete_and_valid_copy(self, snaps):
        self._open()
        copy = sqlite3.connect(f"file:{self._today(snaps)}?mode=ro", uri=True)
        try:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert (
                copy.execute("SELECT COUNT(*) FROM ccreport_records").fetchone()[0]
                == 200
            )
        finally:
            copy.close()

    def test_the_copy_is_stepped_rather_than_one_blocking_call(self, snaps):
        """Whole-DB backup() holds a read lock for its entire duration."""
        recorder = _BackupRecorder()
        _path, fresh = cache_db._maybe_snapshot(cast("sqlite3.Connection", recorder))
        assert fresh
        assert recorder.calls[0]["pages"] > 0
        assert recorder.calls[0]["sleep"] > 0
        # Stepping alone bounds nothing: the loop inside backup() has no cap.
        assert callable(recorder.calls[0]["progress"])

    def test_a_copy_the_source_keeps_restarting_is_abandoned(self, snaps, capsys):
        """A restart returns SQLITE_OK, so nothing downstream would ever see it.

        Every statusline render writes cache.db, and each write restarts the
        copy from page 1 — with no cap the detached refresh that owns the daily
        snapshot can sit there for the rest of the run.
        """
        stepped = _SteppedBackup([100, 90] * 8)
        assert cache_db._maybe_snapshot(cast("sqlite3.Connection", stepped)) == (None, False)
        assert stepped.steps == 2 * cache_db._SNAPSHOT_MAX_RESTARTS + 3
        assert not self._today(snaps).exists()
        assert not self._today(snaps).with_suffix(".db.tmp").exists()
        assert "restarts" in capsys.readouterr().err

    def test_a_copy_past_its_deadline_is_abandoned(self, snaps, monkeypatch, capsys):
        monkeypatch.setattr(cache_db, "_SNAPSHOT_DEADLINE_S", 0.0)
        stepped = _SteppedBackup([100, 90, 80])
        assert cache_db._maybe_snapshot(cast("sqlite3.Connection", stepped)) == (None, False)
        assert stepped.steps == 1, "the bound is checked every step, not at the end"
        assert not self._today(snaps).exists()
        assert "deadline" in capsys.readouterr().err

    def test_a_copy_inside_its_bounds_is_kept(self, snaps):
        """Progress that only ever goes forward is not a restart."""
        stepped = _SteppedBackup([100, 50, 0])
        assert cache_db._maybe_snapshot(cast("sqlite3.Connection", stepped)) == (self._today(snaps), True)

    def test_giving_up_leaves_yesterdays_snapshot_alone(self, snaps, monkeypatch):
        """A skipped day costs a day of history, not the history."""
        snaps.mkdir(parents=True)
        older = snaps / "2020-01-01.db"
        older.write_bytes(b"old")
        monkeypatch.setattr(cache_db, "_SNAPSHOT_DEADLINE_S", 0.0)
        cache_db._maybe_snapshot(cast("sqlite3.Connection", _SteppedBackup([100])))
        assert older.exists()

    def test_a_copy_already_under_way_is_not_repeated(self, snaps):
        """target.exists() is checked before a copy that takes seconds."""
        snaps.mkdir(parents=True)
        self._today(snaps).with_suffix(".db.tmp").touch()
        recorder = _BackupRecorder()
        assert cache_db._maybe_snapshot(cast("sqlite3.Connection", recorder)) == (None, False)
        assert recorder.calls == []

    def test_a_tmp_left_by_a_dead_process_is_taken_over(self, snaps):
        snaps.mkdir(parents=True)
        tmp = self._today(snaps).with_suffix(".db.tmp")
        tmp.touch()
        stale = time.time() - cache_db._SNAPSHOT_TMP_STALE_S - 1
        os.utime(tmp, (stale, stale))
        recorder = _BackupRecorder()
        _path, fresh = cache_db._maybe_snapshot(cast("sqlite3.Connection", recorder))
        assert fresh, "an abandoned tmp must not bar the snapshot all day"

    def test_an_existing_snapshot_is_not_rewritten(self, snaps):
        self._open()
        recorder = _BackupRecorder()
        path, fresh = cache_db._maybe_snapshot(cast("sqlite3.Connection", recorder))
        assert (path, fresh) == (self._today(snaps), False)
        assert recorder.calls == []


class TestSnapshotRetention:
    """Recent copies stay daily; older ones thin to the last day of each week.

    The bands are read off the file names, so these cases carry their own clock
    and none of them depends on the day the suite runs.
    """

    @staticmethod
    def _drop(names, keep=7, weeks=7):
        paths = [Path(f"/snaps/{n}.db") for n in names]
        return [p.name[:-3] for p in cache_db._snapshots_to_drop(paths, keep, weeks)]

    @staticmethod
    def _run(start, end):
        """Every date from *start* to *end* inclusive, as YYYY-MM-DD."""
        first = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
        last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
        span = (last - first).days
        return [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span + 1)]

    def test_a_short_history_loses_nothing(self):
        assert self._drop(self._run("2026-08-10", "2026-08-15")) == []

    def test_the_newest_keep_files_stay_daily(self):
        # 2026-08-09 is a Sunday, so 08-09..08-15 spans two ISO weeks.
        assert set(self._drop(self._run("2026-08-03", "2026-08-15"))) == set(
            self._run("2026-08-03", "2026-08-08")
        )

    def test_a_daily_that_ages_out_becomes_its_weeks_keeper(self):
        """The boundary day: 08-09 ends ISO week 32 and leaves the daily band."""
        dropped = self._drop(self._run("2026-08-03", "2026-08-16"))
        assert "2026-08-09" not in dropped
        assert set(dropped) == set(self._run("2026-08-03", "2026-08-08"))

    def test_a_week_is_kept_by_its_last_day_not_its_first(self):
        """Otherwise the keeper walks forward a file at a time as days age out."""
        assert self._drop(self._run("2026-08-03", "2026-08-23"), keep=3) == [
            *self._run("2026-08-03", "2026-08-08"),
            *self._run("2026-08-10", "2026-08-15"),
            *self._run("2026-08-17", "2026-08-20"),
        ]

    def test_the_weekly_bound_drops_the_oldest_weeks(self):
        mondays = self._run("2026-05-04", "2026-06-22")[::7]
        assert self._drop(mondays, keep=1, weeks=3) == mondays[:5]

    def test_zero_weeks_keeps_the_dailies_alone(self):
        days = self._run("2026-08-03", "2026-08-15")
        assert self._drop(days, keep=2, weeks=0) == days[:-2]

    def test_a_file_that_is_not_a_date_is_left_alone(self):
        """Nothing here put it there, so nothing here removes it."""
        names = [*self._run("2026-08-03", "2026-08-15"), "2026-13-45"]
        assert "2026-13-45" not in self._drop(names, keep=1, weeks=0)

    def test_the_weekly_bound_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_WEEKS", "3")
        assert cache_db._snapshot_weeks() == 3
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_WEEKS", "not a number")
        assert cache_db._snapshot_weeks() == cache_db._SNAPSHOT_WEEKS_DEFAULT

    def test_rotation_applies_the_thinning(self, tmp_path, monkeypatch):
        snaps = tmp_path / "snaps"
        snaps.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(snaps))
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_KEEP", "2")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_WEEKS", "1")
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", raising=False)
        for name in self._run("2026-08-03", "2026-08-09"):
            (snaps / f"{name}.db").write_bytes(b"old")
        recorder = _BackupRecorder()
        _path, fresh = cache_db._maybe_snapshot(cast("sqlite3.Connection", recorder))
        assert fresh
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        assert sorted(p.name for p in snaps.glob("*.db")) == sorted(
            ["2026-08-09.db", f"{today}.db"]
        )


class TestSnapshotCompression:
    """Rotated snapshots become .db.xz; the two newest stay readable files."""

    @pytest.fixture
    def snaps(self, tmp_path, monkeypatch):
        snaps = tmp_path / "snaps"
        snaps.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DIR", str(snaps))
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", raising=False)
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
        return snaps

    @staticmethod
    def _names(snaps):
        return sorted(p.name for p in snaps.iterdir())

    def test_the_newest_two_stay_plain_and_the_rest_compress(self, snaps):
        for day in range(3, 8):
            (snaps / f"2026-08-0{day}.db").write_bytes(b"page" * 4096)
        cache_db._rotate_snapshots(snaps)
        assert self._names(snaps) == [
            "2026-08-03.db.xz",
            "2026-08-04.db.xz",
            "2026-08-05.db.xz",
            "2026-08-06.db",
            "2026-08-07.db",
        ]

    def test_the_boundary_day_compresses_exactly_one_file(self, snaps):
        """The day a plain snapshot becomes a compressed one."""
        (snaps / "2026-08-05.db.xz").write_bytes(b"already")
        for day in (6, 7):
            (snaps / f"2026-08-0{day}.db").write_bytes(b"page" * 4096)
        cache_db._rotate_snapshots(snaps)
        assert self._names(snaps) == ["2026-08-05.db.xz", "2026-08-06.db", "2026-08-07.db"]
        (snaps / "2026-08-08.db").write_bytes(b"page" * 4096)
        cache_db._rotate_snapshots(snaps)
        assert self._names(snaps) == [
            "2026-08-05.db.xz",
            "2026-08-06.db.xz",
            "2026-08-07.db",
            "2026-08-08.db",
        ]

    def test_both_kinds_are_one_series_for_retention(self, snaps, monkeypatch):
        """A compressed file counts against keep, or the series never shortens."""
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_KEEP", "3")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_WEEKS", "0")
        for day in (3, 4, 5):
            (snaps / f"2026-08-0{day}.db.xz").write_bytes(b"old")
        for day in (6, 7, 8):
            (snaps / f"2026-08-0{day}.db").write_bytes(b"page" * 4096)
        cache_db._rotate_snapshots(snaps)
        assert self._names(snaps) == ["2026-08-06.db.xz", "2026-08-07.db", "2026-08-08.db"]

    def test_a_compressed_snapshot_restores_to_the_original_bytes(self, snaps):
        import lzma

        body = bytes(range(256)) * 800
        for day in (3, 4, 5):
            (snaps / f"2026-08-0{day}.db").write_bytes(body)
        cache_db._rotate_snapshots(snaps)
        with lzma.open(snaps / "2026-08-03.db.xz") as restored:
            assert restored.read() == body

    def test_rotation_takes_the_sidecars_with_the_snapshot(self, snaps, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_KEEP", "1")
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_WEEKS", "0")
        for day in (3, 4):
            (snaps / f"2026-08-0{day}.db").write_bytes(b"page" * 4096)
            (snaps / f"2026-08-0{day}.db-shm").write_bytes(b"shm")
            (snaps / f"2026-08-0{day}.db-wal").write_bytes(b"wal")
        cache_db._rotate_snapshots(snaps)
        assert self._names(snaps) == ["2026-08-04.db", "2026-08-04.db-shm", "2026-08-04.db-wal"]

    def test_sidecars_left_by_an_earlier_rotation_are_swept(self, snaps):
        """11 of these were on the machine, the oldest for a long-gone .db."""
        (snaps / "2026-07-06.db-shm").write_bytes(b"shm")
        (snaps / "2026-07-06.db-wal").write_bytes(b"wal")
        (snaps / "2026-08-07.db").write_bytes(b"page" * 4096)
        cache_db._rotate_snapshots(snaps)
        assert self._names(snaps) == ["2026-08-07.db"]

    def test_a_failed_compression_leaves_the_plain_file(self, snaps, monkeypatch):
        for day in (3, 4, 5):
            (snaps / f"2026-08-0{day}.db").write_bytes(b"page" * 4096)

        def boom(*_a, **_k):
            raise OSError("no space")

        monkeypatch.setattr(Path, "open", boom)
        cache_db._rotate_snapshots(snaps)
        assert "2026-08-03.db" in self._names(snaps)
        assert not list(snaps.glob("*.tmp"))

    def test_the_restore_hint_matches_the_format_it_points_at(self, snaps):
        assert cache_db._restore_hint(snaps / "2026-08-04.db").startswith("cp ")
        compressed = cache_db._restore_hint(snaps / "2026-08-03.db.xz")
        assert "lzma" in compressed
        assert not compressed.startswith("cp ")


class TestAccountEvents:
    """The change log is append-on-change, not append-per-render."""

    ACC = {
        "accountUuid": "uuid-work",
        "emailAddress": "me@work.example",
        "organizationUuid": "org-work",
        "organizationName": "Work AS",
        "seatTier": "team_tier_1",
        "userRateLimitTier": "default_claude_max_5x",
        "organizationRateLimitTier": "default_raven",
        # Fields the table deliberately does not keep.
        "billingType": "stripe_subscription",
        "displayName": "Me",
        "organizationRole": "owner",
    }
    IDENTITY = (100.0, "uuid-work", "me@work.example", "org-work", "Work AS")
    TIERS = ("team_tier_1", "default_claude_max_5x", "default_raven")

    def _rows(self, db):
        return db.execute(
            "SELECT ts, account_uuid, email, organization_uuid, organization_name "
            "FROM account_events ORDER BY ts"
        ).fetchall()

    def _tiers(self, db):
        return db.execute(
            "SELECT seat_tier, user_rate_limit_tier, organization_rate_limit_tier "
            "FROM account_events ORDER BY ts"
        ).fetchall()

    def test_the_identity_and_the_tiers_are_stored_and_nothing_else(self, db):
        assert record_account_event(self.ACC, now=100.0) is True
        assert self._rows(db) == [self.IDENTITY]
        assert self._tiers(db) == [self.TIERS]
        assert _columns(db, "account_events") == {
            "ts",
            "account_uuid",
            "email",
            "organization_uuid",
            "organization_name",
            "seat_tier",
            "user_rate_limit_tier",
            "organization_rate_limit_tier",
        }

    def test_a_personal_plan_stores_its_missing_tiers_as_null(self, db):
        record_account_event(
            {
                "accountUuid": "uuid-home",
                "emailAddress": "me@home.example",
                "organizationRateLimitTier": "default_claude_max_20x",
            },
            now=100.0,
        )
        assert self._tiers(db) == [(None, None, "default_claude_max_20x")]

    def test_an_unchanged_account_writes_nothing(self, db):
        record_account_event(self.ACC, now=100.0)
        assert record_account_event(self.ACC, now=200.0) is False
        assert (
            record_account_event(dict(self.ACC, displayName="Other"), now=300.0)
            is False
        )
        assert len(self._rows(db)) == 1

    @pytest.mark.parametrize(
        "key", ["seatTier", "userRateLimitTier", "organizationRateLimitTier"]
    )
    def test_a_tier_change_on_the_same_login_appends(self, db, key):
        """The date a seat was upgraded is exactly what this log is for."""
        record_account_event(self.ACC, now=100.0)
        assert record_account_event(dict(self.ACC, **{key: "moved"}), now=200.0) is True
        assert [r[1] for r in self._rows(db)] == ["uuid-work", "uuid-work"]

    def test_a_switch_appends_without_touching_the_old_row(self, db):
        record_account_event(self.ACC, now=100.0)
        other = {
            "accountUuid": "uuid-personal",
            "emailAddress": "me@home.example",
            "organizationUuid": "org-personal",
            "organizationName": "Personal",
        }
        assert record_account_event(other, now=200.0) is True
        assert self._rows(db) == [
            (100.0, "uuid-work", "me@work.example", "org-work", "Work AS"),
            (200.0, "uuid-personal", "me@home.example", "org-personal", "Personal"),
        ]

    def test_switching_back_records_a_third_event(self, db):
        record_account_event(self.ACC, now=100.0)
        record_account_event({"accountUuid": "uuid-personal"}, now=200.0)
        assert record_account_event(self.ACC, now=300.0) is True
        assert [r[1] for r in self._rows(db)] == [
            "uuid-work",
            "uuid-personal",
            "uuid-work",
        ]

    def test_the_same_email_under_a_new_org_is_a_change(self, db):
        """Work and personal billing can share an address; the org splits them."""
        record_account_event(self.ACC, now=100.0)
        moved = dict(self.ACC, organizationUuid="org-2", organizationName="Other AS")
        assert record_account_event(moved, now=200.0) is True
        assert [r[4] for r in self._rows(db)] == ["Work AS", "Other AS"]

    @pytest.mark.parametrize(
        "oauth",
        [
            {},
            {"emailAddress": "me@work.example"},
            {"accountUuid": None, "emailAddress": "me@work.example"},
            {"accountUuid": "", "emailAddress": "me@work.example"},
            {"accountUuid": {"nested": 1}},
        ],
    )
    def test_no_usable_uuid_is_never_stored(self, db, oauth):
        """A row here is permanent history; a NULL key would be uncorrectable."""
        assert record_account_event(oauth, now=100.0) is False
        assert self._rows(db) == []

    def test_a_non_string_field_reads_as_absent(self, db):
        record_account_event(
            {
                "accountUuid": "uuid-work",
                "emailAddress": None,
                "organizationName": {"x": 1},
            },
            now=100.0,
        )
        assert self._rows(db) == [(100.0, "uuid-work", None, None, None)]

    def test_load_account_events_returns_the_log_oldest_first(self, db):
        record_account_event(self.ACC, now=300.0)
        record_account_event({"accountUuid": "uuid-personal"}, now=100.0)
        events = cache_db.load_account_events()
        assert [e["ts"] for e in events] == [100.0, 300.0]
        assert events[1] == {
            "ts": 300.0,
            "account_uuid": "uuid-work",
            "email": "me@work.example",
            "organization_uuid": "org-work",
            "organization_name": "Work AS",
            "seat_tier": "team_tier_1",
            "user_rate_limit_tier": "default_claude_max_5x",
            "organization_rate_limit_tier": "default_raven",
        }

    def test_an_empty_log_loads_as_an_empty_list(self, db):
        assert cache_db.load_account_events() == []


class TestEffectiveLimitTier:
    """Which of the two rate-limit tiers a row was actually subject to."""

    def _row(self, user=None, org=None):
        return {"user_rate_limit_tier": user, "organization_rate_limit_tier": org}

    def test_the_user_tier_wins_over_the_org_pool(self):
        row = self._row(user="default_claude_max_5x", org="default_raven")
        assert cache_db.effective_limit_tier(row) == "default_claude_max_5x"

    def test_the_org_tier_answers_when_there_is_no_user_tier(self):
        assert (
            cache_db.effective_limit_tier(self._row(org="default_claude_max_20x"))
            == "default_claude_max_20x"
        )

    def test_neither_recorded_is_none(self):
        assert cache_db.effective_limit_tier(self._row()) is None

    def test_a_row_from_before_the_columns_existed_is_none(self):
        """Nothing recorded the tier then, and absent is not a tier."""
        assert (
            cache_db.effective_limit_tier({"account_uuid": "u", "email": "e"}) is None
        )


class TestAdoptedAccount:
    """The one backdated row, and the readers that keep it apart from captures."""

    ACC = {
        "accountUuid": "uuid-work",
        "emailAddress": "me@work.example",
        "organizationUuid": "org-work",
        "organizationName": "Work AS",
    }
    ROW = {
        "account_uuid": "uuid-adopted",
        "email": "me@adopted.example",
        "organization_uuid": "org-a",
        "organization_name": "Adopted AS",
        # What ccreport adopt writes: the claim says nothing about which tier
        # that pre-capture history ran under, and nothing can be asked.
        "seat_tier": None,
        "user_rate_limit_tier": None,
        "organization_rate_limit_tier": None,
    }

    def _identities(self, events):
        return [(e["ts"], e["account_uuid"]) for e in events]

    def test_an_empty_log_has_neither_a_capture_nor_an_adoption(self, db):
        assert cache_db.read_latest_account() is None
        assert cache_db.read_adopted_account() is None

    def test_the_adoption_row_lands_at_ts_zero(self, db):
        cache_db.set_adopted_account(self.ROW)
        adopted = present(cache_db.read_adopted_account())
        assert adopted == {"ts": cache_db.ADOPTED_TS, **self.ROW}

    def test_an_adoption_is_not_read_back_as_a_capture(self, db):
        """It is a claim about history, not a reading of who is signed in."""
        cache_db.set_adopted_account(self.ROW)
        assert cache_db.read_latest_account() is None

    def test_the_newest_capture_wins_over_an_older_one(self, db):
        record_account_event(self.ACC, now=100.0)
        record_account_event({"accountUuid": "uuid-home"}, now=200.0)
        assert present(cache_db.read_latest_account())["account_uuid"] == "uuid-home"

    def test_an_adoption_never_shadows_a_capture(self, db):
        record_account_event(self.ACC, now=100.0)
        cache_db.set_adopted_account(self.ROW)
        assert present(cache_db.read_latest_account())["account_uuid"] == "uuid-work"

    def test_re_adopting_replaces_rather_than_appends(self, db):
        cache_db.set_adopted_account(self.ROW)
        cache_db.set_adopted_account(dict(self.ROW, account_uuid="uuid-other"))
        events = cache_db.load_account_events()
        assert self._identities(events) == [(cache_db.ADOPTED_TS, "uuid-other")]

    def test_the_adoption_sorts_ahead_of_every_capture(self, db):
        """Attribution reads the log in order; the claim has to come first."""
        record_account_event(self.ACC, now=100.0)
        cache_db.set_adopted_account(self.ROW)
        assert self._identities(cache_db.load_account_events()) == [
            (cache_db.ADOPTED_TS, "uuid-adopted"),
            (100.0, "uuid-work"),
        ]

    def test_clearing_reports_whether_there_was_anything_to_clear(self, db):
        assert cache_db.clear_adopted_account() is False
        cache_db.set_adopted_account(self.ROW)
        assert cache_db.clear_adopted_account() is True
        assert cache_db.clear_adopted_account() is False
        assert cache_db.read_adopted_account() is None

    def test_clearing_leaves_every_capture_alone(self, db):
        record_account_event(self.ACC, now=100.0)
        cache_db.set_adopted_account(self.ROW)
        cache_db.clear_adopted_account()
        assert self._identities(cache_db.load_account_events()) == [
            (100.0, "uuid-work")
        ]

    def test_an_adoption_does_not_disturb_the_capture_comparison(self, db):
        """record_account_event compares against the newest row, and ts=0 is oldest."""
        record_account_event(self.ACC, now=100.0)
        cache_db.set_adopted_account(dict(self.ROW, account_uuid="uuid-home"))
        # Same account still signed in: still nothing new to record.
        assert record_account_event(self.ACC, now=200.0) is False
        # A real switch still appends.
        assert record_account_event({"accountUuid": "uuid-home"}, now=300.0) is True
        assert self._identities(cache_db.load_account_events()) == [
            (cache_db.ADOPTED_TS, "uuid-home"),
            (100.0, "uuid-work"),
            (300.0, "uuid-home"),
        ]


class TestRateLimitSnapshots:
    """Offered on every render; a row only when the reading actually moved."""

    RESETS = 5_000.0
    GATE = cache_db._RL_SNAPSHOT_MIN_INTERVAL_S

    def _sample(self, pct, resets=None, window="session", model=None, source="stdin"):
        return RateLimitSample(
            window,
            pct,
            self.RESETS if resets is None else resets,
            model,
            source,
        )

    def _rows(self, db, window="session"):
        return db.execute(
            "SELECT ts, used_pct, resets_at, model, source FROM rate_limit_snapshots "
            "WHERE window = ? ORDER BY ts",
            (window,),
        ).fetchall()

    def test_the_first_sample_of_a_window_is_always_written(self, db):
        record_rate_limit_snapshots([self._sample(23.5)], now=1000.0)
        assert self._rows(db) == [(1000.0, 23.5, self.RESETS, None, "stdin")]

    def test_the_raw_float_is_stored_not_the_gated_integer(self, db):
        """The gate rounds so the table stays small; fill rate needs the float."""
        record_rate_limit_snapshots([self._sample(23.456)], now=1000.0)
        assert self._rows(db)[0][1] == 23.456

    def test_an_unchanged_reading_writes_nothing(self, db):
        record_rate_limit_snapshots([self._sample(23.5)], now=1000.0)
        record_rate_limit_snapshots([self._sample(23.5)], now=1000.0 + 10 * self.GATE)
        assert len(self._rows(db)) == 1

    def test_a_reading_inside_the_same_whole_percent_writes_nothing(self, db):
        record_rate_limit_snapshots([self._sample(23.4)], now=1000.0)
        record_rate_limit_snapshots([self._sample(22.8)], now=1000.0 + 10 * self.GATE)
        assert len(self._rows(db)) == 1

    def test_a_changed_reading_inside_the_interval_writes_nothing(self, db):
        """Two sessions rendering together straddle an integer boundary."""
        record_rate_limit_snapshots([self._sample(23.4)], now=1000.0)
        record_rate_limit_snapshots([self._sample(23.6)], now=1000.0 + self.GATE - 1)
        assert len(self._rows(db)) == 1

    def test_a_changed_reading_after_the_interval_is_written(self, db):
        record_rate_limit_snapshots([self._sample(23.4)], now=1000.0)
        record_rate_limit_snapshots([self._sample(23.6)], now=1000.0 + self.GATE)
        assert [r[0:2] for r in self._rows(db)] == [(1000.0, 23.4), (1300.0, 23.6)]

    def test_a_new_window_instance_is_written_immediately(self, db):
        """A fresh window's first sample must not wait out the interval."""
        record_rate_limit_snapshots([self._sample(80.0)], now=1000.0)
        record_rate_limit_snapshots(
            [self._sample(0.2, resets=self.RESETS + 18_000)],
            now=1001.0,
        )
        assert [(r[0], r[2]) for r in self._rows(db)] == [
            (1000.0, self.RESETS),
            (1001.0, self.RESETS + 18_000),
        ]

    def test_each_window_is_gated_on_its_own_history(self, db):
        record_rate_limit_snapshots(
            [
                self._sample(23.5),
                self._sample(41.0, window="week"),
            ],
            now=1000.0,
        )
        # session moved a whole percent, week did not.
        record_rate_limit_snapshots(
            [
                self._sample(30.0),
                self._sample(41.2, window="week"),
            ],
            now=1000.0 + self.GATE,
        )
        assert len(self._rows(db)) == 2
        assert len(self._rows(db, "week")) == 1

    def test_the_scoped_window_keeps_its_model_and_source(self, db):
        record_rate_limit_snapshots(
            [self._sample(12, window="scoped", model="claude-opus-4", source="api")],
            now=1000.0,
        )
        assert self._rows(db, "scoped") == [
            (1000.0, 12.0, self.RESETS, "claude-opus-4", "api"),
        ]

    def test_the_gated_out_render_takes_no_write_lock(self, db):
        """Every render offers every window; the unchanged case is the norm."""
        record_rate_limit_snapshots([self._sample(23.5)], now=1000.0)
        commits = _count_commits(db)
        try:
            record_rate_limit_snapshots([self._sample(23.5)], now=2000.0)
        finally:
            db.set_trace_callback(None)
        assert commits() == 0

    def test_two_samples_of_one_window_in_the_same_tick_keep_the_later(self, db):
        record_rate_limit_snapshots([self._sample(23.5)], now=1000.0)
        record_rate_limit_snapshots(
            [self._sample(0.5, resets=self.RESETS + 18_000)],
            now=1000.0,
        )
        assert self._rows(db) == [(1000.0, 0.5, self.RESETS + 18_000, None, "stdin")]

    def test_an_empty_sample_list_is_a_no_op(self, db):
        record_rate_limit_snapshots([], now=1000.0)
        assert self._rows(db) == []

    def test_every_sample_loads_back_oldest_first(self, db):
        """Window breaks the ts tie: one render offers every window at once."""
        record_rate_limit_snapshots(
            [
                self._sample(41.0, window="week"),
                self._sample(23.5),
            ],
            now=1000.0,
        )
        record_rate_limit_snapshots(
            [self._sample(12.0, window="scoped", model="claude-fable-5", source="api")],
            now=2000.0,
        )
        assert cache_db.load_rate_limit_snapshots() == [
            {
                "ts": 1000.0,
                "window": "session",
                "used_pct": 23.5,
                "resets_at": self.RESETS,
                "model": None,
                "source": "stdin",
            },
            {
                "ts": 1000.0,
                "window": "week",
                "used_pct": 41.0,
                "resets_at": self.RESETS,
                "model": None,
                "source": "stdin",
            },
            {
                "ts": 2000.0,
                "window": "scoped",
                "used_pct": 12.0,
                "resets_at": self.RESETS,
                "model": "claude-fable-5",
                "source": "api",
            },
        ]

    def test_an_empty_table_loads_as_an_empty_list(self, db):
        assert cache_db.load_rate_limit_snapshots() == []

    def test_the_newest_lookup_is_a_primary_key_scan(self, db):
        """The render pays this SELECT per window on every single render."""
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT ts, used_pct, resets_at "
            "FROM rate_limit_snapshots WHERE window = ? ORDER BY ts DESC LIMIT 1",
            ("session",),
        ).fetchall()
        detail = " ".join(r[3] for r in plan)
        assert "USING PRIMARY KEY" in detail
        assert "SCAN" not in detail
        assert "TEMP B-TREE" not in detail


# ---------------------------------------------------------------------------
# Reads bounded to what the caller asked for
# ---------------------------------------------------------------------------


class TestRecordsForPaths:
    """The rebuild of the orphan totals reads the orphaned files and no others."""

    @staticmethod
    def _rec(**kw):
        return {
            "mid": "m",
            "model": "claude-opus-5",
            "ts": 1.5,
            "sid": "s1",
            "project": "proj",
            "cwd": "/tmp/proj",
            "repo": None,
            "dk": None,
            "cost": 0.25,
            "t": [1, 2, 3, 4],
            **kw,
        }

    def test_only_the_named_paths_come_back(self, db):
        save_ccreport_file("/p/a.jsonl", 1, 1, [self._rec(dk="a")])
        save_ccreport_file("/p/b.jsonl", 1, 1, [self._rec(dk="b")])
        got = cache_db.load_ccreport_records_for_paths(["/p/a.jsonl"])
        assert set(got) == {"/p/a.jsonl"}
        assert [r["dk"] for r in got["/p/a.jsonl"]] == ["a"]

    def test_an_empty_path_set_reads_nothing(self, db):
        save_ccreport_file("/p/a.jsonl", 1, 1, [self._rec(dk="a")])
        assert cache_db.load_ccreport_records_for_paths([]) == {}

    def test_insert_order_survives_the_chunking(self, db, monkeypatch):
        """Dedup is first-occurrence-wins, so the order is part of the answer.

        Chunked into one path per statement, which is what a real orphan set
        larger than SQLite's parameter limit does — the file written second
        must still come back second, not first by alphabet. The chunker itself
        is replaced rather than its size constant, which _param_chunks bound as
        a default argument at definition time.
        """
        monkeypatch.setattr(
            cache_db,
            "_param_chunks",
            lambda paths, size=1: [[p] for p in sorted(paths)],
        )
        save_ccreport_file("/p/z.jsonl", 1, 1, [self._rec(dk="z")])
        save_ccreport_file("/p/a.jsonl", 1, 1, [self._rec(dk="a")])
        got = cache_db.load_ccreport_records_for_paths(["/p/a.jsonl", "/p/z.jsonl"])
        assert list(got) == ["/p/z.jsonl", "/p/a.jsonl"]

    def test_a_mismatched_salt_reads_nothing(self, db):
        save_ccreport_file("/p/a.jsonl", 1, 1, [self._rec()])
        cache_db._set_meta(db, "ccreport_schema_salt", "not-the-salt")
        db.commit()
        assert cache_db.load_ccreport_records_for_paths(["/p/a.jsonl"]) == {}


class TestOrphanAllTimeRows:
    ROWS = [("/p/-tmp-proj/", "proj", "/tmp/proj", "", 4.5)]

    def test_the_rows_come_back_under_their_own_fingerprint(self, db):
        cache_db.save_orphan_alltime(self.ROWS, "fp-1")
        assert cache_db.load_orphan_alltime("fp-1") == self.ROWS

    def test_a_moved_fingerprint_reads_as_a_rebuild(self, db):
        cache_db.save_orphan_alltime(self.ROWS, "fp-1")
        assert cache_db.load_orphan_alltime("fp-2") == []

    def test_a_save_replaces_the_previous_set(self, db):
        cache_db.save_orphan_alltime(self.ROWS, "fp-1")
        cache_db.save_orphan_alltime([], "fp-2")
        assert cache_db.load_orphan_alltime("fp-2") == []
        assert db.execute("SELECT COUNT(*) FROM ccreport_orphan_costs").fetchone() == (
            0,
        )

    def test_a_mismatched_salt_reads_as_a_rebuild(self, db):
        cache_db.save_orphan_alltime(self.ROWS, "fp-1")
        cache_db._set_meta(db, "ccreport_schema_salt", "not-the-salt")
        db.commit()
        assert cache_db.load_orphan_alltime("fp-1") == []

    def test_the_stamp_ignores_files_outside_the_orphan_set(self, db):
        """A live session log being re-parsed must not re-sum the purged ones."""
        rec = TestRecordsForPaths._rec()
        save_ccreport_file("/p/gone.jsonl", 1, 1, [rec])
        save_ccreport_file("/p/live.jsonl", 1, 1, [rec])
        before = cache_db.orphan_alltime_stamp(["/p/gone.jsonl"])

        save_ccreport_file("/p/live.jsonl", 2, 2, [rec, dict(rec, dk="second")])
        assert cache_db.orphan_alltime_stamp(["/p/gone.jsonl"]) == before

    def test_the_stamp_moves_when_an_orphans_own_row_does(self, db):
        rec = TestRecordsForPaths._rec()
        save_ccreport_file("/p/gone.jsonl", 1, 1, [rec])
        before = cache_db.orphan_alltime_stamp(["/p/gone.jsonl"])
        save_ccreport_file("/p/gone.jsonl", 2, 9, [rec])
        assert cache_db.orphan_alltime_stamp(["/p/gone.jsonl"]) != before

    def test_the_stamp_survives_a_corpus_of_realistic_mtimes(self, db):
        """SUM(mtime_ns) over a few thousand files overflows SQLite's integers."""
        rec = TestRecordsForPaths._rec()
        base = 1_786_000_000_000_000_000
        paths = [f"/p/f{i}.jsonl" for i in range(300)]
        for i, path in enumerate(paths):
            save_ccreport_file(path, base + i, 10, [rec])
        assert cache_db.orphan_alltime_stamp(paths)


class TestFileAllTimeUnder:
    """all_time is window-independent, so reading it must not touch the windows."""

    ENTRY = {
        "mtime_ns": 111,
        "size": 222,
        "week_cost": 1.0,
        "month_cost": 2.0,
        "all_time_cost": 3.0,
        "week_model_costs": {},
        "dedup_keys": ["k1", "k2"],
    }

    def _seed(self):
        bulk_save_file_costs(
            {"/p/-tmp-proj/a.jsonl": dict(self.ENTRY)},
            "w1",
            "m1",
            changed={"/p/-tmp-proj/a.jsonl"},
        )

    def test_the_stored_all_time_and_keys_come_back(self, db):
        self._seed()
        got = cache_db.load_file_all_time_under("/p/-tmp-proj/")
        assert got == {"/p/-tmp-proj/a.jsonl": (111, 222, 3.0, ["k1", "k2"])}

    def test_another_projects_files_stay_out(self, db):
        self._seed()
        assert cache_db.load_file_all_time_under("/p/-tmp-other/") == {}

    def test_a_rolled_over_week_neither_empties_nor_truncates(self, db):
        """load_cost_cache would DELETE the table here; this reader may not."""
        self._seed()
        got = cache_db.load_file_all_time_under("/p/-tmp-proj/")
        assert got["/p/-tmp-proj/a.jsonl"][2] == 3.0
        assert db.execute("SELECT COUNT(*) FROM file_costs").fetchone() == (1,)
        # The contrast: the same read through load_cost_cache, with the same
        # moved-on keys, takes the row with it.
        assert load_cost_cache("w2", "m2") == {}
        assert db.execute("SELECT COUNT(*) FROM file_costs").fetchone() == (0,)
