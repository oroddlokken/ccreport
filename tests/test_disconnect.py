"""`ccreport server disconnect`: stop pushing to a server and forget it locally.

The pulled cost tables are the half that matters. Left behind, `-A` and the
status line's merged windows go on adding a server nobody pushes to.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from ccreport import cache_db, push
from ccreport import ccreport as ccr

A = "https://a.example.net"
B = "https://b.example.net"


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Two configured servers, with local state stored under the first."""
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
    monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cache_db, "_conn", None)
    conn = cache_db.get_connection()
    path = tmp_path / "push.toml"
    push.write_server(path, A, {"token": "ta", "label": "Laptop"})
    push.write_server(path, B, {"token": "tb", "label": "Laptop"})

    cache_db.save_push_state(A, [("/p/a.jsonl", 1, 10)], 100.0)
    cache_db.write_push_samples_at(A, 5.0)
    cache_db.write_push_attempt(A, 100.0, 3, reason="refused")
    cache_db.save_remote_costs(
        A, "acct-1",
        [("desk-1", "Desk", "seven_day", 4.0, 90.0)],
        [("desk-1", "2026-08-01", "proj", 4.0, 1, 2, 3, 4, 5, 90.0)],
        100.0,
    )
    conn.execute(
        "INSERT INTO account_events (ts, account_uuid, email) VALUES (1.0, 'acct-1', 'me@x')"
    )
    conn.commit()
    yield path
    cache_db.close_connection()


def _run(monkeypatch, argv) -> str:
    buf = io.StringIO()
    monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
    monkeypatch.setattr(ccr.sys, "argv", ["ccreport", *argv])
    try:
        ccr.main()
    except SystemExit:
        pass
    return buf.getvalue()


class TestThePreview:
    def test_it_names_what_would_go_before_it_goes(self, wired, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: "n")
        out = _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        assert "1 acknowledged file(s)" in out
        assert "2 pulled cost row(s)" in out

    def test_it_warns_that_the_token_cannot_be_recovered(self, wired, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: "n")
        out = _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        assert "minted afresh" in out

    def test_saying_no_changes_nothing(self, wired, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _p: "n")
        _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        assert capsys.readouterr().out.strip() == "Aborted."
        assert A in push.read_raw(wired)
        assert cache_db.count_server_rows(A)["push_state"] == 1


class TestTheDisconnect:
    @pytest.fixture(autouse=True)
    def _yes(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: "y")

    def test_the_entry_goes_and_the_other_server_stays(self, wired, monkeypatch):
        _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        assert list(push.read_raw(wired)) == [B]

    def test_every_local_row_keyed_on_it_goes(self, wired, monkeypatch):
        _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        assert cache_db.count_server_rows(A) == {
            "push_state": 0, "remote_window_costs": 0, "remote_day_costs": 0, "meta": 0,
        }

    def test_the_merged_report_stops_counting_it(self, wired, monkeypatch):
        assert ccr._remote_records(None, None, None, None).records
        _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        assert ccr._remote_records(None, None, None, None).records == []

    def test_the_status_line_window_stops_gaining_it(self, wired, monkeypatch):
        from ccreport import statusline

        _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        usage = {"seven_day_cost": "1.0"}
        statusline._merge_remote_costs(usage, 100.0)
        assert usage == {"seven_day_cost": "1.0"}

    def test_the_restricted_marker_survives(self, wired, monkeypatch):
        """It claims this machine has been restricted, not that one server was."""
        marker = push._marker_path(wired)
        marker.write_text("restricted")
        _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired)])
        assert marker.exists()

    def test_yes_skips_the_prompt(self, wired, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: (_ for _ in ()).throw(
            AssertionError("--yes must not prompt")))
        _run(monkeypatch, ["server", "disconnect", A, "--config", str(wired), "--yes"])
        assert list(push.read_raw(wired)) == [B]


class TestNamingTheServer:
    @pytest.fixture(autouse=True)
    def _yes(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: "y")

    def test_two_servers_and_no_url_asks_for_one(self, wired, monkeypatch, capsys):
        _run(monkeypatch, ["server", "disconnect", "--config", str(wired)])
        assert "name the server" in capsys.readouterr().err
        assert len(push.read_raw(wired)) == 2

    def test_one_server_needs_no_url(self, wired, monkeypatch):
        push.remove_server(wired, B)
        _run(monkeypatch, ["server", "disconnect", "--config", str(wired)])
        assert push.read_raw(wired) == {}

    def test_an_unknown_url_is_an_error(self, wired, monkeypatch, capsys):
        _run(monkeypatch, ["server", "disconnect", "https://nope", "--config", str(wired)])
        assert "is not in" in capsys.readouterr().err
        assert len(push.read_raw(wired)) == 2

    def test_an_empty_config_says_so(self, tmp_path, monkeypatch, capsys):
        _run(monkeypatch, ["server", "disconnect", "--config", str(tmp_path / "none.toml")])
        assert "nothing to disconnect" in capsys.readouterr().err


class TestTheMetaKeyList:
    def test_every_push_meta_name_is_listed(self):
        """A key forget_server does not know about is a key a disconnect leaves."""
        import re
        from pathlib import Path

        source = Path(cache_db.__file__).read_text()
        named = set(re.findall(r'_push_meta_key\(\s*"([a-z_]+)"', source))
        assert named <= set(cache_db._PUSH_META_NAMES), (
            f"unlisted: {named - set(cache_db._PUSH_META_NAMES)}"
        )
