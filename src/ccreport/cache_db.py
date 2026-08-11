"""Unified SQLite cache for Claude Code usage, costs, and reporting.

Single database at ~/.cache/ccreport/cache.db.

Consumers:
  - usage_api.py   (usage data + cost cache)
  - statusline.py  (usage read + session stats/costs)
  - ccreport.py    (file-level record cache)
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from typing import Any, NamedTuple

# pricing.py imports cache_db only inside functions, so this direction is safe.
from ccreport.pricing import project_key, rolling_cost_keys

_CACHE_DIR = Path.home() / ".cache" / "ccreport"
DB_PATH = _CACHE_DIR / "cache.db"

# Snapshots live outside ~/.cache so aggressive cache cleanup can't take out
# the live DB and all its backups in one sweep.
_DEFAULT_SNAPSHOT_DIR = Path.home() / ".local" / "share" / "ccreport" / "snapshots"
_SNAPSHOT_KEEP_DEFAULT = 14

# Where the cache and its snapshots lived while this tooling was a directory in
# the macsetup repo. relocate_legacy_paths() below moves them the first time a
# process opens a DB that isn't there yet; `ccreport migrate` calls the same
# function to do it on demand and report what it found.
_LEGACY_CACHE_DIR = Path.home() / ".cache" / "macsetup" / "claude"
_LEGACY_SNAPSHOT_DIR = Path.home() / ".local" / "share" / "macsetup" / "claude" / "snapshots"

_conn: sqlite3.Connection | None = None

# Stamped into the DB's PRAGMA user_version once the bootstrap below has run to
# completion; get_connection skips the entire bootstrap when the two match.
#
# BUMP THIS on any change to _SCHEMA_SQL, _ADDED_COLUMNS, or the migration list
# in _run_migrations — an existing DB is otherwise never reopened on the slow
# path and never sees the new DDL. A needless bump costs one slow open per DB.
SCHEMA_VERSION = 9

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS usage (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    session_percent       INTEGER,
    session_reset         TEXT,
    week_percent          INTEGER,
    week_reset            TEXT,
    sonnet_percent        INTEGER,
    sonnet_reset          TEXT,
    scoped_percent        INTEGER,
    scoped_model          TEXT,
    scoped_reset          TEXT,
    extra_percent         INTEGER,
    extra_spent           REAL,
    extra_limit           REAL,
    last_updated          TEXT,
    session_cost          REAL,
    session_window_cost   REAL,
    week_cost             REAL,
    month_cost            REAL,
    six_hour_cost         REAL,
    twelve_hour_cost      REAL,
    twenty_four_hour_cost REAL,
    seven_day_cost        REAL,
    thirty_day_cost       REAL,
    all_time_cost         REAL,
    six_hour_project_cost         REAL,
    twelve_hour_project_cost      REAL,
    twenty_four_hour_project_cost REAL,
    seven_day_project_cost        REAL,
    thirty_day_project_cost       REAL,
    all_time_project_cost         REAL,
    meta_json             TEXT
);

-- week_model_json holds the week total split by model family as a JSON object,
-- the one bucket a per-model weekly quota is spent against. A column rather
-- than a table: it is a handful of keys per file, read and written whole.
CREATE TABLE IF NOT EXISTS file_costs (
    path            TEXT PRIMARY KEY,
    mtime_ns        INTEGER NOT NULL,
    size            INTEGER NOT NULL,
    week_cost       REAL NOT NULL DEFAULT 0,
    month_cost      REAL NOT NULL DEFAULT 0,
    all_time_cost   REAL NOT NULL DEFAULT 0,
    session_cost    REAL,
    week_model_json TEXT
) WITHOUT ROWID;

-- file_path leads the key so the ON DELETE CASCADE and the per-file rewrite in
-- bulk_save_file_costs are PK range scans. Keyed the other way round this table
-- needed a secondary index on file_path, and a secondary index on a WITHOUT
-- ROWID table stores the indexed columns plus the whole PK — i.e. a second copy
-- of the table, on the highest-churn table there is.
CREATE TABLE IF NOT EXISTS dedup_keys (
    dk        TEXT NOT NULL,
    file_path TEXT NOT NULL REFERENCES file_costs(path) ON DELETE CASCADE,
    PRIMARY KEY (file_path, dk)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cache_stats (
    session_id       TEXT PRIMARY KEY,
    total_in_tokens  INTEGER NOT NULL,
    cum_fresh        INTEGER NOT NULL,
    cum_cache_create INTEGER NOT NULL,
    cum_cache_read   INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS session_costs (
    session_id  TEXT PRIMARY KEY,
    fingerprint INTEGER NOT NULL,
    cost        REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ccreport_files (
    path     TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ccreport_records (
    id            INTEGER PRIMARY KEY,
    file_path     TEXT NOT NULL REFERENCES ccreport_files(path) ON DELETE CASCADE,
    mid           TEXT,
    model         TEXT NOT NULL,
    ts            REAL NOT NULL,
    sid           TEXT NOT NULL,
    project       TEXT NOT NULL,
    cwd           TEXT,
    repo          TEXT,
    dk            TEXT,
    cost          REAL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_create  INTEGER NOT NULL,
    cache_read    INTEGER NOT NULL
);

-- file_path leads because every scoped read starts from a path: the cascade
-- from ccreport_files, the per-file fetch, and the prefix range the statusline
-- uses to pull one project's records. ts trails it so a cutoff can ride along
-- inside a file scope. There is deliberately no standalone index on ts — no
-- statement filters ts without also bounding file_path.
CREATE INDEX IF NOT EXISTS idx_ccr_file_ts ON ccreport_records(file_path, ts);
CREATE INDEX IF NOT EXISTS idx_ccr_sid ON ccreport_records(sid);

-- Per-day aggregates of the records ccreport has already read, for the days
-- old enough that nothing can still change them. A bare report deserializes
-- ~95k record rows to fold them into a handful of tables; the days past the
-- rollup cutoff fold to a few thousand rows here instead.
--
-- The key is the finest grain any report needs: session for the session table,
-- project/model/account for theirs, day for the daily and monthly ones, and
-- oslo_date because the NOK rate is per Oslo date and a local day can straddle
-- two of them. cost is frozen at build time — it is the sum of what each
-- record's cost() answered, log-provided or computed — so pricing.py is hashed
-- into the fingerprint, which the record cache deliberately does not do.
--
-- Whether these rows still describe the corpus is one meta row,
-- ccreport_rollup_fp; rows and fingerprint are written in one transaction, and
-- ccreport rebuilds the lot on any mismatch. Nothing here is irreplaceable:
-- every row is derivable from ccreport_records.
CREATE TABLE IF NOT EXISTS ccreport_rollups (
    day           TEXT NOT NULL,   -- local YYYY-MM-DD, what the reports bucket by
    oslo_date     TEXT NOT NULL,   -- ISO date the NOK rate is looked up under
    sid           TEXT NOT NULL,
    project       TEXT NOT NULL,
    model         TEXT NOT NULL,
    account       TEXT NOT NULL,
    min_ts        REAL NOT NULL,
    max_ts        REAL NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_create  INTEGER NOT NULL,
    cache_read    INTEGER NOT NULL,
    cost          REAL NOT NULL,
    n             INTEGER NOT NULL,
    PRIMARY KEY (day, oslo_date, sid, project, model, account)
) WITHOUT ROWID;

-- The all-time cost of every record whose source JSONL is gone, pre-summed.
-- Orphaned records are 83% of ccreport_records on a real machine and none of
-- them can ever change: the log they were parsed from is deleted, so nothing
-- re-parses them. compute_costs still had to walk all of them on every render
-- because all_time has no window to bound it by.
--
-- The grain is the coarsest one that still answers "is this the cwd's own
-- project": the directory prefix the file sat under, which is what
-- path_in_project tests, plus the (project, cwd, repo) identity every record
-- in a file shares, which is what record_project resolves. Both tests then
-- run over a few hundred rows instead of ~86k. The override rules are
-- deliberately NOT baked in — the identity is stored raw and resolved at read
-- time, so a `ccreport merge` re-groups these totals with no rebuild.
--
-- Valid only against ccreport_orphan_fp, written in the same transaction; see
-- pricing._orphan_alltime_fingerprint for what that covers.
CREATE TABLE IF NOT EXISTS ccreport_orphan_costs (
    dir_prefix TEXT NOT NULL,   -- '<projects dir>/<dir>/', '' if outside one
    project    TEXT NOT NULL,
    cwd        TEXT NOT NULL,   -- '' rather than NULL: part of the key
    repo       TEXT NOT NULL,   -- ''  ""
    cost       REAL NOT NULL,
    PRIMARY KEY (dir_prefix, project, cwd, repo)
) WITHOUT ROWID;

-- Manual project-grouping rules, applied as a pure function over the signals
-- stored on each record (name/remote/cwd) at report time. Local data, never
-- committed: merges and renames live here, not in code.
CREATE TABLE IF NOT EXISTS project_overrides (
    id          INTEGER PRIMARY KEY,
    match_kind  TEXT NOT NULL,   -- 'name' | 'remote' | 'cwd_prefix'
    match_value TEXT NOT NULL,
    target      TEXT NOT NULL,
    UNIQUE (match_kind, match_value)
);

-- The project scope a render resolves for a cwd: the merge target's name, and
-- every project directory whose records resolve to that same target. Deriving
-- it needs a GROUP BY over every cached record, 0.020s of an 0.085s statusline
-- call, and the answer is a pure function of project_overrides and those
-- records. So a present row is valid by construction rather than by a
-- fingerprint: every writer of either input — both override writers,
-- save_ccreport_files, invalidate_ccreport — clears what it can have moved in
-- the same transaction, and readers gate on the ccreport salt so a stale row
-- format degrades the cached scope exactly as it degrades a freshly derived
-- one. A rule change and an invalidation empty the table; a record save empties
-- it only when it actually changes a file's identity, since re-parsing a
-- session log that grew rewrites the identity it already had
-- (_save_invalidates_scopes).
--
-- Not airtight, deliberately: a scope derived just before a ccreport write and
-- stored just after survives until the next write, costing one merged directory
-- in the cost windows.
CREATE TABLE IF NOT EXISTS project_scopes (
    cwd      TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    prefixes TEXT NOT NULL   -- JSON array of path prefixes
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS extra_usage_snapshots (
    ts    REAL PRIMARY KEY,
    spent REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS exchange_rates (
    date  TEXT PRIMARY KEY,
    rate  REAL NOT NULL
) WITHOUT ROWID;

-- Append-only log of which Claude account was signed in, and from when. A
-- session JSONL carries no account field and ~/.claude.json holds only the
-- current login, so this timeline is the only thing that can attribute a
-- historic record to an account. A row is written when the account changes
-- and never otherwise, so this stays a handful of rows for a machine's life.
-- ts leads as the primary key: both readers want it ordered.
--
-- Two kinds of field, and the difference matters to the readers. The first four
-- are the identity: accountUuid is the stable key, emailAddress the label a
-- report shows, and the organization pair is what separates the same address
-- billing through work from the same address billing personally. The three
-- tiers are what that account was entitled to at the time — they say nothing
-- about *who* it is, so nothing that compares identities may look at them, but
-- a change in one is worth a row because a seat upgrade is exactly the kind of
-- thing a fill-rate report needs a date for.
--
-- The tiers come out of the same cached oauthAccount blob as the rest, and
-- Claude Code only refreshes it on /login (profileFetchedAt), so a tier that
-- changed server-side can read stale here until the next sign-in.
CREATE TABLE IF NOT EXISTS account_events (
    ts                          REAL PRIMARY KEY,
    account_uuid                TEXT NOT NULL,
    email                       TEXT,
    organization_uuid           TEXT,
    organization_name           TEXT,
    seat_tier                   TEXT,  -- Team seat product, e.g. 'team_tier_1'; NULL on personal plans
    user_rate_limit_tier        TEXT,  -- per-user bucket, e.g. 'default_claude_max_5x'
    organization_rate_limit_tier TEXT  -- org pool, e.g. 'default_raven'
) WITHOUT ROWID;

-- Append-only utilization samples, written by the statusline render: the live
-- percentages are the only record there is that a window ever filled, so
-- without this table a report can say what a window cost but not how it got
-- there. resets_at is the window-instance key, normalized to the whole minute
-- (rl_window_key). Never pruned — the write gate in record_rate_limit_snapshots
-- is what bounds this table, and a window that filled a year ago is
-- unreconstructible once dropped.
--
-- Deliberately no account column: a row is attributed by its ts against
-- account_events, exactly as ccreport attributes a record, so a later /login or
-- an `adopt` re-attributes these samples too with nothing to rewrite here.
CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
    ts        REAL NOT NULL,
    window    TEXT NOT NULL,   -- 'session' | 'week' | 'sonnet' | 'scoped'
    used_pct  REAL NOT NULL,
    resets_at REAL NOT NULL,   -- epoch seconds; rows sharing it are one window instance
    model     TEXT,            -- scoped window only
    source    TEXT NOT NULL,   -- 'stdin' | 'api'
    PRIMARY KEY (window, ts)
) WITHOUT ROWID;
"""


# account_events, split the way its readers read it — identity first, then the
# tiers. Up here rather than beside the functions that use them (see "Account
# change log" below) because _ADDED_COLUMNS needs the tier names to migrate an
# existing DB, and a name spelled twice is a name that can drift.
_ACCOUNT_IDENTITY_COLS = (
    "account_uuid", "email", "organization_uuid", "organization_name",
)
_ACCOUNT_TIER_COLS = (
    "seat_tier", "user_rate_limit_tier", "organization_rate_limit_tier",
)
_ACCOUNT_COLS = _ACCOUNT_IDENTITY_COLS + _ACCOUNT_TIER_COLS


