"""The cost-window key names all trace back to pricing.ROLLING_WINDOWS.

The usage table's columns and the UsageData TypedDict are declarations, not
generated lists — a window added to ROLLING_WINDOWS and nowhere else does not
raise, it just never reaches the cache or the segment. These tests are what
turns that silence into a failure.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ccreport import cache_db
from ccreport.cache_db import _SCHEMA_SQL, _USAGE_FIELDS
from ccreport.pricing import (
    ROLLING_COST_NAMES,
    ROLLING_WINDOWS,
    UsageData,
    project_key,
    project_path_prefixes,
    rolling_cost_keys,
)


@pytest.fixture(scope="module")
def usage_columns() -> set[str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_SCHEMA_SQL)
        return {r[1] for r in conn.execute("PRAGMA table_info(usage)")}
    finally:
        conn.close()


class TestRollingWindowKeys:
    def test_names_and_labels_are_unique(self):
        assert len({w.name for w in ROLLING_WINDOWS}) == len(ROLLING_WINDOWS)
        assert len({w.label for w in ROLLING_WINDOWS}) == len(ROLLING_WINDOWS)

    def test_cost_names_end_with_the_untimed_bucket(self):
        assert ROLLING_COST_NAMES[-1] == "all_time"
        assert ROLLING_COST_NAMES[:-1] == [w.name for w in ROLLING_WINDOWS]

    def test_keys_are_a_total_and_project_pair_per_window(self):
        keys = rolling_cost_keys()
        assert len(keys) == 2 * len(ROLLING_COST_NAMES)
        for name in ROLLING_COST_NAMES:
            assert f"{name}_cost" in keys
            assert f"{name}_project_cost" in keys

    def test_usage_table_has_a_column_per_key(self, usage_columns):
        missing = [k for k in rolling_cost_keys() if k not in usage_columns]
        assert not missing, f"usage table is missing columns: {missing}"

    def test_every_usage_field_is_a_column(self, usage_columns):
        missing = [f for f in _USAGE_FIELDS if f not in usage_columns]
        assert not missing, f"_USAGE_FIELDS names non-existent columns: {missing}"

    def test_usage_fields_carry_every_key(self):
        missing = [k for k in rolling_cost_keys() if k not in _USAGE_FIELDS]
        assert not missing, f"_USAGE_FIELDS is missing: {missing}"

    def test_usage_data_typeddict_declares_every_key(self):
        declared = set(UsageData.__annotations__)
        missing = [k for k in rolling_cost_keys() if k not in declared]
        assert not missing, f"UsageData is missing: {missing}"


class TestProjectKey:
    """One encoding for the projects-dir name and the cost-summary cache key."""

    CWD = "/Users/x/git/repo"

    def test_slashes_become_dashes(self):
        assert project_key(self.CWD) == "-Users-x-git-repo"

    def test_prefixes_end_with_a_separator(self):
        prefixes = project_path_prefixes(self.CWD, [Path("/root/a"), Path("/root/b")])
        assert prefixes == [
            "/root/a/-Users-x-git-repo/",
            "/root/b/-Users-x-git-repo/",
        ]

    def test_cache_writer_and_reader_share_the_suffix(self):
        # A divergence here is a silent cache miss, so assert they agree.
        assert cache_db._cost_summary_suffix(self.CWD) == f":{project_key(self.CWD)}"
        assert cache_db._cost_summary_suffix(None) == ""
        assert cache_db._cost_summary_suffix("") == ""


class TestStatuslineToggles:
    def test_every_window_has_a_toggle(self):
        toggles = _load_statusline()._COST_WINDOW_TOGGLES
        assert {w.name for w in ROLLING_WINDOWS} == set(toggles)
        for env_key, default in toggles.values():
            assert env_key.endswith("_COST")
            assert isinstance(default, bool)


def _load_statusline():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ccreport import statusline

    return statusline
