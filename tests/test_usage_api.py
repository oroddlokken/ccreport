"""Tests for usage_api.py's follower wait loop and token lookup."""

from __future__ import annotations

import subprocess
from urllib.error import HTTPError

import pytest
from _narrow import present

from ccreport import usage_api as gcu


class FakeClock:
    """Drives the loop without real time: sleeping advances monotonic()."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if len(self.sleeps) > 1000:
            msg = "poll loop did not terminate"
            raise AssertionError(msg)
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Fake clock plus a follower that never finds anything to emit."""
    c = FakeClock()
    monkeypatch.setattr(gcu.time, "monotonic", c.monotonic)
    monkeypatch.setattr(gcu.time, "sleep", c.sleep)
    monkeypatch.setattr(gcu, "read_usage_cache", lambda _max_age: None)
    monkeypatch.setattr(gcu, "read_usage_stale", lambda: None)
    monkeypatch.setattr(gcu, "_enrich_and_emit", lambda *a, **k: None)
    return c


def _wait(timeout: int) -> None:
    with pytest.raises(SystemExit):
        gcu._wait_for_leader(timeout, None, None)


class TestWaitForLeaderBackoff:
    def test_delays_double_from_the_minimum(self, clock):
        _wait(30)
        assert clock.sleeps[:5] == [0.1, 0.2, 0.4, 0.8, 1.0]

    def test_delay_is_capped(self, clock):
        _wait(30)
        assert max(clock.sleeps) == gcu.LEADER_POLL_MAX_DELAY

    def test_total_wait_does_not_exceed_the_timeout(self, clock):
        _wait(4)
        assert sum(clock.sleeps) == pytest.approx(4.0)
        assert clock.monotonic() == pytest.approx(1004.0)

    def test_backoff_reads_less_than_the_old_flat_half_second_poll(self, clock):
        _wait(30)
        assert len(clock.sleeps) < 60

    def test_zero_timeout_never_polls(self, clock, monkeypatch):
        reads = []
        monkeypatch.setattr(gcu, "read_usage_cache", reads.append)
        _wait(0)
        assert clock.sleeps == []
        assert reads == []


class TestWaitForLeaderResult:
    def test_emits_and_exits_zero_once_the_leader_writes(self, clock, monkeypatch):
        row = {"session_percent": 12}
        polls = {"n": 0}

        def fake_read(_max_age):
            polls["n"] += 1
            return row if polls["n"] == 3 else None

        emitted = []
        monkeypatch.setattr(gcu, "read_usage_cache", fake_read)
        monkeypatch.setattr(gcu, "_enrich_and_emit", lambda d, s, c: emitted.append(d))

        with pytest.raises(SystemExit) as exc:
            gcu._wait_for_leader(30, "sess", "/tmp")

        assert exc.value.code == 0
        assert emitted == [row]
        assert clock.sleeps == [0.1, 0.2, 0.4]

    def test_timeout_falls_back_to_stale(self, clock, monkeypatch):
        emitted = []
        monkeypatch.setattr(gcu, "read_usage_stale", lambda: {"session_percent": 5})
        monkeypatch.setattr(gcu, "_enrich_and_emit", lambda d, s, c: emitted.append(d))

        with pytest.raises(SystemExit) as exc:
            gcu._wait_for_leader(4, None, None)

        assert exc.value.code == 0
        assert emitted == [{"session_percent": 5, "_stale": True}]

    def test_timeout_with_no_stale_data_exits_one(self, clock):
        with pytest.raises(SystemExit) as exc:
            gcu._wait_for_leader(4, None, None)
        assert exc.value.code == 1


class TestApiQuotaFields:
    """write_usage_cache no longer nulls absent keys, so an omitted quota has to
    be written as an explicit null — and that list has to stay complete.
    """

    FULL_BODY = {
        "five_hour": {"utilization": 23, "resets_at": "2026-08-07T18:00:00"},
        "seven_day": {"utilization": 41, "resets_at": "2026-08-12T09:00:00"},
        "seven_day_sonnet": {"utilization": 12, "resets_at": "2026-08-12T09:00:00"},
        "limits": [
            {
                "kind": "weekly_scoped",
                "percent": 9,
                "resets_at": "2026-08-12T09:00:00",
                "scope": {"model": {"display_name": "Fable"}},
            }
        ],
        "extra_usage": {
            "utilization": 3,
            "used_credits": 350,
            "monthly_limit": 20000,
        },
    }

    def test_every_field_the_mapper_can_emit_is_listed(self, monkeypatch):
        monkeypatch.setattr(gcu, "request_usage_body", lambda _t: self.FULL_BODY)
        produced = set(gcu.fetch_usage_api("token"))
        assert produced - set(gcu._API_QUOTA_FIELDS) == set()
        assert produced == set(gcu._API_QUOTA_FIELDS)

    def test_the_list_names_real_columns(self):
        from ccreport import cache_db

        assert set(gcu._API_QUOTA_FIELDS) <= set(cache_db._USAGE_FIELDS)

    def test_an_omitted_quota_is_written_as_null(self):
        """A plan that loses its Sonnet cap must not keep rendering the old %."""
        from ccreport import cache_db

        cache_db.write_usage_cache({"sonnet_percent": 55, "session_percent": 10})
        partial = {"session_percent": 20, "week_percent": 30}
        cache_db.write_usage_cache({**dict.fromkeys(gcu._API_QUOTA_FIELDS), **partial})
        row = present(cache_db.read_usage_stale())
        assert "sonnet_percent" not in row
        assert row["session_percent"] == 20


