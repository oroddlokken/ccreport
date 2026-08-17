"""Restricted machines, the network gate, and the command that configures both."""

from __future__ import annotations

import io
import ipaddress
import stat

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient
from rich.console import Console
from test_push import TS, _cached_file, _write_config

from ccreport import cache_db, push
from ccreport import ccreport as ccr
from ccreport.server.factory import create_app


@pytest.fixture(autouse=True)
def isolate_server_globals(monkeypatch):
    from ccreport import exchange

    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})


def _server(**over) -> push.ServerConfig:
    fields = {
        "url": "https://ccr.example.net", "token": "tok", "label": "Laptop",
        "machine_id": "laptop-1", "restricted": True, "allow": ("ccr-projA",),
        "salt": "s41t",
    }
    fields.update(over)
    return push.ServerConfig(**fields)


def _record(**over) -> dict:
    rec = {
        "mid": "m1", "model": "claude-haiku-4-5", "ts": TS, "utc_offset": 0,
        "sid": "sess-1", "project": "ccr-projB", "cwd": "/tmp/ccr-projB",
        "repo": "github.com/o/secret", "dk": "m1:r1", "cost": None,
        "input_tokens": 1000, "output_tokens": 200,
        "cache_create": 5000, "cache_read": 30000,
        "account_uuid": "u", "account_label": "me@work.example",
    }
    rec.update(over)
    return rec


class TestRedaction:
    def test_an_unrestricted_machine_sends_real_names(self):
        rec = _record()
        assert push.redact(rec, _server(restricted=False)) == rec

    def test_a_project_outside_the_allow_list_loses_its_identity(self):
        out = push.redact(_record(), _server())
        assert out["project"] is None
        assert out["cwd"] is None
        assert out["repo"] is None
        assert out["sid"] is None

    def test_no_salted_value_reaches_the_request(self):
        """A pseudonym per project counts the private projects for the server."""
        out = push.redact(_record(), _server())
        salted = {
            push.pseudonym("s41t", "ccr-projB"), push.pseudo_session("s41t", "sess-1"),
        }
        assert not salted & {str(value) for value in out.values()}

    def test_its_token_counts_match_the_source_exactly(self):
        """Every record still pays; what it loses is who it was."""
        source = _record()
        out = push.redact(source, _server())
        for key in ("input_tokens", "output_tokens", "cache_create", "cache_read",
                    "model", "ts", "cost", "account_uuid"):
            assert out[key] == source[key]

    def test_an_opted_in_project_survives_intact(self):
        rec = _record(project="ccr-projA", cwd="/tmp/ccr-projA")
        assert push.redact(rec, _server()) == rec

    def test_two_projects_fold_into_the_same_nothing(self):
        """One bucket per account, so the server cannot count them."""
        one = push.redact(_record(project="one"), _server())
        two = push.redact(_record(project="two"), _server())
        assert one["project"] is two["project"] is None

    def test_the_salt_no_longer_changes_what_is_sent(self):
        """Nothing derives from it now; REDACTION_SHAPE is what moves the policy."""
        mine = push.redact(_record(), _server())
        theirs = push.redact(_record(), _server(salt="other"))
        assert mine == theirs

    def test_a_record_with_no_session_keeps_none(self):
        assert push.redact(_record(sid=None), _server())["sid"] is None

    def test_an_empty_allow_list_identifies_nothing(self):
        """A valid restricted state: complete usage, no names at all."""
        out = push.redact(_record(project="ccr-projA"), _server(allow=()))
        assert out["project"] is None


class TestFailClosed:
    def test_a_lost_restricted_flag_is_overruled_by_the_marker(self, tmp_path):
        """An edit or a restored older copy must not unredact a machine."""
        path = _write_config(tmp_path)
        push._marker_path(path).write_text("x")
        server = push.load_config(path)[0]
        assert server.restricted
        assert server.allow == ()

    def test_the_marker_is_written_when_restricted_is_set(self, tmp_path):
        path = tmp_path / "push.toml"
        push.write_server(path, "https://ccr.example.net", {
            "token": "t", "restricted": True, "allow": ["a"], "salt": "s",
        })
        assert push._marker_path(path).exists()

    def test_an_unrestricted_machine_gets_no_marker(self, tmp_path):
        path = tmp_path / "push.toml"
        push.write_server(path, "https://ccr.example.net", {"token": "t"})
        assert not push._marker_path(path).exists()

    def test_a_broken_file_pushes_nothing_at_all(self, tmp_path):
        """No server means no request, which is already the closed state."""
        path = tmp_path / "push.toml"
        path.write_text("= = not toml")
        push._marker_path(path).write_text("x")
        assert push.load_config(path) == []