# Columns a DB created before them is missing. CREATE TABLE covers new DBs;
# these ALTERs bring an existing one up to the same shape.
#
# - the per-window cost columns, derived so a new rolling window needs no
#   migration of its own (the project half arrived after the totals)
# - ccreport cwd: NULL for orphan rows whose source JSONL is already gone —
#   those names are frozen in `project`
# - ccreport repo: normalized git remote, captured at parse time while the
#   working dir still exists. NULL for orphans parsed before this existed.
# - the weekly_scoped per-model limit, as named columns rather than meta_json
#   so the SELECT built from _USAGE_FIELDS picks them up
# - file_costs week_model_json: the ALTER only makes the column readable. Rows
#   written before it carry NULL while still matching on mtime and size, so
#   _COST_ENTRY_SCHEMA below is what makes them re-scan
# - the account_events tier columns: every event captured before them reads back
#   with NULL tiers, which is the truth — nothing recorded what the tier was
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    *(("usage", key, "REAL") for key in rolling_cost_keys()),
    ("usage", "scoped_percent", "INTEGER"),
    ("usage", "scoped_model", "TEXT"),
    ("usage", "scoped_reset", "TEXT"),
    ("ccreport_records", "cwd", "TEXT"),
    ("ccreport_records", "repo", "TEXT"),
    ("file_costs", "week_model_json", "TEXT"),
    *(("account_events", col, "TEXT") for col in _ACCOUNT_TIER_COLS),
]

# Shape of a file_costs row's payload, stored in meta as `cost_schema` and
# checked the way the week and month keys are. Bump it when a stored entry
# gains or loses a field: a row from the previous shape still matches on mtime
# and size, so nothing else would ever make it re-scan, and the missing field
# reads as an empty total rather than an error.
_COST_ENTRY_SCHEMA = "2"


def _add_column(
    conn: sqlite3.Connection, table: str, col: str, col_type: str,
) -> None:
    """ALTER TABLE ADD COLUMN, tolerating a column that is already there."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of *table*, empty if it does not exist."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# Bound on one statement's parameters. SQLITE_MAX_VARIABLE_NUMBER is 32766
# from SQLite 3.32 but 999 on older builds, so stay under the lower ceiling —
# both path sets these queries bind grow with the corpus and never shrink.
_PARAM_CHUNK = 500


# Paths per invalidation transaction. Well under _PARAM_CHUNK because what
# bounds a chunk there is parameters bound and here it is rows written: one path
# carries every record of one session log — ~47 on this corpus, up to 900 — so
# 500 paths is most of a 98k-row table rewritten inside one transaction, which
# is what a render's 0.25 s busy timeout used to lose to.
_INVALIDATE_CHUNK = 100


def _param_chunks(paths: set[str], size: int = _PARAM_CHUNK) -> list[list[str]]:
    """*paths* split into batches small enough to bind in one statement."""
    ordered = sorted(paths)
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def _rollback_if_open(conn: sqlite3.Connection) -> None:
    """ROLLBACK, unless the transaction is already over.

    A failing COMMIT ends the transaction, so an unconditional rollback in the
    handler raises "cannot rollback - no transaction is active" and that
    replaces the real error.
    """
    if conn.in_transaction:
        conn.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_DEFAULT_DB_TIMEOUT_S = 10.0
# Above this a "timeout" is indistinguishable from a hang, so it reads as a
# typo rather than an intent — and it also filters out inf.
_MAX_DB_TIMEOUT_S = 3600.0


def _db_timeout() -> float:
    """Seconds to wait for the write lock, from CLAUDE_CACHE_DB_TIMEOUT.

    The CLI tools can afford the default; the statusline sets it low, because a
    render that blocks behind a writer prints nothing at all where one that
    gives up prints the same line with a slightly stale stat. Anything
    unparseable, non-positive or absurd falls back to the default rather than
    turning the wait off.
    """
    raw = os.environ.get("CLAUDE_CACHE_DB_TIMEOUT", "")
    if not raw:
        return _DEFAULT_DB_TIMEOUT_S
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_DB_TIMEOUT_S
    return val if 0 < val <= _MAX_DB_TIMEOUT_S else _DEFAULT_DB_TIMEOUT_S


def _move(src: Path, dst: Path) -> None:
    """Move *src* onto *dst*, which must not exist.

    A rename, so a 163 MB cache costs what the config file costs and no reader
    ever sees a half-written copy: within one filesystem the operation is
    atomic, and across one it raises EXDEV rather than leaving both. The copy
    fallback is for that second case, which under $HOME should not arise.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        import shutil

        shutil.move(str(src), str(dst))


def relocate_legacy_paths() -> list[str]:
    """Move the cache, its snapshots and the config out of their macsetup paths.

    All three were named after the repo this tooling used to live in. The cache
    *directory* is renamed whole rather than cache.db alone, which is what keeps
    the file together with its -wal and -shm sidecars: a DB moved without its
    WAL loses every transaction still sitting in it.

    A destination that already exists wins and the legacy path is left where it
    is — it is then stale leftovers rather than the live data, and guessing
    which of two databases is the real one is not this function's job. Returns
    one line per move performed, empty when there was nothing to do, so a caller
    can tell "migrated" from "already migrated" without repeating the checks.
    """
    from ccreport.project_identity import CONFIG_PATH, LEGACY_CONFIG_PATH

    moved: list[str] = []
    for src, dst in (
        (_LEGACY_CACHE_DIR, _CACHE_DIR),
        (_LEGACY_SNAPSHOT_DIR, _DEFAULT_SNAPSHOT_DIR),
        (LEGACY_CONFIG_PATH, CONFIG_PATH),
    ):
        if not src.exists() or dst.exists():
            continue
        try:
            _move(src, dst)
        except OSError as e:
            moved.append(f"could not move {src} -> {dst}: {e}")
        else:
            moved.append(f"{src} -> {dst}")
    return moved


def get_connection() -> sqlite3.Connection:
    """Return a module-level singleton connection, creating the DB if needed."""
    global _conn
    if _conn is not None:
        return _conn
    # Costs one stat, and only on the open that finds no DB: every later run
    # takes the branch below and never looks at the legacy paths at all.
    if not DB_PATH.exists() and _LEGACY_CACHE_DIR.exists():
        relocate_legacy_paths()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db_existed = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    conn = sqlite3.connect(str(DB_PATH), timeout=_db_timeout())
    # Registered before the bootstrap can fail, so a connection abandoned
    # half-built still gets closed. close_connection no-ops while _conn is None.
    atexit.register(close_connection)
    try:
        # These three are per-connection state SQLite does not persist, so they
        # sit outside the version gate below and are re-applied on every open.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA cache_size = -2000")  # negative is KiB, not pages: 2000 pages is ~8 MB
        # Every process pays this bootstrap on its first DB touch — statusline
        # imports this module for every render — and on a warm DB all of it is
        # no-ops: every CREATE ... IF NOT EXISTS in _SCHEMA_SQL, one ALTER per
        # _ADDED_COLUMNS entry raising and catching "duplicate column", a SELECT
        # per migration flag. The stamp says the whole thing already ran to
        # completion at this SCHEMA_VERSION.
        bootstrap_needed = _user_version(conn) != SCHEMA_VERSION
        # Snapshot before any schema change or data migration touches the DB.
        # A process that has deferred the daily copy still takes this one: it is
        # the pre-image for the only thing that can rewrite existing rows, and
        # the bootstrap it guards runs at most once per SCHEMA_VERSION.
        snapshot_written = False
        if db_existed and (bootstrap_needed or not _daily_snapshot_deferred()):
            _, snapshot_written = _maybe_snapshot(conn)
        migration_ran = False
        if bootstrap_needed:
            # journal_mode lives in the DB header, so unlike the pragmas above
            # it only needs setting on a DB this build has not opened before.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA_SQL)
            for table, col, col_type in _ADDED_COLUMNS:
                _add_column(conn, table, col, col_type)
            migration_ran = _run_migrations(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        # Once a day (the run that writes the snapshot), plus any run that
        # migrated data — damage arrives from both directions. Deliberately
        # outside the gate: the daily cadence is the point, and migration_ran
        # is False on the fast path because nothing migrated. It rides the
        # snapshot rather than the calendar, so deferring the daily copy moves
        # the check onto the same process and off the render with it.
        if db_existed and (snapshot_written or migration_ran):
            _sanity_check(conn)
    except BaseException:
        conn.close()
        raise
    # Published only now. Assigning before the bootstrap leaves a failure
    # visible to every later get_connection() in the process as a working
    # connection over a half-built schema — and the broad `except Exception`
    # handlers in pricing.py and the statusline make that reachable.
    _conn = conn
    return _conn


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _purge_cost_summaries(conn: sqlite3.Connection) -> None:
    """Drop every cached compute_costs() result, project-scoped ones included.

    A migration that changes what a cost is worth has to clear this cache too,
    or the statusline serves the pre-migration figure until it ages out. The
    keys are not the bare 'cost_summary'/'cost_summary_time' they look like:
    write_cost_summary appends _cost_summary_suffix(cwd), and its only caller
    always passes a cwd, so in practice every stored key is
    cost_summary:<project>. Match on the prefix, not the two literals —
    migrations 1, 2 and 2b spelled out the literals and therefore cleared
    nothing at all.
    """
    conn.execute("DELETE FROM meta WHERE key LIKE 'cost_summary%'")


def _run_migrations(conn: sqlite3.Connection) -> bool:
    """Run one-time data migrations, tracked by meta flags.

    Returns True if any migration actually executed this invocation, which
    makes the caller run the sanity check on top of its daily cadence — a
    migration is the one moment a bug can wipe rows or costs outside that
    window. Migrations that touch ccreport_records must keep setting it.
    """
    ran = False

    # Migration 1: Opus 4.6 / Sonnet 4.6 switched to flat pricing (no 200k tier)
    # on 2026-03-13T18:00 UTC. Cached costs for files modified after that date
    # used inflated tiered rates. Only clear those — older files had correct
    # pricing and their JSONL sources may already be purged from disk.
    if not _get_meta(conn, "migrated_flat_pricing_2026_03_13"):
        cutoff_ns = 1773424800000000000  # 2026-03-13T18:00 UTC in nanoseconds
        conn.execute("DELETE FROM file_costs WHERE mtime_ns >= ?", (cutoff_ns,))
        conn.execute("DELETE FROM session_costs")
        _purge_cost_summaries(conn)
        _set_meta(conn, "migrated_flat_pricing_2026_03_13", "1")
        conn.commit()
        ran = True

    # Migration 2: Also NULL out cached costs in ccreport_records for
    # post-flat-pricing Opus/Sonnet 4.6 records so _rec_cost and record_cost
    # recompute from tokens with the new flat pricing.
    if not _get_meta(conn, "migrated_flat_pricing_ccreport"):
        cutoff_ts = 1773424800.0  # 2026-03-13T18:00 UTC
        conn.execute(
            "UPDATE ccreport_records SET cost = NULL "
            "WHERE ts >= ? AND model IN ('claude-opus-4-6', 'claude-sonnet-4-6')",
            (cutoff_ts,),
        )
        conn.execute("DELETE FROM session_costs")
        _purge_cost_summaries(conn)
        _set_meta(conn, "migrated_flat_pricing_ccreport", "1")
        conn.commit()
        ran = True

    # Migration 2b: Migration 2 matched model names by equality, so dated and
    # bracketed variants ('claude-opus-4-6[1m]') slipped through and kept their
    # inflated pre-flat-pricing costs. find_pricing matches by substring, so it
    # resolves those variants to the flat rates — recomputing from tokens is
    # correct even for orphan records, whose cost can't be re-read from disk.
    if not _get_meta(conn, "migrated_flat_pricing_ccreport_variants"):
        cutoff_ts = 1773424800.0  # 2026-03-13T18:00 UTC
        flat_keys = ("claude-opus-4-6", "claude-sonnet-4-6")
        affected = [
            model
            for (model,) in conn.execute(
                "SELECT DISTINCT model FROM ccreport_records WHERE ts >= ?",
                (cutoff_ts,),
            )
            # The substring rule find_pricing applies, run in reverse.
            if any(key in model or model in key for key in flat_keys)
        ]
        if affected:
            placeholders = ",".join("?" * len(affected))
            conn.execute(
                f"UPDATE ccreport_records SET cost = NULL "  # noqa: S608
                f"WHERE ts >= ? AND model IN ({placeholders})",
                (cutoff_ts, *affected),
            )
            conn.execute("DELETE FROM session_costs")
            _purge_cost_summaries(conn)
        _set_meta(conn, "migrated_flat_pricing_ccreport_variants", "1")
        conn.commit()
        ran = True

    # Migration 3: Rename misleading file_size → fingerprint in session_costs.
    # The ALTER and its flag go in one transaction: legacy isolation autocommits
    # around DDL, so as two commits a crash between them leaves the flag saying
    # done over an unrenamed table, and every session-cost read then raises
    # "no such column: fingerprint" with no path back.
    if not _get_meta(conn, "migrated_rename_fingerprint"):
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                conn.execute(
                    "ALTER TABLE session_costs RENAME COLUMN file_size TO fingerprint"
                )
            except sqlite3.OperationalError as e:
                if not _rename_already_done(e):
                    raise
            # The flag is a claim about the table, so read the table rather than
            # trusting that the ALTER meant what we hoped.
            if "fingerprint" not in _table_columns(conn, "session_costs"):
                raise sqlite3.OperationalError(
                    "session_costs has no fingerprint column after the rename "
                    "migration; refusing to record it as done"
                )
            _set_meta(conn, "migrated_rename_fingerprint", "1")
            conn.execute("COMMIT")
            ran = True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    # Migration 4: drop the three indexes the new DDL replaces. _SCHEMA_SQL
    # only ever creates, so on an existing DB the old ones would otherwise sit
    # there being maintained on every insert and read by nothing:
    #   idx_ccr_file    — a strict prefix of idx_ccr_file_ts, so redundant
    #   idx_ccr_ts      — no live statement has ever filtered ts in SQL; only
    #                     the long-retired migration 2 did
    #   idx_dedup_file  — file_path now leads the dedup_keys PK
    if not _get_meta(conn, "migrated_drop_dead_indexes"):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name in ("idx_ccr_file", "idx_ccr_ts", "idx_dedup_file"):
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            _set_meta(conn, "migrated_drop_dead_indexes", "1")
            conn.execute("COMMIT")
            ran = True
        except BaseException:
            _rollback_if_open(conn)
            raise

    # Migration 5: rebuild dedup_keys with file_path leading the primary key.
    # A WITHOUT ROWID table's primary key cannot be altered in place, so this is
    # the create/copy/drop/rename dance, all inside one transaction so a crash
    # leaves either the old table or the new one and never neither.
    if not _get_meta(conn, "migrated_dedup_keys_pk_order"):
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = _dedup_keys_pk_order(conn)
            # An empty order means there is no dedup_keys table to rebuild.
            # _SCHEMA_SQL creates it in the final shape and runs ahead of this
            # on every real path, so the only way here is _run_migrations
            # called on its own against a partial DB.
            if order and order != ["file_path", "dk"]:
                # dedup_keys is only ever a child, so PRAGMA foreign_keys can
                # stay ON: the DROP below removes a referencing table, not a
                # referenced one, and nothing points at dedup_keys to dangle.
                conn.execute(
                    "CREATE TABLE dedup_keys_new ("
                    "dk TEXT NOT NULL, "
                    "file_path TEXT NOT NULL "
                    "REFERENCES file_costs(path) ON DELETE CASCADE, "
                    "PRIMARY KEY (file_path, dk)) WITHOUT ROWID"
                )
                # Orphans are skipped, not copied: OR IGNORE covers uniqueness
                # but never a FOREIGN KEY violation, so with foreign_keys ON one
                # parentless row in the old table aborts the whole migration —
                # and with it every ccreport run. Dropping such a key is what the
                # cascade would have done anyway; the cost is one re-parse of
                # its source file.
                conn.execute(
                    "INSERT OR IGNORE INTO dedup_keys_new (dk, file_path) "
                    "SELECT dk, file_path FROM dedup_keys "
                    "WHERE file_path IN (SELECT path FROM file_costs)"
                )
                conn.execute("DROP TABLE dedup_keys")
                conn.execute("ALTER TABLE dedup_keys_new RENAME TO dedup_keys")
                # Same discipline as migration 3: the flag is a claim about the
                # table, so re-read the table rather than trust the DDL above.
                rebuilt = _dedup_keys_pk_order(conn)
                if rebuilt != ["file_path", "dk"]:
                    raise sqlite3.OperationalError(
                        f"dedup_keys primary key is {rebuilt} after the rebuild "
                        "migration; refusing to record it as done"
                    )
                if conn.execute("PRAGMA foreign_key_check(dedup_keys)").fetchall():
                    raise sqlite3.OperationalError(
                        "dedup_keys has rows with no file_costs parent after "
                        "the rebuild migration; refusing to record it as done"
                    )
            _set_meta(conn, "migrated_dedup_keys_pk_order", "1")
            conn.execute("COMMIT")
            ran = True
        except BaseException:
            _rollback_if_open(conn)
            raise

    return ran


def _dedup_keys_pk_order(conn: sqlite3.Connection) -> list[str]:
    """dedup_keys' primary-key columns, in key order. Empty if there is no table.

    PRAGMA table_info reports each column's 1-based position within the primary
    key in its `pk` field, and 0 for columns outside it.
    """
    cols = [
        (row[5], row[1])
        for row in conn.execute("PRAGMA table_info(dedup_keys)")
        if row[5]
    ]
    return [name for _pos, name in sorted(cols)]


def _rename_already_done(err: sqlite3.OperationalError) -> bool:
    """Whether *err* means the fingerprint rename has nothing left to do.

    Either the source column is gone (renamed by an earlier run, or never there
    because CREATE TABLE now ships `fingerprint` outright) or the target is
    already present. Everything else — "database is locked" above all, which is
    the routine failure on a contended cache.db — is a retryable failure and
    must reach the caller, or the migration is marked done and lost forever.
    """
    msg = str(err).lower()
    return ("no such column" in msg and "file_size" in msg) or "duplicate column" in msg


# ---------------------------------------------------------------------------
# Snapshot & sanity guard
# ---------------------------------------------------------------------------
#
# One daily snapshot of the live DB, written with SQLite's online backup API so
# WAL-mode writers can't corrupt it, into a directory outside ~/.cache/ where a
# cache-cleanup sweep can't take the backups out with the original.
#
# The sanity guard rides that same once-a-day cadence, plus any run that
# migrated: it compares the irreplaceable ccreport_records against the most
# recent snapshot from a *prior* day and warns on a material loss of rows or of
# costs, and on records left parentless in ccreport_files.
#
# Env overrides:
#   CLAUDE_CACHE_SNAPSHOT_DIR       — destination directory
#   CLAUDE_CACHE_SNAPSHOT_KEEP      — retention count (default 14)
#   CLAUDE_CACHE_SNAPSHOT_DISABLE=1 — skip snapshots entirely
#   CLAUDE_CACHE_SNAPSHOT_DEFER=1   — skip only the daily one; leave it to a
#                                     process that isn't on a render path
#   CLAUDE_CACHE_SANITY_DISABLE=1   — skip sanity check
#   CLAUDE_CACHE_SANITY_ABORT=1     — raise instead of warn on drop


def _snapshot_dir() -> Path:
    override = os.environ.get("CLAUDE_CACHE_SNAPSHOT_DIR")
    return Path(override).expanduser() if override else _DEFAULT_SNAPSHOT_DIR


def _snapshot_keep() -> int:
    raw = os.environ.get("CLAUDE_CACHE_SNAPSHOT_KEEP")
    if not raw:
        return _SNAPSHOT_KEEP_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _SNAPSHOT_KEEP_DEFAULT


def _today_utc() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def _daily_snapshot_deferred() -> bool:
    """Whether this process leaves the routine daily snapshot to another one.

    Distinct from CLAUDE_CACHE_SNAPSHOT_DISABLE, which turns snapshots off
    including the one taken before a migration. Deferring is a statement about
    *this* process being a bad place to copy the DB, not about wanting fewer
    backups, so it never suppresses the pre-bootstrap copy.
    """
    return os.environ.get("CLAUDE_CACHE_SNAPSHOT_DEFER") == "1"


# A tmp file this old belonged to a process that died mid-copy. Left in place
# it would bar the snapshot for the rest of the day, since the claim below is
# what every other process waits behind.
_SNAPSHOT_TMP_STALE_S = 3600.0

# Pages copied per step of the online backup. conn.backup() with no arguments
# copies the whole DB in one uninterrupted call holding a read lock; stepping it
# lets a writer in between batches. 1024 pages is 4 MB at the default page size,
# so even a large DB is a few dozen steps.
#
# sleep= is not a pause between steps: CPython sleeps only after a step that
# returned SQLITE_BUSY or SQLITE_LOCKED, i.e. only when the copy could not
# proceed at all. Steps that make progress follow each other with nothing in
# between, so this value bounds nothing on its own — _snapshot_guard does.
_SNAPSHOT_BACKUP_PAGES = 1024
_SNAPSHOT_BACKUP_SLEEP = 0.01

# What stops a copy that will never finish. SQLite restarts a backup from page 1
# whenever another process writes the source, and every statusline render writes
# — so on a busy machine the loop inside conn.backup() can restart indefinitely.
# It has no cap of its own, a restart returns SQLITE_OK, and the process that
# takes the daily snapshot is the detached refresh whose stderr is DEVNULL, so a
# wedged copy is invisible while the whole cost refresh waits behind it.
#
# The deadline is the real bound; the restart cap ends a copy that is plainly
# losing the race without first burning the full deadline of IO for a file that
# gets thrown away. Hitting either skips the day — tomorrow's run tries again,
# and yesterday's snapshot is still there.
_SNAPSHOT_DEADLINE_S = 20.0
_SNAPSHOT_MAX_RESTARTS = 5


class _SnapshotAbortedError(Exception):
    """The stepped backup hit its deadline or its restart cap."""


def _snapshot_guard(deadline: float) -> Callable[[int, int, int], None]:
    """A conn.backup progress callback that gives up on a copy going nowhere.

    Raising out of the callback is the only way to stop CPython's backup loop;
    it aborts the copy and propagates the exception, which _maybe_snapshot
    catches. A restart shows up as *remaining* going back up — the copy is
    handed the whole page count again — since nothing else in the API reports
    one.
    """
    state = {"remaining": -1, "restarts": 0}

    def progress(_status: int, remaining: int, _pagecount: int) -> None:
        if 0 <= state["remaining"] < remaining:
            state["restarts"] += 1
            if state["restarts"] > _SNAPSHOT_MAX_RESTARTS:
                raise _SnapshotAbortedError(
                    f"gave up after {state['restarts']} restarts "
                    "(the source keeps changing under the copy)"
                )
        state["remaining"] = remaining
        if time.monotonic() >= deadline:
            raise _SnapshotAbortedError(
                f"gave up after its {_SNAPSHOT_DEADLINE_S:.0f}s deadline"
            )

    return progress


def _claim_snapshot_tmp(tmp: Path) -> bool:
    """Create *tmp* exclusively; True if this process won the right to copy.

    target.exists() alone does not serialise anything — it is checked before a
    copy that takes seconds, so every process that starts within that window
    runs a full copy of the DB and only the last rename wins.
    An exclusive create makes the losers cheap: they skip the copy entirely.
    """
    def _create() -> None:
        os.close(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))

    try:
        _create()
        return True
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        if time.time() - tmp.stat().st_mtime < _SNAPSHOT_TMP_STALE_S:
            return False
        tmp.unlink()
        _create()
        return True
    except OSError:
        return False


