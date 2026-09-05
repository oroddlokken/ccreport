"""The merged database: schema, column tuples, and the whole-file write."""

from __future__ import annotations

import gc
import sqlite3
import threading
import time

import pytest

from ccreport import migrations
from ccreport.server import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "server.db")
    yield connection
    connection.close()


def _record(**over) -> dict:
    rec = {
        "machine_id": "m1", "file_path": "/p/a.jsonl",
        "account_uuid": "acct-1", "account_label": "a@example.net",
        "mid": "msg_1", "model": "claude-opus-4", "ts": 1_700_000_000.0,
        "day": "2023-11-14", "oslo_date": "2023-11-14",
        "sid": "sess-1", "project": "proj", "cwd": "/tmp/proj", "repo": "github.com/o/p",
        "dk": "msg_1:req_1", "cost": 1.25, "log_cost": None,
        "t": [10, 20, 30, 40],
    }
    rec.update(over)
    return rec


class TestSchema:
    def test_the_column_tuple_matches_the_create_table(self, conn):
        """REC_COLS drives every SELECT and INSERT; the DDL is what it must match.

        Nothing at runtime ties the tuple to the table, so a column added to
        one and not the other shifts every insert one place and lands here.
        """
        cols = [row[1] for row in conn.execute("PRAGMA table_info(server_records)")]
        assert cols == ["id", *db.REC_COLS, "dup"]

    @pytest.mark.parametrize(
        ("table", "cols"),
        [
            ("machines", ["machine_id", "label", "first_seen", "last_seen", "label_updated_at"]),
            ("machine_tokens",
             ["token_hash", "machine_id", "created_at", "last_used_at", "revoked_at"]),
            ("ingest_files",
             ["machine_id", "file_path", "mtime_ns", "size", "n_records", "updated_at"]),
            ("exchange_rates", ["date", "rate"]),
        ],
    )
    def test_the_supporting_tables_have_the_columns_the_epic_named(self, conn, table, cols):
        assert [row[1] for row in conn.execute(f"PRAGMA table_info({table})")] == cols

    def test_the_stamp_makes_a_reopen_skip_the_ddl(self, tmp_path):
        path = tmp_path / "server.db"
        first = db.connect(path)
        first.close()
        second = db.connect(path)
        assert second.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        second.close()

    def test_a_range_query_has_an_index_to_use(self, conn):
        """Without it the 7-day toggle scans every row the all-time one does."""
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM server_records WHERE ts >= ? AND ts < ?", (0, 1),
        ).fetchall()
        assert any("idx_srec_ts" in str(step) for step in plan), plan

    def test_an_older_database_picks_up_a_new_index(self, tmp_path):
        """The version stamp is the only thing that re-runs the DDL, so a bump
        is what a database written before the index needs."""
        path = tmp_path / "server.db"
        first = db.connect(path)
        first.execute("DROP INDEX idx_srec_ts")
        first.execute("PRAGMA user_version = 1")
        first.close()
        second = db.connect(path)
        names = [row[0] for row in second.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'server_records'",
        )]
        second.close()
        assert "idx_srec_ts" in names

    def test_the_grouped_fold_walks_an_index_instead_of_sorting(self, conn):
        """The temp b-tree was half of what a detail page spent in SQL."""
        from ccreport.server import reports

        dedup, params = reports._dedup_clause(reports.Filters())
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + reports._GROUPED_SQL % dedup, params,
        ).fetchall()
        assert not any("TEMP B-TREE" in str(step) for step in plan), plan
        assert any("COVERING INDEX idx_srec_group" in str(step) for step in plan), plan

    def test_a_database_written_before_the_group_index_gains_it(self, tmp_path):
        """Stamped one below the step that carries it, which is where a server
        that last ran the previous build sits."""
        path = tmp_path / "server.db"
        first = db.connect(path)
        first.execute("DROP INDEX idx_srec_group")
        first.execute("PRAGMA user_version = 8")
        first.close()
        second = db.connect(path)
        try:
            assert second.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_srec_group'",
            ).fetchone() is not None
            assert second.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        finally:
            second.close()

    def test_the_reconcile_searches_the_account_and_key_index(self, conn):
        """On idx_srec_acct_ts it reads the account's every row per chunk: 1.52s
        against 0.37s over 800,000, and no ANALYZE may exist here to fix it."""
        plan = str(conn.execute(
            "EXPLAIN QUERY PLAN " + db._RECONCILE_SQL % "(?, ?)", ("acct", "dk"),
        ).fetchall())
        assert "idx_srec_account_dk" in plan, plan
        assert "idx_srec_acct_ts" not in plan, plan

    def test_the_group_index_leads_with_the_dedup_flag(self, conn):
        """Half the table is dup=1; leading with it means a fold never reads that half."""
        cols = [row[2] for row in conn.execute("PRAGMA index_info(idx_srec_group)")]
        assert cols[0] == "dup"

    def test_the_newest_label_lookup_has_a_covering_index(self, conn):
        """Without it the accounts page sorts every row of an account for one string."""
        plan = conn.execute("""
            EXPLAIN QUERY PLAN
            SELECT account_label FROM server_records
             WHERE account_uuid = ? AND account_label IS NOT NULL
          ORDER BY ts DESC LIMIT 1
        """, ("acct-1",)).fetchall()
        assert not any("TEMP B-TREE" in str(step) for step in plan), plan
        assert any("idx_srec_acct_ts" in str(step) for step in plan), plan

    def test_a_database_written_before_the_flag_gains_it_filled_in(self, tmp_path):
        """The column is derived, so the step that adds it has to decide every
        row: a backfill of zeroes would read as a corpus with no duplicates."""
        path = tmp_path / "server.db"
        first = db.connect(path)
        for machine_id in ("m1", "m2"):
            db.upsert_machine(first, machine_id, machine_id, 100.0)
            db.replace_file_records(
                first, machine_id, "/p/a.jsonl", 1, 10,
                [db.record_to_row(_record(machine_id=machine_id))], 500.0,
            )
        first.execute("DROP INDEX idx_srec_group")
        first.execute("ALTER TABLE server_records DROP COLUMN dup")
        first.execute("PRAGMA user_version = 9")
        first.close()

        second = db.connect(path)
        try:
            assert second.execute(
                "SELECT machine_id FROM server_records WHERE dup = 0",
            ).fetchall() == [("m1",)]
            assert second.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        finally:
            second.close()

    def test_every_connection_reads_through_a_memory_map(self, conn):
        """The map is where a report's index reads land, and it is shared where
        a per-connection page cache is not."""
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == db.MMAP_SIZE

    def test_a_wal_under_the_threshold_is_left_alone(self, tmp_path):
        """Truncation is for a file that grew, not for every push."""
        path = tmp_path / "server.db"
        conn = db.connect(path)
        try:
            db.upsert_machine(conn, "m1", "laptop", 100.0)
            assert db.truncate_wal(conn, path) is False
        finally:
            conn.close()

    def test_an_oversized_wal_is_truncated(self, tmp_path, monkeypatch):
        """It reached 204 MB against a 774 MB database, all of it checkpointed."""
        path = tmp_path / "server.db"
        conn = db.connect(path)
        try:
            db.upsert_machine(conn, "m1", "laptop", 100.0)
            monkeypatch.setattr(db, "WAL_TRUNCATE_BYTES", 1)
            assert db.truncate_wal(conn, path) is True
            assert path.with_name(path.name + "-wal").stat().st_size == 0
        finally:
            conn.close()

    def test_a_reader_makes_the_truncation_give_up_at_once(self, tmp_path, monkeypatch):
        """It holds the writer lock while it waits, so waiting out the busy
        timeout blocks every other push for 5.769s and then gives up anyway."""
        import threading

        path = tmp_path / "server.db"
        conn = db.connect(path)
        started, release, elapsed = threading.Event(), threading.Event(), []

        def hold() -> None:
            reader = db.connect(path)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM machines").fetchone()
            started.set()
            release.wait()
            reader.execute("COMMIT")
            reader.close()

        thread = threading.Thread(target=hold, daemon=True)
        try:
            db.upsert_machine(conn, "m1", "laptop", 100.0)
            monkeypatch.setattr(db, "WAL_TRUNCATE_BYTES", 1)
            thread.start()
            started.wait()
            start = time.perf_counter()
            assert db.truncate_wal(conn, path) is False
            elapsed.append(time.perf_counter() - start)
            assert elapsed[0] < 1.0, elapsed
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
        finally:
            release.set()
            thread.join()
            conn.close()

    def test_a_connection_waits_out_another_write_before_it_raises(self, conn):
        """A request that hits this ceiling 500s; the number is here to be read."""
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS

    def test_a_new_column_reaches_a_database_that_already_has_the_table(
        self, tmp_path, monkeypatch,
    ):
        """What the CREATE script cannot do and this file cannot go without: it
        is the only copy of a machine's records once its own logs have rotated,
        so a column added to server_records has to arrive by migration."""
        path = tmp_path / "server.db"
        first = db.connect(path)
        db.upsert_machine(first, "m1", "laptop", 100.0)
        db.replace_file_records(
            first, "m1", "/p/a.jsonl", 1, 10, [db.record_to_row(_record())], 500.0,
        )
        first.close()

        def add_region(conn: sqlite3.Connection) -> None:
            conn.execute("ALTER TABLE server_records ADD COLUMN region TEXT")

        # One slot above the shipped chain, with that chain kept: the first
        # connect applied it, and a fake step in a slot already stamped is a
        # database at the head with nothing left to run.
        step = migrations.Step(db.SCHEMA_VERSION + 1, "record region", add_region)
        monkeypatch.setattr(db, "MIGRATION_CHAIN", (*db.MIGRATION_CHAIN, step))
        monkeypatch.setattr(db, "SCHEMA_VERSION", step.version)

        second = db.connect(path)
        try:
            assert "region" in [row[1] for row in second.execute(
                "PRAGMA table_info(server_records)")]
            assert [rec["mid"] for rec in db.load_file_records(second, "m1", "/p/a.jsonl")] == [
                "msg_1",
            ]
            assert second.execute("PRAGMA user_version").fetchone()[0] == step.version
        finally:
            second.close()

    def test_a_record_cannot_name_a_machine_that_does_not_exist(self, conn):
        """Foreign keys are on, which is what keeps orphan rows out of a merge."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO server_records ({', '.join(db.REC_COLS)}) "
                f"VALUES ({', '.join('?' * len(db.REC_COLS))})",
                db.record_to_row(_record()),
            )

    def test_cost_is_required_but_the_logged_one_is_not(self, conn):
        """The server always computes a cost; only the log's own may be absent."""
        info = {row[1]: row for row in conn.execute("PRAGMA table_info(server_records)")}
        assert info["cost"][3] == 1
        assert info["log_cost"][3] == 0

    def test_a_redacted_record_may_omit_its_identity_columns(self, conn):
        """A project not opted in pushes counts with sid/project/cwd/repo stripped."""
        info = {row[1]: row for row in conn.execute("PRAGMA table_info(server_records)")}
        assert [info[name][3] for name in ("sid", "project", "cwd", "repo")] == [0, 0, 0, 0]


