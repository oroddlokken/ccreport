"""Send this machine's records to a ccreport server.

Runs two ways: `ccreport push`, and a detached spawn from the status line's
slow path on each server's `interval_minutes`, thirty by default. Neither is on
the render path — the status line spawns this the way it spawns usage_api.py and
never imports it.

Nothing happens without ~/.config/ccreport/push.toml, which
`ccreport server connect` writes. A machine that has not opted in pays nothing:
no config, no push, no spawn.

The cache is opened read-only for everything except the watermark, which is one
short write transaction per batch. A render must never wait on this process, and
a long-held lock on cache.db is what would make it.
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
from dataclasses import replace as _replace_fields
from datetime import UTC, datetime
from pathlib import Path

from ccreport import protocol, tier_timeline

CONFIG_PATH = Path.home() / ".config" / "ccreport" / "push.toml"

BASE_INTERVAL_S = 30 * 60
"""How often the status line's spawn is allowed to try, absent an
`interval_minutes` in push.toml. The client cache is already whole; this only
decides how fresh the merged view is."""

MAX_INTERVAL_S = 8 * 60 * 60
"""The ceiling the failure widening reaches. A server down overnight is then
eight attempts short of one per interval, rather than twenty. A configured base
above it is not clamped to it: the ceiling never shortens an interval someone
asked for."""

REQUEST_TIMEOUT_S = 120
"""A first push is a machine's whole history, which the server prices as it
stores. Later pushes are the handful of files that changed."""

_EXCLUDE_MARK = "\x01"
"""What separates the two project lists inside the policy digest. A byte no
project name holds, so a name cannot straddle the boundary and read as a move
between the lists that never happened."""

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
    interval_s: int = BASE_INTERVAL_S
    """How long after an attempt this server is due again, from
    `interval_minutes`. A property of the machine and its link, not of the
    server: a wired desktop keeps the merged view minutes fresh, a metered
    laptop pushes rarely."""
    restricted: bool = False
    """Whether a project has to be opted in by name to be identified at all.

    False is what the personal machines want: everything pushes under its real
    name. True means every project outside *allow* pushes its token counts with
    its identity stripped."""
    allow: tuple[str, ...] = ()
    """Projects that keep their names, already resolved through this machine's
    merge rules so an alias matches the way a report groups it."""
    exclude: tuple[str, ...] = ()
    """Projects that lose their names whatever *restricted* says.

    The other direction from *allow*, and the one an open server needs: name
    every project but these. A machine pushing to its own server wants the
    whole picture except the two repos it cannot show anyone, and listing the
    other hundred to get there is a list that goes stale on the next clone.

    Resolved through the merge rules like *allow*, and stripped the same way,
    so an excluded project lands in the account's aggregated bucket rather
    than a bucket of its own."""
    salt: str = ""
    """What a pseudonym would be hashed against. Generated when restricted is
    first set and never leaves the machine. Nothing derives from it since
    redact() went to nulls; it is kept written so re-introducing a pseudonym
    needs no config migration."""
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
    samples: int = 0
    """Rate-limit utilization samples stored. Counted apart from records: they
    come from a different table and answer a different question, and a run that
    sent nothing but samples has not sent no data."""
    extra: int = 0
    """Extra-usage readings stored, counted apart for the same reason."""
    pulled: int = 0
    """Contributing machines the reply named. Zero on a plain push, which asks
    for no remainder, and on a machine signed in to no account at all."""
    declared: int = 0
    """Plan changes the server declared for this account, written to the local
    change log. Zero where the server declares none, which is not the same as a
    timeline of no entries — see store_tiers."""
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
    """Where "this machine has been restricted, for these servers" is recorded.

    Beside push.toml rather than in cache.db, and read before the file it
    guards: a wiped cache must not be able to unredact a restricted machine,
    and a push.toml that stopped parsing must not either.
    """
    return path.parent / ".restricted"


MARKER_PROSE = (
    "Every server below is one this machine pushes to under a restriction, and\n"
    "a file with no URLs claims all of them. An `exclude <url> <project>` line\n"
    "names one project that server redacts however open it is otherwise.\n"
    "Deleting a line lifts neither: push.toml is what says so, and this only\n"
    "stops a lost setting from reading as permission to send real names.\n"
)
"""The marker's header. A bare URL per restricted server follows it, then an
`exclude <url> <project>` line per excluded project."""

MARKER_EXCLUDE = "exclude "
"""What an exclusion line starts with. The URL and the project name follow it,
space separated, the name running to the end of the line so one holding a space
survives the round trip."""


def _restricted_urls(path: Path) -> frozenset[str] | None:
    """Which servers the marker claims, or None where it claims every one of them.

    A restriction is declared per server, so the guard behind it is recorded
    per server: one URL per line under the prose. None is the marker written
    before the scope existed — it names nothing and means everything, so a
    machine restricted under the old format keeps redacting until something
    narrows it. An absent marker is the empty set, which claims no server.

    So is a marker holding exclusion lines and no bare URL, which is what an
    open server with an `exclude` writes. Nothing predating the scope could
    have written one, so it is a file that names the servers it claims and
    claims none of them — reading it as the whole machine would restrict every
    server the moment one project was hidden on one of them.
    """
    try:
        text = _marker_path(path).read_text()
    except OSError:
        return frozenset()
    lines = [line.strip() for line in text.splitlines()]
    urls = frozenset(line for line in lines if line.startswith(("http://", "https://")))
    if urls or any(line.startswith(MARKER_EXCLUDE) for line in lines):
        return urls
    return None


def _marker_excludes(path: Path) -> dict[str, frozenset[str]]:
    """Which projects the marker redacts, per server.

    Empty for every server the marker names no exclusion for, including one it
    claims a whole restriction over: the two guards answer different questions
    and neither implies the other. There is no unscoped form here — the format
    arrived with the key, so a line always carries its URL.
    """
    try:
        text = _marker_path(path).read_text()
    except OSError:
        return {}
    found: dict[str, set[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(MARKER_EXCLUDE):
            continue
        url, _, project = line[len(MARKER_EXCLUDE):].partition(" ")
        if url and project:
            found.setdefault(url, set()).add(project)
    return {url: frozenset(names) for url, names in found.items()}


def _write_marker(path: Path, entries: dict, url: str, *, replace_exclude: bool = False) -> None:
    """Record what *url* redacts in the marker, keeping every other server's claim.

    An unscoped marker is narrowed here and nowhere else, to every URL push.toml
    declares `restricted = true` for. That reads the file the marker exists to
    distrust, so it takes a connect or an allow someone typed, and the only
    server it can release is one whose own entry claims no restriction.

    *url*'s exclusions are narrowed on the same terms, and *replace_exclude* is
    what says a person typed them: an `exclude` in the fields being written is
    the list as they meant it, so it replaces what the marker held. Anything
    else — a connect, an allow, a network change — carries no opinion about the
    exclusions, and there the marker's names are added to rather than dropped.
    """
    scope = _restricted_urls(path)
    declared = {
        name for name, entry in entries.items()
        if isinstance(entry, dict) and entry.get("restricted")
    }
    claims = {url} if entries.get(url, {}).get("restricted") else set()
    urls = sorted((declared if scope is None else set(scope)) | claims)
    excludes = {one: set(names) for one, names in _marker_excludes(path).items()}
    named = {str(name) for name in (entries.get(url, {}).get("exclude") or ())}
    if replace_exclude:
        excludes[url] = named
    else:
        excludes.setdefault(url, set()).update(named)
    lines = [f"{one}\n" for one in urls]
    lines += [
        f"{MARKER_EXCLUDE}{one} {name}\n"
        for one in sorted(excludes)
        for name in sorted(excludes[one])
    ]
    _marker_path(path).write_text(MARKER_PROSE + "".join(lines))


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
    The marker beside it names the servers this machine has been restricted
    for, and for those it wins: the entry redacts everything rather than
    falling back to real names. An `exclude` lost the same way is restored the
    same way, from the marker's own lines: the union of the two, never the
    entry alone, so a deletion nobody typed cannot name a project.
    """
    path = path or CONFIG_PATH
    marked = _restricted_urls(path)
    marked_exclude = _marker_excludes(path)
    servers = []
    for url, entry in read_raw(path).items():
        if not isinstance(entry, dict) or not entry.get("token"):
            continue
        states_restriction = bool(entry.get("restricted"))
        was_restricted = marked is None or url in marked
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
            interval_s=_interval_seconds(entry.get("interval_minutes")),
            restricted=states_restriction or was_restricted,
            allow=allow,
            exclude=tuple(sorted(
                {str(name) for name in (entry.get("exclude") or ())}
                | set(marked_exclude.get(url, ()))
            )),
            salt=str(entry.get("salt") or ""),
            networks=tuple(str(net) for net in (entry.get("networks") or ())),
        ))
    return servers


