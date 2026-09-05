"""Tests for ``scripts/release-prep`` against a local bare-repo remote.

The script switches branches in the clone rather than in a worktree, so the
cases here cover what that costs: the branch it leaves you on, a release
branch that exists only on the remote, and a rejected push that must not
publish its RC tag.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release-prep"

CHANGELOG = "# CHANGELOG\n\n## [Unreleased]\n\n### Fixed\n\n- Something\n"

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}
"""Env that keeps test repos out of reach of the machine's real git config."""


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stdout, raising on a non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


def _make_gh_stub(bin_dir: Path) -> None:
    """Put a ``gh`` on PATH that reports no PR and creates a fake one.

    release-prep calls gh for PR discovery and creation; neither is under test
    here, and a real gh would try to reach GitHub.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1 $2" == "pr create" ]]; then\n'
        '    echo "https://example.invalid/pr/1"\n'
        "    exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _make_watch_gh_stub(bin_dir: Path, counter: Path) -> None:
    """Put a ``gh`` on PATH whose check rollup fills only on the second poll.

    The counter file carries across invocations, which is how one stub reports
    an empty rollup once and a registered check after that.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'args="$*"\n'
        'case "$args" in\n'
        '    *"--json state,reviewDecision"*) exit 1 ;;\n'
        '    *"--json statusCheckRollup"*)\n'
        f"        n=$(cat {counter} 2>/dev/null || echo 0)\n"
        f'        n=$((n + 1)); echo "$n" > {counter}\n'
        '        if [[ "$n" -ge 2 ]]; then echo 1; else echo 0; fi ;;\n'
        '    *"--json state"*) echo MERGED ;;\n'
        '    "pr checks"*)\n'
        f"        n=$(cat {counter} 2>/dev/null || echo 0)\n"
        '        if [[ "$n" -ge 2 ]]; then exit 0; fi\n'
        '        echo "no checks reported on the branch" >&2; exit 1 ;;\n'
        '    "pr create"*) echo "https://example.invalid/pr/1" ;;\n'
        '    "run list"*) echo 12345 ;;\n'
        '    "run watch"*) exit 0 ;;\n'
        "    *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    # The poll sleeps 5s between tries; the script resolves sleep through PATH.
    naptime = bin_dir / "sleep"
    naptime.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    naptime.chmod(0o755)


