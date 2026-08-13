"""Tests for the suite's own isolation fixtures.

A fixture that silently stops working takes the tests it was protecting with
it, and they go on passing on the machine that broke them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from conftest import CONFIGURED_BY_ENV

_SRC = Path(__file__).resolve().parent.parent / "src" / "ccreport"

_READ = re.compile(r"""environ\.(?:get|setdefault)\(\s*["']([A-Z_]+)["']""")

# Read by the code and left alone on purpose, for the reasons CONFIGURED_BY_ENV
# states. Named here so a new variable cannot join them by accident.
_EXEMPT = {"COLUMNS", "TMPDIR", "TZ", "XDG_CONFIG_HOME"}


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
