"""Read OpenCode usage metadata from one consistent, read-only storage snapshot.

V1 step-finish parts are calls; assistant and session totals repeat their usage.
SQLite wins over the older JSON store, which commonly remains after migration.
No credentials, message text, or tool arguments enter the projected snapshot.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import orjson

from ccreport.aggregate import TokenCounts, UsageRecord
from ccreport.sources import SourceUnit

_VIRTUAL_ROOT = Path("/__ccreport_sources__/opencode")


class OpenCodeReadError(ValueError):
    """An unsupported or incomplete snapshot must not replace cached usage."""


def is_opencode_path(path: Path) -> bool:
    """Identify virtual per-session cache paths, which are never opened as files."""
    return path.is_relative_to(_VIRTUAL_ROOT)


def _default_root() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode"


def _dict(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise OpenCodeReadError(f"Invalid OpenCode {name} metadata")
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise OpenCodeReadError(f"Missing OpenCode {name}")
    return value


def _millis(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise OpenCodeReadError(f"Invalid OpenCode {name} timestamp")
    return value


def _usage(value) -> tuple[TokenCounts, int]:
    value = _dict(value, "tokens")
    cache = _dict(value.get("cache", {}), "cache tokens")
    counts = [value.get("input", 0), value.get("output", 0), value.get("reasoning", 0),
              cache.get("read", 0), cache.get("write", 0)]
    if any(type(count) is not int or count < 0 for count in counts):
        raise OpenCodeReadError("Invalid OpenCode token count")
    inp, out, reasoning, read, write = counts
    # OpenCode has already removed cache from input and reasoning from output.
    return TokenCounts(input=inp, output=out + reasoning, cache_read=read, cache_create=write), reasoning


def _reported_cost(value) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise OpenCodeReadError("Invalid OpenCode reported cost")
    return float(value)


def _sqlite_snapshot(path: Path) -> tuple[dict, list, dict]:
    # mode=ro still sees WAL commits; immutable=1 would silently miss them.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "session_message" in tables and conn.execute("SELECT 1 FROM session_message LIMIT 1").fetchone():
            raise OpenCodeReadError("OpenCode V2 session_message usage is not supported yet")
        if not {"session", "message", "part", "project"} <= tables:
            raise OpenCodeReadError("Unsupported OpenCode SQLite schema")
        sessions = {row["id"]: dict(row) for row in conn.execute("""
            SELECT s.id, s.parent_id, s.time_created, s.directory,
                   p.worktree FROM session s LEFT JOIN project p ON p.id = s.project_id
        """)}
        messages = [dict(row) for row in conn.execute("""
            SELECT id, session_id,
                   json_extract(data, '$.time.created') AS created,
                   json_extract(data, '$.time.completed') AS completed,
                   json_extract(data, '$.modelID') AS model,
                   json_extract(data, '$.providerID') AS provider,
                   json_extract(data, '$.tokens') AS tokens,
                   json_extract(data, '$.cost') AS cost,
                   json_type(data, '$.cost') AS cost_type
            FROM message WHERE json_extract(data, '$.role') = 'assistant'
            ORDER BY id
        """)]
        parts: dict[str, list[dict]] = defaultdict(list)
        for row in conn.execute("""
            SELECT id, message_id, session_id, time_created AS created,
                   json_extract(data, '$.tokens') AS tokens,
                   json_extract(data, '$.cost') AS cost,
                   json_type(data, '$.cost') AS cost_type
            FROM part WHERE json_extract(data, '$.type') = 'step-finish'
            ORDER BY id
        """):
            item = dict(row)
            if item["cost_type"] not in (None, "null", "integer", "real"):
                raise OpenCodeReadError("Invalid OpenCode reported cost")
            item["tokens"] = orjson.loads(item["tokens"]) if item["tokens"] is not None else None
            parts[item["message_id"]].append(item)
        for item in messages:
            if item["cost_type"] not in (None, "null", "integer", "real"):
                raise OpenCodeReadError("Invalid OpenCode reported cost")
            item["tokens"] = orjson.loads(item["tokens"]) if item["tokens"] is not None else None
        return sessions, messages, parts
    finally:
        conn.close()


def _json_objects(root: Path, kind: str):
    for path in sorted((root / kind).rglob("*.json")):
        yield _dict(orjson.loads(path.read_bytes()), kind)


def _legacy_snapshot(root: Path) -> tuple[dict, list, dict]:
    projects = {p["id"]: p.get("worktree") for p in _json_objects(root, "project")}
    sessions = {}
    for item in _json_objects(root, "session"):
        sessions[_text(item.get("id"), "session ID")] = {
            "id": item["id"], "parent_id": item.get("parentID"),
            "directory": item.get("directory"), "worktree": projects.get(item.get("projectID")),
            "time_created": _dict(item.get("time"), "session time").get("created"),
        }
    messages = []
    for item in _json_objects(root, "message"):
        if item.get("role") != "assistant":
            continue
        time = _dict(item.get("time"), "message time")
        messages.append({"id": item.get("id"), "session_id": item.get("sessionID"),
                         "created": time.get("created"), "completed": time.get("completed"),
                         "model": item.get("modelID"), "provider": item.get("providerID"),
                         "tokens": item.get("tokens"), "cost": item.get("cost")})
    parts: dict[str, list[dict]] = defaultdict(list)
    for item in _json_objects(root, "part"):
        if item.get("type") != "step-finish":
            continue
        message = _text(item.get("messageID"), "part message ID")
        parts[message].append({"id": item.get("id"), "message_id": message,
                               "session_id": item.get("sessionID"), "created": None,
                               "tokens": item.get("tokens"), "cost": item.get("cost")})
    return sessions, messages, parts


def _thread(session_id: str, sessions: dict) -> str:
    current = session_id
    seen = set()
    while current in sessions:
        if current in seen:
            raise OpenCodeReadError("Cyclic OpenCode session parent metadata")
        seen.add(current)
        parent = sessions[current].get("parent_id")
        if not parent:
            return current
        current = _text(parent, "parent session ID")
    return current


def _project(session: dict) -> tuple[str, str | None, str | None]:
    from ccreport import scan

    cwd = session.get("directory") or session.get("worktree")
    if cwd is not None:
        cwd = _text(cwd, "project directory")
    if not cwd:
        return "unknown", cwd, None
    name, repo = scan.project_for_cwd(cwd)
    return name, cwd, repo


def _units(root: Path, sessions: dict, messages: list, parts: dict) -> list[SourceUnit]:
    records: dict[str, list[UsageRecord]] = {sid: [] for sid in sessions}
    projects = {}
    message_ids = set()
    seen_parts = set()
    for msg in messages:
        mid = _text(msg.get("id"), "message ID")
        if mid in message_ids:
            raise OpenCodeReadError("Duplicate OpenCode message ID")
        message_ids.add(mid)
        sid = _text(msg.get("session_id"), "session ID")
        if sid not in sessions:
            raise OpenCodeReadError("OpenCode message has no session metadata")
        session = sessions[sid]
        created = _millis(msg["created"], "message created")
        session_created = _millis(session["time_created"], "session created")
        # Session.fork clones IDs but retains original message times. Such
        # history is not new usage, even though a fork has no parent_id.
        if created < session_created:
            continue
        calls = parts.get(mid, [])
        if not calls:
            if msg.get("tokens") is None:
                continue
            tokens, _ = _usage(msg["tokens"])
            if not tokens.total:
                continue
            # A still-streaming assistant has no final authoritative fallback.
            if msg.get("completed") is None:
                continue
            calls = [{"id": mid, "session_id": sid, "created": msg["completed"],
                      "tokens": msg["tokens"], "cost": msg["cost"]}]
        for call in calls:
            cid = _text(call.get("id"), "call ID")
            if cid in seen_parts:
                raise OpenCodeReadError("Duplicate OpenCode call ID")
            seen_parts.add(cid)
            if call.get("session_id") != sid:
                raise OpenCodeReadError("OpenCode part belongs to a different session")
            tokens, reasoning = _usage(call["tokens"])
            stamp = call.get("created")
            if stamp is None:
                stamp = msg.get("completed")
            when = datetime.fromtimestamp(_millis(stamp, "call completed") / 1000, UTC)
            if sid not in projects:
                projects[sid] = _project(session)
            project, cwd, repo = projects[sid]
            records[sid].append(UsageRecord(
                message_id=cid, dedup_key=cid, model=_text(msg["model"], "model"),
                provider=_text(msg["provider"], "provider") if msg.get("provider") is not None else None,
                tokens=tokens, reasoning_output_tokens=reasoning, timestamp=when,
                source="opencode", session_id=sid, thread_id=_thread(sid, sessions),
                project=project, cwd=cwd, repo=repo, account_uuid=None, cost_usd=None,
                reported_cost_usd=_reported_cost(call.get("cost")),
            ))
    if set(parts) - message_ids:
        raise OpenCodeReadError("OpenCode step-finish has no assistant metadata")
    namespace = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    units = []
    for sid, recs in sorted(records.items()):
        # The fingerprint covers every persisted field, including project
        # resolution and raw provider costs, but excludes unrelated transcripts.
        recs.sort(key=lambda rec: rec.message_id)
        payload = orjson.dumps([{
            "id": rec.message_id, "model": rec.model, "provider": rec.provider,
            "input": rec.tokens.input, "output": rec.tokens.output,
            "read": rec.tokens.cache_read, "write": rec.tokens.cache_create,
            "reasoning": rec.reasoning_output_tokens, "time": rec.timestamp.isoformat(),
            "thread": rec.thread_id, "project": rec.project, "cwd": rec.cwd, "repo": rec.repo,
            "reported_cost": rec.reported_cost_usd,
        } for rec in recs], option=orjson.OPT_SORT_KEYS)
        digest = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & ((1 << 63) - 1)
        safe_sid = _text(sid, "session ID")
        if "/" in safe_sid or safe_sid in {".", ".."}:
            raise OpenCodeReadError("Invalid OpenCode session ID")
        units.append(SourceUnit(path=_VIRTUAL_ROOT / namespace / f"{safe_sid}.jsonl",
                                mtime_ns=digest, size=len(payload), records=recs))
    return units


def discover_opencode_units(root: Path | None = None) -> list[SourceUnit]:
    """Read V1 SQLite (including WAL), or legacy JSON only when SQLite is absent.

    Root is the OpenCode data directory, defaulting to $XDG_DATA_HOME/opencode.
    Malformed/unsupported snapshots raise, so scan can retain previous records.
    Each unit is a session with an opaque content fingerprint, not a disk file.
    """
    root = (root if root is not None else _default_root()).expanduser().resolve()
    db = root / "opencode.db"
    try:
        if db.exists():
            snapshot = _sqlite_snapshot(db)
        elif (root / "storage").exists():
            snapshot = _legacy_snapshot(root / "storage")
        else:
            return []
        return _units(root, *snapshot)
    except (KeyError, TypeError, OverflowError) as exc:
        # Keep errors inside scan's documented snapshot-failure contract;
        # never publish the valid half of an inconsistent legacy store.
        raise OpenCodeReadError("Malformed OpenCode usage metadata") from exc
