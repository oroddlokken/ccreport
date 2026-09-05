"""Tests for ``scripts/check-sdist`` against synthetic tarballs.

The cases are the two ways the tarball goes wrong: a swept-in directory the
include-list was added to keep out, and a tarball past its ceiling. Building a
real sdist would cost seconds per case and pin the test to hatchling's
behaviour rather than the script's, so each tarball is assembled by hand.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-sdist"

ROOT = "ccreport-1.2.3"

# Mirrors the sdist hatchling produces: the include-list's own paths plus the
# three files hatchling adds unasked.
CLEAN_MEMBERS = (
    ".gitignore",
    "CHANGELOG.md",
    "PKG-INFO",
    "README.md",
    "bin/ccreport",
    "docs/calculation-reference.md",
    "pyproject.toml",
    "src/ccreport/__init__.py",
)


def _make_tarball(
    path: Path,
    members: tuple[str, ...] = CLEAN_MEMBERS,
    payload: bytes = b"x",
) -> Path:
    """Write a gzipped tarball whose members sit under a single root dir."""
    src = path.parent / "build"
    with tarfile.open(path, "w:gz") as tar:
        for member in members:
            built = src / member
            built.parent.mkdir(parents=True, exist_ok=True)
            built.write_bytes(payload)
            tar.add(built, arcname=f"{ROOT}/{member}")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run check-sdist and capture both streams."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.fixture
def tarball(tmp_path: Path) -> Path:
    """Return a tarball shaped like a clean release."""
    return _make_tarball(tmp_path / f"{ROOT}.tar.gz")


class TestAccepts:
    """A tarball shaped like the one hatchling ships."""

    def test_clean_tarball_passes(self, tarball: Path) -> None:
        """The include-list's own entries clear every check."""
        result = _run(str(tarball))
        assert result.returncode == 0, result.stderr
        assert "Package present" in result.stdout

    def test_entry_count_matches_the_tarball(self, tarball: Path) -> None:
        """The reported count matches the tarball's distinct top-level names."""
        expected = len({member.split("/")[0] for member in CLEAN_MEMBERS})
        result = _run(str(tarball))
        assert f"({expected})" in result.stdout, result.stdout


class TestRejects:
    """Tarballs carrying what the include-list exists to keep out."""

    def test_swept_in_issue_store_fails(self, tmp_path: Path) -> None:
        """.dogcats is 2.9 MB of issue store, and hatchling would ship it."""
        path = _make_tarball(
            tmp_path / f"{ROOT}.tar.gz",
            (*CLEAN_MEMBERS, ".dogcats/issues.jsonl"),
        )
        result = _run(str(path))
        assert result.returncode != 0
        assert ".dogcats" in result.stderr

    def test_screenshot_directory_fails(self, tmp_path: Path) -> None:
        """A directory outside the allowlist is named in the failure."""
        path = _make_tarball(
            tmp_path / f"{ROOT}.tar.gz",
            (*CLEAN_MEMBERS, "assets/dashboard.png"),
        )
        result = _run(str(path))
        assert result.returncode != 0
        assert "assets" in result.stderr

    def test_oversized_tarball_fails(self, tmp_path: Path) -> None:
        """Size is the first gate, so it fails before the entry check runs."""
        path = _make_tarball(tmp_path / f"{ROOT}.tar.gz")
        result = _run("--max-bytes", "10", str(path))
        assert result.returncode != 0
        assert "ceiling" in result.stderr

    def test_tarball_without_the_package_fails(self, tmp_path: Path) -> None:
        """An sdist holding no src/ passes size and entries while being useless."""
        path = _make_tarball(
            tmp_path / f"{ROOT}.tar.gz",
            tuple(m for m in CLEAN_MEMBERS if not m.startswith("src/")),
        )
        result = _run(str(path))
        assert result.returncode != 0
        assert "no package" in result.stderr

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        """A path that does not exist fails before any tar is read."""
        result = _run(str(tmp_path / "absent.tar.gz"))
        assert result.returncode != 0
        assert "Not a file" in result.stderr


class TestDistDiscovery:
    """The no-argument form, which resolves dist/*.tar.gz itself."""

    def _run_in(self, cwd: Path) -> subprocess.CompletedProcess[str]:
        """Run check-sdist with no argument from ``cwd``."""
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_finds_the_single_dist_tarball(self, tmp_path: Path) -> None:
        """One tarball in dist/ is checked without being named."""
        dist = tmp_path / "dist"
        dist.mkdir()
        _make_tarball(dist / f"{ROOT}.tar.gz")
        result = self._run_in(tmp_path)
        assert result.returncode == 0, result.stderr

    def test_ambiguous_dist_fails(self, tmp_path: Path) -> None:
        """Two tarballs leave no basis to pick, so the guard refuses."""
        dist = tmp_path / "dist"
        dist.mkdir()
        _make_tarball(dist / f"{ROOT}.tar.gz")
        _make_tarball(dist / "ccreport-1.2.4.tar.gz")
        result = self._run_in(tmp_path)
        assert result.returncode != 0
        assert "Name one" in result.stderr

    def test_empty_dist_fails(self, tmp_path: Path) -> None:
        """An empty dist/ points at the build command instead of passing."""
        (tmp_path / "dist").mkdir()
        result = self._run_in(tmp_path)
        assert result.returncode != 0
        assert "uv build" in result.stderr
