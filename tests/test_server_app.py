"""The app factory, its configuration and the web UI's network gate."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ccreport.server import config as server_config
from ccreport.server import db
from ccreport.server.config import ServerConfig, load_config
from ccreport.server.factory import create_app
from ccreport.server.middleware import ip_allowed, restrict_remote_addr_dep


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    """Keep this file's create_app calls from following the worker out.

    create_app points exchange.py's rate store at its own database, which is a
    process-wide global; restoring it here stops a server test from redirecting
    whatever test_exchange.py runs next on the same xdist worker.
    """
    from ccreport import exchange

    for name in (
        server_config.DB_ENV, server_config.HOST_ENV,
        server_config.PORT_ENV, server_config.NETWORKS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(exchange, "_store", exchange._store)


class TestConfig:
    def test_the_defaults_bind_loopback_and_admit_loopback(self):
        config = load_config()
        assert config.host == server_config.DEFAULT_HOST
        assert config.port == server_config.DEFAULT_PORT
        assert config.networks == server_config.DEFAULT_NETWORKS

    def test_every_value_comes_from_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv(server_config.DB_ENV, str(tmp_path / "merged.db"))
        monkeypatch.setenv(server_config.HOST_ENV, "0.0.0.0")
        monkeypatch.setenv(server_config.PORT_ENV, "9001")
        monkeypatch.setenv(server_config.NETWORKS_ENV, "10.0.0.0/8, 192.168.1.5")
        config = load_config()
        assert config.db_path == tmp_path / "merged.db"
        assert config.host == "0.0.0.0"
        assert config.port == 9001
        assert config.networks == ("10.0.0.0/8", "192.168.1.5")

    def test_an_unparseable_port_falls_back_rather_than_refusing_to_start(self, monkeypatch):
        monkeypatch.setenv(server_config.PORT_ENV, "eight-thousand")
        assert load_config().port == server_config.DEFAULT_PORT

    def test_an_empty_network_list_is_the_default_not_an_open_door(self, monkeypatch):
        monkeypatch.setenv(server_config.NETWORKS_ENV, "   ")
        assert load_config().networks == server_config.DEFAULT_NETWORKS


class TestFactory:
    def test_the_app_carries_its_config_and_database(self, tmp_path):
        config = ServerConfig(
            db_path=tmp_path / "server.db", host="127.0.0.1", port=1,
            networks=("127.0.0.1/32",), max_body_bytes=1_000_000,
        )
        app = create_app(config)
        assert isinstance(app, FastAPI)
        assert app.state.config is config
        assert app.state.db.path == tmp_path / "server.db"

    def test_the_schema_is_there_on_the_first_connection(self, tmp_path):
        config = ServerConfig(
            db_path=tmp_path / "sub" / "server.db", host="127.0.0.1", port=1,
            networks=(), max_body_bytes=1_000_000,
        )
        conn = create_app(config).state.db.connect()
        assert [row[1] for row in conn.execute("PRAGMA table_info(server_records)")] == [
            "id", *db.REC_COLS,
        ]
        conn.close()

    def test_exchange_converts_against_the_server_database(self, tmp_path, monkeypatch):
        from ccreport import exchange

        monkeypatch.setattr(exchange, "_store", None)
        config = ServerConfig(
            db_path=tmp_path / "server.db", host="127.0.0.1", port=1,
            networks=(), max_body_bytes=1_000_000,
        )
        create_app(config)
        exchange._store.save_exchange_rates({"2026-08-10": 10.5})
        assert exchange._store.get_exchange_rates("2026-08-01") == {"2026-08-10": 10.5}

    def test_no_config_reads_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv(server_config.DB_ENV, str(tmp_path / "from-env.db"))
        assert create_app().state.db.path == tmp_path / "from-env.db"


class TestNetworkGate:
    @pytest.mark.parametrize(
        ("addr", "networks", "allowed"),
        [
            ("10.1.2.3", ["10.0.0.0/8"], True),
            ("11.1.2.3", ["10.0.0.0/8"], False),
            ("192.168.1.5", ["192.168.1.5"], True),
            ("192.168.1.6", ["192.168.1.5"], False),
            ("::1", ["::1/128"], True),
            ("10.1.2.3", ["not-a-network", "10.0.0.0/8"], True),
            ("10.1.2.3", [], False),
            ("not-an-address", ["10.0.0.0/8"], False),
        ],
    )
    def test_who_reaches_the_pages(self, addr, networks, allowed):
        assert ip_allowed(addr, networks) is allowed

    def _client(self, networks) -> TestClient:
        app = FastAPI()

        @app.get("/pages", dependencies=[Depends(restrict_remote_addr_dep(networks))])
        def pages() -> dict:
            return {"ok": True}

        return TestClient(app)

    def test_an_allowed_caller_gets_the_page(self):
        assert self._client(["127.0.0.1/32"]).get("/pages").status_code == 200

    def test_everyone_else_gets_a_403(self):
        assert self._client(["10.0.0.0/8"]).get("/pages").status_code == 403


class TestEntryPoint:
    def test_the_flags_default_to_the_environment(self, monkeypatch):
        from ccreport.server import fastapi_server

        monkeypatch.setenv(server_config.HOST_ENV, "0.0.0.0")
        monkeypatch.setenv(server_config.PORT_ENV, "9100")
        args = fastapi_server.parse_args([])
        assert (args.host, args.port, args.reload) == ("0.0.0.0", 9100, False)

    def test_a_flag_wins_over_its_variable(self, monkeypatch):
        from ccreport.server import fastapi_server

        monkeypatch.setenv(server_config.PORT_ENV, "9100")
        assert fastapi_server.parse_args(["--port", "9200", "--reload"]).port == 9200
