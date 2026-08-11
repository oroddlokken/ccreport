"""Claude Code Usage — terminal dashboard similar to the /usage screen.

Runs ccreport.usage_api for the numbers and draws a bar, a reset countdown and
a weekly pace line for each quota it reports.

Was a zsh script that piped the fetch through jq. The fetch is still a
subprocess: usage_api.main() coordinates a file lock, a cache TTL and a
fetch-failure backoff on behalf of every reader, and it reports by printing and
exiting. Calling it in-process would mean catching SystemExit and capturing
stdout; the process boundary is the honest one, and it is the seam the tests
stub.

AUDIT: All calculations are documented in docs/calculation-reference.md.
When changing any calculation, caching, or data format here,
update that document to match.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ccreport.pricing import WEEK_WINDOW_S, pace_days, window_start_epoch

BAR_WIDTH = 50

GREEN = "\033[0;32m"
DIM = "\033[0;90m"
BOLD = "\033[1m"
BOLD_GREEN = "\033[1;32m"
RESET = "\033[0m"

# Pace colours, widest band last: the first threshold a delta clears wins.
_PACE_BANDS = (
    (15, "0;31"),   # red — overcooking
    (5, "0;33"),    # yellow — warm
    (-5, "0;32"),   # green — on pace
    (-15, "0;36"),  # cyan — cool
)
_PACE_UNDER = "0;90"  # dim — underusing

USAGE = "Usage: ccu [--force|-f] [--json]"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _usage_argv(*args: str) -> list[str]:
    # -m, not a path: run as a script the module would be __main__ with the
    # package unimported, and its own `from ccreport.cache_db import ...`
    # would fail.
    return [sys.executable, "-m", "ccreport.usage_api", *args]


def _usage_env() -> dict[str, str]:
    """Environment that lets the child resolve the package off its own sys.path.

    An installed ccreport is on the child's path already; a checkout run
    straight from src/ is not. Prepending this package's resolved parent covers
    both without a second way to find it.
    """
    env = dict(os.environ)
    pkg_root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_root}{os.pathsep}{existing}" if existing else pkg_root
    return env


def fetch_usage(*, force: bool) -> dict[str, Any] | None:
    """Run usage_api and parse what it printed. None when it produced nothing.

    stderr is dropped rather than shown: usage_api warns there about a cost
    computation this dashboard never displays, and a warning above the bars
    reads as a failure of the thing that did work.
    """
    argv = _usage_argv(*(["--force"] if force else []))
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, env=_usage_env(),
        ).stdout
    except OSError:
        return None
    if not out.strip():
        return None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def emit_raw() -> int:
    """Passthrough of the API body — no cache, no rendering. Returns exit status."""
    return subprocess.run(_usage_argv("--raw"), env=_usage_env()).returncode


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def tz_name() -> str:
    """The zone's own name ("Europe/Oslo"), not the abbreviation it is in today.

    /etc/localtime is a symlink into the zoneinfo tree on macOS and most Linux,
    and its tail is the only place the full name survives — a datetime carries
    the offset, and time.tzname carries "CEST", which does not say which zone.
    """
    try:
        target = str(Path("/etc/localtime").readlink())
    except OSError:
        target = ""
    _, sep, name = target.partition("zoneinfo/")
    if sep and name:
        return name
    return time.tzname[time.daylight and time.localtime().tm_isdst > 0]


def iso_to_epoch(iso: str) -> float | None:
    """Parse an ISO 8601 timestamp to epoch seconds. None when unparseable."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def countdown(epoch: float, now: float) -> str:
    """Time to *epoch* in words: "2 days and 3 hours", "42 minutes".

    Empty for a moment already past — the caller drops the "in ..." clause
    rather than printing a negative one. Coarsest two units only: a reset four
    days out does not need its minutes.
    """
    delta = int(epoch - now)
    if delta <= 0:
        return ""
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    def plural(n: int, unit: str) -> str:
        return f"{n} {unit}" if n == 1 else f"{n} {unit}s"

    if days > 0:
        return f"{plural(days, 'day')} and {plural(hours, 'hour')}" if hours else plural(days, "day")
    if hours > 0:
        return (
            f"{plural(hours, 'hour')} and {plural(minutes, 'minute')}"
            if minutes
            else plural(hours, "hour")
        )
    return plural(minutes, "minute")


