"""Tests for ccreport.py — record filtering, aggregation, summary rows.

`load_all_records` runs against a temp cache DB and a temp JSONL tree; nothing
here reads the real log or the real cache.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import types
from pathlib import Path

import pytest
from _narrow import present
from rich.console import Console

from ccreport import cache_db
from ccreport import ccreport as ccr

UTC = dt.UTC


def _rec(**kw):
    defaults = {
        "message_id": "m1",
        "model": "claude-opus-5",
        "tokens": ccr.TokenCounts(input=10, output=20, cache_create=30, cache_read=40),
        "timestamp": dt.datetime(2026, 6, 15, 12, tzinfo=UTC),
        "session_id": "s1",
        "project": "proj",
        "cost_usd": 1.0,
        "dedup_key": None,
    }
    return ccr.UsageRecord(**{**defaults, **kw})


def _filters(**kw):
    base = {
        "since": None,
        "until": None,
        "project_filter": None,
        "account_filter": None,
        "seen_keys": set(),
        "override": None,
        "accounts": None,
    }
    return {**base, **kw}


def _timeline(*events):
    """An AccountTimeline from (epoch, email, org) triples, uuid derived."""
    return ccr.AccountTimeline(
        [
            {
                "ts": ts,
                "account_uuid": f"uuid-{email or 'none'}",
                "email": email,
                "organization_uuid": None,
                "organization_name": org,
            }
            for ts, email, org in events
        ]
    )


class TestKeep:
    def test_bare_record_is_kept(self):
        assert ccr._keep(_rec(), **_filters()) is True

    def test_since_and_until_bound_the_window(self):
        rec = _rec(timestamp=dt.datetime(2026, 6, 15, tzinfo=UTC))
        assert (
            ccr._keep(rec, **_filters(since=dt.datetime(2026, 6, 16, tzinfo=UTC)))
            is False
        )
        assert (
            ccr._keep(rec, **_filters(until=dt.datetime(2026, 6, 14, tzinfo=UTC)))
            is False
        )
        assert (
            ccr._keep(
                rec,
                **_filters(
                    since=dt.datetime(2026, 6, 1, tzinfo=UTC),
                    until=dt.datetime(2026, 6, 30, tzinfo=UTC),
                ),
            )
            is True
        )

    def test_project_filter_is_a_case_insensitive_substring(self):
        rec = _rec(project="MacSetup")
        assert ccr._keep(rec, **_filters(project_filter="setup")) is True
        assert ccr._keep(rec, **_filters(project_filter="other")) is False

    def test_dedup_keeps_the_first_occurrence_only(self):
        seen: set[str] = set()
        f = _filters(seen_keys=seen)
        assert ccr._keep(_rec(dedup_key="dk1"), **f) is True
        assert ccr._keep(_rec(dedup_key="dk1"), **f) is False
        assert seen == {"dk1"}

    def test_a_record_with_no_dedup_key_falls_back_to_its_content(self):
        """Dk is NULL when the log carried no message id or requestId."""
        f = _filters()
        assert ccr._keep(_rec(dedup_key=None), **f) is True
        assert ccr._keep(_rec(dedup_key=None), **f) is False

    def test_one_differing_token_count_makes_it_a_different_record(self):
        f = _filters()
        assert ccr._keep(_rec(dedup_key=None), **f) is True
        chunk = _rec(
            dedup_key=None,
            tokens=ccr.TokenCounts(input=10, output=21, cache_create=30, cache_read=40),
        )
        assert ccr._keep(chunk, **f) is True

    def test_a_zero_token_record_still_dedupes_on_its_message_id(self):
        """<synthetic> rows carry no tokens but do carry an id."""
        f = _filters()
        empty = {"dedup_key": None, "model": "<synthetic>", "tokens": ccr.TokenCounts()}
        assert ccr._keep(_rec(**empty), **f) is True
        assert ccr._keep(_rec(**empty), **f) is False
        assert ccr._keep(_rec(message_id="m2", **empty), **f) is True

    def test_no_id_and_no_tokens_is_never_a_duplicate(self):
        """Session and timestamp alone are not enough to drop a record on."""
        f = _filters()
        blank = {"dedup_key": None, "message_id": "", "tokens": ccr.TokenCounts()}
        assert ccr._keep(_rec(**blank), **f) is True
        assert ccr._keep(_rec(**blank), **f) is True

    def test_override_renames_before_the_project_filter_runs(self):
        rec = _rec(project="old-name")
        kept = ccr._keep(
            rec,
            **_filters(
                project_filter="new", override=lambda _repo, _cwd, _n: "new-name"
            ),
        )
        assert kept is True
        assert rec.project == "new-name"


@pytest.fixture
def loader(tmp_path, monkeypatch):
    """load_all_records wired to a temp DB and a temp project tree."""
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cache_db, "_conn", None)
    projects = tmp_path / "projects" / "-tmp-live"
    projects.mkdir(parents=True)
    monkeypatch.setattr(
        ccr, "discover_jsonl_files", lambda: sorted(projects.glob("*.jsonl"))
    )
    monkeypatch.setattr(ccr, "_ensure_cache_valid", lambda _live_paths: None)
    # Stubbing _ensure_cache_valid out skips the meta stamp along with the
    # invalidation, and the readers return nothing without a matching schema
    # salt. Stamped here rather than inside the stub so it costs no COMMIT the
    # save-batching tests would count.
    cache_db.init_ccreport_meta(ccr.CACHE_VERSION, "test-hash")
    monkeypatch.setattr(cache_db, "get_project_overrides", list)
    yield projects
    cache_db.get_connection().close()
    cache_db._conn = None


def _write_jsonl(path: Path, *, when: str, project_cwd: str, ids: list[str]) -> None:
    lines = []
    for i, mid in enumerate(ids):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": when,
                    "sessionId": "sess-1",
                    "cwd": project_cwd,
                    "requestId": f"req-{i}",
                    "message": {
                        "id": mid,
                        "model": "claude-opus-5",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                    "costUSD": 1.0,
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")


def test_cache_invalidation_sees_every_discovered_file(loader, monkeypatch):
    """A path missing from this set gets treated as purged and left stale."""
    _write_jsonl(
        loader / "a.jsonl",
        when="2026-06-15T12:00:00Z",
        project_cwd="/tmp/live",
        ids=["msg-1"],
    )
    seen: list[set[str]] = []
    monkeypatch.setattr(ccr, "_ensure_cache_valid", seen.append)
    ccr.load_all_records()
    assert seen == [{str(loader / "a.jsonl")}]


class TestLoadAllRecordsFiltersOrphans:
    """The point of one _keep: purged-file history obeys the same filters."""

    def test_orphaned_records_survive_the_source_file_and_obey_since(self, loader):
        live = loader / "a.jsonl"
        _write_jsonl(
            live,
            when="2026-06-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-old", "msg-new"],
        )
        assert len(ccr.load_all_records()) == 2

        # Claude Code purges the JSONL; the cache still holds its records.
        live.unlink()
        orphaned = ccr.load_all_records()
        assert len(orphaned) == 2, "cached history should outlive its file"

        # A window that excludes them must exclude them on the orphan path too.
        assert ccr.load_all_records(since=dt.datetime(2026, 7, 1, tzinfo=UTC)) == []
        assert ccr.load_all_records(until=dt.datetime(2026, 6, 1, tzinfo=UTC)) == []
        assert ccr.load_all_records(project_filter="nothing-matches") == []
        assert len(ccr.load_all_records(project_filter="live")) == 2

    def test_a_live_record_is_not_counted_twice_via_the_cache(self, loader):
        _write_jsonl(
            loader / "a.jsonl",
            when="2026-06-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1"],
        )
        ccr.load_all_records()
        assert len(ccr.load_all_records()) == 1

    def test_live_and_purged_files_both_load_in_one_run(self, loader):
        """Orphans come out of the bulk load, so a mixed corpus must still add up."""
        for name, mid in (("a.jsonl", "msg-a"), ("b.jsonl", "msg-b")):
            _write_jsonl(
                loader / name,
                when="2026-06-15T12:00:00Z",
                project_cwd="/tmp/live",
                ids=[mid],
            )
        assert len(ccr.load_all_records()) == 2

        (loader / "b.jsonl").unlink()
        ids = {r.message_id for r in ccr.load_all_records()}
        assert ids == {"msg-a", "msg-b"}


class TestDateFiltersReachSql:
    """A one-day report must not build the whole corpus first (macsetup-6a2f)."""

    SINCE = dt.datetime(2026, 6, 1, tzinfo=UTC)

    def _cached_corpus(self, loader) -> None:
        _write_jsonl(
            loader / "old.jsonl",
            when="2026-01-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-old"],
        )
        _write_jsonl(
            loader / "new.jsonl",
            when="2026-06-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-new"],
        )
        ccr.load_all_records()  # fills the cache, so the next run is a pure read

    def test_the_window_is_pushed_into_the_query(self, loader, monkeypatch):
        self._cached_corpus(loader)
        asked: list[tuple] = []
        real = ccr.load_ccreport_records_in_range
        monkeypatch.setattr(
            ccr,
            "load_ccreport_records_in_range",
            lambda since_ts, until_ts: (
                asked.append((since_ts, until_ts)) or real(since_ts, until_ts)
            ),
        )

        kept = ccr.load_all_records(since=self.SINCE)
        assert [r.message_id for r in kept] == ["msg-new"]
        assert asked == [(self.SINCE.timestamp(), None)]

    def test_out_of_window_rows_are_never_deserialized(self, loader, monkeypatch):
        self._cached_corpus(loader)
        built: list[int] = []
        real = ccr._deserialize_records
        monkeypatch.setattr(
            ccr, "_deserialize_records", lambda raw: built.append(len(raw)) or real(raw)
        )

        ccr.load_all_records(since=self.SINCE)
        assert sum(built) == 1, "a record outside the window was built anyway"

    def test_an_unfiltered_load_still_reads_everything(self, loader):
        self._cached_corpus(loader)
        assert len(ccr.load_all_records()) == 2

    def test_a_project_filter_alone_leaves_the_window_open(self, loader, monkeypatch):
        """Attribution is decided at read time, so it cannot go to SQL."""
        self._cached_corpus(loader)
        asked: list[tuple] = []
        real = ccr.load_ccreport_records_in_range
        monkeypatch.setattr(
            ccr,
            "load_ccreport_records_in_range",
            lambda since_ts, until_ts: (
                asked.append((since_ts, until_ts)) or real(since_ts, until_ts)
            ),
        )

        assert len(ccr.load_all_records(project_filter="live")) == 2
        assert asked == [(None, None)]


class TestRollupRebuildReusesItsCallersWork:
    """The rebuild used to stat and re-parse the whole corpus twice (macsetup-4sx0)."""

    @pytest.fixture
    def corpus(self, loader):
        # Well before _rollup_cutoff(), so the record lands in the rollup half.
        _write_jsonl(
            loader / "a.jsonl",
            when="2026-01-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1"],
        )
        return loader

    def test_the_rebuild_refreshes_the_corpus_once(self, corpus, monkeypatch):
        seen: list[int] = []
        real = ccr._refresh_changed_files
        monkeypatch.setattr(
            ccr,
            "_refresh_changed_files",
            lambda files, meta: seen.append(len(files)) or real(files, meta),
        )

        ccr.load_all_records(use_rollups=True)  # fingerprint misses, so it rebuilds
        assert seen == [1], "the rebuild statted and re-parsed the corpus again"

    def test_the_account_log_is_read_once_per_rebuild(self, corpus, monkeypatch):
        reads: list[int] = []
        real = ccr.load_account_events
        monkeypatch.setattr(
            ccr, "load_account_events", lambda: reads.append(1) or real()
        )

        ccr.load_all_records(use_rollups=True)
        assert len(reads) == 1

    def test_the_rebuild_returns_what_a_full_load_does(self, corpus):
        """Threading the refresh through must not change which records survive."""
        assert [r.message_id for r in ccr.load_all_records(use_rollups=True)] == [
            r.message_id for r in ccr.load_all_records()
        ]


class TestJsonOutput:
    """--json streams; the document it streams must be the one it always was."""

    def _rendered(self, capsys, records, nok) -> str:
        ccr.report_json(records, nok=nok)
        return capsys.readouterr().out

    def _dumped(self, records, nok) -> str:
        return json.dumps([ccr._json_entry(r, nok) for r in records], indent=2) + "\n"

    def test_the_streamed_document_matches_a_single_dumps(self, capsys):
        nok = ccr.NokCtx()
        records = [_rec(message_id=f"m{i}") for i in range(3)]
        assert self._rendered(capsys, records, nok) == self._dumped(records, nok)

    def test_one_record_matches_too(self, capsys):
        nok = ccr.NokCtx()
        records = [_rec()]
        assert self._rendered(capsys, records, nok) == self._dumped(records, nok)

    def test_an_empty_corpus_is_an_empty_array(self, capsys):
        assert self._rendered(capsys, [], ccr.NokCtx()) == "[]\n"

    def test_the_nok_keys_still_ride_along(self, capsys):
        nok = ccr.NokCtx({"2026-06-15": 10.0}, "2026-06-15", True)
        records = [_rec()]
        out = self._rendered(capsys, records, nok)
        assert out == self._dumped(records, nok)
        assert json.loads(out)[0]["cost_nok"] == 12.5


def test_the_script_hash_is_memoized():
    """A rollup run asks twice, and no process edits its own source mid-run."""
    assert ccr._script_hash() is ccr._script_hash()


class TestSaveBatching:
    """A full re-parse commits per batch, not per file (macsetup-92y0)."""

    def _commits(self, conn):
        seen: list[str] = []
        conn.set_trace_callback(seen.append)
        return lambda: sum(1 for s in seen if s.strip().upper().startswith("COMMIT"))

    def _corpus(self, loader, n: int) -> None:
        for i in range(n):
            _write_jsonl(
                loader / f"f{i}.jsonl",
                when="2026-06-15T12:00:00Z",
                project_cwd="/tmp/live",
                ids=[f"msg-{i}"],
            )

    def test_a_whole_corpus_under_one_batch_commits_once(self, loader):
        self._corpus(loader, 12)
        commits = self._commits(cache_db.get_connection())
        assert len(ccr.load_all_records()) == 12
        assert commits() == 1

    def test_the_batch_size_bounds_each_transaction(self, loader, monkeypatch):
        monkeypatch.setattr(ccr, "_SAVE_BATCH", 5)
        self._corpus(loader, 12)
        commits = self._commits(cache_db.get_connection())
        ccr.load_all_records()
        assert commits() == 3  # 5 + 5 + the trailing 2

    def test_batching_caches_every_file_and_then_writes_nothing(
        self, loader, monkeypatch
    ):
        monkeypatch.setattr(ccr, "_SAVE_BATCH", 5)
        self._corpus(loader, 12)
        ccr.load_all_records()
        file_meta, by_file = cache_db.bulk_load_ccreport_cache()
        assert len(file_meta) == 12
        assert sum(len(v) for v in by_file.values()) == 12

        commits = self._commits(cache_db.get_connection())
        assert len(ccr.load_all_records()) == 12
        assert commits() == 0, "an all-hit run must not write"


class _FlakyFile:
    """A file that hands back *lines*, then dies mid-read like a bad disk."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        yield from self._lines
        msg = "read failed mid-file"
        raise OSError(msg)


