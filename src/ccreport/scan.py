"""Read Claude Code's JSONL session logs into the cache the reports run off.

Separate from ccreport.py because the detached push client sends what these
tables hold and imports no rich, as accounts.py is separate for the same
reason. What a name derives from lives here now, so `_script_hash` covers this
file rather than every report renderer beside it.
"""

import hashlib
import re
import subprocess
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import orjson

from ccreport import project_identity
from ccreport.aggregate import TokenCounts, UsageRecord
from ccreport.cache_db import (
    check_ccreport_valid,
    init_ccreport_meta,
    invalidate_ccreport,
    load_ccreport_file_meta,
    save_ccreport_files,
)
from ccreport.pricing import extract_assistant_fields

_CONFIG_PATH = project_identity.CONFIG_PATH
_repo_from_path = project_identity.repo_from_path

_PROJECT_ROOTS = (
    Path.home() / ".claude" / "projects",
    Path.home() / ".config" / "claude" / "projects",
)

# Git remote is the durable project identity: it survives a folder being moved
# or deleted, where a path does not. Resolved lazily at parse time (only while
# the working dir still exists) and cached per cwd within a run.
_remote_cache: dict[str, str | None] = {}


def _normalize_remote(url: str) -> str:
    """Reduce a git remote URL to a stable host/path key.

    Handles scp-style (git@host:org/repo.git), ssh:// (with optional port),
    and https:// forms; strips credentials, port, and the .git suffix.
    """
    url = url.strip()
    url = re.sub(r"\.git$", "", url)
    m = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)  # scp-style: git@host:path
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return url


