"""The quota verdict the UserPromptSubmit and PreToolUse hooks share.

Hook input carries no quota data, so every reading here comes from what a
status line render already stored. Stdlib only and no rich, for the reason the
status line has none: PreToolUse runs before every tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

WARN_ENV = "CCQUOTA_WARN"
STOP_ENV = "CCQUOTA_STOP"

# A slow render writes the stdin reading, so an active session's file is
# seconds old. The budget covers a render that failed, not a session nobody
# has rendered.
NATIVE_MAX_AGE_S = 900

# statusline.USAGE_HEARTBEAT_S is the ceiling on the usage row's age: with S/W
# arriving natively, a render calls the API for Sonnet, scoped and Extra only
# when one of them can change the line, and hourly otherwise.
API_MAX_AGE_S = 3600

# Layout guard on the file statusline._save_quota_reading writes. A reading
# rebuilt from a shape that has moved is worse than no reading.
QUOTA_FILE_SCHEMA = 1

WINDOW_LABELS = {
    "session": "5-hour",
    "week": "weekly",
    "sonnet": "weekly Sonnet",
    "scoped": "weekly scoped",
}

# Which budget a window's absence is measured against, for the message alone:
# S and W have a native source, Sonnet and scoped are the API's to supply.
_DEFAULT_BUDGET = {
    "session": NATIVE_MAX_AGE_S,
    "week": NATIVE_MAX_AGE_S,
    "sonnet": API_MAX_AGE_S,
    "scoped": API_MAX_AGE_S,
}

# (window, percent column, reset column) in the singleton usage row.
_USAGE_QUOTA_COLS = (
    ("session", "session_percent", "session_reset"),
    ("week", "week_percent", "week_reset"),
    ("sonnet", "sonnet_percent", "sonnet_reset"),
    ("scoped", "scoped_percent", "scoped_reset"),
)

# Long enough to outwait a render's write, short enough that a wedged database
# costs the turn a block rather than a hang.
DB_TIMEOUT_S = 2.0


class Reading(NamedTuple):
    """One window's utilization as one source last saw it."""

    percent: float
    resets_at: float
    age_s: float
    budget_s: float


class WindowState(NamedTuple):
    """What the guard knows about one window right now.

    A percent of None is unknown, which blocks; a window the plan does not have
    never reaches here at all.
    """

    window: str
    percent: float | None
    resets_at: float | None
    budget_s: float


class Verdict(NamedTuple):
    """One answer: kind is ok, warn or stop.

    key is the resets_at a warning dedupes on, and None where there is nothing
    to dedupe against.
    """

    kind: str
    message: str
    window: str
    key: float | None


# ---------------------------------------------------------------------------
# Where the readings sit
# ---------------------------------------------------------------------------


def _session_state_path(session_id: str, suffix: str = "") -> Path:
    """Per-session scratch file in TMPDIR, as the status line builds it.

    Rebuilt rather than imported: importing that module for a path costs every
    tool call 2600 lines of tokenizing. tests/test_quota_guard.py asserts the
    two agree.
    """
    sid = re.sub(r"[^A-Za-z0-9-]", "_", session_id)[:64]
    tmp = os.environ.get("TMPDIR") or "/tmp"  # noqa: S108 — uid-suffixed filename below
    return Path(tmp) / f"claude-statusline-{os.getuid()}-{sid}{suffix}.json"


def quota_reading_path(session_id: str) -> Path:
    """The stdin S/W reading a render stores while this session is armed."""
    return _session_state_path(session_id, ".quota")


def warn_memo_path(session_id: str) -> Path:
    """Which window instances this session has already warned about."""
    return _session_state_path(session_id, ".quota-warn")


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


def _parse_iso_epoch(iso: str) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, OSError):
        return None


def _reading(pct: Any, resets_at: Any, ts: Any, now: float, budget_s: float) -> Reading | None:
    """A candidate, or None when any of the three values is unusable.

    Negative age is clamped to zero: a file written by a render a fraction of a
    second ahead of this clock is fresh, not from the future.
    """
    if pct is None or resets_at is None or ts is None:
        return None
    try:
        return Reading(float(pct), float(resets_at), max(0.0, now - float(ts)), budget_s)
    except (TypeError, ValueError):
        return None


