"""Bounded, best-effort diagnostics for local report startup.

One JSON file beside cache.db keeps the latest run, including its current
phase if interrupted. It contains durations and counts, never log contents.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from ccreport import cache_db

PREVIEW_STALE_AFTER_S = 86400

_active: ContextVar[ReportRun | None] = ContextVar("report_startup", default=None)


class ReportRun:
    def __init__(self, *, debug: bool = False) -> None:
        self.path = cache_db.DB_PATH.with_name("startup-timings.json")
        self.debug = debug
        self.started = time.perf_counter()
        self.data: dict = {
            "started_at": time.time(), "status": "running", "phases": {},
            "counts": {}, "active_phases": [],
        }
        self.token: Token | None = None

    def __enter__(self) -> Self:
        self.token = _active.set(self)
        self.save()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.data["status"] = "complete" if exc_type is None else "interrupted" if (
            exc_type is KeyboardInterrupt
        ) else "failed"
        self.data["total_seconds"] = round(time.perf_counter() - self.started, 4)
        self.save()
        if self.token is not None:
            _active.reset(self.token)
        if self.debug:
            print(f"Startup timings: {self.path}", file=sys.stderr)
            for name, seconds in self.data["phases"].items():
                print(f"  {name}: {seconds:.3f}s", file=sys.stderr)
            print(f"  total: {self.data['total_seconds']:.3f}s", file=sys.stderr)

    def save(self) -> None:
        """Replace the previous diagnostic atomically; failures never block a report."""
        _write_json(self.path, self.data)

    def preview_due(self) -> bool:
        """Show provisional output only after an unknown or day-old index."""
        stamp = cache_db.read_ccreport_indexed_at()
        return stamp is None or time.time() - stamp >= PREVIEW_STALE_AFTER_S

    def age_message(self) -> str:
        stamp = cache_db.read_ccreport_indexed_at()
        if stamp is None:
            return "Last index time is unknown; catching up may take a while."
        days = max(0, int((time.time() - stamp) / 86400))
        if days == 0:
            return "Last indexing completed less than a day ago."
        unit = "day" if days == 1 else "days"
        return f"Last indexing completed {days} {unit} ago; catching up may take a while."


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Measure a phase only inside a report; other cache readers pay no I/O."""
    run = _active.get()
    if run is None:
        yield
        return
    started = time.perf_counter()
    run.data["active_phases"].append(name)
    run.save()
    try:
        yield
    except BaseException:
        run.data.setdefault("stopped_phase", name)
        raise
    finally:
        phases = run.data["phases"]
        phases[name] = round(phases.get(name, 0) + time.perf_counter() - started, 4)
        run.data["active_phases"].remove(name)
        run.save()


def count(name: str, value: int) -> None:
    """Attach a corpus count without storing any record or project identity."""
    run = _active.get()
    if run is not None:
        run.data["counts"][name] = value


_PREVIEW_MAX_BYTES = 1024 * 1024
_PREVIEW_SCHEMA = 1


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A saved report contains local project/account names, like cache.db.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(path)
    except (OSError, UnicodeError):
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def save_preview(options: dict, text: str) -> None:
    """Keep one bounded, private snapshot of a successfully rendered report."""
    path = cache_db.DB_PATH.with_name("report-preview.json")
    try:
        text_size = len(text.encode("utf-8"))
    except UnicodeError:
        return
    if text_size > _PREVIEW_MAX_BYTES:
        # Do not leave an older snapshot looking like the latest saved report.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    _write_json(path, {
        "schema": _PREVIEW_SCHEMA, "options": options, "text": text, "saved_at": time.time(),
    })


def read_preview(options: dict) -> tuple[str, float] | None:
    """Return a matching saved layout without opening the database or logs."""
    path = cache_db.DB_PATH.with_name("report-preview.json")
    try:
        if path.stat().st_size > _PREVIEW_MAX_BYTES * 2:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != _PREVIEW_SCHEMA or data.get("options") != options:
            return None
        text, stamp = data.get("text"), data.get("saved_at")
        if not isinstance(text, str) or not isinstance(stamp, (int, float)):
            return None
        if not 0 < stamp <= time.time() or len(text.encode("utf-8")) > _PREVIEW_MAX_BYTES:
            return None
        # Validate the timestamp before the CLI converts it on any platform.
        datetime.fromtimestamp(stamp, tz=UTC)
        return text, stamp
    except (OSError, ValueError, AttributeError, OverflowError):
        return None