class TestWindowBoundFlags:
    """--session-reset/--week-reset carry the caller's native stdin window bounds.

    The rollover case they exist for: a five_hour response without resets_at
    nulls the column, compute_costs then omits session_window_cost rather than
    zeroing it, and the row keeps the previous window's total (macsetup-x2aq).
    """

    NATIVE = "2026-08-09T19:20:00"
    CACHED = "2026-08-09T14:20:00"
    FROM_API = "2026-08-09T20:00:00"

    @pytest.fixture
    def computed(self, monkeypatch):
        """The kwargs every compute_costs call was made with."""
        seen: list[dict] = []

        def fake(**kwargs):
            seen.append(kwargs)
            return {}

        monkeypatch.setattr(gcu, "compute_costs", fake)
        return seen

    @staticmethod
    def _argv(monkeypatch, *args):
        monkeypatch.setattr(gcu.sys, "argv", ["usage_api.py", *args])

    def test_costs_only_prefers_the_flag_over_the_cached_row(
        self, computed, monkeypatch
    ):
        from ccreport import cache_db

        cache_db.write_usage_cache({"session_reset": self.CACHED})
        self._argv(monkeypatch, "--costs-only", "--session-reset", self.NATIVE)
        gcu._run_costs_only(None, None)
        assert computed[0]["session_reset_iso"] == self.NATIVE

    def test_costs_only_falls_back_to_the_cached_row(self, computed, monkeypatch):
        from ccreport import cache_db

        cache_db.write_usage_cache({"session_reset": self.CACHED})
        self._argv(monkeypatch, "--costs-only")
        gcu._run_costs_only(None, None)
        assert computed[0]["session_reset_iso"] == self.CACHED

    @pytest.fixture
    def full_fetch(self, monkeypatch):
        """main()'s leader path with everything but the response body stubbed."""
        monkeypatch.setattr(gcu, "get_usage_token", lambda: "tok")
        monkeypatch.setattr(gcu, "read_usage_cache", lambda _max_age: None)
        monkeypatch.setattr(gcu, "try_acquire_fetch_lock", lambda *a, **k: True)
        monkeypatch.setattr(gcu, "release_fetch_lock", lambda: None)
        monkeypatch.setattr(gcu, "clear_fetch_failures", lambda: None)
        monkeypatch.setattr(gcu, "write_usage_cache", lambda *a, **k: None)

        def _run(mapped):
            monkeypatch.setattr(gcu, "fetch_usage_api", lambda _t: dict(mapped))
            gcu.main()

        return _run

    def test_the_response_reset_wins_over_the_flag(
        self, computed, full_fetch, monkeypatch, capsys
    ):
        self._argv(monkeypatch, "--session-reset", self.NATIVE)
        full_fetch({"session_percent": 7, "session_reset": self.FROM_API})
        capsys.readouterr()
        assert computed[0]["session_reset_iso"] == self.FROM_API

    def test_the_flag_stands_in_when_the_response_omits_resets_at(
        self,
        computed,
        full_fetch,
        monkeypatch,
        capsys,
    ):
        self._argv(
            monkeypatch, "--session-reset", self.NATIVE, "--week-reset", self.CACHED
        )
        full_fetch({"session_percent": 7})
        capsys.readouterr()
        assert computed[0]["session_reset_iso"] == self.NATIVE
        assert computed[0]["week_reset_iso"] == self.CACHED

    def test_no_flag_and_no_response_reset_leaves_the_window_unset(
        self,
        computed,
        full_fetch,
        monkeypatch,
        capsys,
    ):
        self._argv(monkeypatch)
        full_fetch({"session_percent": 7})
        capsys.readouterr()
        assert computed[0]["session_reset_iso"] is None


def _dump_output(count: int) -> bytes:
    """A `security dump-keychain` transcript listing *count* candidate services.

    Modification dates descend with the index, so entry 0 is the newest.
    """
    items = []
    for i in range(count):
        items.append(
            'class: "genp"\n'
            "attributes:\n"
            f'    "svce"<blob>="{gcu.CREDENTIALS_SERVICE}-{i}"\n'
            f'    "mdat"<timedate>=0x3230 "{9999 - i:04d}0101120000Z\\000"\n'
        )
    return "".join(items).encode()


