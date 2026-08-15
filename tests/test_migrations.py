"""Tests for migrations.py — the numbered chain both databases step through.

Every test builds its own SQLite file under tmp_path; nothing here opens the real
cache or a server database.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time

import pytest

from ccreport import cache_db, migrations
from ccreport.server import db as server_db

BASELINE = 5


def _open(path, **kw) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), **kw)
    conn.execute(f"PRAGMA user_version = {BASELINE:d}")
    return conn


def _add_note(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE note (body TEXT)")


def _add_other_note(conn: sqlite3.Connection) -> None:
    """Same name and version as _add_note, different body: an edited step."""
    conn.execute("CREATE TABLE note (body TEXT NOT NULL)")


def _run(conn, chain, path, baseline=BASELINE) -> int:
    return migrations.run(conn, chain=chain, baseline=baseline, db_path=path)


class TestChainShape:
    def test_an_empty_chain_leaves_the_head_at_the_baseline(self):
        assert migrations.head((), 11) == 11

    def test_the_head_is_the_last_step(self):
        chain = (migrations.Step(6, "a"), migrations.Step(7, "b"))
        assert migrations.head(chain, BASELINE) == 7

    def test_a_gap_is_refused(self):
        """A skipped number means a step nobody will ever apply."""
        chain = (migrations.Step(6, "a"), migrations.Step(8, "b"))
        with pytest.raises(ValueError, match="out of order"):
            migrations.head(chain, BASELINE)

    def test_a_repeated_version_is_refused(self):
        """Two steps in one slot: the stamp carries the DB past both after one."""
        chain = (migrations.Step(6, "a"), migrations.Step(6, "b"))
        with pytest.raises(ValueError, match="out of order"):
            migrations.head(chain, BASELINE)

    def test_a_chain_that_does_not_start_above_the_baseline_is_refused(self):
        with pytest.raises(ValueError, match="out of order"):
            migrations.head((migrations.Step(BASELINE, "a"),), BASELINE)

    def test_a_step_needs_a_name(self):
        """The name is what the log row records and what drift is checked on."""
        with pytest.raises(ValueError, match="no name"):
            migrations.head((migrations.Step(6, ""),), BASELINE)


class TestApply:
    def test_a_step_above_the_stamp_runs_and_moves_it(self, tmp_path):
        path = tmp_path / "a.db"
        conn = _open(path)
        assert _run(conn, (migrations.Step(6, "note", _add_note),), path) == 6
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        conn.execute("INSERT INTO note (body) VALUES ('x')")

    def test_steps_run_in_order_and_only_above_the_stamp(self, tmp_path):
        ran: list[int] = []
        chain = tuple(
            migrations.Step(v, f"s{v}", lambda _c, v=v: ran.append(v)) for v in (6, 7, 8)
        )
        path = tmp_path / "a.db"
        conn = _open(path)
        conn.execute("PRAGMA user_version = 7")
        _run(conn, chain, path)
        assert ran == [8]

    def test_a_second_open_applies_nothing(self, tmp_path):
        """The stamp is the whole gate: a step is not idempotent, and CREATE
        TABLE the second time is an error rather than a no-op."""
        ran: list[int] = []
        chain = (migrations.Step(6, "note", lambda _c: ran.append(1)),)
        path = tmp_path / "a.db"
        conn = _open(path)
        _run(conn, chain, path)
        _run(conn, chain, path)
        assert ran == [1]

    def test_a_database_below_the_baseline_starts_at_it(self, tmp_path):
        """The caller's CREATE ... IF NOT EXISTS script has just brought it up to
        the baseline shape, so the chain owes it only what came after."""
        ran: list[int] = []
        path = tmp_path / "a.db"
        conn = _open(path)
        conn.execute("PRAGMA user_version = 0")
        _run(conn, (migrations.Step(6, "note", lambda _c: ran.append(6)),), path)
        assert ran == [6]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6

    def test_an_empty_chain_still_stamps_the_baseline(self, tmp_path):
        path = tmp_path / "a.db"
        conn = _open(path)
        conn.execute("PRAGMA user_version = 0")
        assert _run(conn, (), path) == BASELINE
        assert conn.execute("PRAGMA user_version").fetchone()[0] == BASELINE

    def test_a_bump_entry_carries_the_version_with_no_callable(self, tmp_path):
        """What a new table needs: the CREATE script covers the table, and the
        entry is what moves the version that re-runs the script."""
        path = tmp_path / "a.db"
        conn = _open(path)
        assert _run(conn, (migrations.Step(6, "new table"),), path) == 6
        assert conn.execute(
            "SELECT source_sha FROM schema_migrations WHERE version = 6",
        ).fetchone()[0] is None

    def test_the_log_records_what_ran(self, tmp_path):
        path = tmp_path / "a.db"
        conn = _open(path)
        _run(conn, (migrations.Step(6, "note", _add_note),), path)
        version, name, applied_at, sha = conn.execute(
            "SELECT version, name, applied_at, source_sha FROM schema_migrations",
        ).fetchone()
        assert (version, name) == (6, "note")
        assert applied_at > 0
        assert sha is not None

    @pytest.mark.parametrize("isolation", [{"isolation_level": None}, {}])
    def test_both_connection_styles_work(self, tmp_path, isolation):
        """cache_db opens with sqlite3's implicit transactions and the server
        without them; the explicit BEGIN here has to hold under either."""
        path = tmp_path / "a.db"
        conn = _open(path, **isolation)
        assert _run(conn, (migrations.Step(6, "note", _add_note),), path) == 6


class TestAtomicity:
    def test_a_failing_step_leaves_neither_its_change_nor_its_stamp(self, tmp_path):
        """The stamp moves inside the step's transaction, so a crash cannot
        record a schema change that is not there."""
        def boom(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE half (x TEXT)")
            raise RuntimeError("mid-step")

        path = tmp_path / "a.db"
        conn = _open(path)
        with pytest.raises(RuntimeError, match="mid-step"):
            _run(conn, (migrations.Step(6, "half", boom),), path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == BASELINE
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert "half" not in tables
        assert not conn.execute("SELECT 1 FROM schema_migrations").fetchall()

    def test_a_later_step_keeps_what_an_earlier_one_did(self, tmp_path):
        """Each step commits on its own, so a chain that dies halfway resumes
        from where it stopped rather than starting over."""
        def boom(_conn: sqlite3.Connection) -> None:
            raise RuntimeError("second")

        path = tmp_path / "a.db"
        conn = _open(path)
        chain = (migrations.Step(6, "note", _add_note), migrations.Step(7, "boom", boom))
        with pytest.raises(RuntimeError, match="second"):
            _run(conn, chain, path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6

    def test_an_open_transaction_is_refused(self, tmp_path):
        """A step BEGINs for itself; nesting one inside a caller's transaction
        would commit work the caller had not finished."""
        path = tmp_path / "a.db"
        conn = _open(path)
        conn.execute("BEGIN")
        with pytest.raises(sqlite3.ProgrammingError, match="outside a transaction"):
            _run(conn, (migrations.Step(6, "note", _add_note),), path)


class TestDrift:
    def test_a_stamp_past_this_build_is_refused(self, tmp_path):
        """A newer ccreport wrote the file. Applying nothing and reporting
        success is how a missing column reaches a query instead."""
        path = tmp_path / "a.db"
        conn = _open(path)
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(ValueError, match="past this build's head"):
            _run(conn, (migrations.Step(6, "note", _add_note),), path)

    def test_a_renamed_step_stops_the_next_run(self, tmp_path):
        path = tmp_path / "a.db"
        conn = _open(path)
        _run(conn, (migrations.Step(6, "note", _add_note),), path)
        conn.execute("PRAGMA user_version = 6")
        with pytest.raises(ValueError, match="changed after it was applied"):
            _run(conn, (migrations.Step(6, "renamed", _add_note), migrations.Step(7, "next")), path)

    def test_a_step_edited_in_place_stops_the_next_run(self, tmp_path):
        """The version slot was rewritten rather than appended to: the stamp has
        already carried the DB past it, so nothing would ever apply the edit."""
        path = tmp_path / "a.db"
        conn = _open(path)
        _run(conn, (migrations.Step(6, "note", _add_note),), path)
        with pytest.raises(ValueError, match="no longer holds the code"):
            _run(
                conn,
                (migrations.Step(6, "note", _add_other_note), migrations.Step(7, "next")),
                path,
            )

    def test_a_version_the_chain_no_longer_has_stops_the_next_run(self, tmp_path):
        path = tmp_path / "a.db"
        conn = _open(path)
        _run(conn, (migrations.Step(6, "note", _add_note), migrations.Step(7, "next")), path)
        conn.execute("PRAGMA user_version = 6")
        with pytest.raises(ValueError, match="no version 6"):
            _run(conn, (migrations.Step(7, "next"),), path, baseline=6)

    def test_a_database_migrated_before_the_log_existed_is_checked_against_nothing(
        self, tmp_path,
    ):
        path = tmp_path / "a.db"
        conn = _open(path)
        _run(conn, (migrations.Step(6, "note", _add_note),), path)
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
        assert _run(conn, (migrations.Step(6, "note", _add_note), migrations.Step(7, "next")),
                    path) == 7


class TestSourceSha:
    def test_a_bump_entry_hashes_to_nothing(self):
        assert migrations.source_sha(None) is None

    def test_a_step_with_no_readable_source_hashes_to_nothing(self):
        """An installation from a zip has no source file to read, and refusing
        to start over a source nobody can produce would strand it."""
        scope: dict = {}
        exec("def step(conn): pass", scope)
        assert migrations.source_sha(scope["step"]) is None

    def test_the_same_function_hashes_the_same_way(self):
        assert migrations.source_sha(_add_note) == migrations.source_sha(_add_note)

    def test_two_bodies_hash_differently(self):
        assert migrations.source_sha(_add_note) != migrations.source_sha(_add_other_note)


class TestLock:
    def test_a_held_lock_times_out_rather_than_waiting_forever(self, tmp_path, monkeypatch):
        """flock belongs to the open file description, so a second descriptor in
        this process is refused exactly as another process would be."""
        monkeypatch.setattr(migrations, "LOCK_TIMEOUT_S", 0.1)
        path = tmp_path / "a.db"
        conn = _open(path)
        held = os.open(f"{path}{migrations.LOCK_SUFFIX}", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            with pytest.raises(TimeoutError, match="migration lock"):
                _run(conn, (migrations.Step(6, "note", _add_note),), path)
        finally:
            os.close(held)

    def test_the_caller_can_bound_the_wait_itself(self, tmp_path):
        """cache_db passes the status line's own timeout, so a render gives up
        where a CLI run would keep waiting."""
        path = tmp_path / "a.db"
        conn = _open(path)
        held = os.open(f"{path}{migrations.LOCK_SUFFIX}", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            with pytest.raises(TimeoutError, match=r"0\.05 s"):
                migrations.run(
                    conn, chain=(migrations.Step(6, "note", _add_note),),
                    baseline=BASELINE, db_path=path, timeout_s=0.05,
                )
            assert time.monotonic() - started < migrations.LOCK_TIMEOUT_S
        finally:
            os.close(held)

    def test_the_lock_is_released_after_a_failing_step(self, tmp_path):
        def boom(_conn: sqlite3.Connection) -> None:
            raise RuntimeError("mid-step")

        path = tmp_path / "a.db"
        conn = _open(path)
        with pytest.raises(RuntimeError):
            _run(conn, (migrations.Step(6, "boom", boom),), path)
        # A second run gets the lock rather than timing out on the first one.
        assert _run(conn, (migrations.Step(6, "note", _add_note),), path) == 6

    def test_a_database_at_the_head_never_opens_the_lock_file(self, tmp_path):
        """The path every process takes once one of them has migrated."""
        path = tmp_path / "a.db"
        conn = _open(path)
        assert _run(conn, (), path) == BASELINE
        assert not (tmp_path / f"a.db{migrations.LOCK_SUFFIX}").exists()

    def test_a_symlinked_lock_path_is_refused(self, tmp_path):
        """The path is derived from the database path, so a symlink planted
        there has this process open whatever it points at."""
        path = tmp_path / "a.db"
        conn = _open(path)
        (tmp_path / f"a.db{migrations.LOCK_SUFFIX}").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(ValueError, match="must not be a symlink"):
            _run(conn, (migrations.Step(6, "note", _add_note),), path)


class TestWiring:
    """Both databases derive their stamp from the chain instead of carrying one."""

    @pytest.mark.parametrize("module", [cache_db, server_db])
    def test_the_schema_version_is_the_chain_head(self, module):
        derived = migrations.head(module.MIGRATION_CHAIN, module.MIGRATION_BASELINE)
        assert derived == module.SCHEMA_VERSION

    @pytest.mark.parametrize("module", [cache_db, server_db])
    def test_the_chain_is_well_formed(self, module):
        migrations.validate(module.MIGRATION_CHAIN, module.MIGRATION_BASELINE)