def _maybe_snapshot(conn: sqlite3.Connection) -> tuple[Path | None, bool]:
    """Take today's snapshot if it doesn't already exist. Rotate old ones.

    Returns (path, fresh): the snapshot path on success (existing or newly
    written) or None if skipped or failed, and whether this call was the one
    that wrote it. `fresh` is true at most once a day, which is what the
    caller hangs the sanity check off. Failures never raise — snapshots are a
    safety net, not a prerequisite.
    """
    if os.environ.get("CLAUDE_CACHE_SNAPSHOT_DISABLE") == "1":
        return None, False
    snap_dir = _snapshot_dir()
    target = snap_dir / f"{_today_utc()}.db"
    if target.exists():
        return target, False
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None, False
    if not _claim_snapshot_tmp(tmp):
        return None, False
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            conn.backup(
                dst,
                pages=_SNAPSHOT_BACKUP_PAGES,
                sleep=_SNAPSHOT_BACKUP_SLEEP,
                progress=_snapshot_guard(time.monotonic() + _SNAPSHOT_DEADLINE_S),
            )
        finally:
            dst.close()
        tmp.replace(target)
    except (sqlite3.Error, OSError, _SnapshotAbortedError) as e:
        try:
            print(f"Warning: cache.db snapshot failed: {e}", file=sys.stderr)
        except OSError:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None, False
    try:
        snapshots = sorted(snap_dir.glob("????-??-??.db"))
        keep = _snapshot_keep()
        for old in snapshots[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return target, True


def _sanity_report(msg: str) -> None:
    """Raise under CLAUDE_CACHE_SANITY_ABORT=1, otherwise warn on stderr."""
    if os.environ.get("CLAUDE_CACHE_SANITY_ABORT") == "1":
        raise RuntimeError(msg)
    try:
        print(f"Warning: {msg}", file=sys.stderr)
    except OSError:
        pass


def _ccr_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    """(row count, rows carrying a cost) for ccreport_records.

    The second number is what catches an over-broad `SET cost = NULL`: it
    leaves every row in place, so a row count alone reads as healthy.
    """
    return conn.execute(
        "SELECT COUNT(*), COUNT(cost) FROM ccreport_records"
    ).fetchone()


# What separates a wipe from ordinary churn. ccreport_records only ever grows:
# records outlive the JSONL they were parsed from, and the only thing that
# removes any is save_ccreport_files replacing one file's rows with the ones it
# just re-parsed. So a drop past a tenth of the corpus is damage, not attrition.
# The minimum prior count keeps a fresh dev DB quiet — at a handful of rows a
# single ordinary deletion clears the percentage.
_SANITY_DROP_THRESHOLD_PCT = 10.0
_SANITY_MIN_PRIOR_COUNT = 100


def _warn_on_drop(label: str, prev: int, cur: int, snapshot: Path) -> None:
    """Report a material drop in one aggregate against the prior snapshot.

    Requires a meaningful prior value before acting so a small dev DB doesn't
    raise false alarms.
    """
    if prev < _SANITY_MIN_PRIOR_COUNT:
        return
    drop_pct = 100.0 * (prev - cur) / prev
    if drop_pct < _SANITY_DROP_THRESHOLD_PCT:
        return
    _sanity_report(
        f"cache.db lost ccreport_records {label}: "
        f"{drop_pct:.1f}% drop ({prev} -> {cur}).\n"
        f"  Prior snapshot: {snapshot}\n"
        f"  Restore with:   cp '{snapshot}' '{DB_PATH}'"
    )


def _sanity_check(conn: sqlite3.Connection) -> None:
    """Warn if the irreplaceable ccreport data lost rows, costs, or parents.

    Row and cost totals are compared against the most recent snapshot from a
    day before today, so a same-run snapshot can't mask a wipe. Called from
    get_connection() on the run that writes the day's snapshot and on any run
    that migrated data; the referential check needs no snapshot and runs
    whenever this does.
    """
    if os.environ.get("CLAUDE_CACHE_SANITY_DISABLE") == "1":
        return
    # Records whose ccreport_files parent is gone are invisible to every
    # reader — all of them enter through that table — so they read as data
    # loss without being one row short of a count.
    parentless = conn.execute("PRAGMA foreign_key_check(ccreport_records)").fetchall()
    if parentless:
        _sanity_report(
            f"cache.db has {len(parentless)} ccreport_records rows with no "
            "ccreport_files parent; every reader joins through that table, "
            "so those records are unreachable."
        )
    snap_dir = _snapshot_dir()
    if not snap_dir.is_dir():
        return
    today_name = f"{_today_utc()}.db"
    snapshots = sorted(snap_dir.glob("????-??-??.db"))
    prior = [s for s in snapshots if s.name != today_name]
    if not prior:
        return
    compare_snap = prior[-1]
    try:
        src = sqlite3.connect(f"file:{compare_snap}?mode=ro", uri=True)
        try:
            prev_rows, prev_costs = _ccr_totals(src)
        finally:
            src.close()
    except sqlite3.Error:
        return
    cur_rows, cur_costs = _ccr_totals(conn)
    _warn_on_drop("rows", prev_rows, cur_rows, compare_snap)
    _warn_on_drop("costs", prev_costs, cur_costs, compare_snap)


def close_connection() -> None:
    """Explicitly close the module-level connection."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# Usage data
# ---------------------------------------------------------------------------

_USAGE_FIELDS = [
    "session_percent", "session_reset", "week_percent", "week_reset",
    "sonnet_percent", "sonnet_reset",
    "scoped_percent", "scoped_model", "scoped_reset", "extra_percent", "extra_spent",
    "extra_limit", "last_updated",
    "session_cost", "session_window_cost", "week_cost", "month_cost",
    # The rolling window columns come from pricing.ROLLING_WINDOWS, so adding a
    # window there reaches the cache without a second edit. Order is internal —
    # the SELECT, the INSERT and the row mapping all read this one list.
    *rolling_cost_keys(),
]

# The singleton row's column list, in the one order _usage_row_to_dict indexes
# by. Every statement that names these columns builds its text from here — the
# SELECT, the INSERT, and the ON CONFLICT update.
_USAGE_COLS = ["id", *_USAGE_FIELDS, "meta_json"]
_USAGE_SELECT_COLS = ", ".join(_USAGE_COLS)


def _usage_row_to_dict(row: tuple) -> dict[str, Any]:
    """Convert a usage table row to a dict matching the old usage.json shape."""
    d: dict[str, Any] = {}
    for i, field in enumerate(_USAGE_FIELDS):
        val = row[i + 1]  # skip id column
        if val is not None:
            d[field] = val
    meta_json = row[len(_USAGE_FIELDS) + 1]
    if meta_json:
        try:
            extra = json.loads(meta_json)
            for k, v in extra.items():
                d[k] = v
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def usage_is_fresh(d: dict[str, Any], max_age: int) -> bool:
    """Whether a usage row is fresh: not expired, and no window has shifted.

    *max_age* is in seconds, measured from the row's last_updated. A predicate
    over an already-read row rather than a query, so the statusline can ask it
    about the row it holds instead of re-reading.
    """
    last_updated = d.get("last_updated")
    if not last_updated:
        return False
    try:
        lu_dt = datetime.fromisoformat(last_updated)
        age = time.time() - lu_dt.timestamp()
        if age > max_age:
            return False
    except (ValueError, TypeError):
        return False
    now = datetime.now(tz=UTC).astimezone()
    for key in ("session_reset", "week_reset"):
        iso = d.get(key)
        if iso:
            try:
                if datetime.fromisoformat(iso) <= now:
                    return False
            except (ValueError, TypeError):
                pass
    return True


def read_usage_cache(max_age: int = 600) -> dict[str, Any] | None:
    """Read cached usage data if fresh enough.

    Returns None if no data, age > *max_age* seconds, or any reset time has
    passed.
    """
    d = read_usage_stale()
    if d is None or not usage_is_fresh(d, max_age):
        return None
    return d


def read_usage_stale() -> dict[str, Any] | None:
    """Read cached usage data regardless of freshness.

    The one place the singleton row is fetched: freshness is a predicate over
    the returned dict, not a second query, so a caller that wants both answers
    pays for one SELECT.
    """
    conn = get_connection()
    row = conn.execute(
        f"SELECT {_USAGE_SELECT_COLS} FROM usage WHERE id = 1"  # noqa: S608
    ).fetchone()
    if row is None:
        return None
    return _usage_row_to_dict(row)


# ---------------------------------------------------------------------------
# Fetch lock & error backoff
# ---------------------------------------------------------------------------

# Seconds to bar a fetch after N consecutive failures, indexed by N - 1; the
# last step is also the one every higher count gets. It has to stay under
# statusline.USAGE_FETCH_INTERVAL_S, the age at which a render calls the cached
# row stale and spawns a refresh — a backoff longer than that outlives the row
# it protects, so every cycle after a failure run starts already barred and the
# quota percentages age past the cadence they promise.
_BACKOFF_SCHEDULE = [45, 120, 240]

# Seconds before a held lock reads as abandoned. This one is the costs lock's:
# its holder does a JSONL rescan and a cache write and nothing that waits on the
# network, so 30 s covers the hold several times over.
_LOCK_STALE_TIMEOUT = 30

# The fetch lock gets its own, an order of magnitude longer, because its holder
# can spend a degraded keychain lookup and a retrying API call under it. It must
# stay at or above usage_api.FETCH_LOCK_MAX_HOLD_S, which derives the worst case
# from the timeouts that actually run there — that expression is the authority
# for this number, not the literal below.
#
# Not imported from there: usage_api imports this module, and a lazy import
# would pull urllib and subprocess onto the render path. What keeps the two in
# step instead is a test on that side (tests/test_usage_api.py,
# TestFetchLockHoldBudget), so raising the hold without raising this fails there.
#
# Set under the hold, the next spawn calls a fetch that is still doing its job
# abandoned and starts a second one — against the endpoint that, in the case
# that made the holder slow, is already answering 429.
FETCH_LOCK_STALE_TIMEOUT = 80.0


# UUID token per lock prefix, set while this process holds that lock.
_lock_owners: dict[str, str] = {}


_BACKOFF_KEYS = ("fetch_fail_count", "fetch_fail_time")


def _backoff_active(
    count_str: str | None, fail_time_str: str | None, now: float,
) -> bool:
    """Whether the recorded failures still bar a fetch at *now*.

    Split from the read so the lock path can decide from meta values it already
    fetched instead of querying the same two keys again.
    """
    if not count_str or not fail_time_str:
        return False
    try:
        count = int(count_str)
        elapsed = now - float(fail_time_str)
    except ValueError:
        return False
    if count <= 0:
        return False
    idx = min(count - 1, len(_BACKOFF_SCHEDULE) - 1)
    return elapsed < _BACKOFF_SCHEDULE[idx]


def _check_backoff_in_txn(conn: sqlite3.Connection, now: float) -> bool:
    """Whether we are inside the error backoff window.

    Reads only, so it is safe outside a transaction; the lock path calls it
    inside its BEGIN IMMEDIATE so a failure cannot be recorded between the
    backoff check and the lock write.
    """
    meta = _get_meta_many(conn, _BACKOFF_KEYS)
    return _backoff_active(meta.get(_BACKOFF_KEYS[0]), meta.get(_BACKOFF_KEYS[1]), now)


def _try_acquire_lock(prefix: str, *, check_backoff: bool, stale_timeout: float) -> bool:
    """Atomically acquire the ``{prefix}_lock``. Returns True if acquired.

    Uses BEGIN IMMEDIATE to serialise concurrent writers so the
    read-check-write is atomic.  A lock older than *stale_timeout*
    is treated as abandoned (e.g. crashed process), and so is one whose
    timestamp does not parse. Each lock passes its own, because what a stale
    lock means is a claim about how long that lock's holder can legitimately
    run; is_fetch_blocked has to judge both by the same values this does.

    An owner token (UUID) is stored alongside the lock so that only
    the process that acquired the lock can release it.

    A busy database past the connection timeout means another writer holds it,
    which is the same answer as a held lock — so BEGIN IMMEDIATE sits inside
    the try and OperationalError returns False. Callers run in the detached
    refresh subprocess, whose stderr is DEVNULL: raising here would silently
    abandon the refresh and leave costs stale.

    Everything the decision needs is read in one statement and written in
    another: this runs inside BEGIN IMMEDIATE, so each extra round trip here is
    time every other writer on the machine spends blocked.
    """
    import uuid

    conn = get_connection()
    now = time.time()
    time_key = f"{prefix}_lock_time"
    owner_key = f"{prefix}_lock_owner"
    keys = (*_BACKOFF_KEYS, time_key) if check_backoff else (time_key,)
    try:
        conn.execute("BEGIN IMMEDIATE")
        meta = _get_meta_many(conn, keys)
        # Folded into the same transaction so a failure cannot be recorded
        # between the backoff check and the lock write.
        if check_backoff and _backoff_active(
            meta.get(_BACKOFF_KEYS[0]), meta.get(_BACKOFF_KEYS[1]), now,
        ):
            conn.execute("COMMIT")
            return False

        locked_at_str = meta.get(time_key)
        if locked_at_str:
            try:
                if now - float(locked_at_str) < stale_timeout:
                    conn.execute("COMMIT")
                    return False
            except ValueError:
                print(f"Warning: corrupt {prefix}_lock_time {locked_at_str!r}, "
                      f"treating as stale", file=sys.stderr)
        owner = str(uuid.uuid4())
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ((time_key, str(now)), (owner_key, owner)),
        )
        conn.execute("COMMIT")
        _lock_owners[prefix] = owner
        return True
    except sqlite3.OperationalError:
        _rollback_if_open(conn)
        return False
    except Exception:
        _rollback_if_open(conn)
        raise


def _release_lock(prefix: str) -> None:
    """Release the ``{prefix}_lock`` only if this process owns it."""
    conn = get_connection()
    owner = _lock_owners.get(prefix)
    if owner is not None and _get_meta(conn, f"{prefix}_lock_owner") != owner:
        # Not our lock — another process took over after staleness timeout
        _lock_owners.pop(prefix, None)
        return
    conn.execute(
        "DELETE FROM meta WHERE key IN (?, ?)",
        (f"{prefix}_lock_time", f"{prefix}_lock_owner"),
    )
    conn.commit()
    _lock_owners.pop(prefix, None)


def try_acquire_fetch_lock() -> bool:
    """Acquire the API fetch lock, refusing while in error backoff."""
    return _try_acquire_lock(
        "fetch", check_backoff=True, stale_timeout=FETCH_LOCK_STALE_TIMEOUT,
    )


def release_fetch_lock() -> None:
    """Release the fetch lock only if this process owns it."""
    _release_lock("fetch")


def try_acquire_costs_lock() -> bool:
    """Acquire the costs-only refresh lock. Returns True if acquired.

    Deliberately separate from the fetch lock: a cost recompute must never make
    a real API fetch fall into _wait_for_leader, where it would poll for a
    freshness bump that a costs-only run never makes. Also skips the API error
    backoff, which says nothing about whether local JSONL can be rescanned.
    """
    return _try_acquire_lock(
        "costs", check_backoff=False, stale_timeout=_LOCK_STALE_TIMEOUT,
    )


def release_costs_lock() -> None:
    """Release the costs-only lock only if this process owns it."""
    _release_lock("costs")


def record_fetch_failure() -> None:
    """Increment consecutive failure count and record time."""
    conn = get_connection()
    count_str = _get_meta(conn, "fetch_fail_count") or "0"
    try:
        count = int(count_str) + 1
    except ValueError:
        count = 1
    _set_meta(conn, "fetch_fail_count", str(count))
    _set_meta(conn, "fetch_fail_time", str(time.time()))
    conn.commit()


def clear_fetch_failures() -> None:
    """Clear failure count on successful fetch."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM meta WHERE key IN ('fetch_fail_count', 'fetch_fail_time')"
    )
    conn.commit()


