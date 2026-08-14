"""The web UI: minting, revoking, and who is allowed to reach any of it."""

from __future__ import annotations

import time

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport.server import tokens
from ccreport.server.factory import create_app

UI_ROUTES = ["/", "/machines", "/machines/laptop-1", "/accounts"]


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
    return line.split("--token", 1)[1].split()[0].removesuffix("</pre>")


def _command_from(page: str) -> str:
    """The whole connect command, policy flags included."""
    line = next(ln for ln in page.splitlines() if "--token" in ln)
    return line.split('<pre class="command">', 1)[-1].removesuffix("</pre>").strip()


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

    def test_a_health_check_alone_moves_the_token_stamp(self, app, client):
        """Which is why that column is not called a push."""
        token = _token_from(_mint(client).text)
        client.get("/v1/health", headers=sf.auth(token))
        assert app.state.db.connect().execute(
            "SELECT last_used_at FROM machine_tokens").fetchone()[0] is not None

    def test_an_id_nothing_was_minted_for_is_a_404(self, client):
        """Rendering it would draw a machine that does not exist, with 0 records."""
        assert client.get("/machines/never-minted").status_code == 404

    def test_the_token_column_is_named_for_what_stamps_it(self, client):
        """Any authenticated request moves last_used_at, health checks included."""
        _mint(client)
        body = client.get("/machines").text
        assert "Token last used" in body
        assert "Last push" not in body


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


class TestMintedPolicy:
    """The push policy is typed here and lands in the command, not in the database."""

    def _mint(self, client, **fields):
        data = {"machine_id": "laptop-1", "label": "Laptop"}
        data.update(fields)
        return client.post("/machines/mint", data=data)

    def test_both_mint_forms_carry_the_policy_fields(self, client):
        _mint(client)
        for route in ("/machines", "/machines/laptop-1"):
            body = client.get(route).text
            assert 'name="networks"' in body
            assert 'name="restricted"' in body
            assert 'name="allow"' in body

    def test_a_plain_mint_carries_no_policy_flag(self, client):
        command = _command_from(self._mint(client).text)
        assert "--only-on-network" not in command
        assert "--opt-in-repos" not in command

    def test_the_networks_field_becomes_one_comma_list(self, client):
        page = self._mint(client, networks="10.0.0.0/8, 192.168.1.0/24").text
        assert "--only-on-network 10.0.0.0/8,192.168.1.0/24" in _command_from(page)

    def test_the_checkbox_with_names_opts_those_projects_in(self, client):
        page = self._mint(client, restricted="1", allow="ccreport, kantine").text
        assert "--opt-in-repos ccreport,kantine" in _command_from(page)

    def test_the_checkbox_with_no_names_restricts_and_identifies_nothing(self, client):
        """The bare flag, which is what the CLI reads as restricted with an empty list."""
        command = _command_from(self._mint(client, restricted="1").text)
        assert command.endswith("--opt-in-repos")

    def test_names_without_the_checkbox_send_nothing(self, client):
        """Unchecked is an open machine, whatever is left in the names field."""
        assert "--opt-in-repos" not in _command_from(self._mint(client, allow="ccreport").text)

    def test_a_typo_in_a_cidr_refuses_to_mint(self, app, client):
        resp = self._mint(client, networks="10.0.0.0/8, not-a-network")
        assert resp.status_code == 400
        assert "not-a-network is not a network." in resp.text
        assert app.state.db.connect().execute(
            "SELECT COUNT(*) FROM machine_tokens").fetchone()[0] == 0

    def test_the_minted_command_is_one_the_client_parses(self, client):
        page = self._mint(client, networks="10.0.0.0/8", restricted="1", allow="ccreport").text
        args = _command_from(page).split()
        assert args[:4] == ["ccreport", "server", "connect", "http://testserver"]
        assert args[-4:-2] == ["--only-on-network", "10.0.0.0/8"]
        assert args[-2:] == ["--opt-in-repos", "ccreport"]


