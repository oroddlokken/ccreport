"""The quota guard: what the two hooks read, and what they answer with."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ccreport import cache_db
from ccreport import quota_guard as qg
from ccreport import statusline as sl

NOW = 1_800_000_000.0
SESSION = "quota-session"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone().isoformat()


@pytest.fixture(autouse=True)
def _tmpdir(tmp_path, monkeypatch):
    """Both files the guard touches live under TMPDIR."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))


def _usage_row(**fields) -> None:
    """The singleton usage row, with every quota null unless named."""
    row: dict[str, Any] = dict.fromkeys(
        (
            "session_percent", "session_reset", "week_percent", "week_reset",
            "sonnet_percent", "sonnet_reset", "scoped_percent", "scoped_reset",
        ),
    )
    row["last_updated"] = _iso(NOW)
    row.update(fields)
    cache_db.write_usage_cache(row)


def _quota_file(windows: dict, ts: float = NOW, schema: int = qg.QUOTA_FILE_SCHEMA) -> None:
    qg.quota_reading_path(SESSION).write_text(
        json.dumps({"schema": schema, "ts": ts, "windows": windows}), encoding="utf-8",
    )


def _snapshot(
    window: str, percent: float, ts: float, resets_in: float = 3600.0, source: str = "stdin",
) -> None:
    """One rate_limit_snapshots row, stamped at *ts* rather than at now."""
    cache_db.record_rate_limit_snapshots(
        [cache_db.RateLimitSample(window, percent, NOW + resets_in, None, source)], ts,
    )


def _window(percent: float, resets_in: float = 3600.0) -> dict:
    return {"percent": percent, "resets_at": NOW + resets_in}


def _state(window: str, percent: float | None, resets_in: float = 3600.0) -> qg.WindowState:
    return qg.WindowState(window, percent, None if percent is None else NOW + resets_in, 900.0)


class TestArming:
    """CCQUOTA_STOP is the whole switch, and it is read before Python starts."""

    def test_an_unset_variable_answers_nothing(self):
        assert qg.hook_output({"session_id": SESSION}, NOW, {}) is None

    def test_a_blank_variable_answers_nothing(self):
        assert qg.hook_output({"session_id": SESSION}, NOW, {qg.STOP_ENV: "  "}) is None

    def test_the_wrapper_starts_no_interpreter_unarmed(self, tmp_path):
        """The gate is what keeps an unarmed session at one bash spawn per prompt."""
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        marker = tmp_path / "python-ran"
        fake = bin_dir / "python3"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        fake.chmod(0o755)
        # Copied where ../.venv/bin/python cannot exist, so PATH is the only
        # interpreter the wrapper can find.
        script = tmp_path / "bin" / "quota-guard.sh"
        script.parent.mkdir()
        shutil.copy(Path(__file__).resolve().parent.parent / "bin" / "quota-guard.sh", script)
        # fakebin first, so `command -v python3` in the wrapper finds the
        # marker script rather than the system interpreter.
        env = {"PATH": f"{bin_dir}:/bin:/usr/bin", "TMPDIR": str(tmp_path)}

        unarmed = subprocess.run(
            ["bash", str(script)], input="{}", capture_output=True, text=True,
            env=env, check=True,
        )
        assert unarmed.stdout == ""
        assert not marker.exists()

        subprocess.run(
            ["bash", str(script)], input="{}", capture_output=True, text=True,
            env={**env, qg.STOP_ENV: "95"}, check=True,
        )
        assert marker.exists()


class TestVerdict:
    """The pure part: percentages and two lines against them."""

    def test_at_the_stop_line_stops(self):
        v = qg.verdict([_state("session", 95.0)], 85.0, 95.0)
        assert v.kind == "stop"
        assert "5-hour" in v.message
        assert qg.STOP_ENV in v.message

    def test_between_the_lines_warns(self):
        v = qg.verdict([_state("week", 87.0)], 85.0, 95.0)
        assert v.kind == "warn"
        assert "weekly" in v.message

    def test_under_the_warn_line_passes(self):
        assert qg.verdict([_state("week", 40.0)], 85.0, 95.0).kind == "ok"

    def test_no_warn_line_leaves_only_the_stop(self):
        assert qg.verdict([_state("week", 94.0)], None, 95.0).kind == "ok"

    def test_the_fullest_window_is_the_one_named(self):
        v = qg.verdict([_state("session", 96.0), _state("week", 99.0)], 85.0, 95.0)
        assert "weekly" in v.message

    def test_an_unknown_window_stops(self):
        v = qg.verdict([_state("session", 10.0), _state("sonnet", None)], 85.0, 95.0)
        assert v.kind == "stop"
        assert "weekly Sonnet" in v.message
        assert qg.STOP_ENV in v.message

    def test_a_spent_window_outranks_an_unknown_one(self):
        v = qg.verdict([_state("session", 97.0), _state("sonnet", None)], 85.0, 95.0)
        assert "5-hour" in v.message

    def test_nothing_readable_stops(self):
        v = qg.verdict([], 85.0, 95.0)
        assert v.kind == "stop"
        assert qg.STOP_ENV in v.message


