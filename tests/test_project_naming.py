"""Which project a cwd is filed under: worktrees, scratchpads and glob rules.

Three shapes of cwd used to become a project each: a worktree whose directory
was deleted (named after the worktree, or after the container directory above
its checkout), Claude Code's per-session scratchpad (named "scratchpad"), and a
one-off mkdtemp directory (named after its random suffix, which no single rule
could reach).
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest
from _narrow import present

from ccreport import cache_db, codex_scan, opencode_scan, project_identity, scan
from ccreport import ccreport as ccr


@pytest.fixture(autouse=True)
def clean_naming(tmp_path, monkeypatch):
    """No remotes, no repo roots and no memo from another test.

    The baseline root is ~/git; a TMPDIR beneath it would name every cwd here
    after the enclosing directory, so it is pointed somewhere empty.
    """
    monkeypatch.setattr(scan, "_remote_cache", {})
    monkeypatch.setattr(scan, "_checkout_cache", {})
    monkeypatch.setattr(scan, "_known_cwds", None)
    monkeypatch.setattr(project_identity, "_BASELINE_REPO_ROOT", tmp_path / "no-repo-roots")
    project_identity.repo_roots.cache_clear()
    yield
    project_identity.repo_roots.cache_clear()


def _no_remotes(monkeypatch, remotes: dict[str, str] | None = None) -> None:
    """Answer remotes from *remotes* by exact path, None for every other dir."""
    table = remotes or {}
    monkeypatch.setattr(scan, "_resolve_remote", lambda cwd: table.get(cwd))


def _scratchpad(real: str, uid: int = 501, prefix: str = "/private/tmp") -> str:
    encoded = project_identity.claude_encode(real)
    return f"{prefix}/claude-{uid}/{encoded}/0f3c2a9e-session/scratchpad"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
                        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


class TestWorktreeCheckout:
    @pytest.mark.parametrize(("cwd", "checkout"), [
        ("/src/work/app/.worktrees/fix-login", "/src/work/app"),
        ("/src/work/app/.worktrees/fix-login/web/src", "/src/work/app"),
        ("/src/work/app/.claude/worktrees/agent-a2b6", "/src/work/app"),
        ("/src/work/app/.worktrees", "/src/work/app"),
        ("/src/work/app/.worktrees/a/.claude/worktrees/b", "/src/work/app"),
        ("/src/work/app", None),
        ("/src/work/app/worktrees/x", None),
        ("/src/work/app/.claude", None),
    ])
    def test_the_checkout_is_whatever_holds_the_outermost_marker(self, cwd, checkout):
        assert project_identity.worktree_checkout(cwd) == checkout


class TestDeletedWorktrees:
    def test_a_deleted_worktree_is_named_after_its_checkout_not_the_container(
        self, tmp_path, monkeypatch,
    ):
        """~/dev/work/<repo>: under a ~/dev root the container used to win."""
        root = tmp_path / "dev"
        monkeypatch.setattr(project_identity, "_BASELINE_REPO_ROOT", root)
        project_identity.repo_roots.cache_clear()
        _no_remotes(monkeypatch)
        cwd = str(root / "work" / "calculator" / ".claude" / "worktrees" / "agent-a38d")
        assert scan.project_for_cwd(cwd) == ("calculator", None)
        assert project_identity.name_for_cwd(cwd) == "calculator"

    def test_a_deleted_worktree_takes_the_remote_its_checkout_still_has(
        self, tmp_path, monkeypatch,
    ):
        checkout = tmp_path / "site.example"
        checkout.mkdir()
        _no_remotes(monkeypatch, {str(checkout): "git.example.test/someone/site.example"})
        cwd = str(checkout / ".worktrees" / "fix-footer" / "tests")
        assert scan.project_for_cwd(cwd) == (
            "site.example", "git.example.test/someone/site.example")

    def test_a_worktree_with_its_own_remote_answers_with_it(self, tmp_path, monkeypatch):
        cwd = str(tmp_path / "app" / ".worktrees" / "x")
        _no_remotes(monkeypatch, {cwd: "example.test/org/app"})
        assert scan.project_for_cwd(cwd) == ("app", "example.test/org/app")

    def test_a_cwd_outside_every_worktree_is_named_as_before(self, tmp_path, monkeypatch):
        _no_remotes(monkeypatch)
        assert scan.project_for_cwd(str(tmp_path / "plain" / "sub")) == ("sub", None)


class TestSiblingWorktrees:
    """~/dev/<repo>-<branch>: git can say whose it is while it exists, nothing after."""

    @pytest.fixture
    def repo(self, tmp_path):
        main = tmp_path / "shop.example"
        main.mkdir()
        _git("init", "-q", "-b", "main", cwd=main)
        _git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
        _git("worktree", "add", "-q", str(tmp_path / "shop.example-pr12"), cwd=main)
        return main

    def test_a_live_linked_worktree_without_a_remote_names_its_main_checkout(
        self, repo, tmp_path, monkeypatch,
    ):
        _no_remotes(monkeypatch)
        sibling = (tmp_path / "shop.example-pr12").resolve()
        assert scan.project_for_cwd(str(sibling)) == ("shop.example", None)
        assert scan.project_for_cwd(str(repo.resolve())) == ("shop.example", None)

    def test_a_deleted_sibling_keeps_its_own_name(self, repo, tmp_path, monkeypatch):
        """A name that merely starts with another repo's is no evidence."""
        _no_remotes(monkeypatch)
        gone = str((tmp_path / "shop.example-old-branch").resolve())
        assert scan.project_for_cwd(gone) == ("shop.example-old-branch", None)


