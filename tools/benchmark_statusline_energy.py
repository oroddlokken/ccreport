#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Reproducible energy-impact benchmark for a status line render.

The target is bin/statusline-command_x.sh, the wrapper Claude Code invokes.

Four phases:
  1. Per-invocation cost — wall time, CPU seconds, peak RSS (process tree
     via os.wait4) for a full render.
  2. Direct power — macmon (as meter only; the statusline has no macmon
     segment) idle baseline vs back-to-back render loop
     (cpu_power / sys_power averages).
  3. Derived energy — J/render estimate and Wh per hour at various render
     rates, expressed as % of battery.
  4. Detached children — how many processes a render starts with
     start_new_session, and how many those start in turn.

Run on an otherwise quiescent machine; the idle baseline is ambient.
The three detached spawns (ccreport.usage_api, ccreport.update_check,
ccreport.push) escape the process tree, so os.wait4 never sees them and
phase 1 does not count them. Phase 4 counts them by watching the process
table instead; it runs last so its forced spawns cannot reach phase 2.

Usage: ./benchmark_statusline_energy.py [--runs N] [--samples N]
       [--battery-wh F] [--child-runs N] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The wrapper, not the module: it is what Claude Code's settings.json invokes,
# and its choice of interpreter and its `python -c` import boot (which is what
# gets the module's bytecode cached) are part of the per-render cost measured
# here. `python -m ccreport.statusline` would skip both.
STATUSLINE = HERE.parent / "bin" / "statusline-command_x.sh"

# Rough per-active-core power bracket for Apple Silicon (E-core .. P-core)
# used to convert CPU-seconds to joules when the direct measurement is
# below the noise floor.
CORE_W_LOW = 1.0
CORE_W_HIGH = 4.0
NOISE_FLOOR_W = 0.5  # power deltas below this are ambient noise

# Loop child: renders statusline back-to-back until killed.
LOOP_SRC = """
import subprocess, sys
while True:
    subprocess.run([sys.argv[1], "-t"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""


def measure_child(cmd: list[str], env: dict | None = None) -> dict:
    """Run cmd to completion; return wall s, CPU s, and peak RSS MB.

    os.wait4 rusage covers the child and every descendant it reaped,
    so statusline's git/pmset/ps subprocesses are included.
    """
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, cwd=HERE,
    )
    _, status, ru = os.wait4(proc.pid, 0)
    proc.returncode = os.waitstatus_to_exitcode(status)
    return {
        "wall_s": time.monotonic() - t0,
        "cpu_s": ru.ru_utime + ru.ru_stime,
        "maxrss_mb": ru.ru_maxrss / 1e6,  # bytes on macOS
    }


def median_of(cmd: list[str], runs: int, env: dict | None = None) -> dict:
    results = [measure_child(cmd, env) for _ in range(runs)]
    return {
        k: statistics.median(r[k] for r in results)
        for k in ("wall_s", "cpu_s", "maxrss_mb")
    }


# --- Phase 4: the children os.wait4 cannot see ---

BENCH_RUN_ENV = "CCREPORT_ENERGY_BENCH_RUN"
"""Marker put in the render's environment and inherited by what it spawns.

