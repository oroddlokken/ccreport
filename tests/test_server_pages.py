"""The web UI: minting, revoking, and who is allowed to reach any of it."""

from __future__ import annotations

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport.server import tokens
from ccreport.server.factory import create_app

UI_ROUTES = ["/", "/machines", "/machines/laptop-1"]


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    """Undo what create_app changes process-wide, and keep the API off the wire."""
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


@pytest.fixture
def app(tmp_path):
    return create_app(sf.config(tmp_path))


@pytest.fixture
def client(app):
    return TestClient(app)


def _mint(client, machine_id="laptop-1", label="Laptop"):
    return client.post("/machines/mint", data={"machine_id": machine_id, "label": label})


def _token_from(page: str) -> str:
    """The token out of the rendered connect command."""
    line = next(ln for ln in page.splitlines() if "--token" in ln)
    return line.split("--token")[1].strip().removesuffix("</pre>").strip()


class TestMachinesPage:
    def test_an_empty_server_says_so(self, client):
        body = client.get("/machines").text
        assert "No machine has a token yet." in body

    def test_a_minted_machine_shows_up_as_active(self, client):
        _mint(client)
        body = client.get("/machines").text
        assert "Laptop" in body
        assert "active" in body

    def test_the_page_reports_what_that_machine_has_stored(self, app, client):
        token = _token_from(_mint(client).text)
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert ">1<" in client.get("/machines").text.replace(" ", "").replace("\n", "")

    def test_a_machine_page_lists_its_tokens(self, client):
        _mint(client)
        body = client.get("/machines/laptop-1").text
        assert "Revoke" in body
        assert "active" in body


class TestMinting:
    def test_the_token_it_shows_is_one_ingest_accepts(self, client):
        token = _token_from(_mint(client).text)
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert resp.status_code == 200
        assert resp.json()["machine_id"] == "laptop-1"

    def test_the_token_cannot_be_recovered_from_the_database(self, app, client):
        """Only a hash is stored, which is what makes a database copy useless."""
        token = _token_from(_mint(client).text)
        rows = app.state.db.connect().execute("SELECT * FROM machine_tokens").fetchall()
        stored = " ".join(str(value) for row in rows for value in row)
        assert token not in stored
        assert tokens.token_hash(token) in stored

    def test_the_page_shows_the_command_that_consumes_it(self, client):
        page = _mint(client).text
        token = _token_from(page)
        assert f"ccreport server connect http://testserver --token {token}" in page

    def test_the_command_matches_what_the_connect_client_will_parse(self, client):
        """Guards the shape ccreport-xx2g has to accept: URL, then --token."""
        token = _token_from(_mint(client).text)
        command = tokens.connect_command("http://testserver/", token)
        assert command.split() == [
            "ccreport", "server", "connect", "http://testserver", "--token", token,
        ]

    def test_two_mints_for_one_machine_are_two_live_tokens(self, app, client):
        first = _token_from(_mint(client).text)
        second = _token_from(_mint(client).text)
        assert first != second
        for token in (first, second):
            assert client.get("/v1/health", headers=sf.auth(token)).status_code == 200

    def test_a_label_left_blank_falls_back_to_the_machine_id(self, client):
        client.post("/machines/mint", data={"machine_id": "bare-1", "label": ""})
        assert "bare-1" in client.get("/machines").text


class TestRevoking:
    def test_revoking_stops_the_next_push(self, client):
        token = _token_from(_mint(client).text)
        assert client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token)).status_code == 200
        client.post(f"/tokens/{tokens.token_hash(token)}/revoke")
        assert client.post("/v1/ingest", json=sf.batch(mtime_ns=2), headers=sf.auth(token)
                           ).status_code == 401

    def test_a_revoked_token_reads_as_revoked_on_the_page(self, client):
        token = _token_from(_mint(client).text)
        client.post(f"/tokens/{tokens.token_hash(token)}/revoke")
        assert "revoked" in client.get("/machines/laptop-1").text

    def test_revoking_one_token_leaves_the_other_working(self, client):
        first = _token_from(_mint(client).text)
        second = _token_from(_mint(client).text)
        client.post(f"/tokens/{tokens.token_hash(first)}/revoke")
        assert client.get("/v1/health", headers=sf.auth(first)).status_code == 401
        assert client.get("/v1/health", headers=sf.auth(second)).status_code == 200

    def test_revoking_twice_keeps_the_first_revocation_time(self, app, client):
        from ccreport.server import db

        token = _token_from(_mint(client).text)
        digest = tokens.token_hash(token)
        conn = app.state.db.connect()
        assert db.revoke_token(conn, digest, 100.0)
        assert not db.revoke_token(conn, digest, 200.0)
        assert conn.execute(
            "SELECT revoked_at FROM machine_tokens WHERE token_hash = ?", (digest,),
        ).fetchone()[0] == 100.0


class TestAccessControl:
    @pytest.fixture
    def gated(self, tmp_path):
        """A server whose allowlist admits nobody the TestClient can be."""
        return create_app(sf.config(tmp_path, networks=sf.ELSEWHERE))

    @pytest.mark.parametrize("route", UI_ROUTES)
    def test_a_disallowed_address_gets_403_on_every_ui_route(self, gated, route):
        assert TestClient(gated).get(route).status_code == 403

    def test_it_cannot_mint_either(self, gated):
        resp = TestClient(gated).post(
            "/machines/mint", data={"machine_id": "x", "label": "x"},
        )
        assert resp.status_code == 403

    def test_it_cannot_revoke_either(self, gated):
        assert TestClient(gated).post("/tokens/abc/revoke").status_code == 403

    def test_ingest_still_works_from_a_disallowed_address(self, gated):
        """A machine pushes from a hotel; its token is what admits it."""
        client = TestClient(gated)
        token = sf.mint_for(gated)
        assert client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token)).status_code == 200

    def test_health_works_from_a_disallowed_address_too(self, gated):
        """`ccreport server connect` runs on the machine, not on the network."""
        client = TestClient(gated)
        token = sf.mint_for(gated)
        assert client.get("/v1/health", headers=sf.auth(token)).status_code == 200
