"""Shared fixtures for the ccreport test suite."""

from __future__ import annotations

import pytest

from ccreport import exchange as _exchange

_real_start_prefetch = _exchange.start_prefetch
"""Captured at import, before no_speculative_prefetch swaps the module attribute."""

CONFIGURED_BY_ENV = (
    "CCQUOTA_STOP",
    "CCQUOTA_STOP_SESSION",
    "CCQUOTA_STOP_WEEK",
    "CCQUOTA_WARN",
    "CCQUOTA_WARN_SESSION",
    "CCQUOTA_WARN_WEEK",
    "CF_BADGE",
    "CLAUDE_CACHE_DB_TIMEOUT",
    "CLAUDE_CACHE_SANITY_ABORT",
    "CLAUDE_CACHE_SANITY_DISABLE",
    "CLAUDE_CACHE_SNAPSHOT_DEFER",
    "CLAUDE_CACHE_SNAPSHOT_DIR",
    "CLAUDE_CACHE_SNAPSHOT_DISABLE",
    "CLAUDE_CACHE_SNAPSHOT_KEEP",
    "CLAUDE_CACHE_SNAPSHOT_WEEKS",
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
    """Keep every test off the real ~/.local/share/ccreport/cache.db.

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
    monkeypatch.setattr(cache_db, "_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cache_db, "_LEGACY_CACHE_DIR", tmp_path / "legacy-cache")
    monkeypatch.setattr(cache_db, "_LEGACY_XDG_CACHE_DIR", tmp_path / "legacy-xdg-cache")
    monkeypatch.setattr(cache_db, "_DEFAULT_SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(cache_db, "_LEGACY_SNAPSHOT_DIR", tmp_path / "legacy-snapshots")
    monkeypatch.setattr(project_identity, "CONFIG_PATH", tmp_path / "ccreport.toml")
    monkeypatch.setattr(
        project_identity, "LEGACY_CONFIG_PATH", tmp_path / "legacy-ccreport.toml")
    monkeypatch.setattr(cache_db, "_conn", None)
    yield
    cache_db.close_connection()


class BlockedNetworkError(RuntimeError):
    """Raised in place of an HTTP request a test did not stub."""


@pytest.fixture(autouse=True, scope="session")
def block_network():
    """Take urlopen away from the whole session, not just from a test.

    exchange.RateFetch runs its request on a daemon thread, so a prefetch
    started by one test can call urlopen after that test's monkeypatch has been
    undone — and land its rows in whichever database the next test installed.
    A session-scoped swap is what a function-scoped one cannot be: still in
    place between tests. Every per-test patch layers on top and is restored to
    this, not to the real function.

    BlockedNetwork rather than OSError, which exchange._fetch_api catches and
    reports as an empty range: a test that reaches out has to fail, not read as
    an API with nothing to say.
    """
    import urllib.request

    from ccreport import usage_api

    def blocked(*args, **kwargs):
        raise BlockedNetworkError("the suite does not reach the network; stub this call")

    original = urllib.request.urlopen
    original_usage = usage_api.urlopen
    # usage_api binds the name at import, so the module attribute is a second
    # door into the same function and closing one leaves the other open.
    urllib.request.urlopen = blocked
    usage_api.urlopen = blocked
    yield
    urllib.request.urlopen = original
    usage_api.urlopen = original_usage


@pytest.fixture(autouse=True)
def no_speculative_prefetch(monkeypatch):
    """Stop the CLI's rate prefetch before it opens a socket.

    ccreport.main calls exchange.start_prefetch() before the corpus load, so
    every test that drives the CLI puts a Norges Bank request on a daemon
    thread that outlives the test — six of them did. block_network refuses the
    request, and this keeps the thread from being started at all. A test that
    is about the prefetch asks for live_prefetch.
    """
    from ccreport import exchange

    monkeypatch.setattr(exchange, "start_prefetch", lambda: None)


@pytest.fixture
def live_prefetch(monkeypatch):
    """Give start_prefetch back to a test that is about the prefetch itself.

    Such a test stubs exchange._fetch_api, so the thread this starts reaches no
    further than the stub.
    """
    from ccreport import exchange

    monkeypatch.setattr(exchange, "start_prefetch", _real_start_prefetch)


@pytest.fixture(autouse=True)
def isolate_rate_store(monkeypatch):
    """Put exchange's rate store back where the next test expects it.

    server.factory.create_app points exchange._store at the server database it
    just opened, and nothing puts it back: a test that builds an app leaves
    every later test on the same xdist worker reading rates out of that app's
    file, which by then is a closed database in a previous test's tmp_path.
    isolate_cache_db redirects cache_db.DB_PATH and cannot see this, so the
    write went to the isolated cache and the read went elsewhere.
    """
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)


@pytest.fixture(autouse=True)
def close_server_pools(monkeypatch):
    """Close the server connections this test opened on its own thread.

    Database keeps one connection per thread and leaves them to process exit:
    right for a long-lived server, wrong for a test process. An abandoned
    connection is finalized by the GC rather than closed, which sqlite3 reports
    as an unclosed database.
    """
    from ccreport.server import db

    built: list[db.Database] = []
    original = db.Database

    def track(path):
        pool = original(path)
        built.append(pool)
        return pool

    monkeypatch.setattr(db, "Database", track)
    yield
    for pool in built:
        pool.close()


@pytest.fixture(autouse=True)
def isolate_session_logs(tmp_path, monkeypatch):
    """Keep every test off the developer's own ~/.claude/projects.

    A test reaches the roots through scan rather than through the name it
    stubbed — push.run_once refreshes the cache on its way to sending — so
    without this the suite parses this machine's whole history into a temporary
    database, and its numbers change with whatever the developer worked on
    yesterday. Left uncreated: discover_jsonl_files skips a root that is not
    there, and a directory in tmp_path is a directory some other test counts.
    """
    from ccreport import scan

    monkeypatch.setattr(scan, "_PROJECT_ROOTS", (tmp_path / "isolated-projects",))
