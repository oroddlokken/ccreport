"""Send this machine's records to a ccreport server.

Runs two ways: `ccreport push`, and a detached spawn from the status line's
slow path every thirty minutes. Neither is on the render path — the status
line spawns this the way it spawns usage_api.py and never imports it.

Nothing happens without ~/.config/ccreport/push.toml, which
`ccreport server connect` writes. A machine that has not opted in pays nothing:
no config, no push, no spawn.

The cache is opened read-only for everything except the watermark, which is one
short write transaction at the end. A render must never wait on this process,
and a long-held lock on cache.db is what would make it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "ccreport" / "push.toml"

BASE_INTERVAL_S = 30 * 60
"""How often the status line's spawn is allowed to try. The client cache is
already whole; this only decides how fresh the merged view is."""

MAX_INTERVAL_S = 8 * 60 * 60
"""The ceiling the interval widens to. A server down overnight is then eight
attempts short of one per interval, rather than twenty."""

REQUEST_TIMEOUT_S = 120
"""A first push is a machine's whole history, which the server prices as it
stores. Later pushes are the handful of files that changed."""

DEFAULT_MAX_BODY = 8 * 1024 * 1024
"""What one request may carry before a file is held back for the next one.
Under the server's own default, so the limit that bites is this one, where the
client can still do something about it."""


class PushError(Exception):
    """The push could not be completed. Transient unless *terminal*."""

    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


@dataclass(frozen=True)
class ServerConfig:
    """One server's entry in push.toml, token and policy together."""

    url: str
    token: str
    label: str
    machine_id: str
    max_body: int = DEFAULT_MAX_BODY
    restricted: bool = False
    """Whether a project has to be opted in by name to be identified at all.

    False is what the personal machines want: everything pushes under its real
    name. True is the work laptop, where every project outside *allow* pushes
    its token counts with its identity stripped."""
    allow: tuple[str, ...] = ()
    """Projects that keep their names, already resolved through this machine's
    merge rules so an alias matches the way a report groups it."""
    salt: str = ""
    """What the pseudonyms are hashed against. Generated when restricted is
    first set and never leaves the machine, so the server sees a durable
    grouping key it cannot reverse."""
    networks: tuple[str, ...] = ()
    """CIDRs this machine must hold an address inside before it pushes here.
    Empty means no gate, which is what a personal machine wants."""


@dataclass
class PushResult:
    """What one run did, in the words `ccreport push` prints."""

    server: str
    accepted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    records: int = 0
    blocked_by: tuple[str, ...] = ()
    """The CIDRs a blocked push wanted and could not find an address inside.

    Set instead of anything else: a gated machine sends no request and records
    no watermark, and `ccreport push` prints this so the silence is explained
    rather than mysterious."""

    @property
    def ok(self) -> bool:
        return not self.rejected

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)


def _marker_path(path: Path) -> Path:
    """Where "this machine has been restricted" is recorded.

    Beside push.toml rather than in cache.db, and read before the file it
    guards: a wiped cache must not be able to unredact a work laptop, and a
    push.toml that stopped parsing must not either.
    """
    return path.parent / ".restricted"


def read_raw(path: Path | None = None) -> dict:
    """push.toml as it stands, or an empty document.

    The `[server."URL"]` tables only. Everything a caller writes goes back
    through write_server, so this is the one place the file's shape is read.
    """
    path = path or CONFIG_PATH
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    servers = raw.get("server")
    return servers if isinstance(servers, dict) else {}


