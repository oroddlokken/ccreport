"""The version the client and the server agree on, in all three directions.

One integer, separate from the package version and from the git commit, checked
where a mismatch would otherwise be a 200 over a payload the peer dropped.
"""

from __future__ import annotations

import io
import json

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient
from rich.console import Console

from ccreport import cache_db, protocol, push
from ccreport import ccreport as ccr
from ccreport.server.factory import create_app

AHEAD = protocol.PROTOCOL_VERSION + 1
BEHIND = protocol.PROTOCOL_VERSION - 1


class TestDescribe:
    def test_an_agreed_version_says_nothing(self):
        assert protocol.describe(protocol.PROTOCOL_VERSION) == ""

    def test_a_server_behind_names_what_is_lost(self):
        assert "would be dropped" in protocol.describe(BEHIND)

    def test_a_pre_versioning_server_is_named_as_one(self):
        assert "pre-versioning" in protocol.describe(protocol.PRE_VERSIONING)

    def test_a_server_ahead_says_pushes_still_work(self):
        assert "pushes still work" in protocol.describe(AHEAD)


class TestTheServerSide:
    @pytest.fixture
    def wired(self, tmp_path):
        app = create_app(sf.config(tmp_path))
        token = sf.mint_for(app, "laptop-1", "Laptop")
        return TestClient(app), token

    @staticmethod
    def _batch(**over) -> dict:
        batch = {"label": "Laptop", "files": []}
        batch.update(over)
        return batch

    def test_a_client_at_this_version_is_accepted(self, wired):
        client, token = wired
        resp = client.post("/v1/ingest", headers=sf.auth(token),
                           json=self._batch(protocol=protocol.PROTOCOL_VERSION))
        assert resp.status_code == 200
        assert resp.json()["protocol"] == protocol.PROTOCOL_VERSION

    def test_a_client_behind_is_accepted(self, wired):
        """It sends a subset of what this build reads; nothing is lost."""
        client, token = wired
        resp = client.post("/v1/ingest", headers=sf.auth(token),
                           json=self._batch(protocol=BEHIND))
        assert resp.status_code == 200

    def test_a_pre_versioning_client_is_accepted(self, wired):
        client, token = wired
        assert client.post("/v1/ingest", headers=sf.auth(token),
                           json=self._batch()).status_code == 200

    def test_a_client_ahead_is_refused_with_409(self, wired):
        client, token = wired
        resp = client.post("/v1/ingest", headers=sf.auth(token),
                           json=self._batch(protocol=AHEAD))
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert str(protocol.PROTOCOL_VERSION) in detail
        assert str(AHEAD) in detail

    def test_a_refused_batch_stores_nothing(self, tmp_path):
        """Not even the machine row: a stale server must collect no half-payloads."""
        app = create_app(sf.config(tmp_path))
        token = sf.mint_for(app, "laptop-1", "Laptop")
        client = TestClient(app)
        client.post("/v1/ingest", headers=sf.auth(token), json=self._batch(
            protocol=AHEAD,
            files=[{"path": "/p/a.jsonl", "mtime_ns": 1, "size": 10,
                    "records": [sf.record()]}],
        ))
        conn = app.state.db.connect()
        assert conn.execute("SELECT COUNT(*) FROM server_records").fetchone()[0] == 0

    def test_health_reports_the_version(self, wired):
        client, token = wired
        assert client.get("/v1/health", headers=sf.auth(token)).json()["protocol"] == (
            protocol.PROTOCOL_VERSION
        )


