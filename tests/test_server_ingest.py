"""POST /v1/ingest and GET /v1/health: what gets in, and what gets stored."""

from __future__ import annotations

import time

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport.server import db, tokens
from ccreport.server.factory import create_app


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    """Undo what create_app changes process-wide, and keep the API off the wire.

    exchange._store is a module global create_app repoints, and _warm_rates
    would otherwise reach Norges Bank once per push.
    """
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


@pytest.fixture
def app(tmp_path):
    return create_app(sf.config(tmp_path))


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def token(app):
    return sf.mint_for(app)


class TestAuth:
    def test_a_good_token_gets_in(self, client, token):
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert resp.status_code == 200

    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer"},
                                         {"Authorization": "Basic abc"}])
    def test_no_usable_token_is_401(self, client, token, headers):
        assert client.post("/v1/ingest", json=sf.batch(), headers=headers).status_code == 401

    def test_an_unknown_token_is_401(self, client, token):
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth("never-minted"))
        assert resp.status_code == 401

    def test_a_revoked_token_is_401_on_the_next_push(self, app, client, token):
        assert client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token)).status_code == 200
        conn = app.state.db.connect()
        db.revoke_token(conn, tokens.token_hash(token), time.time())
        conn.commit()
        assert client.post("/v1/ingest", json=sf.batch(mtime_ns=2), headers=sf.auth(token)
                           ).status_code == 401

    def test_a_refusal_says_nothing_about_why(self, client):
        """Naming the reason tells a caller its wrong token was once a right one."""
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth("never-minted"))
        assert resp.json() == {"detail": ""}

    def test_the_batch_never_names_its_own_machine(self, app, client, token):
        """The machine comes from the token row, so a push writes nobody else's rows."""
        sf.mint_for(app, "other-machine", "Other")
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert resp.json()["machine_id"] == "laptop-1"
        assert sf.stored(app, "other-machine") == []

    def test_a_push_stamps_the_token_as_used(self, app, client, token):
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        row = app.state.db.connect().execute(
            "SELECT last_used_at FROM machine_tokens WHERE token_hash = ?",
            (tokens.token_hash(token),),
        ).fetchone()
        assert row[0] is not None


class TestStoring:
    def test_a_good_push_stores_every_record_it_carried(self, app, client, token):
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert resp.json()["files"] == [
            {"path": "/p/a.jsonl", "status": "accepted", "records": 1, "detail": None},
        ]
        assert len(sf.stored(app, "laptop-1")) == 1

    def test_the_server_prices_the_record_itself(self, app, client, token):
        """A machine that has not pulled must not write a stale price."""
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        rec = sf.stored(app, "laptop-1")[0]
        assert rec["cost"] > 0
        assert rec["log_cost"] is None

    def test_a_cost_the_log_carried_is_kept_apart_from_the_computed_one(
        self, app, client, token,
    ):
        client.post("/v1/ingest", json=sf.batch([sf.record(cost=1.5)]), headers=sf.auth(token))
        rec = sf.stored(app, "laptop-1")[0]
        assert rec["log_cost"] == 1.5
        assert rec["cost"] != 1.5

    def test_the_oslo_date_is_stamped_at_ingest(self, app, client, token):
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert sf.stored(app, "laptop-1")[0]["oslo_date"] == "2026-02-02"

    def test_a_redacted_record_keeps_its_counts_and_loses_its_identity(
        self, app, client, token,
    ):
        """A project not opted in still pays; it just does not say where."""
        bare = sf.record(sid=None, project=None, cwd=None, repo=None)
        client.post("/v1/ingest", json=sf.batch([bare]), headers=sf.auth(token))
        rec = sf.stored(app, "laptop-1")[0]
        assert (rec["sid"], rec["project"], rec["cwd"], rec["repo"]) == (None, None, None, None)
        assert rec["t"] == [1000, 200, 5000, 30000]

    def test_a_pseudo_model_costs_a_known_zero(self, app, client, token):
        """<synthetic> had no call, so it has no price and that is not an error."""
        resp = client.post(
            "/v1/ingest",
            json=sf.batch([sf.record(model="<synthetic>", input_tokens=0, output_tokens=0,
                                     cache_create=0, cache_read=0)]),
            headers=sf.auth(token),
        )
        assert resp.json()["files"][0]["status"] == "accepted"
        assert sf.stored(app, "laptop-1")[0]["cost"] == 0.0


