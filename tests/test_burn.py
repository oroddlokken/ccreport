"""The burn-rate projection, and the two surfaces that print it."""

from __future__ import annotations

import io

import pytest

from ccreport import burn

NOW = 1_800_000_000.0
RESET = NOW + 3 * 3600


def _samples(points, window="session", resets_at=RESET) -> list[dict]:
    """(minutes before now, percent) pairs as snapshot rows."""
    return [
        {"ts": NOW - minutes * 60, "used_pct": pct, "window": window,
         "resets_at": resets_at, "model": None, "source": "stdin"}
        for minutes, pct in points
    ]


class TestProjection:
    def test_a_steady_rise_lands_where_the_arithmetic_says(self):
        """10 points an hour from 40%, so 60 points left is six hours."""
        result = burn.project(_samples([(60, 30.0), (30, 35.0), (0, 40.0)],
                                       resets_at=NOW + 10 * 3600), NOW)
        assert result is not None
        assert result.rate_pp_per_s == pytest.approx(10 / 3600, rel=1e-6)
        assert result.exhausts_at == pytest.approx(NOW + 6 * 3600, rel=1e-6)

    def test_a_flat_slope_says_nothing(self):
        """Nobody is working; there is no rate to project."""
        assert burn.project(_samples([(60, 40.0), (30, 40.0), (0, 40.0)]), NOW) is None

    def test_a_falling_slope_says_nothing(self):
        """The percentage is the server's; a correction is not an announcement."""
        assert burn.project(_samples([(60, 50.0), (0, 40.0)]), NOW) is None

    def test_one_sample_says_nothing(self):
        """A point is not a rate."""
        assert burn.project(_samples([(0, 40.0)]), NOW) is None

    def test_no_samples_say_nothing(self):
        assert burn.project([], NOW) is None

    def test_a_window_that_would_reset_first_says_so(self):
        result = burn.project(_samples([(60, 10.0), (0, 11.0)]), NOW)
        assert result is not None
        assert result.exhausts_at is None
        assert not result.before_reset

    def test_a_window_already_at_100_says_nothing(self):
        assert burn.project(_samples([(60, 90.0), (0, 100.0)]), NOW) is None

    def test_a_window_whose_reset_has_passed_says_nothing(self):
        assert burn.project(
            _samples([(60, 10.0), (0, 20.0)], resets_at=NOW - 60), NOW,
        ) is None

    def test_recent_samples_weigh_more_than_old_ones(self):
        """The question is about the rate right now, not this morning's average."""
        slow_then_fast = burn.project(
            _samples([(240, 0.0), (180, 1.0), (30, 20.0), (0, 40.0)],
                     resets_at=NOW + 10 * 3600), NOW)
        even = burn.project(
            _samples([(240, 0.0), (180, 10.0), (30, 30.0), (0, 40.0)],
                     resets_at=NOW + 10 * 3600), NOW)
        assert slow_then_fast is not None
        assert even is not None
        assert slow_then_fast.rate_pp_per_s > even.rate_pp_per_s

    def test_the_projection_is_anchored_at_the_last_reading(self):
        """The gap since it is time nobody sampled, not time the window filled."""
        stale = burn.project(_samples([(120, 30.0), (60, 40.0)],
                                      resets_at=NOW + 10 * 3600), NOW)
        assert stale is not None
        # 10 points an hour, 60 left, measured from the reading an hour ago.
        assert stale.exhausts_at == pytest.approx(NOW - 3600 + 6 * 3600, rel=1e-6)