class TestPartialParseNeverOverwritesTheCache:
    """A truncated parse saved over a complete cache entry is silent loss."""

    def test_parse_jsonl_file_propagates_a_mid_file_read_error(
        self, loader, monkeypatch
    ):
        path = loader / "a.jsonl"
        _write_jsonl(
            path,
            when="2026-06-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1", "msg-2"],
        )
        first_line = path.read_bytes().splitlines()[0]
        monkeypatch.setattr(
            ccr, "open", lambda *_a, **_kw: _FlakyFile([first_line]), raising=False
        )
        with pytest.raises(OSError):
            ccr.parse_jsonl_file(path)

    def test_a_failed_reparse_leaves_the_cached_records_whole(
        self, loader, monkeypatch
    ):
        path = loader / "a.jsonl"
        _write_jsonl(
            path,
            when="2026-06-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1", "msg-2"],
        )
        assert len(ccr.load_all_records()) == 2

        # Grow the file so its fingerprint misses and a re-parse is forced,
        # then make that re-parse fail after the first line.
        _write_jsonl(
            path,
            when="2026-06-15T12:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1", "msg-2", "msg-3"],
        )
        real_open, failing = open, [True]

        def flaky_open(*args, **kwargs):
            if failing[0]:
                failing[0] = False
                return _FlakyFile(path.read_bytes().splitlines()[:1])
            return real_open(*args, **kwargs)

        monkeypatch.setattr(ccr, "open", flaky_open, raising=False)
        # This run under-reports the unreadable file...
        assert ccr.load_all_records() == []
        # ...but its cache entry is untouched, so nothing is lost for good.
        _meta, by_file = cache_db.bulk_load_ccreport_cache()
        assert len(by_file[str(path)]) == 2
        assert len(ccr.load_all_records()) == 3


class TestExplicitJsonNulls:
    """A key present with JSON null defeats a get() default and hits NOT NULL."""

    @staticmethod
    def _write_null_jsonl(path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-06-15T12:00:00Z",
                    "sessionId": None,
                    "cwd": "/tmp/live",
                    "requestId": "req-0",
                    "message": {
                        "id": "msg-null",
                        "model": None,
                        "usage": {
                            "input_tokens": None,
                            "output_tokens": None,
                            "cache_creation_input_tokens": None,
                            "cache_read_input_tokens": None,
                        },
                    },
                }
            )
            + "\n"
        )

    def test_nulls_parse_to_the_intended_defaults(self, loader):
        path = loader / "nulls.jsonl"
        self._write_null_jsonl(path)
        (rec,) = ccr.parse_jsonl_file(path)
        assert rec.session_id == path.stem
        assert rec.model == "unknown"
        assert rec.tokens.total == 0
        assert (rec.tokens.input, rec.tokens.output) == (0, 0)
        assert (rec.tokens.cache_create, rec.tokens.cache_read) == (0, 0)

    def test_a_record_of_nulls_round_trips_through_the_cache(self, loader):
        """One IntegrityError aborts the whole file's insert, every run."""
        self._write_null_jsonl(loader / "nulls.jsonl")
        assert len(ccr.load_all_records()) == 1
        _meta, by_file = cache_db.bulk_load_ccreport_cache()
        (cached,) = by_file[str(loader / "nulls.jsonl")]
        assert cached["sid"] == "nulls"
        assert cached["model"] == "unknown"
        assert cached["t"] == [0, 0, 0, 0]

    def test_a_null_record_does_not_take_its_file_down_with_it(self, loader):
        """The good records in the same file must still land."""
        path = loader / "mixed.jsonl"
        good = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-06-15T12:00:00Z",
                "sessionId": "sess-1",
                "cwd": "/tmp/live",
                "requestId": "req-1",
                "message": {
                    "id": "msg-good",
                    "model": "claude-opus-5",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        )
        bad = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-06-15T12:01:00Z",
                "sessionId": None,
                "cwd": "/tmp/live",
                "requestId": "req-2",
                "message": {
                    "id": "msg-bad",
                    "model": None,
                    "usage": {"input_tokens": None},
                },
            }
        )
        path.write_text(good + "\n" + bad + "\n")
        assert len(ccr.load_all_records()) == 2


class TestOverrideReachesPurgedHistory:
    """Orphan records carry no repo and no cwd; only a name can reach them."""

    @staticmethod
    def _fn(monkeypatch, *rules):
        # The builder reads the table through cache_db at call time, so that is
        # where the stub goes — pricing calls the same builder.
        monkeypatch.setattr(
            cache_db,
            "get_project_overrides",
            lambda: [
                {"id": i, "match_kind": k, "match_value": v, "target": t}
                for i, (k, v, t) in enumerate(rules, 1)
            ],
        )
        return present(ccr._build_override_fn())

    def test_no_rules_costs_the_hot_loop_nothing(self, monkeypatch):
        monkeypatch.setattr(cache_db, "get_project_overrides", list)
        assert ccr._build_override_fn() is None

    def test_a_remote_rule_follows_its_live_records_onto_the_orphans(self, monkeypatch):
        fn = self._fn(monkeypatch, ("remote", "github.com/org/foo", "bar"))
        assert fn("github.com/org/foo", "/tmp/foo", "foo") == "bar"
        assert fn(None, None, "foo") == "bar", "orphan of the same repo"
        assert fn(None, None, "unrelated") == "unrelated"

    def test_a_cwd_prefix_rule_reaches_orphans_under_its_repo_name(self, monkeypatch):
        under_root = str(Path.home() / "git" / "macsetup")
        fn = self._fn(monkeypatch, ("cwd_prefix", under_root, "tools"))
        assert fn(None, under_root + "/claude", "macsetup") == "tools"
        assert fn(None, None, "macsetup") == "tools"

    def test_a_cwd_prefix_outside_every_repo_root_implies_its_basename(
        self, monkeypatch
    ):
        fn = self._fn(monkeypatch, ("cwd_prefix", "/tmp/live/", "archive"))
        assert fn(None, None, "live") == "archive"
        # Named after its own subdirectory, so still out of reach by design.
        assert fn(None, None, "sub") == "sub"

    def test_a_record_with_a_cwd_is_not_treated_as_an_orphan(self, monkeypatch):
        """It has a signal; matching it by implied name would regroup live rows."""
        fn = self._fn(monkeypatch, ("remote", "github.com/org/foo", "bar"))
        assert fn(None, "/somewhere/else/foo", "foo") == "foo"

    def test_first_rule_in_insertion_order_still_wins_for_orphans(self, monkeypatch):
        fn = self._fn(
            monkeypatch,
            ("remote", "github.com/org/foo", "first"),
            ("name", "foo", "second"),
        )
        assert fn("github.com/org/foo", "/tmp/foo", "foo") == "first"
        assert fn(None, None, "foo") == "first", "orphan lands where live rows land"

        flipped = self._fn(
            monkeypatch,
            ("name", "foo", "second"),
            ("remote", "github.com/org/foo", "first"),
        )
        assert flipped("github.com/org/foo", "/tmp/foo", "foo") == "second"
        assert flipped(None, None, "foo") == "second"

    def test_implied_name_mirrors_how_parse_derives_a_project(self):
        assert ccr._implied_name("remote", "github.com/org/foo") == "foo"
        assert ccr._implied_name("cwd_prefix", "/tmp/live/") == "live"
        assert ccr._implied_name("name", "foo") is None


class TestMergeWarnsAboutUnreachableHistory:
    def _orphan_rows(self, n: int) -> None:
        cache_db.save_ccreport_file(
            "/gone.jsonl",
            1,
            1,
            [
                {
                    "mid": f"m{i}",
                    "model": "claude-opus-5",
                    "ts": 1.0,
                    "sid": "s",
                    "project": "foo",
                    "cwd": None,
                    "repo": None,
                    "t": [1, 1, 1, 1],
                }
                for i in range(n)
            ],
        )

    def _merge(self, kind: str, source: str, target: str = "bar") -> None:
        ccr.cmd_overrides(
            types.SimpleNamespace(
                command="merge", kind=kind, source=source, target=target
            )
        )

    def test_a_remote_rule_reports_what_it_cannot_reach(self, capsys):
        self._orphan_rows(3)
        self._merge("remote", "github.com/org/foo")
        err = capsys.readouterr().err
        assert "3 cached record(s)" in err
        assert "'foo'" in err
        assert "ccreport merge <that-name> bar" in err

    def test_a_name_rule_needs_no_warning(self, capsys):
        self._orphan_rows(3)
        self._merge("name", "foo")
        assert capsys.readouterr().err == ""

    def test_nothing_to_warn_about_when_every_record_has_a_cwd(self, capsys):
        cache_db.save_ccreport_file(
            "/live.jsonl",
            1,
            1,
            [
                {
                    "mid": "m1",
                    "model": "claude-opus-5",
                    "ts": 1.0,
                    "sid": "s",
                    "project": "foo",
                    "cwd": "/tmp/foo",
                    "repo": "github.com/org/foo",
                    "t": [1, 1, 1, 1],
                },
            ],
        )
        self._merge("cwd_prefix", "/tmp/foo")
        assert capsys.readouterr().err == ""