def _resolve_remote(cwd: str) -> str | None:
    """Return the normalized origin remote for a cwd, or None.

    None when the dir is gone, it isn't a git repo, or there is no origin —
    callers then fall back to the path-based name.
    """
    if cwd in _remote_cache:
        return _remote_cache[cwd]
    result: str | None = None
    if Path(cwd).is_dir():
        try:
            out = subprocess.run(
                ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                result = _normalize_remote(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            result = None
    _remote_cache[cwd] = result
    return result

# --- File-level cache ---
# BUMP THIS when the dicts _serialize_records writes change shape:
# _deserialize_records subscripts their keys, so a stored row in the old shape
# is a KeyError, not a degraded read. check_ccreport_valid passes only when this
# number, _script_hash() and cache_db.CACHE_SCHEMA_SALT all match what was stored
# beside the rows; any one differing clears the cache and re-parses the corpus.
# Not folded into that hash even though it covers this file: the hash moves on
# every edit here, this comment included, so it can say only "something changed",
# never "the stored format did". This is the deliberate knob on the reader's
# side of the contract, as the salt is on cache_db's.
CACHE_VERSION = 2

# Freshly parsed files buffered before one write transaction. Small enough
# that a full re-parse never holds the write lock across a long stretch of
# parsing — a statusline waiting on that lock gives up after 10 s.
_SAVE_BATCH = 250


@cache
def _script_hash() -> str:
    """SHA256 of the project-naming inputs, used to invalidate the cache.

    This module, project_identity.py, and the repo-roots config all shape the
    project names frozen into cached records at parse time, so editing any of
    them must trigger a re-parse. Nothing in ccreport.py does, since the parse
    moved here — a change to a report renderer no longer costs a corpus
    re-parse. pricing.py deliberately does not participate:
    a price change rewrites costs through the cost columns, not through names,
    and hashing it would re-parse the whole corpus every time a model is added.

    Cached because a rollup run asks twice — once for the cache contract, once
    inside the rollup fingerprint — and the answer cannot change under a
    process that is reading its own source.
    """
    h = hashlib.sha256()
    try:
        h.update(Path(__file__).read_bytes())
        h.update(Path(project_identity.__file__).read_bytes())
    except OSError:
        return ""
    try:
        h.update(_CONFIG_PATH.read_bytes())
    except OSError:
        pass  # no config is a valid state; hash covers just the code
    return h.hexdigest()


def _ensure_cache_valid(live_paths: set[str]) -> None:
    """Ensure ccreport cache is valid; invalidate and reinitialize if stale.

    *live_paths* bounds the invalidation to files still on disk — records from
    purged files can't be re-parsed, so their costs must survive.
    """
    sh = _script_hash()
    if not check_ccreport_valid(CACHE_VERSION, sh):
        invalidate_ccreport(live_paths)
        init_ccreport_meta(CACHE_VERSION, sh)


def _serialize_records(records: list) -> list[dict]:
    """Convert UsageRecords to compact cache dicts."""
    return [
        {
            "mid": r.message_id,
            "model": r.model,
            "ts": r.timestamp.timestamp(),
            "sid": r.session_id,
            "project": r.project,
            "cwd": r.cwd,
            "repo": r.repo,
            "dk": r.dedup_key,
            "cost": r.cost_usd,
            "t": [r.tokens.input, r.tokens.output, r.tokens.cache_create, r.tokens.cache_read],
        }
        for r in records
    ]


def _deserialize_records(raw: list[dict]) -> list:
    """Convert compact cache dicts back to UsageRecords."""
    return [
        UsageRecord(
            message_id=r["mid"],
            model=r["model"],
            timestamp=datetime.fromtimestamp(r["ts"], tz=UTC),
            session_id=r["sid"],
            project=r["project"],
            cwd=r.get("cwd"),
            repo=r.get("repo"),
            dedup_key=r.get("dk"),
            cost_usd=r.get("cost"),
            tokens=TokenCounts(
                input=r["t"][0], output=r["t"][1],
                cache_create=r["t"][2], cache_read=r["t"][3],
            ),
        )
        for r in raw
    ]


def project_display_name(project_dir: str) -> str:
    """Convert directory name like '-Users-ove-git-foo' to 'foo'."""
    parts = project_dir.strip("-").split("-")
    if parts:
        return parts[-1]
    return project_dir


def discover_jsonl_files() -> list[Path]:
    files = []
    for d in _PROJECT_ROOTS:
        if d.is_dir():
            files.extend(d.rglob("*.jsonl"))
    return sorted(files)


def _resolve_from_filesystem(dir_name: str) -> str | None:
    """Reconstruct a real project name from a dash-encoded directory name.

    Claude Code encodes both '/' and '-' as '-' in projects-dir names, so a
    project at /Users/ove/git/project-name-v2 lands as
    -Users-ove-git-project-name-v2 — ambiguous without context. Try every
    possible split point and pick the one whose reconstructed path exists
    on disk; prefer the longest tail (most dashes preserved in the name).
    """
    parts = dir_name.strip("-").split("-")
    if not parts:
        return None
    for i in range(len(parts)):
        prefix = Path("/" + "/".join(parts[:i])) if i > 0 else Path("/")
        name = "-".join(parts[i:])
        if name and (prefix / name).is_dir():
            return name
    return None


def _derive_project(path: Path) -> str:
    """Derive project display name from a JSONL file's location.

    Used as fallback when records lack a cwd field. Tries to reconstruct
    the real project name against the filesystem; falls back to the
    last-segment heuristic if no real path matches.
    """
    for root in _PROJECT_ROOTS:
        try:
            rel = path.relative_to(root)
            if rel.parts:
                dir_name = rel.parts[0]
                return _resolve_from_filesystem(dir_name) or project_display_name(dir_name)
        except ValueError:
            continue
    return project_display_name(path.parent.name)


def parse_jsonl_file(path: Path) -> list[UsageRecord]:
    """Parse a single JSONL file and extract usage records.

    A read error propagates rather than yielding the lines read so far: the
    caller writes whatever comes back over the file's complete cache entry,
    so a truncated return is silent, permanent data loss.
    """
    records = []
    cwd_from_records: str | None = None

    with open(path, "rb") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue

            if cwd_from_records is None:
                c = rec.get("cwd")
                if isinstance(c, str) and c:
                    cwd_from_records = c

            fields = extract_assistant_fields(rec)
            if fields is None:
                continue
            msg, usage, message_id, _request_id, dedup_key, ts = fields

            # `or` rather than a get() default: a key present with JSON null
            # returns None, which lands in a NOT NULL column and takes down the
            # whole file's insert on every run until the JSONL changes.
            tokens = TokenCounts(
                input=usage.get("input_tokens") or 0,
                output=usage.get("output_tokens") or 0,
                cache_create=usage.get("cache_creation_input_tokens") or 0,
                cache_read=usage.get("cache_read_input_tokens") or 0,
            )

            cost_usd = rec.get("costUSD")
            if cost_usd is not None:
                try:
                    cost_usd = float(cost_usd)
                except (ValueError, TypeError):
                    cost_usd = None

            records.append(UsageRecord(
                message_id=message_id,
                model=msg.get("model") or "unknown",
                tokens=tokens,
                timestamp=ts,
                session_id=rec.get("sessionId") or path.stem,
                project="",
                cost_usd=cost_usd,
                dedup_key=dedup_key,
            ))

    repo = _resolve_remote(cwd_from_records) if cwd_from_records else None
    if repo:
        # Group by the repo's own name, not the full remote, so a host/org move
        # (e.g. GitLab -> GitHub) keeps history together. A true repo rename is
        # a manual `ccreport merge` away.
        project = repo.rsplit("/", 1)[-1]
    elif cwd_from_records:
        project = _repo_from_path(cwd_from_records) or Path(cwd_from_records).name
    else:
        project = _derive_project(path)
    for r in records:
        r.project = project
        r.cwd = cwd_from_records
        r.repo = repo

    return records


def _refresh_changed_files(
    files: list[Path], file_meta: dict[str, tuple[int, int]],
) -> tuple[dict[str, list[UsageRecord]], set[str]]:
    """Re-parse and cache every file whose (mtime_ns, size) left the cache.

    Returns what each re-parsed file now holds, plus the paths that could not
    be read at all — a caller must drop those rather than fall back to their
    cached records, which is what makes an unreadable file under-report for one
    run instead of reporting a mix of two parses.

    Saves in batches so no single transaction spans a long stretch of parsing.
    """
    fresh: dict[str, list[UsageRecord]] = {}
    unreadable: set[str] = set()
    pending: list[tuple[str, int, int, list[dict]]] = []
    for path in files:
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            unreadable.add(key)
            continue
        cached = file_meta.get(key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            continue
        try:
            records = parse_jsonl_file(path)
        except (OSError, UnicodeDecodeError):
            # Skipping the save leaves the file's previous cache entry whole;
            # this run under-reports it, the next readable parse restores it.
            # Saving a partial parse would not.
            unreadable.add(key)
            continue
        fresh[key] = records
        pending.append((key, st.st_mtime_ns, st.st_size, _serialize_records(records)))
        if len(pending) >= _SAVE_BATCH:
            save_ccreport_files(pending)
            pending = []
    save_ccreport_files(pending)
    return fresh, unreadable


def refresh_cache() -> None:
    """Parse into the cache every session log that has changed since the last run.

    What a report does on its way to the numbers, for a caller that needs the
    cache current and nothing else: the push, whose records come out of these
    tables and which would otherwise offer the server a corpus that stops at
    the last time someone ran the CLI.
    """
    files = discover_jsonl_files()
    _ensure_cache_valid({str(p) for p in files})
    _refresh_changed_files(files, load_ccreport_file_meta())