A detached child reparents to launchd the moment the render exits, so ppid
answers "whose child is this?" only while the render is still alive. The
render's _refresh_env copies its whole environment into each spawn, so this
token is in theirs and in no other process on the machine.
"""

CHILD_TIMEOUT_S = 25.0   # give up waiting for a run's detached children to exit
CHILD_SETTLE_S = 2.0     # after the render exits, wait this long for a late spawn

_ENV_TOKEN = re.compile(r" [A-Z_][A-Z0-9_]*=")


def _proc_table() -> dict[int, tuple[int, int, str]]:
    """pid -> (ppid, pgid, state) for every process, command column omitted.

    Formatting commands costs ps another ~6 ms per call, which is most of the
    lifetime of a grandchild like `git remote get-url` — poll with it and the
    grandchild is gone between two samples. Commands are read per pid instead,
    once, when a pid first appears.
    """
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid=,state="], capture_output=True, text=True,
    ).stdout
    table: dict[int, tuple[int, int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        table[pid] = (ppid, pgid, parts[3])
    return table


def _describe(pid: int, tag: str = "") -> tuple[str, bool]:
    """(command line, carries our marker) for one pid, ("", False) once it is gone.

    One pid per call: ps -p prints nothing at all when any pid in a list has
    already exited, and every pid here is a process that may exit mid-poll.

    *tag* narrows the marker to one run's spawns. Two runs overlap whenever the
    suite watches a render while `just bench` is doing the same, and each was
    otherwise free to adopt the other's children and report them twice.
    """
    proc = subprocess.run(
        ["ps", "-Ewww", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
    )
    raw = proc.stdout.strip()
    if not raw:
        return "", False
    # -E appends the environment to the command; cut at the first VAR= token so
    # the report shows the command and not a screenful of exported variables.
    cut = _ENV_TOKEN.search(raw)
    return (raw[: cut.start()] if cut else raw), f"{BENCH_RUN_ENV}={tag}" in raw


def _bench_home() -> str:
    """A private HOME with the three interval gates cold, and a push target.

    All three gates are state under the user's home: the usage row's age and
    push_next_at in ~/.cache/ccreport/cache.db, update_checked_at beside them,
    and ~/.config/ccreport/push.toml, whose absence stops the push before any
    gate is read. A HOME of its own resets all three for one render without
    writing anything into the cache the machine actually reports from. The
    server URL is a closed port: the push has to start to be counted, and
    nothing here wants it to arrive.
    """
    home = tempfile.mkdtemp(prefix="ccreport-bench-home-")
    cfg = Path(home, ".config", "ccreport")
    cfg.mkdir(parents=True)
    (cfg / "push.toml").write_text('[server."http://127.0.0.1:9"]\ntoken = "energy-bench"\n')
    return home


def _detached_run(tag: str) -> dict:
    """Watch one cold render and return what it detached.

    The render is started in a session of its own, so its reaped children carry
    its pid as their pgid and each detached spawn carries its own — that pgid is
    what sorts one group from the other, and it is what a detached child's own
    children inherit. Ownership of a group is settled by having been seen while
    its ppid was still the render, or, past that, by the marker in its
    environment.

    Every classification below is made from the process table alone, and ps is
    asked about a single pid only to name a process or to test a group sighted
    after the render had already exited. Each of those calls is ~7 ms the loop
    is not sampling in, which is most of the life of a process like
    `git remote get-url`.
    """
    home = _bench_home()
    env = dict(os.environ, HOME=home)
    env[BENCH_RUN_ENV] = tag
    baseline = set(_proc_table())
    proc = subprocess.Popen(
        [str(STATUSLINE), "-t"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, cwd=HERE, start_new_session=True,
    )
    roots: dict[int, str] = {}
    kin: dict[int, tuple[int, str]] = {}   # pid -> (pgid, name)
    tested: set[int] = set()               # leaders whose environment has been read once
    peak = 0
    exited_at = None
    deadline = time.monotonic() + CHILD_TIMEOUT_S
    while time.monotonic() < deadline:
        table = _proc_table()
        to_name: list[int] = []
        to_test: list[int] = []
        for pid in sorted(table):     # a group's leader has the lower pid of the two
            ppid, pgid, _ = table[pid]
            if pid in baseline or pid in kin or pid == proc.pid:
                continue
            if pgid == proc.pid:
                kin[pid] = (pgid, UNNAMED)     # the reaped tree: counted, never named
            elif pgid == pid and ppid == proc.pid:
                # Named here rather than in the batch below, which runs a ps
                # per pid after the whole table has been walked: the push is
                # the shortest-lived of the three spawns and was reaching that
                # batch already dead, landing in the report as "(exited)".
                cmd, _ = _describe(pid, tag)
                roots[pid] = label_of(cmd)
                kin[pid] = (pgid, roots[pid])
            elif pgid in roots:
                kin[pid] = (pgid, UNNAMED)
                to_name.append(pid)
            elif pgid == pid and pid not in tested:
                tested.add(pid)                # a leader met after the render exited
                to_test.append(pid)
        for pid in to_test:
            cmd, marked = _describe(pid, tag)
            if marked:
                roots[pid] = label_of(cmd)
                kin[pid] = (pid, label_of(cmd))
        for pid in to_name:
            cmd, _ = _describe(pid, tag)
            if cmd:
                kin[pid] = (kin[pid][0], label_of(cmd))
        # Concurrency, not headcount: what these processes compete for is cores,
        # and a zombie holds none. The render's own tree is in the figure because
        # the question is how many of its processes are ever runnable at once.
        live = sum(
            1 for pid, (_, pgid, state) in table.items()
            if pid in kin and not state.startswith("Z")
            and (pgid == proc.pid or pgid in roots)
        )
        peak = max(peak, live)
        if proc.poll() is not None:
            if exited_at is None:
                exited_at = time.monotonic()
            outstanding = any(
                pid in table and not table[pid][2].startswith("Z")
                for pid in kin if kin[pid][0] in roots or pid in roots
            )
            if not outstanding and time.monotonic() - exited_at >= CHILD_SETTLE_S:
                break
    proc.wait()
    survivors = [pid for pid in roots if pid in _proc_table()]
    shutil.rmtree(home, ignore_errors=True)
    return {
        "returncode": proc.returncode,
        "detached": sorted(roots.values()),
        # A grandchild can outlive its first sighting by less than the one ps
        # call it takes to name it. Its parent is still known, and "one more
        # process under update_check" is the part of it this phase is counting.
        "descendants": sorted(
            f"child of {roots[pgid]}" if name == UNNAMED else name
            for pid, (pgid, name) in kin.items() if pid not in roots and pgid in roots
        ),
        "peak_live": peak,
        "survivors": len(survivors),
    }


UNNAMED = "(exited)"
"""Stands in for the command of a process that ended before ps could print it."""


def label_of(cmd: str) -> str:
    """A command as one word: the module for `-m pkg.mod`, else the program name.

    Full argv here is a screen of absolute paths and a session id, and the
    report's question is which process, not with which arguments.
    """
    words = cmd.split()
    if not words:
        return UNNAMED
    if "-m" in words[1:]:
        return words[words.index("-m", 1) + 1]
    return Path(words[0].strip("()")).name or words[0]


def tally(names: list[str]) -> str:
    """The names, repeats folded into a count: "security x2, git"."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(n if c == 1 else f"{n} x{c}" for n, c in ordered) or "none"