class TestKeychainCandidateCap:
    """A machine that has logged in many times accumulates candidate services,
    and each one costs a serial KEYCHAIN_TIMEOUT under the fetch lock.
    """

    @pytest.fixture
    def security_calls(self, monkeypatch):
        """Record every `security` invocation; every lookup misses.

        The dump lists twice the cap, so an uncapped loop is visible as extra
        find-generic-password calls rather than as a slow test.
        """
        calls: list[list[str]] = []
        dump = _dump_output(2 * gcu.MAX_KEYCHAIN_CANDIDATES)

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "dump-keychain":
                return subprocess.CompletedProcess(argv, 0, stdout=dump, stderr=b"")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

        monkeypatch.setattr(gcu.subprocess, "run", fake_run)
        monkeypatch.setattr(gcu.sys, "platform", "darwin")
        monkeypatch.setattr(gcu, "_read_token_from_credentials_file", lambda: None)
        return calls

    def test_the_candidate_list_is_capped(self, security_calls):
        assert len(gcu._list_keychain_candidates()) == gcu.MAX_KEYCHAIN_CANDIDATES

    def test_the_newest_candidates_are_the_ones_kept(self, security_calls):
        kept = gcu._list_keychain_candidates()
        assert kept[0] == f"{gcu.CREDENTIALS_SERVICE}-0"
        assert kept == sorted(kept, key=lambda s: int(s.rsplit("-", 1)[1]))

    def test_a_total_miss_probes_the_primary_plus_the_cap(self, security_calls):
        assert gcu.get_usage_token() is None
        lookups = [c for c in security_calls if c[1] == "find-generic-password"]
        assert len(lookups) == 1 + gcu.MAX_KEYCHAIN_CANDIDATES

    def test_a_candidate_hit_stops_the_loop(self, monkeypatch, security_calls):
        token = '{"claudeAiOauth": {"accessToken": "found"}}'
        hit = f"{gcu.CREDENTIALS_SERVICE}-1"
        real_run = gcu.subprocess.run

        def fake_run(argv, **kwargs):
            if argv[1] == "find-generic-password" and argv[3] == hit:
                real_run(argv, **kwargs)
                return subprocess.CompletedProcess(argv, 0, stdout=token, stderr="")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(gcu.subprocess, "run", fake_run)
        assert gcu.get_usage_token() == "found"
        lookups = [c for c in security_calls if c[1] == "find-generic-password"]
        assert [c[3] for c in lookups] == [
            gcu.CREDENTIALS_SERVICE,
            f"{gcu.CREDENTIALS_SERVICE}-0",
            hit,
        ]


class TestFetchLockHoldBudget:
    """The lock TTL has to outlast what main() legitimately does while holding
    it, or the next spawn calls a live fetch abandoned and starts a second one.
    """

    def test_the_budget_covers_the_worst_case_hold(self):
        keychain = gcu.KEYCHAIN_TIMEOUT * (1 + gcu.MAX_KEYCHAIN_CANDIDATES)
        keychain += gcu.KEYCHAIN_DUMP_TIMEOUT
        api = (1 + gcu.USAGE_API_RETRIES) * gcu.USAGE_API_TIMEOUT
        api += gcu.USAGE_API_RETRIES * gcu.USAGE_API_MAX_RETRY_DELAY
        assert keychain + api < gcu.FETCH_LOCK_MAX_HOLD_S

    def test_a_hostile_retry_after_stays_inside_the_budget(self, monkeypatch):
        """A 429 asking for a 15-minute wait must not stretch the hold past it."""
        import email.message

        headers = email.message.Message()
        headers["Retry-After"] = "900"
        sleeps: list[float] = []

        def refuse(*_a, **_k):
            raise HTTPError(gcu.USAGE_API_URL, 429, "Too Many Requests", headers, None)

        monkeypatch.setattr(gcu, "urlopen", refuse)
        monkeypatch.setattr(gcu.time, "sleep", sleeps.append)
        with pytest.raises(HTTPError):
            gcu.request_usage_body("tok")
        assert len(sleeps) == gcu.USAGE_API_RETRIES
        assert sum(sleeps) <= gcu.USAGE_API_RETRIES * gcu.USAGE_API_MAX_RETRY_DELAY

    def test_the_lock_ttl_cache_db_enforces_covers_that_budget(self):
        """cache_db holds its own copy of the number: importing this module from
        there would invert the layering and cost the render path a pricing
        import. So the two are kept in step here instead — a TTL below the hold
        lets the next spawn call a live fetch abandoned and start a second one
        against the endpoint that is already answering 429 (macsetup-3dl3).
        """
        from ccreport import cache_db

        ttl = getattr(cache_db, "FETCH_LOCK_STALE_TIMEOUT", None)
        assert ttl is not None, (
            "cache_db must expose FETCH_LOCK_STALE_TIMEOUT separately from the "
            "costs lock's _LOCK_STALE_TIMEOUT, and is_fetch_blocked must read it"
        )
        assert ttl >= gcu.FETCH_LOCK_MAX_HOLD_S
