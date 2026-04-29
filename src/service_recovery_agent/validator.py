"""Validation helpers for patched code.

The default validation command is the repository test suite:

``python -m pytest -q``

Callers can provide additional commands later, but the module intentionally
returns structured results instead of raising on test failure.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ValidationCommandResult:
    command: list[str]
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def passed(self) -> bool:
        return self.return_code == 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        return data


@dataclass(frozen=True)
class ValidationResult:
    status: str
    command_results: list[ValidationCommandResult]

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "command_results": [result.to_dict() for result in self.command_results],
        }


def default_validation_commands() -> list[list[str]]:
    return [[sys.executable, "-m", "pytest", "-q"]]


def parse_command(command: str | Iterable[str]) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


def validate_project(
    *,
    project_root: str | Path = ".",
    commands: Iterable[str | Iterable[str]] | None = None,
    timeout_seconds: float = 120.0,
) -> ValidationResult:
    """Run validation commands and return structured results."""

    root = Path(project_root).resolve()
    parsed_commands = [parse_command(command) for command in commands] if commands else default_validation_commands()
    results = [
        run_validation_command(command, project_root=root, timeout_seconds=timeout_seconds)
        for command in parsed_commands
    ]
    status = "PASS" if all(result.passed for result in results) else "FAIL"
    return ValidationResult(status=status, command_results=results)


def run_validation_command(
    command: list[str],
    *,
    project_root: str | Path = ".",
    timeout_seconds: float = 120.0,
) -> ValidationCommandResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=Path(project_root).resolve(),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        return_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr = f"{stderr}\nValidation command timed out after {timeout_seconds} seconds.".strip()

    return ValidationCommandResult(
        command=command,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - start,
    )


def format_validation_result(result: ValidationResult) -> str:
    lines: list[str] = []
    lines.append("# Validation Result")
    lines.append("")
    lines.append(f"- Status: **{result.status}**")
    lines.append("")
    for command_result in result.command_results:
        lines.append(f"## Command: `{' '.join(command_result.command)}`")
        lines.append(f"- Passed: {command_result.passed}")
        lines.append(f"- Return code: {command_result.return_code}")
        lines.append(f"- Duration: {command_result.duration_seconds:.2f}s")
        if command_result.stdout.strip():
            lines.append("")
            lines.append("stdout:")
            lines.append("```text")
            lines.append(command_result.stdout.rstrip())
            lines.append("```")
        if command_result.stderr.strip():
            lines.append("")
            lines.append("stderr:")
            lines.append("```text")
            lines.append(command_result.stderr.rstrip())
            lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()