class TestTheClientSide:
    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        monkeypatch.setattr(cache_db, "DB_PATH", tmp_path / "cache.db")
        monkeypatch.setattr(cache_db, "_conn", None)
        cache_db.get_connection()
        yield push.ServerConfig(
            url="https://ccr.example.net", token="t", label="Laptop", machine_id="laptop-1",
        )
        cache_db.close_connection()

    @staticmethod
    def _reply(monkeypatch, body: dict, sent: list | None = None):
        """Answer every urlopen with *body*, recording the request bodies in *sent*.

        BytesIO is the whole of what post_batch asks of a response: a context
        manager it reads once.
        """
        def urlopen(request, **_kw):
            if sent is not None:
                sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps(body).encode())

        monkeypatch.setattr(push.urllib.request, "urlopen", urlopen)

    def test_the_request_carries_this_builds_version(self, config, monkeypatch):
        sent: list[dict] = []
        self._reply(monkeypatch, {"protocol": protocol.PROTOCOL_VERSION}, sent)
        push.post_batch(config, {"label": "Laptop", "files": []})
        assert sent[0]["protocol"] == protocol.PROTOCOL_VERSION

    def test_a_server_at_this_version_is_accepted(self, config, monkeypatch):
        self._reply(monkeypatch, {"protocol": protocol.PROTOCOL_VERSION, "files": []})
        assert push.post_batch(config, {"label": "L", "files": []})["files"] == []

    def test_a_server_ahead_is_accepted(self, config, monkeypatch):
        self._reply(monkeypatch, {"protocol": AHEAD, "files": []})
        assert push.post_batch(config, {"label": "L", "files": []})["files"] == []

    def test_a_server_behind_is_terminal(self, config, monkeypatch):
        """A 200 over a payload the server half read is what this catches."""
        self._reply(monkeypatch, {"protocol": BEHIND, "files": []})
        with pytest.raises(push.PushError) as caught:
            push.post_batch(config, {"label": "L", "files": []})
        assert caught.value.terminal is True

    def test_a_pre_versioning_server_is_terminal(self, config, monkeypatch):
        self._reply(monkeypatch, {"files": []})
        with pytest.raises(push.PushError) as caught:
            push.post_batch(config, {"label": "L", "files": []})
        assert "pre-versioning" in str(caught.value)

    def test_a_409_is_terminal_and_keeps_the_servers_words(self, config, monkeypatch):
        import urllib.error

        body = io.BytesIO(json.dumps({"detail": "server speaks 1, client speaks 9"}).encode())

        def urlopen(*_a, **_kw):
            raise urllib.error.HTTPError("u", 409, "Conflict", {}, body)  # type: ignore[arg-type]

        monkeypatch.setattr(push.urllib.request, "urlopen", urlopen)
        with pytest.raises(push.PushError) as caught:
            push.post_batch(config, {"label": "L", "files": []})
        assert caught.value.terminal is True
        assert "client speaks 9" in str(caught.value)


class TestTheCommands:
    @staticmethod
    def _run(monkeypatch, argv) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport", *argv])
        try:
            ccr.main()
        except SystemExit:
            pass
        return buf.getvalue()

    @staticmethod
    def _health(monkeypatch, theirs: int | None):
        row = {"label": "Laptop", "machine_id": "laptop-1", "records": 3}
        if theirs is not None:
            row["protocol"] = theirs
        monkeypatch.setattr("ccreport.remote.fetch_health", lambda base, token: row)

    def test_connect_refuses_a_server_behind_and_writes_nothing(
        self, tmp_path, monkeypatch, capsys,
    ):
        self._health(monkeypatch, BEHIND)
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "t",
            "--config", str(path),
        ])
        assert "would be dropped" in capsys.readouterr().err
        assert not path.exists()

    def test_connect_accepts_a_server_ahead(self, tmp_path, monkeypatch):
        self._health(monkeypatch, AHEAD)
        path = tmp_path / "push.toml"
        out = self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "t",
            "--config", str(path),
        ])
        assert "Connected to" in out
        assert path.exists()

    def test_status_says_when_the_versions_agree(self, tmp_path, monkeypatch):
        self._health(monkeypatch, protocol.PROTOCOL_VERSION)
        path = tmp_path / "push.toml"
        path.write_text('[server."https://ccr.example.net"]\ntoken = "t"\n')
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "agreed" in out

    def test_status_names_a_server_ahead(self, tmp_path, monkeypatch):
        self._health(monkeypatch, AHEAD)
        path = tmp_path / "push.toml"
        path.write_text('[server."https://ccr.example.net"]\ntoken = "t"\n')
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "pushes still work" in out

    def test_status_does_not_claim_agreement_with_an_unreachable_server(
        self, tmp_path, monkeypatch,
    ):
        """Nothing was compared, which is not the same claim as agreement."""
        from ccreport.remote import RemoteError

        def refuse(base, _token):
            raise RemoteError(f"{base} could not be reached")

        monkeypatch.setattr("ccreport.remote.fetch_health", refuse)
        path = tmp_path / "push.toml"
        path.write_text('[server."https://ccr.example.net"]\ntoken = "t"\n')
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "not compared" in out
        assert "agreed" not in out

    def test_status_prints_the_reason_a_push_stopped(self, tmp_path, monkeypatch):
        self._health(monkeypatch, protocol.PROTOCOL_VERSION)
        url = "https://ccr.example.net"
        cache_db.write_push_attempt(url, 100.0, 1, stopped=True,
                                    reason="the server speaks pre-versioning")
        path = tmp_path / "push.toml"
        path.write_text(f'[server."{url}"]\ntoken = "t"\n')
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "the server speaks pre-versioning" in out