def check_fetch_backoff() -> bool:
    """Return True if we should skip fetching due to error backoff."""
    return _check_backoff_in_txn(get_connection(), time.time())


def _lock_is_live(locked_at_str: str | None, now: float, stale_timeout: float) -> bool:
    """Whether a stored lock timestamp describes a lock still worth respecting.

    Same staleness rule _try_acquire_lock applies, so the gate below and the
    acquire agree on what an abandoned lock is. A timestamp that does not parse
    reads as stale there and as absent here — either way, not blocking.
    """
    if not locked_at_str:
        return False
    try:
        return now - float(locked_at_str) < stale_timeout
    except ValueError:
        return False


def is_fetch_blocked() -> bool:
    """Whether a live refresh lock or the error backoff bars a refresh now.

    Both locks, because the statusline's only use of this is deciding whether
    spawning a refresh could achieve anything, and both spawns it can make end
    in a _try_acquire_lock. A leader holding the costs lock across a
    multi-second compute_costs used to be invisible here, so every slow render
    in that window spawned a detached interpreter that acquired nothing and
    exited.

    Each lock is judged by its own staleness timeout, the one its acquirer
    applies. Judging the fetch lock by the costs lock's 30 s would call a fetch
    that is still inside its 80 s budget abandoned and spawn the duplicate this
    gate exists to prevent, 50 s before try_acquire_fetch_lock would hand it the
    lock.

    One SELECT for all four keys — the statusline asks this on every render
    where the cached row has expired.
    """
    now = time.time()
    meta = _get_meta_many(
        get_connection(),
        (*_BACKOFF_KEYS, "fetch_lock_time", "costs_lock_time"),
    )
    if _backoff_active(meta.get(_BACKOFF_KEYS[0]), meta.get(_BACKOFF_KEYS[1]), now):
        return True
    return any(
        _lock_is_live(meta.get(key), now, stale_timeout)
        for key, stale_timeout in (
            ("fetch_lock_time", FETCH_LOCK_STALE_TIMEOUT),
            ("costs_lock_time", _LOCK_STALE_TIMEOUT),
        )
    )


def write_usage_cache(data: dict[str, Any], *, snapshot_extra: bool = True) -> None:
    """Write usage data to the singleton usage row.

    Only the keys *data* actually carries are written; every other column keeps
    the value it had. The INSERT OR REPLACE this used to be deleted and
    re-inserted the row, so a caller whose cost computation had failed nulled
    all sixteen cost columns and the statusline rendered empty cost segments
    until the next successful run. A caller that means "this reading no longer
    applies" — the API omitting a quota — has to say so with an explicit None.

    *snapshot_extra* is False for costs-only refreshes, which carry an
    extra_spent value copied from the existing row rather than a fresh reading.
    Re-snapshotting it would stamp a stale figure with a current timestamp and
    skew the per-window deltas.
    """
    conn = get_connection()
    extra: dict[str, Any] = {}
    for k in ("_meta", "_cleaned_session", "_cleaned"):
        if k in data:
            extra[k] = data[k]

    present = [f for f in _USAGE_FIELDS if f in data]
    vals: list[Any] = [data[f] for f in present]
    if extra:
        present.append("meta_json")
        vals.append(json.dumps(extra))

    cols = ", ".join(["id", *present])
    placeholders = ", ".join(["?"] * (len(present) + 1))
    # A write naming nothing still has to be a legal upsert; it just keeps the
    # row exactly as it was.
    updates = ", ".join(f"{c} = excluded.{c}" for c in present) or "id = id"
    conn.execute(
        f"INSERT INTO usage ({cols}) VALUES ({placeholders}) "  # noqa: S608
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        [1, *vals],
    )

    es = data.get("extra_spent") if snapshot_extra else None
    if es is not None:
        now_ts = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO extra_usage_snapshots (ts, spent) VALUES (?, ?)",
            (now_ts, float(es)),
        )
        # 31 days because Extra is a monthly quota: a baseline has to still be
        # there for a window that started at the beginning of the longest month.
        cutoff = now_ts - 31 * 86400
        conn.execute("DELETE FROM extra_usage_snapshots WHERE ts < ?", (cutoff,))

    conn.commit()


