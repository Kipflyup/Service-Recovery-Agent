"""Log reading and lightweight traceback watching utilities.

This module provides the first Agent-side bridge in the demo chain:

service writes logs -> Agent reads new log text -> Agent extracts traceback.

The implementation intentionally starts with a polling tailer.  It is simple,
portable, easy to test, and enough for the local demo.  The project dependency
list also includes ``watchdog`` so we can later replace or complement polling
with OS-level filesystem events if needed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .traceback_parser import TracebackEvent, parse_latest_traceback, parse_tracebacks


@dataclass
class LogCursor:
    """Mutable cursor for tailing a single log file."""

    path: Path
    offset: int = 0

    @classmethod
    def from_start(cls, path: str | os.PathLike[str]) -> "LogCursor":
        return cls(path=Path(path), offset=0)

    @classmethod
    def from_end(cls, path: str | os.PathLike[str]) -> "LogCursor":
        log_path = Path(path)
        offset = log_path.stat().st_size if log_path.exists() else 0
        return cls(path=log_path, offset=offset)

    def read_new_text(self) -> str:
        """Read text appended since the previous call.

        If the file is truncated or rotated, the cursor automatically restarts
        from the beginning of the new file.
        """

        if not self.path.exists():
            return ""

        current_size = self.path.stat().st_size
        if current_size < self.offset:
            self.offset = 0

        with self.path.open("r", encoding="utf-8", errors="replace") as file:
            file.seek(self.offset)
            text = file.read()
            self.offset = file.tell()
        return text


def read_log_text(log_path: str | os.PathLike[str]) -> str:
    """Read the whole log file, returning an empty string if it does not exist."""

    path = Path(log_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def read_latest_traceback(log_path: str | os.PathLike[str]) -> TracebackEvent | None:
    """Parse and return the latest Python traceback from a log file."""

    return parse_latest_traceback(read_log_text(log_path), source_path=str(log_path))


def iter_tracebacks(log_path: str | os.PathLike[str]) -> Iterator[TracebackEvent]:
    """Yield all Python tracebacks currently present in a log file."""

    yield from parse_tracebacks(read_log_text(log_path), source_path=str(log_path))


def wait_for_traceback(
    log_path: str | os.PathLike[str],
    *,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.5,
    start_at_end: bool = True,
) -> TracebackEvent | None:
    """Wait until a traceback appears in ``log_path``.

    Args:
        log_path: File to monitor.
        timeout_seconds: Maximum wait time.
        poll_interval_seconds: Delay between reads.
        start_at_end: When true, ignore existing content and only consider new
            log lines.  When false, parse existing content first.
    """

    cursor = LogCursor.from_end(log_path) if start_at_end else LogCursor.from_start(log_path)
    buffer = ""
    deadline = time.monotonic() + timeout_seconds

    if not start_at_end:
        buffer += cursor.read_new_text()
        event = parse_latest_traceback(buffer, source_path=str(log_path))
        if event:
            return event

    while time.monotonic() < deadline:
        new_text = cursor.read_new_text()
        if new_text:
            buffer += new_text
            event = parse_latest_traceback(buffer, source_path=str(log_path))
            if event:
                return event
        time.sleep(poll_interval_seconds)

    return None


def watch_tracebacks(
    log_path: str | os.PathLike[str],
    callback: Callable[[TracebackEvent], None],
    *,
    poll_interval_seconds: float = 0.5,
    start_at_end: bool = True,
) -> None:
    """Continuously watch a log file and call ``callback`` for new tracebacks.

    This is a blocking loop intended for the later Agent runner.  Stop it with
    Ctrl+C during manual demos.
    """

    cursor = LogCursor.from_end(log_path) if start_at_end else LogCursor.from_start(log_path)
    buffer = ""
    seen_raw_blocks: set[str] = set()

    while True:
        new_text = cursor.read_new_text()
        if new_text:
            buffer += new_text
            # Keep memory bounded for long-running demos.
            if len(buffer) > 200_000:
                buffer = buffer[-100_000:]

            for event in parse_tracebacks(buffer, source_path=str(log_path)):
                if event.raw not in seen_raw_blocks:
                    seen_raw_blocks.add(event.raw)
                    callback(event)
        time.sleep(poll_interval_seconds)