class TestPolicyHash:
    def test_the_same_policy_hashes_the_same(self):
        assert push.policy_hash(_server()) == push.policy_hash(_server())

    @pytest.mark.parametrize("change", [
        {"restricted": False}, {"allow": ("ccr-projA", "ccr-projB")},
        {"allow": ()}, {"salt": "other"},
    ])
    def test_every_part_of_it_moves_the_hash(self, change):
        assert push.policy_hash(_server()) != push.policy_hash(_server(**change))

    def test_the_local_merge_rules_are_in_it(self):
        """They decide which name the allow list is matched against."""
        assert push.policy_hash(_server(), "rule-a") != push.policy_hash(_server(), "rule-b")

    def test_the_allow_list_order_does_not_matter(self):
        assert push.policy_hash(_server(allow=("a", "b"))) == push.policy_hash(
            _server(allow=("b", "a")),
        )

    def test_the_redaction_shape_is_in_it(self, monkeypatch):
        """A code edit moves nothing else here, so old rows would stand forever."""
        before = push.policy_hash(_server())
        monkeypatch.setattr(push, "REDACTION_SHAPE", "something-else")
        assert push.policy_hash(_server()) != before


class TestNetworkGate:
    def test_no_networks_is_no_gate(self):
        """What the personal machines want."""
        assert push.on_allowed_network(())

    def test_loopback_is_reachable_from_here(self):
        assert push.on_allowed_network(("127.0.0.0/8",))

    def test_a_network_this_machine_holds_no_address_in_blocks(self):
        assert not push.on_allowed_network(("192.0.2.0/24",))

    def test_a_malformed_cidr_blocks_rather_than_being_skipped(self):
        """A typo in a machine's config must not read as permission."""
        assert not push.on_allowed_network(("10.0.0.0/8", "not-a-network"))
        assert not push.on_allowed_network(("not-a-network", "127.0.0.0/8"))

    def test_ipv6_loopback_is_handled(self):
        assert push.on_allowed_network(("::1/128",))

    def test_any_one_matching_network_is_enough(self):
        assert push.on_allowed_network(("192.0.2.0/24", "127.0.0.0/8"))

    def test_it_sends_no_packets(self, monkeypatch):
        """A connected UDP socket picks a route without putting one on the wire."""
        sent = []
        real_socket = push.socket.socket

        class Watched(real_socket):
            def send(self, *a, **kw):
                sent.append(a)
                return 0

            def sendto(self, *a, **kw):
                sent.append(a)
                return 0

        monkeypatch.setattr(push.socket, "socket", Watched)
        push.on_allowed_network(("127.0.0.0/8",))
        assert sent == []

    def test_a_single_host_route_still_has_somewhere_to_aim(self):
        """next(hosts()) is empty for a /32, so the network address stands in."""
        assert push._probe_source_address(ipaddress.ip_network("127.0.0.1/32")) is not None