def _clock(when: datetime) -> str:
    """12-hour time, minutes only when they are not zero: "5pm", "5:59pm"."""
    hour = when.hour % 12 or 12
    ampm = "am" if when.hour < 12 else "pm"
    return f"{hour}{ampm}" if when.minute == 0 else f"{hour}:{when.minute:02d}{ampm}"


def _month_day(when: datetime) -> str:
    return f"{when.strftime('%b')} {when.day}"


def reset_line(reset_iso: str, now: float, zone: str) -> str:
    """"Resets in 3 hours and 14 minutes at 5:59pm (Europe/Oslo)".

    A reset at exactly midnight is a date the API gave no time for, so the
    clock is dropped rather than printed as "12am" — which would claim a
    precision the response did not carry. A day further out than tomorrow gets
    its date alongside the time, since "at 5pm" alone would read as today's.
    """
    epoch = iso_to_epoch(reset_iso)
    if epoch is None:
        return ""
    when = datetime.fromtimestamp(epoch)  # noqa: DTZ006 — local zone is the display zone
    left = countdown(epoch, now)
    month_day = _month_day(when)

    if (when.hour, when.minute) == (0, 0):
        where = f"on {month_day}" if left else month_day
    else:
        today = _month_day(datetime.fromtimestamp(now))  # noqa: DTZ006
        tomorrow = _month_day(datetime.fromtimestamp(now + 86400))  # noqa: DTZ006
        at = f"at {_clock(when)}"
        where = at if month_day in (today, tomorrow) else f"{at} on {month_day}"

    return f"Resets in {left} {where} ({zone})" if left else f"Resets {where} ({zone})"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def bar(pct: float) -> str:
    """A 50-cell meter, green for the filled part and dark grey for the rest."""
    filled = max(0, min(BAR_WIDTH, int(pct) * BAR_WIDTH // 100))
    return f"{GREEN}{'█' * filled}{DIM}{'░' * (BAR_WIDTH - filled)}{RESET}"


def pace_line(actual: float, reset_iso: str, now: float) -> str:
    """How the week's usage compares with the clock: "6d 5h into 7-day window ...".

    Expected is the fraction of CLAUDE_CODE_PACE_DAYS elapsed, not of the seven
    the window actually runs: a pace of 5 means the whole quota is meant to be
    gone by Friday, so the bar it is measured against rises faster than time.
    """
    week_start = window_start_epoch(reset_iso, WEEK_WINDOW_S, now)
    if week_start is None:
        return ""
    elapsed = now - week_start
    if elapsed <= 0 or elapsed > WEEK_WINDOW_S:
        return ""
    pace = pace_days()
    expected = min(int(elapsed * 100 // (pace * 86400)), 100)
    delta = int(actual) - expected

    el_d, rem = divmod(int(elapsed), 86400)
    el_h = rem // 3600
    if el_d and el_h:
        elapsed_str = f"{el_d}d {el_h}h"
    elif el_d:
        elapsed_str = f"{el_d}d"
    else:
        elapsed_str = f"{el_h}h"

    colour = next((c for threshold, c in _PACE_BANDS if delta > threshold), _PACE_UNDER)
    sign = "+" if delta >= 0 else ""
    return (
        f"{DIM}{elapsed_str} into 7-day window (pace: {pace}d) — "
        f"{expected}% expected, \033[{colour}m{sign}{delta}%{RESET}"
    )


def section(
    title: str, pct: float, reset_iso: str, now: float, zone: str, extra: str = "",
) -> list[str]:
    """Title, meter, percentage, an optional detail line, and the reset."""
    lines = [f"{BOLD_GREEN}{title}{RESET}", f"{bar(pct)}  {BOLD}{int(pct)}% used{RESET}"]
    if extra:
        lines.append(extra)
    reset = reset_line(reset_iso, now, zone)
    if reset:
        lines.append(reset)
    return lines


def _last_fetched(last_updated: str, now: float) -> str:
    epoch = iso_to_epoch(last_updated)
    if epoch is None:
        return ""
    minutes = int(now - epoch) // 60
    if minutes <= 0:
        return f"{DIM}Last fetched just now{RESET}"
    if minutes == 1:
        return f"{DIM}Last fetched 1 minute ago{RESET}"
    return f"{DIM}Last fetched {minutes} minutes ago{RESET}"


def _num(data: dict[str, Any], key: str) -> float | None:
    """The value at *key* as a number, or None when absent, null or unparseable."""
    raw = data.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _str(data: dict[str, Any], key: str) -> str:
    raw = data.get(key)
    return "" if raw is None else str(raw)


def render(data: dict[str, Any], now: float, zone: str) -> list[str]:
    """Every line of the dashboard, in order, without trailing newlines."""
    lines: list[str] = []
    header = _last_fetched(_str(data, "last_updated"), now)
    if header:
        lines.append(header)
    lines.append("")

    lines += section(
        "Current session", _num(data, "session_percent") or 0.0,
        _str(data, "session_reset"), now, zone,
    )

    week = _num(data, "week_percent")
    if week is not None:
        week_reset = _str(data, "week_reset")
        lines.append("")
        lines += section("Current week (all models)", week, week_reset, now, zone)
        pace = pace_line(week, week_reset, now)
        if pace:
            lines.append(pace)

    sonnet = _num(data, "sonnet_percent")
    if sonnet is not None:
        lines.append("")
        lines += section(
            "Current week (Sonnet only)", sonnet, _str(data, "sonnet_reset"), now, zone,
        )

    scoped = _num(data, "scoped_percent")
    if scoped is not None:
        lines.append("")
        lines += section(
            f"Current week ({_str(data, 'scoped_model') or 'model'} only)",
            scoped, _str(data, "scoped_reset"), now, zone,
        )

    extra_pct = _num(data, "extra_percent")
    if extra_pct is not None:
        spent, limit = _num(data, "extra_spent"), _num(data, "extra_limit")
        detail = f"${spent:.2f} / ${limit:.2f} spent" if spent is not None and limit is not None else ""
        lines.append("")
        # No reset: the API's extra_usage object carries no resets_at, and its
        # daily and weekly members are null even on an account with Extra
        # enabled, so there is nothing to count down to.
        lines += section("Extra usage", extra_pct, "", now, zone, detail)

    lines.append("")
    return lines


def _parse_args(argv: list[str]) -> tuple[bool, bool]:
    """Returns (force, json_only). Exits 2 on anything else."""
    force = json_only = False
    for arg in argv:
        if arg in ("--force", "-f"):
            force = True
        elif arg == "--json":
            json_only = True
        else:
            print(USAGE, file=sys.stderr)
            sys.exit(2)
    return force, json_only


def main(argv: list[str] | None = None) -> int:
    force, json_only = _parse_args(sys.argv[1:] if argv is None else argv)

    if json_only:
        return emit_raw()

    data = fetch_usage(force=force)
    if data is None:
        print("Failed to fetch usage data", file=sys.stderr)
        return 1
    # Session and week are the two every plan has. Neither present means the
    # response was shaped like usage data without being any.
    if _num(data, "session_percent") is None and _num(data, "week_percent") is None:
        print("No usage data available", file=sys.stderr)
        return 1

    print("\n".join(render(data, time.time(), tz_name())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
