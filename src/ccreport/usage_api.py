"""Fetch Claude Code usage data via the Anthropic OAuth API, cached for 10 minutes.

Uses GET https://api.anthropic.com/api/oauth/usage with the user's OAuth token
sourced from macOS Keychain or ~/.claude/.credentials.json.

Run it as `python -m ccreport.usage_api`; the status line spawns it that way,
and importing the package first is what makes its own imports resolve.

AUDIT: All calculations are documented in docs/calculation-reference.md.
When changing any calculation, caching, or data format here,
update that document to match.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ccreport.cache_db import (
    clear_fetch_failures,
    read_usage_cache,
    read_usage_stale,
    record_fetch_failure,
    release_costs_lock,
    release_fetch_lock,
    try_acquire_costs_lock,
    try_acquire_fetch_lock,
    write_usage_cache,
)
from ccreport.pricing import compute_costs

# 10 minutes: how old a usage row may be before main() refetches rather than
# reprint it. statusline.USAGE_FETCH_INTERVAL_S is the authority for that
# interval; this and the max_age=600 defaults of cache_db.read_usage_cache and
# read_cost_summary spell the same number and must move with it. Drop the
# statusline's interval below this one and every render past it spawns a fetch
# that this module answers straight from the cache it wanted refreshed.
CACHE_MAX_AGE = 600

# Every field fetch_usage_api can produce. write_usage_cache leaves columns the
# write dict does not mention alone, which is what keeps a failed cost
# computation from nulling the cost columns — but for these an omission is a
# statement: the API drops a quota that no longer applies (no Sonnet cap on this
# plan, a scoped limit that lapsed), and without an explicit null the old reading
# would outlive the quota it described.
_API_QUOTA_FIELDS = (
    "session_percent", "session_reset", "week_percent", "week_reset",
    "sonnet_percent", "sonnet_reset",
    "scoped_percent", "scoped_model", "scoped_reset",
    "extra_percent", "extra_spent", "extra_limit",
)

# Follower poll backoff in _wait_for_leader: each miss costs a SELECT of the
# whole usage row, so the interval doubles from the first value up to the cap.
LEADER_POLL_MIN_DELAY = 0.1
LEADER_POLL_MAX_DELAY = 1.0

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_API_TIMEOUT = 5  # seconds per attempt
USAGE_API_RETRIES = 2  # max retries on transient errors
USAGE_API_RETRY_DELAY = 1.0  # seconds between retries
USAGE_API_MAX_RETRY_DELAY = 5.0  # ceiling on a Retry-After the server asks for
CREDENTIALS_SERVICE = "Claude Code-credentials"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

KEYCHAIN_TIMEOUT = 5  # seconds per `security` lookup
KEYCHAIN_DUMP_TIMEOUT = 10  # seconds for the one `security dump-keychain`

# Candidate services probed after the primary lookup misses, each costing its
# own KEYCHAIN_TIMEOUT. The list is sorted newest-first, and a machine that has
# logged in repeatedly accumulates entries without bound — uncapped, a keychain
# that answers slowly turns the tail of that list into minutes of serial waiting
# under the fetch lock. Five covers switching between a handful of accounts;
# past that the credentials file is the likelier answer anyway.
MAX_KEYCHAIN_CANDIDATES = 5

# Longest get_usage_token() + fetch_usage_api() can legitimately run, which is
# how long main() holds the fetch lock: every keychain call it may make, then
# every API attempt and the sleeps between them. Kept as an expression over the
# same constants those paths use so it cannot drift from them.
_TOKEN_LOOKUP_MAX_S = (
    KEYCHAIN_TIMEOUT  # primary find-generic-password
    + KEYCHAIN_DUMP_TIMEOUT  # dump-keychain on a miss
    + MAX_KEYCHAIN_CANDIDATES * KEYCHAIN_TIMEOUT  # one lookup per candidate
)
_API_CALL_MAX_S = (1 + USAGE_API_RETRIES) * USAGE_API_TIMEOUT + (
    USAGE_API_RETRIES * USAGE_API_MAX_RETRY_DELAY
)
# Margin for the work the timeouts do not bound: the JSONL scan in
# compute_costs, the cache write, and process startup.
_LOCK_HOLD_MARGIN_S = 15

# A TTL below this makes the next spawn treat a lock that is still doing its job
# as abandoned and issue a second concurrent fetch — against an endpoint that,
# in the case that got the holder here, is already answering 429.
FETCH_LOCK_MAX_HOLD_S = _TOKEN_LOOKUP_MAX_S + _API_CALL_MAX_S + _LOCK_HOLD_MARGIN_S


# ---------------------------------------------------------------------------
# OAuth token retrieval
# ---------------------------------------------------------------------------


def _read_token_from_keychain(service: str = CREDENTIALS_SERVICE) -> str | None:
    """Read OAuth token from macOS Keychain."""
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT,
        )
        if raw.returncode != 0 or not raw.stdout.strip():
            return None
        return _parse_token(raw.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return None


def _list_keychain_candidates() -> list[str]:
    """List Claude Code credential service names from keychain, newest first.

    At most MAX_KEYCHAIN_CANDIDATES of them: each one the caller keeps costs a
    serial `security` call under the fetch lock, and the newest entry is the one
    the running Claude Code wrote.
    """
    try:
        raw = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True, timeout=KEYCHAIN_DUMP_TIMEOUT,
        )
        if raw.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    # Decode with replacement to handle non-UTF8 output
    output = raw.stdout.decode("utf-8", errors="replace")

    import re
    services: list[tuple[str, str | None]] = []
    # Split on individual keychain item boundaries instead of keychain file
    # boundaries, so multiple entries within one keychain are found
    items = re.split(r"(?=class:)", output)
    for item in items:
        svc_m = re.search(r'"svce"<blob>="([^"]+)"', item)
        if not svc_m:
            continue
        svc = svc_m.group(1)
        if not svc.startswith(CREDENTIALS_SERVICE) or svc == CREDENTIALS_SERVICE:
            continue
        mdat_m = re.search(r'"mdat"<timedate>=(?:0x[0-9A-Fa-f]+\s+)?"([^"]+)"', item)
        mdat = mdat_m.group(1).replace("\\000", "").strip() if mdat_m else None
        services.append((svc, mdat))

    services.sort(key=lambda x: x[1] or "", reverse=True)
    return [svc for svc, _ in services[:MAX_KEYCHAIN_CANDIDATES]]


def _read_token_from_credentials_file() -> str | None:
    """Read OAuth token from ~/.claude/.credentials.json."""
    cred_file = Path.home() / ".claude" / ".credentials.json"
    try:
        return _parse_token(cred_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _parse_token(raw: str) -> str | None:
    """Extract claudeAiOauth.accessToken from JSON string."""
    try:
        data = json.loads(raw)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, AttributeError):
        return None


def get_usage_token() -> str | None:
    """Get OAuth token: Keychain (primary + candidates) → credentials file."""
    if sys.platform == "darwin":
        token = _read_token_from_keychain()
        if token:
            return token
        for svc in _list_keychain_candidates():
            token = _read_token_from_keychain(svc)
            if token:
                return token
    return _read_token_from_credentials_file()


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------


def request_usage_body(token: str) -> dict[str, Any]:
    """GET the usage endpoint and return the untouched response body.

    Raises on HTTP/network errors after exhausting retries for transient failures.
    """
    req = Request(
        USAGE_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )

    last_exc: Exception | None = None
    for attempt in range(1 + USAGE_API_RETRIES):
        try:
            resp = urlopen(req, timeout=USAGE_API_TIMEOUT)  # noqa: S310
            body = json.loads(resp.read().decode())
            break
        except HTTPError as e:
            if e.code not in _RETRYABLE_STATUS or attempt == USAGE_API_RETRIES:
                raise
            last_exc = e
            delay = USAGE_API_RETRY_DELAY
            # Respect Retry-After header (seconds only)
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after:
                try:
                    delay = max(delay, min(float(retry_after), USAGE_API_MAX_RETRY_DELAY))
                except ValueError:
                    pass
            time.sleep(delay)
        except (URLError, OSError, TimeoutError) as e:
            if attempt == USAGE_API_RETRIES:
                raise
            last_exc = e
            time.sleep(USAGE_API_RETRY_DELAY)
    else:
        raise last_exc  # type: ignore[misc]

    return body


def fetch_usage_api(token: str) -> dict[str, Any]:
    """Fetch usage data from the Anthropic API, mapped to our column names.

    The keys are a subset of _API_QUOTA_FIELDS — a full response produces all of
    them, and a quota the response leaves out is simply absent from the dict
    rather than present as None. main() is what turns that absence into the
    explicit null the cache needs.

    request_usage_body's errors come out of here untouched: HTTPError once the
    status is not in _RETRYABLE_STATUS or the retries are spent, URLError and
    OSError once they are spent.
    """
    body = request_usage_body(token)

    data: dict[str, Any] = {}

    # five_hour → session
    five_hour = body.get("five_hour", {})
    if five_hour.get("utilization") is not None:
        data["session_percent"] = int(five_hour["utilization"])
    if five_hour.get("resets_at"):
        data["session_reset"] = five_hour["resets_at"]

    # seven_day → week
    seven_day = body.get("seven_day", {})
    if seven_day.get("utilization") is not None:
        data["week_percent"] = int(seven_day["utilization"])
    if seven_day.get("resets_at"):
        data["week_reset"] = seven_day["resets_at"]

    # seven_day_sonnet → sonnet
    sonnet = body.get("seven_day_sonnet", {})
    if sonnet and sonnet.get("utilization") is not None:
        data["sonnet_percent"] = int(sonnet["utilization"])
    if sonnet and sonnet.get("resets_at"):
        data["sonnet_reset"] = sonnet["resets_at"]

    # weekly_scoped model limit (e.g. Fable) → scoped
    # Lives in limits[]; the scope carries the model's display name.
    for lim in body.get("limits") or []:
        if lim.get("kind") != "weekly_scoped" or lim.get("percent") is None:
            continue
        model = (lim.get("scope") or {}).get("model") or {}
        name = model.get("display_name")
        if not name:
            continue
        data["scoped_percent"] = int(lim["percent"])
        data["scoped_model"] = name
        if lim.get("resets_at"):
            data["scoped_reset"] = lim["resets_at"]
        break

    extra = body.get("extra_usage", {})
    if extra.get("utilization") is not None:
        data["extra_percent"] = int(extra["utilization"])
    if extra.get("used_credits") is not None:
        # API returns cents; convert to dollars for our format
        data["extra_spent"] = extra["used_credits"] / 100
    if extra.get("monthly_limit") is not None:
        data["extra_limit"] = extra["monthly_limit"] / 100

    return data


def _enrich_and_emit(
    data: dict[str, Any],
    session_id: str | None,
    cwd: str | None,
    *,
    warn_on_error: bool = True,
) -> None:
    """Merge cost info into *data*, then print as JSON."""
    try:
        costs = compute_costs(
            session_id=session_id, cwd=cwd,
            session_reset_iso=data.get("session_reset") or _arg_value("--session-reset"),
            week_reset_iso=data.get("week_reset") or _arg_value("--week-reset"),
        )
        data.update(costs)
    except Exception as e:  # noqa: BLE001
        if warn_on_error:
            print(f"Warning: cost computation failed: {e}", file=sys.stderr)
    print(json.dumps(data, indent=2))


def _emit_raw() -> None:
    """Print the untouched API response body.

    Bypasses the cache — only mapped fields are stored, never the raw body.
    Failures here don't touch the fetch-failure backoff: this path is a manual
    debugging tool, not the statusline's fetch.
    """
    token = get_usage_token()
    if not token:
        print("Error: no OAuth token found", file=sys.stderr)
        sys.exit(1)
    try:
        print(json.dumps(request_usage_body(token), indent=2))
    except HTTPError as e:
        print(f"Error: API returned {e.code}", file=sys.stderr)
        sys.exit(1)
    except (URLError, OSError, json.JSONDecodeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _arg_value(flag: str) -> str | None:
    """Value following *flag* in argv, or None."""
    if flag in sys.argv:
        idx = sys.argv.index(flag)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


def _run_costs_only(session_id: str | None, cwd: str | None) -> None:
    """Recompute costs and refresh the cache without calling the usage API.

    The statusline spawns this instead of a full fetch when S/W arrive natively
    on stdin and no fetch-only field (Sonnet, scoped, Extra) can affect the
    render — see _api_fetch_needed there. Every API-derived column is carried
    over from the existing row, including last_updated, so the TTL and
    staleness markers keep describing the last *real* fetch rather than
    claiming freshness this run did not obtain.

    Window starts come from --session-reset/--week-reset when given (the native
    stdin values, always current) and fall back to the cached ones.
    """
    if not try_acquire_costs_lock():
        return  # another process is already recomputing
    try:
        row = read_usage_stale() or {}
        row.pop("_stale", None)
        try:
            costs = compute_costs(
                session_id=session_id, cwd=cwd,
                session_reset_iso=_arg_value("--session-reset") or row.get("session_reset"),
                week_reset_iso=_arg_value("--week-reset") or row.get("week_reset"),
            )
        except Exception as e:  # noqa: BLE001
            print(f"Error: cost computation failed: {e}", file=sys.stderr)
            sys.exit(1)
        row.update(costs)
        write_usage_cache(row, snapshot_extra=False)
        print(json.dumps(row, indent=2))
    finally:
        release_costs_lock()


def _parse_cli_args() -> tuple[bool, int, str | None, str | None, bool]:
    """Parse CLI arguments. Returns (force, wait_timeout, session_id, cwd, raw)."""
    force = "--force" in sys.argv
    raw = "--raw" in sys.argv
    # 30s is generous, which is what a CLI invocation the user is watching wants:
    # waiting out a slow leader beats printing an error. The statusline passes
    # --wait-timeout 4 instead, because a render that waits prints nothing.
    wait_timeout = 30
    wt = _arg_value("--wait-timeout")
    if wt:
        try:
            wait_timeout = int(wt)
        except ValueError:
            pass
    return force, wait_timeout, _arg_value("--session"), _arg_value("--cwd"), raw


def _wait_for_leader(
    wait_timeout: int,
    session_id: str | None,
    cwd: str | None,
) -> None:
    """Poll for fresh cache while another process is fetching. Exits on result.

    The interval backs off from LEADER_POLL_MIN_DELAY to LEADER_POLL_MAX_DELAY:
    a leader that returns quickly — the common case — is picked up sooner than a
    flat interval would, and a slow one costs a handful of reads instead of one
    every half second for the whole timeout. The deadline is wall-clock, so
    wait_timeout still bounds the wait however the delays fall.
    """
    deadline = time.monotonic() + wait_timeout
    delay = LEADER_POLL_MIN_DELAY
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        cached = read_usage_cache(CACHE_MAX_AGE)
        if cached:
            _enrich_and_emit(cached, session_id, cwd)
            sys.exit(0)
        delay = min(delay * 2, LEADER_POLL_MAX_DELAY)
    # Leader failed — emit stale data if available
    stale = read_usage_stale()
    if stale:
        stale["_stale"] = True
        _enrich_and_emit(stale, session_id, cwd)
        sys.exit(0)
    print("Error: fetch timed out waiting for leader", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    """Fetch and print Claude usage data, using cache when fresh."""
    force, wait_timeout, session_id_arg, cwd_arg, raw = _parse_cli_args()

    if raw:
        _emit_raw()
        return

    if "--costs-only" in sys.argv:
        _run_costs_only(session_id_arg, cwd_arg)
        return

    if not force:
        cached = read_usage_cache(CACHE_MAX_AGE)
        if cached:
            _enrich_and_emit(cached, session_id_arg, cwd_arg)
            return

    acquired_lock = force or try_acquire_fetch_lock()
    if not acquired_lock:
        _wait_for_leader(wait_timeout, session_id_arg, cwd_arg)
        return

    try:
        token = get_usage_token()
        if not token:
            record_fetch_failure()
            print("Error: no OAuth token found", file=sys.stderr)
            sys.exit(1)

        t_start = time.monotonic()
        data = fetch_usage_api(token)
        fetch_duration = round(time.monotonic() - t_start, 2)

        if not data or ("session_percent" not in data and "week_percent" not in data):
            record_fetch_failure()
            print("Error: API returned no usage data", file=sys.stderr)
            sys.exit(1)

        clear_fetch_failures()
        data["last_updated"] = datetime.now(tz=UTC).astimezone().isoformat()
        data["_meta"] = {"fetch_duration_s": fetch_duration, "method": "api"}

        try:
            # The response's own resets_at wins; the flags are the caller's
            # native stdin bounds, standing in when this response omitted a
            # window — without any bound compute_costs returns no
            # session_window_cost and the previous window's total survives
            # in the row.
            costs = compute_costs(
                session_id=session_id_arg, cwd=cwd_arg,
                session_reset_iso=data.get("session_reset") or _arg_value("--session-reset"),
                week_reset_iso=data.get("week_reset") or _arg_value("--week-reset"),
            )
            data.update(costs)
        except Exception as e:  # noqa: BLE001
            print(f"Warning: cost computation failed: {e}", file=sys.stderr)

        # The quota fields this response omitted are written as explicit nulls;
        # everything else it does not mention (the cost columns, when the
        # computation above failed) keeps the value it had.
        write_usage_cache({**dict.fromkeys(_API_QUOTA_FIELDS), **data})
        print(json.dumps(data, indent=2))

    except HTTPError as e:
        # Don't enter backoff for permanent auth errors — the token is bad,
        # retrying with the same token won't help, but a fresh token (after
        # re-login) should work immediately.
        if e.code in (401, 403):
            print(f"Error: API returned {e.code} (auth)", file=sys.stderr)
        else:
            record_fetch_failure()
            print(f"Error: API returned {e.code}", file=sys.stderr)
        sys.exit(1)
    except (URLError, OSError, json.JSONDecodeError) as e:
        record_fetch_failure()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if acquired_lock and not force:
            release_fetch_lock()


if __name__ == "__main__":
    main()