class TestScratchpad:
    def test_the_encoding_is_claude_codes_projects_dir_spelling(self):
        assert project_identity.claude_encode("/Users/f/dev/shop.example") == "-Users-f-dev-shop-example"
        assert (project_identity.claude_encode("/Users/f/dev/café-app")
                == "-Users-f-dev-caf--app")

    def test_a_scratchpad_is_read_back_to_the_directory_on_disk(self, tmp_path, monkeypatch):
        real = tmp_path.resolve() / "dev" / "shop.example" / ".claude" / "worktrees" / "a-1"
        real.mkdir(parents=True)
        (tmp_path / "dev" / "shop-example-old").mkdir()
        _no_remotes(monkeypatch)
        for prefix in ("/private/tmp", "/tmp", "/private/var/folders/r2/hf4cb/T"):
            pad = _scratchpad(str(real), prefix=prefix)
            assert project_identity.scratchpad_origin(pad) == str(real)
            assert scan.project_for_cwd(pad) == ("shop.example", None)
            assert project_identity.name_for_cwd(pad) == "shop.example"

    def test_two_directories_with_the_same_spelling_are_no_answer(self, tmp_path, monkeypatch):
        base = tmp_path.resolve()
        (base / "a.b").mkdir()
        (base / "a-b").mkdir()
        _no_remotes(monkeypatch)
        pad = _scratchpad(str(base / "a.b"))
        assert project_identity.scratchpad_origin(pad) is None
        assert scan.project_for_cwd(pad) == ("scratchpad", None), "falls back unchanged"

    def test_a_gone_directory_is_matched_against_the_cwds_the_cache_knows(
        self, tmp_path, monkeypatch,
    ):
        gone = str(tmp_path.resolve() / "dev" / "site.example")
        cache_db.init_ccreport_meta(1, "h")
        cache_db.save_ccreport_file("/logs/old.jsonl", 1, 1, [{
            "mid": "m1", "model": "claude-opus-5", "ts": 1.0, "sid": "s",
            "project": "site.example", "cwd": gone, "repo": None, "t": [1, 1, 1, 1],
        }])
        _no_remotes(monkeypatch)
        assert scan.project_for_cwd(_scratchpad(gone)) == ("site.example", None)
        # Without the cache it stays a scratchpad: the disk alone cannot say.
        assert project_identity.scratchpad_origin(_scratchpad(gone)) is None

    def test_an_ambiguous_known_cwd_is_no_answer(self, tmp_path, monkeypatch):
        base = str(tmp_path.resolve() / "gone")
        monkeypatch.setattr(scan, "load_known_cwds", lambda: {base + "/x.y", base + "/x-y"})
        _no_remotes(monkeypatch)
        assert scan.project_for_cwd(_scratchpad(base + "/x.y")) == ("scratchpad", None)

    def test_the_cache_is_only_read_when_the_disk_cannot_answer(self, tmp_path, monkeypatch):
        real = tmp_path.resolve() / "proj"
        real.mkdir()

        def boom():
            raise AssertionError("read the cache for a scratchpad the disk answered")

        monkeypatch.setattr(scan, "load_known_cwds", boom)
        _no_remotes(monkeypatch)
        assert scan.project_for_cwd(_scratchpad(str(real))) == ("proj", None)
        assert scan.project_for_cwd(str(real)) == ("proj", None)

    def test_a_parsed_log_from_a_scratchpad_lands_in_the_real_project(
        self, tmp_path, monkeypatch,
    ):
        real = tmp_path.resolve() / "billing"
        real.mkdir()
        _no_remotes(monkeypatch)
        log = tmp_path / "s.jsonl"
        log.write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-06-15T12:00:00Z", "sessionId": "s",
            "cwd": _scratchpad(str(real)) + "/nvtest", "requestId": "r1",
            "message": {"id": "m1", "model": "claude-opus-5", "usage": {
                "input_tokens": 1, "output_tokens": 1,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        }) + "\n")
        (rec,) = scan.parse_jsonl_file(log)
        assert rec.project == "billing"
        assert present(rec.cwd).endswith("/scratchpad/nvtest"), "the logged cwd is kept as it was"


