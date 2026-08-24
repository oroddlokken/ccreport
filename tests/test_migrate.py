"""Relocating the DB, snapshots and config off the paths they used to have.

Two generations of them, the macsetup repo and ~/.cache. The move happens once
per machine and moves data no re-parse can rebuild, so what is pinned here is
the shape of it: sidecars travel with the DB, a destination that already holds
data is never overwritten, and a second run is a no-op rather than an error.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys

import pytest

from ccreport import cache_db, project_identity
from ccreport.ccreport import cmd_migrate


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """Point every path at tmp_path and return the seven of them.

    conftest's autouse fixture already redirects these; this one re-does it so
    the layout is visible in the test that reads it, and so DB_PATH and the
    snapshots sit inside the data dir the way they do in the real one.
    """
    paths = argparse.Namespace(
        legacy_cache=tmp_path / "old" / "macsetup" / "claude",
        legacy_xdg_cache=tmp_path / "old" / "cache" / "ccreport",
        data=tmp_path / "new" / "share" / "ccreport",
        legacy_snapshots=tmp_path / "old" / "share" / "macsetup" / "claude" / "snapshots",
        snapshots=tmp_path / "new" / "share" / "ccreport" / "snapshots",
        legacy_config=tmp_path / "old" / "config" / "macsetup" / "claude" / "ccreport.toml",
        config=tmp_path / "new" / "config" / "ccreport" / "ccreport.toml",
    )
    monkeypatch.setattr(cache_db, "_LEGACY_CACHE_DIR", paths.legacy_cache)
    monkeypatch.setattr(cache_db, "_LEGACY_XDG_CACHE_DIR", paths.legacy_xdg_cache)
    monkeypatch.setattr(cache_db, "_DATA_DIR", paths.data)
    monkeypatch.setattr(cache_db, "DB_PATH", paths.data / "cache.db")
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


def _seed_xdg_cache(paths, *, body: bytes = b"the ~/.cache one") -> None:
    paths.legacy_xdg_cache.mkdir(parents=True)
    (paths.legacy_xdg_cache / "cache.db").write_bytes(body)
    (paths.legacy_xdg_cache / "cache.db-wal").write_bytes(b"xdg wal")


class TestRelocate:
    def test_a_machine_with_nothing_to_move_reports_nothing(self, legacy):
        assert cache_db.relocate_legacy_paths() == []

    def test_the_cache_moves_with_its_wal_and_shm_sidecars(self, legacy):
        _seed_cache(legacy)

        moved = cache_db.relocate_legacy_paths()

        assert len(moved) == 1
        assert (legacy.data / "cache.db").read_bytes() == b"SQLite format 3\x00"
        assert (legacy.data / "cache.db-wal").read_bytes() == b"wal"
        assert (legacy.data / "cache.db-shm").read_bytes() == b"shm"
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
        legacy.data.mkdir(parents=True)
        (legacy.data / "cache.db").write_bytes(b"the real one")

        assert cache_db.relocate_legacy_paths() == []
        assert (legacy.data / "cache.db").read_bytes() == b"the real one"
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
        legacy.data.mkdir(parents=True)
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
        assert not legacy.data.exists()

    def test_dry_run_on_a_clean_machine_says_there_is_nothing_to_do(self, legacy, capsys):
        cmd_migrate(argparse.Namespace(dry_run=True))

        assert capsys.readouterr().out.strip() == "Nothing to migrate."

    def test_it_reports_what_it_moved(self, legacy, capsys):
        _seed_cache(legacy)

        cmd_migrate(argparse.Namespace(dry_run=False))

        out = capsys.readouterr().out
        assert out.startswith("moved ")
        assert str(legacy.data) in out
        assert (legacy.data / "cache.db").exists()

    def test_running_it_twice_is_not_an_error(self, legacy, capsys):
        _seed_cache(legacy)

        cmd_migrate(argparse.Namespace(dry_run=False))
        capsys.readouterr()
        cmd_migrate(argparse.Namespace(dry_run=False))

        assert capsys.readouterr().out.strip() == "Nothing to migrate."


class TestCacheHomeGeneration:
    """~/.cache/ccreport/cache.db, moved beside server.db in ~/.local/share.

    The DB was never a cache: ccreport_archive, account_events and the two
    snapshot series survive logs that have rotated away, and push_state is the
    only record of what each server already holds. A disk cleaner emptying
    ~/.cache took all of it.
    """

    def test_the_db_moves_into_a_data_dir_that_already_has_other_files(self, legacy):
        """The move is per file: server.db and the snapshots are already there."""
        _seed_xdg_cache(legacy)
        legacy.data.mkdir(parents=True)
        (legacy.data / "server.db").write_bytes(b"the merged one")

        moved = cache_db.relocate_legacy_paths()

        assert len(moved) == 1
        assert cache_db.DB_PATH.read_bytes() == b"the ~/.cache one"
        assert (legacy.data / "cache.db-wal").read_bytes() == b"xdg wal"
        assert (legacy.data / "server.db").read_bytes() == b"the merged one"

    def test_an_emptied_source_directory_goes_and_a_shared_one_stays(self, legacy):
        _seed_xdg_cache(legacy)
        (legacy.legacy_xdg_cache / "something-else").write_bytes(b"not ours")

        cache_db.relocate_legacy_paths()

        assert (legacy.legacy_xdg_cache / "something-else").exists()
        assert not (legacy.legacy_xdg_cache / "cache.db").exists()

    def test_the_newer_generation_wins_when_a_machine_held_both(self, legacy):
        """macsetup is the older layout, so ~/.cache is the DB that was in use."""
        _seed_cache(legacy)
        _seed_xdg_cache(legacy)

        moved = cache_db.relocate_legacy_paths()

        assert len(moved) == 1
        assert cache_db.DB_PATH.read_bytes() == b"the ~/.cache one"
        assert (legacy.legacy_cache / "cache.db").exists()

    def test_opening_a_missing_db_relocates_the_cache_home_one(self, legacy):
        legacy.legacy_xdg_cache.mkdir(parents=True)
        conn = sqlite3.connect(str(legacy.legacy_xdg_cache / "cache.db"))
        conn.execute("CREATE TABLE proof (x TEXT)")
        conn.execute("INSERT INTO proof VALUES ('carried over')")
        conn.commit()
        conn.close()

        rows = cache_db.get_connection().execute("SELECT x FROM proof").fetchall()

        assert rows == [("carried over",)]

    def test_dry_run_names_the_file_rather_than_the_directory(self, legacy, capsys):
        _seed_xdg_cache(legacy)

        cmd_migrate(argparse.Namespace(dry_run=True))

        out = capsys.readouterr().out
        assert str(legacy.legacy_xdg_cache / "cache.db") in out
        assert (legacy.legacy_xdg_cache / "cache.db").exists()


class TestDataHome:
    """XDG_DATA_HOME picks the directory, the way XDG_CONFIG_HOME picks it for
    ccreport.toml. Read at import, so a subprocess is what can observe it."""

    def _db_path(self, env_value: str | None, tmp_path) -> str:
        env = dict(os.environ)
        env.pop("XDG_DATA_HOME", None)
        if env_value is not None:
            env["XDG_DATA_HOME"] = env_value
        env["HOME"] = str(tmp_path / "home")
        out = subprocess.run(
            [sys.executable, "-c", "from ccreport import cache_db; print(cache_db.DB_PATH)"],
            capture_output=True, text=True, check=True, env=env,
        )
        return out.stdout.strip()

    def test_it_is_honoured_when_set(self, tmp_path):
        assert self._db_path(str(tmp_path / "elsewhere"), tmp_path) == str(
            tmp_path / "elsewhere" / "ccreport" / "cache.db")

    def test_it_falls_back_to_local_share(self, tmp_path):
        assert self._db_path(None, tmp_path) == str(
            tmp_path / "home" / ".local" / "share" / "ccreport" / "cache.db")
