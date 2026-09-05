"""The merged SQLite database: schema, connections and the whole-file write.

Separate from the client's cache.db on purpose, though both now sit in
~/.local/share/ccreport: that one is per machine and mostly re-derivable from
the JSONL logs beside it. This one is the only copy of what a laptop pushed
before its logs were rotated away.

Every column list here follows cache_db's rule: one tuple drives the CREATE
TABLE order, the SELECT text, the INSERT text and the row mapping, because a
column added to one of those and forgotten in the others is a format drift
nothing at runtime can catch.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ccreport import migrations, tier_timeline

MIGRATION_BASELINE = 3
"""The version the schema below leaves a database at, before any chain step.

Frozen. Every change from here on is an entry in `MIGRATION_CHAIN` further down,
which is also what moves SCHEMA_VERSION — a bump on its own re-runs the CREATE
script, and that script cannot add a column to a table it finds already there.
"""

_SCHEMA_SQL = """\
-- A machine is what a token maps to and what a record is attributed to. The
-- label is what the web UI shows and is free to change; machine_id is what
-- rows key on and never does. label_updated_at is when the label was last
-- typed, which content_stamp reads: a rename has no push behind it, and
-- without it the dashboard keeps drawing the old name until one arrives.
CREATE TABLE IF NOT EXISTS machines (
    machine_id       TEXT PRIMARY KEY,
    label            TEXT NOT NULL,
    first_seen       REAL NOT NULL,
    last_seen        REAL NOT NULL,
    label_updated_at REAL
) WITHOUT ROWID;

