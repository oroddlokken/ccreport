"""Shared fixtures for the ccreport test suite."""

from __future__ import annotations

import pytest

CONFIGURED_BY_ENV = (
    "CCQUOTA_STOP",
    "CCQUOTA_WARN",
    "CF_BADGE",
    "CLAUDE_CACHE_DB_TIMEOUT",
    "CLAUDE_CACHE_SANITY_ABORT",
    "CLAUDE_CACHE_SANITY_DISABLE",
    "CLAUDE_CACHE_SNAPSHOT_DEFER",
    "CLAUDE_CACHE_SNAPSHOT_DIR",
    "CLAUDE_CACHE_SNAPSHOT_DISABLE",
    "CLAUDE_CACHE_SNAPSHOT_KEEP",
    "CLAUDE_CODE_PACE_DAYS",
    "CLAUDE_STATUSLINE_TIMESTAMP_EPOCH",
    "CLAUDE_STATUSLINE_TOTAL_TOKEN",
    "CLAUDE_STATUSLINE_PUSH",
    "CLAUDE_STATUSLINE_USAGE_JSON",
)
"""Every variable the code reads for configuration, as `just lint-all` sees them.

TZ, TMPDIR, COLUMNS and XDG_CONFIG_HOME are read too and stay: Rich reads
COLUMNS when the module-level console is built at import, before any fixture
runs, and the date tests derive their expectations from the local zone rather
than assuming one.
"""


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    """Keep the developer's own shell out of the suite.

    Each of these changes what the code under test does, so a shell that
    exports one fails tests that pass everywhere else — `CLAUDE_CODE_PACE_DAYS=5`
    took five of the `ccu` pace tests with it. A test that wants one sets it
    itself; isolate_cache_db asks for this fixture by name so its own
    CLAUDE_CACHE_SNAPSHOT_DISABLE survives.
    """
    for name in CONFIGURED_BY_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_cache_db(tmp_path, monkeypatch, isolate_environment):
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
