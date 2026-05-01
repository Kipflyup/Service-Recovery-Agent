"""Watch-mode orchestration for ``RecoveryAgent``.

The first watch-mode implementation is deliberately conservative:

- it calls ``RecoveryAgent.run_once`` when a new traceback is observed;
- it defaults to preview-only (``apply=False``);
- it deduplicates events by a stable traceback fingerprint;
- it throttles repeated fingerprints with a cooldown window;
- it supports ``max_events`` so tests and local demos can stop automatically;
- it does not perform Git operations, create PRs, or send Feishu messages.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .log_watcher import LogCursor
from .traceback_parser import TracebackEvent, parse_tracebacks


PROCESSED = "PROCESSED"
SKIPPED_COOLDOWN = "SKIPPED_COOLDOWN"


@dataclass(frozen=True)
class RecoveryWatchConfig:
    """Configuration for a bounded or continuous recovery watch loop."""

    poll_interval_seconds: float = 0.5
    start_at_end: bool = True
    cooldown_seconds: float = 60.0
    max_events: int | None = None
    idle_timeout_seconds: float | None = None
    apply: bool = False
    run_once_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryWatchEvent:
    """One observed traceback and the watcher's action for it."""

    fingerprint: str
    status: str
    reason: str
    traceback_event: TracebackEvent
    result: Any | None = None
    observed_at: float = 0.0
    last_processed_at: float | None = None

    @property
    def processed(self) -> bool:
        return self.status == PROCESSED

    def to_dict(self) -> dict[str, Any]:
        result_to_dict = getattr(self.result, "to_dict", None)
        return {
            "fingerprint": self.fingerprint,
            "status": self.status,
            "reason": self.reason,
            "processed": self.processed,
            "observed_at": self.observed_at,
            "last_processed_at": self.last_processed_at,
            "traceback_event": self.traceback_event.to_dict(),
            "result": result_to_dict() if callable(result_to_dict) else self.result,
        }


@dataclass(frozen=True)
class RecoveryWatchResult:
    """Summary returned when a bounded watch loop exits."""

    events: list[RecoveryWatchEvent]
    stopped_reason: str

    @property
    def processed_count(self) -> int:
        return sum(1 for event in self.events if event.processed)

    @property
    def skipped_count(self) -> int:
        return sum(1 for event in self.events if not event.processed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped_reason": self.stopped_reason,
            "processed_count": self.processed_count,
            "skipped_count": self.skipped_count,
            "events": [event.to_dict() for event in self.events],
        }