-- One token per machine, minted in the web UI and stored only as a hash: the
-- plaintext is shown once and never again. Ingest takes machine_id from the
-- row the presented hash matches, so a machine can write nobody else's records
-- and a revoked laptop stops writing on its next push. There is no shared
-- token, so there is nothing to rotate across the fleet when one leaks.
CREATE TABLE IF NOT EXISTS machine_tokens (
    token_hash   TEXT PRIMARY KEY,
    machine_id   TEXT NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked_at   REAL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_tokens_machine ON machine_tokens(machine_id);

-- The ccreport_records columns (cache_db.py:139) plus what only the server
-- knows. machine_id comes from the token, the account pair from the client
-- (a session log names no account, so the pusher stamps it on), and day and
-- oslo_date are computed at ingest because the machine's local zone is not
-- necessarily the server's.
--
-- cost is NOT NULL and is what the server's own pricing.py computed, so a
-- machine that has not pulled cannot write wrong money into the merged
-- history. log_cost keeps whatever cost the session log itself carried, so a
-- logged cost stays distinguishable from a computed one.
--
-- sid, project, cwd and repo are nullable where the client's are not: a
-- project a restricted machine has not opted in to pushes its token counts
-- with exactly those four stripped.
--
-- No unique constraint on dk. Two machines sharing a synced home directory
-- both keep their rows; reports collapse them by dedup key and attribute each
-- to the machine that reported it first, which is how a synced log neither
-- double-counts nor silently erases one machine's contribution.
--
-- dup is that collapse, decided once at write time instead of per query: 1 on
-- every row that is not the lowest id within its (account_uuid, dk), 0 on the
-- one that is and on every row the log gave no key. Half this table is dup=1 --
-- 431,439 rows of 815,841 when the flag was added -- and each read used to
-- rediscover which half by materialising 384,063 MIN(id) rows. Not in REC_COLS:
-- nothing pushes it, `_reconcile_dup` derives it, and it is the only column
-- here a client never sees.
CREATE TABLE IF NOT EXISTS server_records (
    id            INTEGER PRIMARY KEY,
    machine_id    TEXT NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL,
    account_uuid  TEXT NOT NULL,
    account_label TEXT,
    mid           TEXT,
    model         TEXT NOT NULL,
    ts            REAL NOT NULL,
    day           TEXT NOT NULL,
    oslo_date     TEXT NOT NULL,
    sid           TEXT,
    project       TEXT,
    cwd           TEXT,
    repo          TEXT,
    dk            TEXT,
    cost          REAL NOT NULL,
    log_cost      REAL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_create  INTEGER NOT NULL,
    cache_read    INTEGER NOT NULL,
    dup           INTEGER NOT NULL DEFAULT 0
);

-- Read-time dedup groups by (account, dedup key): the same call pushed from
-- two machines is one call billed once, and the account bounds the comparison
-- so two people's identical-looking keys never collapse into one.
CREATE INDEX IF NOT EXISTS idx_srec_account_dk ON server_records(account_uuid, dk);
-- What account_overview and account_names ask for: the newest label an account
-- pushed. Without it that scalar subquery reads every row of the account off
-- idx_srec_account_dk, which carries neither ts nor the label, and sorts them
-- in a temp b-tree to return one string -- 0.79s of a 2.13s page.
CREATE INDEX IF NOT EXISTS idx_srec_acct_ts
    ON server_records(account_uuid, ts DESC, account_label);
-- Every ingest deletes one file's rows before inserting them again, and the
-- machine leads because a file path is only unique within a machine.
CREATE INDEX IF NOT EXISTS idx_srec_file ON server_records(machine_id, file_path);
-- Every report and every dashboard range bounds itself in instants, and a
-- range narrower than the history is the common case. Without this the 7-day
-- toggle scans the same rows the all-time one does.
CREATE INDEX IF NOT EXISTS idx_srec_ts ON server_records(ts);

-- idx_srec_group is not here. It leads with dup, which a database written
-- before migration 10 does not have yet, and this script runs before the chain
-- that adds it -- so `_add_dup_column` creates the index as well. Its shape and
-- the reasoning behind it are there.

-- What each machine has already pushed, so a re-push of an unchanged file is a
-- no-op and a re-push of a grown file replaces that file's rows. A request
-- carries whole files and never part of one, which is what makes replacing
-- them wholesale correct.
CREATE TABLE IF NOT EXISTS ingest_files (
    machine_id TEXT NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
    file_path  TEXT NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    n_records  INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (machine_id, file_path)
) WITHOUT ROWID;

-- What this server calls an account. server_records.account_label holds the
-- login email the pushing machine stamped on, and a screenshot of the
-- dashboard leaks it; an alias renames every view the server draws without
-- rewriting a single stored record. An account with no row here renders as its
-- label, so clearing an alias is deleting the row.
CREATE TABLE IF NOT EXISTS account_aliases (
    account_uuid TEXT PRIMARY KEY,
    alias        TEXT NOT NULL,
    updated_at   REAL NOT NULL
) WITHOUT ROWID;

-- Which plan an account was on, and from when. Declared off the billing
-- receipts rather than pushed: a record carries no tier, and a client that
-- learned to send one could only stamp the files it still has — the logs
-- behind the older half have rotated away, and this table is the only copy of
-- what they spent. So the timeline is typed in here once and every record
-- resolves against it by timestamp, which reaches the whole corpus instead of
-- its recent end.
--
-- One row per change, keyed by the account and the moment: the entry in force
-- at a record's ts is the newest at or before it, exactly as account_events is
-- read on a client. A moment older than an account's first row has no tier,
-- which is the truth — the declaration starts where the receipts do.
CREATE TABLE IF NOT EXISTS account_tiers (
    account_uuid                 TEXT NOT NULL,
    from_ts                      REAL NOT NULL,
    seat_tier                    TEXT,
    user_rate_limit_tier         TEXT,
    organization_rate_limit_tier TEXT,
    updated_at                   REAL NOT NULL,
    PRIMARY KEY (account_uuid, from_ts)
) WITHOUT ROWID;

-- What this server calls a project. Two machines that checked the same repo
-- out under different names push two names, and the dashboard draws a row
-- each; typing one alias for both pairs folds them into one row. Keyed on the
-- machine as well as the name, so two machines that use one name for different
-- repos stay separable. A pair with no row here renders as the name it pushed.
CREATE TABLE IF NOT EXISTS project_aliases (
    machine_id TEXT NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
    project    TEXT NOT NULL,
    alias      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (machine_id, project)
) WITHOUT ROWID;

-- One machine's copy of its rate_limit_snapshots rows (cache_db.py). A window
-- is the account's, not the machine's, so two laptops drawing on one quota push
-- readings of the same window instance and the reports union them into one fill
-- curve — which is the whole point of holding them here rather than per machine.
--
-- The account pair comes from the client, as a record's does: a sample names no
-- account, and the change log that attributes it lives on the machine.
--
-- Keyed exactly as the client's table is, plus the machine: (window, ts) is
-- what the write gate holds one instance to ~100 rows under, so a re-push of a
-- sample already stored replaces it with itself. model is not in the key
-- because it is nullable, and the client has the same constraint.
CREATE TABLE IF NOT EXISTS rate_limit_samples (
    machine_id    TEXT NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
    window        TEXT NOT NULL,
    ts            REAL NOT NULL,
    used_pct      REAL NOT NULL,
    resets_at     REAL NOT NULL,
    model         TEXT,
    source        TEXT NOT NULL,
    account_uuid  TEXT NOT NULL,
    account_label TEXT,
    PRIMARY KEY (machine_id, window, ts)
) WITHOUT ROWID;

-- Every window page bounds itself in instants, the way every record page does.
CREATE INDEX IF NOT EXISTS idx_rl_ts ON rate_limit_samples(ts);

-- Extra-usage readings: cumulative dollars Anthropic billed as credits within a
-- billing month, as one machine's status line read them. The only real money in
-- a window report; everything else there is an API-price valuation.
--
-- Never pruned, unlike the client's own copy, which write_usage_cache holds to
-- 31 days. Past that this is the only copy there is, which makes the table a
-- database rather than a cache of one — the same answer ccreport_archive gives
-- on the client when the source is gone.
--
-- Keyed per machine even though the figure is the account's: two machines
-- signed into one account read the *same* dollars, so the reader picks one
-- machine's series per window rather than merging them. Merged, a machine whose
-- fetch lagged would report a lower figure after a higher one, and a drop in
-- this series is what says the billing month rolled over.
CREATE TABLE IF NOT EXISTS extra_usage_samples (
    machine_id    TEXT NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
    ts            REAL NOT NULL,
    spent         REAL NOT NULL,
    account_uuid  TEXT NOT NULL,
    account_label TEXT,
    PRIMARY KEY (machine_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_extra_ts ON extra_usage_samples(ts);

-- The same shape exchange.py already caches on every client, because the
-- server converts to NOK for all of them and reuses that module's Norges Bank
-- walk-back and negative cache rather than owning a second copy of either.
-- A rate of 0.0 is exchange._NO_OBSERVATION, not a rate.
CREATE TABLE IF NOT EXISTS exchange_rates (
    date TEXT PRIMARY KEY,
    rate REAL NOT NULL
) WITHOUT ROWID;
"""

_REC_SOURCE_COLS = ("machine_id", "file_path", "account_uuid", "account_label")
"""Where a record came from. Not on the client's record: it has one of each."""

_REC_FIELD_COLS = (
    "mid", "model", "ts", "day", "oslo_date", "sid", "project", "cwd", "repo", "dk", "cost", "log_cost",
)
_REC_TOKEN_COLS = ("input_tokens", "output_tokens", "cache_create", "cache_read")
REC_COLS = (*_REC_SOURCE_COLS, *_REC_FIELD_COLS, *_REC_TOKEN_COLS)
"""Every server_records column but the rowid, in CREATE TABLE order.

test_server_db asserts that order against PRAGMA table_info, so a column added
to the DDL alone, or to this tuple alone, fails there rather than silently
shifting every insert one place to the left.
"""

_REC_SELECT = ", ".join(REC_COLS)
_REC_PLACEHOLDERS = ", ".join("?" * len(REC_COLS))

# The token counts trail the rest so a record dict can keep them in one compact
# "t" list, as the client's does — every other column is read by column name.
_TOKEN_BASE = len(_REC_SOURCE_COLS) + len(_REC_FIELD_COLS)


def record_to_row(rec: dict) -> tuple:
    """A record dict as an insert row for REC_COLS."""
    return (
        *(rec.get(name) for name in _REC_SOURCE_COLS),
        *(rec.get(name) for name in _REC_FIELD_COLS),
        *rec["t"][:len(_REC_TOKEN_COLS)],
    )


def row_to_record(row: tuple) -> dict:
    """A REC_COLS row as a record dict, token counts folded back into "t"."""
    rec: dict = dict(zip(REC_COLS[:_TOKEN_BASE], row[:_TOKEN_BASE], strict=True))
    rec["t"] = list(row[_TOKEN_BASE:])
    return rec


_ACCOUNT_AT = REC_COLS.index("account_uuid")
_DK_AT = REC_COLS.index("dk")

DedupKey = tuple[str, str]
"""One (account_uuid, dk) pair: the grain the dedup flag is decided at."""

_RECONCILE_CHUNK = 1000
"""Keys per reconcile statement. Two parameters each, well under SQLite's
variable ceiling, and small enough that the VALUES list stays cheap to parse."""

_RECONCILE_SQL = """\
UPDATE server_records AS s
   SET dup = CASE WHEN s.id = w.min_id THEN 0 ELSE 1 END
  FROM (SELECT account_uuid, dk, MIN(id) AS min_id
          FROM server_records INDEXED BY idx_srec_account_dk
         WHERE (account_uuid, dk) IN (VALUES %s)
      GROUP BY account_uuid, dk) AS w
 WHERE s.account_uuid = w.account_uuid AND s.dk = w.dk
   AND s.dup <> CASE WHEN s.id = w.min_id THEN 0 ELSE 1 END
"""
"""One chunk of keys, re-flagged inside the write transaction a push holds.

INDEXED BY holds the inner search on (account_uuid, dk); without it the planner
searches idx_srec_acct_ts and reads the account's every row per chunk, since this
database refuses ANALYZE and has no statistics to say otherwise -- 1.52s against
0.37s for a 10,000-key file over 800,000 rows. The trailing comparison writes
only the rows whose flag moves, which takes that file to 0.02s: a re-push
re-decides thousands of keys and changes almost none, and each unchanged row it
wrote was a dirtied page and more WAL for a value that already stood.
"""


def _reconcile_dup(conn: sqlite3.Connection, keys: set[DedupKey]) -> None:
    """Re-decide the dedup flag for *keys* and nothing else.

    The lowest surviving id within a key wins, which is the rule every report
    read before the flag existed. Called with the keys a write touched, so the
    cost is the keys in one file rather than the table.

    Both directions matter. Inserting a second copy of a call makes it a loser,
    because a new row's id is above every surviving one -- SQLite hands out
    max(id)+1, and a delete only frees ids above what is left. Deleting the
    winner promotes whichever copy is next, which is why the caller collects the
    keys of the rows it is about to delete before deleting them.

    A key whose every row is gone has no row in *w* and is skipped: there is
    nothing left to flag.
    """
    ordered = sorted(keys)
    for start in range(0, len(ordered), _RECONCILE_CHUNK):
        chunk = ordered[start:start + _RECONCILE_CHUNK]
        values = ", ".join(["(?, ?)"] * len(chunk))
        conn.execute(_RECONCILE_SQL % values, [part for key in chunk for part in key])


def reconcile_all_dup(conn: sqlite3.Connection) -> None:
    """Re-decide the dedup flag for every keyed row in the table.

    The whole-table form of `_reconcile_dup`, for the two callers that cannot
    name their keys cheaply: the migration that fills the column, and a machine
    deletion, whose cascade can strip the winner from any key the machine ever
    pushed. Collecting those keys first would be hundreds of thousands of them
    and slower than this, which took 4.2s over 815,841 rows.

    A row the log gave no dedup key is 0: it stands for itself, as it always
    has. Cleared rather than assumed, because this is the form that has to
    answer for whatever it finds -- the incremental one never writes a 1 there,
    but this is what a caller reaches for when it does not trust the column.

    The trailing comparison writes only the rows whose flag moves, which is a
    machine deletion: the cascade shifts a handful of keys and the rest of the
    table already reads correctly, 0.86s against 3.60s over 800,000 rows. The
    migration that fills the column moves every row and pays the write either
    way.
    """
    conn.execute(
        "UPDATE server_records SET dup = 0 WHERE dup <> 0 AND (dk IS NULL OR dk = '')",
    )
    conn.execute("""
        UPDATE server_records AS s
           SET dup = CASE WHEN s.id = w.min_id THEN 0 ELSE 1 END
          FROM (SELECT account_uuid, dk, MIN(id) AS min_id
                  FROM server_records
                 WHERE dk IS NOT NULL AND dk <> ''
              GROUP BY account_uuid, dk) AS w
         WHERE s.account_uuid = w.account_uuid AND s.dk = w.dk
           AND s.dup <> CASE WHEN s.id = w.min_id THEN 0 ELSE 1 END
    """)


def _file_dedup_keys(conn: sqlite3.Connection, machine_id: str, file_path: str) -> set[DedupKey]:
    """The dedup keys one machine's file currently holds rows for."""
    return set(conn.execute(
        "SELECT DISTINCT account_uuid, dk FROM server_records "
        "WHERE machine_id = ? AND file_path = ? AND dk IS NOT NULL AND dk <> ''",
        (machine_id, file_path),
    ))


def _add_label_updated_at(conn: sqlite3.Connection) -> None:
    """Give an existing machines table the column content_stamp reads.

    A database this build created has it from the CREATE script and reaches
    here anyway, where a bare ALTER raises on the duplicate and takes the whole
    bootstrap down with it.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(machines)")]
    if "label_updated_at" not in cols:
        conn.execute("ALTER TABLE machines ADD COLUMN label_updated_at REAL")


_GROUP_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_srec_group ON server_records(
    dup, machine_id, account_uuid, account_label, project, model, day, oslo_date,
    ts, cost, input_tokens, output_tokens, cache_create, cache_read, dk, id)
"""
"""The index every uncached build folds the corpus over.

dup leads, so a report walks the surviving half and never sees the other one --
384,063 index entries instead of 815,841 on the database this was measured
against. reports.GROUP_COLS follows in order, so SQLite walks the groups instead
of sorting them into a temp b-tree, and behind those sit every column the
grouped query reads. dk and id are still among them: the dedup subquery is not
gone, it is what a project- or machine-filtered read still uses, and it is
answered off this index without leaving it.
"""


def _add_dup_column(conn: sqlite3.Connection) -> None:
    """Give server_records the dedup flag, fill it, and reshape the group index.

    Idempotent on the column, as `_add_label_updated_at` is: a database this
    build created has dup from the CREATE script and still reaches here, where a
    bare ALTER would raise on the duplicate and take the bootstrap down.

    The index is created here rather than in the CREATE script because it leads
    with a column that script cannot add -- the script runs first, and on a
    database written before this step dup does not exist yet. The old shape is
    dropped rather than left beside the new one: it indexes the same fourteen
    columns and would cost another 166 MB to keep.

    The backfill took 4.2s over 815,841 rows and the index 2.9s, both inside the
    transaction migrations.run holds.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(server_records)")]
    if "dup" not in cols:
        conn.execute("ALTER TABLE server_records ADD COLUMN dup INTEGER NOT NULL DEFAULT 0")
    conn.execute("DROP INDEX IF EXISTS idx_srec_group")
    reconcile_all_dup(conn)
    conn.execute(_GROUP_INDEX_SQL)


MIGRATION_CHAIN: tuple[migrations.Step, ...] = (
    migrations.Step(4, "machines.label_updated_at", _add_label_updated_at),
    migrations.Step(5, "project_aliases"),
    migrations.Step(6, "rate_limit_samples"),
    migrations.Step(7, "extra_usage_samples"),
    migrations.Step(8, "account_tiers"),
    migrations.Step(9, "idx_srec_group"),
    migrations.Step(10, "server_records.dup", _add_dup_column),
)
"""Every schema change since MIGRATION_BASELINE, in the order they are applied.

Append only, one version above the last. A step that adds a column is what
reaches a server database that already has the table — the CREATE script above
cannot, and this file is the only copy of a machine's records once its logs have
rotated. A change the CREATE script does cover, a new table or index, still needs
an entry here to move the version that re-runs it; `Step(N, "name")` with no
callable is that entry.
"""

SCHEMA_VERSION = migrations.head(MIGRATION_CHAIN, MIGRATION_BASELINE)
"""The version a fully migrated database is stamped at. Derived, never edited."""


class _ThreadConnection:
    """One thread's connection, closed when that thread ends.

    A worker thread that exits drops its threading.local storage, and the
    connection goes with it -- finalized by the GC rather than closed. CPython
    frees that storage on the dying thread, which is the only thread sqlite3
    lets close it.
    """

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __del__(self) -> None:
        # sqlite3 refuses a close from a thread that did not open the connection.
        # A raise here would print an unraisable traceback in place of the
        # warning this exists to remove.
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


class Database:
    """One connection per thread over one server database file.

    FastAPI runs a sync endpoint on a worker thread from a pool, and a sqlite3
    connection may not cross threads. A connection per thread is bounded by the
    pool and costs one open per worker for the process's life; check_same_thread
    stays on, so a connection that does leak across threads raises here rather
    than corrupting a write.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        """The calling thread's connection, opened on its first use."""
        held = getattr(self._local, "held", None)
        if held is None:
            held = self._local.held = _ThreadConnection(connect(self.path))
        return held.conn

    def close(self) -> None:
        """Close this thread's connection, if it has one.

        Only this thread's: sqlite3 refuses a close from anywhere else. Another
        thread's connection is closed by _ThreadConnection when that thread ends.
        """
        held = getattr(self._local, "held", None)
        if held is not None:
            held.conn.close()
            self._local.held = None



MMAP_SIZE = 512 * 1024 * 1024
"""How much of the database each connection reads through a memory map.

Enough to hold every index -- 388 MB of the 774 MB this was measured against,
and where a report's reads land. It took the accounts page from 2.16s to 1.40s.

The page cache is deliberately left at SQLite's 2 MB default beside it. Raising
that to 64 MB on top of the map bought 0.08s, and it is per connection where the
map is not: a map is the host's own page cache addressed directly, so N worker
threads share one copy of a page, while N page caches are N copies on a server
with 5 GB of memory.

An I/O error against a mapped page arrives as SIGBUS rather than as a return
code, which is the trade: a local disk failing takes the process down instead of
raising. Nothing here is on a network filesystem.
"""

BUSY_TIMEOUT_MS = 5000
"""How long a statement waits for another connection's write before it raises.

sqlite3's own default, set here so the number is readable and so `truncate_wal`
has something to restore. A push arrives as one request per file and a write
takes milliseconds, so a wait this long means something is wrong rather than
busy -- and the request that hits the ceiling 500s, which the client retries on
its next interval.
"""

WAL_TRUNCATE_BYTES = 64 * 1024 * 1024
"""How large the WAL may grow before a checkpoint truncates it.

The automatic checkpoint copies pages back but never shortens the file, and the
server's connections are long-lived, so a reader is holding the WAL open often
enough that it is rarely reset either: it reached 204 MB against a 774 MB
database, all of it already checkpointed. Truncation needs a moment with no
reader, which is why it is attempted after an ingest and its failure ignored.
"""


def truncate_wal(conn: sqlite3.Connection, path: Path) -> bool:
    """Shorten an oversized WAL, and say whether it went.

    False covers both halves of "not now": under the threshold, or a reader
    holding the file open so the checkpoint could not reset it. Neither is worth
    reporting -- the next ingest asks again, and the WAL is correct at any size.
    """
    wal = path.with_name(path.name + "-wal")
    try:
        if wal.stat().st_size < WAL_TRUNCATE_BYTES:
            return False
    except OSError:
        return False
    # A TRUNCATE checkpoint holds the writer lock while it waits for the readers
    # it needs gone, so on the usual timeout it blocks every other push for five
    # seconds and then gives up regardless: 5.769s against 0.000s with one report
    # mid-query, and 0.008s where the moment really was free.
    conn.execute("PRAGMA busy_timeout = 0")
    try:
        # (busy, pages in the WAL, pages moved back). Only the first says whether
        # the file was reset; the other two describe a checkpoint that ran either way.
        return conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def connect(path: Path) -> sqlite3.Connection:
    """Open *path*, creating the schema when this build has not stamped it yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None turns off the implicit BEGIN sqlite3 otherwise opens
    # before a DML statement and holds until commit(). The whole-file write
    # below issues its own BEGIN IMMEDIATE, and an implicit transaction already
    # in progress makes that a "cannot start a transaction within a
    # transaction" error the moment anything wrote first.
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA mmap_size = {MMAP_SIZE}")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            # journal_mode lives in the database header, so unlike the pragmas
            # above it only needs setting on a file this build has not opened.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA_SQL)
            # Creates what is missing, then steps a database that already had
            # those tables up to the same shape. The stamp is its doing: it goes
            # on with the last step, so a crash mid-chain resumes rather than
            # recording a schema that is not there.
            migrations.run(
                conn, chain=MIGRATION_CHAIN, baseline=MIGRATION_BASELINE, db_path=path,
            )
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def upsert_machine(conn: sqlite3.Connection, machine_id: str, label: str, now: float) -> None:
    """Record that *machine_id* exists and was heard from at *now*.

    The label is left alone on a machine already known: it is what the web UI
    shows and is edited there, so a push must not overwrite it with whatever
    hostname the laptop happens to report this week.
    """
    conn.execute(
        "INSERT INTO machines (machine_id, label, first_seen, last_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(machine_id) DO UPDATE SET last_seen = excluded.last_seen",
        (machine_id, label, now, now),
    )


def machine_for_token(conn: sqlite3.Connection, token_hash: str) -> str | None:
    """The machine a presented token hash authenticates, or None.

    None covers both an unknown token and a revoked one, deliberately: telling
    the two apart tells a caller holding a wrong token that it once was right.
    """
    row = conn.execute(
        "SELECT machine_id FROM machine_tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (token_hash,),
    ).fetchone()
    return row[0] if row else None


def touch_token(conn: sqlite3.Connection, token_hash: str, now: float) -> None:
    """Stamp a token as used. Every authenticated request does it, health checks included."""
    conn.execute(
        "UPDATE machine_tokens SET last_used_at = ? WHERE token_hash = ?", (now, token_hash),
    )


def revoke_token(conn: sqlite3.Connection, token_hash: str, now: float) -> bool:
    """Revoke a token. False when there was no live token by that hash.

    Revoking an already-revoked token keeps the first revocation time: when it
    stopped working is a fact, and re-stamping it would lose it.
    """
    cur = conn.execute(
        "UPDATE machine_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
        (now, token_hash),
    )
    return cur.rowcount > 0


def delete_token(conn: sqlite3.Connection, token_hash: str) -> bool:
    """Remove a token's row. False when there was no row by that hash.

    Apart from revoke on purpose: revoking is what you do to a machine still
    out there and keeps when it stopped working, deleting is what you do to a
    token minted by mistake, which has no history worth the row.
    """
    return conn.execute(
        "DELETE FROM machine_tokens WHERE token_hash = ?", (token_hash,),
    ).rowcount > 0


def delete_machine(conn: sqlite3.Connection, machine_id: str) -> int:
    """Remove a machine and return how many records went with it.

    Its tokens, its ingest_files and its server_records follow through the
    ON DELETE CASCADE the three declare, which is why the count is read first:
    afterwards there is nothing left to count.

    The cascade can strip the winning copy from any dedup key this machine ever
    pushed, so the flag is re-decided across the table afterwards. Every other
    machine's copy of a shared call is promoted here rather than silently
    vanishing from every report along with the machine that reported it first.
    """
    destroyed = record_count(conn, machine_id)
    conn.execute("DELETE FROM machines WHERE machine_id = ?", (machine_id,))
    reconcile_all_dup(conn)
    return destroyed


def machine_overview(conn: sqlite3.Connection) -> list[dict]:
    """Every machine with its token state and how much it has stored.

    The record count is raw: it is what this machine pushed, which is also what
    deleting the machine takes away. A call two machines both logged counts on
    each of them here, where every report collapses it to one — the page says
    "Pushed" for that reason.

    One query rather than a count per machine: the page is a table and the
    counts are what it is opened to compare.
    """
    rows = conn.execute("""
        SELECT m.machine_id, m.label, m.first_seen, m.last_seen,
               (SELECT COUNT(*) FROM server_records r WHERE r.machine_id = m.machine_id),
               (SELECT COUNT(*) FROM machine_tokens t
                 WHERE t.machine_id = m.machine_id AND t.revoked_at IS NULL),
               (SELECT MAX(t.last_used_at) FROM machine_tokens t WHERE t.machine_id = m.machine_id)
          FROM machines m
      ORDER BY m.last_seen DESC
    """).fetchall()
    return [
        {
            "machine_id": row[0], "label": row[1], "first_seen": row[2], "last_seen": row[3],
            "records": row[4], "active_tokens": row[5], "last_used_at": row[6],
        }
        for row in rows
    ]


def machine_tokens(conn: sqlite3.Connection, machine_id: str) -> list[dict]:
    """A machine's tokens, newest first, for the revoke buttons."""
    rows = conn.execute(
        "SELECT token_hash, created_at, last_used_at, revoked_at FROM machine_tokens "
        "WHERE machine_id = ? ORDER BY created_at DESC",
        (machine_id,),
    ).fetchall()
    return [
        {"token_hash": r[0], "created_at": r[1], "last_used_at": r[2], "revoked_at": r[3]}
        for r in rows
    ]


def set_machine_label(conn: sqlite3.Connection, machine_id: str, label: str, now: float) -> bool:
    """Rename a machine. False when nothing was minted under *machine_id*.

    A blank *label* stores the machine_id itself rather than an empty string.
    Every reader falls back to the id for a machine it has no label for, but a
    row holding "" is a label, and the dashboard would draw a machine with no
    name at all.

    The stamp goes on with it, because a rename changes what every view draws
    and no push follows it.
    """
    return conn.execute(
        "UPDATE machines SET label = ?, label_updated_at = ? WHERE machine_id = ?",
        (label.strip() or machine_id, now, machine_id),
    ).rowcount > 0


def account_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    """Every alias set, keyed by account uuid.

    Read once per report and handed to the row builders, rather than looked up
    per record: a merged corpus is one query's worth of accounts and hundreds of
    thousands of records.
    """
    return dict(conn.execute("SELECT account_uuid, alias FROM account_aliases").fetchall())


def accounts_with_alias(conn: sqlite3.Connection, alias: str | None) -> tuple[str, ...]:
    """The accounts *alias* names, so a filter typed as an alias matches rows.

    A tuple rather than one uuid: nothing stops two accounts being given the
    same alias, and a filter that silently picked one of them would report half
    the spend.
    """
    if not alias:
        return ()
    rows = conn.execute(
        "SELECT account_uuid FROM account_aliases WHERE alias = ?", (alias,),
    ).fetchall()
    return tuple(row[0] for row in rows)


def set_account_alias(conn: sqlite3.Connection, account_uuid: str, alias: str, now: float) -> None:
    """Name *account_uuid*, or clear its name when *alias* is blank."""
    alias = alias.strip()
    if not alias:
        conn.execute("DELETE FROM account_aliases WHERE account_uuid = ?", (account_uuid,))
        return
    conn.execute(
        "INSERT INTO account_aliases (account_uuid, alias, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(account_uuid) DO UPDATE SET "
        "alias = excluded.alias, updated_at = excluded.updated_at",
        (account_uuid, alias, now),
    )


def account_tiers(
    conn: sqlite3.Connection, account_uuid: str | None = None,
) -> list[tier_timeline.Entry]:
    """Every declared plan change, as the entries a TierTimeline is built from.

    Read once per report and handed to the row builders, for the reason
    account_aliases is: a merged corpus is one query's worth of plan changes
    and hundreds of thousands of records.

    *account_uuid* narrows it to one account, which is what a pull sends back:
    a machine is signed in to one account and another's plan history is not
    its to hold.
    """
    where = " WHERE account_uuid = ?" if account_uuid is not None else ""
    rows = conn.execute(
        "SELECT account_uuid, from_ts, seat_tier, user_rate_limit_tier, "  # noqa: S608
        f"organization_rate_limit_tier FROM account_tiers{where} "
        "ORDER BY account_uuid, from_ts",
        (account_uuid,) if account_uuid is not None else (),
    ).fetchall()
    return [
        tier_timeline.Entry(
            ts=row[1], account=row[0], seat_tier=row[2],
            user_rate_limit_tier=row[3], organization_rate_limit_tier=row[4],
        )
        for row in rows
    ]


def set_account_tiers(
    conn: sqlite3.Connection, account_uuid: str,
    entries: list[tier_timeline.Entry], now: float,
) -> None:
    """Replace *account_uuid*'s declared timeline with *entries*.

    Wholesale, not row by row. The timeline is one document a person reads off
    their receipts and re-pastes when they find a receipt they had missed, and
    merging the new text into the old would leave a change they deleted still
    standing — with nothing on the page to show it was there.

    Only this account's rows are touched, so one account's paste cannot drop
    another's.
    """
    conn.execute("DELETE FROM account_tiers WHERE account_uuid = ?", (account_uuid,))
    conn.executemany(
        "INSERT INTO account_tiers (account_uuid, from_ts, seat_tier, "
        "user_rate_limit_tier, organization_rate_limit_tier, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (account_uuid, e.ts, e.seat_tier, e.user_rate_limit_tier,
             e.organization_rate_limit_tier, now)
            for e in entries
        ],
    )


def project_aliases(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Every project alias set, keyed by the (machine, pushed name) pair.

    Read once per report and handed to the row builders, for the reason
    account_aliases is: a merged corpus is one query's worth of projects and
    hundreds of thousands of records.
    """
    rows = conn.execute("SELECT machine_id, project, alias FROM project_aliases").fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def projects_with_alias(conn: sqlite3.Connection, alias: str | None) -> tuple[tuple[str, str], ...]:
    """The (machine, project) pairs *alias* names, so a filter typed as an alias matches.

    Several pairs by design: folding two machines' names into one is what the
    table is for, and a filter that picked one of them would report half the
    spend.
    """
    if not alias:
        return ()
    rows = conn.execute(
        "SELECT machine_id, project FROM project_aliases WHERE alias = ?", (alias,),
    ).fetchall()
    return tuple((row[0], row[1]) for row in rows)


def set_project_alias(
    conn: sqlite3.Connection, machine_id: str, project: str, alias: str, now: float,
) -> None:
    """Name one machine's project, or clear the name when *alias* is blank."""
    alias = alias.strip()
    if not alias:
        conn.execute(
            "DELETE FROM project_aliases WHERE machine_id = ? AND project = ?",
            (machine_id, project),
        )
        return
    conn.execute(
        "INSERT INTO project_aliases (machine_id, project, alias, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(machine_id, project) DO UPDATE SET "
        "alias = excluded.alias, updated_at = excluded.updated_at",
        (machine_id, project, alias, now),
    )


def project_exists(conn: sqlite3.Connection, machine_id: str, project: str) -> bool:
    """Whether that machine has pushed a record under that project name.

    What the rename route checks before storing anything: a pair nobody pushed
    is a mistyped form, and its row would sit in the table naming nothing while
    the page that lists pushed pairs never draws it.
    """
    return conn.execute(
        "SELECT 1 FROM server_records WHERE machine_id = ? AND project = ? LIMIT 1",
        (machine_id, project),
    ).fetchone() is not None


def record_count(conn: sqlite3.Connection, machine_id: str) -> int:
    """How many records the server holds for one machine."""
    return conn.execute(
        "SELECT COUNT(*) FROM server_records WHERE machine_id = ?", (machine_id,),
    ).fetchone()[0]


def oldest_record_ts(conn: sqlite3.Connection) -> float | None:
    """When the earliest stored record was made, or None on an empty database.

    Where the dashboard's all-time range starts.
    """
    return conn.execute("SELECT MIN(ts) FROM server_records").fetchone()[0]


def content_stamp(conn: sqlite3.Connection) -> tuple:
    """A value that moves whenever a render's input changed.

    Read from ingest_files rather than from server_records: every write path
    goes through replace_file_records, which stamps a row here in the same
    transaction, and this table is thousands of rows where that one is
    hundreds of thousands. Three parts, because no single one covers every
    edit — a re-push of one file moves updated_at, a machine dropped by a
    cascade moves the count, and a file that shrank moves the record total.

    account_aliases rides along because it renames what every view draws
    without a push behind it: without its two parts here the dashboard keeps
    showing the email until the next machine pushes. Setting or re-setting an
    alias moves the max, clearing one moves the count.

    machines.label_updated_at is the same case for a renamed machine, and needs
    one part rather than two: a rename writes the stamp whether it names the
    machine or clears it back to the id, and the row itself outlives both.

    project_aliases is account_aliases' case again, down to needing both parts:
    the row is created and deleted by the same field, so a cleared name moves
    the count where a re-typed one moves the max.

    rate_limit_samples is here because a push can carry samples and no file at
    all — a machine whose logs have not changed still renders, and the windows
    it watched moved. Both parts, because a machine deleted by a cascade takes
    its samples with it and moves only the count. extra_usage_samples arrives
    the same way and prices the Extra column beside them.

    account_tiers is account_aliases' case a third time: a timeline is typed in
    with no push behind it, and it changes the tier every folded row carries.
    Both parts, because re-pasting a shorter document moves only the count.

    A rate arriving in exchange_rates is deliberately not in it: nothing here
    would notice a rate updated in place, and the NOK column it moves is
    re-derived on the next push or the next day anyway.
    """
    return conn.execute("""
        SELECT COUNT(*), COALESCE(MAX(updated_at), 0), COALESCE(SUM(n_records), 0),
               (SELECT COUNT(*) FROM account_aliases),
               (SELECT COALESCE(MAX(updated_at), 0) FROM account_aliases),
               (SELECT COALESCE(MAX(label_updated_at), 0) FROM machines),
               (SELECT COUNT(*) FROM project_aliases),
               (SELECT COALESCE(MAX(updated_at), 0) FROM project_aliases),
               (SELECT COUNT(*) FROM rate_limit_samples),
               (SELECT COALESCE(MAX(ts), 0) FROM rate_limit_samples),
               (SELECT COUNT(*) FROM extra_usage_samples),
               (SELECT COALESCE(MAX(ts), 0) FROM extra_usage_samples),
               (SELECT COUNT(*) FROM account_tiers),
               (SELECT COALESCE(MAX(updated_at), 0) FROM account_tiers)
          FROM ingest_files
    """).fetchone()


RL_SAMPLE_COLS = (
    "machine_id", "window", "ts", "used_pct", "resets_at", "model", "source",
    "account_uuid", "account_label",
)
"""Every rate_limit_samples column, in CREATE TABLE order. One tuple drives the
INSERT, the SELECT and the row mapping, for the reason REC_COLS does."""

_RL_SELECT = ", ".join(RL_SAMPLE_COLS)
_RL_PLACEHOLDERS = ", ".join("?" * len(RL_SAMPLE_COLS))


def store_rate_limit_samples(
    conn: sqlite3.Connection, machine_id: str, samples: list[dict],
) -> int:
    """Store one machine's utilization samples, and say how many landed.

    REPLACE rather than IGNORE: a sample the client re-sends after a --full is
    the same reading of the same window at the same instant, and the newer copy
    carries whatever account the machine has since learned to attribute it to.

    No file to key on and nothing to delete first — a sample is a row of its
    own, written once and never edited, so there is no wholesale replace to make
    atomic the way a file's records need.
    """
    if not samples:
        return 0
    rows = [
        tuple(sample.get(name) for name in RL_SAMPLE_COLS[1:])
        for sample in samples
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO rate_limit_samples ({_RL_SELECT}) "  # noqa: S608
            f"VALUES ({_RL_PLACEHOLDERS})",
            [(machine_id, *row) for row in rows],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def load_rate_limit_samples(
    conn: sqlite3.Connection, since: float | None = None, until: float | None = None,
) -> list[dict]:
    """Every stored sample the bounds admit, oldest first, machine label attached.

    Ordered by ts then window, as the client's reader is, so the samples of one
    window instance arrive in fill order however many machines reported them.
    The machine's label rides along because a merged fill curve draws a trace
    per machine and a machine_id is not a name anyone typed.
    """
    clauses, params = [], []
    if since is not None:
        clauses.append("s.ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("s.ts < ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    cols = ", ".join(f"s.{name}" for name in RL_SAMPLE_COLS)
    rows = conn.execute(
        f"SELECT {cols}, COALESCE(m.label, s.machine_id) FROM rate_limit_samples s "  # noqa: S608
        f"LEFT JOIN machines m ON m.machine_id = s.machine_id{where} "
        "ORDER BY s.ts, s.window",
        params,
    ).fetchall()
    samples: list[dict] = []
    for row in rows:
        sample: dict = dict(zip(RL_SAMPLE_COLS, row[:len(RL_SAMPLE_COLS)], strict=True))
        sample["machine"] = row[-1]
        samples.append(sample)
    return samples


EXTRA_SAMPLE_COLS = ("machine_id", "ts", "spent", "account_uuid", "account_label")
"""Every extra_usage_samples column, in CREATE TABLE order."""

_EXTRA_SELECT = ", ".join(EXTRA_SAMPLE_COLS)
_EXTRA_PLACEHOLDERS = ", ".join("?" * len(EXTRA_SAMPLE_COLS))


def store_extra_samples(
    conn: sqlite3.Connection, machine_id: str, samples: list[dict],
) -> int:
    """Store one machine's Extra-usage readings, and say how many landed.

    REPLACE for the reason store_rate_limit_samples uses it: a reading re-sent
    after a --full is the same reading of the same instant, and the newer copy
    carries whatever account the machine has since learned to attribute it to.
    """
    if not samples:
        return 0
    rows = [
        tuple(sample.get(name) for name in EXTRA_SAMPLE_COLS[1:])
        for sample in samples
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO extra_usage_samples ({_EXTRA_SELECT}) "  # noqa: S608
            f"VALUES ({_EXTRA_PLACEHOLDERS})",
            [(machine_id, *row) for row in rows],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def load_extra_samples(
    conn: sqlite3.Connection, since: float | None = None, until: float | None = None,
) -> dict[tuple[str, str | None, str], list[tuple[float, float]]]:
    """(account_uuid, account_label, machine_id) -> `(ts, spent)`, oldest first.

    The label rides in the key because the caller names an account the way the
    window rows do — alias, then the label the push carried, then the uuid — and
    a uuid alone would key the readings under a name no row uses.

    Grouped per machine rather than handed over as one series, because the
    figure is cumulative account spend and every machine on the account reports
    the same dollars. The reader picks one machine per window; merging them
    would let a lagging reading read as the monthly reset.

    The bounds are widened by the caller, not here: a range needs a reading at
    or before its start to subtract from, and that reading is older than the
    range.
    """
    clauses, params = [], []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts < ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    series: dict[tuple[str, str | None, str], list[tuple[float, float]]] = {}
    for account, label, machine, ts, spent in conn.execute(
        "SELECT account_uuid, account_label, machine_id, ts, spent "  # noqa: S608
        f"FROM extra_usage_samples{where} ORDER BY ts",
        params,
    ):
        series.setdefault((account, label, machine), []).append((ts, spent))
    return series


def oldest_sample_ts(conn: sqlite3.Connection) -> float | None:
    """When the oldest stored sample was taken, or None where there are none."""
    return conn.execute("SELECT MIN(ts) FROM rate_limit_samples").fetchone()[0]


def machine_label(conn: sqlite3.Connection, machine_id: str) -> str | None:
    row = conn.execute(
        "SELECT label FROM machines WHERE machine_id = ?", (machine_id,),
    ).fetchone()
    return row[0] if row else None


def file_fingerprint(conn: sqlite3.Connection, machine_id: str, file_path: str) -> tuple[int, int] | None:
    """The (mtime_ns, size) last ingested for a machine's file, or None."""
    row = conn.execute(
        "SELECT mtime_ns, size FROM ingest_files WHERE machine_id = ? AND file_path = ?",
        (machine_id, file_path),
    ).fetchone()
    return (row[0], row[1]) if row else None


def replace_file_records(
    conn: sqlite3.Connection,
    machine_id: str,
    file_path: str,
    mtime_ns: int,
    size: int,
    rows: list[tuple],
    now: float,
) -> None:
    """Store one file's records, replacing whatever that file held before.

    One transaction per file, which is the grain a push request carries: a
    partial file would leave the merged history with half a session in it and
    no fingerprint able to say so.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Before the delete, because afterwards there is nothing left to read
        # them off: a key this file was the only holder of a copy for still has
        # copies elsewhere, and one of those is about to become the winner.
        keys = _file_dedup_keys(conn, machine_id, file_path)
        conn.execute(
            "DELETE FROM server_records WHERE machine_id = ? AND file_path = ?",
            (machine_id, file_path),
        )
        conn.executemany(
            f"INSERT INTO server_records ({_REC_SELECT}) VALUES ({_REC_PLACEHOLDERS})",  # noqa: S608
            rows,
        )
        keys.update(
            (row[_ACCOUNT_AT], row[_DK_AT]) for row in rows if row[_DK_AT]
        )
        _reconcile_dup(conn, keys)
        conn.execute(
            "INSERT INTO ingest_files (machine_id, file_path, mtime_ns, size, n_records, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(machine_id, file_path) DO UPDATE SET "
            "mtime_ns = excluded.mtime_ns, size = excluded.size, "
            "n_records = excluded.n_records, updated_at = excluded.updated_at",
            (machine_id, file_path, mtime_ns, size, len(rows), now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def load_file_records(conn: sqlite3.Connection, machine_id: str, file_path: str) -> list[dict]:
    """Every record stored for one machine's file, as record dicts."""
    rows = conn.execute(
        f"SELECT {_REC_SELECT} FROM server_records "  # noqa: S608
        f"WHERE machine_id = ? AND file_path = ? ORDER BY id",
        (machine_id, file_path),
    ).fetchall()
    return [row_to_record(row) for row in rows]


class RateStore:
    """exchange.py's two storage calls, aimed at the server database.

    exchange.py owns the Norges Bank walk-back, the plausibility check and the
    negative cache, and none of that should exist twice. What it does not own
    is which database the rows land in, so that part is passed in.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def get_exchange_rates(self, since_date: str) -> dict[str, float]:
        rows = self._db.connect().execute(
            "SELECT date, rate FROM exchange_rates WHERE date >= ?", (since_date,),
        ).fetchall()
        return dict(rows)

    def save_exchange_rates(self, rates: dict[str, float]) -> None:
        conn = self._db.connect()
        conn.executemany(
            "INSERT OR REPLACE INTO exchange_rates (date, rate) VALUES (?, ?)",
            list(rates.items()),
        )
        conn.commit()