def load_config(path: Path | None = None) -> list[ServerConfig]:
    """Every server push.toml names, or an empty list if there is no file.

    An absent or unreadable file is not an error: it is the ordinary state of a
    machine that has not been connected to anything, and a machine with no
    server pushes nothing, which is already the closed state.

    What is not safe to read as open is a file that parses but has lost its
    `restricted = true` — an edit, a partial write, a restore of an older copy.
    The marker beside it says this machine has been restricted before, and it
    wins: the entry redacts everything rather than falling back to real names.
    """
    path = path or CONFIG_PATH
    was_restricted = _marker_path(path).exists()
    servers = []
    for url, entry in read_raw(path).items():
        if not isinstance(entry, dict) or not entry.get("token"):
            continue
        states_restriction = bool(entry.get("restricted"))
        # An entry the marker had to correct is an entry that lost a field, so
        # its allow list is not trustworthy either: nothing keeps its name.
        allow = tuple(str(name) for name in (entry.get("allow") or ())) \
            if states_restriction or not was_restricted else ()
        servers.append(ServerConfig(
            url=url,
            token=entry["token"],
            label=entry.get("label") or os.uname().nodename,
            machine_id=entry.get("machine_id") or "",
            max_body=int(entry.get("max_body") or DEFAULT_MAX_BODY),
            restricted=states_restriction or was_restricted,
            allow=allow,
            salt=str(entry.get("salt") or ""),
            networks=tuple(str(net) for net in (entry.get("networks") or ())),
        ))
    return servers


def configured(path: Path | None = None) -> bool:
    """Whether this machine pushes anywhere at all.

    What the status line checks before spawning: one stat, and no spawn on a
    machine that has not opted in.
    """
    return bool(load_config(path))


def write_server(path: Path, url: str, fields: dict) -> None:
    """Merge *fields* into one server's table, leaving every other one alone.

    A config you can only rewrite wholesale is a config people edit by hand and
    get wrong, so re-running connect for one server must not disturb another.
    Written at mode 0600, because the file holds a token.
    """
    entries = read_raw(path)
    entries[url] = {**entries.get(url, {}), **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for server_url, entry in entries.items():
        lines.append(f'[server."{server_url}"]')
        lines += [f"{key} = {_toml_value(value)}" for key, value in entry.items()]
        lines.append("")
    path.write_text("\n".join(lines))
    path.chmod(0o600)
    if entries[url].get("restricted"):
        _marker_path(path).write_text(
            "This machine pushes under a restriction. Deleting this file does not\n"
            "lift it: push.toml is what says so, and this only stops a lost\n"
            "`restricted = true` from reading as permission to send real names.\n",
        )


def _toml_value(value) -> str:
    """One value as TOML. Enough for the four shapes this file holds."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(json.dumps(str(item)) for item in value) + "]"
    return json.dumps(str(value))


def new_salt() -> str:
    """A fresh redaction salt. Generated when restricted is first set."""
    import secrets

    return secrets.token_hex(16)


def pseudonym(salt: str, name: str) -> str:
    """A stable stand-in for a project name the server may not learn.

    The first 8 hex of a hash over the salt and the resolved name, so the same
    project reads as the same key on every push and across machines that share
    a salt — and the salt never leaves the machine, so the server cannot walk
    it back to the name.
    """
    return hashlib.sha256(f"{salt}\x00{name}".encode()).hexdigest()[:8]


def pseudo_session(salt: str, session_id: str) -> str:
    """The same, for a session id.

    Longer than a project pseudonym: a machine has tens of projects and tens of
    thousands of sessions, and the session report is only useful while they
    stay distinct.
    """
    return hashlib.sha256(f"{salt}\x00session\x00{session_id}".encode()).hexdigest()[:16]


def redact(rec: dict, server: ServerConfig) -> dict:
    """Strip a record's identity unless its project is opted in.

    What survives is everything the money is made of: model, timestamps and
    token counts. What goes is project, cwd, repo and session id — the project
    and the session as pseudonyms rather than nulls, so the server can still
    group them, and cwd and repo as nothing at all, since a path and a remote
    are the name written out.
    """
    if not server.restricted or rec["project"] in server.allow:
        return rec
    return {
        **rec,
        "project": pseudonym(server.salt, rec["project"] or ""),
        "cwd": None,
        "repo": None,
        "sid": pseudo_session(server.salt, rec["sid"] or "") if rec["sid"] else None,
    }


def policy_hash(server: ServerConfig, override_rules: object = "") -> str:
    """What the redaction depends on, as one key beside the watermark.

    A project moved out of *allow* has to stop being named on the server, and
    the files that named it were pushed long ago. So a change here forces a
    full re-push rather than waiting for those files to change, which they
    never will — a session log that has been closed is closed for good.

    The local merge rules are in it for the same reason: they decide which name
    `allow` is matched against, so editing one re-points the whole policy.
    """
    material = "\x00".join([
        "1" if server.restricted else "0",
        *sorted(server.allow),
        server.salt,
        repr(override_rules),
    ])
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _probe_source_address(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> str | None:
    """The local address the kernel would send from to reach *network*.

    A connected UDP socket, which picks a route and a source address without
    putting a packet on the wire. That is what keeps this cheap enough for a
    gate evaluated before every push, and it is why a VPN handing out an
    address in the range counts as being on the network — which is the
    intended behaviour.
    """
    family = socket.AF_INET6 if network.version == 6 else socket.AF_INET
    # A host address inside the range, not the network address itself: routing
    # to a /32 or a /128 has to have somewhere to aim.
    target = str(next(network.hosts(), network.network_address))
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect((target, 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def on_allowed_network(networks: tuple[str, ...] | list[str]) -> bool:
    """Whether this machine holds an address inside one of *networks*.

    An empty list is no gate at all, which is what a personal machine wants.

    A malformed CIDR blocks rather than being skipped: a typo in a work
    laptop's config must never read as permission to push from a hotel wifi.
    """
    if not networks:
        return True
    # Every entry is parsed before any is probed. Validating as we go would let
    # a machine that matched the first CIDR never reach the typo in the second,
    # so the same config would pass at the office and fail nowhere else.
    parsed = []
    for entry in networks:
        try:
            parsed.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            return False
    for network in parsed:
        source = _probe_source_address(network)
        if source is None:
            continue
        try:
            if ipaddress.ip_address(source.split("%")[0]) in network:
                return True
        except ValueError:
            continue
    return False


def _read_only(db_path: Path) -> sqlite3.Connection:
    """Open the cache read-only, so a render never waits on this process."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def changed_files(
    conn: sqlite3.Connection, watermark: dict[str, tuple[int, int]],
) -> list[tuple[str, int, int]]:
    """Cached files whose fingerprint differs from what the server acknowledged.

    Straight off ccreport_files rather than off the disk: the cache is what
    holds the records about to be sent, and a file that has grown since it was
    cached is the next `ccreport` run's business, not this one's.
    """
    rows = conn.execute("SELECT path, mtime_ns, size FROM ccreport_files ORDER BY path").fetchall()
    return [
        (path, mtime_ns, size)
        for path, mtime_ns, size in rows
        if watermark.get(path) != (mtime_ns, size)
    ]