class TestGateInARun:
    @pytest.fixture
    def blocked_config(self, tmp_path):
        return _write_config(tmp_path, networks=["192.0.2.0/24"])

    def test_a_blocked_push_sends_nothing(self, blocked_config, monkeypatch):
        calls = []
        monkeypatch.setattr(
            push, "push_to", lambda server, **kw: calls.append(server.url),
        )
        results = push.run_once(config_path=blocked_config, force=True)
        assert calls == []
        assert results[0].blocked
        assert results[0].blocked_by == ("192.0.2.0/24",)

    def test_a_blocked_push_records_no_watermark(self, blocked_config, monkeypatch):
        """So everything queued goes out on the first run back inside the network."""
        _cached_file()
        push.run_once(config_path=blocked_config, force=True)
        assert cache_db.load_push_state("https://ccr.example.net") == {}

    def test_a_blocked_push_still_stamps_the_attempt(self, blocked_config):
        """A day off-network costs one process per interval, not one per render."""
        push.run_once(config_path=blocked_config, force=True)
        attempt, failures, stopped = cache_db.read_push_attempt("https://ccr.example.net")
        assert attempt > 0
        assert failures == 0, "being away is not a failure to back off from"
        assert not stopped

    def test_the_queue_goes_out_on_the_first_allowed_run(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(
            push, "push_to",
            lambda server, **kw: sent.append(server.url) or push.PushResult(server.url),
        )
        blocked = _write_config(tmp_path, networks=["192.0.2.0/24"])
        push.run_once(config_path=blocked, force=True)
        assert sent == []
        allowed = _write_config(tmp_path, networks=["127.0.0.0/8"])
        push.run_once(config_path=allowed, force=True)
        assert sent == ["https://ccr.example.net"]


class TestPolicyForcesARepush:
    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        app = create_app(sf.config(tmp_path / "server"))
        client = TestClient(app)
        token = sf.mint_for(app, "laptop-1", "Laptop")

        def post(server_config, batch):
            resp = client.post(
                "/v1/ingest", json={**batch, "client_version": "test"},
                headers={"Authorization": f"Bearer {server_config.token}"},
            )
            return resp.json()

        monkeypatch.setattr(push, "post_batch", post)
        self._token = token
        return app

    def _config(self, **over):
        return push.ServerConfig(
            url="http://testserver", token=self._token, label="Laptop", machine_id="",
            **over,
        )

    def test_an_unchanged_policy_does_not_resend(self, wired):
        _cached_file()
        config = self._config()
        push.push_to(config)
        assert push.push_to(config).accepted == []

    def test_a_project_leaving_the_allow_list_forces_a_full_repush(self, wired):
        """The files that named it are closed logs; nothing else takes it back."""
        _cached_file()
        push.push_to(self._config(restricted=True, allow=("ccr-projA",), salt="s"))
        result = push.push_to(self._config(restricted=True, allow=(), salt="s"))
        assert result.accepted == ["/p/a.jsonl"], "the watermark should have been cleared"

    def test_the_name_is_gone_from_the_server_afterwards(self, wired):
        _cached_file()
        push.push_to(self._config(restricted=True, allow=("ccr-projA",), salt="s"))
        assert [r["project"] for r in sf.stored(wired, "laptop-1")] == ["ccr-projA"]
        push.push_to(self._config(restricted=True, allow=(), salt="s"))
        assert [r["project"] for r in sf.stored(wired, "laptop-1")] != ["ccr-projA"]

    def test_a_restricted_push_keeps_the_token_counts(self, wired):
        _cached_file()
        push.push_to(self._config(restricted=True, allow=(), salt="s"))
        stored = sf.stored(wired, "laptop-1")[0]
        assert stored["t"] == [1000, 200, 5000, 30000]
        assert stored["cost"] > 0


class TestConnectCommand:
    @pytest.fixture
    def health(self, monkeypatch):
        """A server that accepts one token and refuses everything else."""
        def fetch(base, token):
            from ccreport.remote import RemoteError

            if token != "good":
                raise RemoteError(f"{base} refused that token")
            return {"label": "Laptop", "machine_id": "laptop-1", "records": 0}

        monkeypatch.setattr("ccreport.remote.fetch_health", fetch)

    def _run(self, monkeypatch, argv) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport", *argv])
        ccr.main()
        return buf.getvalue()

    def test_a_fresh_write_lands_at_mode_0600(self, tmp_path, monkeypatch, health):
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--config", str(path),
        ])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert push.load_config(path)[0].token == "good"

    def test_it_records_what_the_server_calls_this_machine(self, tmp_path, monkeypatch, health):
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--config", str(path),
        ])
        server = push.load_config(path)[0]
        assert (server.label, server.machine_id) == ("Laptop", "laptop-1")

    def test_a_rerun_updates_one_server_and_leaves_the_other(
        self, tmp_path, monkeypatch, health,
    ):
        path = tmp_path / "push.toml"
        for url in ("https://a.example", "https://b.example"):
            self._run(monkeypatch, [
                "server", "connect", url, "--token", "good", "--config", str(path),
            ])
        self._run(monkeypatch, [
            "server", "connect", "https://a.example", "--token", "good",
            "--only-on-network", "10.0.0.0/8", "--config", str(path),
        ])
        by_url = {s.url: s for s in push.load_config(path)}
        assert by_url["https://a.example"].networks == ("10.0.0.0/8",)
        assert by_url["https://b.example"].networks == ()
        assert by_url["https://b.example"].token == "good"

    def test_a_bad_token_writes_nothing(self, tmp_path, monkeypatch, health):
        """It fails now rather than silently at a background push in half an hour."""
        path = tmp_path / "push.toml"
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, [
                "server", "connect", "https://ccr.example.net", "--token", "wrong",
                "--config", str(path),
            ])
        assert exit_info.value.code == 1
        assert not path.exists()

    def test_a_world_writable_directory_is_refused(self, tmp_path, monkeypatch, health):
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o777)  # mkdir(mode=) is masked by the umask
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, [
                "server", "connect", "https://ccr.example.net", "--token", "good",
                "--config", str(shared / "push.toml"),
            ])
        assert exit_info.value.code == 1
        assert not (shared / "push.toml").exists()

    def test_opt_in_repos_sets_restricted_with_a_salt(self, tmp_path, monkeypatch, health):
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--opt-in-repos", "ccreport,kantine", "--config", str(path),
        ])
        server = push.load_config(path)[0]
        assert server.restricted
        assert server.allow == ("ccreport", "kantine")
        assert len(server.salt) == 32

    def test_the_names_resolve_through_a_merge_rule(self, tmp_path, monkeypatch, health):
        """An alias has to match the name a record actually carries after merging."""
        cache_db.add_project_override("name", "ccr-old", "ccr-new")
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--opt-in-repos", "ccr-old", "--config", str(path),
        ])
        assert push.load_config(path)[0].allow == ("ccr-new",)

    def test_an_empty_opt_in_list_restricts_and_identifies_nothing(self, tmp_path, monkeypatch,
                                                                   health):
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--opt-in-repos", "", "--config", str(path),
        ])
        server = push.load_config(path)[0]
        assert server.restricted
        assert server.allow == ()

    def test_the_bare_flag_restricts_and_identifies_nothing(self, tmp_path, monkeypatch, health):
        """`--opt-in-repos` with no value is opt-in with an empty list, not open."""
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--config", str(path), "--opt-in-repos",
        ])
        server = push.load_config(path)[0]
        assert server.restricted
        assert server.allow == ()
        assert len(server.salt) == 32

    def test_the_salt_survives_a_reconnect(self, tmp_path, monkeypatch, health):
        """Regenerating it would re-pseudonymize everything already on the server."""
        path = tmp_path / "push.toml"
        argv = ["server", "connect", "https://ccr.example.net", "--token", "good",
                "--opt-in-repos", "ccreport", "--config", str(path)]
        self._run(monkeypatch, argv)
        first = push.load_config(path)[0].salt
        self._run(monkeypatch, argv)
        assert push.load_config(path)[0].salt == first

    def test_only_on_network_writes_the_cidrs(self, tmp_path, monkeypatch, health):
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--only-on-network", "10.172.0.0/22, 10.8.0.0/16", "--config", str(path),
        ])
        assert push.load_config(path)[0].networks == ("10.172.0.0/22", "10.8.0.0/16")

    def test_omitting_it_leaves_no_gate(self, tmp_path, monkeypatch, health):
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--config", str(path),
        ])
        assert push.load_config(path)[0].networks == ()

    def test_the_interval_is_written_in_minutes_and_read_in_seconds(
        self, tmp_path, monkeypatch, health,
    ):
        path = tmp_path / "push.toml"
        out = self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--interval-minutes", "5", "--config", str(path),
        ])
        assert "interval_minutes = 5" in path.read_text()
        assert push.load_config(path)[0].interval_s == 300
        assert "Pushing every 5 min." in out

    def test_omitting_it_leaves_the_default(self, tmp_path, monkeypatch, health):
        path = tmp_path / "push.toml"
        self._run(monkeypatch, [
            "server", "connect", "https://ccr.example.net", "--token", "good",
            "--config", str(path),
        ])
        assert "interval_minutes" not in path.read_text()
        assert push.load_config(path)[0].interval_s == push.BASE_INTERVAL_S

    def test_a_non_positive_interval_is_refused(self, tmp_path, monkeypatch, health):
        """Writing it would store a value that reads back as the default."""
        path = tmp_path / "push.toml"
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, [
                "server", "connect", "https://ccr.example.net", "--token", "good",
                "--interval-minutes", "0", "--config", str(path),
            ])
        assert exit_info.value.code == 1
        assert not path.exists()