class TestOtherToolsShareTheDerivation:
    def test_codex_without_a_logged_remote_goes_through_project_for_cwd(
        self, tmp_path, monkeypatch,
    ):
        _no_remotes(monkeypatch)
        cwd = str(tmp_path / "app" / ".worktrees" / "x")
        assert codex_scan._project({"cwd": cwd}) == ("app", cwd, None)
        assert codex_scan._project({"cwd": cwd, "repo": "git@example.test:org/web.git"}) == (
            "web", cwd, "example.test/org/web")

    def test_opencode_goes_through_project_for_cwd(self, tmp_path, monkeypatch):
        _no_remotes(monkeypatch)
        cwd = str(tmp_path / "app" / ".claude" / "worktrees" / "x")
        assert opencode_scan._project({"directory": cwd}) == ("app", cwd, None)


class TestCwdGlobRule:
    @staticmethod
    def _fn(monkeypatch, *rules):
        monkeypatch.setattr(cache_db, "get_project_overrides", lambda: [
            {"id": i, "match_kind": k, "match_value": v, "target": t}
            for i, (k, v, t) in enumerate(rules, 1)
        ])
        fn = project_identity.build_override_fn()
        assert fn is not None
        return fn

    def test_one_rule_takes_every_mkdtemp_directory_and_what_is_under_it(self, monkeypatch):
        fn = self._fn(monkeypatch, ("cwd_glob", "/private/var/folders/*/T/batch-*", "pipeline"))
        for cwd in ("/private/var/folders/r2/hf4cb/T/batch-hc8i6zzz",
                    "/private/var/folders/r2/hf4cb/T/batch-l1b8um9p/plate"):
            assert fn(None, cwd, cwd.rsplit("/", 1)[-1]) == "pipeline"
        assert fn(None, "/private/var/folders/r2/hf4cb/T/other-1", "other-1") == "other-1"
        assert fn(None, "/private/var/folders/r2/hf4cb/T/Batch-x", "Batch-x") == (
            "Batch-x"), "case-sensitive, like the prefix rule"

    def test_it_never_reaches_a_record_without_a_cwd(self, monkeypatch):
        fn = self._fn(monkeypatch, ("cwd_glob", "*", "everything"))
        assert fn(None, None, "orphan") == "orphan"
        assert project_identity.implied_name("cwd_glob", "/tmp/x-*") is None

    def test_first_rule_in_insertion_order_wins_across_kinds(self, monkeypatch):
        cwd = "/tmp/run-abc"
        first = self._fn(monkeypatch, ("cwd_glob", "/tmp/run-*", "glob"), ("name", "run-abc", "name"))
        assert first(None, cwd, "run-abc") == "glob"
        flipped = self._fn(monkeypatch, ("name", "run-abc", "name"), ("cwd_glob", "/tmp/run-*", "glob"))
        assert flipped(None, cwd, "run-abc") == "name"

    def test_merge_and_unmerge_accept_it_on_the_command_line(self, capsys, monkeypatch):
        cache_db.init_ccreport_meta(1, "h")
        monkeypatch.setattr(ccr.sys, "argv", [
            "ccreport", "merge", "--kind", "cwd_glob", "/tmp/run-*", "runs"])
        ccr.main()
        out = capsys.readouterr()
        assert "Grouping cwd_glob:/tmp/run-* -> runs" in out.out
        assert [(r["match_kind"], r["match_value"], r["target"])
                for r in cache_db.get_project_overrides()] == [("cwd_glob", "/tmp/run-*", "runs")]
        monkeypatch.setattr(ccr.sys, "argv", [
            "ccreport", "unmerge", "--kind", "cwd_glob", "/tmp/run-*"])
        ccr.main()
        assert cache_db.get_project_overrides() == []

    def test_merge_warns_that_purged_history_without_a_cwd_is_out_of_reach(self, capsys):
        cache_db.init_ccreport_meta(1, "h")
        cache_db.save_ccreport_file("/gone.jsonl", 1, 1, [{
            "mid": "m", "model": "claude-opus-5", "ts": 1.0, "sid": "s",
            "project": "run-abc", "cwd": None, "repo": None, "t": [1, 1, 1, 1]}])
        ccr.cmd_overrides(types.SimpleNamespace(
            command="merge", kind="cwd_glob", source="/tmp/run-*", target="runs"))
        err = capsys.readouterr().err
        assert "1 cached record(s) from purged logs carry no cwd to match" in err
        assert "none of them" in err


