"""The usage dashboard: what it prints, for a usage row it is handed.

The fetch is stubbed at fetch_usage in every test here. What is under test is
the rendering, which the zsh version had no way to cover — it drew the bars
with printf and read the JSON through jq, so the only way to see its output was
to call the API.

Times are pinned: NOW is a Wednesday 09:00 local, and every reset in the
fixtures is expressed as an offset from it, so "today"/"tomorrow" and the
plural forms are exercised rather than depending on when the suite runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from ccreport import ccu

ZONE = "Europe/Oslo"
NOW_DT = datetime(2026, 6, 17, 9, 0, 0)  # noqa: DTZ001 — local by design
NOW = NOW_DT.timestamp()


def iso(**delta) -> str:
    return (NOW_DT + timedelta(**delta)).isoformat()


def strip_ansi(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            i = text.index("m", i) + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


class TestBar:
    @pytest.mark.parametrize(("pct", "filled"), [(0, 0), (1, 0), (2, 1), (50, 25), (99, 49), (100, 50)])
    def test_the_meter_fills_one_cell_per_two_percent(self, pct, filled):
        plain = strip_ansi(ccu.bar(pct))
        assert plain.count("█") == filled
        assert len(plain) == ccu.BAR_WIDTH

    @pytest.mark.parametrize("pct", [-10, 140])
    def test_a_reading_outside_the_scale_still_draws_one_bar(self, pct):
        """The API has answered over 100 before; a wrapped bar is worse than a full one."""
        plain = strip_ansi(ccu.bar(pct))
        assert len(plain) == ccu.BAR_WIDTH


class TestCountdown:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            ({"minutes": 42}, "42 minutes"),
            ({"minutes": 1}, "1 minute"),
            ({"hours": 3}, "3 hours"),
            ({"hours": 1}, "1 hour"),
            ({"hours": 3, "minutes": 14}, "3 hours and 14 minutes"),
            ({"hours": 1, "minutes": 1}, "1 hour and 1 minute"),
            ({"days": 1}, "1 day"),
            ({"days": 2, "hours": 3}, "2 days and 3 hours"),
            ({"days": 1, "hours": 1}, "1 day and 1 hour"),
        ],
    )
    def test_it_reads_as_words_with_the_right_plurals(self, delta, expected):
        assert ccu.countdown(NOW + timedelta(**delta).total_seconds(), NOW) == expected

    def test_a_reset_four_days_out_does_not_mention_its_minutes(self):
        assert ccu.countdown(NOW + timedelta(days=4, minutes=30).total_seconds(), NOW) == "4 days"

    @pytest.mark.parametrize("delta", [0, -60, -86400])
    def test_a_moment_already_past_counts_down_to_nothing(self, delta):
        assert ccu.countdown(NOW + delta, NOW) == ""


class TestResetLine:
    def test_a_reset_later_today_names_the_time_alone(self):
        line = ccu.reset_line(iso(hours=3, minutes=14), NOW, ZONE)
        assert line == "Resets in 3 hours and 14 minutes at 12:14pm (Europe/Oslo)"

    def test_a_whole_hour_drops_the_minutes(self):
        assert "at 12pm " in ccu.reset_line(iso(hours=3), NOW, ZONE)

    def test_tomorrow_still_needs_no_date(self):
        line = ccu.reset_line(iso(days=1, hours=1), NOW, ZONE)
        assert "on Jun" not in line
        assert "at 10am" in line

    def test_further_out_than_tomorrow_carries_its_date(self):
        """"at 10am" alone would read as today's."""
        assert "at 10am on Jun 20" in ccu.reset_line(iso(days=3, hours=1), NOW, ZONE)

    def test_midnight_is_a_date_the_api_gave_no_time_for(self):
        """Printing it as "12am" would claim a precision the response lacked."""
        line = ccu.reset_line(iso(days=2, hours=15), NOW, ZONE)
        assert "am" not in line
        assert line == "Resets in 2 days and 15 hours on Jun 20 (Europe/Oslo)"

    def test_a_past_reset_drops_the_in_clause(self):
        line = ccu.reset_line(iso(hours=-3), NOW, ZONE)
        assert line.startswith("Resets at ")
        assert " in " not in line

    @pytest.mark.parametrize("value", ["", "not-a-time", "2026-13-45T99:99"])
    def test_an_unreadable_reset_prints_no_line_at_all(self, value):
        assert ccu.reset_line(value, NOW, ZONE) == ""