class TestStatusPrintsTheInterval:
    """The push interval is invisible otherwise: the file is 0600 and the spawn
    is detached, so this command is where a machine's own cadence is read off.
    """

    @pytest.fixture(autouse=True)
    def health(self, monkeypatch):
        monkeypatch.setattr(
            "ccreport.remote.fetch_health",
            lambda base, token: {"label": "Laptop", "machine_id": "laptop-1", "records": 3},
        )

    def _status(self, monkeypatch, path) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        monkeypatch.setattr(ccr.sys, "argv",
                            ["ccreport", "server", "status", "--config", str(path)])
        ccr.main()
        return buf.getvalue()

    def test_a_configured_one_prints_in_minutes(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path, interval_minutes=5)
        assert "interval     5 min" in self._status(monkeypatch, path)

    def test_an_entry_without_it_prints_the_default(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path)
        assert "interval     30 min" in self._status(monkeypatch, path)


class TestAllowDenyCommands:
    def _run(self, monkeypatch, argv) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport", *argv])
        ccr.main()
        return buf.getvalue()

    @pytest.fixture
    def configured(self, tmp_path):
        path = tmp_path / "push.toml"
        push.write_server(path, "https://ccr.example.net", {
            "token": "t", "restricted": True, "allow": ["ccr-projA"], "salt": "s",
        })
        return path

    def test_allow_adds_a_project(self, configured, monkeypatch):
        self._run(monkeypatch, [
            "server", "allow", "https://ccr.example.net", "ccr-projB",
            "--config", str(configured),
        ])
        assert push.load_config(configured)[0].allow == ("ccr-projA", "ccr-projB")

    def test_deny_removes_one(self, configured, monkeypatch):
        self._run(monkeypatch, [
            "server", "deny", "https://ccr.example.net", "ccr-projA",
            "--config", str(configured),
        ])
        assert push.load_config(configured)[0].allow == ()

    def test_both_force_the_repush_the_change_requires(self, configured, monkeypatch):
        cache_db.save_push_state("https://ccr.example.net", [("/p/a.jsonl", 1, 100)], 500.0)
        self._run(monkeypatch, [
            "server", "deny", "https://ccr.example.net", "ccr-projA",
            "--config", str(configured),
        ])
        assert cache_db.load_push_state("https://ccr.example.net") == {}

    def test_an_unknown_server_is_an_error(self, configured, monkeypatch):
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, [
                "server", "allow", "https://nowhere.example", "x",
                "--config", str(configured),
            ])
        assert exit_info.value.code == 1

    def test_allow_takes_several_projects_at_once(self, configured, monkeypatch):
        self._run(monkeypatch, [
            "server", "allow", "https://ccr.example.net", "ccr-projB", "ccr-projC",
            "--config", str(configured),
        ])
        assert push.load_config(configured)[0].allow == ("ccr-projA", "ccr-projB", "ccr-projC")

    def test_deny_takes_several_projects_at_once(self, tmp_path, monkeypatch):
        path = tmp_path / "push.toml"
        push.write_server(path, "https://ccr.example.net", {
            "token": "t", "restricted": True,
            "allow": ["ccr-projA", "ccr-projB", "ccr-projC"], "salt": "s",
        })
        self._run(monkeypatch, [
            "server", "deny", "https://ccr.example.net", "ccr-projA", "ccr-projC",
            "--config", str(path),
        ])
        assert push.load_config(path)[0].allow == ("ccr-projB",)

    def test_one_server_needs_no_url(self, configured, monkeypatch):
        self._run(monkeypatch, [
            "server", "allow", "ccr-projB", "ccr-projC", "--config", str(configured),
        ])
        assert push.load_config(configured)[0].allow == ("ccr-projA", "ccr-projB", "ccr-projC")

    def test_two_servers_need_the_url_and_the_error_names_both(
        self, configured, monkeypatch, capsys,
    ):
        push.write_server(configured, "https://other.example.net", {"token": "t2"})
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, ["server", "allow", "ccr-projB", "--config", str(configured)])
        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        assert "https://ccr.example.net" in err
        assert "https://other.example.net" in err

    def test_a_url_with_no_project_is_an_error(self, configured, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, [
                "server", "allow", "https://ccr.example.net", "--config", str(configured),
            ])
        assert exit_info.value.code == 1
        assert "name a project" in capsys.readouterr().err
        assert push.load_config(configured)[0].allow == ("ccr-projA",)


