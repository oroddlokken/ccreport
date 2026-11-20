"""Read Codex rollout usage without reading credentials or storing transcripts.

Modern rollouts repeat their usage in cumulative token_count events. Only
token_usage_record is a billable-call event here; cumulative counters are never
added to it. scan.py owns cache writes, as it does for Claude's parser.
"""

from __future__ import annotations

import os
import warnings
from datetime import UTC, datetime
from pathlib import Path

import orjson

from ccreport.aggregate import TokenCounts, UsageRecord
from ccreport.sources import SourceImportWarning

_EVENT_MARKERS = (b'"session_meta"', b'"turn_context"', b'"token_usage_record"', b'"token_count"')
_USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)


def _roots() -> tuple[Path, Path]:
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return root / "sessions", root / "archived_sessions"


def discover_codex_files() -> list[Path]:
    """Discover active and archived rollouts under CODEX_HOME, without auth reads."""
    return sorted({path for root in _roots() if root.is_dir() for path in root.rglob("*.jsonl")})


def is_codex_file(path: Path) -> bool:
    """Whether a log belongs to one of the configured Codex session directories."""
    return any(path.is_relative_to(root) for root in _roots())


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity(payload: dict) -> dict:
    """Keep identity fields only: a session_meta also carries the full instructions."""
    git = payload.get("git")
    return {
        "id": _text(payload.get("id")),
        "session_id": _text(payload.get("session_id")),
        "cwd": _text(payload.get("cwd")),
        "repo": _text(git.get("repository_url")) if isinstance(git, dict) else None,
        "account_uuid": _text(payload.get("creator_account_id")),
        "history_start": payload.get("subagent_history_start_ordinal"),
        "forked": bool(payload.get("forked_from_id")),
    }


def _tokens(usage: dict) -> tuple[TokenCounts, int] | None:
    values = [usage.get(name, 0) for name in _USAGE_FIELDS]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    tin, cached, written, tout, reasoning = values
    # Cache reads and reasoning are observed subsets. The rollout's cache-write
    # accounting is not yet established: reject it rather than inventing a
    # split or charging zero. Every inspected modern rollout has written=0.
    if written != 0 or cached > tin or reasoning > tout:
        return None
    total = usage.get("total_tokens", tin + tout)
    if type(total) is not int or total != tin + tout:
        return None
    return TokenCounts(input=tin - cached, output=tout, cache_read=cached), reasoning


def _project(identity: dict) -> tuple[str, str | None, str | None]:
    # Lazy to avoid a cycle: scan dispatches here and owns these shared rules.
    from ccreport import scan

    cwd = identity.get("cwd")
    remote = identity.get("repo")
    repo = scan._normalize_remote(remote) if remote else None  # noqa: SLF001 - shared identity rules
    if repo:
        return repo.rsplit("/", 1)[-1], cwd, repo
    if cwd:
        name, repo = scan.project_for_cwd(cwd)
        return name, cwd, repo
    return "unknown", cwd, None


def _record(payload: dict, timestamp, model: str, identity: dict, owner: dict) -> UsageRecord | None:
    response = _text(payload.get("response_id"))
    usage = payload.get("usage")
    if not response or not isinstance(usage, dict):
        return None
    parsed = _tokens(usage)
    if parsed is None:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if "project_identity" not in identity:
        identity["project_identity"] = _project(identity)
    project, cwd, repo = identity["project_identity"]
    tokens, reasoning = parsed
    thread_id = _text(payload.get("thread_id")) or owner.get("id")
    return UsageRecord(
        message_id=response, dedup_key=response, model=model,
        tokens=tokens, timestamp=when,
        session_id=_text(payload.get("session_id")) or identity.get("session_id") or thread_id or "unknown",
        project=project, cwd=cwd, repo=repo,
        source="codex", account_uuid=identity.get("account_uuid"),
        thread_id=thread_id, reasoning_output_tokens=reasoning,
    )