class TestAggregationAndRows:
    def test_bucket_by_groups_and_counts(self):
        recs = [
            _rec(project="a", cost_usd=1.0),
            _rec(project="a", cost_usd=2.0),
            _rec(project="b", cost_usd=4.0),
        ]
        buckets = ccr._bucket_by(recs, lambda r: r.project, ccr.NokCtx())
        assert buckets["a"].cost == 3.0
        assert buckets["a"].count == 2
        assert buckets["b"].cost == 4.0
        assert buckets["a"].models == {"claude-opus-5": 3.0}

    def test_synthetic_model_is_counted_but_not_listed(self):
        buckets = ccr._bucket_by(
            [_rec(model="<synthetic>")], lambda _r: "k", ccr.NokCtx()
        )
        assert buckets["k"].count == 1
        assert buckets["k"].models == {}

    def test_agg_bucket_iadd_folds_every_field(self):
        a = ccr.AggBucket(
            tokens=ccr.TokenCounts(1, 2, 3, 4),
            cost=1.0,
            cost_nok=10.0,
            models={"x": 1.0},
            count=1,
        )
        b = ccr.AggBucket(
            tokens=ccr.TokenCounts(1, 1, 1, 1),
            cost=2.0,
            cost_nok=20.0,
            nok_estimated=True,
            models={"x": 0.5, "y": 1.5},
            count=2,
        )
        a += b
        assert (a.cost, a.cost_nok, a.count) == (3.0, 30.0, 3)
        assert a.tokens.total == 14
        assert a.models == {"x": 1.5, "y": 1.5}
        assert a.nok_estimated is True

    def test_computed_cost_is_memoized_without_touching_cost_usd(self):
        # opus-5 has no pricing at the fixture's June timestamp; sonnet-5 does.
        rec = _rec(cost_usd=None, model="claude-sonnet-5")
        first = ccr.record_cost(rec)
        assert first > 0
        assert ccr.record_cost(rec) == first
        # cost_usd means "the log gave us this"; a computed cost must not land
        # there, because that field is what gets written to the SQLite cache.
        assert rec.cost_usd is None
        assert "_cost" not in ccr._serialize_records([rec])[0]

    def test_logged_cost_usd_still_wins_over_computation(self):
        assert ccr.record_cost(_rec(cost_usd=0.25)) == 0.25

    def test_models_cell_lists_cost_priciest_first(self):
        cell = ccr._models_cell(
            {"claude-haiku-4-5": 0.05, "claude-opus-5": 174.0, "claude-sonnet-5": 1.5}
        )
        assert str(cell) == "opus-5 ($174), sonnet-5 ($1.50), haiku-4-5 ($0.0500)"

    def test_models_cell_breaks_cost_ties_by_name(self):
        cell = ccr._models_cell({"claude-sonnet-5": 2.0, "claude-haiku-4-5": 2.0})
        assert str(cell) == "haiku-4-5 ($2.00), sonnet-5 ($2.00)"

    def test_nok_ctx_enabled_follows_the_rates(self):
        assert ccr.NokCtx().enabled is False
        assert ccr.NokCtx({"2026-06-15": 10.0}).enabled is True
        assert ccr.NokCtx(mva=True).label == "NOK+MVA"
        assert ccr.NokCtx(mva=False).label == "NOK"

    def test_mva_multiplies_the_converted_cost(self):
        rates = {"2026-06-15": 10.0}
        rec = _rec()
        with_mva, _ = ccr.record_cost_nok(
            rec, 2.0, ccr.NokCtx(rates, "2026-06-15", True)
        )
        without, _ = ccr.record_cost_nok(
            rec, 2.0, ccr.NokCtx(rates, "2026-06-15", False)
        )
        assert without == pytest.approx(20.0)
        assert with_mva == pytest.approx(25.0)

    @pytest.mark.parametrize("width", [200, 90])
    @pytest.mark.parametrize("rates", [{}, {"2026-06-15": 10.0}])
    def test_summary_row_cell_count_matches_the_columns(self, width, rates):
        """The padding is derived, so every summary row fills the table exactly."""
        buf = io.StringIO()
        ccr.console = Console(file=buf, width=width, no_color=True)
        narrow = width < ccr.NARROW_WIDTH
        nok = ccr.NokCtx(rates, "2026-06-15", True)
        table = ccr._make_report_table("t", "Date", narrow=narrow, nok=nok)
        ccr._summary_row(table, "AVG", 1.0, narrow=narrow, nok=nok, note="note")
        assert len(table.rows) == 1
        for column in table.columns:
            assert len(column._cells) == 1, f"{column.header} has no cell in the row"


class TestAccountTimeline:
    """Attribution is a lookup back from a record's time into the change log."""

    def test_a_record_before_the_first_event_is_unknown(self):
        tl = _timeline((1000.0, "me@work.example", "Work AS"))
        before = dt.datetime.fromtimestamp(999.0, UTC)
        assert tl.label_at(before) == ccr.UNKNOWN_ACCOUNT

    def test_an_empty_log_makes_everything_unknown(self):
        assert _timeline().label_at(_rec().timestamp) == ccr.UNKNOWN_ACCOUNT

    def test_a_record_at_the_event_itself_belongs_to_it(self):
        tl = _timeline((1000.0, "me@work.example", "Work AS"))
        assert tl.label_at(dt.datetime.fromtimestamp(1000.0, UTC)) == "me@work.example"

    def test_records_land_on_the_account_in_force_when_written(self):
        tl = _timeline(
            (1000.0, "me@work.example", "Work AS"),
            (2000.0, "me@home.example", "Personal"),
        )
        at = lambda ts: tl.label_at(dt.datetime.fromtimestamp(ts, UTC))  # noqa: E731
        assert at(1500.0) == "me@work.example"
        assert at(2500.0) == "me@home.example"

    def test_the_lookup_is_zone_independent(self):
        """The log holds epochs; a record's timestamp is aware, so both agree."""
        tl = _timeline((1000.0, "me@work.example", "Work AS"))
        oslo = dt.timezone(dt.timedelta(hours=2))
        assert tl.label_at(dt.datetime.fromtimestamp(1500.0, oslo)) == "me@work.example"
        assert (
            tl.label_at(dt.datetime.fromtimestamp(500.0, oslo)) == ccr.UNKNOWN_ACCOUNT
        )

    def test_one_email_under_two_orgs_is_two_labels(self):
        tl = _timeline(
            (1000.0, "me@example.com", "Work AS"),
            (2000.0, "me@example.com", "Personal"),
        )
        at = lambda ts: tl.label_at(dt.datetime.fromtimestamp(ts, UTC))  # noqa: E731
        assert at(1500.0) == "me@example.com (Work AS)"
        assert at(2500.0) == "me@example.com (Personal)"

    def test_one_email_under_one_org_stays_bare(self):
        tl = _timeline(
            (1000.0, "me@example.com", "Work AS"),
            (2000.0, "you@example.com", "Work AS"),
        )
        assert tl.label_at(dt.datetime.fromtimestamp(1500.0, UTC)) == "me@example.com"

    def test_an_event_with_no_email_falls_back_to_its_uuid(self):
        tl = _timeline((1000.0, None, None))
        assert tl.label_at(dt.datetime.fromtimestamp(1500.0, UTC)) == "uuid-none"


class TestAccountTimelineTiers:
    """The tier lookup rides the same events as the label lookup."""

    def _tl(self, *events):
        """A timeline from (ts, user_tier, org_tier) triples."""
        return ccr.AccountTimeline(
            [
                {
                    "ts": ts,
                    "account_uuid": "u1",
                    "email": "me@work.example",
                    "organization_uuid": "o1",
                    "organization_name": "Work AS",
                    "seat_tier": None,
                    "user_rate_limit_tier": user,
                    "organization_rate_limit_tier": org,
                }
                for ts, user, org in events
            ]
        )

    def _at(self, tl, ts):
        return tl.tier_at(dt.datetime.fromtimestamp(ts, UTC))

    def test_the_tier_in_force_is_the_newest_event_at_or_before(self):
        tl = self._tl(
            (1000.0, "default_claude_max_5x", "default_raven"),
            (2000.0, "default_claude_max_20x", "default_raven"),
        )
        assert self._at(tl, 1500.0) == "default_claude_max_5x"
        assert self._at(tl, 2500.0) == "default_claude_max_20x"

    def test_a_moment_before_the_first_event_has_no_tier(self):
        tl = self._tl((1000.0, "default_claude_max_5x", None))
        assert self._at(tl, 999.0) is None

    def test_an_event_from_before_the_tier_columns_has_no_tier(self):
        """Its columns read NULL, which is absent rather than a change."""
        tl = self._tl((1000.0, None, None))
        assert self._at(tl, 1500.0) is None

    def test_the_org_pool_answers_when_no_user_tier_was_assigned(self):
        tl = self._tl((1000.0, None, "default_claude_max_20x"))
        assert self._at(tl, 1500.0) == "default_claude_max_20x"

    def test_an_empty_log_has_no_tier(self):
        assert self._at(self._tl(), 1500.0) is None


class TestKeepAttributesAccounts:
    """The stamp and the --account filter share _keep with every other filter."""

    def _tl(self):
        return _timeline(
            (1000.0, "me@work.example", "Work AS"),
            (2000.0, "me@home.example", "Personal"),
        )

    def _at(self, ts):
        return _rec(timestamp=dt.datetime.fromtimestamp(ts, UTC))

    def test_no_timeline_leaves_the_default_alone(self):
        rec = self._at(1500.0)
        assert ccr._keep(rec, **_filters()) is True
        assert rec.account == ccr.UNKNOWN_ACCOUNT

    def test_the_record_is_stamped_before_it_is_returned(self):
        rec = self._at(1500.0)
        assert ccr._keep(rec, **_filters(accounts=self._tl())) is True
        assert rec.account == "me@work.example"

    def test_the_filter_is_a_case_insensitive_substring(self):
        f = {"accounts": self._tl(), "account_filter": "WORK"}
        assert ccr._keep(self._at(1500.0), **_filters(**f)) is True
        assert ccr._keep(self._at(2500.0), **_filters(**f)) is False

    def test_records_predating_the_log_match_the_unknown_bucket(self):
        f = _filters(accounts=self._tl(), account_filter="unknown")
        assert ccr._keep(self._at(500.0), **f) is True

    def test_the_stamp_lands_before_the_filter_runs(self):
        """Same ordering rule as the project override: rename, then match."""
        rec = self._at(2500.0)
        assert (
            ccr._keep(rec, **_filters(accounts=self._tl(), account_filter="home"))
            is True
        )
        assert rec.account == "me@home.example"