class TestLateSampling:
    def test_a_window_first_seen_part_full_is_flagged(self):
        result = burn.project(_samples([(60, 40.0), (0, 50.0)],
                                       resets_at=NOW + 10 * 3600), NOW)
        assert result is not None
        assert result.partial

    def test_a_window_seen_from_the_start_is_not(self):
        result = burn.project(_samples([(60, 1.0), (0, 11.0)],
                                       resets_at=NOW + 10 * 3600), NOW)
        assert result is not None
        assert not result.partial

    def test_the_gap_never_enters_the_slope(self):
        """Nothing is extrapolated across what was not observed."""
        late = burn.project(_samples([(60, 40.0), (30, 45.0), (0, 50.0)],
                                     resets_at=NOW + 20 * 3600), NOW)
        early = burn.project(_samples([(60, 0.0), (30, 5.0), (0, 10.0)],
                                      resets_at=NOW + 20 * 3600), NOW)
        assert late is not None
        assert early is not None
        assert late.rate_pp_per_s == pytest.approx(early.rate_pp_per_s)

    def test_it_says_so_in_the_sentence(self):
        result = burn.project(_samples([(60, 40.0), (0, 50.0)],
                                       resets_at=NOW + 10 * 3600), NOW)
        assert result is not None
        assert "sampled late" in burn.describe(result, NOW, lambda ts: "5pm")


class TestCurrentInstance:
    def test_it_picks_the_instance_still_running(self):
        old = _samples([(600, 90.0)], resets_at=NOW - 3600)
        live = _samples([(60, 10.0), (0, 20.0)])
        assert burn.current_instance(old + live, "session", NOW) == live

    def test_another_window_is_not_this_one(self):
        session = _samples([(60, 10.0)], window="session")
        week = _samples([(60, 50.0)], window="week")
        assert burn.current_instance(session + week, "week", NOW) == week

    def test_nothing_running_is_no_instance(self):
        assert burn.current_instance(_samples([(60, 10.0)], resets_at=NOW - 1),
                                     "session", NOW) == []

    def test_a_model_narrows_the_scoped_window(self):
        rows = _samples([(60, 10.0)], window="scoped")
        rows[0]["model"] = "opus"
        assert burn.current_instance(rows, "scoped", NOW, "opus") == rows
        assert burn.current_instance(rows, "scoped", NOW, "sonnet") == []


class TestSpan:
    @pytest.mark.parametrize(("seconds", "text"), [
        (0, "0m"), (90, "1m"), (3600, "1h"), (5400, "1h 30m"),
        (86400, "1d"), (90000, "1d 1h"), (-5, "0m"),
    ])
    def test_it_reads_as_the_coarsest_two_units(self, seconds, text):
        assert burn.span(seconds) == text


class TestDescribe:
    def test_it_names_the_time_and_the_slack(self):
        result = burn.project(_samples([(60, 30.0), (0, 40.0)],
                                       resets_at=NOW + 10 * 3600), NOW)
        assert result is not None
        text = burn.describe(result, NOW, lambda ts: "5pm")
        assert "100% lands in 6h" in text
        assert "at 5pm" in text
        assert "before the reset" in text

    def test_a_reset_first_answer_says_that_instead(self):
        result = burn.project(_samples([(60, 10.0), (0, 11.0)]), NOW)
        assert result is not None
        assert "resets before the quota runs out" in burn.describe(result, NOW, lambda ts: "5pm")


class TestCcuLine:
    def _line(self, monkeypatch, samples, window="session"):
        from ccreport import cache_db, ccu

        monkeypatch.setattr(cache_db, "load_rate_limit_snapshots", lambda: samples)
        return ccu.burn_line(window, NOW)

    def test_it_renders_under_the_bar_it_describes(self, monkeypatch):
        line = self._line(monkeypatch, _samples([(60, 30.0), (0, 40.0)],
                                                resets_at=NOW + 10 * 3600))
        assert "100% lands in" in line

    def test_nothing_to_project_is_no_line(self, monkeypatch):
        assert self._line(monkeypatch, _samples([(0, 40.0)])) == ""

    def test_a_busy_database_costs_the_line_not_the_run(self, monkeypatch):
        from ccreport import cache_db, ccu

        def boom():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(cache_db, "load_rate_limit_snapshots", boom)
        assert ccu.burn_line("session", NOW) == ""

    def test_the_week_window_is_read_separately(self, monkeypatch):
        rows = _samples([(60, 30.0), (0, 40.0)], window="week", resets_at=NOW + 100 * 3600)
        assert "100% lands in" in self._line(monkeypatch, rows, "week")
        assert self._line(monkeypatch, rows, "session") == ""

    def test_the_dashboard_carries_it(self, monkeypatch):
        from ccreport import cache_db, ccu

        monkeypatch.setattr(
            cache_db, "load_rate_limit_snapshots",
            lambda: _samples([(60, 30.0), (0, 40.0)], resets_at=NOW + 10 * 3600),
        )
        lines = ccu.render({"session_percent": 40, "session_reset": ""}, NOW, "Europe/Oslo")
        assert any("100% lands in" in line for line in lines)