def _file_readings(session_id: str, now: float) -> dict[str, list[Reading]]:
    """The stdin S/W reading of the last slow render, keyed by window.

    Empty when this session has no file yet — Claude Code sends no rate_limits
    until the session's first API response, and a render before that writes the
    stamp with no windows under it.
    """
    if not session_id:
        return {}
    try:
        with open(quota_reading_path(session_id), encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != QUOTA_FILE_SCHEMA:
        return {}
    ts = payload.get("ts")
    out: dict[str, list[Reading]] = {}
    for window, w in (payload.get("windows") or {}).items():
        if not isinstance(w, dict):
            continue
        reading = _reading(w.get("percent"), w.get("resets_at"), ts, now, NATIVE_MAX_AGE_S)
        if reading:
            out[window] = [reading]
    return out


def _db_readings(now: float) -> tuple[dict[str, list[Reading]], dict[str, bool]]:
    """Every candidate the cache holds, and which quotas the plan has.

    Its own read-only connection rather than cache_db.get_connection: that one
    runs the schema bootstrap and can write the daily snapshot, neither of
    which belongs on a per-tool-call path.
    """
    import sqlite3

    from ccreport import cache_db

    path = cache_db.DB_PATH
    if not path.exists():
        return {}, {}
    out: dict[str, list[Reading]] = {}
    applicable: dict[str, bool] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=DB_TIMEOUT_S)
    except sqlite3.Error:
        return {}, {}
    try:
        _usage_readings(conn, now, out, applicable)
        _snapshot_readings(conn, now, out)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out, applicable


def _usage_readings(
    conn: Any, now: float, out: dict[str, list[Reading]], applicable: dict[str, bool],
) -> None:
    """Fold the singleton usage row into *out* and *applicable*.

    A null column in a row within API_MAX_AGE_S is the API saying the plan has
    no such quota — the one absence that is not unknown.
    """
    cols = ["last_updated"]
    for _, pct_col, reset_col in _USAGE_QUOTA_COLS:
        cols += [pct_col, reset_col]
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM usage WHERE id = 1",  # noqa: S608
    ).fetchone()
    if row is None:
        return
    updated = _parse_iso_epoch(str(row[0] or ""))
    if updated is None:
        return
    fresh = now - updated <= API_MAX_AGE_S
    for i, (window, _, _) in enumerate(_USAGE_QUOTA_COLS):
        pct, reset_iso = row[1 + i * 2], row[2 + i * 2]
        if fresh:
            applicable[window] = pct is not None
        reading = _reading(pct, _parse_iso_epoch(str(reset_iso or "")), updated, now, API_MAX_AGE_S)
        if reading:
            out.setdefault(window, []).append(reading)


def _snapshot_readings(conn: Any, now: float, out: dict[str, list[Reading]]) -> None:
    """Fold each window's newest rate_limit_snapshots row into *out*.

    A row lands only when the reading moved a whole percent, so its ts dates
    the last change and not the last observation — which is why the file above
    exists and this is the fallback.
    """
    # used_pct, resets_at and source are bare columns beside MAX(ts), which
    # SQLite takes from the row that supplied the maximum — the guarantee that
    # lets one grouped query answer what four ORDER BY ts DESC LIMIT 1 did.
    rows = conn.execute(
        "SELECT window, used_pct, resets_at, MAX(ts), source FROM rate_limit_snapshots "  # noqa: S608
        f"WHERE window IN ({', '.join('?' * len(WINDOW_LABELS))}) GROUP BY window",
        tuple(WINDOW_LABELS),
    ).fetchall()
    for window, used_pct, resets_at, ts, source in rows:
        budget = NATIVE_MAX_AGE_S if source == "stdin" else API_MAX_AGE_S
        reading = _reading(used_pct, resets_at, ts, now, budget)
        if reading:
            out.setdefault(window, []).append(reading)


def read_windows(session_id: str, now: float) -> list[WindowState]:
    """Every window the plan has, each with its percent or None for unknown.

    Windows are merged highest-percent-first among the readings still inside
    their own budget, so a disagreement between two sources resolves the way
    the guard fails.
    """
    candidates = _file_readings(session_id, now)
    db_candidates, applicable = _db_readings(now)
    for window, readings in db_candidates.items():
        candidates.setdefault(window, []).extend(readings)

    states = []
    for window in WINDOW_LABELS:
        fresh = [r for r in candidates.get(window, []) if r.age_s <= r.budget_s]
        # The API's null loses to a fresh reading: S and W arrive on stdin and
        # the row's copy of them is whatever the last full fetch left behind.
        if not fresh and applicable.get(window) is False:
            continue
        # The most generous budget that still found nothing is what the
        # unknown message has to name.
        budget = max(
            (r.budget_s for r in candidates.get(window, [])), default=_DEFAULT_BUDGET[window],
        )
        if not fresh:
            if window in candidates or applicable.get(window):
                states.append(WindowState(window, None, None, budget))
            continue
        # A reset already passed means the window rolled and utilization
        # restarted, which is a reading of zero rather than a missing one.
        percent = max(0.0 if r.resets_at <= now else r.percent for r in fresh)
        states.append(WindowState(window, percent, max(r.resets_at for r in fresh), budget))
    return states


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def threshold(raw: str | None) -> float | None:
    """A percentage from the environment, or None when it is unset or unusable."""
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip().rstrip("%"))
    except ValueError:
        return None