class TestAccountAttributionEndToEnd:
    """Attribution is read-time: no re-parse, and the cached half is stamped too."""

    def _log(self, *events):
        for ts, uuid, email in events:
            cache_db.record_account_event(
                {"accountUuid": uuid, "emailAddress": email, "organizationName": "Org"},
                now=ts,
            )

    def _epoch(self, iso: str) -> float:
        return dt.datetime.fromisoformat(iso).timestamp()

    def test_a_mid_session_switch_splits_one_session_file(self, loader):
        """/login mid-session is the case the change log exists for."""
        _write_jsonl(
            loader / "a.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1"],
        )
        _write_jsonl(
            loader / "b.jsonl",
            when="2026-06-15T14:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-2"],
        )
        self._log(
            (self._epoch("2026-06-15T09:00:00Z"), "u-work", "me@work.example"),
            (self._epoch("2026-06-15T12:00:00Z"), "u-home", "me@home.example"),
        )
        by_id = {r.message_id: r.account for r in ccr.load_all_records()}
        assert by_id == {"msg-1": "me@work.example", "msg-2": "me@home.example"}

    def test_records_older_than_the_log_report_as_unknown(self, loader):
        _write_jsonl(
            loader / "a.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1"],
        )
        self._log((self._epoch("2026-06-16T00:00:00Z"), "u-work", "me@work.example"))
        assert [r.account for r in ccr.load_all_records()] == [ccr.UNKNOWN_ACCOUNT]

    def test_a_later_event_re_attributes_without_a_re_parse(self, loader):
        """Nothing is frozen at parse time, so the cached second run agrees."""
        _write_jsonl(
            loader / "a.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1"],
        )
        assert [r.account for r in ccr.load_all_records()] == [ccr.UNKNOWN_ACCOUNT]
        self._log((self._epoch("2026-06-15T09:00:00Z"), "u-work", "me@work.example"))
        assert [r.account for r in ccr.load_all_records()] == ["me@work.example"]

    def test_the_account_filter_drops_the_other_account(self, loader):
        _write_jsonl(
            loader / "a.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1"],
        )
        _write_jsonl(
            loader / "b.jsonl",
            when="2026-06-15T14:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-2"],
        )
        self._log(
            (self._epoch("2026-06-15T09:00:00Z"), "u-work", "me@work.example"),
            (self._epoch("2026-06-15T12:00:00Z"), "u-home", "me@home.example"),
        )
        kept = ccr.load_all_records(account_filter="home")
        assert [r.message_id for r in kept] == ["msg-2"]

    def test_the_serialized_cache_carries_no_account(self, loader):
        """Adding one would mean a CACHE_VERSION bump and a corpus re-parse."""
        _write_jsonl(
            loader / "a.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-1"],
        )
        self._log((self._epoch("2026-06-15T09:00:00Z"), "u-work", "me@work.example"))
        records = ccr.load_all_records()
        assert records[0].account == "me@work.example"
        assert "account" not in ccr._serialize_records(records)[0]


class TestReportAccount:
    """The table is report_project's shape, bucketed by account."""

    def _render(self, records, width=200):
        buf = io.StringIO()
        ccr.console = Console(file=buf, width=width, no_color=True)
        ccr.report_account(records, nok=ccr.NokCtx())
        return buf.getvalue()

    def test_each_account_gets_a_row_and_a_total(self):
        out = self._render(
            [
                _rec(account="me@work.example", cost_usd=3.0, message_id="m1"),
                _rec(account="me@home.example", cost_usd=1.0, message_id="m2"),
            ]
        )
        assert "Accounts (2)" in out
        assert "me@work.example" in out
        assert "me@home.example" in out
        assert "TOTAL" in out
        # Priciest first, same as the project report.
        assert out.index("me@work.example") < out.index("me@home.example")

    def test_unattributed_records_get_their_own_bucket(self):
        out = self._render([_rec(), _rec(account="me@work.example", message_id="m2")])
        assert ccr.UNKNOWN_ACCOUNT in out

    @pytest.mark.parametrize("width", [200, 90])
    @pytest.mark.parametrize("rates", [{}, {"2026-06-15": 10.0}])
    def test_every_column_gets_a_cell_in_every_row(self, monkeypatch, width, rates):
        """Same derived-padding contract the other reports are held to."""
        buf = io.StringIO()
        ccr.console = Console(file=buf, width=width, no_color=True)
        tables = []
        monkeypatch.setattr(ccr, "_print_report", tables.append)
        ccr.report_account(
            [
                _rec(account="me@work.example", message_id="m1"),
                _rec(account="me@home.example", message_id="m2"),
            ],
            nok=ccr.NokCtx(rates, "2026-06-15", True),
        )
        (table,) = tables
        # Two account rows, TOTAL, AVERAGE.
        assert len(table.rows) == 4
        for column in table.columns:
            assert len(column._cells) == 4, f"{column.header} is short a cell"


class TestConfirm:
    """Anything but an explicit yes is a no."""

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ("y", True),
            ("Y", True),
            ("yes", True),
            ("  YES  ", True),
            ("", False),
            ("n", False),
            ("no", False),
            ("sure", False),
            ("yy", False),
        ],
    )
    def test_only_yes_is_yes(self, monkeypatch, answer, expected):
        monkeypatch.setattr("builtins.input", lambda _p: answer)
        assert ccr._confirm("Proceed?") is expected

    @pytest.mark.parametrize("err", [EOFError, KeyboardInterrupt])
    def test_a_closed_stdin_answers_no(self, monkeypatch, err):
        def _raise(_p):
            raise err

        monkeypatch.setattr("builtins.input", _raise)
        assert ccr._confirm("Proceed?") is False


class TestAdopt:
    """One backdated row claims every record older than the first capture."""

    @pytest.fixture(autouse=True)
    def _never_prompts_by_accident(self, monkeypatch):
        """A test that reaches the prompt is a test that would hang in CI."""

        def _boom(_p):
            msg = "cmd_adopt asked for confirmation unexpectedly"
            raise AssertionError(msg)

        monkeypatch.setattr("builtins.input", _boom)

    def _args(self, **kw):
        return types.SimpleNamespace(**{"remove": False, "yes": True, **kw})

    def _epoch(self, iso: str) -> float:
        return dt.datetime.fromisoformat(iso).timestamp()

    def _capture(self, ts: float, uuid: str, email: str, org: str = "Org"):
        cache_db.record_account_event(
            {"accountUuid": uuid, "emailAddress": email, "organizationName": org},
            now=ts,
        )

    def _corpus(self, loader):
        """One record before the first capture and one after it."""
        _write_jsonl(
            loader / "old.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-old"],
        )
        _write_jsonl(
            loader / "new.jsonl",
            when="2026-06-15T14:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-new"],
        )
        self._capture(self._epoch("2026-06-15T12:00:00Z"), "u-work", "me@work.example")

    def _accounts(self):
        return {r.message_id: r.account for r in ccr.load_all_records()}

    def test_pre_capture_records_are_claimed_and_later_ones_are_not(
        self, loader, capsys
    ):
        self._corpus(loader)
        assert self._accounts() == {
            "msg-old": ccr.UNKNOWN_ACCOUNT,
            "msg-new": "me@work.example",
        }
        ccr.cmd_adopt(self._args())
        capsys.readouterr()
        assert self._accounts() == {
            "msg-old": "me@work.example",
            "msg-new": "me@work.example",
        }

    def test_the_claim_only_reaches_back_and_never_overrides_an_event(
        self, loader, capsys
    ):
        """Every capture keeps its own span; the adoption gets what is left."""
        self._corpus(loader)  # msg-old 10:00, msg-new 14:00, u-work captured 12:00
        _write_jsonl(
            loader / "later.jsonl",
            when="2026-06-15T18:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-later"],
        )
        self._capture(self._epoch("2026-06-15T16:00:00Z"), "u-home", "me@home.example")
        ccr.cmd_adopt(self._args())  # copies u-home, the newest capture
        capsys.readouterr()
        assert self._accounts() == {
            "msg-old": "me@home.example",  # pre-capture: the adoption
            "msg-new": "me@work.example",  # between the two captures
            "msg-later": "me@home.example",  # after the switch
        }

    def test_remove_restores_unknown(self, loader, capsys):
        self._corpus(loader)
        ccr.cmd_adopt(self._args())
        ccr.cmd_adopt(self._args(remove=True))
        assert "Removed" in capsys.readouterr().out
        assert self._accounts()["msg-old"] == ccr.UNKNOWN_ACCOUNT

    def test_remove_is_idempotent_and_exits_zero(self, loader, capsys):
        self._corpus(loader)
        ccr.cmd_adopt(self._args(remove=True))  # must not raise SystemExit
        assert "Nothing to remove" in capsys.readouterr().out

    def test_an_empty_capture_log_is_refused(self, loader, capsys):
        _write_jsonl(
            loader / "old.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-old"],
        )
        with pytest.raises(SystemExit) as exc:
            ccr.cmd_adopt(self._args())
        assert exc.value.code == 1
        assert "No account has been captured yet" in capsys.readouterr().err

    def test_an_adoption_alone_does_not_count_as_a_capture(self, loader, capsys):
        """Otherwise an adoption could re-adopt itself out of thin air."""
        self._corpus(loader)
        ccr.cmd_adopt(self._args())
        capsys.readouterr()
        cache_db.get_connection().execute("DELETE FROM account_events WHERE ts > 0")
        with pytest.raises(SystemExit):
            ccr.cmd_adopt(self._args())

    def test_re_adopting_the_same_account_is_a_no_op(self, loader, capsys):
        self._corpus(loader)
        ccr.cmd_adopt(self._args())
        capsys.readouterr()
        ccr.cmd_adopt(self._args())
        assert "already adopted" in capsys.readouterr().out

    def test_a_new_capture_replaces_the_existing_adoption(self, loader, capsys):
        self._corpus(loader)
        ccr.cmd_adopt(self._args())
        capsys.readouterr()
        self._capture(self._epoch("2026-06-15T16:00:00Z"), "u-home", "me@home.example")
        ccr.cmd_adopt(self._args())
        out = capsys.readouterr().out
        assert "Currently adopted under me@work.example (Org)" in out
        assert present(cache_db.read_adopted_account())["email"] == "me@home.example"
        assert self._accounts()["msg-old"] == "me@home.example"

    def test_the_preview_still_counts_the_covered_records_when_replacing(
        self,
        loader,
        capsys,
    ):
        """A count of the current 'unknown' bucket would read as zero here."""
        self._corpus(loader)
        ccr.cmd_adopt(self._args())
        capsys.readouterr()
        self._capture(self._epoch("2026-06-15T16:00:00Z"), "u-home", "me@home.example")
        ccr.cmd_adopt(self._args())
        assert "Adopt 1 record(s)" in capsys.readouterr().out

    def test_nothing_older_than_the_first_capture_means_nothing_to_adopt(
        self,
        loader,
        capsys,
    ):
        _write_jsonl(
            loader / "new.jsonl",
            when="2026-06-15T14:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-new"],
        )
        self._capture(self._epoch("2026-06-15T12:00:00Z"), "u-work", "me@work.example")
        ccr.cmd_adopt(self._args())
        assert "nothing to adopt" in capsys.readouterr().out
        assert cache_db.read_adopted_account() is None

    def test_the_preview_names_the_account_and_the_cost(self, loader, capsys):
        self._corpus(loader)
        ccr.cmd_adopt(self._args())
        out = capsys.readouterr().out
        assert "Adopt 1 record(s) ($1.00) predating account capture" in out
        assert "under me@work.example (Org)" in out

    def test_declining_the_prompt_writes_nothing(self, loader, monkeypatch, capsys):
        self._corpus(loader)
        monkeypatch.setattr("builtins.input", lambda _p: "n")
        ccr.cmd_adopt(self._args(yes=False))
        assert "Aborted." in capsys.readouterr().out
        assert cache_db.read_adopted_account() is None

    def test_accepting_the_prompt_writes(self, loader, monkeypatch, capsys):
        self._corpus(loader)
        monkeypatch.setattr("builtins.input", lambda _p: "y")
        ccr.cmd_adopt(self._args(yes=False))
        capsys.readouterr()
        assert present(cache_db.read_adopted_account())["email"] == "me@work.example"

    def test_the_claim_copies_the_identity_and_no_tier(self, loader, capsys):
        """What tier that history ran under is unknown, and a copy would fake it."""
        _write_jsonl(
            loader / "old.jsonl",
            when="2026-06-15T10:00:00Z",
            project_cwd="/tmp/live",
            ids=["msg-old"],
        )
        cache_db.record_account_event(
            {
                "accountUuid": "u-work",
                "emailAddress": "me@work.example",
                "organizationName": "Org",
                "seatTier": "team_tier_1",
                "userRateLimitTier": "default_claude_max_5x",
                "organizationRateLimitTier": "default_raven",
            },
            now=self._epoch("2026-06-15T12:00:00Z"),
        )
        ccr.cmd_adopt(self._args())
        capsys.readouterr()
        adopted = present(cache_db.read_adopted_account())
        assert adopted["email"] == "me@work.example"
        assert [adopted[c] for c in ccr._ACCOUNT_TIER_COLS] == [None, None, None]

    def test_remove_never_prompts(self, loader, capsys):
        """The autouse fixture makes a stray prompt an error, not a hang."""
        self._corpus(loader)
        ccr.cmd_adopt(self._args())
        capsys.readouterr()
        ccr.cmd_adopt(self._args(remove=True, yes=False))
        assert "Removed" in capsys.readouterr().out