def parse_rollout_file(path: Path) -> list[UsageRecord]:
    """Parse per-call usage, binding it to its own thread and turn metadata.

    The first session_meta names the file's owner. Forks can then contain their
    parent's metadata and contexts; those must not overwrite the child. Calls
    from inherited threads are left to their own logs. The history boundary
    keeps a parent's last model from becoming the fallback for a child.

    Read failures propagate: returning half a file would overwrite its complete
    cache. A partial final JSON line is ignored until the file grows again.
    Older token_count-only rollouts are explicitly reported as unsupported.
    """
    identities: dict[str, dict] = {}
    owner: dict = {}
    contexts: dict[str, str] = {}
    pending: list[tuple[dict, object, str | None]] = []
    last_model: str | None = None
    has_counts = False
    with path.open("rb") as stream:
        for raw in stream:
            if not any(marker in raw for marker in _EVENT_MARKERS):
                continue
            try:
                event = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                continue
            payload = event["payload"]
            kind = event.get("type")
            if kind == "session_meta":
                identity = _identity(payload)
                if not owner:
                    owner = identity
                if identity["id"]:
                    identities.setdefault(identity["id"], identity)
            elif kind == "turn_context":
                model = _text(payload.get("model"))
                turn = _text(payload.get("turn_id"))
                boundary = owner.get("history_start")
                ordinal = event.get("ordinal")
                inherited = isinstance(boundary, int) and (
                    not isinstance(ordinal, int) or ordinal < boundary
                )
                if model and not inherited:
                    if turn:
                        contexts[turn] = model
                    # A fork with no ordinal boundary cannot establish that
                    # an unmatched turn belongs to the child, so don't guess.
                    if not owner.get("forked") or isinstance(boundary, int):
                        last_model = model
            elif kind == "token_usage_record":
                pending.append((payload, event.get("timestamp"), last_model))
            elif kind == "event_msg" and payload.get("type") == "token_count":
                has_counts = True

    if not pending and has_counts:
        warnings.warn(SourceImportWarning(
            f"Codex rollout {path.name} has only legacy token_count events; its usage is not imported",
            "codex", "Legacy token_count-only logs were skipped.",
        ), stacklevel=2)
    records: list[UsageRecord] = []
    seen: set[str] = set()
    invalid = 0
    unsupported_writes = 0
    missing_context = 0
    for payload, timestamp, fallback in pending:
        thread = _text(payload.get("thread_id")) or owner.get("id")
        if owner.get("id") and thread != owner["id"]:
            continue
        identity = identities.get(thread or "", owner)
        exact_model = contexts.get(_text(payload.get("turn_id")) or "")
        model = exact_model or fallback or "unknown"
        rec = _record(payload, timestamp, model, identity, owner)
        if rec is None:
            usage = payload.get("usage")
            written = usage.get("cache_write_input_tokens") if isinstance(usage, dict) else None
            if type(written) is int and written > 0:
                unsupported_writes += 1
            else:
                invalid += 1
        elif rec.message_id not in seen:
            seen.add(rec.message_id)
            records.append(rec)
            if exact_model is None:
                missing_context += 1
    if invalid:
        warnings.warn(SourceImportWarning(
            f"Skipped {invalid} invalid Codex usage record(s) in {path.name}",
            "codex", "Invalid usage records were skipped.",
        ), stacklevel=2)
    if unsupported_writes:
        warnings.warn(SourceImportWarning(
            f"Skipped {unsupported_writes} Codex usage record(s) with unsupported "
            f"cache-write accounting in {path.name}",
            "codex", "Usage with unsupported cache-write accounting was skipped.",
        ), stacklevel=2)
    if missing_context:
        warnings.warn(SourceImportWarning(
            f"{missing_context} Codex usage record(s) in {path.name} lack matching turn_context; "
            "model attribution uses the preceding own-thread model, or unknown",
            "codex", "Some model attribution uses earlier context or remains unknown.",
        ), stacklevel=2)
    return records