def _run_script(
    repo: Path,
    bin_dir: Path,
    args: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    """Run release-prep in ``repo`` with the gh stub ahead of the real PATH."""
    env = {
        **os.environ,
        **_GIT_ENV,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


@pytest.fixture
def remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Return a bare remote plus a clone on ``master`` holding a CHANGELOG."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "master", "-q", str(remote))

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "master", "-q")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Initial")
    git(repo, "push", "-qu", "origin", "master")
    return remote, repo


@pytest.fixture
def gh_stub(tmp_path: Path) -> Path:
    """Return a bin directory holding the ``gh`` stub."""
    bin_dir = tmp_path / "bin"
    _make_gh_stub(bin_dir)
    return bin_dir


class TestUsage:
    """The forms that touch nothing."""

    def test_help_prints_usage_and_leaves_the_tree(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        _remote, repo = remote_and_clone
        result = _run_script(repo, gh_stub, ["--help"])
        assert result.returncode == 0
        assert "Usage:" in result.stdout
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "tag", "-l") == ""


class TestRemoteOnlyBranch:
    """A release branch on the remote that the clone does not have."""

    def test_reuses_remote_branch_instead_of_forking(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """The rerun builds on the remote tip and pushes without a conflict.

        Probing only ``refs/heads/<branch>`` would branch off master and
        produce a sibling of the remote tip that no push can fast-forward.
        """
        remote, repo = remote_and_clone

        # Previous RC, cut elsewhere: branch and tag exist only on the remote.
        git(repo, "checkout", "-q", "-b", "release/v9.9.9")
        (repo / "CHANGELOG.md").write_text(
            CHANGELOG.replace(
                "## [Unreleased]",
                "## [Unreleased]\n\n## 9.9.9 (2026-01-01)",
            ),
            encoding="utf-8",
        )
        git(repo, "commit", "-qam", "Prepare changelog for v9.9.9")
        git(repo, "tag", "-a", "v9.9.9-rc.1", "-m", "rc1")
        git(repo, "push", "-q", "origin", "release/v9.9.9", "--tags")
        git(repo, "checkout", "-q", "master")
        git(repo, "branch", "-qD", "release/v9.9.9")
        git(repo, "tag", "-d", "v9.9.9-rc.1")

        # A fix lands on master after that RC, which is why another is cut.
        (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "Fix something")
        git(repo, "push", "-q", "origin", "master")

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.9"])

        assert result.returncode == 0, result.stdout + result.stderr

        remote_tip = git(remote, "rev-parse", "release/v9.9.9").strip()
        assert git(repo, "rev-parse", "release/v9.9.9").strip() == remote_tip
        # The branch carries the fix, so it was re-cut from master rather than
        # left at the previous RC.
        assert "fix.txt" in git(repo, "ls-tree", "--name-only", remote_tip)
        assert git(remote, "rev-parse", "v9.9.9-rc.2^{}").strip() == remote_tip

    def test_leaves_the_clone_on_the_original_branch(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """The checkout is undone on exit, however the run ended."""
        _remote, repo = remote_and_clone

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.7"])

        assert result.returncode == 0, result.stdout + result.stderr
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "master"


class TestRejectedBranchPush:
    """A branch push the remote refuses must not leave a tag behind."""

    def test_rejected_push_publishes_no_tag(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """``push --tags`` would send the RC tag even when the branch failed."""
        remote, repo = remote_and_clone

        # The remote branch holds a commit this clone's branch does not, so
        # the non-fast-forward push is rejected.
        git(repo, "checkout", "-q", "-b", "release/v9.9.8")
        (repo / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "Their release work")
        git(repo, "push", "-q", "origin", "release/v9.9.8")
        git(repo, "checkout", "-q", "master")
        # Rewind the local branch so it no longer contains the remote tip.
        git(repo, "branch", "-f", "release/v9.9.8", "master")

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.8"])

        assert result.returncode != 0, result.stdout
        assert git(remote, "tag", "-l", "v9.9.8-rc.1").strip() == ""
        assert git(repo, "tag", "-l", "v9.9.8-rc.1").strip() == ""
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "master"


class TestDirtyTree:
    """An uncommitted edit stops the run before any branch is touched."""

    def test_dirty_tree_aborts(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """The edit would otherwise ride into the release commit."""
        _remote, repo = remote_and_clone
        (repo / "CHANGELOG.md").write_text("edited\n", encoding="utf-8")

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.6"])

        assert result.returncode != 0
        assert "dirty" in result.stderr
        assert git(repo, "branch", "-l", "release/v9.9.6").strip() == ""


class TestWatchPhase:
    """The wait for PR checks, which runs when -W is not passed."""

    def test_waits_for_the_first_check_to_register(
        self,
        tmp_path: Path,
        remote_and_clone: tuple[Path, Path],
    ) -> None:
        """An empty rollup means the check has not registered, not that it failed.

        gh exits non-zero for both, which ended cboard2's first real release
        run after every push had already landed.
        """
        _remote, repo = remote_and_clone
        counter = tmp_path / "rollup-calls"
        bin_dir = tmp_path / "watchbin"
        _make_watch_gh_stub(bin_dir, counter)

        result = _run_script(repo, bin_dir, ["--skip-checks", "9.9.5"])

        assert result.returncode == 0, result.stdout + result.stderr
        assert counter.read_text(encoding="utf-8").strip() == "2"
        assert "All checks passed" in result.stdout
