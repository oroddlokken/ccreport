"""Which project a usage record belongs to — one definition, for every reader.

Three answers used to coexist: a path prefix under the projects dir, a cwd's
basename, and ccreport's override table. Only the third knew about `ccreport
merge`, so a merge regrouped the reports and left the statusline's per-project
cost windows split. Everything that needs a project name now resolves it here,
and pricing.py layers the directory scoping on top.

cache_db is imported inside the function rather than at module level: cache_db
imports pricing and pricing imports this module, so a top-level import would
close the loop before any of the three finished initialising.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from functools import cache
from pathlib import Path

# Repos live directly beneath repo-root container dirs. A session's project
# is the segment just under the deepest matching root, so subdirectories and
# git worktrees collapse into their repo (e.g. ~/git/company/web -> company)
# and a repo opened from two places stays one. ~/git is always a repo root;
# per-machine layouts (~/dev and friends) are added via the config file,
# which can only ever add roots, never remove the baseline.
_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CONFIG_PATH = _CONFIG_HOME / "ccreport" / "ccreport.toml"

# Where it sat while this tooling was a directory in the macsetup repo.
# cache_db.relocate_legacy_paths moves it, alongside the cache and snapshots.
LEGACY_CONFIG_PATH = _CONFIG_HOME / "macsetup" / "claude" / "ccreport.toml"

_BASELINE_REPO_ROOT = Path.home() / "git"

# (repo, cwd, derived name) -> the name the record is reported under.
Resolver = Callable[[str | None, str | None, str], str]


@cache
def repo_roots() -> tuple[str, ...]:
    """Return the ~/git baseline plus repo_roots from the config file.

    Sorted deepest-first, which makes matching longest-prefix regardless of
    config order. No config file (or no repo_roots key) leaves just the
    baseline; a malformed file warns instead of taking the reports down
    with it.

    Read on first use rather than at import, and only once: pricing imports this
    module on every statusline render, including the fast renders that name no
    project at all, and this was a file open and a TOML parse in each of them.
    """
    import tomllib

    roots = {str(_BASELINE_REPO_ROOT)}
    try:
        with open(CONFIG_PATH, "rb") as f:
            extra = tomllib.load(f).get("repo_roots", [])
    except FileNotFoundError:
        extra = []
    except (tomllib.TOMLDecodeError, OSError) as e:
        print(f"warning: ignoring {CONFIG_PATH}: {e}", file=sys.stderr)
        extra = []
    roots.update(str(Path(r).expanduser()) for r in extra if isinstance(r, str))
    return tuple(sorted(roots, key=len, reverse=True))


def __getattr__(name: str) -> object:
    """Resolve the REPO_ROOTS module constant repo_roots() replaced.

    Nothing in this repo reads it any more, but it was a public module
    attribute; anything out of tree that still imports it gets the same tuple
    rather than an ImportError.
    """
    if name == "REPO_ROOTS":
        return repo_roots()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def repo_from_path(cwd: str) -> str | None:
    """Return the repo directory name for a cwd under a repo root.

    None leaves the caller free to fall back to the plain basename for paths
    outside every repo root.
    """
    for root in repo_roots():
        prefix = root + "/"
        if cwd.startswith(prefix):
            repo = cwd[len(prefix):].split("/", 1)[0]
            if repo:
                return repo
    return None


def name_for_cwd(cwd: str) -> str:
    """The project name a record logged from *cwd* would be filed under.

    ccreport.parse_jsonl_file prefers the git remote's repo basename and falls
    back to this; callers that cannot afford to shell out to git (the
    statusline runs this on every render) use it directly. The two agree
    whenever the checkout sits under a repo root with the remote's own name,
    which is every normal clone.
    """
    return repo_from_path(cwd) or Path(cwd).name


def implied_name(kind: str, value: str) -> str | None:
    """The project name parse_jsonl_file would have derived from a rule's source.

    A remote's project is the repo's own basename; a cwd's is its repo under a
    repo root, else its basename. Both mirror parse_jsonl_file exactly — if
    that derivation changes, this must follow it. None for name rules, which
    already are a name.
    """
    if kind == "remote":
        return value.rsplit("/", 1)[-1] or None
    if kind == "cwd_prefix":
        value = value.rstrip("/")
        return repo_from_path(value) or Path(value).name or None
    return None


def build_override_fn() -> Resolver | None:
    """Compile the override table into a (repo, cwd, name) -> name function.

    Rules apply in insertion order; first match wins. Returns None when there
    are no rules so the hot loop pays nothing.

    A record with neither repo nor cwd is history whose source JSONL was purged
    before those columns existed, and no backfill can bring them back. Matching
    remote and cwd_prefix rules on them alone would silently skip precisely the
    rows that cannot be re-parsed, splitting a project in two. So for those
    records only, a remote or cwd_prefix rule also matches the project name its
    own source would have produced — the remote's repo basename, or the cwd's
    repo/basename. The match sits at the rule's own position in the order, so an
    orphan lands wherever the live records of the same project land, and live
    records are unaffected either way.

    Still out of reach: an orphan whose cwd was a subdirectory of a cwd_prefix
    outside every repo root, since it was named after the subdirectory. A
    name-kind rule on that name catches it.
    """
    from ccreport.cache_db import get_project_overrides

    rules = get_project_overrides()
    if not rules:
        return None
    compiled = [
        (r["match_kind"], r["match_value"], r["target"],
         implied_name(r["match_kind"], r["match_value"]))
        for r in rules
    ]

    def resolve(repo: str | None, cwd: str | None, name: str) -> str:
        signalless = repo is None and cwd is None
        for kind, value, target, implied in compiled:
            if kind == "name" and name == value:
                return target
            if kind == "remote" and repo == value:
                return target
            if kind == "cwd_prefix" and cwd and (
                cwd == value or cwd.startswith(value.rstrip("/") + "/")
            ):
                return target
            if signalless and implied is not None and name == implied:
                return target
        return name

    return resolve


def record_project(rec: dict, resolve: Resolver | None) -> str:
    """The project a cached ccreport record belongs to, overrides applied.

    *rec* is the compact dict shape cache_db hands back; *resolve* is one
    build_override_fn() per computation, not one per record — the table is
    small but the record count is not.
    """
    name = rec.get("project") or ""
    if resolve is None:
        return name
    return resolve(rec.get("repo"), rec.get("cwd"), name)
