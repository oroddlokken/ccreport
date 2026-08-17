"""Tests for statusline.py helpers that encode a rule, not a layout."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import cast

import pytest
from _narrow import present

from ccreport import statusline as sl


def _iso_offset(seconds: float) -> str:
    """last_updated as the usage row stores it: local-time ISO, *seconds* from now."""
    return (
        dt.datetime.fromtimestamp(time.time() + seconds, dt.UTC)
        .astimezone()
        .isoformat()
    )


@pytest.fixture(autouse=True)
def _clean_statusline_env(monkeypatch):
    """Every render helper reads the real environment, and a developer's profile
    may export several of these — CLAUDE_CODE_PACE_DAYS alone moves every pace
    figure off the default the tests assert. Tests state what they need; nothing
    is inherited from the shell running pytest.
    """
    for name in [k for k in os.environ if k.startswith("CLAUDE_STATUSLINE_")]:
        monkeypatch.delenv(name, raising=False)
    for name in ("CLAUDE_CODE_PACE_DAYS", "CF_BADGE", "CLAUDE_CACHE_DB_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    # main() puts this one into the real environ itself. Set then deleted so
    # monkeypatch has an entry to undo — otherwise any test that renders end to
    # end leaves it behind for every test file that runs after this one.
    monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DEFER", "")
    monkeypatch.delenv("CLAUDE_CACHE_SNAPSHOT_DEFER")


class TestPctStr:
    """0% is a reading; _ustr's `or` would erase it."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0, "0"),
            ("0", "0"),
            (42, "42"),
            ("42", "42"),
            (0.0, "0.0"),
            (None, ""),
            ("", ""),
        ],
    )
    def test_values(self, raw, expected):
        assert sl._pct_str({"p": raw}, "p") == expected

    def test_missing_key(self):
        assert sl._pct_str({}, "p") == ""

    def test_differs_from_ustr_on_zero(self):
        d = {"p": 0}
        assert sl._pct_str(d, "p") == "0"
        assert sl._ustr(d, "p") == ""


class TestExtraThresholdMet:
    @pytest.mark.parametrize(
        ("pct", "threshold", "expected"),
        [
            (60, "60", True),
            (59, "60", False),
            (100, "60", True),
            (0, "0", True),
            (0, "60", False),
            ("75", "60", True),
        ],
    )
    def test_comparison(self, monkeypatch, pct, threshold, expected):
        monkeypatch.setenv("CLAUDE_STATUSLINE_EXTRA_SESSION_THRESHOLD", threshold)
        assert sl._extra_threshold_met(pct) is expected

    @pytest.mark.parametrize("pct", [None, "", "bogus", object()])
    def test_no_reading_is_never_over_the_line(self, monkeypatch, pct):
        """Even with the threshold pinned to 0, unknown must not read as 0%."""
        monkeypatch.setenv("CLAUDE_STATUSLINE_EXTRA_SESSION_THRESHOLD", "0")
        assert sl._extra_threshold_met(pct) is False


