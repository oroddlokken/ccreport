"""The benchmark's target path is a string with nothing type-checking it.

It pointed at tools/statusline_command.py for as long as it took someone to run
`just bench` again — the file had moved to src/ and been renamed, and main()'s
exists() guard turned that into a quiet `return 1`. These assert the target is
there and runnable the way the benchmark invokes it, `[str(STATUSLINE), "-t"]`.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BENCH = REPO / "tools" / "benchmark_statusline_energy.py"


@pytest.fixture(scope="module")
def bench():
    """The benchmark loaded as a module; it lives outside any package."""
    spec = importlib.util.spec_from_file_location("benchmark_statusline_energy", BENCH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBenchmarkTarget:
    def test_the_target_exists(self, bench):
        assert bench.STATUSLINE.exists(), f"benchmark target is gone: {bench.STATUSLINE}"

    def test_the_target_is_executable(self, bench):
        assert os.access(bench.STATUSLINE, os.X_OK)

    def test_the_target_is_what_claude_code_invokes(self, bench):
        """A `python -m` target would measure a render without the wrapper's
        interpreter choice or its bytecode-cached import boot.
        """
        assert bench.STATUSLINE == REPO / "bin" / "statusline-command_x.sh"

    @pytest.mark.skipif(sys.platform != "darwin", reason="the wrapper is macOS-only")
    def test_the_target_renders_when_run_as_the_benchmark_runs_it(self, bench):
        out = subprocess.run(
            [str(bench.STATUSLINE), "-t"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0
        assert out.stdout.strip()