class TestStatuslineSegment:
    def _rendered(self, monkeypatch, samples, *, on=True):
        from ccreport import cache_db
        from ccreport import statusline as sl

        monkeypatch.setattr(cache_db, "load_rate_limit_snapshots", lambda: samples)
        if on:
            monkeypatch.setenv("CLAUDE_STATUSLINE_BURN", "1")
        return sl._render_burn(NOW)

    def test_it_is_off_by_default(self, monkeypatch):
        """A second opinion on two bars the line already carries."""
        rows = _samples([(60, 30.0), (0, 40.0)], resets_at=NOW + 10 * 3600)
        assert self._rendered(monkeypatch, rows, on=False) == ""

    def test_the_toggle_turns_it_on(self, monkeypatch):
        rows = _samples([(60, 30.0), (0, 40.0)], resets_at=NOW + 10 * 3600)
        assert "S full in 6h" in self._rendered(monkeypatch, rows)

    def test_both_windows_share_one_segment(self, monkeypatch):
        rows = (_samples([(60, 30.0), (0, 40.0)], resets_at=NOW + 10 * 3600)
                + _samples([(60, 30.0), (0, 40.0)], window="week",
                           resets_at=NOW + 100 * 3600))
        out = self._rendered(monkeypatch, rows)
        assert "S full in" in out
        assert "W full in" in out

    def test_a_window_that_resets_first_is_left_out(self, monkeypatch):
        assert self._rendered(monkeypatch, _samples([(60, 10.0), (0, 11.0)])) == ""

    def test_a_busy_database_costs_the_segment_not_the_render(self, monkeypatch):
        from ccreport import cache_db
        from ccreport import statusline as sl

        def boom():
            raise RuntimeError("database is locked")

        monkeypatch.setenv("CLAUDE_STATUSLINE_BURN", "1")
        monkeypatch.setattr(cache_db, "load_rate_limit_snapshots", boom)
        assert sl._render_burn(NOW) == ""

    def test_the_layout_places_it_above_the_update_line(self, monkeypatch):
        from ccreport import statusline as sl

        printed = io.StringIO()
        monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.write(
            " ".join(str(x) for x in a) + "\n"))
        sl._layout_and_print(
            ["top"], "", "", "", "", {}, "", "", "", "UPDATE-LINE",
            NOW, NOW, force_red=False, burn="BURN-LINE",
        )
        out = printed.getvalue()
        assert "BURN-LINE" in out
        assert out.index("BURN-LINE") < out.index("UPDATE-LINE")

    def test_the_toggle_off_renders_what_it_rendered_before(self, monkeypatch):
        """Nothing about the line moves until someone asks for the segment."""
        from ccreport import statusline as sl

        printed = io.StringIO()
        monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.write(
            " ".join(str(x) for x in a) + "\n"))
        sl._layout_and_print(
            ["top"], "", "", "", "", {}, "", "", "", "UPDATE-LINE", NOW, NOW,
        )
        without = printed.getvalue()

        printed.truncate(0)
        printed.seek(0)
        sl._layout_and_print(
            ["top"], "", "", "", "", {}, "", "", "", "UPDATE-LINE", NOW, NOW,
            force_red=False, burn="",
        )
        assert printed.getvalue() == without
