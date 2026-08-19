"""Tests for `ccreport archive` — folding the purged half of the record cache.

The corpus is written as real JSONL, parsed once, then deleted from disk: that
is what makes a file an orphan, and an orphan behind the cutoff is the only
thing this command touches.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pytest

from ccreport import cache_db
from ccreport import ccreport as ccr
from ccreport.aggregate import UNKNOWN_ACCOUNT

OLD = "2026-01-15T12:00:00Z"
"""Well behind any cutoff the tests set, and behind the rollup window too."""


def _write_jsonl(
    path: Path, *, when: str, project_cwd: str, ids: list[str], cost: float = 1.0,
    request: str | None = None,
) -> None:
    """One JSONL file. *request* pins every line to one requestId, making them duplicates."""
    lines = [
        json.dumps({
            "type": "assistant",
            "timestamp": when,
            "sessionId": "sess-" + path.stem,
            "cwd": project_cwd,
            "requestId": request or f"req-{i}",
            "message": {
                "id": mid,
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 10, "output_tokens": 20,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                },
            },
            "costUSD": cost,
        })
        for i, mid in enumerate(ids)
    ]
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A temp cache and a temp projects tree, wired the way load_all_records is."""
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cache_db, "_conn", None)
    projects_root = tmp_path / "projects"
    projects = projects_root / "-tmp-live"
    projects.mkdir(parents=True)
    monkeypatch.setattr(
        ccr, "discover_jsonl_files", lambda: sorted(projects.glob("*.jsonl"))
    )
    monkeypatch.setattr(ccr, "_ensure_cache_valid", lambda _live_paths: None)
    monkeypatch.setattr(ccr, "_get_projects_dirs", lambda: [projects_root])
    cache_db.init_ccreport_meta(ccr.CACHE_VERSION, "test-hash")
    yield projects
    cache_db.get_connection().close()
    cache_db._conn = None


def _args(*, dry_run: bool = False, min_age_days: int = 30) -> argparse.Namespace:
    return argparse.Namespace(dry_run=dry_run, min_age_days=min_age_days)


def _totals(records) -> dict[tuple[str, str, str], tuple[float, int, int]]:
    """(day, project, account) -> (cost, calls, input tokens), the report's grain."""
    out: dict[tuple[str, str, str], list] = {}
    for rec in records:
        key = (rec.day_key(), rec.project, rec.account)
        row = out.setdefault(key, [0.0, 0, 0])
        row[0] += rec.cost()
        row[1] += rec.count
        row[2] += rec.tokens.input
    return {k: (round(v[0], 6), v[1], v[2]) for k, v in out.items()}


def _purge(*paths: Path) -> None:
    """Cache the files, then take them off disk, as Claude Code's cleanup does."""
    ccr.load_all_records()
    for path in paths:
        path.unlink()