class TestMintedPage:
    def test_the_form_says_where_the_policy_lives(self, client):
        """It is the machine's file, so the page has to point at where to edit it."""
        _mint(client)
        for route in ("/machines", "/machines/laptop-1"):
            body = client.get(route).text
            assert "push.toml" in body
            assert "ccreport server allow" in body

    def test_the_form_says_which_fields_allow_and_deny_reach(self, client):
        """They write the allow list alone; networks and restricted need connect."""
        assert "need connect run again" in client.get("/machines").text

    def test_the_command_has_a_copy_control(self, client):
        page = _mint(client).text
        assert 'class="command-box"' in page
        assert 'class="ghost copy"' in page
        assert "/static/copy.js" in page


class TestConnectCommand:
    def test_a_shell_character_in_a_name_is_quoted(self):
        """The command is pasted into a shell, which would otherwise read it."""
        command = tokens.connect_command(
            "http://x/", "t", restricted=True, allow="a;rm -rf b",
        )
        assert command.endswith("--opt-in-repos 'a;rm,-rf,b'")

    def test_the_bare_flag_goes_last(self):
        """It takes an optional value, so anything after it would be swallowed."""
        command = tokens.connect_command(
            "http://x/", "t", networks="10.0.0.0/8", restricted=True,
        )
        assert command.endswith("--opt-in-repos")

    def test_csv_list_takes_commas_or_spaces(self):
        assert tokens.csv_list(" a, b  c,,d ") == "a,b,c,d"
        assert tokens.csv_list("  ") == ""


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


class TestAccountsPage:
    """Where the login email gets a name the server draws instead."""

    def _push(self, client, **over):
        """One record stamped now, so the dashboard's default range covers it."""
        token = _token_from(_mint(client).text)
        over.setdefault("ts", time.time())
        return client.post(
            "/v1/ingest", json=sf.batch([sf.record(**over)]), headers=sf.auth(token),
        )

    def test_an_empty_server_says_so(self, client):
        assert "No machine has pushed a record yet." in client.get("/accounts").text

    def test_it_lists_the_account_with_its_label_records_and_cost(self, client):
        self._push(client)
        body = client.get("/accounts").text
        assert "me@example.net" in body
        assert "acct-1" in body
        assert 'name="alias"' in body

    def test_the_nav_reaches_it(self, client):
        assert 'href="/accounts"' in client.get("/machines").text

    def test_setting_an_alias_renames_the_account_on_the_dashboard(self, client):
        self._push(client)
        resp = client.post("/accounts/acct-1/alias", data={"alias": "personal"})
        assert resp.status_code == 200
        body = client.get("/").text
        assert "personal" in body
        assert "me@example.net" not in body

    def test_the_field_comes_back_holding_what_was_set(self, client):
        self._push(client)
        client.post("/accounts/acct-1/alias", data={"alias": "personal"})
        assert 'value="personal"' in client.get("/accounts").text

    def test_clearing_it_puts_the_label_back(self, client):
        self._push(client)
        client.post("/accounts/acct-1/alias", data={"alias": "personal"})
        client.post("/accounts/acct-1/alias", data={"alias": "  "})
        assert "me@example.net" in client.get("/").text
        assert client.app.state.db.connect().execute(
            "SELECT COUNT(*) FROM account_aliases").fetchone()[0] == 0

    def test_the_merged_report_reads_the_same_name(self, client):
        self._push(client)
        client.post("/accounts/acct-1/alias", data={"alias": "personal"})
        assert client.get("/v1/report/account").json()["accounts"] == ["personal"]

    def test_a_disallowed_address_cannot_read_or_set_one(self, tmp_path):
        gated = TestClient(create_app(sf.config(tmp_path, networks=sf.ELSEWHERE)))
        assert gated.get("/accounts").status_code == 403
        assert gated.post("/accounts/acct-1/alias", data={"alias": "x"}).status_code == 403