def measure_detached(runs: int) -> dict:
    """The widest fan-out seen over *runs* cold renders.

    Sampling ps can only miss a process, never invent one, so the run that saw
    the most is the run that missed the least — a median would report a dropped
    sample as the truth. Each run gets its own HOME: a second render against the
    first one's cache is a render with all three gates shut.
    """
    observed = [_detached_run(f"phase4-{i}") for i in range(runs)]
    widest = max(observed, key=lambda r: (len(r["detached"]), len(r["descendants"])))
    return {
        "runs": runs,
        "detached": widest["detached"],
        "descendants": widest["descendants"],
        # Which spawns exist, as against how many one render was seen making.
        # A miss is the only error a sampler makes, so a name any run saw is a
        # name every run made — the counts above stay one render's.
        "seen": sorted({name for r in observed for name in r["detached"]}),
        "peak_live": max(r["peak_live"] for r in observed),
        "failed_renders": sum(1 for r in observed if r["returncode"] != 0),
        "survivors": max(r["survivors"] for r in observed),
    }


def sample_power(samples: int) -> dict:
    """Average cpu_power / sys_power over N one-second macmon samples."""
    out = subprocess.run(
        ["macmon", "pipe", "-s", str(samples), "-i", "1000"],
        capture_output=True, text=True, timeout=samples * 2 + 30,
    )
    cpu, sys_p = [], []
    for line in out.stdout.splitlines():
        try:
            d = json.loads(line)
            cpu.append(d["cpu_power"])
            sys_p.append(d["sys_power"])
        except (json.JSONDecodeError, KeyError):
            continue
    if not cpu:
        raise RuntimeError("macmon produced no samples")
    return {"cpu_w": sum(cpu) / len(cpu), "sys_w": sum(sys_p) / len(sys_p)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=3,
                    help="timed runs per command (median reported, default 3)")
    ap.add_argument("--samples", type=int, default=8,
                    help="1s power samples per phase-2 condition (default 8)")
    ap.add_argument("--battery-wh", type=float, default=50.0,
                    help="battery capacity for context (default 50 Wh)")
    ap.add_argument("--child-runs", type=int, default=3,
                    help="cold renders watched for detached children (default 3, 0 skips)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit machine-readable JSON instead of the report")
    args = ap.parse_args()

    if not STATUSLINE.exists():
        print(f"missing {STATUSLINE}", file=sys.stderr)
        return 1
    have_macmon = shutil.which("macmon") is not None
    if not have_macmon:
        print("macmon not found — skipping the power phase", file=sys.stderr)

    log = (lambda *_a: None) if args.as_json else print

    # Warmup: populate uv env cache, page caches, statusline SQLite cache
    log("warmup render ...")
    measure_child([str(STATUSLINE), "-t"])

    log(f"phase 1: per-invocation cost (median of {args.runs}) ...")
    full = median_of([str(STATUSLINE), "-t"], args.runs)

    idle = load = None
    renders_done = 0
    load_duration = 0.0
    if have_macmon:
        log(f"phase 2: idle power baseline ({args.samples}s) ...")
        idle = sample_power(args.samples)

        log(f"phase 2: power under back-to-back rendering ({args.samples}s) ...")
        loop = subprocess.Popen(
            [sys.executable, "-c", LOOP_SRC, str(STATUSLINE)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=HERE, start_new_session=True,
        )
        t0 = time.monotonic()
        try:
            load = sample_power(args.samples)
        finally:
            load_duration = time.monotonic() - t0
            os.killpg(loop.pid, signal.SIGKILL)
            loop.wait()
        renders_done = int(load_duration / full["wall_s"])

    # After phase 2: this phase resets the gates that make a render spawn, and
    # three extra interpreters landing in the power window would be charged to
    # the render loop.
    detached = None
    if args.child_runs > 0:
        log(f"phase 4: detached children ({args.child_runs} cold renders) ...")
        detached = measure_detached(args.child_runs)

    cpu_s = full["cpu_s"]
    j_low, j_high = cpu_s * CORE_W_LOW, cpu_s * CORE_W_HIGH
    measured = None
    if idle and load:
        delta_cpu = load["cpu_w"] - idle["cpu_w"]
        delta_sys = load["sys_w"] - idle["sys_w"]
        measured = {"delta_cpu_w": delta_cpu, "delta_sys_w": delta_sys}
        if delta_sys >= NOISE_FLOOR_W and renders_done:
            renders_per_s = renders_done / load_duration
            measured["j_per_render"] = delta_sys / renders_per_s

    j_mid = measured.get("j_per_render", (j_low + j_high) / 2) if measured else (j_low + j_high) / 2
    rates = [100, 500, int(3600 / full["wall_s"])]  # renders/hour scenarios
    scenarios = [
        {"renders_per_hour": r,
         "wh_per_hour": j_mid * r / 3600,
         "battery_pct_per_hour": j_mid * r / 3600 / args.battery_wh * 100}
        for r in rates
    ]

    result = {
        "full_render": full,
        "idle_power": idle,
        "load_power": load,
        "measured_delta": measured,
        "joules_per_render_est": {"low": j_low, "mid": j_mid, "high": j_high},
        "scenarios": scenarios,
        "battery_wh": args.battery_wh,
        "detached_children": detached,
    }

    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0

    def row(label: str, m: dict | None) -> str:
        if m is None:
            return f"  {label:<22} (skipped)"
        return (f"  {label:<22} wall {m['wall_s']:6.2f}s   "
                f"cpu {m['cpu_s']:5.2f}s   rss {m['maxrss_mb']:5.1f}MB")

    print()
    print("=== statusline energy benchmark ===")
    print()
    print("per-invocation cost (median):")
    print(row("full render", full))
    print()
    if detached:
        names = tally(detached["detached"])
        kin = tally(detached["descendants"])
        print(f"detached children (outside the figures above, {detached['runs']} cold renders):")
        print(f"  {'spawned by a render':<22} {len(detached['detached']):>2}   {names}")
        print(f"  {'spawned by those':<22} {len(detached['descendants']):>2}   {kin}")
        print(f"  {'peak live at once':<22} {detached['peak_live']:>2}   "
              "(sampled, the reaped tree included)")
        if detached["failed_renders"]:
            print(f"  {detached['failed_renders']} of these renders exited non-zero — "
                  "the counts below them are not a render's")
        if detached["survivors"]:
            print(f"  {detached['survivors']} outlived the {CHILD_TIMEOUT_S:.0f}s watch "
                  "and were left running")
        print()
    if idle and load:
        print("direct power (macmon, ambient included):")
        print(f"  idle:  cpu {idle['cpu_w']:.2f}W   sys {idle['sys_w']:.2f}W")
        print(f"  load:  cpu {load['cpu_w']:.2f}W   sys {load['sys_w']:.2f}W"
              f"   ({renders_done} renders in {load_duration:.0f}s)")
        if "j_per_render" in (measured or {}):
            print(f"  measured: {measured['j_per_render']:.2f} J/render")
        else:
            print(f"  delta below noise floor (<{NOISE_FLOOR_W}W) — "
                  "using CPU-time estimate")
        print()
    print(f"energy per render: ~{j_low:.1f}–{j_high:.1f} J "
          f"(cpu {cpu_s:.2f}s x {CORE_W_LOW:.0f}–{CORE_W_HIGH:.0f}W/core), "
          f"using {j_mid:.1f} J below")
    print()
    print(f"hourly scenarios ({args.battery_wh:.0f}Wh battery):")
    for s in scenarios:
        print(f"  {s['renders_per_hour']:>5} renders/h -> "
              f"{s['wh_per_hour']:.2f} Wh/h "
              f"({s['battery_pct_per_hour']:.2f}% battery/h)")
    print()
    print("note: last scenario is the physical max (back-to-back renders).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