class TestFoldAndDrop:
    def test_a_purged_file_behind_the_cutoff_is_folded(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1", "m2"])
        _purge(corpus / "a.jsonl")
        ccr.cmd_archive(_args())
        capsys.readouterr()
        assert len(cache_db.load_ccreport_archive()) == 1
        assert cache_db.archived_file_paths() == {str(corpus / "a.jsonl")}

    def test_the_records_are_gone_from_the_table(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1", "m2"])
        _purge(corpus / "a.jsonl")
        ccr.cmd_archive(_args())
        capsys.readouterr()
        conn = cache_db.get_connection()
        assert conn.execute("SELECT COUNT(*) FROM ccreport_records").fetchone()[0] == 0

    def test_the_report_totals_do_not_move(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1", "m2"])
        _write_jsonl(corpus / "b.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m3"], cost=2.5)
        _purge(corpus / "a.jsonl", corpus / "b.jsonl")
        before = _totals(ccr.load_all_records())
        ccr.cmd_archive(_args())
        capsys.readouterr()
        assert _totals(ccr.load_all_records()) == before

    def test_a_live_file_is_never_folded(self, corpus, capsys):
        _write_jsonl(corpus / "live.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1"])
        ccr.load_all_records()
        ccr.cmd_archive(_args())
        capsys.readouterr()
        assert cache_db.load_ccreport_archive() == []

    def test_a_file_newer_than_the_cutoff_is_held_back(self, corpus, capsys):
        recent = dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_jsonl(corpus / "a.jsonl", when=recent, project_cwd="/tmp/live",
                     ids=["m1"])
        _purge(corpus / "a.jsonl")
        ccr.cmd_archive(_args())
        out = capsys.readouterr().out
        assert cache_db.load_ccreport_archive() == []
        assert "newer than the cutoff" in out

    def test_dry_run_writes_nothing(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1"])
        _purge(corpus / "a.jsonl")
        ccr.cmd_archive(_args(dry_run=True))
        out = capsys.readouterr().out
        assert "1 purged files" in out
        assert cache_db.load_ccreport_archive() == []
        conn = cache_db.get_connection()
        assert conn.execute("SELECT COUNT(*) FROM ccreport_records").fetchone()[0] == 1

    def test_the_preview_counts_the_rows_the_delete_takes(self, corpus, capsys):
        """Half a real machine's folded rows lost a dedup and were never reported."""
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1", "m1"], request="req-0")
        _purge(corpus / "a.jsonl")
        plan, _cutoff = ccr._plan_archive(30)
        assert plan.records == 2
        assert sum(row[-1] for row in plan.rows) == 1
        deleted = ccr.save_ccreport_archive(plan.rows, plan.paths)
        assert deleted == plan.records

    def test_a_second_run_finds_nothing_left(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1"])
        _purge(corpus / "a.jsonl")
        ccr.cmd_archive(_args())
        capsys.readouterr()
        ccr.cmd_archive(_args())
        assert "Nothing to archive." in capsys.readouterr().out
        assert len(cache_db.load_ccreport_archive()) == 1


class TestTheCutoffFollowsTheRateLimitHistory:
    """`ccreport limits` prices a window against the records covering its span."""

    def test_with_no_samples_the_age_bound_stands(self, corpus):
        cutoff = ccr._archive_cutoff(30)
        expected = (
            dt.datetime.now().astimezone().replace(
                hour=0, minute=0, second=0, microsecond=0)
            - dt.timedelta(days=30)
        ).timestamp()
        assert cutoff == pytest.approx(expected)

    def test_an_old_sample_pushes_the_cutoff_further_back(self, corpus):
        from ccreport import windows

        oldest = dt.datetime.now(tz=dt.UTC).timestamp() - 90 * 86400
        cache_db.record_rate_limit_snapshots([
            cache_db.RateLimitSample(
                window="session", used_pct=10.0, resets_at=oldest + 3600,
                model=None, source="native",
            )
        ], oldest)
        assert ccr._archive_cutoff(30) == pytest.approx(
            oldest - max(windows.LIMIT_WINDOW_SPAN_S.values())
        )

    def test_a_recent_sample_never_pulls_the_cutoff_forward(self, corpus):
        now = dt.datetime.now(tz=dt.UTC).timestamp()
        cache_db.record_rate_limit_snapshots([
            cache_db.RateLimitSample(
                window="session", used_pct=10.0, resets_at=now + 3600,
                model=None, source="native",
            )
        ], now)
        assert ccr._archive_cutoff(30) < now - 29 * 86400


class TestReadTimeAttributionSurvives:
    """The whole reason identity goes in raw."""

    @pytest.fixture
    def archived(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1"])
        _purge(corpus / "a.jsonl")
        ccr.cmd_archive(_args())
        capsys.readouterr()
        return corpus

    @staticmethod
    def _projects(records) -> set[str]:
        return {rec.project for rec in records}

    def test_merge_regroups_an_archived_day(self, archived):
        name = next(iter(self._projects(ccr.load_all_records())))
        cache_db.add_project_override("name", name, "merged-name")
        assert self._projects(ccr.load_all_records()) == {"merged-name"}

    def test_unmerge_puts_it_back(self, archived):
        name = next(iter(self._projects(ccr.load_all_records())))
        cache_db.add_project_override("name", name, "merged-name")
        cache_db.delete_project_override(name, "name")
        assert self._projects(ccr.load_all_records()) == {name}

    def test_adopt_claims_an_archived_span(self, archived):
        assert {r.account for r in ccr.load_all_records()} == {UNKNOWN_ACCOUNT}
        cache_db.set_adopted_account(
            dict.fromkeys(cache_db._ACCOUNT_COLS)
            | {"account_uuid": "uuid-1", "email": "me@work.example"}
        )
        assert {r.account for r in ccr.load_all_records()} == {"me@work.example"}

    def test_a_file_spanning_an_account_change_is_held_back(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when="2026-01-15T00:00:00Z",
                     project_cwd="/tmp/live", ids=["m1"])
        _write_jsonl(corpus / "b.jsonl", when="2026-01-15T23:00:00Z",
                     project_cwd="/tmp/live", ids=["m2"])
        # One file, two records, with the change log landing between them.
        (corpus / "a.jsonl").write_text(
            (corpus / "a.jsonl").read_text() + (corpus / "b.jsonl").read_text()
        )
        (corpus / "b.jsonl").unlink()
        _purge(corpus / "a.jsonl")
        conn = cache_db.get_connection()
        mid = dt.datetime(2026, 1, 15, 12, tzinfo=dt.UTC).timestamp()
        conn.execute(
            "INSERT INTO account_events (ts, account_uuid, email) VALUES (?, ?, ?)",
            (mid, "uuid-2", "later@work.example"),
        )
        conn.commit()
        ccr.cmd_archive(_args())
        assert "spans an account change" in capsys.readouterr().out
        assert cache_db.load_ccreport_archive() == []


class TestTheRestOfTheMachine:
    @pytest.fixture
    def archived(self, corpus, capsys):
        _write_jsonl(corpus / "a.jsonl", when=OLD, project_cwd="/tmp/live",
                     ids=["m1"])
        _purge(corpus / "a.jsonl")
        ccr.cmd_archive(_args())
        capsys.readouterr()
        return corpus

    def test_the_sanity_guard_counts_archived_calls(self, archived):
        conn = cache_db.get_connection()
        assert cache_db._ccr_totals(conn) == (1, 1)

    def test_the_push_does_not_offer_an_emptied_file(self, archived):
        from ccreport import push

        conn = push._read_only(cache_db.DB_PATH)
        try:
            assert push.changed_files(conn, {}) == []
        finally:
            conn.close()

    def test_the_orphan_all_time_total_still_holds(self, archived):
        from ccreport import pricing

        rows = pricing._build_orphan_alltime(
            {str(archived / "a.jsonl")}, [archived.parent],
        )
        assert sum(row[4] for row in rows) == pytest.approx(1.0)

    def test_the_archived_directory_still_names_its_project(self, archived):
        paths = [row[0] for row in cache_db.load_ccreport_file_identities()]
        assert any(p.startswith(str(archived) + "/") for p in paths)