def _interval_seconds(value: object) -> int:
    """`interval_minutes` as seconds, or BASE_INTERVAL_S for anything unusable.

    A hand-edited 0600 file is the input, so a typo has to cost the default
    rather than the push: a float, a bool, a word and a zero all read as absent.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return BASE_INTERVAL_S
    try:
        minutes = int(value)
    except ValueError:
        return BASE_INTERVAL_S
    return minutes * 60 if minutes > 0 else BASE_INTERVAL_S


def attempt_interval(failures: int, base: int = BASE_INTERVAL_S) -> int:
    """How long after an attempt the next one is allowed, *failures* in a row.

    Doubling per failure, so a server that has been down all night is asked a
    handful of times rather than every interval. The cap cannot shorten a
    configured base, hence max() rather than MAX_INTERVAL_S alone.
    """
    return min(base * (2 ** max(failures, 0)), max(MAX_INTERVAL_S, base))


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
    _write_entries(path, entries)
    guarded = entries[url].get("restricted") or entries[url].get("exclude")
    if guarded or url in _marker_excludes(path):
        _write_marker(path, entries, url, replace_exclude="exclude" in fields)


def remove_server(path: Path, url: str) -> bool:
    """Take one server's table out of push.toml. True if there was one.

    Every other entry is rewritten as it stood, for the reason write_server
    merges rather than replaces: a config you can only rewrite wholesale is one
    people edit by hand and get wrong.

    The `.restricted` marker is deliberately left alone. Disconnecting is not
    a decision about what this machine may name, and taking the URL out here is
    how a later reconnect to the same server would read as open.
    """
    entries = read_raw(path)
    if url not in entries:
        return False
    del entries[url]
    _write_entries(path, entries)
    return True


_MAX_LINE = 110
"""Where an array stops fitting on one line, the project's own line length."""