class TestPreCaptureRecords:
    """The preview's record set, independent of whether an adoption exists."""

    def _events(self, *ts):
        return [
            {
                "ts": t,
                "account_uuid": "u",
                "email": "e",
                "organization_uuid": None,
                "organization_name": None,
            }
            for t in ts
        ]

    def _at(self, ts):
        return _rec(timestamp=dt.datetime.fromtimestamp(ts, UTC))

    def test_no_capture_covers_nothing(self):
        """With only an adoption row there is no boundary to be older than."""
        recs = [self._at(500.0)]
        assert ccr._pre_capture_records(recs, self._events(ccr.ADOPTED_TS)) == []
        assert ccr._pre_capture_records(recs, []) == []

    def test_the_boundary_is_the_first_capture_not_the_adoption(self):
        recs = [self._at(500.0), self._at(1500.0)]
        events = self._events(ccr.ADOPTED_TS, 1000.0, 2000.0)
        covered = ccr._pre_capture_records(recs, events)
        assert [r.timestamp.timestamp() for r in covered] == [500.0]

    def test_a_record_at_the_capture_belongs_to_the_capture(self):
        recs = [self._at(1000.0)]
        assert ccr._pre_capture_records(recs, self._events(1000.0)) == []


class TestSameAccount:
    """Identity, not the rendered description, decides whether to replace."""

    def _row(self, **kw):
        base = {
            "ts": 0.0,
            "account_uuid": "u1",
            "email": "me@example.com",
            "organization_uuid": "o1",
            "organization_name": "Org",
        }
        return {**base, **kw}

    def test_the_capture_time_is_not_part_of_the_identity(self):
        assert ccr._same_account(self._row(ts=0.0), self._row(ts=500.0)) is True

    def test_a_different_uuid_behind_the_same_address_is_a_different_account(self):
        """Both render as 'me@example.com (Org)'; only the identity separates them."""
        other = self._row(account_uuid="u2")
        assert ccr._account_description(other) == ccr._account_description(self._row())
        assert ccr._same_account(self._row(), other) is False

    @pytest.mark.parametrize(
        "field",
        [
            "account_uuid",
            "email",
            "organization_uuid",
            "organization_name",
        ],
    )
    def test_every_identity_field_counts(self, field):
        assert ccr._same_account(self._row(), self._row(**{field: "changed"})) is False

    @pytest.mark.parametrize(
        "field",
        [
            "seat_tier",
            "user_rate_limit_tier",
            "organization_rate_limit_tier",
        ],
    )
    def test_a_tier_difference_is_still_the_same_account(self, field):
        """A seat upgrade does not make the login somebody else."""
        base = self._row(**dict.fromkeys(ccr._ACCOUNT_TIER_COLS))
        assert ccr._same_account(base, {**base, field: "moved"}) is True


class TestAccountsWorthShowing:
    """The default run volunteers the account table only when it says something."""

    def _recs(self, *accounts):
        return [_rec(account=a, message_id=f"m{i}") for i, a in enumerate(accounts)]

    def test_no_records_show_nothing(self):
        assert ccr._accounts_worth_showing([]) is False

    def test_one_account_is_the_total_row_again(self):
        assert ccr._accounts_worth_showing(self._recs("me@work.example")) is False

    def test_two_accounts_are_worth_a_table(self):
        recs = self._recs("me@work.example", "me@home.example")
        assert ccr._accounts_worth_showing(recs) is True

    def test_only_unknown_is_not_an_account(self):
        recs = self._recs(ccr.UNKNOWN_ACCOUNT, ccr.UNKNOWN_ACCOUNT)
        assert ccr._accounts_worth_showing(recs) is False

    def test_one_account_beside_its_own_unknown_history_stays_hidden(self):
        """That pair is one account drawn twice; adopt is what merges it."""
        recs = self._recs(ccr.UNKNOWN_ACCOUNT, "me@work.example")
        assert ccr._accounts_worth_showing(recs) is False

    def test_unknown_never_makes_up_the_second_account(self):
        recs = self._recs(ccr.UNKNOWN_ACCOUNT, "me@work.example", "me@home.example")
        assert ccr._accounts_worth_showing(recs) is True

    def test_repeats_of_one_account_are_still_one_account(self):
        recs = self._recs(*["me@work.example"] * 20)
        assert ccr._accounts_worth_showing(recs) is False


class TestDefaultReportDispatch:
    """What a bare `ccreport` prints, and in what order."""

    @pytest.fixture
    def run(self, monkeypatch):
        """Run main() over *accounts*, returning the reports it called."""

        def _run(*accounts, argv=("ccreport.py",)):
            records = [
                _rec(account=a, message_id=f"m{i}") for i, a in enumerate(accounts)
            ]
            called: list[str] = []
            monkeypatch.setattr(ccr, "load_all_records", lambda **_kw: records)
            monkeypatch.setattr(
                ccr, "load_rates_for_records", lambda _r, **_kw: (ccr.NokCtx(), True)
            )
            for name in ("daily", "monthly", "project", "session", "account"):
                monkeypatch.setattr(
                    ccr,
                    f"report_{name}",
                    lambda *_a, _n=name, **_kw: called.append(_n),
                )
            monkeypatch.setattr(ccr.sys, "argv", list(argv))
            ccr.main()
            return called

        return _run

    def test_one_account_prints_the_original_four(self, run):
        assert run("me@work.example") == ["daily", "monthly", "project", "session"]

    def test_two_accounts_append_the_table_last(self, run):
        assert run("me@work.example", "me@home.example") == [
            "daily",
            "monthly",
            "project",
            "session",
            "account",
        ]

    def test_unknown_beside_one_account_still_prints_four(self, run):
        assert run(ccr.UNKNOWN_ACCOUNT, "me@work.example") == [
            "daily",
            "monthly",
            "project",
            "session",
        ]

    def test_the_subcommand_is_unconditional(self, run):
        """One account, explicitly asked for: still printed."""
        assert run("me@work.example", argv=("ccreport.py", "account")) == ["account"]

    def test_the_subcommand_prints_only_itself(self, run):
        called = run(
            "me@work.example", "me@home.example", argv=("ccreport.py", "account")
        )
        assert called == ["account"]