class TestOrphanRestamp:
    """A purged log's records are renamed from the cwd they kept."""

    @staticmethod
    def _rec(mid: str, cwd: str | None, project: str, repo: str | None = None) -> dict:
        return {"mid": mid, "model": "claude-opus-5", "ts": 1.0, "sid": "s",
                "project": project, "cwd": cwd, "repo": repo, "t": [1, 1, 1, 1]}

    def _projects(self) -> dict[str, tuple[str, str | None]]:
        conn = cache_db.get_connection()
        return {mid: (project, repo) for mid, project, repo in conn.execute(
            "SELECT mid, project, repo FROM ccreport_records")}

    def test_orphans_are_renamed_and_everything_else_is_left(self, tmp_path, monkeypatch):
        cache_db.init_ccreport_meta(1, "h")
        wt = str(tmp_path / "work" / "app" / ".worktrees" / "fix")
        cache_db.save_ccreport_file("/gone/a.jsonl", 1, 1, [self._rec("orphan", wt, "fix")])
        cache_db.save_ccreport_file("/gone/b.jsonl", 1, 1, [
            self._rec("has-repo", wt, "web", "example.test/org/web")])
        cache_db.save_ccreport_file("/gone/c.jsonl", 1, 1, [self._rec("no-cwd", None, "fix")])
        cache_db.save_ccreport_file("/live/d.jsonl", 1, 1, [self._rec("live", wt, "fix")])
        conn = cache_db.get_connection()
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, 'x')",
                     (cache_db._ORPHAN_FP_KEY,))
        conn.commit()
        _no_remotes(monkeypatch)

        moved = cache_db.restamp_orphan_identities({"/live/d.jsonl"}, scan.project_for_cwd)

        assert moved == 1
        assert self._projects() == {
            "orphan": ("app", None),
            "has-repo": ("web", "example.test/org/web"),
            "no-cwd": ("fix", None),
            "live": ("fix", None),
        }
        assert cache_db._get_meta(conn, cache_db._ORPHAN_FP_KEY) is None, (
            "the orphan totals froze the old name")

    def test_the_repo_a_checkout_still_has_is_stamped_on_too(self, tmp_path, monkeypatch):
        cache_db.init_ccreport_meta(1, "h")
        checkout = tmp_path / "app"
        cache_db.save_ccreport_file("/gone/a.jsonl", 1, 1, [
            self._rec("orphan", str(checkout / ".claude" / "worktrees" / "agent-1"), "agent-1")])
        _no_remotes(monkeypatch, {str(checkout): "example.test/org/app"})
        cache_db.restamp_orphan_identities(set(), scan.project_for_cwd)
        assert self._projects() == {"orphan": ("app", "example.test/org/app")}

    def test_nothing_to_rename_keeps_the_stored_totals(self, tmp_path, monkeypatch):
        cache_db.init_ccreport_meta(1, "h")
        cache_db.save_ccreport_file("/gone/a.jsonl", 1, 1, [
            self._rec("orphan", str(tmp_path / "plain"), "plain")])
        conn = cache_db.get_connection()
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, 'x')",
                     (cache_db._ORPHAN_FP_KEY,))
        conn.commit()
        _no_remotes(monkeypatch)
        assert cache_db.restamp_orphan_identities(set(), scan.project_for_cwd) == 0
        assert cache_db._get_meta(conn, cache_db._ORPHAN_FP_KEY) == "x"

    def test_an_invalidation_runs_it(self, tmp_path, monkeypatch):
        """A changed derivation reaches the purged half, not just the re-parsed one."""
        cache_db.init_ccreport_meta(scan.CACHE_VERSION, "an older hash")
        cache_db.save_ccreport_file("/gone/a.jsonl", 1, 1, [
            self._rec("orphan", str(tmp_path / "app" / ".worktrees" / "x"), "x")])
        _no_remotes(monkeypatch)
        scan._ensure_cache_valid(set())
        assert self._projects() == {"orphan": ("app", None)}
        assert cache_db.check_ccreport_valid(scan.CACHE_VERSION, scan._script_hash())

    def test_an_invalidation_reads_a_scratchpad_back_through_the_cache(
        self, tmp_path, monkeypatch,
    ):
        """The salt is cleared while it runs, and the known cwds must still load."""
        gone = str(tmp_path.resolve() / "site.example")
        cache_db.init_ccreport_meta(scan.CACHE_VERSION, "an older hash")
        cache_db.save_ccreport_file("/gone/main.jsonl", 1, 1, [self._rec("main", gone, "site.example")])
        cache_db.save_ccreport_file("/gone/pad.jsonl", 1, 1, [
            self._rec("pad", _scratchpad(gone), "scratchpad")])
        _no_remotes(monkeypatch)
        scan._ensure_cache_valid(set())
        assert self._projects() == {"main": ("site.example", None), "pad": ("site.example", None)}