def _write_entries(path: Path, entries: dict) -> None:
    """Write the whole `[server."URL"]` set at mode 0600, because it holds tokens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for server_url, entry in entries.items():
        lines.append(f'[server."{server_url}"]')
        lines += [_toml_line(key, value) for key, value in entry.items()]
        lines.append("")
    path.write_text("\n".join(lines))
    path.chmod(0o600)


def _toml_line(key: str, value) -> str:
    """`key = value`, stacking an array whose flat form runs past _MAX_LINE.

    A long `allow` list is otherwise a 140-character line no editor wraps. The
    file is hand-edited, so re-running connect leaves the stacked shape as it
    stands.
    """
    flat = f"{key} = {_toml_value(value)}"
    if not isinstance(value, (list, tuple)) or len(flat) <= _MAX_LINE:
        return flat
    stacked = "".join(f"    {json.dumps(str(item))},\n" for item in value)
    return f"{key} = [\n{stacked}]"


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

    No caller: redact() nulls the project instead, because a row per pseudonym
    told the server how many private projects there are. Kept for the day a
    grouping key is worth that again.
    """
    return hashlib.sha256(f"{salt}\x00{name}".encode()).hexdigest()[:8]


def pseudo_session(salt: str, session_id: str) -> str:
    """The same, for a session id, and with no caller for the same reason.

    Longer than a project pseudonym: a machine has tens of projects and tens of
    thousands of sessions, and the session report is only useful while they
    stay distinct.
    """
    return hashlib.sha256(f"{salt}\x00session\x00{session_id}".encode()).hexdigest()[:16]


REDACTION_SHAPE = "null-identity"
"""What redact() leaves of an unallowed record, as a value policy_hash moves on.

The salt no longer changes when the redaction does — nothing derives from it
since the pseudonyms went — so this string is the only thing that can force the
re-push a shape change needs. Change what redact() strips, change this.
"""


def redact(rec: dict, server: ServerConfig) -> dict:
    """Strip a record's identity unless its project is named on this server.

    Two lists decide that, and they compose rather than take precedence over
    each other: *exclude* strips whatever else is true, and *allow* is what
    survives a restriction. A project in both is stripped, which is the only
    reading that keeps `exclude` meaning what it says.

    What survives is everything the money is made of: model, timestamps and
    token counts. What goes is project, cwd, repo and session id, all four to
    nothing at all.

    A pseudonym per project would let the server draw a row each, and a row per
    private project is the count and the shape of the work — which is the thing
    being kept back. The session goes with it for the same reason: a session
    count per bucket says how much hidden work there was. All of it lands in one
    bucket per account instead, which the server names.
    """
    project = rec["project"]
    if project not in server.exclude and (not server.restricted or project in server.allow):
        return rec
    return {**rec, "project": None, "cwd": None, "repo": None, "sid": None}


