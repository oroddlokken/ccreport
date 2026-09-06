"""Tests for the suite's own isolation fixtures.

A fixture that silently stops working takes the tests it was protecting with
it, and they go on passing on the machine that broke them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from conftest import CONFIGURED_BY_ENV

_SRC = Path(__file__).resolve().parent.parent / "src" / "ccreport"

_READ = re.compile(r"""environ\.(?:get|setdefault)\(\s*["']([A-Z_]+)["']""")

# Read by the code and left alone on purpose, for the reasons CONFIGURED_BY_ENV
# states. Named here so a new variable cannot join them by accident.
_EXEMPT = {"COLUMNS", "TMPDIR", "TZ", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}


class TestEnvironmentIsolation:
    """The developer's shell must not reach the code under test."""

    def test_every_named_variable_is_gone(self):
        """All but the one isolate_cache_db sets for itself, right after."""
        leaked = set(CONFIGURED_BY_ENV) & set(os.environ)
        assert leaked == {"CLAUDE_CACHE_SNAPSHOT_DISABLE"}

    def test_the_snapshot_disable_is_the_suite_s_own_and_not_inherited(self):
        assert os.environ["CLAUDE_CACHE_SNAPSHOT_DISABLE"] == "1"

    def test_the_list_covers_what_the_code_reads(self):
        """Adding a variable to the code adds it here, or it leaks in unwatched."""
        found: set[str] = set()
        for path in _SRC.glob("*.py"):
            found |= set(_READ.findall(path.read_text()))
        assert found - _EXEMPT - set(CONFIGURED_BY_ENV) == set()

    def test_the_exemptions_are_still_read(self):
        """An exemption for a variable nobody reads is a stale one."""
        found: set[str] = set()
        for path in _SRC.glob("*.py"):
            found |= set(_READ.findall(path.read_text()))
        assert found >= _EXEMPT


class TestRateStoreIsolation:
    """exchange._store is a process global that create_app moves and never puts back."""

    def test_the_store_starts_each_test_on_the_client_cache(self):
        from ccreport import cache_db, exchange

        assert exchange._store is cache_db

    def test_an_app_moves_the_store_and_the_fixture_moves_it_back(self, tmp_path):
        """The leak this fixture repairs, reproduced inside one test.

        A save through cache_db and a load through exchange landed in two
        databases, so load_rates returned rows the test never wrote.
        """
        from ccreport import cache_db, exchange
        from ccreport.server import db
        from tests import server_fixture as sf

        create_app = __import__(
            "ccreport.server.factory", fromlist=["create_app"]).create_app
        create_app(sf.config(tmp_path / "srv"))
        assert exchange._store is not cache_db
        assert isinstance(exchange._store, db.RateStore)


class TestNetworkIsolation:
    """No test reaches Norges Bank, or any other host.

    BlockedNetworkError is imported as tests.conftest below, not as conftest: the
    directory is a package and the file is importable under both names, which
    hands back two class objects that no `except` clause matches across.
    """

    def test_urlopen_is_gone(self):
        import urllib.request

        from tests.conftest import BlockedNetworkError

        with pytest.raises(BlockedNetworkError):
            urllib.request.urlopen("https://example.invalid")

    def test_the_second_binding_is_gone_too(self):
        """usage_api imported the name, so the module attribute is its own door."""
        from ccreport import usage_api
        from tests.conftest import BlockedNetworkError

        with pytest.raises(BlockedNetworkError):
            usage_api.urlopen("https://example.invalid")

    def test_a_fetch_raises_rather_than_reading_as_an_empty_api(self):
        """exchange._fetch_api catches OSError, so the block may not be one."""
        from datetime import date

        from ccreport import exchange
        from tests.conftest import BlockedNetworkError

        with pytest.raises(BlockedNetworkError):
            exchange._fetch_api(date(2026, 1, 1), date(2026, 1, 2))

    def test_the_cli_prefetch_starts_no_thread(self):
        from ccreport import exchange

        assert exchange.start_prefetch() is None

    def test_live_prefetch_hands_the_real_one_back(self, live_prefetch):
        from ccreport import exchange

        assert exchange.start_prefetch.__module__ == "ccreport.exchange"
