"""Ask GitHub how far master has moved past the local checkout.

There are no tagged versions here — master is the release, and the unit of
"an update is available" is a commit. So this asks the compare API where the
local HEAD sits relative to origin's master and stores the answer for the
status line to render.

Run detached, never from a render: the request takes as long as the network
takes. `statusline.py` spawns `python3 -m ccreport.update_check` when the
stored stamp is older than UPDATE_CHECK_INTERVAL_S, and the stamp is written
even when the check fails, so an unreachable API costs one process per
interval rather than one per render.

Nothing here writes to the checkout. The remote state comes over HTTPS, and
the local SHA is read out of `.git` as a file — no fetch, no refs touched, and
no git process on the render path.

Module-level imports stay stdlib-cheap and already-loaded, because
`statusline.py` imports this on its slow path for the two readers at the top.
urllib, subprocess and cache_db are deferred into the functions that need them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

# master is the release. A fork whose default branch is named differently
# compares against a branch it does not have and silently reports nothing.
UPSTREAM_BRANCH = "master"

# How old the stored answer may be before the status line stops trusting it.
# Longer than the check interval by enough to survive a laptop that was closed,
# short enough that a fortnight of failed checks goes quiet rather than
# repeating a number that has had time to become wrong.
UPDATE_MAX_AGE_S = 129_600  # 1.5 days

_HTTP_TIMEOUT_S = 10
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
# Both remote forms `git clone` writes: git@github.com:owner/repo.git and
# https://github.com/owner/repo.git, with or without the suffix.
_SLUG_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?\Z")


def checkout_root() -> Path | None:
    """The git checkout this module was imported from, or None if there is none.

    None means there is nothing to update in place: `uv tool install .` copies
    the package into an environment with no `.git` beside it, and so does a
    wheel. A `.git` *file* — a submodule or a worktree — reads as None too,
    since the readers below expect the real directory.
    """
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").is_dir() else None


def local_head_sha(root: Path) -> str | None:
    """The SHA at HEAD, read out of `.git` without running git.

    This is called from a render, where a subprocess would cost more than
    everything else on the line put together. Three layouts answer HEAD: a
    detached SHA written straight into the file, a loose ref file, and
    `packed-refs` — which is where a fresh clone keeps every branch until
    something writes to one.
    """
    git_dir = root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if _SHA_RE.match(head):
        return head
    if not head.startswith("ref: "):
        return None
    ref = head[5:].strip()
    try:
        loose = (git_dir / ref).read_text(encoding="utf-8").strip()
        if _SHA_RE.match(loose):
            return loose
    except OSError:
        pass
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in packed.splitlines():
        # '#' opens the header, '^' the peeled target of the tag above it —
        # neither line names a ref of its own.
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _SHA_RE.match(sha):
            return sha
    return None


def remote_slug(root: Path) -> str | None:
    """`owner/repo` for origin, or None when origin is not a GitHub remote.

    Derived rather than hardcoded: a fork checks itself against its own origin,
    which is the remote its user pulls from.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    m = _SLUG_RE.search(out.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def commits_behind(slug: str, sha: str) -> int | None:
    """How many commits UPSTREAM_BRANCH is ahead of *sha*, or None if unanswered.

    One unauthenticated request, which GitHub allows 60 of an hour per IP
    against the 2 a day this spends. The compare endpoint answers ancestry and
    count together, which is why it is this one and not `/commits`: `status`
    separates "you are behind" from "you have local commits", and `ahead_by`
    counts the branch's lead either way.

    None, not 0, for every failure — a 404 because the local commit was never
    pushed, a rate-limited 403, an unreachable host, a body that does not
    parse. The caller stores None as "do not render a number", where 0 would
    render as up to date on evidence nobody has.
    """
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{slug}/compare/{sha}...{UPSTREAM_BRANCH}"
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
    status = body.get("status")
    # Ahead means master carries commits the checkout lacks, and diverged means
    # that too plus local commits of its own — still worth reporting, since
    # ahead_by counts only master's side. The other two leave nothing to pull.
    if status not in ("ahead", "diverged"):
        return 0
    try:
        return max(0, int(body.get("ahead_by", 0)))
    except (TypeError, ValueError):
        return None


def run() -> None:
    """Perform one check and store the result. Never raises.

    The stamp is written on every outcome, including the ones that store no
    count: it is what paces the spawn, and a check that could not answer must
    still push the next attempt out by a full interval.
    """
    from ccreport import cache_db

    root = checkout_root()
    if root is None:
        return
    sha = local_head_sha(root)
    now = time.time()
    if sha is None:
        cache_db.write_update_check("", None, now)
        return
    slug = remote_slug(root)
    behind = commits_behind(slug, sha) if slug else None
    cache_db.write_update_check(sha, behind, now)


def main() -> None:
    try:
        run()
    except Exception:  # noqa: BLE001 — a detached child has nobody to report to
        pass


if __name__ == "__main__":
    main()
