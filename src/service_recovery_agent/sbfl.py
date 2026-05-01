"""Spectrum-Based Fault Localization (SBFL) helpers.

The first implementation is intentionally local and conservative:

- it supports explicit pytest nodeids and pytest commands;
- it collects per-run executed source lines with Python's stdlib ``trace``
  module in a subprocess, so the parent process and current pytest session stay
  isolated;
- it computes Ochiai suspiciousness;
- it does not touch Git, create PRs, call LLMs, or send notifications;
- it is not wired into ``RecoveryAgent`` by default because it can be slow.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


PASS = "PASS"
PARTIAL = "PARTIAL"
UNAVAILABLE = "UNAVAILABLE"

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".trae",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "logs",
    "node_modules",
    "venv",
}


@dataclass(frozen=True)
class TestCoverageResult:
    """Coverage collected while running one pytest nodeid or command."""

    nodeid: str
    passed: bool
    covered_lines: dict[str, set[int]]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["covered_lines"] = {
            file: sorted(lines)
            for file, lines in sorted(self.covered_lines.items())
        }
        return data


@dataclass(frozen=True)
class SuspiciousLine:
    """Ochiai suspiciousness for one executed source line."""

    file: str
    line_number: int
    score: float
    failed_and_covered: int
    passed_and_covered: int
    total_failed: int
    source_line: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SBFLResult:
    """Spectrum-based fault localization output."""

    status: str
    summary: str
    suspicious_lines: list[SuspiciousLine]
    test_results: list[TestCoverageResult] = field(default_factory=list)
    total_failed: int = 0
    total_passed: int = 0

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "passed": self.passed,
            "total_failed": self.total_failed,
            "total_passed": self.total_passed,
            "suspicious_lines": [line.to_dict() for line in self.suspicious_lines],
            "test_results": [result.to_dict() for result in self.test_results],
        }


@dataclass(frozen=True)
class _PytestRunSpec:
    label: str
    args: list[str]
    command: list[str]


def run_sbfl(
    *,
    project_root: str | Path = ".",
    pytest_nodeids: Iterable[str] | None = None,
    commands: Iterable[str | Iterable[str]] | None = None,
    top_k: int = 10,
    timeout_seconds: float = 60.0,
    python_executable: str | Path | None = None,
    excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
    exclude_test_files: bool = True,
) -> SBFLResult:
    """Run SBFL for explicit pytest nodeids and/or pytest commands.

    ``commands`` must be pytest commands, for example ``pytest -q tests/x.py``
    or ``python -m pytest tests/x.py::test_name``.  Unsupported commands return
    ``UNAVAILABLE`` rather than being executed, because V1 only knows how to
    collect Python line spectra from pytest.
    """

    root = Path(project_root).resolve()
    try:
        specs = _build_pytest_run_specs(pytest_nodeids=pytest_nodeids, commands=commands)
    except ValueError as exc:
        return SBFLResult(status=UNAVAILABLE, summary=str(exc), suspicious_lines=[])

    if not specs:
        return SBFLResult(
            status=UNAVAILABLE,
            summary="SBFL requires at least one pytest nodeid or pytest command.",
            suspicious_lines=[],
        )

    interpreter = str(python_executable or sys.executable)
    test_results = [
        _run_pytest_spec_under_trace(
            spec,
            project_root=root,
            python_executable=interpreter,
            timeout_seconds=timeout_seconds,
            excluded_dirs=set(excluded_dirs),
            exclude_test_files=exclude_test_files,
        )
        for spec in specs
    ]

    total_failed = sum(1 for result in test_results if not result.passed)
    total_passed = sum(1 for result in test_results if result.passed)

    if total_failed == 0:
        return SBFLResult(
            status=PARTIAL,
            summary="No failing pytest run was observed, so SBFL cannot rank suspicious lines.",
            suspicious_lines=[],
            test_results=test_results,
            total_failed=total_failed,
            total_passed=total_passed,
        )

    suspicious_lines = _rank_suspicious_lines(
        test_results,
        project_root=root,
        total_failed=total_failed,
        top_k=max(0, int(top_k)),
    )
    if not suspicious_lines:
        return SBFLResult(
            status=PARTIAL,
            summary="Failing pytest run(s) were observed, but no project source coverage lines were collected.",
            suspicious_lines=[],
            test_results=test_results,
            total_failed=total_failed,
            total_passed=total_passed,
        )

    return SBFLResult(
        status=PASS,
        summary=(
            f"SBFL ranked {len(suspicious_lines)} suspicious line(s) "
            f"from {total_failed} failing and {total_passed} passing pytest run(s)."
        ),
        suspicious_lines=suspicious_lines,
        test_results=test_results,
        total_failed=total_failed,
        total_passed=total_passed,
    )


def ochiai_score(
    *,
    failed_and_covered: int,
    passed_and_covered: int,
    total_failed: int,
) -> float:
    """Compute Ochiai suspiciousness for one line."""

    if failed_and_covered <= 0 or total_failed <= 0:
        return 0.0
    denominator = math.sqrt(total_failed * (failed_and_covered + passed_and_covered))
    if denominator == 0:
        return 0.0
    return failed_and_covered / denominator


def format_sbfl_result(result: SBFLResult) -> str:
    """Render a compact human-readable SBFL report."""

    lines = ["# SBFL Result", "", f"- Status: **{result.status}**", f"- Summary: {result.summary}"]
    lines.append(f"- Failing runs: {result.total_failed}")
    lines.append(f"- Passing runs: {result.total_passed}")
    if not result.suspicious_lines:
        return "\n".join(lines)

    lines.extend(["", "## Top Suspicious Lines"])
    for item in result.suspicious_lines:
        source = f" `{item.source_line.strip()}`" if item.source_line else ""
        lines.append(
            f"- `{item.file}:{item.line_number}` score={item.score:.4f} "
            f"failed_covered={item.failed_and_covered} "
            f"passed_covered={item.passed_and_covered}{source}"
        )
    return "\n".join(lines)


def parse_pytest_command(command: str | Iterable[str]) -> list[str]:
    """Return pytest args from a supported pytest command."""

    tokens = shlex.split(command) if isinstance(command, str) else [str(part) for part in command]
    if not tokens:
        raise ValueError("Empty command cannot be used for SBFL.")

    executable_name = Path(tokens[0]).name
    if executable_name in {"pytest", "pytest.exe"}:
        return tokens[1:]

    if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "pytest":
        return tokens[3:]

    raise ValueError(
        "SBFL V1 only supports pytest commands, for example "
        "`pytest tests/test_x.py::test_y` or `python -m pytest ...`."
    )


def _build_pytest_run_specs(
    *,
    pytest_nodeids: Iterable[str] | None,
    commands: Iterable[str | Iterable[str]] | None,
) -> list[_PytestRunSpec]:
    specs: list[_PytestRunSpec] = []

    for nodeid in pytest_nodeids or []:
        clean = str(nodeid).strip()
        if not clean:
            continue
        args = [clean, "-q"]
        specs.append(_PytestRunSpec(label=clean, args=args, command=["pytest", *args]))

    for command in commands or []:
        command_tokens = shlex.split(command) if isinstance(command, str) else [str(part) for part in command]
        args = parse_pytest_command(command_tokens)
        label = " ".join(command_tokens)
        specs.append(_PytestRunSpec(label=label, args=args, command=command_tokens))

    return specs


def _run_pytest_spec_under_trace(
    spec: _PytestRunSpec,
    *,
    project_root: Path,
    python_executable: str,
    timeout_seconds: float,
    excluded_dirs: set[str],
    exclude_test_files: bool,
) -> TestCoverageResult:
    runner = _trace_pytest_runner_source()
    with tempfile.TemporaryDirectory(prefix="sra-sbfl-") as temp_dir:
        result_path = Path(temp_dir) / "result.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = _prepend_pythonpath(str(project_root), env.get("PYTHONPATH", ""))
        try:
            completed = subprocess.run(
                [
                    python_executable,
                    "-c",
                    runner,
                    str(project_root),
                    json.dumps(spec.args),
                    str(result_path),
                ],
                cwd=project_root,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
            completed_return_code = completed.returncode
            completed_stdout = completed.stdout
            completed_stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            completed_return_code = 124
            completed_stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            completed_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            completed_stderr = (
                f"{completed_stderr}\nSBFL pytest trace runner timed out after {timeout_seconds} seconds."
            ).strip()
        except OSError as exc:
            completed_return_code = 127
            completed_stdout = ""
            completed_stderr = f"SBFL pytest trace runner could not start: {exc}"

        payload: dict[str, Any] = {}
        if result_path.exists():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}

        return_code = int(payload.get("return_code", completed_return_code))
        stdout = str(payload.get("stdout", ""))
        stderr = str(payload.get("stderr", ""))
        if completed_stdout.strip():
            stdout = f"{stdout}\n{completed_stdout}".strip()
        if completed_stderr.strip():
            stderr = f"{stderr}\n{completed_stderr}".strip()

        raw_covered = payload.get("covered_lines", {})
        covered_lines = _normalize_covered_lines(
            raw_covered if isinstance(raw_covered, dict) else {},
            project_root=project_root,
            excluded_dirs=excluded_dirs,
            exclude_test_files=exclude_test_files,
        )

    return TestCoverageResult(
        nodeid=spec.label,
        passed=return_code == 0,
        covered_lines=covered_lines,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        command=spec.command,
    )


def _trace_pytest_runner_source() -> str:
    return textwrap.dedent(
        r'''
        import contextlib
        import io
        import json
        import os
        import sys
        import trace

        root = os.path.abspath(sys.argv[1])
        pytest_args = json.loads(sys.argv[2])
        result_path = sys.argv[3]
        os.chdir(root)
        if root not in sys.path:
            sys.path.insert(0, root)

        stdout = io.StringIO()
        stderr = io.StringIO()
        return_code = 2
        covered_lines = {}
        try:
            import pytest
            tracer = trace.Trace(count=True, trace=False, ignoredirs=[sys.prefix, sys.exec_prefix])
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    return_code = tracer.runfunc(pytest.main, pytest_args)
                except SystemExit as exc:
                    code = exc.code
                    return_code = code if isinstance(code, int) else 1
            for (filename, line_number), count in tracer.results().counts.items():
                if count <= 0:
                    continue
                path = os.path.abspath(filename)
                if path == root or path.startswith(root + os.sep):
                    covered_lines.setdefault(path, set()).add(int(line_number))
        except BaseException as exc:
            stderr.write(f"SBFL pytest trace runner failed: {type(exc).__name__}: {exc}\n")
            return_code = 2

        serializable = {path: sorted(lines) for path, lines in covered_lines.items()}
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "return_code": int(return_code),
                    "stdout": stdout.getvalue(),
                    "stderr": stderr.getvalue(),
                    "covered_lines": serializable,
                },
                handle,
            )
        '''
    )


def _normalize_covered_lines(
    raw_covered: dict[str, Any],
    *,
    project_root: Path,
    excluded_dirs: set[str],
    exclude_test_files: bool,
) -> dict[str, set[int]]:
    normalized: dict[str, set[int]] = {}
    for raw_file, raw_lines in raw_covered.items():
        path = Path(raw_file).resolve()
        if not _is_project_source_file(
            path,
            project_root=project_root,
            excluded_dirs=excluded_dirs,
            exclude_test_files=exclude_test_files,
        ):
            continue
        relative = _relative_or_absolute(path, project_root)
        lines = {int(line) for line in raw_lines if str(line).isdigit() or isinstance(line, int)}
        if lines:
            normalized.setdefault(relative, set()).update(lines)
    return normalized


def _rank_suspicious_lines(
    test_results: list[TestCoverageResult],
    *,
    project_root: Path,
    total_failed: int,
    top_k: int,
) -> list[SuspiciousLine]:
    line_keys = sorted(
        {
            (file, line)
            for result in test_results
            for file, lines in result.covered_lines.items()
            for line in lines
        }
    )
    suspicious: list[SuspiciousLine] = []
    for file, line_number in line_keys:
        failed_and_covered = sum(
            1 for result in test_results
            if not result.passed and line_number in result.covered_lines.get(file, set())
        )
        if failed_and_covered <= 0:
            continue
        passed_and_covered = sum(
            1 for result in test_results
            if result.passed and line_number in result.covered_lines.get(file, set())
        )
        score = ochiai_score(
            failed_and_covered=failed_and_covered,
            passed_and_covered=passed_and_covered,
            total_failed=total_failed,
        )
        suspicious.append(
            SuspiciousLine(
                file=file,
                line_number=line_number,
                score=round(score, 6),
                failed_and_covered=failed_and_covered,
                passed_and_covered=passed_and_covered,
                total_failed=total_failed,
                source_line=_source_line(project_root / file, line_number),
            )
        )

    suspicious.sort(
        key=lambda item: (
            -item.score,
            -item.failed_and_covered,
            item.passed_and_covered,
            item.file,
            item.line_number,
        )
    )
    return suspicious[:top_k] if top_k else []


def _is_project_source_file(
    path: Path,
    *,
    project_root: Path,
    excluded_dirs: set[str],
    exclude_test_files: bool,
) -> bool:
    if path.suffix != ".py":
        return False
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return False
    parts = set(relative.parts)
    if parts & excluded_dirs:
        return False
    if exclude_test_files and _looks_like_test_file(relative):
        return False
    return True


def _looks_like_test_file(relative_path: Path) -> bool:
    parts = relative_path.parts
    return "tests" in parts or relative_path.name.startswith("test_") or relative_path.name.endswith("_test.py")


def _source_line(path: Path, line_number: int) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1]
    return None


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _prepend_pythonpath(prefix: str, existing: str) -> str:
    if not existing:
        return prefix
    return prefix + os.pathsep + existing