def _records_for(conn: sqlite3.Connection, path: str) -> list[dict]:
    """One file's cached records, as the rows they were stored as."""
    from ccreport.cache_db import _CCR_COLS

    cols = ", ".join(_CCR_COLS)
    rows = conn.execute(
        f"SELECT {cols} FROM ccreport_records WHERE file_path = ? ORDER BY id",  # noqa: S608
        (path,),
    ).fetchall()
    return [dict(zip(_CCR_COLS, row, strict=True)) for row in rows]


def _payload_record(rec: dict, timeline, override) -> dict:
    """One cached record as the object the ingest endpoint accepts.

    Two things are resolved here rather than on the server. The account, from
    the change log, because a session log names none and the server has no copy
    of this machine's. The project name, through this machine's own override
    rules, because the server holds none and treats what arrives as final.
    """
    when = datetime.fromtimestamp(rec["ts"], tz=UTC)
    local = when.astimezone()
    offset = local.utcoffset()
    project = rec["project"]
    if override:
        project = override(rec["repo"], rec["cwd"], project)
    return {
        "mid": rec["mid"],
        "model": rec["model"],
        "ts": rec["ts"],
        "utc_offset": int(offset.total_seconds()) if offset else 0,
        "sid": rec["sid"],
        "project": project,
        "cwd": rec["cwd"],
        "repo": rec["repo"],
        "dk": rec["dk"],
        # Only what the session log itself carried. Everything else the server
        # prices, so a machine that has not pulled cannot write wrong money.
        "cost": rec["cost"],
        "input_tokens": rec["input_tokens"],
        "output_tokens": rec["output_tokens"],
        "cache_create": rec["cache_create"],
        "cache_read": rec["cache_read"],
        "account_uuid": timeline.uuid_at(when) or "unknown",
        "account_label": timeline.label_at(when),
    }