class TestThreshold:
    @pytest.mark.parametrize(("raw", "expected"), [("95", 95.0), (" 95 ", 95.0), ("95%", 95.0)])
    def test_a_percentage_parses(self, raw, expected):
        assert qg.threshold(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "  ", "soon", "ninety"])
    def test_anything_else_is_none(self, raw):
        assert qg.threshold(raw) is None


class TestReadWindows:
    """Which reading wins, and which absence is unknown rather than absent."""

    def test_the_session_file_supplies_s_and_w(self):
        _usage_row(session_percent=10, session_reset=_iso(NOW + 3600))
        _quota_file({"session": _window(42.0), "week": _window(61.0)})
        pcts = {s.window: s.percent for s in qg.read_windows(SESSION, NOW)}
        assert pcts["session"] == 42.0
        assert pcts["week"] == 61.0

    def test_the_highest_fresh_reading_wins(self):
        """Two sources disagreeing resolve the way the guard fails."""
        _usage_row(session_percent=80, session_reset=_iso(NOW + 3600))
        _quota_file({"session": _window(42.0)})
        pcts = {s.window: s.percent for s in qg.read_windows(SESSION, NOW)}
        assert pcts["session"] == 80.0

    def test_a_stale_file_and_a_stale_row_leave_it_unknown(self):
        _usage_row(session_percent=10, session_reset=_iso(NOW + 3600))
        _quota_file({"session": _window(42.0)}, ts=NOW - qg.NATIVE_MAX_AGE_S - 60)
        later = NOW + qg.API_MAX_AGE_S + 60
        states = {s.window: s.percent for s in qg.read_windows(SESSION, later)}
        assert states["session"] is None

    def test_an_unknown_shape_is_no_reading(self):
        _usage_row()
        _quota_file({"session": _window(42.0)}, schema=qg.QUOTA_FILE_SCHEMA + 1)
        assert qg.read_windows(SESSION, NOW) == []

    def test_a_quota_the_plan_lacks_is_not_watched(self):
        """The distinction the whole guard turns on: null on a fresh row means
        the plan has no such quota."""
        _usage_row(session_percent=10, session_reset=_iso(NOW + 3600))
        windows = [s.window for s in qg.read_windows(SESSION, NOW)]
        assert windows == ["session"]

    def test_a_stale_row_leaves_every_quota_unknown(self):
        _usage_row(
            session_percent=10, session_reset=_iso(NOW + 3600),
            sonnet_percent=20, sonnet_reset=_iso(NOW + 3600),
        )
        later = NOW + qg.API_MAX_AGE_S + 60
        states = {s.window: s.percent for s in qg.read_windows(SESSION, later)}
        assert states == {"session": None, "sonnet": None}

    def test_the_unknown_message_names_the_budget_that_found_nothing(self):
        _usage_row(sonnet_percent=20, sonnet_reset=_iso(NOW + 7200))
        later = NOW + qg.API_MAX_AGE_S + 60
        v = qg.verdict(qg.read_windows(SESSION, later), 85.0, 95.0)
        assert "60 minutes" in v.message

    def test_a_snapshot_row_stands_in_for_a_missing_file(self):
        _usage_row()
        cache_db.record_rate_limit_snapshots(
            [cache_db.RateLimitSample("session", 55.0, NOW + 3600, None, "stdin")], NOW,
        )
        pcts = {s.window: s.percent for s in qg.read_windows(SESSION, NOW)}
        assert pcts["session"] == 55.0

    def test_a_rolled_window_reads_zero_rather_than_unknown(self):
        """A reset in the past is the window restarting, which is a reading."""
        _usage_row()
        _quota_file({"session": _window(96.0, resets_in=-60)})
        pcts = {s.window: s.percent for s in qg.read_windows(SESSION, NOW)}
        assert pcts["session"] == 0.0

    def test_no_session_id_still_reads_the_database(self):
        _usage_row(week_percent=70, week_reset=_iso(NOW + 3600))
        pcts = {s.window: s.percent for s in qg.read_windows("", NOW)}
        assert pcts["week"] == 70.0

    def test_an_empty_machine_reads_nothing(self):
        assert qg.read_windows(SESSION, NOW) == []


