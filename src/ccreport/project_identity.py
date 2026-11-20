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
import re
import sys
from collections.abc import Callable, Collection, Mapping
from fnmatch import fnmatchcase
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

# What a `ccreport merge` rule can match on; build_override_fn is the matcher.
OVERRIDE_KINDS = ("name", "remote", "cwd_prefix", "cwd_glob")

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


# Where a git worktree hangs off its checkout: a common layout for worktrees
# kept inside the repo, and the one Claude Code's isolated subagents create. A worktree is
# deleted when its branch merges, after which no git command can say whose it
# was, so the path is the only witness left.
_WORKTREE_MARKERS = ("/.worktrees/", "/.claude/worktrees/")

# Claude Code's per-session scratch area: <tmp>/claude-<uid>/<encoded cwd>/<session>/.
# A subagent or `claude -p` started there logs the scratch path as its cwd, which
# without this names the project "scratchpad".
_SCRATCHPAD_RE = re.compile(
    r"^(?:/private)?(?:/tmp|/var/folders/[^/]+/[^/]+/T)/claude-\d+/(-[^/]+)(?:/|$)"
)
_NOT_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def claude_encode(path: str) -> str:
    """A path the way Claude Code spells it in a directory name.

    Every character but an ASCII letter or digit becomes "-", so "/", "." and
    "ø" all read the same afterwards: ~/dev/a.b and ~/dev/a/b encode
    alike. That loss is why scratchpad_origin checks a decoding against disk
    rather than reversing it. pricing.project_key is a different spelling with
    a different job (a cache key that only swaps "/") and is not this.
    """
    return _NOT_ALNUM_RE.sub("-", path)


def worktree_checkout(cwd: str) -> str | None:
    """The checkout a cwd under <repo>/.worktrees/ or <repo>/.claude/worktrees/ belongs to.

    The outermost marker wins, so a worktree nested in a worktree still names
    the repo they all came from. None for a cwd outside both layouts.
    """
    padded = cwd.rstrip("/") + "/"
    hits = [i for marker in _WORKTREE_MARKERS if (i := padded.find(marker)) > 0]
    return padded[:min(hits)] if hits else None


def _decode_on_disk(encoded: str) -> list[str]:
    """Every existing directory whose claude_encode() is *encoded*.

    Walks down from / one component at a time and only into entries whose
    encoding is a prefix of what is left, so the cost is a directory listing
    per level of the answer, not a search. Stops once a second answer turns
    up: the caller only takes a unique one.
    """
    found: list[str] = []

    def walk(directory: str, rest: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if len(found) > 1:
                return
            enc = "-" + claude_encode(entry.name)
            if not rest.startswith(enc):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if not is_dir:
                continue
            if len(rest) == len(enc):
                found.append(entry.path)
            elif rest[len(enc)] == "-":
                walk(entry.path, rest[len(enc):])

    walk("/", encoded)
    return found


def scratchpad_origin(
    cwd: str, known_cwds: Callable[[], Mapping[str, Collection[str]]] | None = None,
) -> str | None:
    """The working directory a Claude Code scratchpad cwd was made for.

    None when *cwd* is no scratchpad, or when the encoded name cannot be read
    back to a single directory — the caller then names the record the way it
    always did. The directory is looked for on disk first; one that has since
    been renamed or deleted is matched against *known_cwds*, the cwds earlier
    records carried, keyed by claude_encode(). It is a callable so the index is
    only built for a scratchpad the disk could not answer. Two candidates
    either way is no answer, never a guess.
    """
    m = _SCRATCHPAD_RE.match(cwd)
    if m is None:
        return None
    encoded = m.group(1)
    on_disk = _decode_on_disk(encoded)
    if len(on_disk) == 1:
        return on_disk[0]
    if on_disk or known_cwds is None:
        return None
    candidates = known_cwds().get(encoded, ())
    return next(iter(candidates)) if len(candidates) == 1 else None


def name_for_cwd(cwd: str) -> str:
    """The project name a record logged from *cwd* would be filed under.

    scan.project_for_cwd prefers the git remote's repo basename and falls back
    to this; callers that cannot afford to shell out to git (the statusline
    runs this on every render) use it directly. The two agree whenever the
    checkout sits under a repo root with the remote's own name, which is every
    normal clone.

    A scratchpad stands for the directory it was made for, and a worktree under
    .worktrees/ or .claude/worktrees/ for the checkout it hangs off — named
    after that checkout itself rather than the segment under a repo root, since
    a checkout inside a container directory (~/dev/work/<repo>) is named for the
    container otherwise.
    """
    real = scratchpad_origin(cwd) or cwd
    checkout = worktree_checkout(real)
    if checkout is not None:
        return Path(checkout).name
    return repo_from_path(real) or Path(real).name


def implied_name(kind: str, value: str) -> str | None:
    """The project name parse_jsonl_file would have derived from a rule's source.

    A remote's project is the repo's own basename; a cwd's is its repo under a
    repo root, else its basename. Both mirror parse_jsonl_file exactly — if
    that derivation changes, this must follow it. None for name rules, which
    already are a name, and for glob rules, which match many directories and
    so imply no one name.
    """
    if kind == "remote":
        return value.rsplit("/", 1)[-1] or None
    if kind == "cwd_prefix":
        value = value.rstrip("/")
        return name_for_cwd(value) or None
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

    A cwd_glob rule matches the whole cwd with fnmatch, case-sensitively, and
    its "*" crosses "/": /var/folders/*/T/tmp-* takes every directory
    mkdtemp(prefix="tmp-") made and everything under them. It implies no name,
    so it never reaches a record without a cwd.
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
            if kind == "cwd_glob" and cwd and fnmatchcase(cwd, value):
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