def compute_extra_window_deltas(
    current_spent: float,
    session_window_start_epoch: float | None,
    week_window_start_epoch: float | None,
) -> dict[str, float | None]:
    """Compute extra usage deltas for session and week windows.

    Looks up the snapshot closest to (but <=) each window start and returns the
    difference from current_spent. A billing-reset (spent drops) yields 0.

    Always returns both keys, `extra_session_delta` and `extra_week_delta`. A
    value of None means no snapshot predates that window, which is unknown
    rather than zero — a caller must not add it to anything.
    """
    conn = get_connection()
    result: dict[str, float | None] = {
        "extra_session_delta": None,
        "extra_week_delta": None,
    }

    for key, start_epoch in (
        ("extra_session_delta", session_window_start_epoch),
        ("extra_week_delta", week_window_start_epoch),
    ):
        if start_epoch is None:
            continue
        row = conn.execute(
            "SELECT spent FROM extra_usage_snapshots "
            "WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (start_epoch,),
        ).fetchone()
        if row is not None:
            baseline = row[0]
            delta = current_spent - baseline
            # Billing reset: spent dropped below baseline → show 0
            result[key] = max(0.0, delta)
        # No pre-window snapshot → leave as None (unknown, not zero)

    return result


# ---------------------------------------------------------------------------
# Cost cache
# ---------------------------------------------------------------------------

def load_cost_cache(week_key: str, month_key: str) -> dict[str, dict[str, Any]]:
    """Load all file_costs entries. Truncates if week/month keys shifted.

    Returns dict keyed by file path with mtime_ns, size, week_cost,
    month_cost, all_time_cost, session_cost, week_model_costs, dedup_keys.

    A stored entry shape older than _COST_ENTRY_SCHEMA truncates too — the
    whole corpus re-scans once, which is what a row missing a field costs.
    """
    conn = get_connection()

    stored_week = _get_meta(conn, "cost_week")
    stored_month = _get_meta(conn, "cost_month")
    stored_schema = _get_meta(conn, "cost_schema")
    if (stored_week != week_key or stored_month != month_key
            or stored_schema != _COST_ENTRY_SCHEMA):
        conn.execute("DELETE FROM file_costs")
        _set_meta(conn, "cost_week", week_key)
        _set_meta(conn, "cost_month", month_key)
        _set_meta(conn, "cost_schema", _COST_ENTRY_SCHEMA)
        conn.commit()
        return {}

    rows = conn.execute(
        "SELECT path, mtime_ns, size, week_cost, month_cost, all_time_cost, session_cost, "
        "week_model_json FROM file_costs"
    ).fetchall()

    # Also load dedup_keys per file. file_path leads the primary key, so the
    # ORDER BY is the storage order and costs nothing — it just lets groupby
    # cut each file's list in one slice instead of a setdefault per key, and
    # this table carries a row per assistant message inside the retention
    # window. Files pruned by bulk_save_file_costs are simply absent.
    dk_map = {
        path: [dk for _p, dk in group]
        for path, group in groupby(
            conn.execute("SELECT file_path, dk FROM dedup_keys ORDER BY file_path"),
            key=itemgetter(0),
        )
    }

    result: dict[str, dict[str, Any]] = {}
    for path, mtime_ns, size, wc, mc, atc, sc, wmj in rows:
        entry: dict[str, Any] = {
            "mtime_ns": mtime_ns,
            "size": size,
            "week_cost": wc,
            "month_cost": mc,
            "all_time_cost": atc,
            "week_model_costs": json.loads(wmj) if wmj else {},
            "dedup_keys": dk_map.get(path, []),
        }
        if sc is not None:
            entry["session_cost"] = sc
        result[path] = entry
    return result



def _delete_departed_paths(conn: sqlite3.Connection, live: set[str]) -> None:
    """Drop file_costs rows whose path is not in *live*, cascading dedup_keys.

    The difference is taken in Python against one indexed read of the path
    column rather than as `path NOT IN (?, ?, …)` over thousands of live
    paths. Files depart rarely, so the usual run issues no DELETE at all.
    """
    existing = {r[0] for r in conn.execute("SELECT path FROM file_costs")}
    for chunk in _param_chunks(existing - live):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM file_costs WHERE path IN ({placeholders})", chunk)  # noqa: S608


def bulk_save_file_costs(
    entries: dict[str, dict[str, Any]],
    week_key: str,
    month_key: str,
    changed: set[str] | None = None,
    dedup_cutoff_ns: int | None = None,
) -> None:
    """Persist *entries* as the whole file_costs + dedup_keys dataset.

    *changed* names the paths whose entry actually differs from what is
    stored; the rest are written straight back unmodified, so they are
    skipped. Passing None means "assume everything changed".

    *dedup_cutoff_ns* is the oldest mtime whose dedup keys are still worth
    storing — the start of the widest window whose totals those keys can
    change. Keys for files older than it are neither written nor kept, which
    bounds a table that otherwise grows by a row per assistant message and is
    never pruned. The accepted risk: dedup is first-occurrence-wins across
    files, so a message id shared between a fresh file and one that aged out is
    counted twice in all_time. Claude Code writes
    a message id once, into one session file; the collision needs a copied or
    resumed transcript *and* a month between the two copies. Passing None keeps
    every key.

    Rewriting untouched rows is not merely wasted work: DELETE on a
    file_costs row cascades to its dedup_keys, so the old delete-and-rebuild
    churned one row per assistant message corpus-wide every time a single
    JSONL grew by a line. ON CONFLICT keeps the parent row alive, so unchanged
    files' dedup keys are never touched.

    A path present in *entries* but absent from both *changed* and the table
    would be dropped — compute_costs cannot produce one, since an entry it
    reuses unchanged came from the table in the first place.
    """
    conn = get_connection()
    to_write = (
        entries if changed is None
        else {p: e for p, e in entries.items() if p in changed}
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        _set_meta(conn, "cost_week", week_key)
        _set_meta(conn, "cost_month", month_key)
        _set_meta(conn, "cost_schema", _COST_ENTRY_SCHEMA)

        _delete_departed_paths(conn, set(entries))

        if to_write:
            conn.executemany(
                "INSERT INTO file_costs "
                "(path, mtime_ns, size, week_cost, month_cost, all_time_cost, session_cost, "
                " week_model_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "mtime_ns = excluded.mtime_ns, size = excluded.size, "
                "week_cost = excluded.week_cost, month_cost = excluded.month_cost, "
                "all_time_cost = excluded.all_time_cost, "
                "session_cost = excluded.session_cost, "
                "week_model_json = excluded.week_model_json",
                [
                    (
                        path,
                        entry["mtime_ns"],
                        entry["size"],
                        entry.get("week_cost", 0),
                        entry.get("month_cost", 0),
                        entry.get("all_time_cost", 0),
                        entry.get("session_cost"),
                        # NULL for a file with nothing in the week window, which
                        # is most of the corpus.
                        json.dumps(wm) if (wm := entry.get("week_model_costs")) else None,
                    )
                    for path, entry in to_write.items()
                ],
            )
            # Replaced wholesale per rewritten file: a re-parse can drop keys
            # as well as add them, and INSERT OR IGNORE alone never removes.
            conn.executemany(
                "DELETE FROM dedup_keys WHERE file_path = ?",
                [(p,) for p in to_write],
            )
            dk_rows = [
                (dk, path)
                for path, entry in to_write.items()
                if dedup_cutoff_ns is None or entry["mtime_ns"] >= dedup_cutoff_ns
                for dk in entry.get("dedup_keys", [])
            ]
            if dk_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO dedup_keys (dk, file_path) VALUES (?, ?)",
                    dk_rows,
                )
        if dedup_cutoff_ns is not None:
            # Files aged out since the last save, whose keys were written back
            # when they were still in window. One statement: the subquery picks
            # the paths and each delete is a primary-key range scan.
            conn.execute(
                "DELETE FROM dedup_keys WHERE file_path IN "
                "(SELECT path FROM file_costs WHERE mtime_ns < ?)",
                (dedup_cutoff_ns,),
            )
        conn.execute("COMMIT")
    except Exception:
        _rollback_if_open(conn)
        raise


# ---------------------------------------------------------------------------
# Cache stats
# ---------------------------------------------------------------------------

def read_cache_stats(session_id: str) -> tuple[int, int, int, int] | None:
    """Read (total_in_tokens, cum_fresh, cum_create, cum_read) or None."""
    conn = get_connection()
    return conn.execute(
        "SELECT total_in_tokens, cum_fresh, cum_cache_create, cum_cache_read "
        "FROM cache_stats WHERE session_id = ?",
        (session_id,),
    ).fetchone()


def accumulate_cache_stats(
    session_id: str,
    total_in_tokens: int,
    fresh_delta: int,
    create_delta: int,
    read_delta: int,
) -> tuple[int, int, int]:
    """Add one message's token counts to a session's totals.

    Returns the totals after the write: (cum_fresh, cum_create, cum_read).

    The addition happens in the statement, not in the caller. Read-modify-write
    across two statements loses an increment whenever two renders of the same
    session interleave — both read the old total, both write old+delta.

    *total_in_tokens* is the change key rather than a delta: an unchanged value
    means the same API response we already counted, so the upsert's WHERE makes
    that render a no-op and the stored totals are reported back unchanged.
    """
    conn = get_connection()
    row = conn.execute(
        "INSERT INTO cache_stats "
        "(session_id, total_in_tokens, cum_fresh, cum_cache_create, cum_cache_read) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "total_in_tokens = excluded.total_in_tokens, "
        "cum_fresh = cache_stats.cum_fresh + excluded.cum_fresh, "
        "cum_cache_create = cache_stats.cum_cache_create + excluded.cum_cache_create, "
        "cum_cache_read = cache_stats.cum_cache_read + excluded.cum_cache_read "
        "WHERE cache_stats.total_in_tokens IS NOT excluded.total_in_tokens "
        "RETURNING cum_fresh, cum_cache_create, cum_cache_read",
        (session_id, total_in_tokens, fresh_delta, create_delta, read_delta),
    ).fetchone()
    conn.commit()
    if row is not None:
        return (row[0], row[1], row[2])
    # Suppressed by the change key — no row was written, so report the stored
    # totals. A row that vanished between the two statements reads as zeros.
    stored = read_cache_stats(session_id)
    return (stored[1], stored[2], stored[3]) if stored else (0, 0, 0)


# ---------------------------------------------------------------------------
# Session costs
# ---------------------------------------------------------------------------

def read_session_cost(session_id: str) -> tuple[str, float] | None:
    """Read (fingerprint, cost) or None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT fingerprint, cost FROM session_costs WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return (str(row[0]), row[1])


def write_session_cost(session_id: str, fingerprint: str, cost: float) -> None:
    """Upsert session cost entry keyed by fingerprint."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO session_costs (session_id, fingerprint, cost) "
        "VALUES (?, ?, ?)",
        (session_id, fingerprint, cost),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# ccreport cache
# ---------------------------------------------------------------------------

# Bump this when schema or serialization changes in cache_db.py affect
# the format of stored ccreport records.
CACHE_SCHEMA_SALT = "3"


def check_ccreport_valid(version: int, script_hash: str) -> bool:
    """Check if ccreport cache is valid (version + script_hash + schema salt)."""
    conn = get_connection()
    stored_version = _get_meta(conn, "ccreport_version")
    stored_hash = _get_meta(conn, "ccreport_script_hash")
    stored_salt = _get_meta(conn, "ccreport_schema_salt")
    return (
        stored_version == str(version)
        and stored_hash == script_hash
        and stored_salt == CACHE_SCHEMA_SALT
    )


def _ccreport_readable(conn: sqlite3.Connection) -> bool:
    """Whether the stored ccreport rows are in the format this build reads.

    The salt is the only third of check_ccreport_valid a passive reader can
    evaluate: version and script_hash are ccreport.py's own parse contract, and
    the statusline knows neither. The salt is the narrower claim that matters
    here anyway — that the columns mean what _group_by_file assumes.

    Every loader below returns an empty result when this is False, and none of
    them repairs anything. Invalidation is the writer's job (ccreport's
    _ensure_cache_valid, which re-parses what it clears); a statusline render
    that took it upon itself to NULL costs would destroy exactly the orphan
    records no re-parse can rebuild. Degrading to "no cached records" costs the
    render its orphan costs until the next ccreport run and nothing more.
    """
    return _get_meta(conn, "ccreport_schema_salt") == CACHE_SCHEMA_SALT


def invalidate_ccreport(live_paths: set[str]) -> None:
    """Invalidate the ccreport cache for *live_paths*, forcing their re-parse.

    Both writes are scoped to files still on disk. Orphaned records — those
    whose JSONL Claude Code has already purged — keep their fingerprints and
    their costs, because a purged file's `cost` came from costUSD in a source
    that no longer exists: NULLing it is permanent loss, not a placeholder
    the next parse refills.

    Many small transactions rather than one, for the reason _refresh_changed_files
    saves in batches of _SAVE_BATCH: everything else on the machine that writes
    this DB waits behind the lock, and the two waiters here are a render that
    gives up after 0.25 s and the detached refresh that gives up after 10 s.
    Each chunk is self-consistent — a file's fingerprint and its records' costs
    are cleared together — so an interrupted run leaves whole files done and
    whole files untouched, and the meta keys it cleared first make the next run
    invalidate again from the top.
    """
    conn = get_connection()
    # First, and alone: this is what marks the cache invalid, so a crash
    # anywhere below leaves a corpus that re-invalidates rather than one that
    # half-passes check_ccreport_valid.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM meta WHERE key IN "
            "('ccreport_version', 'ccreport_script_hash', 'ccreport_schema_salt')"
        )
        # The rollups froze costs the UPDATEs below NULL, so they no longer
        # describe the corpus. Dropping the fingerprint is enough — the rebuild
        # replaces the rows — and it keeps the stale set unreadable in the
        # window before that rebuild runs.
        conn.execute("DELETE FROM meta WHERE key = ?", (_ROLLUP_FP_KEY,))
        # Unlike save_ccreport_files, this one really does invalidate every
        # scope: the only caller invalidates on a script-hash change, and that
        # hash covers project_identity.py — so the identity a re-parse stamps
        # on a record, which is the input a scope is derived from, is exactly
        # what may have changed.
        _clear_project_scopes(conn)
        conn.execute("COMMIT")
    except Exception:
        _rollback_if_open(conn)
        raise
    for chunk in _param_chunks(live_paths, _INVALIDATE_CHUNK):
        placeholders = ",".join("?" * len(chunk))
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Reset fingerprints so live files fail the mtime/size check and get
            # re-parsed, and NULL their costs so they recompute with current
            # pricing — the re-parse restores whatever the JSONL actually said.
            conn.execute(
                f"UPDATE ccreport_files SET mtime_ns = 0, size = 0 "  # noqa: S608
                f"WHERE path IN ({placeholders})",
                chunk,
            )
            conn.execute(
                f"UPDATE ccreport_records SET cost = NULL "  # noqa: S608
                f"WHERE file_path IN ({placeholders})",
                chunk,
            )
            conn.execute("COMMIT")
        except Exception:
            _rollback_if_open(conn)
            raise


