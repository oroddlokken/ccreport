"""A slow refresh must leave useful, clearly provisional output in scrollback."""

import argparse
import io
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from rich.console import Console

from ccreport import aggregate, cache_db, scan, startup, update_check
from ccreport import ccreport as ccr


@pytest.fixture
def status_events(monkeypatch):
    events = []

    @contextmanager
    def status(self, message):
        events.append(message)
        try:
            yield SimpleNamespace(update=events.append)
        finally:
            events.append("stopped")

    monkeypatch.setattr(Console, "status", status)
    return events


def _record(**kwargs):
    return aggregate.UsageRecord(**{
        "message_id": "one", "model": "claude-opus-4.5",
        "tokens": aggregate.TokenCounts(10, 5, 0, 0),
        "timestamp": datetime(2026, 10, 15, tzinfo=UTC),
        "session_id": "s", "project": "project", "cost_usd": 1.0, **kwargs,
    })


def test_saved_preview_never_opens_database_or_logs(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(ccr, "console", Console(file=output, force_terminal=True, color_system=None))
    args = argparse.Namespace(command=None, tool=None, all=False, debug=False)
    startup.save_preview(ccr._preview_options(args), "SPEND 1.0 [project] :smile:\n")

    def forbidden(*args, **kwargs):
        pytest.fail("saved preview touched the database or logs")

    monkeypatch.setattr(cache_db, "get_connection", forbidden)
    monkeypatch.setattr(ccr, "load_all_records", forbidden)
    monkeypatch.setattr(ccr.exchange, "read_rates_since", forbidden)
    ccr._show_saved_report(args)
    assert "SPEND 1.0 [project] :smile:" in output.getvalue()


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("as_json", [True, False])
@pytest.mark.parametrize("index_age", [None, 0, 86399, 86400, 7 * 86400])
def test_preview_is_visible_before_refresh_and_pipes_stay_clean(
    monkeypatch, interactive, as_json, index_age, status_events,
):
    output = io.StringIO()
    monkeypatch.setattr(ccr, "console", Console(file=output, force_terminal=interactive, color_system=None))
    monkeypatch.setattr(ccr.sys, "argv", ["ccreport", "--json"] if as_json else ["ccreport"])
    monkeypatch.setattr(ccr.sys.stdout, "isatty", lambda: interactive)
    monkeypatch.setattr(ccr.exchange, "start_prefetch", lambda: None)
    monkeypatch.setattr(startup, "read_preview", lambda options: ("SPEND 1.0\n", 1_000_000.0))
    monkeypatch.setattr(ccr, "_remote_records", lambda *args: ccr.RemoteSpend([], set()))
    monkeypatch.setattr(ccr, "load_rates_for_records", lambda *a, **kw: (aggregate.NokCtx(), True))
    if index_age is not None:
        cache_db.record_ccreport_indexed_at(startup.time.time() - index_age)
    should_preview = interactive and not as_json and (index_age is None or index_age >= 86400)
    events = []

    def render(args, records, *rest):
        if interactive:
            assert status_events[-1] == "stopped"

        value = records[0].cost()
        events.append(f"render {value}")
        ccr.console.print(f"SPEND {value}")

    def load(**kwargs):
        events.append("refresh")
        if should_preview:
            assert "Cached report" in output.getvalue()
            assert "SPEND 1.0" in output.getvalue()
        else:
            assert output.getvalue() == ""
        if interactive and not as_json:
            assert status_events[0] == "Loading local usage…"
            assert "Updating agent-tool logs" in status_events[-1]
        else:
            assert status_events == []
        return [_record(cost_usd=2.0)]

    monkeypatch.setattr(ccr, "load_all_records", load)
    monkeypatch.setattr(ccr, "_render_local_report", render)
    monkeypatch.setattr(ccr, "report_json", lambda *a, **kw: ccr.console.print('[{"cost": 2.0}]'))
    ccr.main()
    if as_json:
        assert json.loads(output.getvalue()) == [{"cost": 2.0}]
        assert events == ["refresh"]
    elif interactive:
        assert events == ["refresh", "render 2.0"]
        saved = json.loads(cache_db.DB_PATH.with_name("report-preview.json").read_text())
        assert saved["text"] == "SPEND 2.0\n"
        if should_preview:
            assert output.getvalue().index("Updated report") > output.getvalue().index("SPEND 1.0")
        else:
            assert output.getvalue() == "SPEND 2.0\n"
        assert "Loading" not in output.getvalue()
        assert status_events[-1] == "stopped"
    else:
        assert output.getvalue() == "SPEND 2.0\n"
        assert events == ["refresh", "render 2.0"]


def test_empty_preview_does_not_prevent_first_index(monkeypatch, status_events):
    output = io.StringIO()
    monkeypatch.setattr(ccr, "console", Console(file=output, force_terminal=True, color_system=None))
    monkeypatch.setattr(ccr.sys, "argv", ["ccreport"])
    monkeypatch.setattr(ccr.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(ccr, "discover_jsonl_files", list)
    monkeypatch.setattr(ccr.exchange, "start_prefetch", lambda: None)
    with pytest.raises(SystemExit) as exc:
        ccr.main()
    assert exc.value.code == 1
    assert output.getvalue() == ""
    assert status_events[-1] == "stopped"
    data = json.loads(cache_db.DB_PATH.with_name("startup-timings.json").read_text())
    assert cache_db.read_ccreport_indexed_at() is not None
    assert data["status"] == "complete"
    assert data["counts"]["discovered_files"] == 0
    assert data["phases"]["discovery"] >= 0


def test_diagnostics_keep_age_after_interrupt_and_replace_previous_run(monkeypatch):
    monkeypatch.setattr(startup.time, "time", lambda: 1_000_000.0)
    with startup.ReportRun() as first:
        assert "unknown" in first.age_message()
        cache_db.record_ccreport_indexed_at(1_000_000.0)
        with startup.phase("discovery"):
            startup.count("discovered_files", 12)
    monkeypatch.setattr(startup.time, "time", lambda: 1_000_000.0 + 7 * 86400)
    with pytest.raises(KeyboardInterrupt), startup.ReportRun() as interrupted:  # noqa: PT012 - inspect live state
        assert "7 days ago" in interrupted.age_message()
        with startup.phase("discovery"):
            # The on-disk state identifies the blocking phase even after SIGKILL.
            live = json.loads(interrupted.path.read_text())
            assert live["active_phases"] == ["discovery"]
            raise KeyboardInterrupt
    data = json.loads(interrupted.path.read_text())
    assert data["status"] == "interrupted"
    assert data["stopped_phase"] == "discovery"
    assert cache_db.read_ccreport_indexed_at() == 1_000_000.0
    assert data["counts"] == {}
    assert data["active_phases"] == []
    with startup.ReportRun() as last:
        assert "7 days ago" in last.age_message()
    assert json.loads(last.path.read_text())["phases"] == {}
    assert not list(last.path.parent.glob("startup-timings.*.tmp"))


@pytest.mark.parametrize("contents", ["not json", "[]", '{"last_indexed_at": "broken"}'])
def test_bad_diagnostic_file_is_ignored(contents):
    path = cache_db.DB_PATH.with_name("startup-timings.json")
    path.write_text(contents)
    with startup.ReportRun() as run:
        assert "unknown" in run.age_message()


def test_diagnostics_write_failure_does_not_fail_report(monkeypatch):
    # A parent that is a file cannot hold a diagnostic file.
    cache_db.DB_PATH.parent.joinpath("blocked").write_text("file")
    monkeypatch.setattr(cache_db, "DB_PATH", cache_db.DB_PATH.parent / "blocked" / "cache.db")
    with startup.ReportRun() as run, startup.phase("discovery"):
        startup.count("discovered_files", 3)
    assert run.data["status"] == "complete"


def test_debug_durations_go_to_stderr(capsys):
    with startup.ReportRun(debug=True), startup.phase("discovery"):
        pass
    output = capsys.readouterr()
    assert output.out == ""
    assert "Startup timings:" in output.err
    assert "discovery:" in output.err


@pytest.mark.parametrize("options", [
    {"args": {"tool": "codex"}, "width": 120},
    {"args": {"tool": "claude"}, "width": 80},
    {"args": {"tool": "claude", "all": True}, "width": 120},
])
def test_saved_preview_requires_matching_options(options):
    original = {"args": {"tool": "claude"}, "width": 120}
    startup.save_preview(original, "cached report")
    assert startup.read_preview(original) is not None
    assert startup.read_preview(options) is None


def test_saved_preview_is_bounded_private_and_replaces_previous():
    startup.save_preview({}, "old")
    startup.save_preview({}, "new")
    saved = startup.read_preview({})
    assert saved is not None
    assert saved[0] == "new"
    path = cache_db.DB_PATH.with_name("report-preview.json")
    assert path.stat().st_mode & 0o777 == 0o600
    startup.save_preview({}, "x" * (startup._PREVIEW_MAX_BYTES + 1))
    assert startup.read_preview({}) is None
    assert not path.exists()


@pytest.mark.parametrize("contents", [
    "broken", "[]", '{"schema": 1, "options": {}, "text": "x", "saved_at": "bad"}',
])
def test_corrupt_preview_is_ignored(contents):
    cache_db.DB_PATH.with_name("report-preview.json").write_text(contents)
    assert startup.read_preview({}) is None


def test_failed_render_keeps_last_successful_snapshot_and_clears_recording(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(ccr, "console", Console(file=output, force_terminal=True, color_system=None))
    args = argparse.Namespace(command=None, tool=None, all=False, debug=False)
    startup.save_preview(ccr._preview_options(args), "last success")

    def fail(*args, **kwargs):
        ccr.console.print("partial report")
        raise ValueError("render failed")

    monkeypatch.setattr(ccr, "_render_local_report", fail)
    with pytest.raises(ValueError, match="render failed"):
        ccr._render_and_save_report(args, [], [], ccr.RemoteSpend([], set()), aggregate.NokCtx(), save=True)
    assert not ccr.console.record
    ccr.console.record = True
    assert ccr.console.export_text() == ""
    ccr.console.record = False
    saved = startup.read_preview(ccr._preview_options(args))
    assert saved is not None
    assert saved[0] == "last success"


def test_source_policy_change_skips_saved_report(monkeypatch):
    args = argparse.Namespace(command=None, tool=None, all=False, debug=False)
    monkeypatch.setattr(ccr.sources, "enabled_sources", lambda: frozenset({"claude", "codex"}))
    startup.save_preview(ccr._preview_options(args), "includes Codex")
    monkeypatch.setattr(ccr.sources, "enabled_sources", lambda: frozenset({"claude"}))
    assert startup.read_preview(ccr._preview_options(args)) is None


@pytest.mark.parametrize("as_json", [False, True])
def test_force_color_does_not_enable_interactive_output_in_pipe(monkeypatch, as_json):
    output = io.StringIO()
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(ccr, "console", Console(file=output, color_system=None))
    assert ccr.console.is_terminal
    monkeypatch.setattr(ccr.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(ccr.sys, "argv", ["ccreport", "--json"] if as_json else ["ccreport"])
    monkeypatch.setattr(ccr.exchange, "start_prefetch", lambda: None)
    monkeypatch.setattr(ccr, "load_all_records", lambda **kw: [_record()])
    monkeypatch.setattr(ccr, "_remote_records", lambda *a: ccr.RemoteSpend([], set()))
    monkeypatch.setattr(ccr, "load_rates_for_records", lambda *a, **kw: (aggregate.NokCtx(), True))
    monkeypatch.setattr(ccr, "_render_local_report", lambda *a: ccr.console.print("REPORT"))
    monkeypatch.setattr(ccr, "report_json", lambda *a, **kw: ccr.console.print("[]"))

    def forbidden(*a):
        pytest.fail("a pipe must not read or write a preview")

    monkeypatch.setattr(startup, "read_preview", forbidden)
    monkeypatch.setattr(startup, "save_preview", forbidden)
    ccr.main()
    assert output.getvalue() == ("[]\n" if as_json else "REPORT\n")


def test_successful_phase_inside_handled_exception_is_not_stopped():
    with startup.ReportRun() as run:
        try:
            raise ValueError("handled")
        except ValueError:
            with startup.phase("successful"):
                pass
    assert run.data["status"] == "complete"
    assert "stopped_phase" not in run.data
    assert "successful" in run.data["phases"]


def test_snapshot_invalidated_by_upgrade_or_config_change(monkeypatch):
    args = argparse.Namespace(command=None, tool=None, all=False, debug=False)
    monkeypatch.setattr(update_check, "installed_version", lambda: "1.0")
    original = ccr._preview_options(args)
    startup.save_preview(original, "old report")
    monkeypatch.setattr(update_check, "installed_version", lambda: "1.1")
    assert startup.read_preview(ccr._preview_options(args)) is None
    monkeypatch.setattr(update_check, "installed_version", lambda: "1.0")
    from ccreport import project_identity
    project_identity.CONFIG_PATH.write_text('repo_roots = ["/work"]\n', encoding="utf-8")
    assert startup.read_preview(ccr._preview_options(args)) is None


def test_preview_and_diagnostics_use_utf8_in_ascii_locale(tmp_path):
    script = r"""
import locale
import sys
from pathlib import Path
from ccreport import cache_db, startup
assert not sys.flags.utf8_mode
assert locale.getencoding().lower() not in ('utf-8', 'utf8')
cache_db.DB_PATH = Path(sys.argv[1]) / 'cache.db'
text = 'bl\u00e5b\u00e6r \u00c6\u00d8\u00c5 \u2502'
startup.save_preview({'name': text}, text)
saved = startup.read_preview({'name': text})
assert saved is not None and saved[0] == text
with startup.ReportRun() as run:
    run.data['test_unicode'] = text
print('ok')
"""
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout == "ok\n"
    data = json.loads((tmp_path / "startup-timings.json").read_text(encoding="utf-8"))
    assert data["test_unicode"] == "blåbær ÆØÅ │"


def test_unencodable_preview_does_not_fail_report():
    startup.save_preview({}, "valid")
    startup.save_preview({}, "invalid \ud800")
    saved = startup.read_preview({})
    assert saved is not None
    assert saved[0] == "valid"


@pytest.mark.parametrize("use_rollups", [False, True])
def test_index_stamp_is_shared_by_detached_push_and_reports(monkeypatch, use_rollups):
    monkeypatch.setattr(scan, "discover_jsonl_files", list)
    monkeypatch.setattr(ccr, "discover_jsonl_files", list)
    monkeypatch.setattr(startup.time, "time", lambda: 100.0)
    from ccreport import push
    push.refresh_cache()
    assert cache_db.read_ccreport_indexed_at() == 100.0
    monkeypatch.setattr(startup.time, "time", lambda: 200.0)
    assert ccr.load_all_records(use_rollups=use_rollups) == []
    assert cache_db.read_ccreport_indexed_at() == 200.0
    with startup.ReportRun() as run:
        assert "less than a day" in run.age_message()


def test_index_stamp_cannot_move_back_between_connections():
    import sqlite3
    cache_db.record_ccreport_indexed_at(200.0)
    # Another process's later stamp arrives before an older writer commits.
    with sqlite3.connect(cache_db.DB_PATH) as other:
        other.execute("UPDATE meta SET value = '300.0' WHERE key = 'ccreport_indexed_at'")
    cache_db.record_ccreport_indexed_at(100.0)
    assert cache_db.read_ccreport_indexed_at() == 300.0


@pytest.fixture(params=["full", "rollups", "push"])
def refresh(request, monkeypatch):
    monkeypatch.setattr(scan, "discover_jsonl_files", list)
    monkeypatch.setattr(ccr, "discover_jsonl_files", list)
    if request.param == "push":
        from ccreport import push
        return push.refresh_cache
    return lambda: ccr.load_all_records(use_rollups=request.param == "rollups")


def test_incomplete_scan_does_not_advance_index_stamp(monkeypatch, tmp_path, refresh):
    cache_db.record_ccreport_indexed_at(100.0)
    monkeypatch.setattr(scan, "parse_jsonl", lambda p: (_ for _ in ()).throw(OSError("unreadable")))
    path = tmp_path / "session.jsonl"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scan, "discover_jsonl_files", lambda: [path])
    monkeypatch.setattr(ccr, "discover_jsonl_files", lambda: [path])
    refresh()
    assert cache_db.read_ccreport_indexed_at() == 100.0


def test_failed_scan_does_not_advance_index_stamp(monkeypatch, refresh):
    cache_db.record_ccreport_indexed_at(100.0)

    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(scan, "save_ccreport_files", fail)
    with pytest.raises(OSError, match="write failed"):
        refresh()
    assert cache_db.read_ccreport_indexed_at() == 100.0


def test_unencodable_diagnostics_do_not_fail_report():
    with startup.ReportRun() as run:
        run.data["invalid_unicode"] = "\ud800"
    assert run.data["status"] == "complete"
    assert not list(run.path.parent.glob("startup-timings.*.tmp"))


def test_invalid_utf8_preview_is_ignored():
    cache_db.DB_PATH.with_name("report-preview.json").write_bytes(b"\xff")
    assert startup.read_preview({}) is None


@pytest.mark.parametrize("value", ["bad", "nan", "inf", "-1", "0"])
def test_invalid_index_stamp_is_unknown(value):
    conn = cache_db.get_connection()
    conn.execute("INSERT INTO meta (key, value) VALUES ('ccreport_indexed_at', ?)", (value,))
    conn.commit()
    assert cache_db.read_ccreport_indexed_at() is None


@pytest.mark.parametrize(("age", "expected"), [
    (None, True), (-100, False), (0, False), (86399, False), (86400, True), (172800, True),
])
def test_preview_staleness_threshold(monkeypatch, age, expected):
    monkeypatch.setattr(startup.time, "time", lambda: 1_000_000.0)
    if age is not None:
        cache_db.record_ccreport_indexed_at(1_000_000.0 - age)
    with startup.ReportRun() as run:
        assert run.preview_due() is expected


def test_raw_all_hit_scan_keeps_database_unchanged():
    conn = cache_db.get_connection()
    before = conn.total_changes
    assert scan._refresh_changed_files([], {}) == ({}, set())
    assert conn.total_changes == before
    assert cache_db.read_ccreport_indexed_at() is None