class TestRecordRows:
    def test_a_record_survives_the_round_trip_through_a_row(self):
        rec = _record()
        assert db.row_to_record(db.record_to_row(rec)) == rec

    def test_the_row_is_in_column_order(self):
        row = db.record_to_row(_record())
        assert row[db.REC_COLS.index("cost")] == 1.25
        assert row[db.REC_COLS.index("machine_id")] == "m1"
        assert row[-4:] == (10, 20, 30, 40)

    def test_a_missing_field_becomes_null_rather_than_a_key_error(self):
        """The pusher omits what a redacted project strips; that is not an error."""
        rec = _record()
        del rec["repo"]
        assert db.record_to_row(rec)[db.REC_COLS.index("repo")] is None


class TestMachinesAndTokens:
    def test_a_second_push_updates_last_seen_and_keeps_the_ui_label(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        db.upsert_machine(conn, "m1", "hostname-of-the-week", 200.0)
        row = conn.execute("SELECT label, first_seen, last_seen FROM machines").fetchone()
        assert row == ("laptop", 100.0, 200.0)

    def test_a_rename_replaces_the_label_and_stamps_when(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        assert db.set_machine_label(conn, "m1", " workstation ", 700.0) is True
        assert conn.execute(
            "SELECT label, label_updated_at FROM machines").fetchone() == ("workstation", 700.0)

    def test_a_blank_name_stores_the_id_rather_than_an_empty_label(self, conn):
        """Every reader falls back to the id, but "" is a label and would draw as one."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        db.set_machine_label(conn, "m1", "   ", 700.0)
        assert db.machine_label(conn, "m1") == "m1"

    def test_naming_a_machine_that_was_never_minted_says_so(self, conn):
        assert db.set_machine_label(conn, "never-minted", "laptop", 700.0) is False

    def test_a_token_resolves_to_its_own_machine(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        conn.execute(
            "INSERT INTO machine_tokens (token_hash, machine_id, created_at) VALUES (?, ?, ?)",
            ("hash-1", "m1", 100.0),
        )
        assert db.machine_for_token(conn, "hash-1") == "m1"

    def test_a_revoked_token_reads_the_same_as_an_unknown_one(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        conn.execute(
            "INSERT INTO machine_tokens (token_hash, machine_id, created_at, revoked_at) "
            "VALUES (?, ?, ?, ?)",
            ("hash-1", "m1", 100.0, 150.0),
        )
        assert db.machine_for_token(conn, "hash-1") is None
        assert db.machine_for_token(conn, "never-minted") is None


class TestWholeFileIngest:
    def _push(self, conn, rows, *, mtime_ns=1, size=10):
        db.replace_file_records(conn, "m1", "/p/a.jsonl", mtime_ns, size, rows, 500.0)

    def test_a_file_replaces_its_own_previous_rows(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record(mid="msg_1"))])
        self._push(
            conn,
            [db.record_to_row(_record(mid="msg_1")), db.record_to_row(_record(mid="msg_2"))],
            mtime_ns=2, size=20,
        )
        stored = db.load_file_records(conn, "m1", "/p/a.jsonl")
        assert [rec["mid"] for rec in stored] == ["msg_1", "msg_2"]
        assert db.file_fingerprint(conn, "m1", "/p/a.jsonl") == (2, 20)
        assert conn.execute("SELECT n_records FROM ingest_files").fetchone()[0] == 2

    def test_another_machines_copy_of_the_same_path_is_untouched(self, conn):
        """A synced home directory is two machines' rows, not one overwriting the other."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        db.upsert_machine(conn, "m2", "desktop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        db.replace_file_records(
            conn, "m2", "/p/a.jsonl", 1, 10, [db.record_to_row(_record(machine_id="m2"))], 500.0,
        )
        self._push(conn, [db.record_to_row(_record(mid="msg_9"))], mtime_ns=2, size=20)
        assert len(db.load_file_records(conn, "m2", "/p/a.jsonl")) == 1
        assert [r["mid"] for r in db.load_file_records(conn, "m1", "/p/a.jsonl")] == ["msg_9"]

    def test_a_failed_insert_leaves_the_previous_rows_in_place(self, conn):
        """One transaction per file: half a session must never reach the merge."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record(mid="msg_1"))])
        bad = db.record_to_row(_record(mid="msg_2", model=None))
        with pytest.raises(sqlite3.IntegrityError):
            self._push(conn, [bad], mtime_ns=2, size=20)
        assert [r["mid"] for r in db.load_file_records(conn, "m1", "/p/a.jsonl")] == ["msg_1"]
        assert db.file_fingerprint(conn, "m1", "/p/a.jsonl") == (1, 10)

    def test_an_unpushed_file_has_no_fingerprint(self, conn):
        assert db.file_fingerprint(conn, "m1", "/p/never.jsonl") is None


class TestDedupFlag:
    """The column that says which copy of a shared call every report reads."""

    def _flags(self, conn) -> list[tuple]:
        return conn.execute(
            "SELECT machine_id, mid, dup FROM server_records ORDER BY id",
        ).fetchall()

    def _push(self, conn, machine_id, rows, *, path="/p/a.jsonl", mtime_ns=1, size=10):
        db.upsert_machine(conn, machine_id, machine_id, 100.0)
        db.replace_file_records(conn, machine_id, path, mtime_ns, size, rows, 500.0)

    def test_the_first_copy_of_a_key_wins_and_later_ones_lose(self, conn):
        """Two machines on a synced home directory push the same call twice."""
        self._push(conn, "m1", [db.record_to_row(_record())])
        self._push(conn, "m2", [db.record_to_row(_record(machine_id="m2"))])
        assert self._flags(conn) == [("m1", "msg_1", 0), ("m2", "msg_1", 1)]

    def test_a_record_the_log_gave_no_key_stands_for_itself(self, conn):
        """Nothing matches it, so nothing may collapse it into something else."""
        self._push(conn, "m1", [
            db.record_to_row(_record(mid="msg_1", dk=None)),
            db.record_to_row(_record(mid="msg_2", dk="")),
            db.record_to_row(_record(mid="msg_3", dk=None)),
        ])
        assert [row[2] for row in self._flags(conn)] == [0, 0, 0]

    def test_one_key_under_two_accounts_is_two_calls(self, conn):
        """The dedup groups by account, so two people's keys never collide."""
        self._push(conn, "m1", [
            db.record_to_row(_record(account_uuid="acct-1")),
            db.record_to_row(_record(account_uuid="acct-2")),
        ])
        assert [row[2] for row in self._flags(conn)] == [0, 0]

    def test_losing_the_winner_promotes_the_next_copy(self, conn):
        """The file holding the winner is re-pushed without it."""
        self._push(conn, "m1", [db.record_to_row(_record())])
        self._push(conn, "m2", [db.record_to_row(_record(machine_id="m2"))])
        self._push(conn, "m1", [db.record_to_row(_record(mid="msg_9", dk="other"))],
                   mtime_ns=2, size=20)
        assert self._flags(conn) == [("m2", "msg_1", 0), ("m1", "msg_9", 0)]

    def test_a_repushed_file_does_not_take_the_key_back(self, conn):
        """Its rows are gone and reinserted above every id that survived."""
        self._push(conn, "m1", [db.record_to_row(_record())])
        self._push(conn, "m2", [db.record_to_row(_record(machine_id="m2"))])
        self._push(conn, "m1", [db.record_to_row(_record())], mtime_ns=2, size=20)
        assert self._flags(conn) == [("m2", "msg_1", 0), ("m1", "msg_1", 1)]

    def test_deleting_a_machine_promotes_what_it_was_hiding(self, conn):
        """Otherwise a shared call leaves with the machine that reported it first."""
        self._push(conn, "m1", [db.record_to_row(_record())])
        self._push(conn, "m2", [db.record_to_row(_record(machine_id="m2"))])
        assert db.delete_machine(conn, "m1") == 1
        assert self._flags(conn) == [("m2", "msg_1", 0)]

    def test_the_whole_table_form_agrees_with_the_incremental_one(self, conn):
        """Every write maintains the flag; the migration and a delete rebuild it."""
        self._push(conn, "m1", [
            db.record_to_row(_record(mid="msg_1")),
            db.record_to_row(_record(mid="msg_2", dk=None)),
        ])
        self._push(conn, "m2", [db.record_to_row(_record(machine_id="m2"))])
        incremental = self._flags(conn)
        conn.execute("UPDATE server_records SET dup = 1 - dup")
        db.reconcile_all_dup(conn)
        assert self._flags(conn) == incremental

    def test_a_flag_that_already_stands_is_not_rewritten(self, conn):
        """A re-push re-decides every key in the file and moves almost none;
        each unchanged row it wrote was a dirtied page and more WAL."""
        rows = [db.record_to_row(_record(mid=f"msg_{n}", dk=f"dk_{n}")) for n in range(20)]
        self._push(conn, "m1", rows)
        before = conn.total_changes
        db._reconcile_dup(conn, {("acct-1", f"dk_{n}") for n in range(20)})
        assert conn.total_changes == before

    def test_more_keys_than_one_statement_holds_are_all_flagged(self, conn):
        """The reconcile chunks its VALUES list; the seam must not drop a key."""
        count = db._RECONCILE_CHUNK * 2 + 1
        rows = [db.record_to_row(_record(mid=f"msg_{n}", dk=f"dk_{n}")) for n in range(count)]
        self._push(conn, "m1", rows)
        self._push(conn, "m2", [
            db.record_to_row(_record(machine_id="m2", mid=f"msg_{n}", dk=f"dk_{n}"))
            for n in range(count)
        ])
        assert conn.execute(
            "SELECT COUNT(*) FROM server_records WHERE machine_id = 'm2' AND dup = 1",
        ).fetchone()[0] == count


class TestProjectAliases:
    """The table that folds two machines' names for one repo into one project."""

    def _pushed(self, conn, machine_id="m1", project: str | None = "proj", path="/p/a.jsonl"):
        db.upsert_machine(conn, machine_id, machine_id, 100.0)
        db.replace_file_records(
            conn, machine_id, path, 1, 10,
            [db.record_to_row(_record(machine_id=machine_id, project=project, file_path=path))],
            500.0,
        )

    def test_a_name_is_keyed_on_the_machine_as_well_as_the_project(self, conn):
        self._pushed(conn)
        self._pushed(conn, machine_id="m2", project="other")
        db.set_project_alias(conn, "m1", "proj", "shared", 700.0)
        db.set_project_alias(conn, "m2", "other", "shared", 700.0)
        assert db.project_aliases(conn) == {("m1", "proj"): "shared", ("m2", "other"): "shared"}

    def test_a_blank_name_deletes_the_row(self, conn):
        self._pushed(conn)
        db.set_project_alias(conn, "m1", "proj", "shared", 700.0)
        db.set_project_alias(conn, "m1", "proj", "  ", 800.0)
        assert db.project_aliases(conn) == {}

    def test_the_pairs_a_name_covers_come_back_for_a_filter(self, conn):
        self._pushed(conn)
        self._pushed(conn, machine_id="m2", project="other")
        db.set_project_alias(conn, "m1", "proj", "shared", 700.0)
        db.set_project_alias(conn, "m2", "other", "shared", 700.0)
        assert sorted(db.projects_with_alias(conn, "shared")) == [("m1", "proj"), ("m2", "other")]
        assert db.projects_with_alias(conn, None) == ()
        assert db.projects_with_alias(conn, "nobody") == ()

    def test_only_a_pair_that_pushed_exists(self, conn):
        self._pushed(conn)
        assert db.project_exists(conn, "m1", "proj") is True
        assert db.project_exists(conn, "m1", "typo") is False
        assert db.project_exists(conn, "m2", "proj") is False

    def test_deleting_the_machine_takes_its_names_with_it(self, conn):
        self._pushed(conn)
        db.set_project_alias(conn, "m1", "proj", "shared", 700.0)
        db.delete_machine(conn, "m1")
        assert db.project_aliases(conn) == {}


class TestContentStamp:
    """What the dashboard holds a cached page against."""

    def _push(self, conn, rows, *, path="/p/a.jsonl", mtime_ns=1, size=10, now=500.0):
        db.replace_file_records(conn, "m1", path, mtime_ns, size, rows, now)

    def test_an_empty_database_stamps_without_raising(self, conn):
        assert db.content_stamp(conn) == (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    def test_naming_a_project_moves_it(self, conn):
        """A project name is a rename with no push behind it, like the other two."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        before = db.content_stamp(conn)
        db.set_project_alias(conn, "m1", "proj", "shared", 700.0)
        assert db.content_stamp(conn) != before

    def test_clearing_a_project_name_moves_it_back_off(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        db.set_project_alias(conn, "m1", "proj", "shared", 700.0)
        named = db.content_stamp(conn)
        db.set_project_alias(conn, "m1", "proj", "", 800.0)
        assert db.content_stamp(conn) != named

    def test_naming_an_account_moves_it(self, conn):
        """A rename has no push behind it, and it changes every rendered name."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        before = db.content_stamp(conn)
        db.set_account_alias(conn, "acct-1", "personal", 700.0)
        assert db.content_stamp(conn) != before

    def test_clearing_that_name_moves_it_back_off(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        db.set_account_alias(conn, "acct-1", "personal", 700.0)
        named = db.content_stamp(conn)
        db.set_account_alias(conn, "acct-1", "", 800.0)
        assert db.content_stamp(conn) != named

    def test_renaming_a_machine_moves_it(self, conn):
        """Same case as an account name: nothing pushes after a rename."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        before = db.content_stamp(conn)
        db.set_machine_label(conn, "m1", "workstation", 700.0)
        assert db.content_stamp(conn) != before

    def test_clearing_a_machine_name_moves_it_back_off(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        db.set_machine_label(conn, "m1", "workstation", 700.0)
        named = db.content_stamp(conn)
        db.set_machine_label(conn, "m1", "", 800.0)
        assert db.content_stamp(conn) != named

    def test_reading_it_twice_gives_the_same_answer(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        assert db.content_stamp(conn) == db.content_stamp(conn)

    def test_a_new_file_moves_it(self, conn):
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        before = db.content_stamp(conn)
        self._push(conn, [db.record_to_row(_record(mid="msg_2"))], path="/p/b.jsonl", now=600.0)
        assert db.content_stamp(conn) != before

    def test_a_re_push_of_the_same_file_moves_it(self, conn):
        """Same file, same count, different content: only updated_at is left."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record())])
        before = db.content_stamp(conn)
        self._push(conn, [db.record_to_row(_record(mid="msg_9"))], mtime_ns=2, size=20, now=600.0)
        assert db.content_stamp(conn) != before

    def test_a_file_that_shrank_moves_it(self, conn):
        """The record total is what catches a re-push that lost rows."""
        db.upsert_machine(conn, "m1", "laptop", 100.0)
        self._push(conn, [db.record_to_row(_record()), db.record_to_row(_record(mid="msg_2"))])
        before = db.content_stamp(conn)
        self._push(conn, [db.record_to_row(_record())], mtime_ns=2, size=20, now=500.0)
        assert db.content_stamp(conn) != before


class TestRateStore:
    def test_exchange_reads_and_writes_the_server_database(self, tmp_path, monkeypatch):
        """exchange.py keeps the walk-back; only the storage moves."""
        from datetime import date

        from ccreport import exchange

        store = db.RateStore(db.Database(tmp_path / "server.db"))
        monkeypatch.setattr(exchange, "_store", store)
        store.save_exchange_rates({"2026-08-10": 10.5, "2026-08-11": 10.6})
        assert exchange._read_cached(date(2026, 8, 11)) == ({"2026-08-11": 10.6}, set())

    def test_a_no_observation_row_is_not_handed_on_as_a_rate(self, tmp_path, monkeypatch):
        from datetime import date

        from ccreport import exchange

        store = db.RateStore(db.Database(tmp_path / "server.db"))
        monkeypatch.setattr(exchange, "_store", store)
        store.save_exchange_rates({"2026-08-10": 10.5, "2026-08-11": exchange._NO_OBSERVATION})
        rates, gaps = exchange._read_cached(date(2026, 8, 1))
        assert rates == {"2026-08-10": 10.5}
        assert gaps == {"2026-08-11"}


class TestDatabase:
    def test_the_connection_is_reused_within_a_thread(self, tmp_path):
        database = db.Database(tmp_path / "server.db")
        assert database.connect() is database.connect()
        database.close()

    def test_each_thread_gets_its_own(self, tmp_path):
        import threading

        database = db.Database(tmp_path / "server.db")
        seen = []
        thread = threading.Thread(target=lambda: seen.append(database.connect()))
        thread.start()
        thread.join()
        assert seen[0] is not database.connect()
        database.close()


class TestPoolConnectionsClose:
    """Whoever opened a pool connection is who closes it."""

    def test_a_worker_threads_connection_closes_when_that_thread_ends(self, tmp_path):
        """Left open, the GC finalizes it and sqlite3 calls it an unclosed database."""
        database = db.Database(tmp_path / "server.db")
        opened: list[sqlite3.Connection] = []
        thread = threading.Thread(target=lambda: opened.append(database.connect()))
        thread.start()
        thread.join()
        del thread
        gc.collect()
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_close_still_takes_the_calling_threads_connection(self, tmp_path):
        database = db.Database(tmp_path / "server.db")
        conn = database.connect()
        database.close()
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        assert database.connect() is not conn