class TestSnapshotReadings:
    """One grouped query stands in for four, so each window keeps its own row."""

    def test_a_window_with_no_rows_contributes_nothing(self):
        """A fresh usage row with every quota null is the plan lacking them, so
        only the window the snapshot covers is watched."""
        _usage_row()
        _snapshot("session", 55.0, ts=NOW)
        assert [s.window for s in qg.read_windows(SESSION, NOW)] == ["session"]

    def test_a_row_past_its_budget_reads_unknown(self):
        _snapshot("session", 55.0, ts=NOW - qg.NATIVE_MAX_AGE_S - 60)
        states = {s.window: s.percent for s in qg.read_windows(SESSION, NOW)}
        assert states == {"session": None}

    def test_each_window_is_measured_against_its_own_source(self):
        """stdin takes NATIVE_MAX_AGE_S and api takes API_MAX_AGE_S, off one row
        each at the same age."""
        ts = NOW - qg.NATIVE_MAX_AGE_S - 60
        _snapshot("session", 55.0, ts=ts)
        _snapshot("sonnet", 66.0, ts=ts, source="api")
        states = {s.window: s.percent for s in qg.read_windows(SESSION, NOW)}
        assert states == {"session": None, "sonnet": 66.0}

    def test_four_windows_each_keep_their_newest_row(self):
        """Every window carries an older row at a higher percent, so a group
        that picked the wrong row reads too high."""
        for i, window in enumerate(qg.WINDOW_LABELS):
            _snapshot(window, 90.0 + i, ts=NOW - 5000 - i, resets_in=1800 + i)
            _snapshot(window, 10.0 + i, ts=NOW - 100 - i, resets_in=3600 + i)
        states = {s.window: (s.percent, s.resets_at) for s in qg.read_windows(SESSION, NOW)}
        assert states == {
            window: (10.0 + i, NOW + 3600 + i) for i, window in enumerate(qg.WINDOW_LABELS)
        }

    def test_a_newest_row_per_window_carries_its_own_ts(self):
        """The four newest rows differ in age, and the budget cuts between two
        of them."""
        for i, window in enumerate(qg.WINDOW_LABELS):
            _snapshot(window, 40.0 + i, ts=NOW - 300 * i)
        later = NOW + qg.NATIVE_MAX_AGE_S - 450
        states = {s.window: s.percent for s in qg.read_windows(SESSION, later)}
        assert states == {"session": 40.0, "week": 41.0, "sonnet": None, "scoped": None}


class TestHookOutput:
    """The JSON each hook prints."""

    ENV = {qg.STOP_ENV: "95", qg.WARN_ENV: "85"}

    def test_over_the_stop_line_halts_the_turn(self):
        _usage_row(session_percent=96, session_reset=_iso(NOW + 3600))
        out = qg.hook_output({"session_id": SESSION}, NOW, self.ENV) or {}
        assert set(out) == {"continue", "stopReason"}
        assert out["continue"] is False
        assert out["stopReason"].startswith("Quota guard: the 5-hour window is at 96%")

    def test_the_stop_reason_names_the_way_out(self):
        _usage_row(session_percent=96, session_reset=_iso(NOW + 3600))
        out = qg.hook_output({"session_id": SESSION}, NOW, self.ENV) or {}
        assert f"Unset {qg.STOP_ENV}" in out["stopReason"]

    def test_a_warning_is_a_system_message(self):
        _usage_row(session_percent=87, session_reset=_iso(NOW + 3600))
        out = qg.hook_output({"session_id": SESSION}, NOW, self.ENV) or {}
        assert set(out) == {"systemMessage"}

    def test_one_warning_per_window_instance(self):
        _usage_row(session_percent=87, session_reset=_iso(NOW + 3600))
        assert qg.hook_output({"session_id": SESSION}, NOW, self.ENV) is not None
        assert qg.hook_output({"session_id": SESSION}, NOW + 1, self.ENV) is None

    def test_the_next_window_earns_its_own_warning(self):
        _usage_row(session_percent=87, session_reset=_iso(NOW + 60))
        assert qg.hook_output({"session_id": SESSION}, NOW, self.ENV) is not None
        _usage_row(session_percent=88, session_reset=_iso(NOW + 18_000))
        assert qg.hook_output({"session_id": SESSION}, NOW + 120, self.ENV) is not None

    def test_under_both_lines_prints_nothing(self):
        _usage_row(session_percent=12, session_reset=_iso(NOW + 3600))
        assert qg.hook_output({"session_id": SESSION}, NOW, self.ENV) is None

    def test_an_unparsable_stop_line_blocks(self):
        _usage_row(session_percent=12, session_reset=_iso(NOW + 3600))
        out = qg.hook_output({"session_id": SESSION}, NOW, {qg.STOP_ENV: "ninety"}) or {}
        assert out["continue"] is False
        assert "ninety" in out["stopReason"]

    def test_an_unparsable_warn_line_leaves_the_stop_alone(self):
        _usage_row(session_percent=96, session_reset=_iso(NOW + 3600))
        env = {qg.STOP_ENV: "95", qg.WARN_ENV: "most"}
        out = qg.hook_output({"session_id": SESSION}, NOW, env) or {}
        assert out["continue"] is False