class TestPaceLine:
    def _pace(self, actual, elapsed_days):
        reset = iso(days=7 - elapsed_days)
        return strip_ansi(ccu.pace_line(actual, reset, NOW))

    def test_it_reports_elapsed_time_expected_use_and_the_gap(self):
        assert self._pace(50, 3.5) == "3d 12h into 7-day window (pace: 7d) — 50% expected, +0%"

    def test_being_behind_reads_as_a_negative_delta(self):
        assert "20% expected, +30%" in self._pace(50, 1.4)

    def test_a_shorter_pace_raises_the_bar_it_is_measured_against(self, monkeypatch):
        """pace 5 means the quota should be gone by Friday, so expected climbs faster."""
        monkeypatch.setenv("CLAUDE_CODE_PACE_DAYS", "5")
        assert "70% expected" in self._pace(50, 3.5)

    @pytest.mark.parametrize(
        ("delta_days", "colour"),
        [(0.5, "0;31"), (3.0, "0;33"), (3.5, "0;32"), (4.0, "0;36"), (5.5, "0;90")],
    )
    def test_the_colour_bands_run_from_overcooking_to_underusing(self, delta_days, colour):
        assert f"\033[{colour}m" in ccu.pace_line(50, iso(days=7 - delta_days), NOW)

    def test_a_window_that_has_not_started_gets_no_line(self):
        assert ccu.pace_line(50, iso(days=7), NOW) == ""

    def test_an_unreadable_reset_gets_no_line(self):
        assert ccu.pace_line(50, "nonsense", NOW) == ""

    def test_hours_alone_when_the_window_is_less_than_a_day_old(self):
        assert self._pace(5, 0.25).startswith("6h into")


class TestLastFetched:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "just now"), (30, "just now"), (60, "1 minute ago"), (600, "10 minutes ago")],
    )
    def test_the_header_ages_in_whole_minutes(self, seconds, expected):
        row = {"last_updated": iso(seconds=-seconds), "session_percent": 5}
        assert expected in strip_ansi(ccu.render(row, NOW, ZONE)[0])

    def test_a_row_with_no_timestamp_opens_on_the_blank_line(self):
        assert ccu.render({"session_percent": 5}, NOW, ZONE)[0] == ""


class TestRender:
    def _row(self, **over):
        row = {
            "last_updated": iso(minutes=-2),
            "session_percent": 8,
            "session_reset": iso(hours=2),
            "week_percent": 61,
            "week_reset": iso(days=2),
        }
        row.update(over)
        return row

    def _plain(self, row):
        return strip_ansi("\n".join(ccu.render(row, NOW, ZONE)))

    def test_session_and_week_are_the_two_every_plan_gets(self):
        out = self._plain(self._row())
        assert "Current session" in out
        assert "Current week (all models)" in out
        assert "Sonnet" not in out
        assert "Extra usage" not in out

    def test_the_week_carries_a_pace_line_and_the_session_does_not(self):
        out = self._plain(self._row())
        assert out.count("into 7-day window") == 1

    def test_sonnet_appears_only_when_the_plan_reports_it(self):
        out = self._plain(self._row(sonnet_percent=12, sonnet_reset=iso(days=2)))
        assert "Current week (Sonnet only) " not in out
        assert "Current week (Sonnet only)" in out
        assert "12% used" in out

    def test_a_scoped_quota_is_titled_with_its_model(self):
        out = self._plain(self._row(scoped_percent=33, scoped_model="Fable", scoped_reset=iso(days=2)))
        assert "Current week (Fable only)" in out

    def test_a_scoped_quota_with_no_model_name_still_renders(self):
        out = self._plain(self._row(scoped_percent=33, scoped_reset=iso(days=2)))
        assert "Current week (model only)" in out

    def test_extra_usage_shows_what_was_spent_against_the_limit(self):
        out = self._plain(
            self._row(extra_percent=40, extra_spent=20, extra_limit=50),
        )
        assert "Extra usage" in out
        assert "$20.00 / $50.00 spent" in out

    def test_extra_usage_without_amounts_still_shows_the_bar(self):
        out = self._plain(self._row(extra_percent=40))
        assert "Extra usage" in out
        assert "spent" not in out

    def test_extra_usage_carries_no_reset_line(self):
        """The API gives the Extra quota no reset, so the section draws none —
        the week's reset above it is the only one on screen.
        """
        out = self._plain(self._row(extra_percent=40, extra_spent=20, extra_limit=50))
        assert out.count("Resets in") == 2  # session and week

    def test_a_null_quota_is_absent_rather_than_zero(self):
        """write_usage_cache stores a lapsed quota as an explicit null."""
        out = self._plain(self._row(sonnet_percent=None, scoped_percent=None))
        assert "Sonnet" not in out
        assert "0% used" not in out

    def test_it_opens_and_closes_on_a_blank_line(self):
        lines = ccu.render(self._row(), NOW, ZONE)
        assert lines[1] == ""
        assert lines[-1] == ""