def init_ccreport_meta(version: int, script_hash: str) -> None:
    """Set version, script_hash, and schema salt in meta table."""
    conn = get_connection()
    _set_meta(conn, "ccreport_version", str(version))
    _set_meta(conn, "ccreport_script_hash", script_hash)
    _set_meta(conn, "ccreport_schema_salt", CACHE_SCHEMA_SALT)
    conn.commit()


# The record columns every ccreport reader selects and every writer inserts.
# One list, because a column added to any of them and forgotten in the others
# is a silent format drift the salt can't catch. The SELECT text, the INSERT
# text, the placeholder count, the value tuple and the row mapping below are
# all derived from it; a new column means editing this and the CREATE TABLE.
#
# The four token counts trail the rest because a record dict keeps them in one
# compact "t" list instead of under their own keys — every other column is read
# straight off the dict by column name.
_CCR_FIELD_COLS = (
    "mid", "model", "ts", "sid", "project", "cwd", "repo", "dk", "cost",
)
_CCR_TOKEN_COLS = ("input_tokens", "output_tokens", "cache_create", "cache_read")
_CCR_COLS = (*_CCR_FIELD_COLS, *_CCR_TOKEN_COLS)

# What the readers interpolate. bulk_load_ccreport_cache and the scoped loaders
# prepend file_path and strip it off the row before mapping.
_CCR_SELECT = ", ".join(_CCR_COLS)
_CCR_INSERT_COLS = ", ".join(("file_path", *_CCR_COLS))
_CCR_INSERT_PLACEHOLDERS = ", ".join("?" * (len(_CCR_COLS) + 1))


def _ccr_record_to_row(path: str, rec: dict) -> tuple:
    """A record dict as an insert row for _CCR_INSERT_COLS."""
    return (
        path,
        *(rec.get(name) for name in _CCR_FIELD_COLS),
        *rec["t"][:len(_CCR_TOKEN_COLS)],
    )


def _group_by_file(rows: list[tuple]) -> dict[str, list[dict]]:
    """Rows of (file_path, *_CCR_COLS) as {path: [record dict]}, order kept.

    Every cached read lands here, ~98k rows on a full report and a project's
    worth on every slow statusline render, so the record dict is built as one
    literal indexed straight off the row. Going through _CCR_COLS by name meant
    a slice, a zipped dict, a comprehension and a list per row — four containers
    thrown away for one the caller reads once.

    The indices below are positions in (file_path, *_CCR_COLS) and nothing at
    runtime ties them to that tuple; test_the_record_dict_matches_the_column_tuple
    is what fails if a column is added there and not here.
    """
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append({
            "mid": row[1], "model": row[2], "ts": row[3], "sid": row[4],
            "project": row[5], "cwd": row[6], "repo": row[7], "dk": row[8],
            "cost": row[9], "t": [row[10], row[11], row[12], row[13]],
        })
    return grouped


def prefix_range(prefix: str) -> tuple[str, str]:
    """Half-open [lo, hi) bounds selecting exactly the strings starting *prefix*.

    hi is *prefix* with its last character stepped up one code point, which is
    the first string that sorts past every extension of *prefix*. Used instead
    of LIKE or a Python startswith so the range rides the index on file_path —
    and unlike a `>= prefix` scan it stops at the end of the directory rather
    than running to the end of the table.

    Raises:
        ValueError: *prefix* is empty. There is no last character to step, so
            the bounds would widen to every row in the table and the caller
            would silently get the whole corpus where it asked for one scope.
    """
    if not prefix:
        raise ValueError("prefix_range needs a non-empty prefix")
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def load_ccreport_records_under(prefix: str) -> dict[str, list[dict]]:
    """Cached records whose file path starts with *prefix*, as {path: [record]}.

    The statusline renders one project at a time and threw away everything else
    bulk_load_ccreport_cache handed it — the whole table, on every render.
    Deliberately unbounded in time: the caller's all_time total needs every
    record the prefix covers.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}
    lo, hi = prefix_range(prefix)
    return _group_by_file(conn.execute(
        f"SELECT file_path, {_CCR_SELECT} FROM ccreport_records "  # noqa: S608
        f"WHERE file_path >= ? AND file_path < ?",
        (lo, hi),
    ).fetchall())


def load_ccreport_file_meta_under(prefix: str) -> dict[str, tuple[int, int]]:
    """Cached (mtime_ns, size) for every file path starting *prefix*.

    The fingerprint half of load_ccreport_records_under, for a reader deciding
    per file whether the cached records still describe what is on disk.
    load_ccreport_file_identities answers a different question — which project a
    file belongs to — and carries no fingerprint, and bulk_load_ccreport_cache
    pays for every file on the machine.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable. That is what makes a stale-format cache degrade to a
    full re-parse rather than to wrong numbers.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}
    lo, hi = prefix_range(prefix)
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT path, mtime_ns, size FROM ccreport_files "
            "WHERE path >= ? AND path < ?",
            (lo, hi),
        ).fetchall()
    }


def load_ccreport_records_for_session(session_id: str) -> dict[str, list[dict]]:
    """Cached records for one session id, as {path: [record]}.

    Answers the purged-JSONL fallback in compute_session_cost, which used to
    load the table and drop every row with a different sid in Python. Callers
    still narrow by project prefix themselves — a session id is near-unique, so
    the index on sid does the elimination that matters.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}
    return _group_by_file(conn.execute(
        f"SELECT file_path, {_CCR_SELECT} FROM ccreport_records WHERE sid = ?",  # noqa: S608
        (session_id,),
    ).fetchall())


def bulk_load_ccreport_cache() -> tuple[dict[str, tuple[int, int]], dict[str, list[dict]]]:
    """Bulk-load all ccreport file metadata and records.

    Returns (file_meta, records_by_file) where:
      file_meta: {path: (mtime_ns, size)}
      records_by_file: {path: [list of record dicts]}

    Both halves come back empty when the cached rows are not in this build's
    format; see _ccreport_readable. Prefer a scoped loader above when the
    caller only wants one project or one session — this reads every row.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}, {}
    file_rows = conn.execute("SELECT path, mtime_ns, size FROM ccreport_files").fetchall()
    file_meta = {r[0]: (r[1], r[2]) for r in file_rows}
    if not file_meta:
        return {}, {}
    rec_rows = conn.execute(
        f"SELECT file_path, {_CCR_SELECT} FROM ccreport_records"  # noqa: S608
    ).fetchall()
    return file_meta, _group_by_file(rec_rows)


def load_ccreport_file_meta() -> dict[str, tuple[int, int]]:
    """Cached (mtime_ns, size) for every cached file, machine-wide.

    The unscoped twin of load_ccreport_file_meta_under, for the rollup read
    path: it needs to know which files moved on disk and nothing else about
    them, and bulk_load_ccreport_cache's second query is exactly the ~95k
    record rows that path exists to not read.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT path, mtime_ns, size FROM ccreport_files")
    }


def load_ccreport_records_since(cutoff_ts: float) -> dict[str, list[dict]]:
    """Cached records at or after *cutoff_ts*, as {path: [record]}.

    One scan covers live and orphaned files alike, which is what lets the
    rollup path apply the same dedup to the recent slice that a full load
    applies to everything. A full table scan on purpose: no index leads with
    ts, and a standalone one is deliberately not there.

    ORDER BY id pins the row order to insert order — the order
    bulk_load_ccreport_cache hands the same rows to the same dedup — rather
    than leaving first-occurrence winners to whatever the planner picks. A
    table scan already yields rowid order, so it costs no sort.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}
    return _group_by_file(conn.execute(
        f"SELECT file_path, {_CCR_SELECT} FROM ccreport_records "  # noqa: S608
        "WHERE ts >= ? ORDER BY id",
        (cutoff_ts,),
    ).fetchall())


def load_ccreport_records_in_range(
    since_ts: float | None, until_ts: float | None,
) -> dict[str, list[dict]]:
    """Cached records inside a timestamp window, as {path: [record]}.

    The two-sided twin of load_ccreport_records_since, for a filtered report:
    `ccreport daily --since yesterday` used to deserialize the whole corpus
    into UsageRecords and then drop all but one day of it.
    Either bound may be None, meaning open-ended on that side; both None is
    every row, which is what bulk_load_ccreport_cache already answers more
    cheaply when the file metadata is wanted too.

    The bounds are inclusive on both ends, matching ccreport._keep, which
    drops a record on `ts < since` or `ts > until`. Records outside the window
    never reach the dedup there either — _keep returns before computing a key —
    so filtering here cannot change which duplicate wins.

    A full table scan on purpose, and ORDER BY id for insert order, for the
    same reasons as load_ccreport_records_since.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}
    bounds = [("ts >= ?", since_ts), ("ts <= ?", until_ts)]
    clauses = [sql for sql, value in bounds if value is not None]
    params = tuple(value for _sql, value in bounds if value is not None)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    return _group_by_file(conn.execute(
        f"SELECT file_path, {_CCR_SELECT} FROM ccreport_records "  # noqa: S608
        f"{where}ORDER BY id",
        params,
    ).fetchall())


# The fingerprint the ccreport_orphan_costs rows were summed under.
_ORPHAN_FP_KEY = "ccreport_orphan_fp"


def load_ccreport_records_for_paths(paths: Iterable[str]) -> dict[str, list[dict]]:
    """Cached records belonging to *paths*, as {path: [record]}.

    For rebuilding the orphan all-time totals: the caller has already worked
    out which cached files are gone from disk, and reading the rest back only
    to drop it is the walk this exists to stop. Chunked so a path set larger
    than SQLite's parameter limit still goes as an indexed lookup rather than
    a table scan.

    The id is selected and sorted on rather than left to the chunk order: the
    rows come back one path range at a time, and dedup is first-occurrence-wins,
    so handing them over grouped by path would let the alphabetically first
    file win a duplicate that insert order gives to another. Sorting restores
    the order an unbounded table scan would have produced.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return {}
    rows: list[tuple] = []
    for chunk in _param_chunks(set(paths)):
        placeholders = ",".join("?" * len(chunk))
        rows.extend(conn.execute(
            f"SELECT id, file_path, {_CCR_SELECT} FROM ccreport_records "  # noqa: S608
            f"WHERE file_path IN ({placeholders})",
            chunk,
        ))
    rows.sort(key=itemgetter(0))
    return _group_by_file([row[1:] for row in rows])


def orphan_alltime_stamp(orphan_paths: Iterable[str]) -> str:
    """The DB-side half of the orphan all-time fingerprint.

    A digest of the ccreport_files rows of the orphaned files themselves, and
    nothing wider. Deliberately blind to the live half of the corpus: every
    writer that can reach an orphaned record either makes it non-orphaned
    first or bumps SCHEMA_VERSION. save_ccreport_files only ever writes files
    it just parsed off disk, so a path it touches is live by definition, and
    invalidate_ccreport scopes both of its UPDATEs to live paths on purpose —
    a purged file's stored cost is the only copy there is. What is left is the
    one-time data migrations, which the version covers.

    A whole-table stamp (or a MAX(id) over the records) would be cheaper still
    and would rebuild ~86k rows every time `ccreport` re-parsed one live
    session log, which cannot move this total by a cent.

    mtime_ns is folded modulo a prime because a bare SUM over a couple of
    thousand nanosecond epochs is ~4e21 and SQLite answers that with "integer
    overflow". The modulus only has to make a changed mtime change the digest.
    """
    conn = get_connection()
    n = mtimes = sizes = 0
    for chunk in _param_chunks(set(orphan_paths)):
        placeholders = ",".join("?" * len(chunk))
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(mtime_ns % 1000000007), 0), "  # noqa: S608
            f"COALESCE(SUM(size), 0) FROM ccreport_files WHERE path IN ({placeholders})",
            chunk,
        ).fetchone()
        n += row[0]
        mtimes += row[1]
        sizes += row[2]
    return f"{n}:{mtimes}:{sizes}:{SCHEMA_VERSION}:{CACHE_SCHEMA_SALT}"


def load_orphan_alltime(fingerprint: str) -> list[tuple[str, str, str, str, float]]:
    """Stored orphan all-time rows, or [] if they no longer describe the corpus.

    [] means "rebuild", never "there is nothing" — an empty orphan set stores
    no rows but does stamp its fingerprint, and the caller's rebuild of nothing
    costs nothing.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return []
    if _get_meta(conn, _ORPHAN_FP_KEY) != fingerprint:
        return []
    return conn.execute(
        "SELECT dir_prefix, project, cwd, repo, cost FROM ccreport_orphan_costs"
    ).fetchall()


def save_orphan_alltime(
    rows: list[tuple[str, str, str, str, float]], fingerprint: str,
) -> None:
    """Replace the orphan all-time table and stamp it with *fingerprint*.

    One transaction for both, for the same reason as save_ccreport_rollups: a
    fingerprint outliving the rows it describes reads as valid and serves a
    short total as the whole of history.
    """
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM ccreport_orphan_costs")
        if rows:
            conn.executemany(
                "INSERT INTO ccreport_orphan_costs "
                "(dir_prefix, project, cwd, repo, cost) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        _set_meta(conn, _ORPHAN_FP_KEY, fingerprint)
        conn.execute("COMMIT")
    except Exception:
        _rollback_if_open(conn)
        raise


def load_file_all_time_under(prefix: str) -> dict[str, tuple[int, int, float, list[str]]]:
    """file_costs' time-independent half for one project's files.

    {path: (mtime_ns, size, all_time_cost, dedup_keys)}.

    load_cost_cache is the wrong door for a reader that only wants all_time:
    it takes the week and month keys and *truncates the table* when they have
    moved on, which a render computing rolling costs has no business doing —
    it does not even know which window the stored week_cost belongs to. What
    it reads here survives that rollover by construction, since an all-time
    total and a dedup key are both independent of where the windows sit.
    """
    conn = get_connection()
    lo, hi = prefix_range(prefix)
    rows = conn.execute(
        "SELECT path, mtime_ns, size, all_time_cost FROM file_costs "
        "WHERE path >= ? AND path < ?",
        (lo, hi),
    ).fetchall()
    if not rows:
        return {}
    dk_map: dict[str, list[str]] = {}
    for path, dk in conn.execute(
        "SELECT file_path, dk FROM dedup_keys WHERE file_path >= ? AND file_path < ?",
        (lo, hi),
    ):
        dk_map.setdefault(path, []).append(dk)
    return {
        path: (mtime_ns, size, atc, dk_map.get(path, []))
        for path, mtime_ns, size, atc in rows
    }


def load_ccreport_file_identities() -> list[tuple[str, str | None, str | None, str]]:
    """(file_path, repo, cwd, project) for every cached file, one row each.

    Answers "which files belong to the same project as this one" without
    dragging the records back: parse_jsonl_file stamps one identity onto every
    record in a file, so the bare columns a GROUP BY file_path picks are the
    file's identity whichever row SQLite lands on. Grouping rides
    idx_ccr_file_ts, so this reads the index rather than sorting the table.

    Only pricing's project scoping calls it, and only when merge rules exist —
    without them a project is its own directory and no lookup is needed.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return []
    return [
        (row[0], row[1], row[2], row[3] or "")
        for row in conn.execute(
            "SELECT file_path, repo, cwd, project FROM ccreport_records "
            "GROUP BY file_path"
        ).fetchall()
    ]