class TestReportDefersTheDailySnapshot:
    """A report is often the day's first DB toucher, and pays for the copy.

    get_connection reads the deferral once, when it opens the singleton
    connection, so setting it anywhere below the top of main() would be
    setting it after the copy (macsetup-2huo).
    """

    @pytest.fixture
    def seen(self, monkeypatch):
        """The deferral as get_connection saw it, per open, over `ccreport overrides`."""
        values: list[str | None] = []
        real = cache_db.get_connection

        def spy():
            values.append(os.environ.get("CLAUDE_CACHE_SNAPSHOT_DEFER"))
            return real()

        monkeypatch.setattr(cache_db, "get_connection", spy)
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport.py", "overrides"])
        return values

    def test_it_is_set_before_the_first_connection(self, seen, monkeypatch, capsys):
        monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DEFER", raising=False)
        ccr.main()
        capsys.readouterr()
        assert seen, "the report never opened the DB; the spy proves nothing"
        assert all(v == "1" for v in seen)
        assert os.environ["CLAUDE_CACHE_SNAPSHOT_DEFER"] == "1"

    def test_an_explicit_setting_from_the_shell_wins(self, seen, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DEFER", "0")
        ccr.main()
        capsys.readouterr()
        assert seen == ["0"] * len(seen)
        assert os.environ["CLAUDE_CACHE_SNAPSHOT_DEFER"] == "0"


# --- Rollups: precomputed aggregates for the days past the cutoff ---
#
# (macsetup-4rte) The corpus below is built so that everything a rollup row
# collapses is present on both sides of the cutoff: two sessions that span it,
# a duplicated message on each side, three accounts, four models, an override
# rule, orphaned records whose file is gone, and records with and without a
# logged costUSD.


def _entry(
    when,
    sid,
    cwd,
    mid,
    rid,
    model="claude-sonnet-5",
    tokens=(1000, 500, 200, 100),
    cost=None,
) -> dict:
    """One assistant line of a session JSONL, at an absolute instant."""
    line = {
        "type": "assistant",
        "timestamp": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessionId": sid,
        "cwd": cwd,
        "requestId": rid,
        "message": {
            "id": mid,
            "model": model,
            "usage": {
                "input_tokens": tokens[0],
                "output_tokens": tokens[1],
                "cache_creation_input_tokens": tokens[2],
                "cache_read_input_tokens": tokens[3],
            },
        },
    }
    if cost is not None:
        line["costUSD"] = cost
    return line


def _write_entries(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _rates_for(records) -> ccr.NokCtx:
    """A different rate for every Oslo date the corpus touches.

    Different on purpose: a record converted under the wrong date then lands on
    a visibly wrong number, which is what a rollup row carrying its own Oslo
    date exists to prevent.
    """
    dates = sorted({ccr.record_oslo_date(r) for r in records})
    rates = {d.isoformat(): 8.0 + i for i, d in enumerate(dates)}
    return ccr.NokCtx(rates, max(rates), True)


def _render_reports(records, nok) -> str:
    """Every table a bare `ccreport` prints, at a fixed width."""
    buf = io.StringIO()
    previous = ccr.console
    ccr.console = Console(file=buf, width=200, no_color=True)
    try:
        ccr.report_daily(records, breakdown=True, nok=nok)
        ccr.report_monthly(records, nok=nok)
        ccr.report_project(records, limit=None, nok=nok)
        ccr.report_session(records, limit=None, nok=nok)
        ccr.report_account(records, nok=nok)
    finally:
        ccr.console = previous
    return buf.getvalue()


@pytest.fixture
def rollup_corpus(loader, monkeypatch):
    """A corpus straddling the rollup cutoff, plus the read-time state it needs.

    The cwds are paths that cannot exist, so project names are derived without
    shelling out to git and without depending on the machine's checkouts.
    """
    now = dt.datetime.now().astimezone()

    def day(n: int, hour: int) -> dt.datetime:
        return (now - dt.timedelta(days=n)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )

    a, b, c, d = (
        "/tmp/ccr-projA",
        "/tmp/ccr-projB",
        "/tmp/ccr-projC",
        "/tmp/ccr-projD",
    )
    _write_entries(
        loader / "a-old.jsonl",
        [
            # Three calls that share a rollup key (day, session, project, model,
            # account) and one that differs only in model — so the table both
            # aggregates and keeps the model split the Models column needs.
            _entry(day(40, 7), "sess-a", a, "old-1c", "r1c", tokens=(20, 30, 40, 50)),
            _entry(day(40, 9), "sess-a", a, "old-1", "r1"),
            _entry(day(40, 9), "sess-a", a, "old-1b", "r1b", cost=0.25),
            _entry(
                day(40, 10),
                "sess-a",
                a,
                "old-2",
                "r2",
                model="claude-haiku-4-5",
                cost=0.5,
            ),
            _entry(day(30, 11), "sess-a", a, "old-3", "r3", cost=1.25),
        ],
    )
    _write_entries(
        loader / "b-old.jsonl",
        [
            # The same message as a-old's, in another project: whichever copy dedup
            # keeps decides which project the cost lands in.
            _entry(day(40, 9), "sess-b", b, "old-1", "r1"),
            # sess-a again, between that session's first and last call of the day.
            # Its group starts later than a-old's but ends earlier, so the session
            # table's project depends on the rollup rows being ordered by when each
            # group started rather than by the timestamp they report.
            _entry(day(40, 8), "sess-a", b, "old-1d", "r1d"),
            _entry(day(30, 12), "sess-b", b, "old-4", "r4"),
            _entry(
                day(20, 13),
                "sess-b",
                b,
                "old-5",
                "r5",
                model="<synthetic>",
                tokens=(0, 0, 0, 0),
            ),
        ],
    )
    _write_entries(
        loader / "c-recent.jsonl",
        [
            _entry(day(5, 8), "sess-c", a, "new-1", "r6"),
            _entry(
                day(2, 9),
                "sess-c",
                a,
                "new-2",
                "r7",
                model="claude-haiku-4-5",
                cost=2.0,
            ),
        ],
    )
    # sess-a again, weeks later and under a name an override rule renames.
    _write_entries(
        loader / "d-recent.jsonl",
        [
            _entry(day(2, 10), "sess-a", c, "new-3", "r8", model="claude-sonnet-4-5"),
        ],
    )
    _write_entries(
        loader / "e-recent.jsonl",
        [
            _entry(
                day(2, 9),
                "sess-e",
                b,
                "new-2",
                "r7",
                model="claude-haiku-4-5",
                cost=2.0,
            ),
            _entry(day(1, 11), "sess-e", b, "new-4", "r9"),
        ],
    )
    # Cached, then purged off disk: orphan records on both sides of the cutoff.
    purged = loader / "z-old.jsonl"
    _write_entries(
        purged,
        [
            _entry(
                day(35, 7),
                "sess-z",
                d,
                "old-6",
                "r10",
                model="claude-haiku-4-5",
                cost=0.75,
            ),
            _entry(day(3, 8), "sess-z", d, "new-5", "r11"),
        ],
    )

    for ts, uuid, email in (
        (day(25, 0).timestamp(), "u-work", "me@work.example"),
        (day(3, 0).timestamp(), "u-home", "me@home.example"),
    ):
        cache_db.record_account_event(
            {"accountUuid": uuid, "emailAddress": email, "organizationName": "Org"},
            now=ts,
        )

    rules = [
        {
            "id": 1,
            "match_kind": "name",
            "match_value": "ccr-projC",
            "target": "ccr-projA",
        }
    ]
    # list() per call so appending a rule changes what the fingerprint sees.
    monkeypatch.setattr(cache_db, "get_project_overrides", lambda: list(rules))

    ccr.load_all_records()  # caches the file about to be purged
    purged.unlink()
    return types.SimpleNamespace(dir=loader, rules=rules, day=day)


class TestRollupParity:
    """The rollup path must render what the record path renders, to the byte."""

    def test_every_report_is_identical_through_both_paths(self, rollup_corpus):
        full = ccr.load_all_records()
        nok = _rates_for(full)
        expected = _render_reports(full, nok)

        built = ccr.load_all_records(use_rollups=True)
        assert _render_reports(built, nok) == expected, "the building run diverged"

        served = ccr.load_all_records(use_rollups=True)
        assert len(served) < len(full), "nothing came from a rollup"
        assert _render_reports(served, nok) == expected

    def test_the_call_count_survives_the_aggregation(self, rollup_corpus):
        """The Calls column adds rec.count, which is why it still adds up."""
        full = ccr.load_all_records()
        ccr.load_all_records(use_rollups=True)
        served = ccr.load_all_records(use_rollups=True)
        assert sum(r.count for r in served) == len(full)

    def test_every_rollup_record_carries_the_date_it_was_rolled_up_under(
        self,
        rollup_corpus,
    ):
        ccr.load_all_records(use_rollups=True)
        served = ccr.load_all_records(use_rollups=True)
        stored = {row[1] for row in cache_db.load_ccreport_rollups()}
        assert {r.oslo_date.isoformat() for r in served if r.oslo_date} == stored
        # Live records derive theirs; carrying one would be the wrong answer
        # the moment the record cache was written in another zone.
        assert all(r.count == 1 for r in served if r.oslo_date is None)


class TestRollupFingerprint:
    """Every read-time input a rollup row froze has to force a rebuild."""

    @pytest.fixture
    def builds(self, monkeypatch):
        """The fingerprint of each rebuild, in order."""
        written: list[str] = []
        real = ccr.save_ccreport_rollups

        def spy(rows, fingerprint):
            written.append(fingerprint)
            real(rows, fingerprint)

        monkeypatch.setattr(ccr, "save_ccreport_rollups", spy)
        return written

    def _warm(self, builds) -> None:
        ccr.load_all_records(use_rollups=True)
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 1, "the second run rebuilt for no reason"

    def test_an_unchanged_corpus_never_rebuilds(self, rollup_corpus, builds):
        self._warm(builds)
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 1

    def test_a_pricing_change_rebuilds(
        self, rollup_corpus, builds, monkeypatch, tmp_path
    ):
        """Rollups freeze computed costs; nothing recomputes a frozen sum."""
        stand_in = tmp_path / "pricing_stand_in.py"
        stand_in.write_text("RATES = 1\n")
        monkeypatch.setattr(ccr.pricing, "__file__", str(stand_in))
        self._warm(builds)
        stand_in.write_text("RATES = 2\n")
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_a_new_override_rule_rebuilds(self, rollup_corpus, builds):
        self._warm(builds)
        rollup_corpus.rules.append(
            {
                "id": 2,
                "match_kind": "name",
                "match_value": "ccr-projB",
                "target": "ccr-projA",
            }
        )
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_a_new_account_event_rebuilds(self, rollup_corpus, builds):
        """Attribution is read-time everywhere else; a rollup would freeze it."""
        self._warm(builds)
        cache_db.record_account_event(
            {
                "accountUuid": "u-third",
                "emailAddress": "third@example.com",
                "organizationName": "Org",
            },
            now=rollup_corpus.day(1, 0).timestamp(),
        )
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_adopting_pre_capture_history_rebuilds(self, rollup_corpus, builds):
        """The ts=0 row re-attributes the oldest days, which are all rolled up."""
        self._warm(builds)
        cache_db.set_adopted_account(present(cache_db.read_latest_account()))
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_touching_a_file_with_old_records_rebuilds(self, rollup_corpus, builds):
        self._warm(builds)
        path = rollup_corpus.dir / "a-old.jsonl"
        appended = _entry(
            rollup_corpus.day(40, 15), "sess-a", "/tmp/ccr-projA", "old-7", "r12"
        )
        path.write_text(path.read_text() + json.dumps(appended) + "\n")
        records = ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2
        # The appended call is older than the cutoff, so only a rebuild counts it.
        expected = len([r for r in ccr.load_all_records() if r.session_id == "sess-a"])
        assert sum(r.count for r in records if r.session_id == "sess-a") == expected

    def test_purging_a_file_rebuilds(self, rollup_corpus, builds):
        """It moves to the back of the dedup order, which can move a project."""
        self._warm(builds)
        (rollup_corpus.dir / "b-old.jsonl").unlink()
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_a_local_timezone_change_rebuilds(self, rollup_corpus, builds, monkeypatch):
        """Days are bucketed in local time, so every one of them moves."""
        self._warm(builds)
        monkeypatch.setenv("TZ", "Pacific/Kiritimati")
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_the_day_rolling_over_rebuilds(self, rollup_corpus, builds, monkeypatch):
        """The cutoff moves forward at local midnight: one rebuild a day."""
        self._warm(builds)
        real = ccr._rollup_cutoff
        monkeypatch.setattr(
            ccr, "_rollup_cutoff", lambda: real() + dt.timedelta(days=1)
        )
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_a_naming_change_rebuilds(self, rollup_corpus, builds, monkeypatch):
        """_script_hash covers how a project name is derived, and rollups key on it."""
        self._warm(builds)
        monkeypatch.setattr(ccr, "_script_hash", lambda: "a-different-hash")
        ccr.load_all_records(use_rollups=True)
        assert len(builds) == 2

    def test_a_new_recent_file_alone_does_not_rebuild(self, rollup_corpus, builds):
        """Only the frozen half of the corpus is fingerprinted."""
        self._warm(builds)
        _write_entries(
            rollup_corpus.dir / "g-recent.jsonl",
            [
                _entry(
                    rollup_corpus.day(1, 12), "sess-g", "/tmp/ccr-projA", "new-9", "r13"
                ),
            ],
        )
        records = ccr.load_all_records(use_rollups=True)
        assert len(builds) == 1
        assert any(r.message_id == "new-9" for r in records)


class TestRollupsServeOnlyTheUnfilteredReport:
    """A rollup row is a day of one session; anything finer needs the records."""

    @pytest.fixture
    def reads(self, rollup_corpus, monkeypatch):
        """Rollup reads, counted, with a valid rollup table already in place."""
        ccr.load_all_records(use_rollups=True)  # builds
        assert len(ccr.load_all_records(use_rollups=True)) < len(
            ccr.load_all_records()
        ), "the rollups are not serving yet"
        counted: list[str] = []
        real = ccr.load_ccreport_rollups

        def spy():
            counted.append("read")
            return real()

        monkeypatch.setattr(ccr, "load_ccreport_rollups", spy)
        monkeypatch.setattr(
            ccr, "load_rates_for_records", lambda _r, **_kw: (ccr.NokCtx(), True)
        )
        return counted

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("since", dt.datetime(2020, 1, 1, tzinfo=UTC)),
            ("until", dt.datetime(2099, 1, 1, tzinfo=UTC)),
            ("project_filter", "ccr-projA"),
            ("account_filter", "work"),
        ],
    )
    def test_a_filtered_load_never_reads_them(self, reads, name, value):
        assert ccr.load_all_records(**{name: value})
        assert reads == []

    def test_adopt_never_reads_them(self, reads, capsys):
        ccr.cmd_adopt(types.SimpleNamespace(remove=False, yes=True))
        assert "Adopt" in capsys.readouterr().out
        assert reads == []

    def test_a_json_run_never_reads_them(self, reads, monkeypatch, capsys):
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport.py", "--json"])
        ccr.main()
        assert capsys.readouterr().out.startswith("[")
        assert reads == []

    def test_a_bare_report_reads_them(self, reads, monkeypatch, capsys):
        """The control: without this, the four above prove nothing."""
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport.py"])
        ccr.main()
        capsys.readouterr()
        assert reads == ["read"]

    def test_asking_for_rollups_with_a_filter_is_refused(self, rollup_corpus):
        with pytest.raises(ValueError, match="aggregate"):
            ccr.load_all_records(project_filter="ccr-projA", use_rollups=True)


