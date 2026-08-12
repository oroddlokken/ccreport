"""Tests for update_check.py — the twice-a-day "master has moved" check.

GitHub is never called: every test patches the urlopen underneath
``commits_behind``, and every git invocation is a stubbed ``subprocess.run``.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
from email.message import Message

import pytest

from ccreport import cache_db, update_check

SHA = "a" * 40
OTHER_SHA = "b" * 40


def _mkrepo(tmp_path, head: str = "ref: refs/heads/master\n"):
    """A tree shaped like the checkout: <root>/src/ccreport/, <root>/.git/."""
    (tmp_path / "src" / "ccreport").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(head, encoding="utf-8")
    return tmp_path


def _as_module(monkeypatch, root):
    """Point update_check at *root* by moving where it thinks it lives."""
    monkeypatch.setattr(
        update_check, "__file__", str(root / "src" / "ccreport" / "update_check.py"))


def _serve(monkeypatch, body, status_code=None):
    """Answer the compare request with *body*, or raise an HTTPError instead."""
    def _urlopen(req, timeout=None):
        if status_code is not None:
            raise urllib.error.HTTPError(req.full_url, status_code, "no", Message(), None)
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def _serve_git(monkeypatch, stdout: str, returncode: int = 0):
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(subprocess, "run", _run)


class TestCheckoutRoot:
    """Whether there is a checkout to pull into at all."""

    def test_a_git_directory_beside_src_is_the_root(self, tmp_path, monkeypatch):
        root = _mkrepo(tmp_path)
        _as_module(monkeypatch, root)
        assert update_check.checkout_root() == root

    def test_no_git_directory_is_no_checkout(self, tmp_path, monkeypatch):
        """What `uv tool install .` leaves: the package, no repository."""
        (tmp_path / "src" / "ccreport").mkdir(parents=True)
        _as_module(monkeypatch, tmp_path)
        assert update_check.checkout_root() is None

    def test_a_git_file_is_no_checkout(self, tmp_path, monkeypatch):
        """A submodule or worktree points elsewhere; the readers want the directory."""
        (tmp_path / "src" / "ccreport").mkdir(parents=True)
        (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        _as_module(monkeypatch, tmp_path)
        assert update_check.checkout_root() is None


class TestLocalHeadSha:
    """Reading HEAD out of .git, in each layout git leaves it in."""

    def test_a_loose_ref(self, tmp_path):
        root = _mkrepo(tmp_path)
        refs = root / ".git" / "refs" / "heads"
        refs.mkdir(parents=True)
        (refs / "master").write_text(f"{SHA}\n", encoding="utf-8")
        assert update_check.local_head_sha(root) == SHA

    def test_a_detached_head_holds_the_sha_itself(self, tmp_path):
        root = _mkrepo(tmp_path, head=f"{SHA}\n")
        assert update_check.local_head_sha(root) == SHA

    def test_packed_refs_when_no_loose_file_exists(self, tmp_path):
        """Where a fresh clone keeps every branch until something writes to one."""
        root = _mkrepo(tmp_path)
        (root / ".git" / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{OTHER_SHA} refs/heads/other\n"
            f"{SHA} refs/heads/master\n"
            f"^{OTHER_SHA}\n",
            encoding="utf-8",
        )
        assert update_check.local_head_sha(root) == SHA

    def test_a_loose_ref_wins_over_packed_refs(self, tmp_path):
        root = _mkrepo(tmp_path)
        refs = root / ".git" / "refs" / "heads"
        refs.mkdir(parents=True)
        (refs / "master").write_text(f"{SHA}\n", encoding="utf-8")
        (root / ".git" / "packed-refs").write_text(
            f"{OTHER_SHA} refs/heads/master\n", encoding="utf-8")
        assert update_check.local_head_sha(root) == SHA

    def test_a_ref_named_nowhere_is_none(self, tmp_path):
        root = _mkrepo(tmp_path)
        (root / ".git" / "packed-refs").write_text(
            f"{OTHER_SHA} refs/heads/other\n", encoding="utf-8")
        assert update_check.local_head_sha(root) is None

    def test_no_head_file_is_none(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert update_check.local_head_sha(tmp_path) is None

    def test_a_head_that_is_neither_sha_nor_ref_is_none(self, tmp_path):
        root = _mkrepo(tmp_path, head="not a ref\n")
        assert update_check.local_head_sha(root) is None


class TestRemoteSlug:
    """owner/repo, derived from origin so a fork checks itself."""

    CASES = [
        ("git@github.com:oroddlokken/ccreport.git", "oroddlokken/ccreport"),
        ("git@github.com:oroddlokken/ccreport", "oroddlokken/ccreport"),
        ("https://github.com/oroddlokken/ccreport.git", "oroddlokken/ccreport"),
        ("https://github.com/oroddlokken/ccreport", "oroddlokken/ccreport"),
        ("ssh://git@github.com/someone/fork.git", "someone/fork"),
    ]

    @pytest.mark.parametrize(("url", "slug"), CASES)
    def test_every_form_git_clone_writes(self, monkeypatch, tmp_path, url, slug):
        _serve_git(monkeypatch, f"{url}\n")
        assert update_check.remote_slug(tmp_path) == slug

    def test_a_remote_elsewhere_is_none(self, monkeypatch, tmp_path):
        _serve_git(monkeypatch, "git@gitlab.com:someone/ccreport.git\n")
        assert update_check.remote_slug(tmp_path) is None

    def test_no_origin_is_none(self, monkeypatch, tmp_path):
        _serve_git(monkeypatch, "", returncode=128)
        assert update_check.remote_slug(tmp_path) is None

    def test_no_git_on_path_is_none(self, monkeypatch, tmp_path):
        def _boom(cmd, **kwargs):
            raise OSError

        monkeypatch.setattr(subprocess, "run", _boom)
        assert update_check.remote_slug(tmp_path) is None


class TestCommitsBehind:
    """What the compare endpoint's answer becomes."""

    def test_ahead_reports_its_count(self, monkeypatch):
        _serve(monkeypatch, {"status": "ahead", "ahead_by": 12})
        assert update_check.commits_behind("o/r", SHA) == 12

    def test_diverged_counts_only_masters_side(self, monkeypatch):
        """Local commits of your own do not hide the ones you are missing."""
        _serve(monkeypatch, {"status": "diverged", "ahead_by": 3, "behind_by": 1})
        assert update_check.commits_behind("o/r", SHA) == 3

    def test_identical_is_zero(self, monkeypatch):
        _serve(monkeypatch, {"status": "identical", "ahead_by": 0})
        assert update_check.commits_behind("o/r", SHA) == 0

    def test_behind_is_zero(self, monkeypatch):
        """Unpushed commits, nothing to pull."""
        _serve(monkeypatch, {"status": "behind", "ahead_by": 0, "behind_by": 2})
        assert update_check.commits_behind("o/r", SHA) == 0

    def test_the_url_carries_the_sha_and_the_branch(self, monkeypatch):
        seen = []

        def _urlopen(req, timeout=None):
            seen.append(req.full_url)
            return io.BytesIO(b'{"status": "identical", "ahead_by": 0}')

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        update_check.commits_behind("owner/repo", SHA)
        assert seen == [
            (
                f"https://api.github.com/repos/owner/repo/compare/"
                f"{SHA}...{update_check.UPSTREAM_BRANCH}"
            ),
        ]

    @pytest.mark.parametrize("code", [403, 404, 500])
    def test_an_http_error_is_unanswered_not_zero(self, monkeypatch, code):
        """404 is the normal state for an unpushed commit; 403 is the rate limit."""
        _serve(monkeypatch, None, status_code=code)
        assert update_check.commits_behind("o/r", SHA) is None

    def test_an_unreachable_host_is_unanswered(self, monkeypatch):
        def _urlopen(req, timeout=None):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        assert update_check.commits_behind("o/r", SHA) is None

    def test_a_body_that_is_not_json_is_unanswered(self, monkeypatch):
        def _urlopen(req, timeout=None):
            return io.BytesIO(b"<html>rate limited</html>")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        assert update_check.commits_behind("o/r", SHA) is None

    def test_a_count_that_is_not_a_number_is_unanswered(self, monkeypatch):
        _serve(monkeypatch, {"status": "ahead", "ahead_by": "lots"})
        assert update_check.commits_behind("o/r", SHA) is None


