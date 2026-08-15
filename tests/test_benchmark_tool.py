"""The benchmark's target path is a string with nothing type-checking it.

It pointed at tools/statusline_command.py for as long as it took someone to run
`just bench` again — the file had moved to src/ and been renamed, and main()'s
exists() guard turned that into a quiet `return 1`. These assert the target is
there and runnable the way the benchmark invokes it, `[str(STATUSLINE), "-t"]`.

Phase 4 is here too: it counts and prices processes the benchmark's own os.wait4
cannot see, and what it counts depends on a private HOME leaving all three spawn
gates cold.
"""

from __future__ import annotations

import importlib.util
import os
import resource
import shutil
import subprocess
import sys
import tomllib
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


class TestProcessLabels:
    def test_a_module_spawn_is_named_by_its_module(self, bench):
        cmd = "/repo/.venv/bin/python -m ccreport.usage_api --session mock --cwd /repo"
        assert bench.label_of(cmd) == "ccreport.usage_api"

    def test_a_plain_program_is_named_by_its_basename(self, bench):
        assert bench.label_of("/usr/bin/git remote get-url origin") == "git"

    def test_an_accounting_only_process_keeps_its_name(self, bench):
        """ps parenthesizes a command it could only read back from the kernel."""
        assert bench.label_of("(security)") == "security"

    def test_a_process_that_ended_before_ps_read_it_is_marked(self, bench):
        assert bench.label_of("") == bench.UNNAMED

    def test_repeats_fold_into_a_count(self, bench):
        assert bench.tally(["security", "git", "security"]) == "security x2, git"

    def test_nothing_counted_says_so(self, bench):
        assert bench.tally([]) == "none"


class TestCpuTime:
    def test_minutes_and_hundredths(self, bench):
        assert bench.parse_cpu_time("0:00.03") == pytest.approx(0.03)
        assert bench.parse_cpu_time("76:08.18") == pytest.approx(4568.18)

    def test_an_hours_field_is_the_leading_one(self, bench):
        assert bench.parse_cpu_time("2:01:30.00") == pytest.approx(7290.0)

    def test_a_field_ps_did_not_print_reads_as_no_cpu(self, bench):
        assert bench.parse_cpu_time("") == 0.0
        assert bench.parse_cpu_time("-") == 0.0


class TestGroupCosts:
    def test_each_field_takes_the_highest_run(self, bench):
        """A sample misses CPU that was spent, so the larger reading is nearer."""
        folded = bench.fold_groups([
            {"ccreport.push": {"cpu_s": 0.04, "wall_s": 0.30, "procs": 1}},
            {"ccreport.push": {"cpu_s": 0.09, "wall_s": 0.11, "procs": 2}},
        ])
        assert folded["ccreport.push"] == {"cpu_s": 0.09, "wall_s": 0.30, "procs": 2}

    def test_a_group_only_one_run_saw_is_kept(self, bench):
        folded = bench.fold_groups([
            {}, {"ccreport.usage_api": {"cpu_s": 0.2, "wall_s": 1.0, "procs": 3}},
        ])
        assert set(folded) == {"ccreport.usage_api"}

    def test_the_total_is_every_group(self, bench):
        groups = {
            "a": {"cpu_s": 0.25, "wall_s": 1.0, "procs": 1},
            "b": {"cpu_s": 0.75, "wall_s": 1.0, "procs": 1},
        }
        assert bench.total_cpu(groups) == pytest.approx(1.0)

    def test_nothing_spawned_costs_nothing(self, bench):
        assert bench.total_cpu({}) == 0.0


class TestBenchHome:
    def test_it_hands_the_render_a_push_config_to_find(self, bench):
        """No push.toml and _spawn_push returns before it reads any gate."""
        home = Path(bench._bench_home())
        try:
            cfg = home / ".config" / "ccreport" / "push.toml"
            entries = tomllib.loads(cfg.read_text())["server"]
            assert all(e["token"] for e in entries.values())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_it_holds_no_cache_so_every_gate_is_cold(self, bench):
        home = Path(bench._bench_home())
        try:
            assert not (home / ".cache" / "ccreport" / "cache.db").exists()
        finally:
            shutil.rmtree(home, ignore_errors=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="the ps flags and the wrapper are macOS-only")
class TestDetachedChildren:
    def test_the_process_table_answers_for_this_process(self, bench):
        me = bench._proc_table()[os.getpid()]
        assert (me.ppid, me.pgid) == (os.getppid(), os.getpgid(0))
        assert me.state

    def test_the_table_carries_the_cpu_a_process_has_burned(self, bench):
        """The only reading of a detached child's CPU there is: the bench never
        reaped one, so os.wait4 rusage has nothing to say about it.
        """
        me = bench._proc_table()[os.getpid()]
        mine = resource.getrusage(resource.RUSAGE_SELF)
        assert me.cpu_s == pytest.approx(mine.ru_utime + mine.ru_stime, abs=0.5)

    def test_the_marker_is_scoped_to_one_run(self, bench):
        """A `just bench` running beside the suite is two watches, not one."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=dict(os.environ, **{bench.BENCH_RUN_ENV: "mine"}),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            assert bench._describe(proc.pid, "mine")[1]
            assert not bench._describe(proc.pid, "theirs")[1]
        finally:
            proc.kill()
            proc.wait()

    @pytest.mark.slow
    def test_a_cold_render_is_seen_spawning_all_three(self, bench):
        """The three spawns are interval-gated, so only a render against a cold
        HOME makes them at all — which is what this phase exists to arrange.
        """
        # Two renders and their union, not one run's list: ps samples, and a
        # spawn that lives and dies between two of them is missed by the run
        # that made it. The push is the one this happened to — it dies against
        # a closed port — and a third render costs the suite more than the
        # union of two leaves on the table.
        seen = bench.measure_detached(2)
        expected = {"ccreport.usage_api", "ccreport.update_check", "ccreport.push"}
        assert seen["failed_renders"] == 0
        assert expected <= set(seen["seen"])
        # Any other name is a fourth spawn and belongs in the report. UNNAMED
        # is not one: it is a root that exited before ps could print its
        # command, which is the same sampling miss one line up allows for.
        assert set(seen["seen"]) - expected <= {bench.UNNAMED}
        assert seen["peak_live"] >= 3
        # Every spawn one of the two renders was seen making is priced. The
        # figure is a floor — a process under 10 ms of CPU reads as 0.00 — so
        # only the total is asserted against zero.
        assert set(seen["groups"]) == set(seen["seen"])
        assert bench.total_cpu(seen["groups"]) > 0.0