def build_files(
    conn: sqlite3.Connection, pending: list[tuple[str, int, int]], timeline, override,
) -> list[dict]:
    """The file objects a batch carries, whole files only."""
    files = []
    for path, mtime_ns, size in pending:
        files.append({
            "path": path,
            "mtime_ns": mtime_ns,
            "size": size,
            "records": [
                _payload_record(rec, timeline, override) for rec in _records_for(conn, path)
            ],
        })
    return files


def pack_batches(files: list[dict], label: str, max_body: int) -> list[dict]:
    """Split the files into requests that fit *max_body*, never splitting one.

    A file always travels whole, because that is what lets the server delete
    and re-insert it in one transaction. A single file over the limit still
    goes on its own — the server will answer 413 and name the limit, which is
    a report its owner can act on rather than a retry loop.
    """
    batches: list[dict] = []
    current: list[dict] = []
    size = 0
    for item in files:
        weight = len(json.dumps(item))
        if current and size + weight > max_body:
            batches.append({"label": label, "files": current})
            current, size = [], 0
        current.append(item)
        size += weight
    if current:
        batches.append({"label": label, "files": current})
    return batches


def post_batch(server: ServerConfig, batch: dict) -> dict:
    """Send one batch and return the server's verdict.

    Raises:
        PushError: the request failed. A 401 is terminal — a revoked token is
            not a transient failure, and retrying it every interval forever is
            how a revoked laptop keeps knocking for a week.
    """
    body = json.dumps({**batch, "client_version": _client_version()}).encode()
    request = urllib.request.Request(  # noqa: S310
        f"{server.url.rstrip('/')}/v1/ingest",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {server.token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        terminal = exc.code == 401
        reason = "the token was refused" if terminal else f"{exc.code} {exc.reason}"
        raise PushError(f"{server.url}: {reason}", terminal=terminal) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PushError(f"{server.url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PushError(f"{server.url}: the reply was not JSON") from exc


def _client_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ccreport")
    except PackageNotFoundError:
        return "unknown"


def _fingerprints(files: list[dict]) -> dict[str, tuple[int, int]]:
    return {item["path"]: (item["mtime_ns"], item["size"]) for item in files}


def push_to(server: ServerConfig, *, full: bool = False, db_path: Path | None = None) -> PushResult:
    """Send everything *server* has not acknowledged, and record what it stored.

    Raises:
        PushError: nothing was sent. A file the server rejected is reported in
            the result instead, and left out of the watermark so the next run
            offers it again.
    """
    from ccreport import cache_db
    from ccreport.accounts import AccountTimeline
    from ccreport.project_identity import build_override_fn

    override = build_override_fn()
    # A policy change re-points what every past file should have sent, and the
    # files that carried the old names are closed logs that will never change
    # again. Nothing but a full re-push can take a name back off the server.
    policy = policy_hash(server, cache_db.get_project_overrides())
    # A file whose fingerprint has not moved is one the server skips, and after
    # a policy change that is exactly the file whose names have to be replaced.
    # So the re-push says so rather than relying on the fingerprint.
    replace = full or cache_db.read_push_policy(server.url) != policy
    if replace:
        cache_db.clear_push_state(server.url)
    watermark = cache_db.load_push_state(server.url)
    timeline = AccountTimeline(cache_db.load_account_events())

    conn = _read_only(db_path or cache_db.DB_PATH)
    try:
        pending = changed_files(conn, watermark)
        files = build_files(conn, pending, timeline, override)
    finally:
        conn.close()
    for item in files:
        item["records"] = [redact(rec, server) for rec in item["records"]]
        if replace:
            item["replace"] = True

    result = PushResult(server=server.url)
    acknowledged: list[tuple[str, int, int]] = []
    for batch in pack_batches(files, server.label, server.max_body):
        reply = post_batch(server, batch)
        prints = _fingerprints(batch["files"])
        for entry in reply.get("files", ()):
            path = entry["path"]
            status = entry.get("status")
            if status == "rejected":
                result.rejected.append((path, entry.get("detail") or ""))
                continue
            (result.accepted if status == "accepted" else result.skipped).append(path)
            result.records += entry.get("records") or 0
            mtime_ns, size = prints[path]
            acknowledged.append((path, mtime_ns, size))

    # The only writes, and only for what the server said it stored. A rejected
    # file stays unrecorded on purpose, so the next run offers it again.
    cache_db.save_push_state(server.url, acknowledged, time.time())
    cache_db.write_push_policy(server.url, policy)
    return result


def due(last_attempt: float, failures: int, now: float) -> bool:
    """Whether the interval has elapsed, widened by consecutive failures.

    Doubling per failure up to MAX_INTERVAL_S, so a server that has been down
    all night is asked a handful of times rather than every thirty minutes. One
    success resets the count and with it the interval.
    """
    interval = min(BASE_INTERVAL_S * (2 ** max(failures, 0)), MAX_INTERVAL_S)
    return now - last_attempt >= interval


def run_once(*, full: bool = False, only: str | None = None,
             config_path: Path | None = None, force: bool = False) -> list[PushResult]:
    """Push to every configured server that is due, and stamp each attempt.

    *force* skips the interval, which is what the manual command wants and the
    spawn does not.
    """
    from ccreport import cache_db

    results = []
    now = time.time()
    for server in load_config(config_path):
        if only and server.url != only:
            continue
        attempt, failures, stopped = cache_db.read_push_attempt(server.url)
        if stopped and not force:
            continue
        if not force and not due(attempt, failures, now):
            continue
        if not on_allowed_network(server.networks):
            # Blocked: no request, no watermark, so everything queued goes out
            # on the first run back inside the network. The attempt stamp still
            # moves, so a day spent off-network costs one process per interval
            # rather than one per render.
            cache_db.write_push_attempt(server.url, now, 0)
            results.append(PushResult(server=server.url, blocked_by=server.networks))
            continue
        try:
            result = push_to(server, full=full)
        except PushError as exc:
            # Stamped on every outcome, failures included: without it an
            # unreachable server would be probed once per render.
            cache_db.write_push_attempt(
                server.url, now, failures + 1, stopped=exc.terminal,
            )
            results.append(PushResult(server=server.url, rejected=[("", str(exc))]))
            continue
        cache_db.write_push_attempt(server.url, now, 0)
        results.append(result)
    return results


def next_attempt_at(now: float, config_path: Path | None = None) -> float:
    """The soonest any configured server is due again.

    What the status line gates its spawn on, so the render path needs neither
    push.toml nor the widening rule. A server in the terminal state — a revoked
    token — contributes nothing, so a machine whose only server refused it
    stops being spawned for entirely.
    """
    from ccreport import cache_db

    soonest = None
    for server in load_config(config_path):
        attempt, failures, stopped = cache_db.read_push_attempt(server.url)
        if stopped:
            continue
        interval = min(BASE_INTERVAL_S * (2 ** max(failures, 0)), MAX_INTERVAL_S)
        due_at = attempt + interval
        soonest = due_at if soonest is None else min(soonest, due_at)
    # No server, or every one of them stopped: park the spawn a full interval
    # out rather than forever, so re-minting a token is picked up on its own.
    return soonest if soonest is not None else now + MAX_INTERVAL_S


def main(argv: list[str] | None = None) -> int:
    """The detached entry point. Prints nothing; the status line reads no output.

    The next-attempt stamp is written in a finally, so a run that failed in any
    way still moves the gate. Without that, an unreachable server would be
    spawned once per render instead of once per interval.
    """
    from ccreport import cache_db

    argv = sys.argv[1:] if argv is None else argv
    full = "--full" in argv
    try:
        run_once(full=full)
    except Exception:  # noqa: BLE001 - a background push may never take anything down
        return 1
    finally:
        now = time.time()
        try:
            cache_db.write_push_next_attempt(next_attempt_at(now))
        except Exception:  # noqa: BLE001 - a busy database costs the gate, not the run
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