class TestUnknownModel:
    def test_an_unpriced_model_fails_its_file_loudly(self, app, client, token):
        """A silent zero is a week of money that looks like an idle week."""
        resp = client.post(
            "/v1/ingest", json=sf.batch([sf.record(model="gpt-9-ultra")]), headers=sf.auth(token),
        )
        result = resp.json()["files"][0]
        assert result["status"] == "rejected"
        assert "gpt-9-ultra" in result["detail"]
        assert sf.stored(app, "laptop-1") == []

    def test_a_rejected_file_does_not_take_the_batch_with_it(self, app, client, token):
        body = sf.batch()
        body["files"].append({
            "path": "/p/b.jsonl", "mtime_ns": 1, "size": 10,
            "records": [sf.record(model="gpt-9-ultra")],
        })
        resp = client.post("/v1/ingest", json=body, headers=sf.auth(token))
        assert [f["status"] for f in resp.json()["files"]] == ["accepted", "rejected"]
        assert len(sf.stored(app, "laptop-1", "/p/a.jsonl")) == 1

    def test_a_rejected_file_leaves_no_fingerprint_to_skip_on(self, app, client, token):
        client.post(
            "/v1/ingest", json=sf.batch([sf.record(model="gpt-9-ultra")]), headers=sf.auth(token),
        )
        assert db.file_fingerprint(app.state.db.connect(), "laptop-1", "/p/a.jsonl") is None


class TestRepush:
    def test_an_unchanged_file_is_skipped(self, app, client, token):
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert resp.json()["files"][0]["status"] == "skipped"
        assert len(sf.stored(app, "laptop-1")) == 1

    def test_replace_stores_a_file_whose_fingerprint_has_not_moved(self, app, client, token):
        """A restricted machine re-sending closed logs under new names."""
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        again = sf.batch([sf.record(mid="renamed", project=None)])
        again["files"][0]["replace"] = True
        resp = client.post("/v1/ingest", json=again, headers=sf.auth(token))
        assert resp.json()["files"][0]["status"] == "accepted"
        assert [r["mid"] for r in sf.stored(app, "laptop-1")] == ["renamed"]

    def test_a_grown_file_replaces_its_rows(self, app, client, token):
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        grown = sf.batch([sf.record(), sf.record(mid="msg_2", dk="msg_2:req_2")],
                         mtime_ns=2, size=200)
        resp = client.post("/v1/ingest", json=grown, headers=sf.auth(token))
        assert resp.json()["files"][0] == {
            "path": "/p/a.jsonl", "status": "accepted", "records": 2, "detail": None,
        }
        assert [r["mid"] for r in sf.stored(app, "laptop-1")] == ["msg_1", "msg_2"]

    def test_a_truncated_file_leaves_no_stale_rows(self, app, client, token):
        """A JSONL only grows, but a rotated log must not keep the old one's rows."""
        client.post(
            "/v1/ingest",
            json=sf.batch([sf.record(), sf.record(mid="msg_2", dk="msg_2:req_2")], size=200),
            headers=sf.auth(token),
        )
        client.post(
            "/v1/ingest",
            json=sf.batch([sf.record(mid="msg_9", dk="msg_9:req_9")], mtime_ns=9, size=10),
            headers=sf.auth(token),
        )
        assert [r["mid"] for r in sf.stored(app, "laptop-1")] == ["msg_9"]

    def test_two_machines_pushing_the_same_path_both_keep_their_rows(self, app, client, token):
        """A synced home directory is two machines' copies, not one overwriting the other."""
        other = sf.mint_for(app, "desktop-1", "Desktop")
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(other))
        client.post(
            "/v1/ingest",
            json=sf.batch([sf.record(mid="msg_9", dk="msg_9:req_9")], mtime_ns=2, size=200),
            headers=sf.auth(token),
        )
        assert [r["mid"] for r in sf.stored(app, "desktop-1")] == ["msg_1"]
        assert [r["mid"] for r in sf.stored(app, "laptop-1")] == ["msg_9"]


