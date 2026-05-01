"""Correlate traceback crash sites with recent Git diffs.

This module is intentionally read-only.  It never creates branches, commits,
pushes, or pull requests.  The first implementation only inspects recent Git
history/diffs and turns them into deterministic evidence that can later be
injected into ``FaultDiagnosis`` or ``FixPlanner`` prompts.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .traceback_parser import TracebackEvent


FOUND = "FOUND"
NOT_FOUND = "NOT_FOUND"
UNAVAILABLE = "UNAVAILABLE"

GIT_READ_ONLY_SUBCOMMANDS = {"log", "show"}
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
DIFF_HEADER_RE = re.compile(r"^diff --git (.+?) (.+)$")


class GitCommandError(RuntimeError):
    """Raised when a read-only Git command cannot be executed successfully."""


GitRunner = Callable[[Sequence[str], Path, float | None], str]


@dataclass(frozen=True)
class RecentCommit:
    """Metadata for one inspected commit."""

    sha: str
    short_sha: str
    timestamp: int | None
    subject: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiffHunk:
    """A unified-diff hunk for one changed file in one commit."""

    commit_sha: str
    file: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str]

    @property
    def new_end(self) -> int:
        if self.new_count <= 0:
            return self.new_start
        return self.new_start + self.new_count - 1

    def contains_new_line(self, line_number: int) -> bool:
        if self.new_count <= 0:
            return line_number == self.new_start
        return self.new_start <= line_number <= self.new_end

    def text_for_matching(self) -> str:
        return "\n".join([self.header, *self.lines])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiffCorrelationCandidate:
    """One recent commit/file candidate correlated with the crash site."""

    file: str
    commit_sha: str
    score: float
    reason: str
    hunks: list[DiffHunk]
    commit_subject: str = ""
    short_sha: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "commit_sha": self.commit_sha,
            "short_sha": self.short_sha,
            "commit_subject": self.commit_subject,
            "score": self.score,
            "reason": self.reason,
            "evidence": self.evidence,
            "hunks": [hunk.to_dict() for hunk in self.hunks],
        }


@dataclass(frozen=True)
class GitDiffCorrelationResult:
    """Deterministic evidence from recent Git diffs."""

    status: str
    summary: str
    candidates: list[DiffCorrelationCandidate]
    crash_file: str | None = None
    crash_line_number: int | None = None
    crash_function: str | None = None
    inspected_commits: list[RecentCommit] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "crash_file": self.crash_file,
            "crash_line_number": self.crash_line_number,
            "crash_function": self.crash_function,
            "changed_files": self.changed_files,
            "inspected_commits": [commit.to_dict() for commit in self.inspected_commits],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class _TracebackTarget:
    event: TracebackEvent
    crash_file: str
    crash_line_number: int
    crash_function: str | None
    other_frame_files: list[str]


@dataclass(frozen=True)
class _ParsedCommitDiff:
    commit: RecentCommit
    changed_files: list[str]
    hunks: list[DiffHunk]


@dataclass(frozen=True)
class _Score:
    value: float
    evidence: list[str]


def correlate_traceback_with_recent_diff(
    target: Any,
    *,
    project_root: str | Path = ".",
    max_commits: int = 5,
    timeout_seconds: float | None = 10.0,
    runner: GitRunner | None = None,
) -> GitDiffCorrelationResult:
    """Correlate a traceback or ``CodeContext`` with recent Git diffs.

    Args:
        target: Either a ``TracebackEvent`` or a ``CodeContext``-like object with
            ``traceback``, ``crash_file_relative``, ``crash_line_number`` and
            optional ``crash_function`` attributes.
        project_root: Repository root to inspect.
        max_commits: Number of recent commits to inspect.
        timeout_seconds: Timeout for each read-only Git command.
        runner: Optional injectable Git runner for tests.  It receives the Git
            args *after* ``git -C <root>`` and must return stdout.

    Returns:
        ``GitDiffCorrelationResult`` with FOUND / NOT_FOUND / UNAVAILABLE.
    """

    root = Path(project_root).resolve()
    traceback_target = _extract_traceback_target(target, root)

    if max_commits <= 0:
        return GitDiffCorrelationResult(
            status=NOT_FOUND,
            summary="Git diff correlation skipped because max_commits <= 0.",
            candidates=[],
            crash_file=traceback_target.crash_file,
            crash_line_number=traceback_target.crash_line_number,
            crash_function=traceback_target.crash_function,
        )

    run = runner or _run_git_read_only

    try:
        commits = _list_recent_commits(root, max_commits=max_commits, timeout_seconds=timeout_seconds, runner=run)
        if not commits:
            return GitDiffCorrelationResult(
                status=UNAVAILABLE,
                summary="Git history is unavailable or contains no commits to inspect.",
                candidates=[],
                crash_file=traceback_target.crash_file,
                crash_line_number=traceback_target.crash_line_number,
                crash_function=traceback_target.crash_function,
            )

        parsed_diffs = [
            _show_commit_diff(commit, root, timeout_seconds=timeout_seconds, runner=run)
            for commit in commits
        ]
    except GitCommandError as exc:
        return GitDiffCorrelationResult(
            status=UNAVAILABLE,
            summary=f"Git diff correlation unavailable: {exc}",
            candidates=[],
            crash_file=traceback_target.crash_file,
            crash_line_number=traceback_target.crash_line_number,
            crash_function=traceback_target.crash_function,
        )

    candidates: list[DiffCorrelationCandidate] = []
    changed_files = _unique_preserve_order(
        file for parsed in parsed_diffs for file in parsed.changed_files
    )

    for parsed in parsed_diffs:
        if traceback_target.crash_file not in parsed.changed_files:
            continue

        file_hunks = [hunk for hunk in parsed.hunks if hunk.file == traceback_target.crash_file]
        score = _score_candidate(
            traceback_target=traceback_target,
            commit_changed_files=parsed.changed_files,
            hunks=file_hunks,
        )
        candidates.append(
            DiffCorrelationCandidate(
                file=traceback_target.crash_file,
                commit_sha=parsed.commit.sha,
                short_sha=parsed.commit.short_sha,
                commit_subject=parsed.commit.subject,
                score=score.value,
                reason="; ".join(score.evidence),
                evidence=score.evidence,
                hunks=file_hunks,
            )
        )

    candidates = sorted(
        candidates,
        key=lambda candidate: (-candidate.score, _commit_index(commits, candidate.commit_sha), candidate.file),
    )

    if not candidates:
        return GitDiffCorrelationResult(
            status=NOT_FOUND,
            summary=(
                f"No recent diff among the last {len(commits)} commit(s) modified "
                f"the crash file {traceback_target.crash_file}."
            ),
            candidates=[],
            crash_file=traceback_target.crash_file,
            crash_line_number=traceback_target.crash_line_number,
            crash_function=traceback_target.crash_function,
            inspected_commits=commits,
            changed_files=changed_files,
        )

    top = candidates[0]
    return GitDiffCorrelationResult(
        status=FOUND,
        summary=(
            f"Recent diff correlation found {len(candidates)} candidate(s); "
            f"top candidate {top.short_sha or top.commit_sha[:7]} modified "
            f"{top.file} with score {top.score:.2f}."
        ),
        candidates=candidates,
        crash_file=traceback_target.crash_file,
        crash_line_number=traceback_target.crash_line_number,
        crash_function=traceback_target.crash_function,
        inspected_commits=commits,
        changed_files=changed_files,
    )


def format_git_diff_correlation_evidence(result: GitDiffCorrelationResult) -> str:
    """Render a compact prompt-friendly evidence block."""

    lines = ["# Git Diff Correlation Evidence", "", f"- Status: {result.status}", f"- Summary: {result.summary}"]
    if result.crash_file:
        crash_site = result.crash_file
        if result.crash_line_number is not None:
            crash_site += f":{result.crash_line_number}"
        if result.crash_function:
            crash_site += f" in {result.crash_function}"
        lines.append(f"- Crash site: {crash_site}")

    if not result.candidates:
        return "\n".join(lines)

    lines.append("")
    lines.append("## Candidates")
    for candidate in result.candidates[:5]:
        label = candidate.short_sha or candidate.commit_sha[:7]
        lines.append(f"- {label} `{candidate.file}` score={candidate.score:.2f}")
        if candidate.commit_subject:
            lines.append(f"  - Commit: {candidate.commit_subject}")
        for evidence in candidate.evidence:
            lines.append(f"  - Evidence: {evidence}")
        for hunk in candidate.hunks[:3]:
            lines.append(
                f"  - Hunk: {hunk.header} new_range={hunk.new_start}-{hunk.new_end}"
            )
    return "\n".join(lines)


def parse_unified_diff_hunks(diff_text: str, *, commit_sha: str = "") -> list[DiffHunk]:
    """Parse unified diff hunks from ``git show --format=`` output."""

    return _parse_commit_diff_text(RecentCommit(commit_sha, commit_sha[:7], None, ""), diff_text).hunks


def _extract_traceback_target(target: Any, root: Path) -> _TracebackTarget:
    if isinstance(target, TracebackEvent):
        event = target
        crash_frame = event.crash_frame
        if crash_frame is None:
            raise ValueError("TracebackEvent does not contain any stack frames.")
        crash_file = _normalize_repo_path(crash_frame.file, root)
        crash_line_number = crash_frame.line_number
        crash_function = crash_frame.function
    else:
        event = getattr(target, "traceback", None)
        if not isinstance(event, TracebackEvent):
            raise TypeError("target must be a TracebackEvent or CodeContext-like object with a traceback field.")
        crash_file = _normalize_repo_path(getattr(target, "crash_file_relative", ""), root)
        crash_line_number = int(getattr(target, "crash_line_number"))
        crash_function_context = getattr(target, "crash_function", None)
        crash_function = getattr(crash_function_context, "name", None)
        if crash_function is None and event.crash_frame is not None:
            crash_function = event.crash_frame.function

    other_frame_files = []
    for frame in event.frames:
        normalized = _normalize_repo_path(frame.file, root)
        if normalized != crash_file:
            other_frame_files.append(normalized)

    return _TracebackTarget(
        event=event,
        crash_file=crash_file,
        crash_line_number=crash_line_number,
        crash_function=crash_function,
        other_frame_files=_unique_preserve_order(other_frame_files),
    )


def _list_recent_commits(
    root: Path,
    *,
    max_commits: int,
    timeout_seconds: float | None,
    runner: GitRunner,
) -> list[RecentCommit]:
    output = runner(
        ["log", f"-n{max_commits}", "--format=%H%x1f%h%x1f%ct%x1f%s"],
        root,
        timeout_seconds,
    )
    commits: list[RecentCommit] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", maxsplit=3)
        if len(parts) != 4:
            continue
        sha, short_sha, timestamp_text, subject = parts
        commits.append(
            RecentCommit(
                sha=sha,
                short_sha=short_sha,
                timestamp=_parse_int(timestamp_text),
                subject=subject,
            )
        )
    return commits


def _show_commit_diff(
    commit: RecentCommit,
    root: Path,
    *,
    timeout_seconds: float | None,
    runner: GitRunner,
) -> _ParsedCommitDiff:
    diff_text = runner(
        ["show", "--format=", "--unified=3", "--no-ext-diff", commit.sha],
        root,
        timeout_seconds,
    )
    return _parse_commit_diff_text(commit, diff_text)


def _parse_commit_diff_text(commit: RecentCommit, diff_text: str) -> _ParsedCommitDiff:
    changed_files: list[str] = []
    hunks: list[DiffHunk] = []
    current_file: str | None = None
    current_hunk: DiffHunk | None = None
    current_lines: list[str] = []

    def flush_hunk() -> None:
        nonlocal current_hunk, current_lines
        if current_hunk is None:
            return
        hunks.append(
            DiffHunk(
                commit_sha=current_hunk.commit_sha,
                file=current_hunk.file,
                old_start=current_hunk.old_start,
                old_count=current_hunk.old_count,
                new_start=current_hunk.new_start,
                new_count=current_hunk.new_count,
                header=current_hunk.header,
                lines=list(current_lines),
            )
        )
        current_hunk = None
        current_lines = []

    for raw_line in diff_text.splitlines():
        diff_match = DIFF_HEADER_RE.match(raw_line)
        if diff_match:
            flush_hunk()
            current_file = _strip_diff_path(diff_match.group(2)) or _strip_diff_path(diff_match.group(1))
            if current_file:
                changed_files.append(current_file)
            continue

        if raw_line.startswith("+++ "):
            next_file = _strip_diff_path(raw_line[4:].strip())
            if next_file is not None:
                current_file = next_file
                changed_files.append(current_file)
            continue

        hunk_match = HUNK_RE.match(raw_line)
        if hunk_match:
            flush_hunk()
            if current_file is None:
                continue
            old_start, old_count_text, new_start, new_count_text, _scope = hunk_match.groups()
            current_hunk = DiffHunk(
                commit_sha=commit.sha,
                file=current_file,
                old_start=int(old_start),
                old_count=int(old_count_text or "1"),
                new_start=int(new_start),
                new_count=int(new_count_text or "1"),
                header=raw_line,
                lines=[],
            )
            current_lines = []
            continue

        if current_hunk is not None:
            current_lines.append(raw_line)

    flush_hunk()
    return _ParsedCommitDiff(
        commit=commit,
        changed_files=_unique_preserve_order(changed_files),
        hunks=hunks,
    )


def _score_candidate(
    *,
    traceback_target: _TracebackTarget,
    commit_changed_files: list[str],
    hunks: list[DiffHunk],
) -> _Score:
    score = 0.60
    evidence = [f"crash file `{traceback_target.crash_file}` was modified in the recent diff"]

    overlapping_hunks = [
        hunk for hunk in hunks
        if hunk.contains_new_line(traceback_target.crash_line_number)
    ]
    if overlapping_hunks:
        score += 0.20
        evidence.append(
            f"crash line {traceback_target.crash_line_number} overlaps {len(overlapping_hunks)} diff hunk(s)"
        )

    if traceback_target.crash_function and any(
        traceback_target.crash_function in hunk.text_for_matching()
        for hunk in hunks
    ):
        score += 0.10
        evidence.append(f"crash function `{traceback_target.crash_function}` appears in diff hunk text")

    other_modified_frames = [
        file for file in traceback_target.other_frame_files
        if file in commit_changed_files
    ]
    if other_modified_frames:
        score += 0.10
        evidence.append(
            "other traceback frame file(s) also modified: " + ", ".join(other_modified_frames)
        )

    return _Score(value=round(min(1.0, score), 2), evidence=evidence)


def _run_git_read_only(args: Sequence[str], root: Path, timeout_seconds: float | None) -> str:
    if not args:
        raise GitCommandError("empty git command")
    subcommand = args[0]
    if subcommand not in GIT_READ_ONLY_SUBCOMMANDS:
        raise GitCommandError(f"refusing non-read-only git subcommand: {subcommand}")

    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or f"git {subcommand} failed"
        raise GitCommandError(stderr)
    return completed.stdout


def _normalize_repo_path(path: str | Path, root: Path) -> str:
    raw = str(path).replace("\\", "/")
    if not raw:
        return raw
    path_obj = Path(raw)
    if path_obj.is_absolute():
        try:
            return path_obj.resolve().relative_to(root).as_posix()
        except ValueError:
            return path_obj.as_posix().lstrip("/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("a/") or raw.startswith("b/"):
        raw = raw[2:]
    return raw


def _strip_diff_path(path: str) -> str | None:
    cleaned = path.strip()
    if cleaned == "/dev/null":
        return None
    if cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]
    if cleaned.startswith("a/") or cleaned.startswith("b/"):
        cleaned = cleaned[2:]
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.replace("\\", "/")


def _commit_index(commits: Sequence[RecentCommit], sha: str) -> int:
    for index, commit in enumerate(commits):
        if commit.sha == sha:
            return index
    return len(commits)


def _unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