def _file_identity(records: list[dict]) -> tuple | None:
    """The (repo, cwd, project) a file's records carry; None if it has none.

    parse_jsonl_file stamps one identity onto every record of a file, so the
    first record answers for the file — the same assumption
    load_ccreport_file_identities makes when it lets GROUP BY pick whichever row
    it lands on.
    """
    if not records:
        return None
    first = records[0]
    return (first.get("repo"), first.get("cwd"), first.get("project"))


def _stored_identities(conn: sqlite3.Connection, paths: list[str]) -> dict[str, tuple]:
    """The identity currently cached for each of *paths* that has one."""
    stored: dict[str, tuple] = {}
    for chunk in _param_chunks(set(paths)):
        placeholders = ",".join("?" * len(chunk))
        stored.update({
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                f"SELECT file_path, repo, cwd, project FROM ccreport_records "  # noqa: S608
                f"WHERE file_path IN ({placeholders}) GROUP BY file_path",
                chunk,
            )
        })
    return stored


def _identity_already_cached_before(
    conn: sqlite3.Connection, path: str, identity: tuple,
) -> bool:
    """Whether a cached file sorting before *path* in its directory carries *identity*.

    Both things a scope is derived from are then already settled for this
    directory. Which project directories a scope's prefixes cover is decided per
    directory — one file resolving to the scope's name puts the whole directory
    in — and *identity* is what resolve() is a function of, so a second file
    saying the same thing adds no directory. And the name itself comes from the
    first identity in path order under the cwd's own directories, which a file
    that sorts after an existing one cannot become.

    The directory here is the file's parent, which is at or below the project
    directory pricing groups by — narrower than it needs to be, never wider.
    """
    parent = path.rsplit("/", 1)[0] + "/"
    if parent == path:
        return False
    return conn.execute(
        "SELECT 1 FROM ccreport_records "
        "WHERE file_path >= ? AND file_path < ? "
        "AND repo IS ? AND cwd IS ? AND project IS ? LIMIT 1",
        (parent, path, *identity),
    ).fetchone() is not None


def _save_invalidates_scopes(
    conn: sqlite3.Connection, entries: list[tuple[str, int, int, list[dict]]],
) -> bool:
    """Whether writing *entries* can change any cached project scope.

    A scope is a pure function of project_overrides and the cached record
    identities, so a save that leaves every identity where it was cannot move
    one — and that is the ordinary save: ccreport re-parses a session log that
    grew, and re-writes the same (repo, cwd, project) it wrote before. Truncating
    the table on every batch regardless is what made an ordinary ccreport run
    cost every open session the ~0.020 s scope derivation on its next slow
    render.

    Reads the pre-write state, so it must run before the DELETE below. Answering
    True clears every scope rather than a computed subset: a genuinely new
    identity can join its directory to any name, and deciding which names it
    joins means running the override rules over the whole corpus — the work the
    cache exists to avoid.
    """
    stored = _stored_identities(conn, [path for path, _m, _s, _r in entries])
    for path, _m, _s, records in entries:
        identity = _file_identity(records)
        if stored.get(path) == identity:
            continue
        # A file that now parses to nothing takes an identity away; only a
        # rederivation can say what that leaves behind.
        if identity is None:
            return True
        if not _identity_already_cached_before(conn, path, identity):
            return True
    return False


def save_ccreport_files(entries: list[tuple[str, int, int, list[dict]]]) -> None:
    """Save/replace several (path, mtime_ns, size, records) entries at once.

    One transaction for the whole batch. A full rebuild re-parses every file
    in the corpus, and committing per file made that thousands of WAL
    write-lock cycles, each able to stall a rendering statusline for up to the
    busy timeout. Callers batch in chunks so no single
    transaction spans a long stretch of parsing.

    Atomic per call: a crash leaves each file in the batch either fully
    cached or fully stale, never half its records.
    """
    if not entries:
        return
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Before the DELETE, which is what takes the old identities away.
        scopes_stale = _save_invalidates_scopes(conn, entries)
        # Deleting the parent cascades its old records away.
        conn.executemany(
            "DELETE FROM ccreport_files WHERE path = ?",
            [(path,) for path, _m, _s, _r in entries],
        )
        conn.executemany(
            "INSERT INTO ccreport_files (path, mtime_ns, size) VALUES (?, ?, ?)",
            [(path, mtime_ns, size) for path, mtime_ns, size, _r in entries],
        )
        rows = [
            _ccr_record_to_row(path, r)
            for path, _m, _s, records in entries
            for r in records
        ]
        if rows:
            conn.executemany(
                f"INSERT INTO ccreport_records ({_CCR_INSERT_COLS}) "  # noqa: S608
                f"VALUES ({_CCR_INSERT_PLACEHOLDERS})",
                rows,
            )
        if scopes_stale:
            _clear_project_scopes(conn)
        conn.execute("COMMIT")
    except Exception:
        _rollback_if_open(conn)
        raise


def save_ccreport_file(
    path: str, mtime_ns: int, size: int, records: list[dict],
) -> None:
    """Save/replace a single file entry and all its records."""
    save_ccreport_files([(path, mtime_ns, size, records)])


def load_ccreport_file_meta_before(cutoff_ts: float) -> list[tuple[str, int, int]]:
    """(path, mtime_ns, size) per cached file holding a record before *cutoff_ts*.

    Sorted by path. The half of the corpus a rollup froze, identified the same way the record
    cache identifies a file. Growing, shrinking or re-parsing any of these
    changes what the rollup should have said, so this is what the rollup
    fingerprint is built over. EXISTS rather than a join so idx_ccr_file_ts
    answers each file with one seek and stops.

    Empty when the cached rows are not in this build's format; see
    _ccreport_readable.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return []
    return conn.execute(
        "SELECT path, mtime_ns, size FROM ccreport_files f WHERE EXISTS ("
        "  SELECT 1 FROM ccreport_records r WHERE r.file_path = f.path AND r.ts < ?"
        ") ORDER BY path",
        (cutoff_ts,),
    ).fetchall()


# The rollup columns, in table order: the six-part key, the timestamp span, the
# four token sums, then cost and record count. Both the SELECT and the INSERT
# are built from this, so ccreport reads a row back in the order it wrote one.
_CCR_ROLLUP_COLS = (
    "day", "oslo_date", "sid", "project", "model", "account",
    "min_ts", "max_ts",
    "input_tokens", "output_tokens", "cache_create", "cache_read",
    "cost", "n",
)
_CCR_ROLLUP_SELECT = ", ".join(_CCR_ROLLUP_COLS)
_CCR_ROLLUP_PLACEHOLDERS = ", ".join("?" * len(_CCR_ROLLUP_COLS))

_ROLLUP_FP_KEY = "ccreport_rollup_fp"


def read_ccreport_rollup_fingerprint() -> str | None:
    """The fingerprint the stored rollup rows were built under, or None.

    None also when the rows are not in this build's format — the salt gates
    this the same as every other ccreport reader, so a format change makes the
    rollups miss and rebuild rather than serve rows nobody can interpret.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return None
    return _get_meta(conn, _ROLLUP_FP_KEY)


def load_ccreport_rollups() -> list[tuple]:
    """Every rollup row, as tuples in _CCR_ROLLUP_COLS order.

    Callers must have checked read_ccreport_rollup_fingerprint first: these
    rows carry no validity of their own, and a stale set is wrong numbers
    rather than missing ones.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return []
    return conn.execute(
        f"SELECT {_CCR_ROLLUP_SELECT} FROM ccreport_rollups"  # noqa: S608
    ).fetchall()


def save_ccreport_rollups(rows: list[tuple], fingerprint: str) -> None:
    """Replace the whole rollup table and stamp it with *fingerprint*.

    One transaction for both, because a fingerprint that outlives the rows it
    describes is the one failure mode that reads as valid: the next run would
    serve a short table as the whole of history. Whole-table replace rather
    than a merge — the cutoff moves a day forward every day, so most of what
    changes between builds is which rows exist at all.
    """
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM ccreport_rollups")
        if rows:
            conn.executemany(
                f"INSERT INTO ccreport_rollups ({_CCR_ROLLUP_SELECT}) "  # noqa: S608
                f"VALUES ({_CCR_ROLLUP_PLACEHOLDERS})",
                rows,
            )
        _set_meta(conn, _ROLLUP_FP_KEY, fingerprint)
        conn.execute("COMMIT")
    except Exception:
        _rollback_if_open(conn)
        raise


def count_ccreport_records_without_signals() -> int:
    """Count records carrying neither cwd nor repo — reachable only by name.

    Both columns were added after the fact and never backfilled, so rows
    written before that keep them NULL forever. ccreport warns on this count
    when a remote or cwd_prefix rule is added, since those rules match on the
    two columns these rows do not have.
    """
    conn = get_connection()
    return conn.execute(
        "SELECT COUNT(*) FROM ccreport_records WHERE cwd IS NULL AND repo IS NULL"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Project overrides (manual grouping rules)
# ---------------------------------------------------------------------------

def get_project_overrides() -> list[dict]:
    """Return all override rules, lowest id first (insertion order = priority)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, match_kind, match_value, target FROM project_overrides ORDER BY id"
    ).fetchall()
    return [
        {"id": r[0], "match_kind": r[1], "match_value": r[2], "target": r[3]}
        for r in rows
    ]


def add_project_override(match_kind: str, match_value: str, target: str) -> None:
    """Insert or replace a rule. (match_kind, match_value) is unique."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO project_overrides (match_kind, match_value, target) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (match_kind, match_value) DO UPDATE SET target = excluded.target",
        (match_kind, match_value, target),
    )
    _clear_project_scopes(conn)
    conn.commit()


def delete_project_override(match_value: str, match_kind: str | None = None) -> int:
    """Delete rules matching a value (optionally scoped to a kind). Returns count."""
    conn = get_connection()
    if match_kind:
        cur = conn.execute(
            "DELETE FROM project_overrides WHERE match_value = ? AND match_kind = ?",
            (match_value, match_kind),
        )
    else:
        cur = conn.execute(
            "DELETE FROM project_overrides WHERE match_value = ?", (match_value,)
        )
    _clear_project_scopes(conn)
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Resolved project scopes (per cwd)
# ---------------------------------------------------------------------------

def load_project_scope(cwd: str) -> tuple[str, list[str]] | None:
    """The cached (name, prefixes) pricing.project_scope resolved for *cwd*.

    None when nothing is cached, and also when the salt says the rows are not
    in this build's format: load_ccreport_file_identities reads as empty there
    and project_scope degrades to the unmerged scope, so a cached scope has to
    degrade with it rather than keep serving merged prefixes its own reader
    could no longer re-derive.
    """
    conn = get_connection()
    if not _ccreport_readable(conn):
        return None
    row = conn.execute(
        "SELECT name, prefixes FROM project_scopes WHERE cwd = ?", (cwd,)
    ).fetchone()
    if row is None:
        return None
    return (row[0], list(json.loads(row[1])))


def save_project_scope(cwd: str, name: str, prefixes: list[str]) -> None:
    """Cache the scope resolved for *cwd*, replacing any earlier answer."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO project_scopes (cwd, name, prefixes) "
        "VALUES (?, ?, ?)",
        (cwd, name, json.dumps(prefixes)),
    )
    conn.commit()


def _clear_project_scopes(conn: sqlite3.Connection) -> None:
    """Drop every cached scope. No commit — this rides the caller's write.

    Emptying rather than patching: a rule or a record can move any cwd's scope,
    and the cwd nobody is standing in costs nothing to leave uncached. Callers
    are every writer of the two inputs, which is what lets a surviving row be
    trusted without a fingerprint of its own.
    """
    conn.execute("DELETE FROM project_scopes")


# ---------------------------------------------------------------------------
# Account change log
# ---------------------------------------------------------------------------
#
# Seven fields survive out of ~/.claude.json's oauthAccount blob: the four
# identity ones and the three tiers, both described at the CREATE TABLE. Nothing
# else there is kept — billingType, the role fields and displayName are either
# volatile or say more about the person than a cost report needs.
#
# _ACCOUNT_COLS, _ACCOUNT_IDENTITY_COLS and _ACCOUNT_TIER_COLS are defined
# beside the schema, above.

_ACCOUNT_SELECT = ", ".join(_ACCOUNT_COLS)
# Bound as (ts, *_ACCOUNT_COLS), so the count follows the column list rather
# than a hand-written run of question marks that a new column silently breaks.
_ACCOUNT_PLACEHOLDERS = ", ".join("?" * (1 + len(_ACCOUNT_COLS)))

# Timestamp of the one row `ccreport adopt` writes, which claims the history
# that predates capture for an account. Zero because attribution takes the
# newest event at or before a record: an event older than every record on the
# machine is the one every otherwise-unattributed record lands on. It is a
# claim, not a capture, and the readers below keep the two apart.
ADOPTED_TS = 0.0

# The oauthAccount keys behind _ACCOUNT_COLS, in the same order.
_ACCOUNT_SOURCE_KEYS = (
    "accountUuid", "emailAddress", "organizationUuid", "organizationName",
    "seatTier", "userRateLimitTier", "organizationRateLimitTier",
)


