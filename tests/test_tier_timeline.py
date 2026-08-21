"""The declared plan timeline: what the file may say, and what it answers."""

from __future__ import annotations

import datetime as dt

import pytest

from ccreport import tier_timeline


def _epoch(text: str) -> float:
    return dt.datetime.fromisoformat(text).timestamp()


class TestParse:
    """A person types this file to correct their own history."""

    ONE = """
        [[tier]]
        at = 2026-02-03T21:03:33Z
        account = "me@example.net"
        organization_rate_limit_tier = "default_claude_max_5x"
    """

    def test_it_reads_an_entry(self):
        (entry,) = tier_timeline.parse(self.ONE)
        assert entry.ts == _epoch("2026-02-03T21:03:33+00:00")
        assert entry.account == "me@example.net"
        assert entry.organization_rate_limit_tier == "default_claude_max_5x"
        assert entry.seat_tier is None
        assert entry.user_rate_limit_tier is None

    def test_an_empty_document_declares_nothing(self):
        assert tier_timeline.parse("") == []

    def test_a_bare_date_is_midnight_utc(self):
        """What a PDF invoice can honestly say: the day, and no clock time."""
        (entry,) = tier_timeline.parse(
            '[[tier]]\nat = 2026-04-02\naccount = "a"\n'
        )
        assert entry.ts == _epoch("2026-04-02T00:00:00+00:00")

    def test_a_naive_datetime_is_read_as_utc(self):
        """Not the zone of whichever machine happens to apply the file."""
        (entry,) = tier_timeline.parse(
            '[[tier]]\nat = 2026-04-02T06:00:00\naccount = "a"\n'
        )
        assert entry.ts == _epoch("2026-04-02T06:00:00+00:00")

    def test_an_offset_is_honoured(self):
        (entry,) = tier_timeline.parse(
            '[[tier]]\nat = 2026-04-02T08:00:00+02:00\naccount = "a"\n'
        )
        assert entry.ts == _epoch("2026-04-02T06:00:00+00:00")

    def test_entries_come_back_sorted_by_account_then_time(self):
        entries = tier_timeline.parse("""
            [[tier]]
            at = 2026-03-01T00:00:00Z
            account = "b"
            [[tier]]
            at = 2026-02-01T00:00:00Z
            account = "b"
            [[tier]]
            at = 2026-05-01T00:00:00Z
            account = "a"
        """)
        assert [(e.account, e.ts) for e in entries] == sorted(
            (e.account, e.ts) for e in entries
        )

    def test_an_empty_tier_string_is_absent_rather_than_stored(self):
        (entry,) = tier_timeline.parse(
            '[[tier]]\nat = 2026-04-02\naccount = "a"\nseat_tier = ""\n'
        )
        assert entry.seat_tier is None

    def test_an_unrecognized_tier_name_is_taken_as_given(self):
        """The names come from Anthropic; a list here would reject the next one."""
        (entry,) = tier_timeline.parse(
            '[[tier]]\nat = 2026-04-02\naccount = "a"\nseat_tier = "whatever_5x"\n'
        )
        assert entry.seat_tier == "whatever_5x"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[[tier]\n", "not readable as TOML"),
            ("tier = 3\n", "list of tables"),
            ('[[tier]]\naccount = "a"\n', "missing 'at'"),
            ("[[tier]]\nat = 2026-04-02\n", "'account' must be"),
            ('[[tier]]\nat = 2026-04-02\naccount = ""\n', "'account' must be"),
            ('[[tier]]\nat = "yesterday"\naccount = "a"\n', "must be a TOML date"),
            ('[[tier]]\nat = 2026-04-02\naccount = "a"\nseat_tier = 5\n', "must be a string"),
            ('[[tier]]\nat = 2026-04-02\naccount = "a"\ntier = "x"\n', "unknown key"),
        ],
    )
    def test_it_refuses_rather_than_dropping_a_line(self, text, expected):
        """A silently dropped line leaves a gap shaped like a plan never had."""
        with pytest.raises(ValueError, match=expected):
            tier_timeline.parse(text)

    def test_two_entries_at_one_instant_for_one_account_is_an_error(self):
        """They contradict each other; picking one would be a guess."""
        with pytest.raises(ValueError, match="two entries"):
            tier_timeline.parse("""
                [[tier]]
                at = 2026-04-02T00:00:00Z
                account = "a"
                seat_tier = "one"
                [[tier]]
                at = 2026-04-02T00:00:00Z
                account = "a"
                seat_tier = "two"
            """)

    def test_one_instant_across_two_accounts_is_fine(self):
        assert len(tier_timeline.parse("""
            [[tier]]
            at = 2026-04-02T00:00:00Z
            account = "a"
            [[tier]]
            at = 2026-04-02T00:00:00Z
            account = "b"
        """)) == 2

    def test_the_error_names_which_entry_was_bad(self):
        with pytest.raises(ValueError, match=r"#2"):
            tier_timeline.parse(
                '[[tier]]\nat = 2026-04-02\naccount = "a"\n[[tier]]\naccount = "b"\n'
            )


