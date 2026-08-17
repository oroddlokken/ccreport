"""Tests for `ccreport update` — the live "master has moved" check.

Neither GitHub nor git is reached: the readers `update_check` exposes are
stubbed per test, and `_pull_ff_only` gets a fake `subprocess.run`. What is
asserted is the wiring — which reader is consulted, what the user is told, and
what the check leaves behind in the meta table for the status line to render.

The three that read `.git` as files are the exception. `TestUpstreamReaders`
builds a real HEAD and config under tmp_path and reads them for real, because
what they parse is git's own on-disk format and a stub would only assert the
stub.
"""

from __future__ import annotations

import argparse
import subprocess

import pytest

from ccreport import cache_db, update_check
from ccreport import ccreport as ccr

SHA = "c" * 40


def _args(pull: bool = False) -> argparse.Namespace:
    return argparse.Namespace(command="update", pull=pull)


def _never(*args, **kwargs):
    raise AssertionError("this reader should not have been consulted")


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout at *tmp_path* with HEAD readable, origin on GitHub, master out."""
    monkeypatch.setattr(update_check, "checkout_root", lambda: tmp_path)
    monkeypatch.setattr(update_check, "local_head_sha", lambda root: SHA)
    monkeypatch.setattr(update_check, "remote_slug", lambda root: "owner/repo")
    monkeypatch.setattr(update_check, "pull_reaches_upstream", lambda root: True)
    return tmp_path


def _behind(monkeypatch, count):
    monkeypatch.setattr(update_check, "commits_behind", lambda slug, sha: count)


def _fake_pull(monkeypatch, returncode=0, stdout="Fast-forward\n", stderr=""):
    seen = []

    def _run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(ccr.subprocess, "run", _run)
    return seen


class TestCheckOutcomes:
    """What each state of the checkout prints."""

    def test_no_checkout_says_so_and_asks_nobody(self, monkeypatch, capsys):
        """An installed package has no .git, so the API is never called."""
        monkeypatch.setattr(update_check, "checkout_root", lambda: None)
        monkeypatch.setattr(update_check, "commits_behind", _never)
        ccr.cmd_update(_args())
        assert "Installed as a package" in capsys.readouterr().out

    def test_an_unreadable_head_stops_before_the_remote(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(update_check, "checkout_root", lambda: tmp_path)
        monkeypatch.setattr(update_check, "local_head_sha", lambda root: None)
        monkeypatch.setattr(update_check, "remote_slug", _never)
        ccr.cmd_update(_args())
        assert "Could not read HEAD" in capsys.readouterr().out

    def test_a_non_github_origin_has_nothing_to_compare(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(update_check, "checkout_root", lambda: tmp_path)
        monkeypatch.setattr(update_check, "local_head_sha", lambda root: SHA)
        monkeypatch.setattr(update_check, "remote_slug", lambda root: None)
        monkeypatch.setattr(update_check, "commits_behind", _never)
        ccr.cmd_update(_args())
        assert "not a GitHub remote" in capsys.readouterr().out

    def test_an_unanswered_check_is_not_up_to_date(self, checkout, monkeypatch, capsys):
        _behind(monkeypatch, None)
        ccr.cmd_update(_args())
        out = capsys.readouterr().out
        assert "Could not reach GitHub" in out
        assert "Up to date" not in out

    def test_zero_is_up_to_date(self, checkout, monkeypatch, capsys):
        _behind(monkeypatch, 0)
        ccr.cmd_update(_args())
        assert "Up to date with origin/master" in capsys.readouterr().out

    def test_one_commit_is_singular(self, checkout, monkeypatch, capsys):
        _behind(monkeypatch, 1)
        ccr.cmd_update(_args())
        assert "1 commit behind origin/master." in capsys.readouterr().out

    def test_several_commits_name_the_count_and_the_way_out(
        self, checkout, monkeypatch, capsys
    ):
        _behind(monkeypatch, 7)
        ccr.cmd_update(_args())
        out = capsys.readouterr().out
        assert "7 commits behind origin/master." in out
        assert "ccreport update --pull" in out


class TestWhatTheCheckStores:
    """The status line reads the same three meta keys this writes."""

    def test_a_count_lands_beside_the_sha_it_compared(
        self, checkout, monkeypatch, capsys
    ):
        _behind(monkeypatch, 4)
        ccr.cmd_update(_args())
        capsys.readouterr()
        checked_at, sha, behind = cache_db.read_update_check()
        assert (sha, behind) == (SHA, 4)
        assert checked_at > 0

    def test_an_unanswered_check_stamps_without_a_count(
        self, checkout, monkeypatch, capsys
    ):
        """None, not 0 — the status line must not render up to date on no evidence."""
        _behind(monkeypatch, None)
        ccr.cmd_update(_args())
        capsys.readouterr()
        checked_at, sha, behind = cache_db.read_update_check()
        assert behind is None
        assert (sha, checked_at > 0) == (SHA, True)

    def test_an_unreadable_head_clears_the_stored_count(
        self, tmp_path, monkeypatch, capsys
    ):
        cache_db.write_update_check(SHA, 9, 1.0)
        monkeypatch.setattr(update_check, "checkout_root", lambda: tmp_path)
        monkeypatch.setattr(update_check, "local_head_sha", lambda root: None)
        ccr.cmd_update(_args())
        capsys.readouterr()
        assert cache_db.read_update_check()[2] is None


class TestPull:
    """--pull fast-forwards, and only when there is something to fast-forward."""

    def test_up_to_date_runs_no_git(self, checkout, monkeypatch, capsys):
        seen = _fake_pull(monkeypatch)
        _behind(monkeypatch, 0)
        ccr.cmd_update(_args(pull=True))
        assert seen == []

    def test_behind_runs_ff_only_in_the_checkout(self, checkout, monkeypatch, capsys):
        seen = _fake_pull(monkeypatch)
        _behind(monkeypatch, 2)
        ccr.cmd_update(_args(pull=True))
        assert seen == [["git", "-C", str(checkout), "pull", "--ff-only"]]
        assert "Fast-forward" in capsys.readouterr().out

    def test_without_pull_git_is_never_run(self, checkout, monkeypatch, capsys):
        seen = _fake_pull(monkeypatch)
        _behind(monkeypatch, 2)
        ccr.cmd_update(_args())
        assert seen == []

    def test_a_refused_fast_forward_exits_nonzero(self, checkout, monkeypatch, capsys):
        _fake_pull(monkeypatch, returncode=128, stdout="", stderr="Not possible to fast-forward")
        _behind(monkeypatch, 2)
        with pytest.raises(SystemExit) as exc:
            ccr.cmd_update(_args(pull=True))
        assert exc.value.code == 128
        err = capsys.readouterr().err
        assert "Not possible to fast-forward" in err
        assert "Resolve it with git" in err

    def test_git_missing_is_reported_not_raised(self, checkout, monkeypatch, capsys):
        def _boom(cmd, **kwargs):
            raise OSError("no git")

        monkeypatch.setattr(ccr.subprocess, "run", _boom)
        _behind(monkeypatch, 2)
        with pytest.raises(SystemExit) as exc:
            ccr.cmd_update(_args(pull=True))
        assert exc.value.code == 1
        assert "Could not run git pull" in capsys.readouterr().err


class TestPullRefusesWhatItCannotReach:
    """A pull that would follow another ref is not run at all."""

    def _elsewhere(self, monkeypatch, branch, tracks):
        monkeypatch.setattr(update_check, "pull_reaches_upstream", lambda root: False)
        monkeypatch.setattr(update_check, "head_branch", lambda root: branch)
        monkeypatch.setattr(update_check, "tracked_upstream", lambda root: tracks)

    def test_a_fork_topic_branch_is_named_and_git_stays_unrun(
        self, checkout, monkeypatch, capsys
    ):
        """The bug this gate exists for: pulling fork/topic never moves the count."""
        seen = _fake_pull(monkeypatch)
        self._elsewhere(monkeypatch, "legend-width", "fork/legend-width")
        _behind(monkeypatch, 17)
        with pytest.raises(SystemExit) as exc:
            ccr.cmd_update(_args(pull=True))
        assert exc.value.code == 1
        assert seen == []
        err = capsys.readouterr().err
        assert "legend-width tracks fork/legend-width" in err
        assert "origin/master" in err

    def test_the_count_is_still_printed_before_the_refusal(
        self, checkout, monkeypatch, capsys
    ):
        """Knowing master moved is worth having even where this command cannot help."""
        _fake_pull(monkeypatch)
        self._elsewhere(monkeypatch, "topic", "fork/topic")
        _behind(monkeypatch, 17)
        with pytest.raises(SystemExit):
            ccr.cmd_update(_args(pull=True))
        assert "17 commits behind origin/master." in capsys.readouterr().out

    def test_a_branch_with_no_upstream_says_so(self, checkout, monkeypatch, capsys):
        _fake_pull(monkeypatch)
        self._elsewhere(monkeypatch, "scratch", None)
        _behind(monkeypatch, 3)
        with pytest.raises(SystemExit):
            ccr.cmd_update(_args(pull=True))
        assert "scratch tracks no remote branch" in capsys.readouterr().err

    def test_a_detached_head_says_so(self, checkout, monkeypatch, capsys):
        _fake_pull(monkeypatch)
        self._elsewhere(monkeypatch, None, None)
        _behind(monkeypatch, 3)
        with pytest.raises(SystemExit):
            ccr.cmd_update(_args(pull=True))
        assert "HEAD is detached" in capsys.readouterr().err

    def test_without_pull_the_gate_is_never_asked(self, checkout, monkeypatch, capsys):
        """Reporting the count works from any branch; only the pull is refused."""
        monkeypatch.setattr(update_check, "pull_reaches_upstream", _never)
        _behind(monkeypatch, 3)
        ccr.cmd_update(_args())
        assert "3 commits behind origin/master." in capsys.readouterr().out


class TestUpstreamReaders:
    """`.git/HEAD` and `.git/config` as git actually writes them."""

    def _checkout(self, root, head, config=""):
        git = root / ".git"
        git.mkdir()
        (git / "HEAD").write_text(head, encoding="utf-8")
        if config:
            (git / "config").write_text(config, encoding="utf-8")
        return root

    MASTER = (
        '[remote "origin"]\n'
        "\turl = https://github.com/owner/repo.git\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        '[branch "master"]\n'
        "\tremote = origin\n"
        "\tmerge = refs/heads/master\n"
    )

    def test_head_names_the_branch_it_points_at(self, tmp_path):
        self._checkout(tmp_path, "ref: refs/heads/master\n")
        assert update_check.head_branch(tmp_path) == "master"

    def test_a_slash_in_the_branch_name_survives(self, tmp_path):
        self._checkout(tmp_path, "ref: refs/heads/feat/nested\n")
        assert update_check.head_branch(tmp_path) == "feat/nested"

    def test_a_detached_head_names_no_branch(self, tmp_path):
        self._checkout(tmp_path, f"{SHA}\n")
        assert update_check.head_branch(tmp_path) is None

    def test_a_missing_head_names_no_branch(self, tmp_path):
        assert update_check.head_branch(tmp_path) is None

    def test_the_tracking_pair_reads_as_one_ref(self, tmp_path):
        """Tab-indented keys under a quoted subsection — git's own layout."""
        self._checkout(tmp_path, "ref: refs/heads/master\n", self.MASTER)
        assert update_check.tracked_upstream(tmp_path) == "origin/master"

    def test_a_branch_without_a_section_tracks_nothing(self, tmp_path):
        self._checkout(tmp_path, "ref: refs/heads/scratch\n", self.MASTER)
        assert update_check.tracked_upstream(tmp_path) is None

    def test_a_fork_topic_branch_tracks_the_fork(self, tmp_path):
        config = self.MASTER + '[branch "topic"]\n\tremote = fork\n\tmerge = refs/heads/topic\n'
        self._checkout(tmp_path, "ref: refs/heads/topic\n", config)
        assert update_check.tracked_upstream(tmp_path) == "fork/topic"

    def test_a_missing_config_tracks_nothing(self, tmp_path):
        self._checkout(tmp_path, "ref: refs/heads/master\n")
        assert update_check.tracked_upstream(tmp_path) is None

    def test_a_local_name_may_differ_from_the_ref_it_tracks(self, tmp_path):
        """`main` set to pull origin/master is reachable, whatever it is called here."""
        config = self.MASTER + '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/master\n'
        self._checkout(tmp_path, "ref: refs/heads/main\n", config)
        assert update_check.pull_reaches_upstream(tmp_path) is True

    def test_the_fork_topic_branch_is_the_case_that_fails(self, tmp_path):
        config = self.MASTER + '[branch "topic"]\n\tremote = fork\n\tmerge = refs/heads/topic\n'
        self._checkout(tmp_path, "ref: refs/heads/topic\n", config)
        assert update_check.pull_reaches_upstream(tmp_path) is False

    def test_master_tracking_origin_is_the_case_that_works(self, tmp_path):
        self._checkout(tmp_path, "ref: refs/heads/master\n", self.MASTER)
        assert update_check.pull_reaches_upstream(tmp_path) is True


class TestParsing:
    """The subcommand as argparse sees it."""

    def test_update_takes_no_positional_and_defaults_to_reporting(self, monkeypatch):
        called = []
        monkeypatch.setattr(ccr, "cmd_update", called.append)
        monkeypatch.setattr("sys.argv", ["ccreport", "update"])
        ccr.main()
        assert [(a.command, a.pull) for a in called] == [("update", False)]

    def test_the_pull_flag_reaches_the_command(self, monkeypatch):
        called = []
        monkeypatch.setattr(ccr, "cmd_update", called.append)
        monkeypatch.setattr("sys.argv", ["ccreport", "update", "--pull"])
        ccr.main()
        assert [a.pull for a in called] == [True]