def watch_recovery_agent(
    agent: Any,
    *,
    config: RecoveryWatchConfig | None = None,
    event_source: Iterable[TracebackEvent] | None = None,
    on_event: Callable[[RecoveryWatchEvent], None] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RecoveryWatchResult:
    """Watch for traceback events and call ``agent.run_once`` for new ones.

    Args:
        agent: A ``RecoveryAgent``-like object.  It must provide ``run_once``;
            if ``event_source`` is not supplied, ``agent.config.resolved_log_path``
            is used to locate the log file.
        config: Watch-loop settings.  Defaults are safe for manual demos:
            preview-only, start at end, and cooldown repeated fingerprints.
        event_source: Optional iterable of ``TracebackEvent`` objects for tests
            or custom watchers.  When omitted, this function polls the log file.
        on_event: Optional callback invoked for processed and skipped events.
        clock/sleep: Injectable time functions for deterministic tests.
    """

    watch_config = config or RecoveryWatchConfig()
    now = clock or time.monotonic
    do_sleep = sleep or time.sleep
    events: list[RecoveryWatchEvent] = []
    last_processed_by_fingerprint: dict[str, float] = {}
    processed_count = 0
    stopped_reason = "event_source_exhausted"

    source = event_source
    if source is None:
        source = _iter_tracebacks_from_agent_log(
            agent,
            poll_interval_seconds=watch_config.poll_interval_seconds,
            start_at_end=watch_config.start_at_end,
            idle_timeout_seconds=watch_config.idle_timeout_seconds,
            clock=now,
            sleep=do_sleep,
        )

    for traceback_event in source:
        observed_at = now()
        fingerprint = fingerprint_traceback(traceback_event)
        last_processed_at = last_processed_by_fingerprint.get(fingerprint)
        if _within_cooldown(
            observed_at,
            last_processed_at=last_processed_at,
            cooldown_seconds=watch_config.cooldown_seconds,
        ):
            watch_event = RecoveryWatchEvent(
                fingerprint=fingerprint,
                status=SKIPPED_COOLDOWN,
                reason="Traceback fingerprint was already processed within the cooldown window.",
                traceback_event=traceback_event,
                result=None,
                observed_at=observed_at,
                last_processed_at=last_processed_at,
            )
            events.append(watch_event)
            if on_event is not None:
                on_event(watch_event)
            continue

        run_result = agent.run_once(**_run_once_kwargs(watch_config))
        last_processed_by_fingerprint[fingerprint] = observed_at
        processed_count += 1
        watch_event = RecoveryWatchEvent(
            fingerprint=fingerprint,
            status=PROCESSED,
            reason="RecoveryAgent.run_once was called for this traceback fingerprint.",
            traceback_event=traceback_event,
            result=run_result,
            observed_at=observed_at,
            last_processed_at=last_processed_at,
        )
        events.append(watch_event)
        if on_event is not None:
            on_event(watch_event)

        if watch_config.max_events is not None and processed_count >= watch_config.max_events:
            stopped_reason = "max_events_reached"
            break

    return RecoveryWatchResult(events=events, stopped_reason=stopped_reason)


def fingerprint_traceback(event: TracebackEvent) -> str:
    """Return a stable fingerprint for the logical traceback crash site.

    The fingerprint deliberately ignores raw log text and timestamps, so repeated
    occurrences of the same exception at the same crash frame can be throttled by
    cooldown even if the surrounding log lines differ.
    """

    crash_frame = event.crash_frame
    parts = [
        event.exception_type,
        event.exception_message,
        crash_frame.file if crash_frame else "",
        str(crash_frame.line_number) if crash_frame else "",
        crash_frame.function if crash_frame else "",
        (crash_frame.code or "") if crash_frame else "",
    ]
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _run_once_kwargs(config: RecoveryWatchConfig) -> dict[str, Any]:
    kwargs = dict(config.run_once_kwargs)
    kwargs.setdefault("wait_seconds", 0.0)
    kwargs.setdefault("include_existing", True)
    kwargs.setdefault("apply", config.apply)
    return kwargs


def _within_cooldown(
    observed_at: float,
    *,
    last_processed_at: float | None,
    cooldown_seconds: float,
) -> bool:
    if last_processed_at is None:
        return False
    cooldown = max(0.0, float(cooldown_seconds))
    return observed_at - last_processed_at < cooldown


def _iter_tracebacks_from_agent_log(
    agent: Any,
    *,
    poll_interval_seconds: float,
    start_at_end: bool,
    idle_timeout_seconds: float | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Iterator[TracebackEvent]:
    log_path = _agent_log_path(agent)
    cursor = LogCursor.from_end(log_path) if start_at_end else LogCursor.from_start(log_path)
    buffer = ""
    seen_raw_blocks: set[str] = set()
    idle_deadline = clock() + idle_timeout_seconds if idle_timeout_seconds is not None else None

    while True:
        new_text = cursor.read_new_text()
        if new_text:
            buffer += new_text
            if len(buffer) > 200_000:
                buffer = buffer[-100_000:]
            if idle_timeout_seconds is not None:
                idle_deadline = clock() + idle_timeout_seconds

            for event in parse_tracebacks(buffer, source_path=str(log_path)):
                if event.raw in seen_raw_blocks:
                    continue
                seen_raw_blocks.add(event.raw)
                yield event

        if idle_deadline is not None and clock() >= idle_deadline:
            return
        sleep(max(0.0, poll_interval_seconds))


def _agent_log_path(agent: Any) -> Path:
    config = getattr(agent, "config", None)
    resolver = getattr(config, "resolved_log_path", None)
    if callable(resolver):
        return Path(resolver())
    log_path = getattr(config, "log_path", None)
    if log_path is not None:
        return Path(log_path)
    raise ValueError("watch_recovery_agent requires agent.config.resolved_log_path() or an explicit event_source.")