class TestExtraVisibility:
    """The render, the fetch decision and the stale marker share one rule."""

    USAGE = {"session_percent": 80, "extra_spent": 3.5, "extra_limit": 200.0}

    def test_material_and_rendered_agree(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_EXTRA_SESSION_THRESHOLD", "60")
        assert sl._extra_is_material(dict(self.USAGE)) is True
        assert "E:$3.5" in sl._render_extra_usage(dict(self.USAGE), 0.0)

    @pytest.mark.parametrize(
        "usage_patch",
        [
            {"session_percent": 10},  # below the threshold
            {"extra_spent": 0.0},  # nothing spent
            {"session_percent": None},  # no reading
        ],
    )
    def test_hidden_cases_are_also_immaterial(self, monkeypatch, usage_patch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_EXTRA_SESSION_THRESHOLD", "60")
        usage = {**self.USAGE, **usage_patch}
        assert sl._extra_is_material(usage) is False
        assert sl._render_extra_usage(usage, 0.0) == ""

    def test_extra_off_hides_and_demotes(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_EXTRA", "0")
        assert sl._extra_is_material(dict(self.USAGE)) is False
        assert sl._render_extra_usage(dict(self.USAGE), 0.0) == ""


class TestWindowSize:
    @pytest.mark.parametrize(
        ("ctx_size", "expected"),
        [
            (200_000, "200k"),
            (1_000_000, "1M"),
            (1_500_000, "1.5M"),
            (2_000_000, "2M"),
        ],
    )
    def test_fractional_windows_keep_their_half(self, monkeypatch, ctx_size, expected):
        monkeypatch.setenv("CLAUDE_STATUSLINE_USABLE_CTX", "0")
        out = sl._render_session(
            "Opus 5", "", False, ctx_size // 2, ctx_size, 0, 0, 0, ""
        )
        assert out.endswith(f"/{expected}\033[0m")


class TestCtxPct:
    def test_glued_form_carries_no_label(self):
        assert (
            sl._render_ctx_pct(10_000, 200_000, label=False)
            == "\033[0;90m:\033[0;90m6%\033[0m"
        )

    def test_standalone_form_is_labelled(self):
        assert sl._render_ctx_pct(10_000, 200_000, label=True).startswith(
            "\033[0;90mctx:"
        )

    @pytest.mark.parametrize(("green", "col"), [("0", "90"), ("1", "32")])
    def test_ctx_green_recolors_only_the_low_band(self, monkeypatch, green, col):
        monkeypatch.setenv("CLAUDE_STATUSLINE_CTX_GREEN", green)
        monkeypatch.setenv("CLAUDE_STATUSLINE_USABLE_CTX", "0")
        assert sl._render_ctx_pct(20_000, 200_000, label=False).endswith(
            f"\033[0;{col}m10%\033[0m"
        )
        assert sl._render_ctx_pct(120_000, 200_000, label=False).endswith(
            "\033[0;33m60%\033[0m"
        )
        assert sl._render_ctx_pct(160_000, 200_000, label=False).endswith(
            "\033[0;31m80%\033[0m"
        )


class TestCfBadge:
    """The wrapper's own name is the label, so cf and co sessions read apart."""

    @pytest.mark.parametrize(
        ("value", "label"),
        [
            ("CF", "CF"),
            ("CO", "CO"),
            # cf exported a bare 1 before the value carried a label; sessions
            # launched under that wrapper keep it until they are restarted.
            ("1", "CF"),
        ],
    )
    def test_value_is_the_label(self, monkeypatch, value, label):
        monkeypatch.setenv("CF_BADGE", value)
        out = sl._render_session("Opus 5", "", False, 0, 200_000, 0, 0, 0, "")
        assert f"\033[1;97;46m {label} \033[0m" in out

    def test_badge_stays_stashable_by_force_red(self, monkeypatch):
        monkeypatch.setenv("CF_BADGE", "CO")
        out = sl._render_session("Opus 5", "", False, 0, 200_000, 0, 0, 0, "")
        assert " CO " in present(sl._BADGE_RE.search(out)).group(0)

    @pytest.mark.parametrize("value", ["", None])
    def test_no_value_renders_no_badge(self, monkeypatch, value):
        if value is not None:
            monkeypatch.setenv("CF_BADGE", value)
        out = sl._render_session("Opus 5", "", False, 0, 200_000, 0, 0, 0, "")
        assert "1;97;46m" not in out


class TestKill:
    def test_none_is_a_no_op(self):
        assert sl._kill(None) is None

    def test_kills_a_live_process(self):
        proc = subprocess.Popen(["sleep", "30"])
        sl._kill(proc)
        assert proc.wait(timeout=5) != 0

    def test_already_dead_does_not_raise(self):
        proc = subprocess.Popen(["true"])
        proc.wait(timeout=5)
        sl._kill(proc)
        sl._kill(proc)


class TestFetchDcat:
    """Counts are read from dogcat's append log, not from the dogcat library.

    Verified against `dcat list --expand`.
    """

    @pytest.fixture(autouse=True)
    def _toggle_on(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_DOGCAT", "1")

    def _repo(self, tmp_path, *records):
        import json

        dogcats = tmp_path / ".dogcats"
        dogcats.mkdir()
        (dogcats / "issues.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )
        return str(tmp_path)

    def _issue(self, id_, status, **extra):
        return {"record_type": "issue", "id": id_, "status": status, **extra}

    def test_counts_by_status(self, tmp_path):
        cwd = self._repo(
            tmp_path,
            self._issue("a", "open"),
            self._issue("b", "open"),
            self._issue("c", "in_review"),
        )
        assert sl._fetch_dcat(cwd) == {"by_status": {"open": 2, "in_review": 1}}

    def test_later_record_supersedes(self, tmp_path):
        cwd = self._repo(tmp_path, self._issue("a", "open"), self._issue("a", "closed"))
        assert sl._fetch_dcat(cwd) == {"by_status": {"closed": 1}}

    def test_tombstoned_issue_is_dropped(self, tmp_path):
        cwd = self._repo(
            tmp_path,
            self._issue("a", "open"),
            self._issue("b", "open", deleted_at="2026-08-03T12:00:00"),
        )
        assert sl._fetch_dcat(cwd) == {"by_status": {"open": 1}}

    def test_non_issue_records_ignored(self, tmp_path):
        cwd = self._repo(
            tmp_path,
            self._issue("a", "open"),
            {"record_type": "event", "id": "zzz", "status": "open"},
            {"record_type": "dependency", "id": "yyy"},
        )
        assert sl._fetch_dcat(cwd) == {"by_status": {"open": 1}}

    def test_toggle_off_skips_the_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_DOGCAT", "0")
        assert sl._fetch_dcat(self._repo(tmp_path, self._issue("a", "open"))) == {}

    @pytest.mark.parametrize(
        "cwd_of",
        [
            pytest.param(str, id="no-dogcats-dir"),
            pytest.param(lambda p: "", id="empty-cwd"),
        ],
    )
    def test_missing_storage_is_not_an_error(self, tmp_path, cwd_of):
        assert sl._fetch_dcat(cwd_of(tmp_path)) == {}

    def test_corrupt_log_degrades_to_empty(self, tmp_path):
        """A format change must hide the badge, never break the statusline."""
        dogcats = tmp_path / ".dogcats"
        dogcats.mkdir()
        (dogcats / "issues.jsonl").write_text("{not json\n", encoding="utf-8")
        assert sl._fetch_dcat(str(tmp_path)) == {}


class TestScopedCountdown:
    """The scoped quota resets with the weekly one, so only W carries the clock.

    Both segments run the same pace helper, so a shared reset used to print the
    countdown twice on one line.
    """

    @pytest.fixture
    def render(self, monkeypatch):
        import datetime as dt
        import re
        import time

        monkeypatch.setenv("CLAUDE_STATUSLINE_SCOPED_THRESHOLD", "0")
        now = time.time()

        def _render(scoped_offset_s, week_offset_s=353000):
            def iso(s):
                return dt.datetime.fromtimestamp(now + s, dt.UTC).isoformat()

            usage = {
                "week_percent": "62",
                "week_reset": iso(week_offset_s),
                "scoped_percent": "9",
                "scoped_model": "Fable",
                "scoped_reset": iso(scoped_offset_s),
                "_current_model": "Fable 5",
                "_native_rl": True,
            }
            inners, _, sc_shown = sl._render_rate_limits(usage, now)
            assert sc_shown
            return re.sub(r"\x1b\[[0-9;]*m", "", inners[-1])

        return _render

    def test_shared_reset_drops_the_parenthetical(self, render):
        assert render(353000) == "Fa:9% 2d21h/7d -33%"

    def test_sub_minute_drift_still_reads_as_shared(self, render):
        """W comes from stdin and the scoped reset from a fetch; they can differ."""
        assert render(353047) == "Fa:9% 2d21h/7d -33%"

    def test_own_reset_keeps_its_countdown(self, render):
        assert render(600000) == "Fa:9% 0d1h/7d(6d22h) +8%"

    def test_pace_off_leaves_the_scoped_segment_bare(self, render, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_WEEKLY_PACE", "0")
        assert render(353000) == "Fa:9%"
        assert render(600000) == "Fa:9% 6d22h"


class TestPaceWindowLabel:
    """The elapsed half counts the seven days the window runs, the delta paces
    against CLAUDE_CODE_PACE_DAYS. Once they differ the label has to say so, or
    the two halves of one segment describe different windows silently.
    """

    # Whole seconds: _parse_iso_epoch truncates, and a fractional now would put
    # elapsed a hair under three days and render "2d23h" on some runs.
    NOW = 1785000000.0

    @pytest.fixture
    def render(self):
        import datetime as dt
        import re

        def _render(pct="62"):
            reset = dt.datetime.fromtimestamp(self.NOW + 4 * 86400, dt.UTC).isoformat()
            return re.sub(r"\x1b\[[0-9;]*m", "", sl._weekly_pace(pct, reset, self.NOW))

        return _render

    def test_default_pace_leaves_the_window_bare(self, render):
        assert render() == "3d/7d(4d0h) +19%"

    def test_a_shorter_pace_is_named_beside_the_window(self, render, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_PACE_DAYS", "5")
        assert render() == "3d/7d@5d(4d0h) +2%"

    def test_the_named_pace_matches_what_ccu_prints(self, render, monkeypatch):
        """ccu.pace_line names the same knob; the two must not disagree."""
        from ccreport import ccu

        monkeypatch.setenv("CLAUDE_CODE_PACE_DAYS", "5")
        import datetime as dt
        import re

        reset = dt.datetime.fromtimestamp(self.NOW + 4 * 86400, dt.UTC).isoformat()
        line = re.sub(r"\x1b\[[0-9;]*m", "", ccu.pace_line(62, reset, self.NOW))
        assert "(pace: 5d)" in line
        assert "@5d" in render()


class TestScopedWeekCost:
    """The scoped quota spends the week window, so it carries a week cost too.

    It is week_cost narrowed to the family the quota caps, keyed by the same
    helper the accumulation uses so a display name and a model ID agree.
    """

    @pytest.fixture
    def render(self, monkeypatch):
        import datetime as dt
        import re
        import time

        monkeypatch.setenv("CLAUDE_STATUSLINE_SCOPED_THRESHOLD", "0")
        now = time.time()

        def _render(week_model_costs, scoped_model="Fable"):
            reset = dt.datetime.fromtimestamp(now + 353000, dt.UTC).isoformat()
            usage = {
                "week_percent": "84",
                "week_reset": reset,
                "week_cost": 1694.0,
                "scoped_percent": "31",
                "scoped_model": scoped_model,
                "scoped_reset": reset,
                "_current_model": f"{scoped_model} 5",
                "_native_rl": True,
            }
            if week_model_costs is not None:
                usage["week_model_costs"] = week_model_costs
            inners, _, sc_shown = sl._render_rate_limits(usage, now)
            assert sc_shown
            return re.sub(r"\x1b\[[0-9;]*m", "", inners[-1])

        return _render

    def test_the_familys_share_renders_beside_the_percentage(self, render):
        assert render({"fable": 123.4, "opus": 9.0}) == "Fa:31% $124 2d21h/7d -11%"

    def test_a_family_the_split_does_not_name_renders_bare(self, render):
        assert render({"opus": 9.0}) == "Fa:31% 2d21h/7d -11%"

    def test_no_split_at_all_renders_as_before(self, render):
        """A cost summary written before the split existed, or with none of it."""
        assert render(None) == "Fa:31% 2d21h/7d -11%"

    def test_the_lookup_follows_whichever_model_is_capped(self, render):
        assert render({"opus": 40.0}, scoped_model="Opus") == "Op:31% $40 2d21h/7d -11%"


class TestScopedModeCurrent:
    """SCOPED_MODE=current asks whether the session spends on the capped model.

    The selected model answers only half of it: a Task subagent runs the model
    its definition names, and stdin never says so.
    """

    @pytest.fixture
    def shown(self, monkeypatch):
        import datetime as dt
        import time

        monkeypatch.setenv("CLAUDE_STATUSLINE_SCOPED_THRESHOLD", "0")
        now = time.time()

        def _shown(current_model, families, scoped_model="Fable"):
            reset = dt.datetime.fromtimestamp(now + 353000, dt.UTC).isoformat()
            usage = {
                "week_percent": "84",
                "week_reset": reset,
                "scoped_percent": "31",
                "scoped_model": scoped_model,
                "scoped_reset": reset,
                "_current_model": current_model,
                "_session_families": families,
                "_native_rl": True,
            }
            return sl._render_rate_limits(usage, now)[2]

        return _shown

    def test_the_selected_model_still_shows_it(self, shown):
        assert shown("Fable 5", [])

    def test_a_subagents_family_shows_it_under_another_model(self, shown):
        assert shown("Opus 5 (1M context)", ["fable", "opus"])

    def test_a_family_the_session_never_ran_stays_hidden(self, shown):
        assert not shown("Opus 5 (1M context)", ["opus"])

    def test_a_session_with_no_families_read_yet_stays_hidden(self, shown):
        assert not shown("Opus 5 (1M context)", [])

    def test_an_unknown_capped_model_ignores_the_families(self, shown):
        """Its family is OTHER_FAMILY, which every log's synthetic records match."""
        assert not shown("Opus 5", ["other", "opus"], scoped_model="Glasswing")
        assert shown("Glasswing 1", ["opus"], scoped_model="Glasswing")

    def test_always_and_off_ignore_both(self, shown, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_SCOPED_MODE", "always")
        assert shown("Opus 5", [])
        monkeypatch.setenv("CLAUDE_STATUSLINE_SCOPED_MODE", "off")
        assert not shown("Fable 5", ["fable"])


class TestMergeCostData:
    """The cold-start recompute must get the window bounds from stdin.

    Without them compute_costs has no session window and omits its total, so
    the S segment renders bare on the first call of a session.
    """

    @pytest.fixture
    def calls(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_HISTORIC_COST", "1")
        seen: list[dict] = []

        def fake_compute(**kwargs):
            seen.append(kwargs)
            return {"session_window_cost": 3.5}

        monkeypatch.setattr(sl, "compute_costs", fake_compute)
        return seen

    def test_native_resets_passed_through(self, calls):
        usage: dict = {}
        native = {
            "session_reset": "2026-08-03T18:00:00",
            "week_reset": "2026-08-07T09:00:00",
        }
        sl._merge_cost_data(usage, "sid", "/tmp/proj", native)
        assert calls[0]["session_reset_iso"] == native["session_reset"]
        assert calls[0]["week_reset_iso"] == native["week_reset"]
        assert usage["session_window_cost"] == 3.5

    def test_no_native_rl_still_computes(self, calls):
        sl._merge_cost_data({}, "sid", "/tmp/proj", None)
        assert calls[0]["session_reset_iso"] is None

    def test_skipped_when_usage_present(self, calls):
        sl._merge_cost_data({"session_percent": 10}, "sid", "", None)
        assert calls == []

    def test_summary_values_win_over_the_usage_row(self, calls):
        usage = {"session_percent": 10, "week_cost": 1.0}
        sl._merge_cost_data(usage, "sid", "", None, {"week_cost": 9.0})
        assert usage["week_cost"] == 9.0

    def test_the_per_model_week_split_is_carried_over(self, calls):
        """The usage table has no column for it, so the summary is its only route."""
        usage = {"session_percent": 10}
        split = {"fable": 12.0}
        sl._merge_cost_data(usage, "sid", "", None, {"week_model_costs": split})
        assert usage["week_model_costs"] == split


class TestTheProjectSplitComesFromStorage:
    """Deriving the *_project_cost keys walks the project's JSONL logs.

    No render pays for that walk: a summary too old for the merge above still
    supplies the split, and the detached --costs-only refresh writes the next
    one. _merge_cost_data carries the figures.
    """

    PROJ_KEY = "twenty_four_hour_project_cost"

    @pytest.fixture(autouse=True)
    def historic_cost_on(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_HISTORIC_COST", "1")

    def test_the_key_is_in_the_summary_merge_list(self):
        assert self.PROJ_KEY in sl.rolling_cost_keys()

    def test_a_fresh_summary_carrying_them_ignores_the_stored_set(self):
        usage = {"session_percent": 10}
        sl._merge_cost_data(
            usage, "sid", "/tmp/proj", None, {self.PROJ_KEY: 4.0}, {self.PROJ_KEY: 99.0},
        )
        assert usage[self.PROJ_KEY] == 4.0

    def test_an_expired_summary_supplies_the_split(self):
        usage = {"session_percent": 10}
        sl._merge_cost_data(usage, "sid", "/tmp/proj", None, None, {self.PROJ_KEY: 4.0})
        assert usage[self.PROJ_KEY] == 4.0

    def test_a_fresh_summary_without_them_supplies_nothing(self):
        """It writes no refresh spawn either, so the split waits out its TTL."""
        usage = {"session_percent": 10}
        sl._merge_cost_data(usage, "sid", "/tmp/proj", None, {"week_cost": 4.0})
        assert not [k for k in usage if "project_cost" in k]

    def test_only_the_project_keys_come_from_the_stored_set(self):
        usage = {"session_percent": 10, "week_cost": 1.0}
        sl._merge_cost_data(
            usage, "sid", "/tmp/proj", None, None,
            {self.PROJ_KEY: 4.0, "week_cost": 99.0},
        )
        assert usage["week_cost"] == 1.0

    def test_nothing_stored_leaves_the_split_out(self):
        usage = {"session_percent": 10}
        sl._merge_cost_data(usage, "sid", "/tmp/proj", None, None, None)
        assert not [k for k in usage if "project_cost" in k]


class TestGitDiffstatIsGatedOnItsToggle:
    """`git diff --shortstat HEAD` refreshes the index and diffs every tracked
    path — the costliest of the four spawns, and _render_git drops its result
    unless GIT_DIFFSTAT is on.
    """

    @pytest.fixture
    def spawned(self, monkeypatch):
        seen: list[list[str]] = []

        def fake_popen(cmd, **kw):
            seen.append(cmd)
            return object()

        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT", "1")
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        return seen

    def test_off_spawns_no_diffstat(self, spawned, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT_DIFFSTAT", "0")
        procs = sl._start_git("/tmp/proj")
        assert "diffstat" not in procs
        assert not any("--shortstat" in cmd for cmd in spawned)

    def test_on_spawns_it(self, spawned, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT_DIFFSTAT", "1")
        assert "diffstat" in sl._start_git("/tmp/proj")
        assert any("--shortstat" in cmd for cmd in spawned)

    def test_collect_reads_zeros_when_it_was_never_spawned(self, monkeypatch):
        """_collect_git must not KeyError on the absent entry."""
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT_DIFFSTAT", "0")
        procs = {
            name: _FakeProc(out)
            for name, out in (
                ("status", b"## main\n M a.py\n"),
                ("stash", b""),
                ("toplevel", b"/tmp/proj\n"),
            )
        }
        git = sl._collect_git(cast("dict[str, subprocess.Popen[bytes]]", procs))
        assert (git.branch, git.insertions, git.deletions) == ("main", 0, 0)


class TestRenderGitIndicators:
    """Six any() passes over the porcelain list became one.

    The flags are what changed, not the alphabet, so these pin the mapping from
    status code to indicator and the order they are concatenated in.
    """

    @pytest.fixture(autouse=True)
    def _git_on(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT", "1")
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT_DIFFSTAT", "0")

    def _ind(self, *files, stash="", branch_line="## main"):
        out = sl._render_git("\n".join((branch_line, *files)), stash, "main", 0, 0)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        return plain.removeprefix("main").strip("[]")

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # A conflict code is a staged code too, so "=" rarely travels alone.
            ("UU a.py", "="),
            ("AA a.py", "=+"),
            ("DD a.py", "=+✘!"),
            ("AU a.py", "=+"),
            ("M  a.py", "+"),
            ("A  a.py", "+"),
            ("C  a.py", "+"),
            ("R  a.py -> b.py", "+»"),
            ("D  a.py", "+✘"),
            (" M a.py", "!"),
            (" D a.py", "!"),
            ("?? a.py", "?"),
        ],
    )
    def test_one_file_at_a_time(self, line, expected):
        assert self._ind(line) == expected

    def test_the_order_is_conflict_stash_staged_renamed_deleted_unstaged_untracked(
        self,
    ):
        assert (
            self._ind(
                "UU c.py",
                "R  a.py -> b.py",
                "D  d.py",
                " M e.py",
                "?? f.py",
                stash="x",
            )
            == "=$+»✘!?"
        )

    def test_a_clean_tree_has_no_brackets(self):
        assert sl._render_git("## main", "", "main", 0, 0).endswith("main\x1b[0m")

    def test_a_blank_line_is_not_a_status(self):
        """The porcelain output ends with a newline, so files carries an empty
        entry; "" is a substring of every alphabet these flags test against.
        """
        assert self._ind("") == ""

    def test_the_diffstat_only_shows_when_its_toggle_is_on(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT_DIFFSTAT", "1")
        plain = re.sub(
            r"\x1b\[[0-9;]*m", "", sl._render_git("## main", "", "main", 3, 1)
        )
        assert plain == "main[+3-1]"


class _FakeProc:
    """Stands in for a Popen whose output is already known."""

    def __init__(self, out: bytes):
        self._out = out

    def communicate(self, timeout=None):
        return self._out, b""

    def kill(self):
        pass


class TestDspVerdictIsMemoized:
    """The ancestor claude's argv is fixed at launch, so the ps walk is a
    once-per-session question, not a once-per-slow-render one.
    """

    @pytest.fixture(autouse=True)
    def _dsp_on(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_STATUSLINE_DSP", "1")
        monkeypatch.setenv("TMPDIR", str(tmp_path))

    def _ps(self, *rows: str) -> _FakeProc:
        return _FakeProc(("\n".join(rows) + "\n").encode())

    def test_a_memoized_verdict_spawns_nothing(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **k: pytest.fail("spawned ps")
        )
        assert sl._start_dsp_check(True) is None
        assert sl._start_dsp_check(False) is None

    def test_no_memo_spawns(self, monkeypatch):
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: "proc")
        assert sl._start_dsp_check(None) == "proc"

    def test_the_flag_is_found_up_the_chain(self, monkeypatch):
        monkeypatch.setattr(sl.os, "getppid", lambda: 200)
        proc = self._ps(
            f"100 1 /bin/claude {sl.DSP_FLAG}",
            "200 100 /bin/bash statusline.sh",
        )
        assert sl._collect_dsp(cast("subprocess.Popen[bytes] | None", proc)) is True

    def test_an_unflagged_chain_is_a_real_false(self, monkeypatch):
        monkeypatch.setattr(sl.os, "getppid", lambda: 200)
        proc = self._ps("100 1 /bin/claude", "200 100 /bin/bash")
        assert sl._collect_dsp(cast("subprocess.Popen[bytes] | None", proc)) is False

    @pytest.mark.parametrize("proc", [None, _FakeProc(b"")])
    def test_no_answer_is_none_not_false(self, proc):
        """None is what stops _fetch_all memoizing a verdict it never reached."""
        assert sl._collect_dsp(cast("subprocess.Popen[bytes] | None", proc)) is None

    def test_the_memo_file_outlives_the_fetch_cache(self):
        sid = "dsp-session"
        sl._save_memo(sid, {"dsp": True})
        assert sl._load_memo(sid) == {"dsp": True}
        assert sl._memo_path(sid) != sl._fast_cache_path(sid)

    def test_an_unreadable_memo_is_just_an_empty_one(self):
        sid = "torn-session"
        sl._memo_path(sid).write_text("{not json", encoding="utf-8")
        assert sl._load_memo(sid) == {}

    @pytest.mark.parametrize(
        "sid",
        [
            "9039cf27-6302-4b0c-8be4-33083d6ee07e",
            "../../etc/passwd",  # the substitution is what keeps rm -f in TMPDIR
            "a" * 80,  # past the 64-char cap
            "weird id!#$",
        ],
    )
    def test_the_session_start_hook_deletes_the_file_this_module_writes(self, sid):
        """The hook rebuilds the memo path in bash rather than importing it.
        Drift deletes a name nobody wrote, silently, and the resumed session
        keeps its stale verdict.
        """
        if not shutil.which("jq"):
            pytest.skip("hook parses its stdin with jq")
        hook = Path(__file__).resolve().parent.parent / "bin" / "clear-statusline-memo.sh"
        path = sl._memo_path(sid)
        path.write_text('{"dsp": false}', encoding="utf-8")
        run = subprocess.run(
            ["bash", str(hook)],
            input=json.dumps({"session_id": sid, "hook_event_name": "SessionStart"}),
            capture_output=True,
            text=True,
            check=True,
        )
        assert not path.exists()
        assert run.stdout == ""  # SessionStart stdout is appended to the session's context

    def test_turning_the_toggle_off_beats_a_memoized_true(self, monkeypatch, tmp_path):
        """_start_dsp_check stops seeing the toggle once a verdict is memoized,
        so _fetch_all is what has to re-read it.
        """
        sid = "toggle-session"
        sl._save_memo(sid, {"dsp": True})
        monkeypatch.setenv("CLAUDE_STATUSLINE_DSP", "0")
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT", "0")
        monkeypatch.setenv("CLAUDE_STATUSLINE_HISTORIC_COST", "0")
        monkeypatch.setattr(sl, "_fetch_usage", lambda *a: {})
        monkeypatch.setattr(sl, "_fetch_dcat", lambda cwd: {})
        monkeypatch.setattr(sl, "_capture_account", lambda memo=None: None)
        monkeypatch.setattr(sl, "_accumulate_cache_stats", lambda *a: (0, 0, 0))
        monkeypatch.setattr(sl, "compute_session_usage", lambda *a: (0.0, frozenset()))
        inp = sl._InputData(
            cwd=str(tmp_path),
            model="Opus",
            effort="",
            thinking_off=False,
            used="10",
            ctx_size=200_000,
            lines_added=0,
            lines_removed=0,
            cache_create=0,
            cache_read=0,
            input_fresh=0,
            total_in=0,
            session_id=sid,
        )
        fetched = sl._fetch_all(inp, {}, {}, 1_000_000.0, test_mode=True)
        assert fetched.dsp is False
        assert sl._load_memo(sid) == {"dsp": True}, "the verdict itself is unchanged"


class TestSpawnUsageRefreshWindowBounds:
    """The refresh must be told the window it is totalling, from stdin first.

    Right after a rollover the API can answer without resets_at, which writes
    session_reset as an explicit null; compute_costs then omits
    session_window_cost and the previous window's total survives every
    subsequent refresh.
    """

    NATIVE = {
        "session_reset": "2026-08-09T19:20:00",
        "week_reset": "2026-08-12T09:00:00",
    }
    CACHED = {
        "session_reset": "2026-08-09T14:20:00",
        "week_reset": "2026-08-11T09:00:00",
    }

    @pytest.fixture
    def spawned(self, monkeypatch):
        seen: list[list[str]] = []
        # The render imports subprocess inside the function, so there is no
        # sl.subprocess to patch — the module object itself is the seam.
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.append(cmd))
        return seen

    @staticmethod
    def _flag(cmd, flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None

    @pytest.mark.parametrize("costs_only", [True, False])
    def test_native_resets_win_over_the_cached_row(self, spawned, costs_only):
        sl._spawn_usage_refresh(
            "sid",
            "/tmp/proj",
            dict(self.CACHED),
            costs_only=costs_only,
            native_rl=dict(self.NATIVE),
        )
        assert self._flag(spawned[0], "--session-reset") == self.NATIVE["session_reset"]
        assert self._flag(spawned[0], "--week-reset") == self.NATIVE["week_reset"]

    @pytest.mark.parametrize("native", [None, {}, {"session_percent": 7}])
    def test_the_cached_row_is_the_fallback(self, spawned, native):
        """Including a native reading that carries a percent but no resets_at."""
        sl._spawn_usage_refresh(
            "sid",
            "/tmp/proj",
            dict(self.CACHED),
            costs_only=True,
            native_rl=native,
        )
        assert self._flag(spawned[0], "--session-reset") == self.CACHED["session_reset"]
        assert self._flag(spawned[0], "--week-reset") == self.CACHED["week_reset"]

    def test_a_null_cached_reset_does_not_suppress_the_native_one(self, spawned):
        """The rollover shape: the row's column was nulled by the last response."""
        sl._spawn_usage_refresh(
            "sid",
            "/tmp/proj",
            {"session_reset": None, "week_reset": None},
            costs_only=True,
            native_rl=dict(self.NATIVE),
        )
        assert self._flag(spawned[0], "--session-reset") == self.NATIVE["session_reset"]

    def test_neither_source_has_a_bound(self, spawned):
        sl._spawn_usage_refresh("sid", "/tmp/proj", {}, costs_only=True, native_rl={})
        assert "--session-reset" not in spawned[0]
        assert "--week-reset" not in spawned[0]

    def test_fetch_usage_threads_the_native_limits_down(self, monkeypatch):
        """_spawn_usage_refresh only sees native_rl if its caller passes it."""
        from ccreport import cache_db

        seen: list[dict] = []
        monkeypatch.setattr(
            sl,
            "_spawn_usage_refresh",
            lambda *a, **k: seen.append(k),
        )
        cache_db.write_usage_cache(
            {
                "session_percent": 5,
                # Older than USAGE_FETCH_INTERVAL_S, younger than USAGE_HEARTBEAT_S:
                # a costs-only spawn, which is the one that re-persists the total.
                "last_updated": _iso_offset(-1200),
            }
        )
        native = {"session_percent": 7, **self.NATIVE}
        sl._fetch_usage("sid", "/tmp/proj", native, None)
        assert len(seen) == 1
        assert seen[0]["costs_only"] is True
        assert seen[0]["native_rl"] == native


class TestRefreshEnv:
    """The detached refresh inherits neither the render's timeout nor its deferral.

    It is the writer every render waits on, and — since it is off the render
    path entirely — the process the day's DB snapshot is handed to.
    """

    def test_our_own_short_timeout_is_dropped(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", sl.RENDER_DB_TIMEOUT_S)
        assert "CLAUDE_CACHE_DB_TIMEOUT" not in sl._refresh_env()

    def test_a_deliberate_setting_from_the_shell_is_kept(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", "45")
        assert sl._refresh_env()["CLAUDE_CACHE_DB_TIMEOUT"] == "45"

    def test_the_snapshot_deferral_is_dropped(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DEFER", "1")
        assert "CLAUDE_CACHE_SNAPSHOT_DEFER" not in sl._refresh_env()

    def test_disabling_snapshots_outright_is_passed_through(self, monkeypatch):
        # Deferring says "not in this process"; disabling says "not at all".
        monkeypatch.setenv("CLAUDE_CACHE_SNAPSHOT_DISABLE", "1")
        assert sl._refresh_env()["CLAUDE_CACHE_SNAPSHOT_DISABLE"] == "1"

    def test_the_rest_of_the_environment_is_passed_through(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_EXTRA", "0")
        assert sl._refresh_env()["CLAUDE_STATUSLINE_EXTRA"] == "0"


class TestRenderDefersTheDailySnapshot:
    """The deferral is only worth anything if it beats the first DB touch.

    get_connection reads it once, when it opens the singleton connection, so
    setting it later in the render would be setting it after the copy.
    """

    def test_it_is_set_before_the_first_connection(self, monkeypatch, capsys):
        import json

        from ccreport import cache_db

        seen: list[str | None] = []
        real = cache_db.get_connection

        def spy():
            seen.append(os.environ.get("CLAUDE_CACHE_SNAPSHOT_DEFER"))
            return real()

        monkeypatch.setattr(cache_db, "get_connection", spy)
        monkeypatch.setattr(sl.sys, "argv", ["statusline.py", "-t"])
        monkeypatch.setenv(
            "CLAUDE_STATUSLINE_USAGE_JSON",
            json.dumps(
                {"session_percent": 23, "week_percent": 41},
            ),
        )
        for off in ("GIT", "DSP", "HISTORIC_COST", "SANDBOX", "SESSIONS"):
            monkeypatch.setenv(f"CLAUDE_STATUSLINE_{off}", "0")

        sl.main()
        capsys.readouterr()

        assert seen, "the render never opened the DB; the spy proves nothing"
        assert all(v == "1" for v in seen)


class TestFetchUsageReadsTheRowOnce:
    """Three reads of the singleton row per render is three too many."""

    @pytest.fixture
    def traced(self, monkeypatch):
        from ccreport import cache_db

        monkeypatch.setattr(sl, "_spawn_usage_refresh", lambda *a, **k: None)
        conn = cache_db.get_connection()

        def _run(row, **kwargs):
            cache_db.write_usage_cache(row)
            seen: list[str] = []
            conn.set_trace_callback(seen.append)
            try:
                sl._fetch_usage("sid", "/tmp/proj", kwargs.get("native_rl", {}), None)
            finally:
                conn.set_trace_callback(None)
            return sum("FROM usage WHERE id = 1" in s for s in seen)

        return _run

    def _iso(self, offset_s):
        return _iso_offset(offset_s)

    def test_fresh_row(self, traced):
        assert traced({"session_percent": 5, "last_updated": self._iso(0)}) == 1

    def test_expired_row_that_triggers_a_fetch(self, traced):
        assert traced({"session_percent": 5, "last_updated": self._iso(-4000)}) == 1

    def test_expired_row_while_a_fetch_is_blocked(self, traced):
        from ccreport import cache_db

        cache_db.record_fetch_failure()
        assert traced({"session_percent": 5, "last_updated": self._iso(-4000)}) == 1

    def test_a_blocked_fetch_still_flags_the_row_as_stale(self, monkeypatch):
        from ccreport import cache_db

        monkeypatch.setattr(sl, "_spawn_usage_refresh", lambda *a, **k: None)
        cache_db.write_usage_cache(
            {"session_percent": 5, "last_updated": self._iso(-4000)},
        )
        cache_db.record_fetch_failure()
        assert sl._fetch_usage("sid", "/tmp/proj", {}, None)["_stale"] is True


class TestAFreshRowStillRefreshesAnExpiredSummary:
    """The usage row is global; the cost summary it feeds is per project.

    A session in one project keeping the row fresh used to stop every other
    project's summary from ever being rewritten, and week_model_costs has no
    column of its own to fall back on.
    """

    @pytest.fixture
    def spawns(self, monkeypatch):
        from ccreport import cache_db

        calls: list[dict] = []
        monkeypatch.setattr(
            sl, "_spawn_usage_refresh",
            lambda *a, costs_only, **k: calls.append({"costs_only": costs_only}),
        )
        cache_db.write_usage_cache(
            {"session_percent": 5, "last_updated": _iso_offset(0)},
        )

        def _run(cost_summary):
            sl._fetch_usage("sid", "/tmp/proj", {}, cost_summary)
            return calls

        return _run

    def test_an_expired_summary_spawns_a_costs_only_refresh(self, spawns):
        assert spawns(None) == [{"costs_only": True}]

    def test_a_fresh_summary_spawns_nothing(self, spawns):
        assert spawns({"week_cost": 12.0}) == []

    def test_historic_cost_off_spawns_nothing(self, spawns, monkeypatch):
        """Nothing reads the summary with the toggle off, so nothing pays for it."""
        monkeypatch.setenv("CLAUDE_STATUSLINE_HISTORIC_COST", "0")
        assert spawns(None) == []

    def test_a_live_costs_lock_suppresses_the_spawn(self, spawns):
        from ccreport import cache_db

        assert cache_db.try_acquire_costs_lock()
        try:
            assert spawns(None) == []
        finally:
            cache_db.release_costs_lock()

    def test_the_api_backoff_does_not_suppress_it(self, spawns):
        """A failing API says nothing about whether local JSONL can be rescanned."""
        from ccreport import cache_db

        cache_db.record_fetch_failure()
        assert spawns(None) == [{"costs_only": True}]


class TestARenderServesTheStoredProjectSplit:
    """The wider summary read is a second meta round trip, and only on the miss.

    _fetch_usage gates its --costs-only spawn on the fresh read alone, so a hit
    on the wider one must not reach it: that would suppress the very write the
    render is standing in for.
    """

    PROJ_KEY = "seven_day_project_cost"

    @pytest.fixture
    def render(self, monkeypatch, tmp_path):
        from ccreport import cache_db

        monkeypatch.setenv("CLAUDE_STATUSLINE_HISTORIC_COST", "1")
        monkeypatch.setenv("CLAUDE_STATUSLINE_GIT", "0")
        ages: list[int] = []
        gated: list[dict | None] = []

        def fake_read(max_age=600, cwd=None):
            ages.append(max_age)
            if max_age >= sl.COST_SUMMARY_FALLBACK_MAX_AGE:
                return {self.PROJ_KEY: 7.0, "week_cost": 99.0}
            return None

        def fake_fetch_usage(session_id, cwd, native_rl, cost_summary):
            gated.append(cost_summary)
            # 42.0 is another project's: the singleton row carries whatever the
            # last refresh scoped it to, which _fetch_all strips.
            return {"session_percent": 5, "week_cost": 1.0, self.PROJ_KEY: 42.0}

        monkeypatch.setattr(cache_db, "read_cost_summary", fake_read)
        monkeypatch.setattr(sl, "_fetch_usage", fake_fetch_usage)
        monkeypatch.setattr(sl, "_fetch_dcat", lambda cwd: {})
        monkeypatch.setattr(sl, "_capture_account", lambda memo=None: None)
        monkeypatch.setattr(sl, "_accumulate_cache_stats", lambda *a: (0, 0, 0))
        monkeypatch.setattr(sl, "compute_session_usage", lambda *a: (0.0, frozenset()))
        inp = sl._InputData(
            cwd=str(tmp_path),
            model="Opus",
            effort="",
            thinking_off=False,
            used="10",
            ctx_size=200_000,
            lines_added=0,
            lines_removed=0,
            cache_create=0,
            cache_read=0,
            input_fresh=0,
            total_in=0,
            session_id="stored-split-session",
        )
        fetched = sl._fetch_all(inp, {}, {}, 1_000_000.0, test_mode=True)
        return fetched, ages, gated

    def test_the_miss_takes_the_wider_read(self, render):
        _, ages, _ = render
        assert ages == [sl.COST_SUMMARY_MAX_AGE, sl.COST_SUMMARY_FALLBACK_MAX_AGE]

    def test_the_split_is_the_stored_one(self, render):
        fetched, _, _ = render
        assert fetched.usage[self.PROJ_KEY] == 7.0

    def test_the_refresh_gate_sees_the_miss(self, render):
        _, _, gated = render
        assert gated == [None]

    def test_the_machine_wide_total_stays_with_the_row(self, render):
        fetched, _, _ = render
        assert fetched.usage["week_cost"] == 1.0


class TestRenderSurvivesContention:
    """A busy database costs the render a statistic, never the whole line."""

    @pytest.fixture
    def blocked_db(self, monkeypatch):
        import sqlite3

        from ccreport import cache_db

        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", "0.1")
        cache_db.get_connection()
        cache_db.close_connection()  # reopen under the short timeout
        other = sqlite3.connect(str(cache_db.DB_PATH), timeout=5)
        other.execute("BEGIN IMMEDIATE")
        yield
        other.rollback()
        other.close()

    def test_cache_stats_write_raises_when_the_db_is_held(self, blocked_db):
        """The guard in main() is only worth having if this is what it catches."""
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            sl._accumulate_cache_stats("sid", 1, 1, 1, 100)

    def test_the_line_is_still_printed(self, monkeypatch, capsys, blocked_db):
        """-t renders the mock payload end to end, cache-stats write and all."""
        import json
        import re

        monkeypatch.setattr(sl.sys, "argv", ["statusline.py", "-t"])
        # Keeps the render off the network: no refresh subprocess to spawn.
        monkeypatch.setenv(
            "CLAUDE_STATUSLINE_USAGE_JSON",
            json.dumps(
                {"session_percent": 23, "week_percent": 41},
            ),
        )
        for off in ("GIT", "DSP", "HISTORIC_COST", "SANDBOX", "SESSIONS"):
            monkeypatch.setenv(f"CLAUDE_STATUSLINE_{off}", "0")

        sl.main()

        out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        assert re.search(r"S:\d+%", out)
        assert re.search(r"W:\d+%", out)
        assert "427k/967k" in out  # the session segment survived too


class TestCaptureAccount:
    """The render is the only capture point, so it must never cost the line."""

    ACC = {
        "accountUuid": "uuid-work",
        "emailAddress": "me@work.example",
        "organizationUuid": "org-work",
        "organizationName": "Work AS",
        "seatTier": "team_tier_1",
        "userRateLimitTier": "default_claude_max_5x",
        "organizationRateLimitTier": "default_raven",
    }

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        """A stand-in ~/.claude.json the test writes; returns its path."""
        path = tmp_path / "claude.json"
        monkeypatch.setattr(sl, "CLAUDE_CONFIG_JSON", path)
        return path

    def _log(self):
        from ccreport import cache_db

        return cache_db.load_account_events()

    def test_the_account_is_recorded_from_the_config(self, config):
        import json

        config.write_text(json.dumps({"numStartups": 3, "oauthAccount": self.ACC}))
        sl._capture_account()
        (event,) = self._log()
        assert event["account_uuid"] == "uuid-work"
        assert event["email"] == "me@work.example"
        assert event["organization_name"] == "Work AS"
        # The whole blob is handed over, so the tiers ride along with it.
        assert event["seat_tier"] == "team_tier_1"
        assert event["user_rate_limit_tier"] == "default_claude_max_5x"
        assert event["organization_rate_limit_tier"] == "default_raven"

    def test_repeat_renders_do_not_grow_the_log(self, config):
        import json

        config.write_text(json.dumps({"oauthAccount": self.ACC}))
        for _ in range(5):
            sl._capture_account()
        assert len(self._log()) == 1

    def test_a_switch_between_renders_is_captured(self, config):
        import json

        config.write_text(json.dumps({"oauthAccount": self.ACC}))
        sl._capture_account()
        config.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "accountUuid": "uuid-home",
                        "emailAddress": "me@home.example",
                    }
                }
            )
        )
        sl._capture_account()
        assert [e["email"] for e in self._log()] == [
            "me@work.example",
            "me@home.example",
        ]

    @pytest.mark.parametrize(
        "body",
        [
            None,  # file absent entirely
            "",  # zero bytes
            "{not json",  # half-written
            "{}",  # no oauthAccount
            '{"oauthAccount": null}',  # present but null
            '{"oauthAccount": "nope"}',  # present but not an object
            '{"oauthAccount": {}}',  # no accountUuid to key on
        ],
    )
    def test_an_unusable_config_records_nothing_and_does_not_raise(self, config, body):
        if body is not None:
            config.write_text(body)
        sl._capture_account()
        assert self._log() == []

    def test_an_unchanged_file_is_not_reparsed(self, config):
        """An (mtime, size) that has not moved skips the parse of ~/.claude.json:
        no rewrite, no possible account switch. Rewritten here at the same size
        with the mtime pinned, which is exactly the state the gate is allowed to
        ignore.
        """
        import json

        memo: dict = {}
        config.write_text(json.dumps({"oauthAccount": self.ACC}))
        sl._capture_account(memo)
        stamp = memo["account"]

        st = config.stat()
        # Same field widths, so only the mtime could have given the switch away.
        config.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        **self.ACC,
                        "accountUuid": "uuid-home",
                        "emailAddress": "me@home.example",
                    }
                }
            )
        )
        os.utime(config, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert config.stat().st_size == st.st_size, "the rewrite has to be same-size"
        sl._capture_account(memo)

        assert [e["email"] for e in self._log()] == ["me@work.example"]
        assert memo["account"] == stamp

    def test_a_rewrite_reopens_the_gate(self, config):
        import json

        memo: dict = {}
        config.write_text(json.dumps({"oauthAccount": self.ACC}))
        sl._capture_account(memo)
        config.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "accountUuid": "uuid-home",
                        "emailAddress": "me@home.example",
                    }
                }
            )
        )
        sl._capture_account(memo)
        assert [e["email"] for e in self._log()] == [
            "me@work.example",
            "me@home.example",
        ]

    def test_a_failed_capture_does_not_earn_the_skip(self, config):
        """A torn read has to be retried next render, not skipped as 'seen'."""
        import json

        memo: dict = {}
        config.write_text("{not json")
        sl._capture_account(memo)
        assert "account" not in memo

        config.write_text(json.dumps({"oauthAccount": self.ACC}))
        sl._capture_account(memo)
        assert [e["email"] for e in self._log()] == ["me@work.example"]

    def test_a_held_database_costs_the_log_a_sample_not_the_render(
        self, config, monkeypatch
    ):
        import json
        import sqlite3

        from ccreport import cache_db

        config.write_text(json.dumps({"oauthAccount": self.ACC}))
        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", "0.1")
        cache_db.get_connection()
        cache_db.close_connection()  # reopen under the short timeout
        other = sqlite3.connect(str(cache_db.DB_PATH), timeout=5)
        other.execute("BEGIN IMMEDIATE")
        try:
            sl._capture_account()  # must not raise
        finally:
            other.rollback()
            other.close()
        assert self._log() == []


class TestRenderAccount:
    """Two independent toggles over one row of the account change log."""

    ACC = {
        "accountUuid": "uuid-work",
        "emailAddress": "me@work.example",
        "organizationName": "Work AS",
    }

    def _capture(self, **overrides):
        from ccreport.cache_db import record_account_event

        record_account_event({**self.ACC, **overrides})

    def _rendered(self, monkeypatch, *, user, org):
        monkeypatch.setenv("CLAUDE_STATUSLINE_USER", "1" if user else "0")
        monkeypatch.setenv("CLAUDE_STATUSLINE_ORG", "1" if org else "0")
        return re.sub(r"\033\[[0-9;]*m", "", sl._render_account())

    def test_both_toggles_off_render_nothing(self, monkeypatch):
        self._capture()
        assert self._rendered(monkeypatch, user=False, org=False) == ""

    def test_both_toggles_off_read_no_database(self, monkeypatch):
        """A segment nobody displays must not cost a SELECT on every render."""
        from ccreport import cache_db

        def boom():
            msg = "no read without a toggle"
            raise AssertionError(msg)

        monkeypatch.setattr(cache_db, "read_latest_account", boom)
        assert self._rendered(monkeypatch, user=False, org=False) == ""

    def test_the_user_toggle_alone_renders_the_email(self, monkeypatch):
        self._capture()
        assert self._rendered(monkeypatch, user=True, org=False) == "me@work.example"

    def test_the_org_toggle_alone_renders_the_organization_bare(self, monkeypatch):
        self._capture()
        assert self._rendered(monkeypatch, user=False, org=True) == "Work AS"

    def test_both_toggles_parenthesize_the_organization(self, monkeypatch):
        self._capture()
        assert self._rendered(monkeypatch, user=True, org=True) == "me@work.example (Work AS)"

    def test_nothing_is_rendered_before_the_first_capture(self, monkeypatch):
        assert self._rendered(monkeypatch, user=True, org=True) == ""

    def test_a_missing_field_renders_without_empty_parentheses(self, monkeypatch):
        """An event captured before the org was known must not render "x ()"."""
        self._capture(organizationName=None)
        assert self._rendered(monkeypatch, user=True, org=True) == "me@work.example"

    def test_the_newest_capture_is_the_one_named(self, monkeypatch):
        self._capture()
        self._capture(accountUuid="uuid-home", emailAddress="me@home.example")
        assert self._rendered(monkeypatch, user=True, org=False) == "me@home.example"

    def test_an_unreadable_log_costs_the_segment_not_the_render(self, monkeypatch):
        from ccreport import cache_db

        def boom():
            raise OSError

        monkeypatch.setattr(cache_db, "read_latest_account", boom)
        assert self._rendered(monkeypatch, user=True, org=True) == ""


class TestAccountLandsOnTheLastLine:
    """Where the segment prints, in both layouts and beside the battery."""

    def _lines(self, monkeypatch, capsys, cols, account, battery=""):
        monkeypatch.setenv("COLUMNS", str(cols))
        sl._layout_and_print(
            ["12:00", "ccreport"], "Opus ctx:42%", "S:23%", "W:41%", "24H:$1.20",
            {"_native_rl": True}, account, battery, "", "", 1_000_000.0, time.monotonic(),
        )
        return capsys.readouterr().out.rstrip("\n").split("\n")

    def test_it_is_the_fourth_line_when_narrow(self, monkeypatch, capsys):
        lines = self._lines(monkeypatch, capsys, 100, "me@work.example (Work AS)")
        assert len(lines) == 4
        assert "me@work.example (Work AS)" in lines[3]

    def test_it_is_the_last_line_when_wide(self, monkeypatch, capsys):
        lines = self._lines(monkeypatch, capsys, 200, "me@work.example (Work AS)")
        assert "me@work.example (Work AS)" in lines[-1]

    def test_it_precedes_the_battery_on_that_line(self, monkeypatch, capsys):
        last = self._lines(monkeypatch, capsys, 100, "me@work.example", "BAT:80%")[-1]
        assert last.index("me@work.example") < last.index("BAT:80%")

    def test_no_segment_adds_no_line(self, monkeypatch, capsys):
        assert len(self._lines(monkeypatch, capsys, 100, "")) == 3


class TestUpdateSegment:
    """What has to hold before a commits-behind count goes on screen."""

    NOW = 1_000_000.0
    SHA = "a" * 40
    OTHER = "b" * 40

    @pytest.fixture
    def checkout(self, tmp_path, monkeypatch):
        """A tree update_check reads as its own checkout, with HEAD at SHA."""
        from ccreport import update_check

        (tmp_path / "src" / "ccreport").mkdir(parents=True)
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text(f"{self.SHA}\n", encoding="utf-8")
        monkeypatch.setattr(
            update_check, "__file__", str(tmp_path / "src" / "ccreport" / "update_check.py"))
        return tmp_path

    @pytest.fixture
    def spawns(self, monkeypatch):
        seen: list[list[str]] = []
        monkeypatch.setattr(sl, "_spawn_update_check", lambda: seen.append(["spawn"]))
        return seen

    def _store(self, behind, *, age=0.0, sha=None):
        from ccreport import cache_db

        cache_db.write_update_check(self.SHA if sha is None else sha, behind, self.NOW - age)

    def test_it_names_ccreport_and_what_to_run(self, checkout, spawns):
        self._store(12)
        line = sl._render_update(self.NOW)
        assert "A newer version of ccreport is available" in line
        assert "ccreport update --pull" in line

    def test_it_does_not_send_the_user_to_git_pull(self, checkout, spawns):
        """The line renders in whatever project the session is in, not this checkout."""
        self._store(12)
        line = sl._render_update(self.NOW)
        assert "git pull" not in line.replace("ccreport update --pull", "")

    def test_the_count_gates_the_line_without_appearing_in_it(self, checkout, spawns):
        """One commit behind and twelve read alike; only the gate uses the number."""
        self._store(1)
        one = sl._render_update(self.NOW)
        self._store(12)
        assert one == sl._render_update(self.NOW) != ""

    def test_up_to_date_shows_nothing(self, checkout, spawns):
        self._store(0)
        assert sl._render_update(self.NOW) == ""

    def test_an_unanswered_check_shows_nothing(self, checkout, spawns):
        """A 404 or a rate limit stores no count; silence beats a guess."""
        self._store(None)
        assert sl._render_update(self.NOW) == ""

    def test_a_pull_since_the_check_silences_the_line(self, checkout, spawns):
        """The count describes a commit the user has already left."""
        self._store(12, sha=self.OTHER)
        assert sl._render_update(self.NOW) == ""

    def test_a_count_older_than_the_max_age_is_not_repeated(self, checkout, spawns):
        from ccreport import update_check

        self._store(12, age=update_check.UPDATE_MAX_AGE_S + 1)
        assert sl._render_update(self.NOW) == ""

    def test_the_toggle_turns_it_off(self, checkout, spawns, monkeypatch):
        self._store(12)
        monkeypatch.setenv("CLAUDE_STATUSLINE_UPDATE", "0")
        assert sl._render_update(self.NOW) == ""

    def test_an_installed_package_has_nothing_to_pull(self, tmp_path, monkeypatch, spawns):
        """No .git beside the package: `uv tool install .`, or a wheel."""
        from ccreport import update_check

        (tmp_path / "src" / "ccreport").mkdir(parents=True)
        monkeypatch.setattr(
            update_check, "__file__", str(tmp_path / "src" / "ccreport" / "update_check.py"))
        self._store(12)
        assert sl._render_update(self.NOW) == ""
        assert spawns == []

    def test_a_stale_stamp_spawns_the_check(self, checkout, spawns):
        self._store(0, age=sl.UPDATE_CHECK_INTERVAL_S)
        sl._render_update(self.NOW)
        assert len(spawns) == 1

    def test_a_fresh_stamp_spawns_nothing(self, checkout, spawns):
        self._store(0, age=sl.UPDATE_CHECK_INTERVAL_S - 1)
        sl._render_update(self.NOW)
        assert spawns == []

    def test_a_never_run_check_spawns(self, checkout, spawns):
        sl._render_update(self.NOW)
        assert len(spawns) == 1

    def test_the_toggle_is_read_before_the_database(self, checkout, spawns, monkeypatch):
        """Off means off: no read, and no check spawned to feed a line nobody sees."""
        from ccreport import cache_db

        def boom():
            raise AssertionError("no database read with the segment off")

        monkeypatch.setattr(cache_db, "read_update_check", boom)
        monkeypatch.setenv("CLAUDE_STATUSLINE_UPDATE", "0")
        assert sl._render_update(self.NOW) == ""
        assert spawns == []

    def test_a_busy_database_costs_the_line_not_the_render(
        self, checkout, spawns, monkeypatch,
    ):
        from ccreport import cache_db

        def boom():
            raise OSError

        monkeypatch.setattr(cache_db, "read_update_check", boom)
        assert sl._render_update(self.NOW) == ""

    def test_the_spawn_is_detached(self, monkeypatch):
        """It outlives the render that started it, like the usage refresh."""
        seen: list[dict] = []
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.append({**kw, "cmd": cmd}))
        sl._spawn_update_check()
        assert seen[0]["start_new_session"] is True
        assert seen[0]["cmd"][1:] == ["-m", "ccreport.update_check"]


class TestUpdateLandsUnderTheCostLine:
    """Where the segment prints, in both layouts.

    A stand-in string stands where the rendered segment would: what is under
    test is the placement, and pinning the wording here would make every
    rewording of the sentence look like a layout regression.
    """

    SEG = "UPDATE-SEGMENT"

    def _lines(self, monkeypatch, capsys, cols, update):
        monkeypatch.setenv("COLUMNS", str(cols))
        sl._layout_and_print(
            ["12:00", "ccreport"], "Opus ctx:42%", "S:23%", "W:41%", "24H:$1.20",
            {"_native_rl": True}, "me@work.example", "", "", update,
            1_000_000.0, time.monotonic(),
        )
        return capsys.readouterr().out.rstrip("\n").split("\n")

    @pytest.mark.parametrize("cols", [100, 200])
    def test_it_follows_the_cost_windows(self, monkeypatch, capsys, cols):
        lines = self._lines(monkeypatch, capsys, cols, self.SEG)
        cost = next(i for i, line in enumerate(lines) if "24H:$1.20" in line)
        assert self.SEG in lines[cost + 1]

    @pytest.mark.parametrize("cols", [100, 200])
    def test_it_precedes_the_account_line(self, monkeypatch, capsys, cols):
        lines = self._lines(monkeypatch, capsys, cols, self.SEG)
        assert "me@work.example" in lines[-1]
        assert self.SEG in lines[-2]

    @pytest.mark.parametrize("cols", [100, 200])
    def test_no_segment_adds_no_line(self, monkeypatch, capsys, cols):
        with_it = self._lines(monkeypatch, capsys, cols, self.SEG)
        without = self._lines(monkeypatch, capsys, cols, "")
        assert len(without) == len(with_it) - 1


class TestRateLimitSamples:
    """What the render offers the snapshot gate, and what it refuses to invent."""

    NOW = 1_000_000.0
    # Both on a whole minute, because _rl_sample normalizes a reset time to one
    # (cache_db.rl_window_key) and these cases are about the readings, not that.
    RESETS = 1_008_000.0
    WEEK_RESETS = 1_404_000.0

    def _stdin(self, **windows):
        return {"rate_limits": windows}

    def _iso(self, epoch):
        import datetime as dt

        return dt.datetime.fromtimestamp(epoch).isoformat()  # noqa: DTZ006

    def test_stdin_percentages_are_passed_through_unrounded(self):
        """_native_rate_limits rounds for the display; the fill rate needs the float."""
        data = self._stdin(
            five_hour={"used_percentage": 23.47, "resets_at": self.RESETS},
            seven_day={"used_percentage": 41.02, "resets_at": self.WEEK_RESETS},
        )
        assert sl._rl_samples(data, {}, self.NOW) == [
            ("session", 23.47, self.RESETS, None, "stdin"),
            ("week", 41.02, self.WEEK_RESETS, None, "stdin"),
        ]

    def test_api_resets_are_converted_from_iso(self):
        usage = {
            "sonnet_percent": 12,
            "sonnet_reset": self._iso(self.RESETS),
            "scoped_percent": 7,
            "scoped_reset": self._iso(self.RESETS),
            "scoped_model": "claude-opus-4-5",
        }
        assert sl._rl_samples({}, usage, self.NOW) == [
            ("sonnet", 12.0, self.RESETS, None, "api"),
            ("scoped", 7.0, self.RESETS, "claude-opus-4-5", "api"),
        ]

    def test_a_passed_reset_records_nothing(self):
        """The percentage on hand belongs to the window that just ended."""
        data = self._stdin(
            five_hour={"used_percentage": 80.0, "resets_at": self.NOW - 1},
        )
        usage = {"sonnet_percent": 80, "sonnet_reset": self._iso(self.NOW - 1)}
        assert sl._rl_samples(data, usage, self.NOW) == []

    @pytest.mark.parametrize(
        "window",
        [
            {},  # neither field
            {"used_percentage": 23.5},  # no reset to key the instance on
            {"resets_at": RESETS},  # no reading
            {"used_percentage": None, "resets_at": RESETS},
            {"used_percentage": 23.5, "resets_at": None},
            {"used_percentage": "n/a", "resets_at": RESETS},
            {"used_percentage": 23.5, "resets_at": "soon"},
        ],
    )
    def test_an_incomplete_stdin_window_is_skipped(self, window):
        assert sl._rl_samples(self._stdin(five_hour=window), {}, self.NOW) == []

    def test_a_missing_rate_limits_block_is_not_an_error(self):
        """Pro/Max only, and absent until the session's first API response."""
        assert sl._rl_samples({}, {}, self.NOW) == []
        assert sl._rl_samples({"rate_limits": None}, {}, self.NOW) == []

    def test_one_unusable_window_does_not_take_the_others_with_it(self):
        data = self._stdin(
            five_hour={"used_percentage": None, "resets_at": self.RESETS},
            seven_day={"used_percentage": 41.0, "resets_at": self.RESETS},
        )
        assert [s.window for s in sl._rl_samples(data, {}, self.NOW)] == ["week"]

    def test_a_scoped_reading_with_no_model_still_samples(self):
        """The plan may scope a limit the cache has no model name for."""
        usage = {"scoped_percent": 7, "scoped_reset": self._iso(self.RESETS)}
        assert sl._rl_samples({}, usage, self.NOW) == [
            ("scoped", 7.0, self.RESETS, None, "api"),
        ]


class TestRateLimitResetNormalization:
    """The API's reset time is a float that drifts; the window is not."""

    NOW = 1_000_000.0
    RESETS = 1_008_000.0

    def _sample(self, resets, now=None):
        return sl._rl_sample(
            "scoped",
            42.0,
            resets,
            None,
            "api",
            self.NOW if now is None else now,
        )

    @pytest.mark.parametrize("jitter", [-0.97, -0.03, 0.0, 0.03, 29.4, -29.4])
    def test_sub_minute_drift_lands_on_the_same_window(self, jitter):
        """One reset, one identity — 80 scoped rows shared a window and a float each."""
        assert present(self._sample(self.RESETS + jitter)).resets_at == self.RESETS

    def test_the_next_minute_is_still_a_different_window(self):
        """Normalizing must not merge two windows that genuinely differ."""
        assert present(self._sample(self.RESETS + 60)).resets_at == self.RESETS + 60

    def test_jitter_does_not_bypass_the_write_gate(self, tmp_path):
        """The bypass is what let one day of scoped history reach 80 rows."""
        from ccreport import cache_db

        for i, jitter in enumerate([0.03, -0.44, 0.91, -0.12]):
            # An hour apart and a whole percent apart would each pass the gate on
            # their own; what must not pass is the reset time looking new.
            cache_db.record_rate_limit_snapshots(
                [present(self._sample(self.RESETS + jitter))],
                now=self.NOW + i * 3600,
            )
        rows = cache_db.load_rate_limit_snapshots()
        assert len(rows) == 1
        assert rows[0]["resets_at"] == self.RESETS

    @pytest.mark.parametrize(
        "resets",
        [
            9_999_999_999.0,  # the placeholder Claude Code sends on stdin
            NOW + 8 * 86_400 + 1,  # just past the bound
            NOW + 400 * 86_400,  # a year out
        ],
    )
    def test_a_reset_too_far_out_is_not_a_reading(self, resets):
        assert self._sample(resets) is None

    def test_the_longest_real_window_still_records(self):
        """The bound has to clear a 7-day window quoted generously."""
        assert self._sample(self.NOW + 7 * 86_400) is not None


class TestSnapshotRateLimitsGating:
    """Fabricated readings must never reach a table no later render can correct."""

    NOW = 1_000_000.0

    @pytest.fixture
    def spy(self, monkeypatch):
        # Patched on cache_db, not sl: the render imports it lazily inside
        # _snapshot_rate_limits, so there is no sl-level name to intercept.
        from ccreport import cache_db

        calls: list[tuple] = []
        monkeypatch.setattr(
            cache_db,
            "record_rate_limit_snapshots",
            lambda samples, now: calls.append((samples, now)),
        )
        return calls

    def _data(self):
        return {
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": self.NOW + 8100},
            }
        }

    def test_a_real_render_records(self, spy):
        sl._snapshot_rate_limits(self._data(), {}, self.NOW, test_mode=False)
        assert [s.window for s in spy[0][0]] == ["session"]

    def test_test_mode_records_nothing(self, spy):
        sl._snapshot_rate_limits(self._data(), {}, self.NOW, test_mode=True)
        assert spy == []

    def test_pre_provided_usage_json_records_nothing(self, spy, monkeypatch):
        monkeypatch.setenv("CLAUDE_STATUSLINE_USAGE_JSON", '{"session_percent": 23}')
        sl._snapshot_rate_limits(self._data(), {}, self.NOW, test_mode=False)
        assert spy == []

    def test_nothing_to_sample_takes_no_db_call(self, spy):
        sl._snapshot_rate_limits({}, {}, self.NOW, test_mode=False)
        assert spy == []

    @pytest.fixture
    def blocked_db(self, monkeypatch):
        import sqlite3

        from ccreport import cache_db

        monkeypatch.setenv("CLAUDE_CACHE_DB_TIMEOUT", "0.1")
        cache_db.get_connection()
        cache_db.close_connection()  # reopen under the short timeout
        other = sqlite3.connect(str(cache_db.DB_PATH), timeout=5)
        other.execute("BEGIN IMMEDIATE")
        yield
        other.rollback()
        other.close()

    def test_the_write_raises_when_the_db_is_held(self, blocked_db):
        """The guard is only worth having if this is what it catches."""
        import sqlite3

        from ccreport import cache_db

        with pytest.raises(sqlite3.OperationalError):
            cache_db.record_rate_limit_snapshots(
                sl._rl_samples(self._data(), {}, self.NOW),
                self.NOW,
            )

    def test_a_held_database_costs_the_table_a_sample_not_the_render(self, blocked_db):
        from ccreport import cache_db

        sl._snapshot_rate_limits(self._data(), {}, self.NOW, test_mode=False)
        rows = (
            cache_db.get_connection()
            .execute("SELECT COUNT(*) FROM rate_limit_snapshots")
            .fetchone()[0]
        )
        assert rows == 0


class TestFastCache:
    """Renders within FAST_TTL_S reuse the previous render's fetch results.

    The file is the whole fast path: a miss for any reason just costs a slow
    render, so every check here errs toward missing rather than serving
    another directory's git segment or a stale shape.
    """

    NOW = 1_000_000.0
    CWD = "/some/project"
    SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"

    @pytest.fixture(autouse=True)
    def _tmpdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        return tmp_path

    def _fetched(self, **overrides):
        base = {
            "git": sl.GitInfo("## main", "", "/some/project", "main", 3, 1),
            "battery": {"pct": 80, "state": "charging", "time": ""},
            "dsp": True,
            "dcat": {"by_status": {"open": 2}},
            "usage": {"session_percent": 23, "week_cost": 12.5},
            "chat_cost": 1.25,
            "chat_families": ["fable", "opus"],
            "cums": (10, 20, 30),
            "total_in": 42_000,
            "sandbox": "sbx",
            "sessions": "+2sess",
            "account": "me@work.example (Work AS)",
            "update": "",
        }
        return sl._Fetched(**{**base, **overrides})

    def test_roundtrip(self):
        sl._save_fetched(self.SID, self.CWD, self.NOW, self._fetched())
        got, ts = present(sl._load_fetched(self.SID, self.CWD, self.NOW + 1))
        assert got == self._fetched()
        assert isinstance(got.git, sl.GitInfo)
        assert isinstance(got.cums, tuple)
        assert ts == self.NOW

    def test_the_slow_only_badges_survive_the_roundtrip(self):
        """All three are rendered strings, resolved on the slow path."""
        sl._save_fetched(self.SID, self.CWD, self.NOW, self._fetched())
        got, _ = present(sl._load_fetched(self.SID, self.CWD, self.NOW + 1))
        assert (got.sandbox, got.sessions, got.account) == (
            "sbx",
            "+2sess",
            "me@work.example (Work AS)",
        )

    def test_expired_file_misses(self):
        sl._save_fetched(self.SID, self.CWD, self.NOW, self._fetched())
        assert sl._load_fetched(self.SID, self.CWD, self.NOW + sl.FAST_TTL_S) is None

    def test_a_future_timestamp_misses(self):
        """Clock skew must not grant an unbounded TTL."""
        sl._save_fetched(self.SID, self.CWD, self.NOW + 60, self._fetched())
        assert sl._load_fetched(self.SID, self.CWD, self.NOW) is None

    def test_another_directory_misses(self):
        """The session can change workspace between renders."""
        sl._save_fetched(self.SID, self.CWD, self.NOW, self._fetched())
        assert sl._load_fetched(self.SID, "/elsewhere", self.NOW + 1) is None

    def test_another_session_misses(self):
        sl._save_fetched(self.SID, self.CWD, self.NOW, self._fetched())
        assert sl._load_fetched("other-session", self.CWD, self.NOW + 1) is None

    def test_a_stale_schema_misses(self, monkeypatch):
        sl._save_fetched(self.SID, self.CWD, self.NOW, self._fetched())
        monkeypatch.setattr(sl, "_FAST_CACHE_SCHEMA", sl._FAST_CACHE_SCHEMA + 1)
        assert sl._load_fetched(self.SID, self.CWD, self.NOW + 1) is None

    def test_a_torn_file_misses(self):
        sl._fast_cache_path(self.SID).write_text('{"schema":', encoding="utf-8")
        assert sl._load_fetched(self.SID, self.CWD, self.NOW + 1) is None

    def test_no_session_id_never_caches(self, tmp_path):
        sl._save_fetched("", self.CWD, self.NOW, self._fetched())
        assert list(tmp_path.iterdir()) == []
        assert sl._load_fetched("", self.CWD, self.NOW) is None

    def test_session_id_is_sanitized_into_the_filename(self):
        sid = "../../etc/passwd"
        sl._save_fetched(sid, self.CWD, self.NOW, self._fetched())
        assert present(sl._load_fetched(sid, self.CWD, self.NOW + 1))[0] == self._fetched()
        name = sl._fast_cache_path(sid).name
        assert "/" not in name.replace("claude-statusline-", "", 1)

    @pytest.mark.parametrize(
        "sid",
        [
            "9039cf27-6302-4b0c-8be4-33083d6ee07e",
            "../../etc/passwd",  # the substitution is what keeps rm -f in TMPDIR
            "a" * 80,  # past the 64-char cap
            "weird id!#$",
        ],
    )
    def test_the_config_change_hook_deletes_the_file_this_module_writes(self, sid):
        """The hook rebuilds the fetch-cache path in bash rather than importing it.
        Drift deletes a name nobody wrote, silently, and the edited setting stays
        off the line until the TTL runs out.
        """
        if not shutil.which("jq"):
            pytest.skip("hook parses its stdin with jq")
        hook = Path(__file__).resolve().parent.parent / "bin" / "clear-statusline-cache.sh"
        sl._save_fetched(sid, self.CWD, self.NOW, self._fetched())
        memo = sl._memo_path(sid)
        memo.write_text('{"dsp": true}', encoding="utf-8")
        run = subprocess.run(
            ["bash", str(hook)],
            input=json.dumps(
                {
                    "session_id": sid,
                    "hook_event_name": "ConfigChange",
                    "config_source": "user_settings",
                },
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        assert not sl._fast_cache_path(sid).exists()
        assert memo.exists()  # the DSP verdict is not a settings reading
        assert run.stdout == ""

    @pytest.mark.parametrize(
        "payload", ["", "not json", "{}", '{"session_id": ""}', '{"session_id": "gone"}'],
    )
    def test_the_config_change_hook_never_blocks_the_config_change(self, payload):
        """A ConfigChange hook that exits 2 stops the edit from taking effect."""
        if not shutil.which("jq"):
            pytest.skip("hook parses its stdin with jq")
        hook = Path(__file__).resolve().parent.parent / "bin" / "clear-statusline-cache.sh"
        run = subprocess.run(
            ["bash", str(hook)], input=payload, capture_output=True, text=True, check=False,
        )
        assert run.returncode == 0
        assert run.stdout == ""


class TestCatchUpCacheStats:
    """The one bookkeeping write the fast path keeps (see _catch_up_cache_stats)."""

    NOW = 1_000_000.0
    CWD = "/some/project"
    SID = "catchup-session"

    @pytest.fixture(autouse=True)
    def _tmpdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        return tmp_path

    def _inp(self, total_in):
        return sl._InputData(
            cwd=self.CWD,
            model="Opus",
            effort="",
            thinking_off=False,
            used="10",
            ctx_size=200_000,
            lines_added=0,
            lines_removed=0,
            cache_create=5,
            cache_read=6,
            input_fresh=7,
            total_in=total_in,
            session_id=self.SID,
        )

    def _fetched(self, total_in=42_000):
        return sl._Fetched(
            git=sl.GitInfo("", "", "", "", 0, 0),
            battery={},
            dsp=False,
            dcat={},
            usage={},
            chat_cost=0.0,
            chat_families=[],
            cums=(1, 2, 3),
            total_in=total_in,
            sandbox="",
            sessions="",
            account="",
            update="",
        )

    def test_unchanged_total_in_touches_nothing(self, monkeypatch):
        def boom(*a):
            msg = "no accumulation without a new API response"
            raise AssertionError(msg)

        monkeypatch.setattr(sl, "_accumulate_cache_stats", boom)
        fetched = self._fetched(total_in=42_000)
        assert sl._catch_up_cache_stats(self._inp(42_000), fetched, self.NOW) is fetched

    def test_a_new_total_in_accumulates_and_keeps_the_file_ts(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            sl,
            "_accumulate_cache_stats",
            lambda *a: seen.append(a) or (11, 22, 33),
        )
        sl._save_fetched(self.SID, self.CWD, self.NOW, self._fetched(total_in=42_000))
        got = sl._catch_up_cache_stats(
            self._inp(43_000),
            self._fetched(total_in=42_000),
            self.NOW,
        )
        assert seen == [(self.SID, 6, 5, 7, 43_000)]
        assert got.cums == (11, 22, 33)
        assert got.total_in == 43_000
        # The rewrite must not extend the TTL: stale git data would otherwise
        # ride along for as long as the turn keeps producing API responses.
        reloaded = sl._load_fetched(self.SID, self.CWD, self.NOW + 1)
        assert reloaded is not None
        assert reloaded[0].cums == (11, 22, 33)
        assert sl._load_fetched(self.SID, self.CWD, self.NOW + sl.FAST_TTL_S) is None

    def test_a_held_database_costs_the_stats_not_the_render(self, monkeypatch):
        import sqlite3

        def boom(*a):
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(sl, "_accumulate_cache_stats", boom)
        fetched = self._fetched(total_in=42_000)
        assert sl._catch_up_cache_stats(self._inp(43_000), fetched, self.NOW) is fetched


class TestRenderElapsed:
    """The second time figure is bash's: the render only embeds the token.

    The wrapper times the whole Python invocation and substitutes the token
    afterwards, because no in-process clock can see its own interpreter
    startup and exit.
    """

    def _plain(self, monkeypatch, token):
        import re
        import time

        if token is not None:
            monkeypatch.setenv("CLAUDE_STATUSLINE_TOTAL_TOKEN", token)
        out = sl._render_elapsed(time.monotonic())
        return re.sub(r"\x1b\[[0-9;]*m", "", out)

    @pytest.mark.parametrize("token", [None, ""])
    def test_no_token_shows_in_process_time_alone(self, monkeypatch, token):
        """Unset means nothing downstream will substitute — emitting the token
        would print it literally, so it must not appear.
        """
        import re

        assert re.fullmatch(r"\d\.\d{3}s", self._plain(monkeypatch, token))

    def test_the_token_is_embedded_verbatim(self, monkeypatch):
        """Any transformation here would break bash's exact-match substitution."""
        import re

        out = self._plain(monkeypatch, "__SL_TOTAL__")
        assert re.fullmatch(r"\d\.\d{3}s/__SL_TOTAL__", out)


class TestQuotaGuardSegment:
    """A session armed with CCQUOTA_STOP looks like an unarmed one without this.

    The thresholds are parsed here rather than read from quota_guard, so the two
    parses are pinned against each other below.
    """

    NOW = 1785000000.0
    RED = "\033[0;31m"
    YELLOW = "\033[0;33m"

    def _usage(self, **percents):
        reset = dt.datetime.fromtimestamp(self.NOW + 3600, dt.UTC).isoformat()
        usage = {
            "session_percent": "10",
            "session_reset": reset,
            "week_percent": "20",
            "week_reset": reset,
            "_native_rl": True,
        }
        usage.update(percents)
        return usage

    def _segment(self, monkeypatch, stop="95", warn=None, **percents):
        """The last inner section, ANSI intact."""
        if stop is not None:
            monkeypatch.setenv("CCQUOTA_STOP", stop)
        if warn is not None:
            monkeypatch.setenv("CCQUOTA_WARN", warn)
        inners, _, _ = sl._render_rate_limits(self._usage(**percents), self.NOW)
        return inners[-1]

    def _plain(self, segment):
        return re.sub(r"\x1b\[[0-9;]*m", "", segment)

    @pytest.mark.parametrize("stop", ["", "   "])
    def test_an_unarmed_session_renders_nothing(self, monkeypatch, stop):
        monkeypatch.setenv("CCQUOTA_STOP", stop)
        monkeypatch.setenv("CCQUOTA_WARN", "85")
        inners, _, _ = sl._render_rate_limits(self._usage(), self.NOW)
        assert not any("Q:" in self._plain(i) for i in inners)

    def test_both_lines_render_as_a_pair(self, monkeypatch):
        assert self._plain(self._segment(monkeypatch, warn="85")) == "Q:85/95"

    def test_the_stop_line_alone_renders_alone(self, monkeypatch):
        assert self._plain(self._segment(monkeypatch)) == "Q:95"

    def test_a_fractional_line_keeps_its_decimal(self, monkeypatch):
        assert self._plain(self._segment(monkeypatch, stop="92.5", warn="80")) == "Q:80/92.5"

    def test_below_the_warn_line_it_stays_subdued(self, monkeypatch):
        assert self._segment(monkeypatch, warn="85").startswith(sl.SUBDUED)

    def test_at_the_warn_line_it_turns_yellow(self, monkeypatch):
        assert self._segment(monkeypatch, warn="85", week_percent="85").startswith(self.YELLOW)

    def test_at_the_stop_line_it_turns_red(self, monkeypatch):
        assert self._segment(monkeypatch, warn="85", week_percent="96").startswith(self.RED)

    def test_a_warn_line_of_its_own_still_colours_nothing_below_it(self, monkeypatch):
        """Without CCQUOTA_WARN there is no yellow state, only subdued and red."""
        assert self._segment(monkeypatch, week_percent="90").startswith(sl.SUBDUED)

    @pytest.mark.parametrize("window", ["sonnet_percent", "scoped_percent"])
    def test_any_window_can_drive_the_colour(self, monkeypatch, window):
        """The guard blocks on the highest of the four, hidden segments included."""
        segment = self._segment(monkeypatch, warn="85", **{window: "97"})
        assert segment.startswith(self.RED)

    def test_an_unparseable_stop_line_renders_red(self, monkeypatch):
        """It blocks every prompt in the guard, so it cannot render as armed-and-fine."""
        segment = self._segment(monkeypatch, stop="ninety", warn="85")
        assert self._plain(segment) == "Q:!"
        assert segment.startswith(self.RED)

    def test_it_renders_before_any_window_has_a_reading(self, monkeypatch):
        """Claude Code sends no rate_limits until the session's first response."""
        monkeypatch.setenv("CCQUOTA_STOP", "95")
        inners, have_rl, _ = sl._render_rate_limits({}, self.NOW)
        assert [self._plain(i) for i in inners] == ["Q:95"]
        assert not have_rl

    def test_stale_readings_do_not_disarm_it(self, monkeypatch):
        """The stale clear drops S/W; the guard is armed regardless, and blocks."""
        monkeypatch.setenv("CCQUOTA_STOP", "95")
        usage = self._usage(last_updated=_iso_offset(-sl.STALE_THRESHOLD_S - 60))
        usage["_native_rl"] = False
        inners, have_rl, _ = sl._render_rate_limits(usage, time.time())
        assert [self._plain(i) for i in inners] == ["Q:95"]
        assert not have_rl

    @pytest.mark.parametrize(
        "raw",
        [None, "", "   ", "95", " 95 ", "95%", " 92.5 % ", "0", "ninety", "9 5", "%"],
    )
    def test_the_inline_parse_agrees_with_the_guard(self, raw):
        """quota_guard.threshold reads the same two variables in the hooks."""
        from ccreport import quota_guard

        assert sl._quota_threshold(raw) == quota_guard.threshold(raw)

    def test_the_statusline_never_imports_the_guard(self):
        """A per-render import costs every frame the tokenizing of that module."""
        src = Path(sl.__file__).read_text(encoding="utf-8")
        assert re.search(r"^\s*(?:from|import)\b.*quota_guard", src, re.MULTILINE) is None