class TestDeletingATokenAndAMachine:
    """Revoking is for a machine still out there; deleting is for a mistake."""

    def _push(self, client, token, **over):
        return client.post(
            "/v1/ingest", json=sf.batch(**over), headers=sf.auth(token),
        )

    def test_the_token_table_offers_a_delete(self, client):
        _mint(client)
        assert "Delete" in client.get("/machines/laptop-1").text

    def test_deleting_a_token_removes_its_row(self, app, client):
        token = _token_from(_mint(client).text)
        client.post(f"/tokens/{tokens.token_hash(token)}/delete")
        assert app.state.db.connect().execute(
            "SELECT COUNT(*) FROM machine_tokens").fetchone()[0] == 0

    def test_a_deleted_token_stops_the_next_push(self, client):
        token = _token_from(_mint(client).text)
        assert self._push(client, token).status_code == 200
        client.post(f"/tokens/{tokens.token_hash(token)}/delete")
        assert self._push(client, token, mtime_ns=2).status_code == 401

    def test_deleting_one_token_leaves_the_other_working(self, client):
        first = _token_from(_mint(client).text)
        second = _token_from(_mint(client).text)
        client.post(f"/tokens/{tokens.token_hash(first)}/delete")
        assert client.get("/v1/health", headers=sf.auth(first)).status_code == 401
        assert client.get("/v1/health", headers=sf.auth(second)).status_code == 200

    def test_revoke_still_only_stamps_the_row(self, app, client):
        token = _token_from(_mint(client).text)
        client.post(f"/tokens/{tokens.token_hash(token)}/revoke")
        assert app.state.db.connect().execute(
            "SELECT revoked_at FROM machine_tokens").fetchone()[0] is not None

    def test_the_page_asks_for_the_id_before_it_deletes_the_machine(self, client):
        _mint(client)
        body = client.get("/machines/laptop-1").text
        assert 'action="/machines/laptop-1/delete"' in body
        assert 'name="confirm"' in body

    def test_the_typed_id_takes_the_machine_and_everything_under_it(self, app, client):
        token = _token_from(_mint(client).text)
        self._push(client, token)
        resp = client.post("/machines/laptop-1/delete", data={"confirm": "laptop-1"})
        assert resp.status_code == 200
        assert "Deleted laptop-1 and its 1 records." in resp.text
        conn = app.state.db.connect()
        for table in ("machines", "machine_tokens", "ingest_files", "server_records"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table

    def test_the_deleted_machine_is_a_404(self, client):
        _mint(client)
        client.post("/machines/laptop-1/delete", data={"confirm": "laptop-1"})
        assert client.get("/machines/laptop-1").status_code == 404

    @pytest.mark.parametrize("confirm", ["", "laptop", " laptop-1x"])
    def test_a_confirmation_that_does_not_match_deletes_nothing(self, app, client, confirm):
        token = _token_from(_mint(client).text)
        self._push(client, token)
        resp = client.post("/machines/laptop-1/delete", data={"confirm": confirm})
        assert resp.status_code == 400
        assert "Nothing was deleted." in resp.text
        assert app.state.db.connect().execute(
            "SELECT COUNT(*) FROM server_records").fetchone()[0] == 1

    def test_a_padded_id_still_matches(self, client):
        """The field is typed by hand and a browser is happy to trail a space."""
        _mint(client)
        resp = client.post("/machines/laptop-1/delete", data={"confirm": " laptop-1 "})
        assert "Deleted laptop-1" in resp.text

    def test_deleting_a_machine_that_never_existed_is_a_404(self, client):
        assert client.post(
            "/machines/never-minted/delete", data={"confirm": "never-minted"},
        ).status_code == 404

    def test_the_dashboard_drops_its_spend(self, app, client):
        """cached_build keys on content_stamp, which the cascade has to move."""
        from ccreport.server import dashboard

        token = _token_from(_mint(client).text)
        self._push(client, token, records=[sf.record(ts=time.time())])
        assert dashboard.cached_build(app.state.db, 30).total_cost > 0
        client.post("/machines/laptop-1/delete", data={"confirm": "laptop-1"})
        assert dashboard.cached_build(app.state.db, 30).total_cost == 0

    def test_neither_delete_is_reachable_from_outside(self, tmp_path):
        gated = TestClient(create_app(sf.config(tmp_path, networks=sf.ELSEWHERE)))
        assert gated.post("/tokens/abc/delete").status_code == 403
        assert gated.post("/machines/x/delete", data={"confirm": "x"}).status_code == 403