def _way_out() -> str:
    return f"Unset {STOP_ENV} and relaunch claude to continue."


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)} minutes"


def verdict(states: list[WindowState], warn: float | None, stop: float) -> Verdict:
    """Whether this prompt or tool call may proceed.

    An over-stop reading outranks an unknown window, because naming the quota
    that is actually spent tells the person more than naming a stale one.
    """
    if not states:
        return Verdict(
            "stop",
            f"Quota guard: no quota reading at all on this machine. {_way_out()}",
            "",
            None,
        )
    known = [s for s in states if s.percent is not None]
    over = [s for s in known if s.percent is not None and s.percent >= stop]
    if over:
        worst = max(over, key=lambda s: s.percent or 0.0)
        return Verdict(
            "stop",
            f"Quota guard: the {WINDOW_LABELS[worst.window]} window is at "
            f"{worst.percent:.0f}%, at or over the {STOP_ENV} line of {stop:g}. {_way_out()}",
            worst.window,
            worst.resets_at,
        )
    unknown = [s for s in states if s.percent is None]
    if unknown:
        s = unknown[0]
        return Verdict(
            "stop",
            f"Quota guard: no {WINDOW_LABELS[s.window]} reading in the last "
            f"{_minutes(s.budget_s)}. {_way_out()}",
            s.window,
            None,
        )
    if warn is None:
        return Verdict("ok", "", "", None)
    warned = [s for s in known if s.percent is not None and s.percent >= warn]
    if warned:
        worst = max(warned, key=lambda s: s.percent or 0.0)
        return Verdict(
            "warn",
            f"Quota guard: the {WINDOW_LABELS[worst.window]} window is at "
            f"{worst.percent:.0f}%, past the {WARN_ENV} line of {warn:g}. "
            f"The session stops at {stop:g}.",
            worst.window,
            worst.resets_at,
        )
    return Verdict("ok", "", "", None)


# ---------------------------------------------------------------------------
# Warning dedupe
# ---------------------------------------------------------------------------


def _load_warned(session_id: str) -> dict[str, float]:
    if not session_id:
        return {}
    try:
        with open(warn_memo_path(session_id), encoding="utf-8") as fh:
            memo = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(memo, dict):
        return {}
    return {k: float(v) for k, v in memo.items() if isinstance(v, (int, float))}


def _save_warned(session_id: str, warned: dict[str, float]) -> None:
    if not session_id:
        return
    path = warn_memo_path(session_id)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(warned), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def warning_is_new(session_id: str, v: Verdict) -> bool:
    """Whether this warning is the first for its window instance.

    Keyed on resets_at, so the next window earns its own warning and the rest
    of this one stays quiet.
    """
    if v.key is None:
        return True
    warned = _load_warned(session_id)
    if warned.get(v.window) == v.key:
        return False
    warned[v.window] = v.key
    _save_warned(session_id, warned)
    return True


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


def hook_output(payload: dict, now: float, env: dict[str, str]) -> dict | None:
    """What the hook prints, or None when the session may carry on.

    continue: false is the strongest stop a hook has — it halts this turn and
    leaves the session open for input. stopReason reaches the person, never
    Claude.
    """
    raw_stop = env.get(STOP_ENV, "")
    if not raw_stop.strip():
        return None
    stop = threshold(raw_stop)
    if stop is None:
        return {
            "continue": False,
            "stopReason": f"Quota guard: {STOP_ENV}={raw_stop!r} is not a percentage. "
            f"{_way_out()}",
        }
    session_id = str(payload.get("session_id") or "")
    v = verdict(read_windows(session_id, now), threshold(env.get(WARN_ENV, "")), stop)
    if v.kind == "stop":
        return {"continue": False, "stopReason": v.message}
    if v.kind == "warn" and warning_is_new(session_id, v):
        return {"systemMessage": v.message}
    return None


def main() -> None:
    """Read one hook payload from stdin and print the verdict, if any.

    Exits 0 whatever happens: a guard that crashed on its own bug must not be
    what stops a session, and the stop path speaks through JSON.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        out = hook_output(payload, time.time(), dict(os.environ))
    except Exception:  # noqa: BLE001
        out = None
    if out is not None:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
