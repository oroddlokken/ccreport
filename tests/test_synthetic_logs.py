"""End-to-end read of a generated corpus through scan.parse_jsonl_file.

The generator is the fixture: it returns what it wrote, so every assertion here
compares the reader's output against a value nothing in ccreport derived. That
is what makes this a test of the parse rather than a restatement of it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from ccreport import pricing, project_identity, scan

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "make_synthetic_logs.py"


@pytest.fixture(scope="session")
def gen():
    """The generator loaded as a module; tools/ is outside every package."""
    spec = importlib.util.spec_from_file_location("make_synthetic_logs", TOOL)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before it runs: the tool defers its annotations, so dataclasses
    # resolves Expected's field types out of sys.modules and fails on a module
    # that is not there yet.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def corpus(gen, tmp_path, monkeypatch):
    """A written corpus, read with git and the repo roots pointed away from it.

    The repo-root baseline is ~/git, and a TMPDIR underneath it would hand
    parse_jsonl_file a name off the enclosing directory instead of the cwd's
    own. Moving the baseline keeps repo_from_path itself in the path.
    """
    monkeypatch.setattr(scan, "_remote_cache", {})
    monkeypatch.setattr(project_identity, "_BASELINE_REPO_ROOT", tmp_path / "no-repo-roots")
    project_identity.repo_roots.cache_clear()
    expected = gen.write_corpus(tmp_path / "synth", seed=7, projects=3, sessions=3, days=5)
    yield expected
    project_identity.repo_roots.cache_clear()


def _by_file(expected):
    out: dict[Path, list] = {}
    for exp in expected:
        out.setdefault(exp.path, []).append(exp)
    return out


class TestGeneratedTree:
    def test_it_writes_under_the_projects_dir_claude_code_uses(self, corpus, tmp_path):
        root = tmp_path / "synth" / "projects"
        for exp in corpus:
            assert exp.path.parent.parent == root
            assert exp.path.parent.name == pricing.project_key(exp.cwd)

    def test_every_cwd_exists_and_is_not_a_git_repo(self, corpus):
        for cwd in {exp.cwd for exp in corpus}:
            assert Path(cwd).is_dir()
            assert scan._resolve_remote(cwd) is None

    def test_it_spans_the_days_it_was_asked_for(self, corpus):
        assert len({exp.timestamp.date() for exp in corpus}) == 5

    def test_it_uses_more_than_one_model_and_all_are_priced(self, corpus):
        models = {exp.model for exp in corpus}
        assert len(models) > 1
        for model in models:
            assert pricing.calc_cost(1000, 1000, 0, 0, model, corpus[0].timestamp) > 0

    def test_a_seed_fixes_the_whole_corpus(self, gen, tmp_path):
        a = gen.write_corpus(tmp_path / "a", seed=3, projects=2, sessions=2, days=3)
        b = gen.write_corpus(tmp_path / "b", seed=3, projects=2, sessions=2, days=3)
        strip = [(e.session_id, e.message_id, e.timestamp, e.model, e.cost_usd) for e in a]
        assert strip == [(e.session_id, e.message_id, e.timestamp, e.model, e.cost_usd) for e in b]

    def test_a_different_seed_writes_a_different_corpus(self, gen, tmp_path):
        a = gen.write_corpus(tmp_path / "a", seed=1, projects=2, sessions=2, days=3)
        b = gen.write_corpus(tmp_path / "b", seed=2, projects=2, sessions=2, days=3)
        assert [e.message_id for e in a] != [e.message_id for e in b]

    def test_it_refuses_a_corpus_with_nothing_in_it(self, gen, tmp_path):
        with pytest.raises(ValueError):
            gen.write_corpus(tmp_path / "empty", days=0)


class TestParse:
    def test_each_file_parses_to_the_records_it_was_given(self, corpus):
        for path, expected in _by_file(corpus).items():
            got = scan.parse_jsonl_file(path)
            assert len(got) == len(expected), path
            for rec, exp in zip(got, expected, strict=True):
                assert rec.message_id == exp.message_id
                assert rec.model == exp.model
                assert rec.session_id == exp.session_id
                assert rec.project == exp.project
                assert rec.timestamp == exp.timestamp
                assert rec.tokens.input == exp.input_tokens
                assert rec.tokens.output == exp.output_tokens
                assert rec.tokens.cache_create == exp.cache_creation_tokens
                assert rec.tokens.cache_read == exp.cache_read_tokens
                assert rec.cost_usd == exp.cost_usd
                assert rec.dedup_key == exp.dedup_key
                assert rec.cwd == exp.cwd
                assert rec.repo is None

    def test_the_whole_corpus_parses_to_the_count_the_generator_reported(self, corpus):
        total = sum(len(scan.parse_jsonl_file(p)) for p in _by_file(corpus))
        assert total == len(corpus)

    def test_the_skipped_lines_leave_no_record(self, corpus):
        """Six lines per file the reader must drop, two of them assistant-shaped."""
        for path, expected in _by_file(corpus).items():
            lines = [ln for ln in path.read_text().split("\n") if ln.strip()]
            assert len(lines) == len(expected) * 2 + 5
            got = scan.parse_jsonl_file(path)
            ids = {rec.message_id for rec in got}
            assert not ids & {"msg_no_usage", "msg_bad_ts", "msg_trunc"}

    def test_every_dedup_key_is_its_own(self, corpus):
        keys = [scan.parse_jsonl_file(p) for p in _by_file(corpus)]
        flat = [r.dedup_key for recs in keys for r in recs]
        assert len(set(flat)) == len(flat)

    def test_a_record_prices_to_something(self, corpus):
        recs = [r for p in _by_file(corpus) for r in scan.parse_jsonl_file(p)]
        assert all(r.cost() > 0 for r in recs)


class TestDiscovery:
    def test_discovery_finds_every_generated_session(self, corpus, tmp_path, monkeypatch):
        monkeypatch.setattr(scan, "_PROJECT_ROOTS", (tmp_path / "synth" / "projects",))
        found = scan.discover_jsonl_files()
        assert set(found) == set(_by_file(corpus))

    def test_discovery_and_the_parse_agree_on_the_corpus(self, corpus, tmp_path, monkeypatch):
        monkeypatch.setattr(scan, "_PROJECT_ROOTS", (tmp_path / "synth" / "projects",))
        recs = [r for p in scan.discover_jsonl_files() for r in scan.parse_jsonl_file(p)]
        assert len(recs) == len(corpus)
        assert {r.project for r in recs} == {e.project for e in corpus}


class TestCommandLine:
    def test_the_cli_writes_the_tree(self, tmp_path):
        out = subprocess.run(
            [sys.executable, str(TOOL), str(tmp_path / "cli"),
             "--seed", "5", "--projects", "2", "--sessions", "4", "--days", "3"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
        assert out.returncode == 0, out.stderr
        files = sorted((tmp_path / "cli" / "projects").rglob("*.jsonl"))
        assert len(files) == 8
        assert f"{len(files)} sessions" in out.stdout
        assert len({p.parent for p in files}) == 2