class TestStatusCommand:
    def _run(self, monkeypatch, argv) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(ccr, "console", Console(file=buf, width=200, no_color=True))
        monkeypatch.setattr(ccr.sys, "argv", ["ccreport", *argv])
        ccr.main()
        return buf.getvalue()

    @pytest.fixture
    def reachable(self, monkeypatch):
        monkeypatch.setattr(
            "ccreport.remote.fetch_health",
            lambda base, token: {"label": "Laptop", "machine_id": "laptop-1", "records": 12},
        )

    def test_an_unconfigured_machine_says_so(self, tmp_path, monkeypatch):
        out = self._run(monkeypatch, [
            "server", "status", "--config", str(tmp_path / "absent.toml"),
        ])
        assert "ccreport server connect" in out

    def test_a_machine_that_has_never_pushed_says_never(
        self, tmp_path, monkeypatch, reachable,
    ):
        path = _write_config(tmp_path)
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "never" in out
        assert "laptop-1" in out
        assert "12 records" in out

    def test_it_names_the_policy_and_the_gate(self, tmp_path, monkeypatch, reachable):
        path = tmp_path / "push.toml"
        push.write_server(path, "https://ccr.example.net", {
            "token": "t", "restricted": True, "allow": ["ccr-projA"], "salt": "s",
            "networks": ["192.0.2.0/24"],
        })
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "restricted   yes" in out
        assert "ccr-projA" in out
        assert "192.0.2.0/24" in out
        assert "off-network" in out, "it should explain why nothing is being sent"

    def test_a_refused_token_is_shown_as_stopped(self, tmp_path, monkeypatch, reachable):
        path = _write_config(tmp_path)
        cache_db.write_push_attempt("https://ccr.example.net", 500.0, 1, stopped=True)
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "the token was refused" in out

    def test_a_failed_attempt_is_not_shown_as_a_push(self, tmp_path, monkeypatch, reachable):
        """The stamp moves on every outcome, so it must not sit under 'last push'."""
        path = _write_config(tmp_path)
        cache_db.write_push_attempt(
            "https://ccr.example.net", 500.0, 1, reason="refused: nothing is listening",
        )
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "last push    never" in out
        assert "last attempt failed" in out
        assert "refused: nothing is listening" in out

    def test_a_success_is_what_dates_the_last_push(self, tmp_path, monkeypatch, reachable):
        path = _write_config(tmp_path)
        cache_db.write_push_attempt("https://ccr.example.net", 500.0, 0, succeeded=True)
        cache_db.write_push_attempt("https://ccr.example.net", 900.0, 1, reason="gone")
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert ccr._fmt_epoch(500.0) in out, "the last push is the last one that stored records"
        assert ccr._fmt_epoch(900.0) in out, "the failed attempt keeps its own timestamp"

    def test_a_bare_server_command_reads_the_default_config(
        self, tmp_path, monkeypatch, reachable,
    ):
        """No subparser ran, so the namespace carries no --config to read."""
        path = _write_config(tmp_path)
        monkeypatch.setattr(push, "CONFIG_PATH", path)
        out = self._run(monkeypatch, ["server"])
        assert "laptop-1" in out

    def test_server_push_sends_and_reports(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path)
        monkeypatch.setattr(
            push, "push_to",
            lambda server, full=False, db_path=None: push.PushResult(
                server=server.url, accepted=["/p/a.jsonl"], records=7,
            ),
        )
        out = self._run(monkeypatch, ["server", "push", "--config", str(path)])
        assert "1 sent" in out
        assert "7 records" in out

    def test_server_push_reads_the_config_it_was_given(self, tmp_path, monkeypatch):
        """Not the default one: the flag is what makes the subcommand testable."""
        with pytest.raises(SystemExit) as exit_info:
            self._run(monkeypatch, [
                "server", "push", "--config", str(tmp_path / "absent.toml"),
            ])
        assert exit_info.value.code == 1

    def test_an_unreachable_server_is_reported_not_raised(self, tmp_path, monkeypatch):
        from ccreport.remote import RemoteError

        def refuse(base, token):
            raise RemoteError(f"{base} could not be reached: refused")

        monkeypatch.setattr("ccreport.remote.fetch_health", refuse)
        path = _write_config(tmp_path)
        out = self._run(monkeypatch, ["server", "status", "--config", str(path)])
        assert "unreachable" in out
