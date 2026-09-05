"""Tests for update_check.py — the twice-a-day "a newer release exists" check.

GitHub is never called: every test patches the urlopen underneath
``latest_release``, and every install shape is a patched ``__file__``.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from ccreport import cache_db, update_check

HERE = "0.1.1"
BREW = "/opt/homebrew/Cellar/ccreport/0.1.1/libexec/lib/python3.13/site-packages/ccreport"


def _as_module(monkeypatch, directory: str):
    """Point update_check at *directory* by moving where it thinks it lives."""
    monkeypatch.setattr(update_check, "__file__", f"{directory}/update_check.py")


def _serve(monkeypatch, body, status_code=None):
    """Answer the release request with *body*, or raise an HTTPError instead."""
    def _urlopen(req, timeout=None):
        if status_code is not None:
            raise urllib.error.HTTPError(req.full_url, status_code, "no", Message(), None)
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


class TestIsBrewInstall:
    """Which installs the check speaks for."""

    @pytest.mark.parametrize("directory", [
        "/opt/homebrew/Cellar/ccreport/0.1.1/libexec/lib/python3.13/site-packages/ccreport",
        "/usr/local/Cellar/ccreport/0.1.1/libexec/lib/python3.13/site-packages/ccreport",
        "/home/linuxbrew/.linuxbrew/Cellar/ccreport/0.1.1/lib/python3.13/site-pkgs/ccreport",
    ])
    def test_every_prefix_homebrew_uses(self, monkeypatch, directory):
        _as_module(monkeypatch, directory)
        assert update_check.is_brew_install() is True

    def test_a_checkout_is_not_one(self, monkeypatch):
        _as_module(monkeypatch, "/Users/me/git/ccreport/src/ccreport")
        assert update_check.is_brew_install() is False

    def test_a_uv_tool_install_is_not_one(self, monkeypatch):
        _as_module(
            monkeypatch,
            "/Users/me/.local/share/uv/tools/ccreport/lib/python3.13/site-packages/ccreport")
        assert update_check.is_brew_install() is False

    def test_another_formulas_keg_is_not_one(self, monkeypatch):
        """The name is half the match: ccreport vendored inside some other keg is not this."""
        _as_module(monkeypatch, "/opt/homebrew/Cellar/dogcat/1.0/libexec/ccreport")
        assert update_check.is_brew_install() is False

    def test_a_directory_merely_named_cellar_is_not_one(self, monkeypatch):
        _as_module(monkeypatch, "/Users/me/Cellar-notes/ccreport")
        assert update_check.is_brew_install() is False


class TestParseVersion:
    """Turning both spellings into something orderable."""

    @pytest.mark.parametrize(("text", "parsed"), [
        ("v0.2.0", (0, 2, 0)),
        ("0.2.0", (0, 2, 0)),
        ("V1.10.2", (1, 10, 2)),
        (" v3.4 ", (3, 4)),
        ("0.1.1.dev3+g9e7ff54", (0, 1, 1)),
    ])
    def test_it_reads_the_numeric_run(self, text, parsed):
        assert update_check.parse_version(text) == parsed

    @pytest.mark.parametrize("text", ["", "latest", "vNext", "release-2"])
    def test_anything_unordered_is_none(self, text):
        assert update_check.parse_version(text) is None


class TestIsNewer:
    """Which comparisons earn a line."""

    def test_a_higher_release_is_newer(self):
        assert update_check.is_newer("v0.2.0", "0.1.1") is True

    def test_the_same_release_is_not(self):
        assert update_check.is_newer("v0.1.1", "0.1.1") is False

    def test_a_lower_release_is_not(self):
        assert update_check.is_newer("v0.1.0", "0.1.1") is False

    def test_a_shorter_tag_pads_rather_than_losing(self):
        """0.2 and 0.2.0 name one release; only 0.2.1 is past it."""
        assert update_check.is_newer("v0.2", "0.2.0") is False
        assert update_check.is_newer("v0.2", "0.1.9") is True

    def test_two_digit_components_order_as_numbers(self):
        assert update_check.is_newer("v0.10.0", "0.9.0") is True

    @pytest.mark.parametrize(("latest", "current"),
                             [("latest", "0.1.1"), ("v0.2.0", "unknown")])
    def test_a_side_that_does_not_parse_is_never_newer(self, latest, current):
        assert update_check.is_newer(latest, current) is False


class TestLatestRelease:
    """What the release endpoint answers, and what every failure answers instead."""

    def test_it_returns_the_tag(self, monkeypatch):
        _serve(monkeypatch, {"tag_name": "v0.2.0"})
        assert update_check.latest_release() == "v0.2.0"

    def test_the_url_names_the_repo_and_the_latest_release(self, monkeypatch):
        seen: list[str] = []

        def _urlopen(req, timeout=None):
            seen.append(req.full_url)
            return io.BytesIO(b'{"tag_name": "v0.2.0"}')

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        update_check.latest_release()
        assert seen == [
            f"https://api.github.com/repos/{update_check.UPSTREAM_REPO}/releases/latest"]

    @pytest.mark.parametrize("code", [403, 404, 500])
    def test_an_http_error_is_unanswered(self, monkeypatch, code):
        _serve(monkeypatch, None, status_code=code)
        assert update_check.latest_release() is None

    def test_an_unreachable_host_is_unanswered(self, monkeypatch):
        def _urlopen(req, timeout=None):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        assert update_check.latest_release() is None

    def test_a_body_that_is_not_json_is_unanswered(self, monkeypatch):
        def _urlopen(req, timeout=None):
            return io.BytesIO(b"<html>")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        assert update_check.latest_release() is None

    def test_a_body_with_no_tag_is_unanswered(self, monkeypatch):
        _serve(monkeypatch, {"name": "no tag here"})
        assert update_check.latest_release() is None

    def test_a_body_that_is_not_an_object_is_unanswered(self, monkeypatch):
        _serve(monkeypatch, ["v0.2.0"])
        assert update_check.latest_release() is None


class TestRun:
    """One check, and what it leaves in the database."""

    @pytest.fixture
    def keg(self, monkeypatch):
        _as_module(monkeypatch, BREW)
        monkeypatch.setattr(update_check, "installed_version", lambda: HERE)

    def test_it_stores_the_tag_the_version_and_the_stamp(self, keg, monkeypatch):
        _serve(monkeypatch, {"tag_name": "v0.2.0"})
        monkeypatch.setattr(update_check.time, "time", lambda: 1_000_000.0)
        update_check.run()
        assert cache_db.read_update_check() == (1_000_000.0, HERE, "v0.2.0")

    def test_a_failed_check_still_advances_the_stamp(self, keg, monkeypatch):
        _serve(monkeypatch, None, status_code=403)
        monkeypatch.setattr(update_check.time, "time", lambda: 1_000_000.0)
        update_check.run()
        checked_at, _, latest = cache_db.read_update_check()
        assert (checked_at, latest) == (1_000_000.0, None)

    def test_a_failed_check_clears_a_tag_it_could_not_confirm(self, keg, monkeypatch):
        cache_db.write_update_check(HERE, "v0.2.0", 1.0)
        _serve(monkeypatch, None, status_code=500)
        update_check.run()
        assert cache_db.read_update_check()[2] is None

    def test_an_install_with_no_version_stores_no_tag(self, monkeypatch):
        """No distribution metadata: nothing to compare a tag against."""
        _as_module(monkeypatch, BREW)
        monkeypatch.setattr(update_check, "installed_version", lambda: None)
        monkeypatch.setattr(update_check.time, "time", lambda: 1_000_000.0)
        update_check.run()
        assert cache_db.read_update_check() == (1_000_000.0, "", None)

    def test_no_keg_writes_nothing(self, monkeypatch):
        _as_module(monkeypatch, "/Users/me/git/ccreport/src/ccreport")

        def boom(req, timeout=None):
            raise AssertionError("no request outside a keg")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        update_check.run()
        assert cache_db.read_update_check() == (0.0, "", None)

    def test_main_swallows_what_run_raises(self, monkeypatch):
        def boom():
            raise RuntimeError("no")

        monkeypatch.setattr(update_check, "run", boom)
        update_check.main()  # a detached child has nobody to report to


class TestInstalledVersion:
    """What the installed distribution says it is."""

    def test_it_reads_the_package_metadata(self):
        """The suite runs against an installed ccreport, so this answers a string."""
        assert isinstance(update_check.installed_version(), str)

    def test_no_metadata_is_none(self, monkeypatch):
        import importlib.metadata

        def boom(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", boom)
        assert update_check.installed_version() is None


class TestUpdateCheckStore:
    """The meta round trip, including the state that means 'no tag'."""

    def test_a_never_run_check_reads_empty(self):
        assert cache_db.read_update_check() == (0.0, "", None)

    def test_a_tag_survives_the_round_trip(self):
        cache_db.write_update_check(HERE, "v0.2.0", 1_000_000.0)
        assert cache_db.read_update_check() == (1_000_000.0, HERE, "v0.2.0")

    def test_none_clears_the_tag_and_keeps_the_stamp(self):
        cache_db.write_update_check(HERE, "v0.2.0", 1_000_000.0)
        cache_db.write_update_check(HERE, None, 1_000_100.0)
        assert cache_db.read_update_check() == (1_000_100.0, HERE, None)
