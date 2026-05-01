from __future__ import annotations

from pathlib import Path
from typing import Sequence

from service_recovery_agent.git_diff_correlation import (
    FOUND,
    NOT_FOUND,
    UNAVAILABLE,
    GitCommandError,
    correlate_traceback_with_recent_diff,
    format_git_diff_correlation_evidence,
    parse_unified_diff_hunks,
)
from service_recovery_agent.traceback_parser import StackFrame, TracebackEvent


class FakeGitRunner:
    def __init__(self, *, log_output: str, diffs: dict[str, str]) -> None:
        self.log_output = log_output
        self.diffs = diffs
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], root: Path, timeout_seconds: float | None) -> str:
        del root, timeout_seconds
        self.calls.append(list(args))
        assert args[0] in {"log", "show"}
        assert not any(token in {"commit", "push", "branch", "checkout", "switch", "pr"} for token in args)
        if args[0] == "log":
            return self.log_output
        sha = args[-1]
        return self.diffs.get(sha, "")


def _event(
    *,
    file: str = "demo_service/app.py",
    line: int = 31,
    function: str = "unsafe_divide",
    code: str = "return numerator / denominator",
    extra_frame_file: str | None = None,
) -> TracebackEvent:
    frames = []
    if extra_frame_file is not None:
        frames.append(StackFrame(file=extra_frame_file, line_number=74, function="divide", code="unsafe_divide(...)"))
    frames.append(StackFrame(file=file, line_number=line, function=function, code=code))
    return TracebackEvent(
        exception_type="ZeroDivisionError",
        exception_message="float division by zero",
        frames=frames,
        raw="Traceback (most recent call last):\n...",
        source_path="logs/app.log",
    )


def test_correlate_traceback_with_recent_diff_scores_crash_file_line_and_function(tmp_path: Path) -> None:
    sha = "abc1234567890"
    runner = FakeGitRunner(
        log_output=f"{sha}\x1fabc1234\x1f1760000000\x1fintroduce divide bug\n",
        diffs={
            sha: "\n".join(
                [
                    "diff --git a/demo_service/app.py b/demo_service/app.py",
                    "index 1111111..2222222 100644",
                    "--- a/demo_service/app.py",
                    "+++ b/demo_service/app.py",
                    "@@ -28,7 +28,9 @@ def unsafe_divide(numerator: float, denominator: float) -> float:",
                    "     # Intentional bug for the recovery demo:",
                    "     # denominator == 0 currently raises ZeroDivisionError.",
                    "-    return 0.0",
                    "+    return numerator / denominator",
                ]
            )
        },
    )

    result = correlate_traceback_with_recent_diff(
        _event(),
        project_root=tmp_path,
        max_commits=1,
        runner=runner,
    )

    assert result.status == FOUND
    assert result.crash_file == "demo_service/app.py"
    assert result.crash_line_number == 31
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.commit_sha == sha
    assert candidate.short_sha == "abc1234"
    assert candidate.file == "demo_service/app.py"
    assert candidate.score == 0.90
    assert "crash file" in candidate.reason
    assert "crash line 31" in candidate.reason
    assert "unsafe_divide" in candidate.reason
    assert candidate.hunks[0].contains_new_line(31)
    assert result.to_dict()["candidates"][0]["score"] == 0.90
    assert [call[0] for call in runner.calls] == ["log", "show"]


def test_correlate_traceback_with_recent_diff_adds_other_traceback_frame_bonus(tmp_path: Path) -> None:
    sha = "def4567890123"
    runner = FakeGitRunner(
        log_output=f"{sha}\x1fdef4567\x1f1760000001\x1fchange route and helper\n",
        diffs={
            sha: "\n".join(
                [
                    "diff --git a/demo_service/routes.py b/demo_service/routes.py",
                    "--- a/demo_service/routes.py",
                    "+++ b/demo_service/routes.py",
                    "@@ -70,4 +70,4 @@ def divide():",
                    "-    return unsafe_divide(100.0, x)",
                    "+    return unsafe_divide(100.0, x)",
                    "diff --git a/demo_service/app.py b/demo_service/app.py",
                    "--- a/demo_service/app.py",
                    "+++ b/demo_service/app.py",
                    "@@ -28,7 +28,9 @@ def unsafe_divide(numerator: float, denominator: float) -> float:",
                    "+    return numerator / denominator",
                ]
            )
        },
    )

    result = correlate_traceback_with_recent_diff(
        _event(extra_frame_file="demo_service/routes.py"),
        project_root=tmp_path,
        max_commits=1,
        runner=runner,
    )

    assert result.status == FOUND
    assert result.candidates[0].score == 1.0
    assert "other traceback frame" in result.candidates[0].reason
    assert "demo_service/routes.py" in result.changed_files


def test_correlate_traceback_with_recent_diff_returns_not_found_when_crash_file_absent(tmp_path: Path) -> None:
    sha = "abc1234567890"
    runner = FakeGitRunner(
        log_output=f"{sha}\x1fabc1234\x1f1760000000\x1fchange docs\n",
        diffs={
            sha: "\n".join(
                [
                    "diff --git a/README.md b/README.md",
                    "--- a/README.md",
                    "+++ b/README.md",
                    "@@ -1,1 +1,2 @@",
                    " # README",
                    "+extra",
                ]
            )
        },
    )

    result = correlate_traceback_with_recent_diff(
        _event(),
        project_root=tmp_path,
        max_commits=1,
        runner=runner,
    )

    assert result.status == NOT_FOUND
    assert result.candidates == []
    assert "No recent diff" in result.summary
    assert result.changed_files == ["README.md"]


def test_correlate_traceback_with_recent_diff_returns_unavailable_when_git_fails(tmp_path: Path) -> None:
    def failing_runner(args: Sequence[str], root: Path, timeout_seconds: float | None) -> str:
        del args, root, timeout_seconds
        raise GitCommandError("not a git repository")

    result = correlate_traceback_with_recent_diff(
        _event(),
        project_root=tmp_path,
        runner=failing_runner,
    )

    assert result.status == UNAVAILABLE
    assert result.candidates == []
    assert "not a git repository" in result.summary


def test_parse_unified_diff_hunks_and_format_evidence_are_deterministic(tmp_path: Path) -> None:
    sha = "feedface1234567"
    diff_text = "\n".join(
        [
            "diff --git a/demo_service/app.py b/demo_service/app.py",
            "--- a/demo_service/app.py",
            "+++ b/demo_service/app.py",
            "@@ -28,2 +30,3 @@ def unsafe_divide(numerator, denominator):",
            " context",
            "+    return numerator / denominator",
        ]
    )

    hunks = parse_unified_diff_hunks(diff_text, commit_sha=sha)

    assert len(hunks) == 1
    assert hunks[0].commit_sha == sha
    assert hunks[0].file == "demo_service/app.py"
    assert hunks[0].new_start == 30
    assert hunks[0].new_count == 3
    assert hunks[0].contains_new_line(31)

    runner = FakeGitRunner(
        log_output=f"{sha}\x1ffeedfac\x1f1760000002\x1fchange unsafe divide\n",
        diffs={sha: diff_text},
    )
    result = correlate_traceback_with_recent_diff(
        _event(line=31),
        project_root=tmp_path,
        runner=runner,
    )
    evidence = format_git_diff_correlation_evidence(result)

    assert "# Git Diff Correlation Evidence" in evidence
    assert "Status: FOUND" in evidence
    assert "score=0.90" in evidence
    assert "Hunk:" in evidence
