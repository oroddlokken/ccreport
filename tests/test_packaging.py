"""What an install ships, and how the wrappers find the package inside one.

A wheel carries the two console scripts and the four shell wrappers; a checkout
carries the wrappers in bin/. Each test here fails on the drift that would
leave a Homebrew install with no way to reach the status line.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from ccreport import ccreport

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class TestEntryPoints:
    """The commands a wheel install puts on PATH."""

    @pytest.mark.parametrize("name", ["ccreport", "ccu", "ccreport-statusline", "ccreport-quota-guard"])
    def test_the_console_script_resolves_to_a_callable(self, name):
        target = PYPROJECT["project"]["scripts"][name]
        module, _, attr = target.partition(":")
        assert callable(getattr(importlib.import_module(module), attr))


class TestShippedScripts:
    """The wrappers settings.json points at, which are paths rather than commands."""

    def test_the_wheel_ships_what_the_cli_names(self):
        """`ccreport scripts` lists these, so the two lists are one list."""
        included = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        landed = {Path(dest).name for dest in included.values()}
        assert landed == set(ccreport.SHIPPED_SCRIPTS)

    def test_every_wrapper_lands_under_the_package(self):
        """Inside ccreport/ is what makes scripts_dir() answer on any install."""
        included = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        for source, dest in included.items():
            assert (ROOT / source).is_file()
            assert Path(dest).parent == Path("ccreport/scripts")

    def test_the_checkout_answers_with_bin(self):
        assert ccreport.scripts_dir() == ROOT / "bin"

    def test_the_command_prints_one_path_per_wrapper(self, capsys):
        ccreport.cmd_scripts()
        printed = capsys.readouterr().out.split()
        assert printed == [str(ROOT / "bin" / name) for name in ccreport.SHIPPED_SCRIPTS]

    def test_no_wrappers_anywhere_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(ccreport, "scripts_dir", lambda: None)
        with pytest.raises(SystemExit) as exc:
            ccreport.cmd_scripts()
        assert exc.value.code == 1
        assert "no wrappers" in capsys.readouterr().err


def _installed_tree(tmp_path: Path, wrapper: str) -> tuple[Path, Path, Path]:
    """Lay a wrapper out the way a wheel installs it, under a venv's site-packages.

    Returns the import root the wrapper should pick, the interpreter it should
    prefer, and the wrapper itself.
    """
    site = tmp_path / "venv" / "lib" / "python3.13" / "site-packages"
    scripts = site / "ccreport" / "scripts"
    scripts.mkdir(parents=True)
    (site / "ccreport" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(ROOT / "bin" / wrapper, scripts / wrapper)

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    return site, venv_bin / "python3", scripts / wrapper


def _fake_python(path: Path, marker: Path, label: str) -> None:
    """An interpreter that records who ran it and what import root it was handed."""
    path.write_text(f'#!/bin/sh\necho "{label} $3" > {marker}\n', encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("wrapper", ["statusline-command_x.sh", "quota-guard.sh"])
def test_the_wrapper_finds_the_package_it_was_installed_beside(tmp_path, wrapper):
    """No src/ and no .venv here: the checkout paths would both miss."""
    site, venv_python, script = _installed_tree(tmp_path, wrapper)
    marker = tmp_path / "ran"
    _fake_python(venv_python, marker, "venv")

    stray = tmp_path / "stray"
    stray.mkdir()
    _fake_python(stray / "python3", marker, "path")

    subprocess.run(
        ["bash", str(script)],
        input="{}",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={"PATH": f"{stray}:/bin:/usr/bin", "TMPDIR": str(tmp_path), "CCQUOTA_STOP": "95"},
    )
    who, _, src = marker.read_text(encoding="utf-8").strip().partition(" ")
    assert who == "venv"
    assert Path(src).resolve() == site.resolve()


@pytest.mark.parametrize("wrapper", ["statusline-command_x.sh", "quota-guard.sh"])
def test_the_wrapper_still_prefers_the_checkout_venv(tmp_path, wrapper):
    """bin/ beside src/ is the layout every existing machine runs."""
    repo = tmp_path / "repo"
    (repo / "src" / "ccreport").mkdir(parents=True)
    (repo / "bin").mkdir()
    shutil.copy(ROOT / "bin" / wrapper, repo / "bin" / wrapper)
    venv_python = repo / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    marker = tmp_path / "ran"
    _fake_python(venv_python, marker, "venv")

    subprocess.run(
        ["bash", str(repo / "bin" / wrapper)],
        input="{}",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={"PATH": "/bin:/usr/bin", "TMPDIR": str(tmp_path), "CCQUOTA_STOP": "95"},
    )
    who, _, src = marker.read_text(encoding="utf-8").strip().partition(" ")
    assert who == "venv"
    assert Path(src).resolve() == (repo / "src").resolve()