class TestRun:
    """One check, end to end, and what it leaves in the cache."""

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        root = _mkrepo(tmp_path, head=f"{SHA}\n")
        _as_module(monkeypatch, root)
        _serve_git(monkeypatch, "git@github.com:oroddlokken/ccreport.git\n")
        return root

    def test_it_stores_the_count_the_sha_and_the_stamp(self, repo, monkeypatch):
        _serve(monkeypatch, {"status": "ahead", "ahead_by": 7})
        update_check.run()
        checked_at, sha, behind = cache_db.read_update_check()
        assert (sha, behind) == (SHA, 7)
        assert checked_at > 0

    def test_a_failed_check_still_advances_the_stamp(self, repo, monkeypatch):
        """What paces the spawn: an unreachable API must not respawn every render."""
        _serve(monkeypatch, None, status_code=404)
        update_check.run()
        checked_at, _sha, behind = cache_db.read_update_check()
        assert behind is None
        assert checked_at > 0

    def test_a_failed_check_clears_a_count_it_could_not_confirm(self, repo, monkeypatch):
        _serve(monkeypatch, {"status": "ahead", "ahead_by": 7})
        update_check.run()
        _serve(monkeypatch, None, status_code=403)
        update_check.run()
        assert cache_db.read_update_check()[2] is None

    def test_a_non_github_remote_stores_no_count(self, repo, monkeypatch):
        _serve_git(monkeypatch, "git@gitlab.com:someone/ccreport.git\n")

        def _boom(req, timeout=None):
            msg = "no request without a GitHub slug"
            raise AssertionError(msg)

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        update_check.run()
        _checked_at, _sha, behind = cache_db.read_update_check()
        assert behind is None

    def test_no_checkout_writes_nothing(self, tmp_path, monkeypatch):
        (tmp_path / "src" / "ccreport").mkdir(parents=True)
        _as_module(monkeypatch, tmp_path)
        update_check.run()
        assert cache_db.read_update_check() == (0.0, "", None)

    def test_main_swallows_what_run_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError

        monkeypatch.setattr(update_check, "run", _boom)
        update_check.main()  # a detached child has nobody to report to


class TestUpdateCheckStore:
    """The meta round trip, including the states that mean 'no number'."""

    def test_a_never_run_check_reads_empty(self):
        assert cache_db.read_update_check() == (0.0, "", None)

    def test_zero_survives_as_zero(self):
        """Up to date is a fact; None is the absence of one."""
        cache_db.write_update_check(SHA, 0, 1_000_000.0)
        assert cache_db.read_update_check() == (1_000_000.0, SHA, 0)

    def test_none_clears_the_count_and_keeps_the_stamp(self):
        cache_db.write_update_check(SHA, 4, 1_000_000.0)
        cache_db.write_update_check(SHA, None, 1_000_100.0)
        assert cache_db.read_update_check() == (1_000_100.0, SHA, None)
