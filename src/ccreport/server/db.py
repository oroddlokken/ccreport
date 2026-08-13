"""The merged SQLite database: schema, connections and the whole-file write.

Separate from the client's ~/.cache/ccreport/cache.db on purpose. That one is a
cache — delete it and the next run rebuilds it from the JSONL logs still on that
machine. This one is the only copy of what a laptop pushed before its logs were
rotated away, so it is a database, kept where a backup would find it.

Every column list here follows cache_db's rule: one tuple drives the CREATE
TABLE order, the SELECT text, the INSERT text and the row mapping, because a
column added to one of those and forgotten in the others is a format drift
nothing at runtime can catch.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 2
"""Stamped into PRAGMA user_version once the schema below has been applied.

BUMP THIS on any change to _SCHEMA_SQL — an existing database is otherwise
never reopened on the slow path and never sees the new DDL.
"""

_SCHEMA_SQL = """\
-- A machine is what a token maps to and what a record is attributed to. The
-- label is what the web UI shows and is free to change; machine_id is what
-- rows key on and never does.
CREATE TABLE IF NOT EXISTS machines (
    machine_id TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
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
    cache_read    INTEGER NOT NULL
);

-- Read-time dedup groups by (account, dedup key): the same call pushed from
-- two machines is one call billed once, and the account bounds the comparison
-- so two people's identical-looking keys never collapse into one.
CREATE INDEX IF NOT EXISTS idx_srec_account_dk ON server_records(account_uuid, dk);
-- Every ingest deletes one file's rows before inserting them again, and the
-- machine leads because a file path is only unique within a machine.
CREATE INDEX IF NOT EXISTS idx_srec_file ON server_records(machine_id, file_path);
-- Every report and every dashboard range bounds itself in instants, and a
-- range narrower than the history is the common case. Without this the 7-day
-- toggle scans the same rows the all-time one does.
CREATE INDEX IF NOT EXISTS idx_srec_ts ON server_records(ts);

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
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = connect(self.path)
        return conn

    def close(self) -> None:
        """Close this thread's connection, if it has one.

        Only this thread's: sqlite3 refuses a close from anywhere else, and the
        pool's other connections are released when the process exits.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


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
        conn.execute("PRAGMA synchronous = NORMAL")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            # journal_mode lives in the database header, so unlike the pragmas
            # above it only needs setting on a file this build has not opened.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
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
    """Stamp a token as used, which is what the machines page reports as last seen."""
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


def machine_overview(conn: sqlite3.Connection) -> list[dict]:
    """Every machine with its token state and how much it has stored.

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
    """A value that moves whenever a push changed what the records hold.

    Read from ingest_files rather than from server_records: every write path
    goes through replace_file_records, which stamps a row here in the same
    transaction, and this table is thousands of rows where that one is
    hundreds of thousands. Three parts, because no single one covers every
    edit — a re-push of one file moves updated_at, a machine dropped by a
    cascade moves the count, and a file that shrank moves the record total.

    A rate arriving in exchange_rates is deliberately not in it: nothing here
    would notice a rate updated in place, and the NOK column it moves is
    re-derived on the next push or the next day anyway.
    """
    return conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), 0), COALESCE(SUM(n_records), 0) FROM ingest_files",
    ).fetchone()


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
        conn.execute(
            "DELETE FROM server_records WHERE machine_id = ? AND file_path = ?",
            (machine_id, file_path),
        )
        conn.executemany(
            f"INSERT INTO server_records ({_REC_SELECT}) VALUES ({_REC_PLACEHOLDERS})",  # noqa: S608
            rows,
        )
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