class TestMain:
    def test_a_fetch_that_produced_nothing_is_reported_and_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(ccu, "fetch_usage", lambda **_: None)
        assert ccu.main([]) == 1
        assert "Failed to fetch usage data" in capsys.readouterr().err

    def test_a_row_with_neither_session_nor_week_is_not_usage_data(self, monkeypatch, capsys):
        monkeypatch.setattr(ccu, "fetch_usage", lambda **_: {"last_updated": iso()})
        assert ccu.main([]) == 1
        assert "No usage data available" in capsys.readouterr().err

    def test_a_week_alone_is_enough_to_render(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ccu, "fetch_usage", lambda **_: {"week_percent": 20, "week_reset": iso(days=2)},
        )
        assert ccu.main([]) == 0
        assert "Current week (all models)" in capsys.readouterr().out

    def test_force_is_passed_through_to_the_fetch(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(ccu, "fetch_usage", lambda **kw: seen.update(kw) or {"session_percent": 1})
        ccu.main(["--force"])
        assert seen == {"force": True}
        ccu.main([])
        assert seen == {"force": False}

    def test_json_bypasses_rendering_entirely(self, monkeypatch):
        monkeypatch.setattr(ccu, "emit_raw", lambda: 7)
        monkeypatch.setattr(ccu, "fetch_usage", lambda **_: pytest.fail("must not fetch"))
        assert ccu.main(["--json"]) == 7

    @pytest.mark.parametrize("flag", ["--nope", "-x", "extra"])
    def test_an_unknown_argument_exits_two(self, flag, capsys):
        with pytest.raises(SystemExit) as e:
            ccu.main([flag])
        assert e.value.code == 2
        assert ccu.USAGE in capsys.readouterr().err


class TestFetchUsage:
    def _run(self, monkeypatch, stdout):
        class _Done:
            def __init__(self):
                self.stdout = stdout

        monkeypatch.setattr(ccu.subprocess, "run", lambda *a, **k: _Done())
        return ccu.fetch_usage(force=False)

    def test_it_parses_what_usage_api_printed(self, monkeypatch):
        assert self._run(monkeypatch, json.dumps({"session_percent": 5})) == {"session_percent": 5}

    @pytest.mark.parametrize("stdout", ["", "   \n", "not json", "[1, 2]", '"a string"'])
    def test_anything_that_is_not_a_usage_object_is_no_data(self, monkeypatch, stdout):
        """A crash, a warning on stdout or a JSON array all mean the same to the caller."""
        assert self._run(monkeypatch, stdout) is None

    def test_an_interpreter_that_will_not_start_is_no_data(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("no such file")

        monkeypatch.setattr(ccu.subprocess, "run", boom)
        assert ccu.fetch_usage(force=False) is None

    def test_the_child_gets_the_package_root_on_its_path(self, monkeypatch):
        """usage_api is run with -m, so it resolves ccreport off its own sys.path."""
        monkeypatch.delenv("PYTHONPATH", raising=False)
        assert ccu._usage_env()["PYTHONPATH"].endswith("src") or "ccreport" in ccu._usage_env()["PYTHONPATH"]

    def test_an_existing_pythonpath_is_kept_behind_it(self, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
        assert ccu._usage_env()["PYTHONPATH"].endswith("/somewhere/else")


class TestTimezone:
    def test_it_answers_with_the_zone_name_not_the_abbreviation(self):
        """"CEST" does not say which zone; the reset line names one."""
        name = ccu.tz_name()
        assert name
        assert "zoneinfo" not in name

    def test_a_machine_with_no_localtime_symlink_still_answers(self, monkeypatch):
        def boom(_self):
            raise OSError("not a symlink")

        monkeypatch.setattr(ccu.Path, "readlink", boom)
        assert ccu.tz_name()