def _account_identity(oauth: dict[str, Any]) -> tuple[str | None, ...]:
    """The persisted fields of an oauthAccount blob, in _ACCOUNT_COLS order.

    Anything that is not a non-empty string reads as absent, so a JSON null or
    a nested object in the config cannot reach the table as a value — and two
    renders that disagree only in how a field was spelled as empty do not read
    as an account change.
    """
    values: list[str | None] = []
    for key in _ACCOUNT_SOURCE_KEYS:
        val = oauth.get(key)
        values.append(val if isinstance(val, str) and val else None)
    return tuple(values)


def _account_row_to_dict(row: tuple) -> dict[str, Any]:
    """One account_events row as a dict: ts plus every stored field."""
    return {"ts": row[0], **dict(zip(_ACCOUNT_COLS, row[1:], strict=True))}


def effective_limit_tier(row: dict[str, Any]) -> str | None:
    """The rate-limit tier *row* was actually subject to, or None if unrecorded.

    The per-user tier wins: when Anthropic assigns one it is the bucket the
    account draws against, and the org tier then only names the pool it would
    otherwise have shared.
    """
    return row.get("user_rate_limit_tier") or row.get("organization_rate_limit_tier")


def record_account_event(
    oauth: dict[str, Any], now: float | None = None,
) -> bool:
    """Append *oauth* to the change log if it differs from the newest row.

    Returns whether a row was written. The caller is the statusline, on every
    render, so the unchanged case — which is every render but the handful that
    follow a /login — has to cost one SELECT and no write: ts is the primary
    key of a WITHOUT ROWID table, making "newest" the first step of a reverse
    key scan.

    The comparison is over the whole stored tuple, tiers included, so a seat or
    plan change on an unchanged login appends a row too — that is the point of
    keeping them: the log is where a report finds the date a tier moved.

    An oauthAccount with no accountUuid is dropped rather than stored under a
    NULL key. Without it there is nothing stable to tell two accounts apart by,
    and a row here is permanent history that no later render can correct.
    """
    identity = _account_identity(oauth)
    if identity[0] is None:
        return False
    conn = get_connection()
    row = conn.execute(
        f"SELECT {_ACCOUNT_SELECT} FROM account_events "  # noqa: S608
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is not None and tuple(row) == identity:
        return False
    # OR REPLACE covers only two changes landing inside one tick of time.time():
    # that is the same instant, so the later reading is the one to keep.
    conn.execute(
        f"INSERT OR REPLACE INTO account_events (ts, {_ACCOUNT_SELECT}) "  # noqa: S608
        f"VALUES ({_ACCOUNT_PLACEHOLDERS})",
        (time.time() if now is None else now, *identity),
    )
    conn.commit()
    return True


def load_account_events() -> list[dict[str, Any]]:
    """The whole account change log, oldest first.

    ccreport reads this once per run and walks it to attribute each record to
    the account in force when the record was written. The adoption row, when
    there is one, is simply the oldest entry — attribution treats it like any
    other event, which is the whole trick.
    """
    conn = get_connection()
    return [
        _account_row_to_dict(row)
        for row in conn.execute(
            f"SELECT ts, {_ACCOUNT_SELECT} FROM account_events ORDER BY ts"  # noqa: S608
        )
    ]


def read_latest_account() -> dict[str, Any] | None:
    """The most recently captured account, or None if none was ever captured.

    Skips the adoption row. That row is a claim about history rather than a
    reading of who is signed in, and this is what `ccreport adopt` copies to
    build it — reading it back would let an adoption re-adopt itself and would
    report an empty capture log as if a real account had been seen.
    """
    conn = get_connection()
    row = conn.execute(
        f"SELECT ts, {_ACCOUNT_SELECT} FROM account_events "  # noqa: S608
        "WHERE ts > ? ORDER BY ts DESC LIMIT 1",
        (ADOPTED_TS,),
    ).fetchone()
    return _account_row_to_dict(row) if row else None


def read_adopted_account() -> dict[str, Any] | None:
    """The adoption row, or None when pre-capture history is left unattributed."""
    conn = get_connection()
    row = conn.execute(
        f"SELECT ts, {_ACCOUNT_SELECT} FROM account_events WHERE ts = ?",  # noqa: S608
        (ADOPTED_TS,),
    ).fetchone()
    return _account_row_to_dict(row) if row else None


def set_adopted_account(account: dict[str, Any]) -> None:
    """Point the adoption row at *account*, replacing any row already there.

    *account* is keyed by _ACCOUNT_COLS — a row dict as the readers here hand
    one back, not the camelCase oauthAccount blob record_account_event takes.
    Every column, tiers included, so this stays a plain write of whatever the
    caller decided the row should say; what a claim about history may honestly
    put in the tier columns is the caller's problem, not this function's.
    Unlike a capture this is meant to be overwritten: there is only ever one
    such row, and re-adopting is how a user corrects it.
    """
    conn = get_connection()
    conn.execute(
        f"INSERT OR REPLACE INTO account_events (ts, {_ACCOUNT_SELECT}) "  # noqa: S608
        f"VALUES ({_ACCOUNT_PLACEHOLDERS})",
        (ADOPTED_TS, *(account[col] for col in _ACCOUNT_COLS)),
    )
    conn.commit()


def clear_adopted_account() -> bool:
    """Delete the adoption row. Returns whether there was one to delete.

    The only DELETE this table has, and it can only reach the adoption row —
    captures are permanent history, and losing one silently mis-attributes
    every record after it.
    """
    conn = get_connection()
    cur = conn.execute("DELETE FROM account_events WHERE ts = ?", (ADOPTED_TS,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Rate limit utilization samples (written by the statusline render)
# ---------------------------------------------------------------------------

# Seconds a changed reading has to be apart from the stored one to land, within
# the same window instance. Two sessions rendering side by side read the same
# quota microseconds apart, so a value sitting on an integer boundary — 23.9
# here, 24.1 there — would otherwise write a row per render forever. A window
# fills over hours; nothing worth plotting happens inside five minutes.
_RL_SNAPSHOT_MIN_INTERVAL_S = 300

# How far ahead a reset time can plausibly sit. The longest window here is the
# 7-day one, so a reading claiming more than this is not describing a window:
# Claude Code has been seen sending resets_at = 9999999999 on stdin (a year-2286
# placeholder), and rows written before this check carry it permanently. The
# day of slack over seven is for a span quoted generously, not for a placeholder.
#
# Both ends read it from here: statusline._rl_sample refuses to store one, and
# `ccreport limits` drops the ones already stored. A reader that tolerated what
# the writer rejects would render a window resetting in 2286.
RL_MAX_LOOKAHEAD_S = 8 * 86_400


def rl_window_key(resets_at: float) -> float:
    """*resets_at* rounded to the whole minute — one window instance's identity.

    The usage API returns a float that drifts by up to a second between fetches
    of the same window (observed: 80 scoped rows spanning 1786305599.03 to
    1786305600.95, all of one reset at 1786305600). Both ends of this table
    treat resets_at as the window's identity — the write gate to decide whether
    a reading belongs to the window it already stored, `ccreport limits` to
    group samples into one fill curve — so that drift made every render look
    like a fresh window: the whole-percent gate never applied, and one week of
    scoped history became 80 single-sample instances.

    A minute because real resets land on one; anything finer is fetch latency.
    Rounded rather than truncated, else a reset at :00 splits across two buckets
    depending on which side of it the jitter fell.

    The writer normalizes before storing. The reader applies it again on read,
    because the rows written before this existed keep their jitter forever.
    """
    return round(resets_at / 60.0) * 60.0


# The stored column order, which is also the order the INSERT below spells out.
# Only load_rate_limit_snapshots binds it: it zips rows into dicts, so a column
# added to the table has to be added here or the reader silently drops it.
_RL_SNAPSHOT_COLS = ("ts", "window", "used_pct", "resets_at", "model", "source")


class RateLimitSample(NamedTuple):
    """One window's utilization as a single render read it.

    Named rather than a bare tuple because the two ends live in different files:
    used_pct and resets_at are both floats, so a swapped pair would store a
    plausible-looking row instead of failing.
    """

    window: str
    used_pct: float
    resets_at: float
    model: str | None
    source: str


def record_rate_limit_snapshots(
    samples: list[RateLimitSample], now: float,
) -> None:
    """Append the *samples* whose reading has actually moved.

    The caller is the statusline, on every render, offering every window it can
    see — so the unchanged case has to cost one SELECT per window and no write
    lock: (window, ts) is the primary key of a WITHOUT ROWID table, making
    "newest sample of this window" the first step of a reverse key scan.

    A sample lands when there is nothing stored for the window, when resets_at
    names a different window instance, or when the reading changed by a whole
    percent and _RL_SNAPSHOT_MIN_INTERVAL_S has passed. The whole-percent gate
    is what bounds one window instance at ~100 rows; the resets_at exception is
    there so a fresh window's first sample is not held back by it. used_pct
    stores the raw float — the gate rounds, the row does not.

    That bound holds only while every sample of one window carries the same
    resets_at, which is why the caller normalizes it (rl_window_key) rather than
    passing the reading through: an exact-float comparison against a drifting
    reset time takes the resets_at exception on every render and writes a row.

    No exception handling here: the render call site owns that, like every other
    bookkeeping write it makes.
    """
    conn = get_connection()
    wrote = False
    for window, used_pct, resets_at, model, source in samples:
        prior = conn.execute(
            "SELECT ts, used_pct, resets_at FROM rate_limit_snapshots "
            "WHERE window = ? ORDER BY ts DESC LIMIT 1",
            (window,),
        ).fetchone()
        if prior is not None:
            prior_ts, prior_pct, prior_resets = prior
            if (
                prior_resets == resets_at
                and (
                    round(used_pct) == round(prior_pct)
                    or now - prior_ts < _RL_SNAPSHOT_MIN_INTERVAL_S
                )
            ):
                continue
        # OR REPLACE covers two renders landing inside one tick of time.time():
        # that is the same instant, so the later reading is the one to keep.
        conn.execute(
            "INSERT OR REPLACE INTO rate_limit_snapshots "
            "(ts, window, used_pct, resets_at, model, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, window, float(used_pct), float(resets_at), model, source),
        )
        wrote = True
    # Guarded so the gated-out render leaves no doubt it took no write lock,
    # rather than relying on sqlite3 not having begun a transaction for SELECTs.
    if wrote:
        conn.commit()


def load_rate_limit_snapshots() -> list[dict[str, Any]]:
    """Every utilization sample ever taken, oldest first.

    Unbounded on purpose, and affordable: the write gate holds one window
    instance to ~100 rows, and `ccreport limits` groups by instance, so a LIMIT
    here would silently truncate the oldest instance rather than the report.

    Ordering by ts leaves the samples of one instance already in fill order.
    Window breaks the tie, which makes the order total — (window, ts) is the
    primary key, and every render offers every window at the same ts, so ts
    alone would leave a whole render's samples in whatever order the sort chose.
    """
    conn = get_connection()
    cols = ", ".join(_RL_SNAPSHOT_COLS)
    return [
        dict(zip(_RL_SNAPSHOT_COLS, row, strict=True))
        for row in conn.execute(
            f"SELECT {cols} FROM rate_limit_snapshots ORDER BY ts, window"  # noqa: S608
        )
    ]


# ---------------------------------------------------------------------------
# Cost summary cache (written by compute_costs, read by statusline)
# ---------------------------------------------------------------------------

def _cost_summary_suffix(cwd: str | None) -> str:
    """Project scope for the cost-summary keys.

    The writer and the reader must agree exactly: a divergence here is a
    permanent silent cache miss, not an error, so both read it from here.
    """
    return f":{project_key(cwd)}" if cwd else ""


def write_cost_summary(costs: dict[str, Any], cwd: str | None = None) -> None:
    """Cache the latest compute_costs() result for fast statusline reads.

    Scoped by project (cwd) to prevent cross-contamination between terminals.
    """
    conn = get_connection()
    suffix = _cost_summary_suffix(cwd)
    _set_meta(conn, f"cost_summary{suffix}", json.dumps(costs))
    _set_meta(conn, f"cost_summary_time{suffix}", str(time.time()))
    conn.commit()


def read_cost_summary(max_age: int = 600, cwd: str | None = None) -> dict[str, Any] | None:
    """Read cached cost summary if fresh enough, scoped by project.

    None once the stored summary is older than *max_age* seconds. Both keys are
    known up front, so they come back in one statement — the statusline is on
    the other end of this and reads it on every render.
    """
    suffix = _cost_summary_suffix(cwd)
    time_key = f"cost_summary_time{suffix}"
    data_key = f"cost_summary{suffix}"
    meta = _get_meta_many(get_connection(), (time_key, data_key))
    ts_str = meta.get(time_key)
    if not ts_str:
        return None
    try:
        if time.time() - float(ts_str) > max_age:
            return None
    except ValueError:
        return None
    raw = meta.get(data_key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------

def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _get_meta_many(
    conn: sqlite3.Connection, keys: tuple[str, ...],
) -> dict[str, str]:
    """Fetch several meta keys in one statement. Absent keys are simply missing.

    Callers that need a fixed set of keys — the lock path inside BEGIN
    IMMEDIATE, the statusline on every render — pay one round trip instead of
    one per key.
    """
    if not keys:
        return {}
    placeholders = ", ".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT key, value FROM meta WHERE key IN ({placeholders})",  # noqa: S608
        tuple(keys),
    ).fetchall()
    return dict(rows)


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )


# ---------------------------------------------------------------------------
# Exchange rates (Norges Bank USD/NOK daily spot, used by exchange.py)
# ---------------------------------------------------------------------------

def get_exchange_rates(since_date: str) -> dict[str, float]:
    """Cached rates from *since_date* (ISO ``YYYY-MM-DD``) on, as {date: rate}.

    The table gains a row per calendar day and is never pruned, so the caller
    passes the oldest date its lookups can still reach rather than reading the
    lot. `date` is the WITHOUT ROWID primary key, making the range a covering
    scan of just that slice.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT date, rate FROM exchange_rates WHERE date >= ?", (since_date,)
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def save_exchange_rates(rates: dict[str, float]) -> None:
    """Upsert {ISO date: rate} into the rate cache.

    Callers must validate before calling: a date present here is never
    re-fetched, so a stored rate is permanent.
    """
    if not rates:
        return
    conn = get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO exchange_rates (date, rate) VALUES (?, ?)",
        rates.items(),
    )
    conn.commit()