def policy_hash(server: ServerConfig, override_rules: object = "") -> str:
    """What the redaction depends on, as one key beside the watermark.

    A project moved out of *allow* has to stop being named on the server, and
    the files that named it were pushed long ago. So a change here forces a
    full re-push rather than waiting for those files to change, which they
    never will — a session log that has been closed is closed for good.

    The local merge rules are in it for the same reason: they decide which name
    `allow` is matched against, so editing one re-points the whole policy.

    REDACTION_SHAPE covers the third: what redact() leaves behind. A code edit
    moves nothing else here, so without it the rows a previous shape wrote would
    stand on the server until their files changed, which they never will.

    *exclude* is in it on the same terms as *allow*, behind a separator rather
    than concatenated: a project moved from one list to the other changes what
    is sent, and a digest that could not tell the lists apart would call that
    no change at all. An empty *exclude* contributes nothing, separator
    included, so the key arriving did not move any existing machine's digest
    and cost every one of them a corpus it had already pushed.
    """
    parts = ["1" if server.restricted else "0", *sorted(server.allow)]
    if server.exclude:
        parts += [_EXCLUDE_MARK, *sorted(server.exclude)]
    parts += [server.salt, REDACTION_SHAPE, repr(override_rules)]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


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

    Archived files are left out. Their records folded into ccreport_archive and
    the rows themselves are gone, so offering one would send a file with no
    records — and the server keys a file on (machine, path) and replaces what it
    holds, so a `--full` would empty out history it is the only copy of.
    """
    rows = conn.execute(
        "SELECT path, mtime_ns, size FROM ccreport_files "
        "WHERE archived = 0 ORDER BY path"
    ).fetchall()
    return [
        (path, mtime_ns, size)
        for path, mtime_ns, size in rows
        if watermark.get(path) != (mtime_ns, size)
    ]


def files_naming(conn: sqlite3.Connection, projects: set[str], override) -> set[str]:
    """Unarchived cached files holding a record that resolves to one of *projects*.

    The resolved name, not the stored column: a merge rule can point a record
    at a project it was never logged under, and that is the name `allow` is
    matched against. DISTINCT because the question is per file and a file holds
    thousands of records that answer it the same way.
    """
    rows = conn.execute(
        "SELECT DISTINCT f.path, r.project, r.repo, r.cwd FROM ccreport_records r "
        "JOIN ccreport_files f ON f.id = r.file_id WHERE f.archived = 0"
    )
    hit: set[str] = set()
    for path, project, repo, cwd in rows:
        if path in hit:
            continue
        if (override(repo, cwd, project) if override else project) in projects:
            hit.add(path)
    return hit


@dataclass(frozen=True)
class Repush:
    """What a policy change costs this run: nothing, some files, or all of them."""

    needed: bool = False
    paths: frozenset[str] | None = None
    """The files to re-offer, or None for every one of them."""


def repush_scope(
    conn: sqlite3.Connection, server: ServerConfig, policy: str, overrides, override,
    *, full: bool = False,
) -> Repush:
    """Which files the stored policy no longer covers.

    A digest cannot say which of its six inputs moved, so the two project lists
    are stored beside it. Substituting them back into the hash is what tells
    them from the rest: reproduce the stored digest and `allow`, `exclude` or
    both were the only difference, which changes the bytes of the files naming
    a project that entered or left either list and of nothing else. Any other
    input — restricted, the salt, the redaction shape, the merge rules —
    re-points every record, and there the answer is still the whole corpus.

    Both lists are substituted together rather than one at a time. A project
    that moved from `allow` to `exclude` moved in two lists at once, and
    holding either at its stored value would leave a digest that matches
    neither and read the whole corpus as changed.

    An unstored `exclude` beside a stored `allow` is the empty list, not an
    unknown: the key arrived after the watermark did, and before it every
    server excluded nothing. Reading it as unknown would charge the first
    exclusion a whole corpus that nothing in it needs.
    """
    from ccreport import cache_db

    stored = cache_db.read_push_policy(server.url)
    if not full and stored == policy:
        return Repush()
    stored_allow = cache_db.read_push_allow(server.url)
    stored_exclude = cache_db.read_push_exclude(server.url) or ()
    if full or stored_allow is None:
        return Repush(needed=True)
    was = _replace_fields(server, allow=tuple(stored_allow), exclude=tuple(stored_exclude))
    if policy_hash(was, overrides) != stored:
        return Repush(needed=True)
    moved = (set(stored_allow) ^ set(server.allow)) | (set(stored_exclude) ^ set(server.exclude))
    return Repush(needed=True, paths=frozenset(files_naming(conn, moved, override)))


def _records_for(conn: sqlite3.Connection, path: str) -> list[dict]:
    """One file's cached records, as the rows they were stored as."""
    from ccreport.cache_db import _CCR_COLS

    rows = conn.execute(
        f"SELECT {', '.join('r.' + c for c in _CCR_COLS)} FROM ccreport_records r "  # noqa: S608
        "JOIN ccreport_files f ON f.id = r.file_id WHERE f.path = ? ORDER BY r.id",
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


SAMPLES_PER_BATCH = 2000
"""Rate-limit samples per request. Each is under a hundred bytes, so this is
well inside the smallest configured body limit and still one request for a year
of a quiet machine's history."""


def build_samples(conn: sqlite3.Connection, timeline, since: float) -> list[dict]:
    """The utilization samples newer than the watermark, as the ingest accepts.

    The account is resolved here, the way a record's is: a sample names none —
    it is attributed by its ts against this machine's account log — and the
    server holds no copy of that log to attribute it with.

    Nothing is redacted. A sample carries a window name, a percentage, a reset
    time and a model, and none of those is a project or a session, so a
    restricted machine sends the same rows an open one does. The quota is the
    account's, and how full it got is what the merged page is for.
    """
    from ccreport.cache_db import _RL_SNAPSHOT_COLS

    cols = ", ".join(_RL_SNAPSHOT_COLS)
    rows = conn.execute(
        f"SELECT {cols} FROM rate_limit_snapshots WHERE ts > ? ORDER BY ts, window",  # noqa: S608
        (since,),
    ).fetchall()
    samples = []
    for row in rows:
        sample = dict(zip(_RL_SNAPSHOT_COLS, row, strict=True))
        when = datetime.fromtimestamp(sample["ts"], tz=UTC)
        samples.append({
            **sample,
            "account_uuid": timeline.uuid_at(when) or "unknown",
            "account_label": timeline.label_at(when),
        })
    return samples


def build_extra(conn: sqlite3.Connection, timeline, since: float) -> list[dict]:
    """The Extra-usage readings newer than the watermark, as the ingest accepts.

    The account is resolved here, as a sample's is. Nothing is redacted: a
    reading is an instant and a dollar figure, neither of which names a project
    or a session.

    The client prunes this table to 31 days on every usage-cache write, so a
    machine that has not pushed in a month has already lost what it never sent.
    The server keeps what does arrive for good, which is what lets `/limits`
    price a window `ccreport limits` can no longer reach.
    """
    rows = conn.execute(
        "SELECT ts, spent FROM extra_usage_snapshots WHERE ts > ? ORDER BY ts",
        (since,),
    ).fetchall()
    readings = []
    for ts, spent in rows:
        when = datetime.fromtimestamp(ts, tz=UTC)
        readings.append({
            "ts": ts,
            "spent": spent,
            "account_uuid": timeline.uuid_at(when) or "unknown",
            "account_label": timeline.label_at(when),
        })
    return readings


def pack_samples(samples: list[dict], extra: list[dict], label: str) -> list[dict]:
    """The side-channel requests a batch of samples and readings travels in.

    Their own requests rather than a field on the file batches: a machine whose
    logs have not changed still has new samples to send, and there are no file
    batches on that run to attach them to.

    The two series are zipped into the same requests rather than sent as two
    runs of them. They are the same size and the same shape, and a run that
    carried both would otherwise open twice as many connections to say so.
    """
    chunks = max(
        _chunk_count(samples), _chunk_count(extra),
    )
    return [
        {
            "label": label,
            "files": [],
            "samples": samples[at * SAMPLES_PER_BATCH:(at + 1) * SAMPLES_PER_BATCH],
            "extra": extra[at * SAMPLES_PER_BATCH:(at + 1) * SAMPLES_PER_BATCH],
        }
        for at in range(chunks)
    ]


def _chunk_count(items: list[dict]) -> int:
    """How many SAMPLES_PER_BATCH requests *items* needs."""
    return -(-len(items) // SAMPLES_PER_BATCH)


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
        PushError: the request failed. A 401 and a 409 are terminal — a revoked
            token and a server too old to read this build are both settled until
            somebody acts, and retrying either every interval forever is how a
            revoked laptop keeps knocking for a week.
    """
    body = json.dumps({
        **batch,
        "protocol": protocol.PROTOCOL_VERSION,
        "client_version": _client_version(),
    }).encode()
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
            reply = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        terminal = exc.code in (401, 409)
        reason = _refusal(exc)
        raise PushError(f"{server.url}: {reason}", terminal=terminal) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PushError(f"{server.url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PushError(f"{server.url}: the reply was not JSON") from exc
    # After the transport, because a server old enough to be behind is also old
    # enough to answer 200 to a batch it only half read. This is the only thing
    # that catches one that predates the protocol field entirely.
    theirs = reply.get("protocol", protocol.PRE_VERSIONING)
    if theirs < protocol.PROTOCOL_VERSION:
        raise PushError(f"{server.url}: {protocol.describe(theirs)}", terminal=True)
    return reply


def _refusal(exc: urllib.error.HTTPError) -> str:
    """What a refused request is reported as, in the words the person needs.

    A 409 carries the server's own sentence about the two protocol versions;
    printing "409 Conflict" over it would throw away the only part that says
    what to do.
    """
    if exc.code == 401:
        return "the token was refused"
    if exc.code == 409:
        try:
            detail = json.loads(exc.read()).get("detail")
        except Exception:  # noqa: BLE001 - the status line is the answer either way
            detail = None
        return detail or "the server refused this build's protocol version"
    return f"{exc.code} {exc.reason}"


def _client_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ccreport")
    except PackageNotFoundError:
        return "unknown"


def _fingerprints(files: list[dict]) -> dict[str, tuple[int, int]]:
    return {item["path"]: (item["mtime_ns"], item["size"]) for item in files}


def _account_uuid_now() -> str | None:
    """The account this machine is signed in to right now, or None.

    What a pull scopes to, both when it asks and when the report reads back:
    a login switch must not add a previous account's spend to this one's
    windows.
    """
    from ccreport import cache_db
    from ccreport.accounts import AccountTimeline

    timeline = AccountTimeline(cache_db.load_account_events())
    return timeline.uuid_at(datetime.now(tz=UTC))


def store_pull(server_url: str, reply: dict, now: float) -> int:
    """Store one pull reply's remainder. Returns the machines it named.

    The window totals are summed here rather than on the server: the reply
    carries per-minute cost buckets, and which rolling windows those answer is
    pricing.ROLLING_WINDOWS, the list every other window key on this machine is
    derived from. Summing server-side would have frozen that list into the
    protocol.

    all_time is summed off the day rows rather than off the buckets, which the
    server bounds to the longest rolling window — the daily table is the one
    that keeps every day, and it is exact over all of them.
    """
    from ccreport import cache_db, pricing

    remainder = reply.get("pull") or {}
    account_uuid = remainder.get("account_uuid")
    if not account_uuid:
        return 0
    machines = remainder.get("machines") or []
    windows: list[tuple[str, str, str, float, float]] = []
    days: list[tuple] = []
    for machine in machines:
        machine_id = machine["machine_id"]
        label = machine.get("label") or machine_id
        pushed_at = float(machine.get("last_seen") or 0.0)
        buckets = [(float(ts), float(cost)) for ts, cost in machine.get("buckets", ())]
        for window in pricing.ROLLING_WINDOWS:
            start = now - window.delta.total_seconds()
            windows.append((
                machine_id, label, window.name,
                sum(cost for ts, cost in buckets if ts >= start), pushed_at,
            ))
        all_time = 0.0
        for row in machine.get("days", ()):
            day, project, cost, *counts = row
            all_time += float(cost)
            days.append((
                machine_id, day, project, float(cost), *(int(v) for v in counts),
                pushed_at,
            ))
        windows.append((machine_id, label, "all_time", all_time, pushed_at))
    cache_db.save_remote_costs(server_url, account_uuid, windows, days, now)
    return len(machines)


def store_tiers(reply: dict) -> int:
    """Write the server's declared timeline for this account. Returns the rows.

    The server is the one source: the timeline has to be typed there for the
    dashboard to price a month, and it reaches records whose logs have rotated
    off every machine. So its document replaces this account's backfilled rows
    outright, and a `ccreport tiers` run against an account the server declares
    is undone by the next pull.

    A reply that declares nothing writes nothing, rather than clearing what is
    here. An empty section is a server nobody has typed a timeline into, and
    reading it as "the timeline is empty" would delete the declaration on the
    one machine whose person did type one.

    Identity is copied from this machine's own capture log, as `ccreport tiers`
    copies it: a plan change says nothing about who the account is, and a row
    that introduced an identity could introduce a typo of one.
    """
    from ccreport import cache_db

    remainder = reply.get("pull") or {}
    account_uuid = remainder.get("account_uuid")
    entries = remainder.get("tiers") or []
    if not account_uuid or not entries:
        return 0
    identity = _identity_of(account_uuid)
    if identity is None:
        return 0
    return cache_db.replace_backfilled_account(account_uuid, [
        {
            "ts": float(entry["ts"]),
            **identity,
            **{field: entry.get(field) for field in tier_timeline.TIER_FIELDS},
        }
        for entry in entries
    ])


def _identity_of(account_uuid: str) -> dict | None:
    """The identity columns this machine's log carries for *account_uuid*.

    The newest event naming it, so a person whose login email changed is
    written under the address they use now. None where the log has never seen
    the account, which a pull cannot reach — it asks about the account this
    machine is signed in to — and which is left as a no-op rather than as a row
    naming nobody.
    """
    from ccreport.cache_db import _ACCOUNT_IDENTITY_COLS, load_account_events

    for event in reversed(load_account_events()):
        if event["account_uuid"] == account_uuid:
            return {col: event[col] for col in _ACCOUNT_IDENTITY_COLS}
    return None


def pull_from(server: ServerConfig) -> PushResult:
    """Ask *server* for the spend this machine does not have, and store it.

    An empty batch with a pull attached, so it goes through the token-authed
    ingest endpoint and works from wherever the laptop is. `ccreport server
    sync` is a push followed by one of these, and both spellings exist so each
    half is testable on its own.
    """
    result = PushResult(server=server.url)
    account_uuid = _account_uuid_now()
    if not account_uuid:
        return result
    now = time.time()
    reply = post_batch(server, {
        "label": server.label, "files": [], "samples": [], "extra": [],
        "pull": {"account_uuid": account_uuid},
    })
    result.pulled = store_pull(server.url, reply, now)
    result.declared = store_tiers(reply)
    return result


def push_to(server: ServerConfig, *, full: bool = False, db_path: Path | None = None,
            pull: bool = False) -> PushResult:
    """Send everything *server* has not acknowledged, and record what it stored.

    *pull* attaches the remainder request to the last batch, so a sync costs one
    round trip rather than two and the reply is computed after this run's own
    files are stored.

    The watermark moves after each batch, so a run that dies partway through
    resumes rather than starting over. That matters most on a re-push wide
    enough to need many requests, where an end-of-run write would let one
    failure cost every batch before it.

    Raises:
        PushError: a batch was refused. Whatever the earlier batches stored is
            recorded, and a file the server rejected is reported in the result
            instead, left out of the watermark so the next run offers it again.
    """
    from ccreport import cache_db
    from ccreport.accounts import AccountTimeline
    from ccreport.project_identity import build_override_fn

    override = build_override_fn()
    overrides = cache_db.get_project_overrides()
    # A policy change re-points what past files should have sent, and the files
    # that carried the old names are closed logs that will never change again.
    # Nothing but a re-push can take a name back off the server.
    policy = policy_hash(server, overrides)
    conn = _read_only(db_path or cache_db.DB_PATH)
    try:
        repush = repush_scope(conn, server, policy, overrides, override, full=full)
        # A file whose fingerprint has not moved is one the server skips, and
        # after a policy change that is exactly the file whose names have to be
        # replaced. So the re-push says so rather than relying on the
        # fingerprint.
        #
        # The clear happens once and the stamping goes on until the last batch
        # is acknowledged. Those are separate because the watermark moves per
        # batch: a resumed run that cleared again would throw away the batches
        # the interrupted one already got stored, which is the point of
        # resuming.
        if repush.needed:
            if repush.paths is None:
                cache_db.clear_push_state(server.url)
            else:
                cache_db.clear_push_state_for(server.url, repush.paths)
            cache_db.write_push_policy(server.url, policy)
        # Recorded whether or not anything moved: without them the next `allow`
        # or `exclude` edit has nothing to diff against and goes wide.
        cache_db.write_push_allow(server.url, server.allow)
        cache_db.write_push_exclude(server.url, server.exclude)
        replace = repush.needed or cache_db.read_push_replacing(server.url)
        if replace:
            cache_db.write_push_replacing(server.url, True)
        watermark = cache_db.load_push_state(server.url)
        timeline = AccountTimeline(cache_db.load_account_events())

        # Cleared with the file watermark by --full and by a change no scoping
        # covers, so a repaired server is offered the whole history of all three
        # tables.
        samples_at = cache_db.read_push_samples_at(server.url)
        extra_at = cache_db.read_push_extra_at(server.url)
        pending = changed_files(conn, watermark)
        files = build_files(conn, pending, timeline, override)
        samples = build_samples(conn, timeline, samples_at)
        extra = build_extra(conn, timeline, extra_at)
    finally:
        conn.close()
    for item in files:
        item["records"] = [redact(rec, server) for rec in item["records"]]
        if replace:
            item["replace"] = True

    result = PushResult(server=server.url)
    sent_samples_at = samples_at
    sent_extra_at = extra_at
    batches = pack_batches(files, server.label, server.max_body) + pack_samples(
        samples, extra, server.label,
    )
    account_uuid = _account_uuid_now() if pull else None
    if account_uuid:
        # On the last batch, and one of its own where there is none: the
        # remainder is computed after the files in the same request are stored,
        # so a sync reads a server that already holds what it just sent.
        if not batches:
            batches = [{"label": server.label, "files": []}]
        batches[-1]["pull"] = {"account_uuid": account_uuid}
    pulled_at = time.time()
    for batch in batches:
        reply = post_batch(server, batch)
        result.samples += reply.get("samples") or 0
        result.extra += reply.get("extra") or 0
        for reading in batch.get("extra", ()):
            sent_extra_at = max(sent_extra_at, reading["ts"])
        for sample in batch.get("samples", ()):
            sent_samples_at = max(sent_samples_at, sample["ts"])
        prints = _fingerprints(batch["files"])
        acknowledged: list[tuple[str, int, int]] = []
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
        if reply.get("pull"):
            result.pulled = store_pull(server.url, reply, pulled_at)
            result.declared = store_tiers(reply)
        # Per batch, and only for what the server said it stored. A rejected
        # file stays unrecorded on purpose, so the next run offers it again,
        # and a batch that raises leaves every earlier batch recorded rather
        # than sending the whole corpus a second time. Re-offering a stored
        # sample is a no-op: the server keys them on (machine, window, ts).
        cache_db.save_push_state(server.url, acknowledged, time.time())
        cache_db.write_push_samples_at(server.url, sent_samples_at)
        cache_db.write_push_extra_at(server.url, sent_extra_at)

    if replace:
        # Last, because it is what says the re-push has no batches left to stamp.
        cache_db.write_push_replacing(server.url, False)
    return result


def due(last_attempt: float, failures: int, now: float,
        base: int = BASE_INTERVAL_S) -> bool:
    """Whether this server's interval has elapsed, widened by its failures.

    *base* is the server's own `interval_s`. One success resets the failure
    count and with it the interval.
    """
    return now - last_attempt >= attempt_interval(failures, base)


def refresh_cache() -> None:
    """Parse what has changed on disk into the cache this run sends from.

    Only a parse writes ccreport_files, and until this ran only the CLI did
    one. A machine whose reports nobody opens would send the corpus as it stood
    when someone last typed `ccreport`, while every attempt reported success —
    the running session's own records were not in the table to offer.

    A database another process holds costs this run its fresh records, not its
    push: what is already cached still goes out.
    """
    from ccreport import scan

    try:
        scan.refresh_cache()
    except sqlite3.Error:
        pass


def run_once(*, full: bool = False, only: str | None = None,
             config_path: Path | None = None, force: bool = False,
             pull: bool = True) -> list[PushResult]:
    """Push to every configured server that is due, and stamp each attempt.

    *force* skips the interval, which is what the manual command wants and the
    spawn does not. The cache is refreshed once a server has cleared every
    gate, so a run that sends nothing parses nothing either.

    *pull* asks each server for the spend this machine does not have and stores
    it, on the same request that carried the push. Off for `ccreport server
    push`, which is the half that only sends.
    """
    from ccreport import cache_db

    results = []
    refreshed = False
    now = time.time()
    for server in load_config(config_path):
        if only and server.url != only:
            continue
        attempt, failures, stopped = cache_db.read_push_attempt(server.url)
        if stopped and not force:
            continue
        if not force and not due(attempt, failures, now, server.interval_s):
            continue
        if not on_allowed_network(server.networks):
            # Blocked: no request, no watermark, so everything queued goes out
            # on the first run back inside the network. The attempt stamp still
            # moves, so a day spent off-network costs one process per interval
            # rather than one per render.
            cache_db.write_push_attempt(server.url, now, 0)
            results.append(PushResult(server=server.url, blocked_by=server.networks))
            continue
        if not refreshed:
            refresh_cache()
            refreshed = True
        try:
            result = push_to(server, full=full, pull=pull)
        except PushError as exc:
            # Stamped on every outcome, failures included: without it an
            # unreachable server would be probed once per render.
            cache_db.write_push_attempt(
                server.url, now, failures + 1, stopped=exc.terminal, reason=str(exc),
            )
            results.append(PushResult(server=server.url, rejected=[("", str(exc))]))
            continue
        cache_db.write_push_attempt(server.url, now, 0, succeeded=True)
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
        due_at = attempt + attempt_interval(failures, server.interval_s)
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
