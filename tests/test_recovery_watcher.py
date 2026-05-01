from __future__ import annotations

from pathlib import Path

from service_recovery_agent.recovery_agent import RecoveryRunResult
from service_recovery_agent.recovery_watcher import (
    PROCESSED,
    SKIPPED_COOLDOWN,
    RecoveryWatchConfig,
    fingerprint_traceback,
    watch_recovery_agent,
)
from service_recovery_agent.traceback_parser import StackFrame, TracebackEvent


class FakeConfig:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path

    def resolved_log_path(self) -> Path:
        return self.log_path


class FakeRecoveryAgent:
    def __init__(self, log_path: Path) -> None:
        self.config = FakeConfig(log_path)
        self.calls: list[dict[str, object]] = []

    def run_once(self, **kwargs: object) -> RecoveryRunResult:
        self.calls.append(kwargs)
        return RecoveryRunResult(
            status="PREVIEW_ONLY",
            reason="fake preview-only recovery result",
            rolled_back=False,
            attempt_number=len(self.calls),
            max_repair_attempts=1,
        )


def _event(
    *,
    file: str = "demo_service/app.py",
    line: int = 31,
    function: str = "unsafe_divide",
    code: str = "return numerator / denominator",
    raw: str = "raw traceback block",
) -> TracebackEvent:
    return TracebackEvent(
        exception_type="ZeroDivisionError",
        exception_message="float division by zero",
        frames=[StackFrame(file=file, line_number=line, function=function, code=code)],
        raw=raw,
        source_path="logs/app.log",
    )


def test_watch_recovery_agent_calls_run_once_preview_only_by_default(tmp_path: Path) -> None:
    agent = FakeRecoveryAgent(tmp_path / "app.log")
    observed: list[str] = []

    result = watch_recovery_agent(
        agent,
        config=RecoveryWatchConfig(max_events=1),
        event_source=[_event()],
        on_event=lambda event: observed.append(event.status),
    )

    assert result.stopped_reason == "max_events_reached"
    assert result.processed_count == 1
    assert result.skipped_count == 0
    assert observed == [PROCESSED]
    assert len(agent.calls) == 1
    assert agent.calls[0]["apply"] is False
    assert agent.calls[0]["wait_seconds"] == 0.0
    assert agent.calls[0]["include_existing"] is True
    assert result.events[0].result is not None
    assert result.events[0].result.status == "PREVIEW_ONLY"


def test_watch_recovery_agent_passes_explicit_run_once_kwargs_without_enabling_apply_by_default(tmp_path: Path) -> None:
    agent = FakeRecoveryAgent(tmp_path / "app.log")

    watch_recovery_agent(
        agent,
        config=RecoveryWatchConfig(
            max_events=1,
            run_once_kwargs={
                "run_adversarial_validation": True,
                "validation_timeout_seconds": 3.0,
            },
        ),
        event_source=[_event()],
    )

    assert len(agent.calls) == 1
    assert agent.calls[0]["apply"] is False
    assert agent.calls[0]["run_adversarial_validation"] is True
    assert agent.calls[0]["validation_timeout_seconds"] == 3.0


def test_watch_recovery_agent_skips_same_fingerprint_inside_cooldown_and_processes_after(tmp_path: Path) -> None:
    agent = FakeRecoveryAgent(tmp_path / "app.log")
    event = _event(raw="first raw block")
    same_logical_event = _event(raw="second raw block with different timestamp")
    times = iter([0.0, 10.0, 70.0])

    result = watch_recovery_agent(
        agent,
        config=RecoveryWatchConfig(max_events=2, cooldown_seconds=60.0),
        event_source=[event, same_logical_event, same_logical_event],
        clock=lambda: next(times),
    )

    assert fingerprint_traceback(event) == fingerprint_traceback(same_logical_event)
    assert result.stopped_reason == "max_events_reached"
    assert [watch_event.status for watch_event in result.events] == [
        PROCESSED,
        SKIPPED_COOLDOWN,
        PROCESSED,
    ]
    assert result.processed_count == 2
    assert result.skipped_count == 1
    assert len(agent.calls) == 2
    assert "cooldown" in result.events[1].reason
    assert result.events[1].last_processed_at == 0.0


def test_watch_recovery_agent_max_events_stops_after_processed_count(tmp_path: Path) -> None:
    agent = FakeRecoveryAgent(tmp_path / "app.log")
    events = [
        _event(file="demo_service/app.py", line=31),
        _event(file="demo_service/app.py", line=74, function="divide", code="result = unsafe_divide(...)"),
        _event(file="demo_service/other.py", line=10, function="other", code="raise ZeroDivisionError"),
    ]

    result = watch_recovery_agent(
        agent,
        config=RecoveryWatchConfig(max_events=2, cooldown_seconds=60.0),
        event_source=events,
    )

    assert result.stopped_reason == "max_events_reached"
    assert result.processed_count == 2
    assert len(result.events) == 2
    assert len(agent.calls) == 2


def test_fingerprint_traceback_is_stable_for_same_crash_site_and_differs_for_different_site() -> None:
    first = _event(raw="2026-01-01 Traceback block")
    second = _event(raw="2026-01-02 Same logical traceback block")
    different = _event(line=32, raw="different line")

    assert fingerprint_traceback(first) == fingerprint_traceback(second)
    assert fingerprint_traceback(first) != fingerprint_traceback(different)


def test_watch_recovery_agent_can_poll_existing_log_and_stop_at_max_events(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "Traceback (most recent call last):\n"
        "  File \"demo_service/app.py\", line 31, in unsafe_divide\n"
        "    return numerator / denominator\n"
        "ZeroDivisionError: float division by zero\n",
        encoding="utf-8",
    )
    agent = FakeRecoveryAgent(log_path)

    result = watch_recovery_agent(
        agent,
        config=RecoveryWatchConfig(
            max_events=1,
            start_at_end=False,
            poll_interval_seconds=0.0,
            idle_timeout_seconds=1.0,
        ),
        sleep=lambda seconds: None,
    )

    assert result.stopped_reason == "max_events_reached"
    assert result.processed_count == 1
    assert len(agent.calls) == 1
    assert result.events[0].traceback_event.exception_type == "ZeroDivisionError"
    assert result.events[0].traceback_event.crash_frame is not None
    assert result.events[0].traceback_event.crash_frame.line_number == 31
