"""Relocating the cache, snapshots and config off their legacy macsetup paths.

The move happens once per machine and moves data no re-parse can rebuild, so
what is pinned here is the shape of it: sidecars travel with the DB, a
destination that already holds data is never overwritten, and a second run is
a no-op rather than an error.
"""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from ccreport import cache_db, project_identity
from ccreport.ccreport import cmd_migrate


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """Point every path at tmp_path and return the six of them.

    conftest's autouse fixture already redirects these; this one re-does it so
    the layout is visible in the test that reads it, and so DB_PATH sits inside
    the cache dir the way it does in the real one.
    """
    paths = argparse.Namespace(
        legacy_cache=tmp_path / "old" / "macsetup" / "claude",
        cache=tmp_path / "new" / "ccreport",
        legacy_snapshots=tmp_path / "old" / "share" / "macsetup" / "claude" / "snapshots",
        snapshots=tmp_path / "new" / "share" / "ccreport" / "snapshots",
        legacy_config=tmp_path / "old" / "config" / "macsetup" / "claude" / "ccreport.toml",
        config=tmp_path / "new" / "config" / "ccreport" / "ccreport.toml",
    )
    monkeypatch.setattr(cache_db, "_LEGACY_CACHE_DIR", paths.legacy_cache)
    monkeypatch.setattr(cache_db, "_CACHE_DIR", paths.cache)
    monkeypatch.setattr(cache_db, "DB_PATH", paths.cache / "cache.db")
    monkeypatch.setattr(cache_db, "_LEGACY_SNAPSHOT_DIR", paths.legacy_snapshots)
    monkeypatch.setattr(cache_db, "_DEFAULT_SNAPSHOT_DIR", paths.snapshots)
    monkeypatch.setattr(project_identity, "LEGACY_CONFIG_PATH", paths.legacy_config)
    monkeypatch.setattr(project_identity, "CONFIG_PATH", paths.config)
    return paths


def _seed_cache(paths, *, sidecars: bool = True) -> None:
    paths.legacy_cache.mkdir(parents=True)
    (paths.legacy_cache / "cache.db").write_bytes(b"SQLite format 3\x00")
    if sidecars:
        (paths.legacy_cache / "cache.db-wal").write_bytes(b"wal")
        (paths.legacy_cache / "cache.db-shm").write_bytes(b"shm")


class TestRelocate:
    def test_a_machine_with_nothing_to_move_reports_nothing(self, legacy):
        assert cache_db.relocate_legacy_paths() == []

    def test_the_cache_moves_with_its_wal_and_shm_sidecars(self, legacy):
        _seed_cache(legacy)

        moved = cache_db.relocate_legacy_paths()

        assert len(moved) == 1
        assert (legacy.cache / "cache.db").read_bytes() == b"SQLite format 3\x00"
        assert (legacy.cache / "cache.db-wal").read_bytes() == b"wal"
        assert (legacy.cache / "cache.db-shm").read_bytes() == b"shm"
        assert not legacy.legacy_cache.exists()

    def test_snapshots_and_config_move_alongside_the_cache(self, legacy):
        _seed_cache(legacy)
        legacy.legacy_snapshots.mkdir(parents=True)
        (legacy.legacy_snapshots / "2026-08-09.db").write_bytes(b"snap")
        legacy.legacy_config.parent.mkdir(parents=True)
        legacy.legacy_config.write_text('repo_roots = ["~/dev"]\n')

        moved = cache_db.relocate_legacy_paths()

        assert len(moved) == 3
        assert (legacy.snapshots / "2026-08-09.db").read_bytes() == b"snap"
        assert legacy.config.read_text() == 'repo_roots = ["~/dev"]\n'

    def test_a_second_run_moves_nothing_and_says_so(self, legacy):
        _seed_cache(legacy)

        assert len(cache_db.relocate_legacy_paths()) == 1
        assert cache_db.relocate_legacy_paths() == []

    def test_an_existing_destination_is_never_overwritten(self, legacy):
        """The live DB wins; a leftover under the old path is not the real one."""
        _seed_cache(legacy)
        legacy.cache.mkdir(parents=True)
        (legacy.cache / "cache.db").write_bytes(b"the real one")

        assert cache_db.relocate_legacy_paths() == []
        assert (legacy.cache / "cache.db").read_bytes() == b"the real one"
        assert (legacy.legacy_cache / "cache.db").exists()

    def test_each_path_moves_on_its_own(self, legacy):
        """A config file left behind by an interrupted run still gets moved."""
        legacy.legacy_config.parent.mkdir(parents=True)
        legacy.legacy_config.write_text("repo_roots = []\n")

        moved = cache_db.relocate_legacy_paths()

        assert len(moved) == 1
        assert legacy.config.exists()


class TestGetConnection:
    def test_opening_a_missing_db_relocates_the_legacy_one(self, legacy):
        legacy.legacy_cache.mkdir(parents=True)
        conn = sqlite3.connect(str(legacy.legacy_cache / "cache.db"))
        conn.execute("CREATE TABLE proof (x TEXT)")
        conn.execute("INSERT INTO proof VALUES ('carried over')")
        conn.commit()
        conn.close()

        rows = cache_db.get_connection().execute("SELECT x FROM proof").fetchall()

        assert rows == [("carried over",)]

    def test_an_already_migrated_machine_leaves_the_legacy_path_alone(self, legacy):
        _seed_cache(legacy, sidecars=False)
        legacy.cache.mkdir(parents=True)
        sqlite3.connect(str(cache_db.DB_PATH)).close()

        cache_db.get_connection()

        assert (legacy.legacy_cache / "cache.db").exists()


class TestMigrateCommand:
    def test_dry_run_lists_the_moves_without_making_them(self, legacy, capsys):
        _seed_cache(legacy)

        cmd_migrate(argparse.Namespace(dry_run=True))

        out = capsys.readouterr().out
        assert "would move" in out
        assert str(legacy.legacy_cache) in out
        assert legacy.legacy_cache.exists()
        assert not legacy.cache.exists()

    def test_dry_run_on_a_clean_machine_says_there_is_nothing_to_do(self, legacy, capsys):
        cmd_migrate(argparse.Namespace(dry_run=True))

        assert capsys.readouterr().out.strip() == "Nothing to migrate."

    def test_it_reports_what_it_moved(self, legacy, capsys):
        _seed_cache(legacy)

        cmd_migrate(argparse.Namespace(dry_run=False))

        out = capsys.readouterr().out
        assert out.startswith("moved ")
        assert str(legacy.cache) in out
        assert (legacy.cache / "cache.db").exists()

    def test_running_it_twice_is_not_an_error(self, legacy, capsys):
        _seed_cache(legacy)

        cmd_migrate(argparse.Namespace(dry_run=False))
        capsys.readouterr()
        cmd_migrate(argparse.Namespace(dry_run=False))

        assert capsys.readouterr().out.strip() == "Nothing to migrate."