class TestBodyLimit:
    def test_an_oversized_batch_is_413_and_names_the_limit(self, tmp_path, token):
        app = create_app(sf.config(tmp_path, max_body_bytes=200))
        client = TestClient(app)
        live = sf.mint_for(app)
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(live))
        assert resp.status_code == 413
        assert "200 byte limit" in resp.json()["detail"]

    def test_a_batch_inside_the_limit_still_gets_through(self, tmp_path):
        app = create_app(sf.config(tmp_path, max_body_bytes=1_000_000))
        client = TestClient(app)
        live = sf.mint_for(app)
        assert client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(live)).status_code == 200


class TestHealth:
    def test_it_describes_what_the_token_belongs_to(self, client, token):
        body = client.get("/v1/health", headers=sf.auth(token)).json()
        assert body["machine_id"] == "laptop-1"
        assert body["label"] == "Laptop"
        assert body["records"] == 0
        assert body["version"]

    def test_the_record_count_follows_a_push(self, client, token):
        client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert client.get("/v1/health", headers=sf.auth(token)).json()["records"] == 1

    def test_a_bad_token_fails_at_setup_rather_than_at_the_first_push(self, client):
        assert client.get("/v1/health", headers=sf.auth("never-minted")).status_code == 401


class TestRateLimitSamples:
    """The other half of a push: how full each window got, merged per account."""

    def _push(self, client, token, samples):
        return client.post(
            "/v1/ingest", json=sf.sample_batch(samples), headers=sf.auth(token),
        )

    def test_a_batch_of_samples_is_stored_and_counted(self, app, client, token):
        resp = self._push(client, token, [sf.sample(), sf.sample(ts=1_770_000_300.0)])
        assert resp.json()["samples"] == 2
        rows = db.load_rate_limit_samples(app.state.db.connect())
        assert [row["ts"] for row in rows] == [1_770_000_000.0, 1_770_000_300.0]

    def test_a_sample_carries_the_account_the_client_resolved(self, app, client, token):
        self._push(client, token, [sf.sample()])
        [row] = db.load_rate_limit_samples(app.state.db.connect())
        assert (row["account_uuid"], row["account_label"]) == ("acct-1", "me@example.net")

    def test_the_machine_label_rides_back_out_with_the_row(self, app, client, token):
        self._push(client, token, [sf.sample()])
        [row] = db.load_rate_limit_samples(app.state.db.connect())
        assert row["machine"] == "Laptop"

    def test_a_re_pushed_sample_replaces_itself(self, app, client, token):
        """A --full offers the whole history again; it must not double the rows."""
        self._push(client, token, [sf.sample(used_pct=12.0)])
        self._push(client, token, [sf.sample(used_pct=13.0)])
        rows = db.load_rate_limit_samples(app.state.db.connect())
        assert [row["used_pct"] for row in rows] == [13.0]

    def test_a_batch_with_no_samples_stores_none(self, app, client, token):
        resp = client.post("/v1/ingest", json=sf.batch(), headers=sf.auth(token))
        assert resp.json()["samples"] == 0
        assert db.load_rate_limit_samples(app.state.db.connect()) == []

    def test_a_push_can_carry_records_and_samples_together(self, app, client, token):
        batch = {**sf.batch(), "samples": [sf.sample()]}
        resp = client.post("/v1/ingest", json=batch, headers=sf.auth(token))
        assert resp.json()["samples"] == 1
        assert len(sf.stored(app, "laptop-1")) == 1

    def test_the_bounds_select_by_instant(self, app, client, token):
        self._push(client, token, [sf.sample(), sf.sample(ts=1_770_000_300.0)])
        conn = app.state.db.connect()
        rows = db.load_rate_limit_samples(conn, 1_770_000_100.0, 1_770_000_400.0)
        assert [row["ts"] for row in rows] == [1_770_000_300.0]

    def test_deleting_a_machine_takes_its_samples(self, app, client, token):
        self._push(client, token, [sf.sample()])
        conn = app.state.db.connect()
        db.delete_machine(conn, "laptop-1")
        conn.commit()
        assert db.load_rate_limit_samples(conn) == []

    def test_samples_move_the_content_stamp(self, app, client, token):
        """A push can carry samples and no file, and the window pages read them."""
        conn = app.state.db.connect()
        before = db.content_stamp(conn)
        self._push(client, token, [sf.sample()])
        assert db.content_stamp(conn) != before

    def test_an_unauthenticated_sample_push_is_refused(self, client):
        resp = client.post("/v1/ingest", json=sf.sample_batch([sf.sample()]))
        assert resp.status_code == 401
