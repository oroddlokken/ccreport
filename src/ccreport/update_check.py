"""Ask GitHub whether a release newer than this Homebrew install exists.

Only a Homebrew install is checked, because it is the only one this can name
the upgrade command for. A git checkout pulls, a `uv tool install` reinstalls,
a bare wheel is replaced by whoever put it there — each updates by a route
that has nothing to do with `brew upgrade`, so for those the check answers
nothing and the status line stays quiet.

Run detached, never from a render: the request takes as long as the network
takes. `statusline.py` spawns `python3 -m ccreport.update_check` when the
stored stamp is older than UPDATE_CHECK_INTERVAL_S, and the stamp is written
even when the check fails, so an unreachable API costs one process per
interval rather than one per render.

Module-level imports stay stdlib-cheap and already-loaded, because
`statusline.py` imports this on its slow path for the three readers at the
top. urllib, importlib.metadata and cache_db are deferred into the functions
that need them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

# The release track. `vX.Y.Z` tags publish a GitHub Release and a wheel, and
# the tap's formula points at that wheel, so the newest tag is what a
# `brew upgrade` would land on.
UPSTREAM_REPO = "oroddlokken/ccreport"

# What the status line tells the user to run. Fully qualified, because the tap
# is not one Homebrew searches by default.
BREW_FORMULA = "oroddlokken/tap/ccreport"

# How old the stored answer may be before the status line stops trusting it.
# Longer than the check interval by enough to survive a laptop that was closed,
# short enough that a fortnight of failed checks goes quiet rather than
# repeating a version that has had time to become wrong.
UPDATE_MAX_AGE_S = 129_600  # 1.5 days

_HTTP_TIMEOUT_S = 10
# Homebrew installs every formula under <prefix>/Cellar/<name>/<version>, so
# one segment identifies the install across /opt/homebrew, /usr/local and
# linuxbrew without running `brew --prefix`.
_CELLAR_SEGMENTS = ("Cellar", "ccreport")
_VERSION_RE = re.compile(r"\A[vV]?(\d+(?:\.\d+)*)")


def is_brew_install() -> bool:
    """Whether the package this module belongs to sits inside a Homebrew keg.

    The path is resolved first: the formula installs into `libexec` under
    `opt/ccreport`, which is a symlink into the Cellar, and only the resolved
    path carries the version directory that names the keg.
    """
    parts = Path(__file__).resolve().parts
    return any(parts[i : i + 2] == _CELLAR_SEGMENTS for i in range(len(parts) - 1))


def installed_version() -> str | None:
    """The version of the installed `ccreport` distribution, or None.

    None where the package has no installed metadata, which is a source tree
    on `PYTHONPATH` rather than anything a Homebrew keg can produce.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ccreport")
    except (PackageNotFoundError, ValueError):
        return None


def parse_version(text: str) -> tuple[int, ...] | None:
    """`v0.2.1` or `0.2.1` as a tuple of ints, or None when it is neither.

    Anything past the numeric run is dropped, so `0.2.1.dev3+g9e7ff54` — what
    hatch-vcs writes for a build between tags — compares as the release it
    followed. A dev build one commit past the newest tag therefore reads as up
    to date, which is the honest answer: `brew upgrade` has nothing to give it.
    """
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    """Whether *latest* is a higher version than *current*.

    False whenever either side does not parse: a comparison against a version
    string nobody can order is a line the user cannot act on.
    """
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def latest_release() -> str | None:
    """The newest release tag of UPSTREAM_REPO, or None if unanswered.

    One unauthenticated request, which GitHub allows 60 of an hour per IP
    against the 2 a day this spends. `/releases/latest` skips drafts and
    prereleases, so it names the release the tap's formula was rewritten for.

    None for every failure — a rate-limited 403, an unreachable host, a body
    that does not parse, a repo with no release yet. The caller stores None as
    "do not render a line", where a stale tag would render as an upgrade that
    is not there.
    """
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ccreport-update-check",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    tag = body.get("tag_name")
    return tag if isinstance(tag, str) and tag else None


def run() -> None:
    """Perform one check and store the result. Never raises.

    The stamp is written on every outcome, including the ones that store no
    tag: it is what paces the spawn, and a check that could not answer must
    still push the next attempt out by a full interval.
    """
    from ccreport import cache_db

    if not is_brew_install():
        return
    current = installed_version()
    now = time.time()
    if current is None:
        cache_db.write_update_check("", None, now)
        return
    cache_db.write_update_check(current, latest_release(), now)


def main() -> None:
    try:
        run()
    except Exception:  # noqa: BLE001 — a detached child has nobody to report to
        pass


if __name__ == "__main__":
    main()
