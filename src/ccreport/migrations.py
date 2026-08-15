"""Versioned schema migrations for the two SQLite databases in this project.

Both `cache_db.py` and `server/db.py` bootstrap with a CREATE ... IF NOT EXISTS
script gated on `PRAGMA user_version`. That script reaches an existing database
with a new table or a new index and nothing else: a column added to a table that
already exists is skipped by the very IF NOT EXISTS that makes the script safe to
re-run. cache.db can be rebuilt from the JSONL logs when that goes wrong; the
server database is the only copy of a machine's records once its logs rotate.

A chain of numbered steps is what carries the rest. Each step is a Python
callable rather than a .sql file because these migrations reason about data —
`cache_db`'s flat-pricing repair matched model names by substring, which SQL
cannot express — and because a step shipped inside the package cannot go missing
the way a data directory can.

Numbering continues from each database's existing stamp: the CREATE script, the
ADD COLUMN list and the meta-flagged repairs in `cache_db._run_migrations` are
the frozen pre-baseline bootstrap, and the chain starts one above the version
they leave behind. Nothing renumbers them, so no database in the wild needs its
stamp translated.

Forward only. There are no down steps: neither database has a caller that would
run one, and a rollback that has to be written before the failure it undoes is
known is a guess that runs against real data.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LOCK_SUFFIX = ".migration-lock"
"""Appended to the database path to name the lock file beside it."""

# 0600, like the databases: the lock carries no content, and its mtime is the
# only trace of when a migration last ran.
_LOCK_MODE = 0o600

LOCK_TIMEOUT_S = 60.0
"""How long a run waits for the lock when the caller names no bound of its own.

Far longer than these steps take, and short enough that a hung holder reaches
whoever is watching as a message rather than as a process that never returns.
`cache_db` passes its own, shorter bound: a status line render that waits here is
a frame that does not draw, and one stale figure beats a blank line.
"""

# flock has no timed wait, so the deadline is enforced by polling a non-blocking
# one. Twenty wakeups a second, on a path taken once per schema change.
_LOCK_POLL_S = 0.05

_LOG_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at REAL NOT NULL,
    source_sha TEXT
);
"""


@dataclass(frozen=True)
class Step:
    """One numbered schema change, applied at most once per database.

    `apply` runs inside an open transaction, so it must not BEGIN, COMMIT or
    toggle PRAGMA foreign_keys — that pragma is a no-op inside a transaction, and
    a table rebuild here has to tolerate enforcement staying on. `cache_db`'s
    dedup_keys rebuild is the worked example: it skips parentless rows rather
    than switching the checks off.

    `apply=None` bumps the version and nothing else, which is what a change that
    only adds a table or an index needs — the caller's CREATE ... IF NOT EXISTS
    script re-runs whenever the head moves, and the entry is what moves it.
    """

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None] | None = None


def validate(chain: tuple[Step, ...], baseline: int) -> None:
    """Raise unless *chain* numbers baseline+1, baseline+2, ... with no repeats.

    Called from `head`, so an out-of-order chain fails when the module defining
    it is imported rather than against the first database that reaches the step.
    """
    expected = baseline
    for step in chain:
        expected += 1
        if step.version != expected:
            msg = (
                f"Migration chain is out of order at {step.name!r}: expected version"
                f" {expected}, got {step.version}. Versions run consecutively from"
                f" the baseline {baseline}, so a gap or a repeat means a step is"
                " missing or two share a slot."
            )
            raise ValueError(msg)
        if not step.name:
            msg = f"Migration {step.version} has no name; the name is what the log row records"
            raise ValueError(msg)


def head(chain: tuple[Step, ...], baseline: int) -> int:
    """The version a database is stamped at once *chain* has been applied."""
    validate(chain, baseline)
    return chain[-1].version if chain else baseline