class TestMain:
    """The stdin-to-stdout shell around hook_output."""

    def _run(self, monkeypatch, capsys, payload: str, **env) -> str:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
        qg.main()
        return capsys.readouterr().out

    @pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse"])
    def test_either_event_gets_the_same_verdict(self, monkeypatch, capsys, event):
        """PreToolUse is what halts a turn already running."""
        _usage_row(session_percent=99, session_reset=_iso(NOW + 3600))
        payload = json.dumps({"session_id": SESSION, "hook_event_name": event})
        out = self._run(monkeypatch, capsys, payload, **{qg.STOP_ENV: "95"})
        assert json.loads(out)["continue"] is False

    @pytest.mark.parametrize("payload", ["", "not json", "[]", "{}"])
    def test_an_unusable_payload_prints_nothing(self, monkeypatch, capsys, payload):
        _usage_row(session_percent=12, session_reset=_iso(NOW + 3600))
        assert self._run(monkeypatch, capsys, payload, **{qg.STOP_ENV: "95"}) == ""

    def test_a_reader_that_raises_never_blocks(self, monkeypatch, capsys):
        """A guard tripping over its own bug must not be what stops a session."""
        def boom(*_args):
            raise RuntimeError("no")

        monkeypatch.setattr(qg, "read_windows", boom)
        assert self._run(monkeypatch, capsys, "{}", **{qg.STOP_ENV: "95"}) == ""


class TestStatuslineAgreement:
    """The render writes the file; the guard rebuilds its path in another module."""

    @pytest.mark.parametrize("sid", ["abc123", "with/slash", "..", "weird id!#$", "x" * 200])
    def test_both_modules_name_the_same_file(self, sid):
        assert qg._session_state_path(sid, ".quota") == sl._session_state_path(sid, ".quota")

    def test_the_schema_guards_agree(self):
        assert qg.QUOTA_FILE_SCHEMA == sl._QUOTA_FILE_SCHEMA

    def test_a_render_writes_what_the_guard_reads(self, monkeypatch):
        monkeypatch.setenv(qg.STOP_ENV, "95")
        data = {
            "rate_limits": {
                "five_hour": {"used_percentage": 44.5, "resets_at": NOW + 3600},
                "seven_day": {"used_percentage": 71.0, "resets_at": NOW + 400_000},
            },
        }
        sl._save_quota_reading(SESSION, data, NOW, test_mode=False)
        _usage_row()
        pcts = {s.window: s.percent for s in qg.read_windows(SESSION, NOW)}
        assert pcts == {"session": 44.5, "week": 71.0}

    def test_an_unarmed_render_writes_nothing(self, monkeypatch):
        monkeypatch.delenv(qg.STOP_ENV, raising=False)
        sl._save_quota_reading(SESSION, {"rate_limits": {}}, NOW, test_mode=False)
        assert not qg.quota_reading_path(SESSION).exists()

    def test_a_render_before_the_first_response_still_stamps(self, monkeypatch):
        """No rate_limits on stdin yet: the stamp proves the render, and the
        usage row supplies the reading."""
        monkeypatch.setenv(qg.STOP_ENV, "95")
        sl._save_quota_reading(SESSION, {}, NOW, test_mode=False)
        payload = json.loads(qg.quota_reading_path(SESSION).read_text(encoding="utf-8"))
        assert payload["windows"] == {}

    def test_test_mode_writes_nothing(self, monkeypatch):
        monkeypatch.setenv(qg.STOP_ENV, "95")
        sl._save_quota_reading(SESSION, {"rate_limits": {}}, NOW, test_mode=True)
        assert not qg.quota_reading_path(SESSION).exists()

    def test_the_guard_reads_a_live_database(self):
        """The read-only connection has to see what a WAL writer just wrote."""
        _usage_row(week_percent=33, week_reset=_iso(time.time() + 3600))
        pcts = {s.window: s.percent for s in qg.read_windows(SESSION, time.time())}
        assert pcts["week"] == 33.0

    def test_no_database_at_all_blocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "absent.db")
        out = qg.hook_output({"session_id": SESSION}, NOW, {qg.STOP_ENV: "95"}) or {}
        assert out["continue"] is False


def test_the_repo_ships_the_wrapper_executable():
    script = Path(__file__).resolve().parent.parent / "bin" / "quota-guard.sh"
    assert os.access(script, os.X_OK)
