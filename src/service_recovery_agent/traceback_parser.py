"""Parse Python traceback text into structured records."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable


TRACEBACK_MARKER = "Traceback (most recent call last):"
FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+), in ([^\n]+)\s*$')
EXCEPTION_RE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s*(.*))?$")
LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


@dataclass(frozen=True)
class StackFrame:
    file: str
    line_number: int
    function: str
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TracebackEvent:
    exception_type: str
    exception_message: str
    frames: list[StackFrame]
    raw: str
    source_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    @property
    def crash_frame(self) -> StackFrame | None:
        """Return the innermost stack frame, which is usually the crash site."""

        return self.frames[-1] if self.frames else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["frames"] = [frame.to_dict() for frame in self.frames]
        data["crash_frame"] = self.crash_frame.to_dict() if self.crash_frame else None
        return data


def parse_latest_traceback(
    text: str,
    *,
    source_path: str | None = None,
) -> TracebackEvent | None:
    """Return the latest traceback in ``text``."""

    events = parse_tracebacks(text, source_path=source_path)
    return events[-1] if events else None


def parse_tracebacks(
    text: str,
    *,
    source_path: str | None = None,
) -> list[TracebackEvent]:
    """Parse all Python tracebacks contained in ``text``."""

    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if TRACEBACK_MARKER in line]
    events: list[TracebackEvent] = []

    for position, start in enumerate(starts):
        next_start = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block_lines = _trim_block(lines[start:next_start])
        if not block_lines:
            continue

        frames = _parse_frames(block_lines)
        exception_type, exception_message = _parse_exception(block_lines)
        if not exception_type:
            continue

        end_line = start + len(block_lines)
        events.append(
            TracebackEvent(
                exception_type=exception_type,
                exception_message=exception_message,
                frames=frames,
                raw="\n".join(block_lines),
                source_path=source_path,
                start_line=start + 1,
                end_line=end_line,
            )
        )

    return events


def summarize_traceback(event: TracebackEvent) -> str:
    """Create a compact, human-readable traceback summary."""

    crash_frame = event.crash_frame
    if crash_frame is None:
        return f"{event.exception_type}: {event.exception_message}"
    return (
        f"{event.exception_type}: {event.exception_message} "
        f"at {crash_frame.file}:{crash_frame.line_number} in {crash_frame.function}"
    )


def _parse_frames(lines: Iterable[str]) -> list[StackFrame]:
    materialized = list(lines)
    frames: list[StackFrame] = []

    for index, line in enumerate(materialized):
        match = FRAME_RE.match(line)
        if not match:
            continue

        code: str | None = None
        if index + 1 < len(materialized):
            next_line = materialized[index + 1]
            if next_line.strip() and not FRAME_RE.match(next_line) and not _looks_like_exception(next_line):
                code = next_line.strip()

        frames.append(
            StackFrame(
                file=match.group(1),
                line_number=int(match.group(2)),
                function=match.group(3).strip(),
                code=code,
            )
        )

    return frames


def _parse_exception(lines: Iterable[str]) -> tuple[str | None, str]:
    for line in reversed(list(lines)):
        stripped = line.strip()
        if not stripped or stripped == TRACEBACK_MARKER:
            continue
        if FRAME_RE.match(stripped) or LOG_PREFIX_RE.match(stripped):
            continue

        if _looks_like_exception(stripped):
            match = EXCEPTION_RE.match(stripped)
            if match:
                return match.group(1), match.group(2) or ""

    return None, ""


def _trim_block(lines: list[str]) -> list[str]:
    """Drop unrelated log lines after the exception line when possible."""

    trimmed: list[str] = []
    found_marker = False
    found_exception = False

    for line in lines:
        if TRACEBACK_MARKER in line:
            found_marker = True

        if found_exception and LOG_PREFIX_RE.match(line):
            break

        trimmed.append(line)

        if found_marker and _looks_like_exception(line):
            found_exception = True

    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


def _looks_like_exception(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped == TRACEBACK_MARKER:
        return False
    if FRAME_RE.match(stripped) or LOG_PREFIX_RE.match(stripped):
        return False
    match = EXCEPTION_RE.match(stripped)
    if not match:
        return False

    exception_name = match.group(1)
    return "." in exception_name or exception_name.endswith(
        (
            "Error",
            "Exception",
            "Warning",
            "Interrupt",
            "Exit",
            "Fault",
            "Failure",
        )
    )