class TestRecordOsloDate:
    """Which FX date a record's cost converts under."""

    def test_a_plain_record_derives_it_from_its_timestamp(self):
        """Late enough in UTC that Oslo is already on the next date."""
        rec = _rec(timestamp=dt.datetime(2026, 6, 15, 23, 30, tzinfo=UTC))
        assert ccr.record_oslo_date(rec) == dt.date(2026, 6, 16)

    def test_a_carried_date_wins_over_the_timestamp(self):
        rec = _rec(
            timestamp=dt.datetime(2026, 6, 15, 23, 30, tzinfo=UTC),
            oslo_date=dt.date(2026, 1, 1),
        )
        assert ccr.record_oslo_date(rec) == dt.date(2026, 1, 1)

    def test_the_conversion_uses_the_carried_date(self):
        """A rollup record's timestamp is its group's newest call, not its FX date."""
        nok = ccr.NokCtx({"2026-01-01": 8.0, "2026-06-16": 12.0}, "2026-06-16", False)
        rec = _rec(
            timestamp=dt.datetime(2026, 6, 15, 23, 30, tzinfo=UTC),
            oslo_date=dt.date(2026, 1, 1),
        )
        assert ccr.record_cost_nok(rec, 1.0, nok) == (8.0, False)

    def test_the_bulk_rate_load_asks_for_the_carried_dates(self, monkeypatch):
        """Else the rate a rollup record needs is the one date never fetched."""
        asked: list[set] = []
        monkeypatch.setattr(
            ccr, "load_rates", lambda dates, _pf=None: asked.append(dates) or {}
        )
        ccr.load_rates_for_records([_rec(oslo_date=dt.date(2026, 1, 1))])
        assert asked == [{dt.date(2026, 1, 1)}]


# --- Rate limit utilization history (macsetup-3u9n) ---


def _local_epoch(iso: str) -> float:
    """A local wall-clock time as an epoch, which is how a sample stores it."""
    return dt.datetime.fromisoformat(iso).replace(tzinfo=ccr._local_tz()).timestamp()


# One 5-hour window on the 15th and one on the 16th. Reset times are what group
# the samples, so they are named rather than derived at each call site.
_W1_RESET = _local_epoch("2026-06-15T13:00")
_W2_RESET = _local_epoch("2026-06-16T13:00")
_W1_START = _local_epoch("2026-06-15T08:00")
_W2_START = _local_epoch("2026-06-16T08:00")


def _seed_samples(
    window, resets, start, pcts, *, step=3600.0, model=None, source="stdin"
):
    """Offer *pcts* an hour apart, so every one clears the write gate."""
    for i, pct in enumerate(pcts):
        cache_db.record_rate_limit_snapshots(
            [cache_db.RateLimitSample(window, pct, resets, model, source)],
            now=start + i * step,
        )


def _instances():
    return sorted(
        ccr._window_instances(cache_db.load_rate_limit_snapshots()),
        key=ccr._instance_order,
    )


class TestWindowInstances:
    """A window instance is the samples sharing one reset time."""

    def test_two_reset_times_are_two_instances(self):
        _seed_samples("session", _W1_RESET, _W1_START, [2.0, 40.0])
        _seed_samples("session", _W2_RESET, _W2_START, [1.0, 30.0])
        assert [(i.resets_at, len(i.samples)) for i in _instances()] == [
            (_W1_RESET, 2),
            (_W2_RESET, 2),
        ]

    def test_two_models_under_one_reset_time_are_two_instances(self):
        """The scoped limit follows a model, and which model it scopes can change."""
        _seed_samples(
            "scoped",
            _W2_RESET,
            _W2_START,
            [5.0, 9.0],
            model="claude-fable-5",
            source="api",
        )
        _seed_samples(
            "scoped",
            _W2_RESET,
            _W2_START + 4 * 3600,
            [70.0, 80.0],
            model="claude-opus-5",
            source="api",
        )
        assert [(i.model, i.peak) for i in _instances()] == [
            ("claude-fable-5", 9.0),
            ("claude-opus-5", 80.0),
        ]

    def test_sub_minute_jitter_in_stored_rows_still_groups_as_one(self):
        """Rows written before the writer normalized keep their drift forever."""
        for i, jitter in enumerate([-0.97, 0.03, 0.94, -0.41]):
            cache_db.record_rate_limit_snapshots(
                [
                    cache_db.RateLimitSample(
                        "scoped",
                        10.0 + i * 10,
                        _W2_RESET + jitter,
                        "claude-fable-5",
                        "api",
                    )
                ],
                now=_W2_START + i * 3600,
            )
        (inst,) = _instances()
        assert len(inst.samples) == 4
        assert inst.resets_at == _W2_RESET
        # The instance reports the bucket; the rows keep what was recorded.
        assert [s["resets_at"] for s in inst.samples] != [_W2_RESET] * 4

    def test_the_windows_are_kept_apart(self):
        _seed_samples("session", _W1_RESET, _W1_START, [2.0])
        _seed_samples("week", _W1_RESET, _W1_START, [61.0])
        assert [i.window for i in _instances()] == ["session", "week"]

    def test_the_peak_is_the_fullest_reading_not_the_last(self):
        _seed_samples("session", _W1_RESET, _W1_START, [2.0, 88.4, 40.0])
        assert _instances()[0].peak == 88.4

    def test_the_fill_time_runs_to_the_peak_and_not_past_it(self):
        """Hours spent sitting at the peak are plateau, not fill."""
        _seed_samples("session", _W1_RESET, _W1_START, [2.0, 61.0, 61.0, 61.0])
        inst = _instances()[0]
        assert inst.fill_s == 3600.0
        assert ccr._fmt_span(inst.fill_s) == "1h 00m"

    def test_a_single_sample_fills_in_no_time(self):
        """0 means the peak was already there when the first render saw it."""
        _seed_samples("session", _W1_RESET, _W1_START, [61.0])
        assert _instances()[0].fill_s == 0.0
        assert ccr._fmt_span(0.0) == "0m"

    @pytest.mark.parametrize(
        ("peak", "hit"), [(99.6, True), (100.0, True), (99.4, False)]
    )
    def test_hitting_the_limit_is_decided_on_the_rounded_peak(self, peak, hit):
        """The write gate rounds, so 99.6 is the last sample a full window leaves."""
        _seed_samples("session", _W1_RESET, _W1_START, [2.0, peak])
        assert _instances()[0].hit_limit is hit


class TestWindowBurn:
    """How fast a window filled, and where that rate lands it by reset time."""

    def test_the_rate_is_the_rise_over_the_fill_span(self):
        _seed_samples("session", _W1_RESET, _W1_START, [10.0, 20.0, 40.0])
        assert _instances()[0].burn_pph == 15.0

    def test_the_plateau_is_no_part_of_the_rate(self):
        """Fill runs to the peak, so the rate divides by the same span."""
        _seed_samples("session", _W1_RESET, _W1_START, [10.0, 40.0, 40.0, 40.0])
        assert _instances()[0].burn_pph == 30.0

    def test_the_rise_is_measured_from_the_first_reading_taken(self):
        """Capture starts at a render, not at the window; 77% is where we looked."""
        _seed_samples("week", _W1_RESET, _W1_START, [77.0, 86.0])
        inst = _instances()[0]
        assert (inst.opening_pct, inst.rise, inst.burn_pph) == (77.0, 9.0, 9.0)

    @pytest.mark.parametrize("pcts", [[61.0], [61.0, 61.0]])
    def test_a_window_that_never_rose_while_watched_has_no_rate(self, pcts):
        """None, not 0 — 0 would read as a window that is not filling."""
        _seed_samples("session", _W1_RESET, _W1_START, pcts)
        assert _instances()[0].burn_pph is None

    def _projected(self, pcts, now_iso):
        _seed_samples("session", _W1_RESET, _W1_START, pcts)
        return _instances()[0].projected_pct(_local_epoch(now_iso))

    def test_the_projection_runs_from_the_last_sample_to_the_reset(self):
        """20 pp/h, last read at 09:00 and 30%, four hours to the 13:00 reset."""
        assert self._projected([10.0, 30.0], "2026-06-15T10:00") == 110.0

    def test_the_projection_is_not_capped_at_full(self):
        """Over 100% is the answer: the limit arrives before the reset does."""
        assert present(self._projected([10.0, 30.0], "2026-06-15T10:00")) > 100

    def test_a_closed_window_is_not_projected(self):
        """Its outcome is the peak. There is nothing left to predict."""
        assert self._projected([10.0, 30.0], "2026-06-15T13:01") is None

    def test_a_window_with_no_rate_is_not_projected(self):
        assert self._projected([30.0], "2026-06-15T10:00") is None

    def test_idle_time_between_two_renders_counts_against_the_rate(self):
        """Wall-clock, and named as such: an overnight gap is time it took."""
        _seed_samples("session", _W1_RESET, _W1_START, [10.0, 20.0], step=4 * 3600.0)
        assert _instances()[0].burn_pph == 2.5


def _spend_rec(iso, *, usd, model="claude-opus-5"):
    return _rec(
        model=model,
        cost_usd=usd,
        timestamp=dt.datetime.fromtimestamp(_local_epoch(iso), UTC),
    )


class TestSpendIndex:
    """Range sums over the record corpus, by model family."""

    def _index(self):
        return ccr._SpendIndex(
            [
                _spend_rec("2026-06-15T08:00", usd=1.0),
                _spend_rec("2026-06-15T09:00", usd=2.0, model="claude-fable-5"),
                _spend_rec("2026-06-15T10:00", usd=4.0),
            ]
        )

    def _total(self, start, end, family=None):
        return self._index().total(_local_epoch(start), _local_epoch(end), family)

    def test_both_bounds_are_inclusive(self):
        """A record written in the first sample's second is inside the window."""
        assert self._total("2026-06-15T08:00", "2026-06-15T10:00") == 7.0

    def test_records_outside_the_range_are_left_out(self):
        assert self._total("2026-06-15T09:00", "2026-06-15T09:30") == 2.0

    def test_a_family_sums_only_its_own_models(self):
        assert self._total("2026-06-15T08:00", "2026-06-15T10:00", "fable") == 2.0

    def test_a_family_with_no_records_sums_to_nothing(self):
        assert self._total("2026-06-15T08:00", "2026-06-15T10:00", "haiku") == 0.0

    def test_an_index_with_no_corpus_behind_it_says_so(self):
        assert ccr._SpendIndex([]).empty is True
        assert self._index().empty is False


class TestWindowFamily:
    """Which model family's spend a window's quota counts."""

    def _inst(self, window, model=None):
        return ccr.WindowInstance(
            window,
            model,
            _W1_RESET,
            [
                {
                    "ts": _W1_START,
                    "used_pct": 1.0,
                    "resets_at": _W1_RESET,
                    "model": model,
                    "source": "api",
                },
            ],
        )

    def test_the_scoped_window_follows_the_model_it_names(self):
        assert ccr._window_family(self._inst("scoped", "claude-fable-5")) == "fable"

    def test_the_sonnet_window_is_scoped_without_naming_a_model(self):
        assert ccr._window_family(self._inst("sonnet")) == "sonnet"

    @pytest.mark.parametrize("window", ["session", "week"])
    def test_the_unscoped_windows_count_every_model(self, window):
        assert ccr._window_family(self._inst(window)) is None


class TestInstanceSpend:
    """What a window's rise cost, and what the rest of it is worth."""

    def _priced(self, pcts, *, now_iso="2026-06-15T10:00", records=None):
        _seed_samples("session", _W1_RESET, _W1_START, pcts)
        index = ccr._SpendIndex(
            records
            if records is not None
            else [
                _spend_rec("2026-06-15T08:30", usd=10.0),
                _spend_rec("2026-06-15T09:30", usd=5.0),
            ]
        )
        return ccr._instance_spend(_instances()[0], index, _local_epoch(now_iso))

    def test_the_spend_is_what_the_fill_span_cost(self):
        """08:00 → 09:00 is the fill; the 09:30 record is after the peak."""
        assert self._priced([10.0, 30.0, 30.0]).usd == 10.0

    def test_the_exchange_rate_divides_that_spend_by_the_rise(self):
        assert self._priced([10.0, 30.0, 30.0]).per_pp == 0.5

    def test_the_headroom_prices_the_points_left_in_an_open_window(self):
        """70 points to go at $0.50 each."""
        assert self._priced([10.0, 30.0, 30.0]).headroom_usd == 35.0

    def test_a_closed_window_has_no_headroom_to_price(self):
        assert (
            self._priced([10.0, 30.0, 30.0], now_iso="2026-06-15T13:01").headroom_usd
            is None
        )

    def test_a_full_window_is_worth_nothing_more(self):
        """Not a negative number, which is what 100 - 104 would price."""
        assert self._priced([10.0, 104.0]).headroom_usd == 0.0

    def test_a_window_that_never_rose_prices_as_nothing_at_all(self):
        """$0.00 over an instant would read as a window that was free."""
        assert self._priced([30.0]) == ccr._NO_SPEND

    def test_a_missing_corpus_prices_as_nothing_at_all(self):
        assert self._priced([10.0, 30.0], records=[]) == ccr._NO_SPEND