class TestRender:
    """A stored timeline comes back into the box it was typed in."""

    def test_it_round_trips(self):
        text = """
            [[tier]]
            at = 2026-02-03T21:03:33Z
            account = "me@example.net"
            organization_rate_limit_tier = "default_claude_max_5x"

            [[tier]]
            at = 2026-04-02T00:00:00Z
            account = "u-work"
            seat_tier = "team_tier_1"
            user_rate_limit_tier = "default_claude_max_5x"
            organization_rate_limit_tier = "default_raven"
        """
        entries = tier_timeline.parse(text)
        assert tier_timeline.parse(tier_timeline.render(entries)) == entries

    def test_it_writes_oldest_first(self):
        rendered = tier_timeline.render([
            tier_timeline.Entry(ts=2000.0, account="a"),
            tier_timeline.Entry(ts=1000.0, account="a"),
        ])
        assert rendered.index("1970-01-01T00:16:40Z") < rendered.index(
            "1970-01-01T00:33:20Z"
        )

    def test_a_field_nothing_set_is_left_out(self):
        rendered = tier_timeline.render([
            tier_timeline.Entry(ts=0.0, account="a", seat_tier="team_tier_1"),
        ])
        assert "seat_tier" in rendered
        assert "user_rate_limit_tier" not in rendered

    def test_nothing_renders_as_nothing(self):
        """Not a stray newline, which would parse as a document with content."""
        assert tier_timeline.render([]) == ""


class TestEffectiveTier:
    """The per-user bucket wins; the org pool is what it would have shared."""

    def test_the_user_tier_wins(self):
        assert tier_timeline.effective_tier({
            "user_rate_limit_tier": "default_claude_max_5x",
            "organization_rate_limit_tier": "default_raven",
        }) == "default_claude_max_5x"

    def test_the_org_tier_answers_a_personal_plan(self):
        assert tier_timeline.effective_tier({
            "organization_rate_limit_tier": "default_claude_max_20x",
        }) == "default_claude_max_20x"

    def test_neither_is_none(self):
        assert tier_timeline.effective_tier({"seat_tier": "team_tier_1"}) is None


class TestTierTimeline:
    """Which tier an account was on at a moment, by declared entry."""

    ENTRIES = """
        [[tier]]
        at = 2026-02-01T00:00:00Z
        account = "home"
        organization_rate_limit_tier = "pro"
        [[tier]]
        at = 2026-03-01T00:00:00Z
        account = "home"
        organization_rate_limit_tier = "max_5x"
        [[tier]]
        at = 2026-06-01T00:00:00Z
        account = "work"
        user_rate_limit_tier = "seat"
    """

    @pytest.fixture
    def timeline(self):
        return tier_timeline.TierTimeline(tier_timeline.parse(self.ENTRIES))

    def test_a_moment_takes_the_entry_in_force(self, timeline):
        assert timeline.at("home", _epoch("2026-02-15T00:00:00+00:00")) == "pro"
        assert timeline.at("home", _epoch("2026-03-01T00:00:00+00:00")) == "max_5x"
        assert timeline.at("home", _epoch("2026-09-01T00:00:00+00:00")) == "max_5x"

    def test_a_moment_before_the_first_entry_has_no_tier(self, timeline):
        """The declaration starts where the receipts do."""
        assert timeline.at("home", _epoch("2026-01-01T00:00:00+00:00")) is None

    def test_accounts_do_not_share_a_curve(self, timeline):
        assert timeline.at("work", _epoch("2026-02-15T00:00:00+00:00")) is None
        assert timeline.at("work", _epoch("2026-07-01T00:00:00+00:00")) == "seat"

    def test_an_unknown_account_has_no_tier(self, timeline):
        assert timeline.at("nobody", _epoch("2026-07-01T00:00:00+00:00")) is None

    def test_an_empty_timeline_is_falsy(self):
        assert not tier_timeline.TierTimeline([])
        assert tier_timeline.TierTimeline(tier_timeline.parse(self.ENTRIES))
