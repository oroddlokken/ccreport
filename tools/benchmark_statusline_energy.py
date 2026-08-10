#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Reproducible energy-impact benchmark for src/ccreport/statusline.py.

Three phases:
  1. Per-invocation cost — wall time, CPU seconds, peak RSS (process tree
     via os.wait4) for a full render.
  2. Direct power — macmon (as meter only; the statusline has no macmon
     segment) idle baseline vs back-to-back render loop
     (cpu_power / sys_power averages).
  3. Derived energy — J/render estimate and Wh per hour at various render
     rates, expressed as % of battery.

Run on an otherwise quiescent machine; the idle baseline is ambient.
The detached ccreport.usage_api fetch (start_new_session) escapes the
process tree and is NOT counted in phase 1 — its cost is occasional
(10 min cache TTL) and network-bound.

Usage: ./benchmark_statusline_energy.py [--runs N] [--samples N]
       [--battery-wh F] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATUSLINE = HERE / "statusline_command.py"

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