class TestLimitsAttribution:
    """Account and tier come off the event in force at the first sample."""

    def _capture(self, iso, uuid, email, user_tier):
        cache_db.record_account_event(
            {
                "accountUuid": uuid,
                "emailAddress": email,
                "organizationName": "Org",
                "seatTier": "team_tier_1",
                "userRateLimitTier": user_tier,
                "organizationRateLimitTier": "default_raven",
            },
            now=_local_epoch(iso),
        )

    def _seeded(self):
        """Two session windows straddling a tier change on the same login."""
        self._capture(
            "2026-06-14T12:00", "u-work", "me@work.example", "default_claude_max_5x"
        )
        _seed_samples("session", _W1_RESET, _W1_START, [2.0, 40.0])
        self._capture(
            "2026-06-15T20:00", "u-work", "me@work.example", "default_claude_max_20x"
        )
        _seed_samples("session", _W2_RESET, _W2_START, [1.0, 30.0])
        return _instances(), ccr.AccountTimeline(cache_db.load_account_events())

    def test_each_window_reports_the_tier_it_opened_under(self):
        instances, accounts = self._seeded()
        assert [accounts.tier_at(ccr._as_local(i.first_ts)) for i in instances] == [
            "default_claude_max_5x",
            "default_claude_max_20x",
        ]

    def test_the_account_label_comes_from_the_same_event(self):
        instances, accounts = self._seeded()
        assert [accounts.label_at(ccr._as_local(i.first_ts)) for i in instances] == [
            "me@work.example",
            "me@work.example",
        ]

    def test_a_window_older_than_the_log_reports_neither(self):
        _seed_samples("session", _W1_RESET, _W1_START, [2.0, 40.0])
        self._capture(
            "2026-06-16T12:00", "u-work", "me@work.example", "default_claude_max_5x"
        )
        accounts = ccr.AccountTimeline(cache_db.load_account_events())
        when = ccr._as_local(_instances()[0].first_ts)
        assert accounts.label_at(when) == ccr.UNKNOWN_ACCOUNT
        assert accounts.tier_at(when) is None


class TestCmdLimits:
    """The subcommand: filters, the JSON shape, and the empty cases."""

    @pytest.fixture(autouse=True)
    def _corpus_stub(self, monkeypatch):
        """No corpus unless a test seeds one.

        The real load walks the machine's own ~/.claude tree, which is both slow
        and different on every machine the suite runs on.
        """
        self.records: list = []
        monkeypatch.setattr(ccr, "load_all_records", lambda **_kw: self.records)

    def _args(self, **kw):
        return types.SimpleNamespace(
            **{"since": None, "until": None, "window": None, "json": False, **kw}
        )

    def _corpus(self):
        cache_db.record_account_event(
            {
                "accountUuid": "u-work",
                "emailAddress": "me@work.example",
                "organizationName": "Org",
                "userRateLimitTier": "default_claude_max_5x",
            },
            now=_local_epoch("2026-06-14T12:00"),
        )
        _seed_samples("session", _W1_RESET, _W1_START, [2.0, 99.7])
        _seed_samples("week", _W1_RESET, _W1_START, [30.0, 44.0])
        _seed_samples(
            "scoped",
            _W2_RESET,
            _W2_START,
            [5.0, 9.0],
            model="claude-fable-5",
            source="api",
        )

    def _run_json(self, capsys, **kw):
        """The parsed --json entries and the stderr text, from one capture.

        Both in one call because readouterr() drains both streams: reading the
        entries and then reading stderr hands the second read an empty string.
        """
        ccr.cmd_limits(self._args(json=True, **kw))
        cap = capsys.readouterr()
        return json.loads(cap.out), cap.err

    def _json(self, capsys, **kw):
        return self._run_json(capsys, **kw)[0]

    def test_an_unwritten_table_exits_one_with_a_reason(self, capsys):
        with pytest.raises(SystemExit) as exc:
            ccr.cmd_limits(self._args())
        assert exc.value.code == 1
        assert "No rate-limit samples recorded" in capsys.readouterr().err

    def test_filters_that_match_nothing_exit_one(self, capsys):
        self._corpus()
        with pytest.raises(SystemExit) as exc:
            ccr.cmd_limits(self._args(since="20990101"))
        assert exc.value.code == 1
        assert "match those filters" in capsys.readouterr().err

    def test_the_json_entry_carries_raw_floats_and_epochs(self, capsys):
        self._corpus()
        self.records = [_spend_rec("2026-06-15T08:30", usd=8.0)]
        assert self._json(capsys, window="session") == [
            {
                "window": "session",
                "model": None,
                "resets_at": _W1_RESET,
                "first_ts": _W1_START,
                "peak_ts": _W1_START + 3600.0,
                "last_ts": _W1_START + 3600.0,
                "opening_used_pct": 2.0,
                "peak_used_pct": 99.7,
                "latest_used_pct": 99.7,
                "samples": 2,
                "fill_seconds": 3600.0,
                "burn_pp_per_hour": 97.7,
                "open": False,
                "projected_used_pct": None,
                "spend_usd": 8.0,
                "usd_per_pp": 8.0 / 97.7,
                "headroom_usd": None,
                "hit_limit": True,
                "account": "me@work.example",
                "limit_tier": "default_claude_max_5x",
            }
        ]

    def test_the_json_lists_every_window_in_the_printed_order(self, capsys):
        self._corpus()
        assert [(e["window"], e["model"]) for e in self._json(capsys)] == [
            ("session", None),
            ("week", None),
            ("scoped", "claude-fable-5"),
        ]

    def test_the_window_filter_selects_one_type(self, capsys):
        self._corpus()
        assert [e["window"] for e in self._json(capsys, window="week")] == ["week"]

    def test_a_date_bound_selects_samples_not_whole_instances(self, capsys):
        """A 5-hour window straddling midnight reports the part inside the range."""
        _seed_samples(
            "session",
            _local_epoch("2026-06-16T02:00"),
            _local_epoch("2026-06-15T23:00"),
            [40.0, 90.0],
            step=5400.0,
        )
        entries = self._json(capsys, since="20260616")
        assert [(e["samples"], e["peak_used_pct"]) for e in entries] == [(1, 90.0)]

    SENTINEL = 9_999_999_999.0
    """The placeholder resets_at Claude Code sent on stdin; four rows carry it."""

    def _seed_sentinel(self):
        cache_db.record_rate_limit_snapshots(
            [cache_db.RateLimitSample("session", 12.0, self.SENTINEL, None, "stdin")],
            now=_W1_START,
        )

    def test_a_placeholder_reset_is_dropped_and_said_out_loud(self, capsys):
        self._corpus()
        self._seed_sentinel()
        entries, err = self._run_json(capsys, window="session")
        assert [e["resets_at"] for e in entries] == [_W1_RESET]
        assert "dropped 1 sample(s)" in err
        assert "8 days" in err

    def test_nothing_is_said_when_nothing_was_dropped(self, capsys):
        self._corpus()
        assert self._run_json(capsys)[1] == ""

    def test_the_note_counts_only_what_this_run_would_have_shown(self, capsys):
        """The filters run first, so a --window the placeholder is not in stays quiet."""
        self._corpus()
        self._seed_sentinel()
        assert self._run_json(capsys, window="week")[1] == ""

    def test_a_run_left_with_nothing_but_placeholders_exits_one(self, capsys):
        self._seed_sentinel()
        with pytest.raises(SystemExit) as exc:
            ccr.cmd_limits(self._args())
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "dropped 1 sample(s)" in err
        assert "match those filters" in err

    def test_a_window_this_report_has_no_label_for_still_shows_up(
        self, capsys, monkeypatch
    ):
        """The writer's window list lives elsewhere; drift must not lose history."""
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        _seed_samples("session", _W1_RESET, _W1_START, [2.0])
        _seed_samples("opus_hourly", _W1_RESET, _W1_START, [61.0])
        ccr.cmd_limits(self._args())
        out = buf.getvalue()
        assert out.index("Session (5h)") < out.index("opus_hourly — 1 window(s)")

    def test_the_table_names_each_window_and_summarizes_it(self, capsys, monkeypatch):
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        self._corpus()
        ccr.cmd_limits(self._args())
        out = buf.getvalue()
        assert "Session (5h) — 1 window(s)" in out
        assert "Scoped model (7d) — 1 window(s)" in out
        assert "fable-5" in out
        assert "99.7%" in out
        assert "1 hit" in out
        assert "0 hit" in out
        assert "default_claude_max_5x" in out


class TestLimitsRendering:
    """The table's own decisions: the caption, and what a narrow terminal loses."""

    def _render(self, monkeypatch, *, width=200, records=(), now=None):
        buf = io.StringIO()
        monkeypatch.setattr(
            ccr, "console", Console(file=buf, width=width, no_color=True)
        )
        instances = _instances()
        stamp = _local_epoch(now) if now else _W1_START
        spends = {
            i.key: ccr._instance_spend(i, ccr._SpendIndex(list(records)), stamp)
            for i in instances
        }
        ccr.report_limits(instances, _timeline(), spends, stamp)
        return buf.getvalue()

    def test_an_open_window_is_read_out_under_its_table(self, monkeypatch):
        _seed_samples("session", _W1_RESET, _W1_START, [10.0, 30.0])
        out = self._render(
            monkeypatch,
            now="2026-06-15T10:00",
            records=[
                _spend_rec("2026-06-15T08:30", usd=10.0),
            ],
        )
        assert "open at 30.0%" in out
        assert "seen from 10.0%" in out
        assert "20.0 pp/h" in out
        assert "110% by reset 2026-06-15 13:00" in out
        assert "70.0 pp left" in out

    def test_the_caption_names_the_model_of_a_scoped_window(self, monkeypatch):
        _seed_samples(
            "scoped",
            _W1_RESET,
            _W1_START,
            [10.0, 30.0],
            model="claude-fable-5",
            source="api",
        )
        assert "fable-5: open at 30.0%" in self._render(
            monkeypatch, now="2026-06-15T10:00"
        )

    def test_a_closed_window_is_read_out_to_nobody(self, monkeypatch):
        _seed_samples("session", _W1_RESET, _W1_START, [10.0, 30.0])
        assert "open at" not in self._render(monkeypatch, now="2026-06-15T13:01")

    def test_an_open_window_with_no_rate_says_that_instead_of_a_number(
        self, monkeypatch
    ):
        _seed_samples("session", _W1_RESET, _W1_START, [30.0])
        out = self._render(monkeypatch, now="2026-06-15T10:00")
        assert "no rate to project from yet" in out

    def test_a_narrow_terminal_drops_the_named_columns_in_order(self, monkeypatch):
        _seed_samples("session", _W1_RESET, _W1_START, [10.0, 30.0])
        wide = self._render(monkeypatch, width=200)
        for header in ("Tier", "Account", "Samples"):
            assert header in wide
        narrow = self._render(monkeypatch, width=80)
        assert "Tier" not in narrow
        assert "Account" not in narrow
        assert "Samples" in narrow

    def test_the_numbers_survive_a_terminal_too_narrow_for_the_words(self, monkeypatch):
        """Rich's own answer is to ellipsize every column, losing all of them."""
        _seed_samples("session", _W1_RESET, _W1_START, [10.0, 30.0])
        narrow = self._render(
            monkeypatch,
            width=80,
            records=[
                _spend_rec("2026-06-15T08:30", usd=10.0),
            ],
        )
        assert "20.0" in narrow
        assert "$0.5" in narrow
