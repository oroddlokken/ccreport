"""The Dockerfile's CMD and docker-compose's command, which are a pair.

Editing one alone either publishes a reloading server or leaves local
development without the reloader.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _bracket_list(line: str) -> list[str]:
    """The JSON array a CMD or command line carries, in exec form."""
    match = re.search(r"\[.*\]", line)
    assert match, f"no exec-form list in {line!r}"
    return json.loads(match.group(0))


def image_command() -> list[str]:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    cmds = [line for line in lines if line.startswith("CMD ")]
    assert len(cmds) == 1, f"expected one CMD, found {len(cmds)}"
    return _bracket_list(cmds[0])


def compose_command() -> list[str]:
    """Parsed textually: PyYAML is not a dependency of this project."""
    lines = (ROOT / "docker-compose.yml").read_text().splitlines()
    commands = [line for line in lines if line.strip().startswith("command:")]
    assert len(commands) == 1, f"expected one command:, found {len(commands)}"
    return _bracket_list(commands[0])


class TestPublishedImage:
    """What `docker build` alone gives you."""

    def test_the_image_pins_one_worker(self):
        argv = image_command()
        assert "--workers" in argv
        assert argv[argv.index("--workers") + 1] == "1"

    def test_the_image_does_not_reload(self):
        assert "--reload" not in image_command()

    def test_the_image_runs_the_repo_entry_point(self):
        assert image_command()[:3] == ["python", "-m", "ccreport.server.fastapi_server"]


class TestComposeOverride:
    """What `docker compose up` runs instead."""

    def test_compose_reloads(self):
        assert "--reload" in compose_command()

    def test_compose_runs_the_repo_entry_point(self):
        assert compose_command()[:3] == ["python", "-m", "ccreport.server.fastapi_server"]


@pytest.mark.parametrize("argv", [image_command(), compose_command()])
def test_both_bind_every_interface(argv: list[str]):
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