def run(
    conn: sqlite3.Connection, *, chain: tuple[Step, ...], baseline: int, db_path: Path,
    timeout_s: float | None = None,
) -> int:
    """Apply every step above *conn*'s user_version and stamp the head. Returns it.

    The caller runs its own CREATE ... IF NOT EXISTS bootstrap first: that is what
    a database below *baseline* is brought up to, and the chain carries it from
    there. A database already at the head returns without opening the lock file,
    which is the path every process takes once one of them has migrated.

    *timeout_s* bounds the wait for another process's run, LOCK_TIMEOUT_S when the
    caller names none.
    """
    target = head(chain, baseline)
    if conn.in_transaction:
        msg = "migrations.run needs the connection outside a transaction"
        raise sqlite3.ProgrammingError(msg)

    current = _user_version(conn)
    if current > target:
        msg = (
            f"{db_path} is stamped {current}, past this build's head {target}. A newer"
            " ccreport wrote it, and running would apply nothing while reporting"
            " success. Upgrade, or point at a different database file."
        )
        raise ValueError(msg)
    if current == target:
        return target

    with _lock(db_path, timeout_s):
        # Re-read under the lock: another process may have migrated the database
        # while this one waited. Below the baseline is where the frozen bootstrap
        # has just left a database it created or repaired.
        current = max(_user_version(conn), baseline)
        conn.executescript(_LOG_TABLE_SQL)
        _verify_applied(conn, chain, current)
        for step in chain:
            if step.version > current:
                _apply(conn, step)
        # Only reached with an empty chain, or one whose steps were all applied
        # by the process that held the lock before this one.
        if _user_version(conn) != target:
            conn.execute(f"PRAGMA user_version = {target:d}")
    return target


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _apply(conn: sqlite3.Connection, step: Step) -> None:
    """Run one step, record it and move the stamp, as one transaction."""
    sha = source_sha(step.apply)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if step.apply is not None:
            step.apply(conn)
        # OR REPLACE, not a plain INSERT: a database whose stamp was reset by
        # hand still carries the old row, and failing on its primary key would
        # report a conflict where the real answer is that the step ran again.
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at, source_sha) "
            "VALUES (?, ?, ?, ?)",
            (step.version, step.name, time.time(), sha),
        )
        # user_version lives in the database header and is written inside the
        # transaction, so a crash cannot land between the schema change and the
        # pointer that says it happened.
        conn.execute(f"PRAGMA user_version = {step.version:d}")
        conn.execute("COMMIT")
    except BaseException:
        # A failing COMMIT ends the transaction, so an unconditional ROLLBACK
        # here would raise over the error that caused it.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _verify_applied(conn: sqlite3.Connection, chain: tuple[Step, ...], current: int) -> None:
    """Refuse to go on when a version already applied is not the step in this build.

    A version slot can be rewritten rather than appended to — a step edited in
    place after it shipped, or renumbered in a merge. The stamp has already
    carried the database past it, so the next run finds nothing to apply and
    reports nothing, and whatever that edit adds is missing until a query hits
    the gap. A database migrated before this table existed holds no rows and is
    checked against nothing.
    """
    by_version = {step.version: step for step in chain}
    rows = conn.execute(
        "SELECT version, name, source_sha FROM schema_migrations WHERE version <= ? ORDER BY version",
        (current,),
    ).fetchall()
    for version, name, recorded in rows:
        step = by_version.get(version)
        if step is None:
            what = f"{name!r} ran here and this build's chain has no version {version}"
        elif step.name != name:
            what = f"{name!r} ran here; this build's chain has {step.name!r}"
        elif recorded is None or source_sha(step.apply) in (None, recorded):
            continue
        else:
            what = f"{name!r} no longer holds the code that ran here"
        msg = (
            f"Migration {version} changed after it was applied: {what}. Whatever that"
            " step does is not in this schema, and starting would step past it."
            " Restore the step that ran, or apply the difference by hand and update"
            " the schema_migrations row to match."
        )
        raise ValueError(msg)


def source_sha(fn: Callable[[sqlite3.Connection], None] | None) -> str | None:
    """sha256 over *fn*'s source text, or None where there is none to read.

    None covers a version-bump entry and a step whose source file is gone — an
    installation from a zip, or a function built by exec. Such a step records
    NULL and is then compared against nothing, which is weaker than refusing to
    start over a source nobody can produce.

    The hash is over the source as written, so reformatting a step reads here as
    an edit. That is the intent: the guard cannot tell a moved line from a
    changed one, and a step that already ran should not be touched either way.

    `inspect` pulls in dis, ast and linecache, and `cache_db` imports this module
    on a path the status line reaches; both imports stay inside the one function
    that runs when a schema actually changes.
    """
    if fn is None:
        return None
    import hashlib
    import inspect

    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


@contextmanager
def _lock(db_path: Path, timeout_s: float | None = None) -> Generator[None]:
    """Hold an exclusive OS lock covering the whole run.

    Every process on this machine opens the same cache.db — a status line render,
    a push and a ccu can be inside the bootstrap at once — and a thread lock
    would cover one of them. flock belongs to the open file description, so it
    excludes the other processes, and the kernel drops it if a holder dies
    mid-step.
    """
    path = Path(f"{db_path}{LOCK_SUFFIX}")
    try:
        # O_NOFOLLOW: the path is derived from the database path, and a symlink
        # planted there would have this process create or open whatever it points
        # at, as the user running ccreport.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, _LOCK_MODE)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            msg = f"Migration lock path must not be a symlink: {path}"
            raise ValueError(msg) from exc
        raise
    try:
        _flock_until_deadline(fd, path, timeout_s)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _flock_until_deadline(fd: int, path: Path, timeout_s: float | None = None) -> None:
    """Take an exclusive flock, giving up after *timeout_s*.

    LOCK_EX on its own waits with no ceiling, which on the status line's path is
    a render that never draws.
    """
    limit = LOCK_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + limit
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                msg = f"Timed out after {limit:g} s waiting for the migration lock at {path}"
                raise TimeoutError(msg) from None
            time.sleep(_LOCK_POLL_S)
        else:
            return
