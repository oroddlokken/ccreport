"""Shared fixtures for the ccreport test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_cache_db(tmp_path, monkeypatch):
    """Keep every test off the real ~/.cache/ccreport/cache.db.

    The modules under test reach cache_db through helpers that open the
    singleton connection on demand, several levels below what a test thinks
    it stubbed. Without this, a test that forgets to redirect DB_PATH runs
    schema work and data migrations against the user's real usage history —
    which holds orphaned records no re-parse can rebuild. Tests that need a
    DB of their own still redirect DB_PATH themselves; being autouse, this
    fixture is set up first, so theirs wins.

    The legacy paths are redirected for a sharper reason: get_connection moves
    them into place whenever DB_PATH is missing, and DB_PATH is missing in
    every test. Left alone, the first test to open a connection would relocate
    the developer's actual cache out from under the running status line.
    """
    from ccreport import cache_db, project_identity

    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "isolated-cache.db")
    monkeypatch.setattr(cache_db, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache_db, "_LEGACY_CACHE_DIR", tmp_path / "legacy-cache")
    monkeypatch.setattr(cache_db, "_DEFAULT_SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(cache_db, "_LEGACY_SNAPSHOT_DIR", tmp_path / "legacy-snapshots")
    monkeypatch.setattr(project_identity, "CONFIG_PATH", tmp_path / "ccreport.toml")
    monkeypatch.setattr(
        project_identity, "LEGACY_CONFIG_PATH", tmp_path / "legacy-ccreport.toml")
    monkeypatch.setattr(cache_db, "_conn", None)
    yield
    cache_db.close_connection()
