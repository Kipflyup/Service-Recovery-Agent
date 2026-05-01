"""Contract-preservation validation helpers.

Normal validation answers "do the configured checks pass after the patch?".
Contract validation compares before/after outcomes for the same validation
commands and blocks regressions: a command that passed before the patch must
not fail after the patch.

The first implementation is command-level rather than pytest-nodeid-level.  It
is intentionally deterministic and lightweight, while leaving room for a later
per-test baseline implementation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .validator import ValidationCommandResult, ValidationResult, validate_project


PASS = "PASS"
FAIL = "FAIL"
PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class ContractCommandBaseline:
    """Before/after outcome for one validation command."""

    command: list[str]
    before_passed: bool
    after_passed: bool
    before_return_code: int
    after_return_code: int
    regression: bool
    stdout_before: str = ""
    stderr_before: str = ""
    stdout_after: str = ""
    stderr_after: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContractValidationResult:
    """Structured result for command-level contract preservation."""

    status: str
    summary: str
    command_baselines: list[ContractCommandBaseline] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "passed": self.passed,
            "command_baselines": [baseline.to_dict() for baseline in self.command_baselines],
            "regression_count": sum(1 for baseline in self.command_baselines if baseline.regression),
            "total_command_count": len(self.command_baselines),
        }


def run_contract_baseline(
    *,
    project_root: str | Path = ".",
    commands: Iterable[str | Iterable[str]] | None = None,
    timeout_seconds: float = 120.0,
) -> ValidationResult:
    """Run commands before patch application for later contract comparison."""

    return validate_project(
        project_root=project_root,
        commands=commands,
        timeout_seconds=timeout_seconds,
    )


def build_contract_validation_result(
    before_result: ValidationResult,
    after_result: ValidationResult,
) -> ContractValidationResult:
    """Compare before/after validation results and detect regressions."""

    before_commands = before_result.command_results
    after_commands = after_result.command_results
    baselines: list[ContractCommandBaseline] = []

    max_count = max(len(before_commands), len(after_commands))
    for index in range(max_count):
        before = before_commands[index] if index < len(before_commands) else None
        after = after_commands[index] if index < len(after_commands) else None
        baselines.append(_build_command_baseline(before, after))

    return summarize_contract_baselines(baselines)


def summarize_contract_baselines(
    baselines: list[ContractCommandBaseline],
) -> ContractValidationResult:
    """Build a status summary from command baselines."""

    if not baselines:
        return ContractValidationResult(
            status=PARTIAL,
            summary="No contract validation command baselines were available.",
            command_baselines=[],
        )

    regressions = [baseline for baseline in baselines if baseline.regression]
    after_failures = [baseline for baseline in baselines if not baseline.after_passed]
    if regressions:
        status = FAIL
        summary = (
            f"{len(regressions)}/{len(baselines)} validation command(s) regressed: "
            f"{', '.join(_command_text(baseline.command) for baseline in regressions)}."
        )
    elif after_failures:
        status = PARTIAL
        summary = (
            "No previously passing command regressed, but "
            f"{len(after_failures)}/{len(baselines)} command(s) still fail after the patch."
        )
    else:
        status = PASS
        summary = f"No contract regression detected across {len(baselines)} validation command(s)."

    return ContractValidationResult(
        status=status,
        summary=summary,
        command_baselines=baselines,
    )


def format_contract_validation_result(result: ContractValidationResult) -> str:
    """Render a human-readable contract validation report."""

    lines: list[str] = []
    lines.append("# Contract Validation")
    lines.append("")
    lines.append(f"- Status: **{result.status}**")
    lines.append(f"- Summary: {result.summary}")

    if not result.command_baselines:
        return "\n".join(lines)

    lines.append("")
    lines.append("## Command Baselines")
    for baseline in result.command_baselines:
        regression = "YES" if baseline.regression else "NO"
        lines.append(f"- `{' '.join(baseline.command)}`")
        lines.append(f"  - Before passed: {baseline.before_passed} (rc={baseline.before_return_code})")
        lines.append(f"  - After passed: {baseline.after_passed} (rc={baseline.after_return_code})")
        lines.append(f"  - Regression: {regression}")
        if baseline.regression:
            excerpt = baseline.stderr_after.strip() or baseline.stdout_after.strip()
            if excerpt:
                lines.append(f"  - After failure excerpt: {_truncate(excerpt, 240)}")
    return "\n".join(lines).rstrip()


def _build_command_baseline(
    before: ValidationCommandResult | None,
    after: ValidationCommandResult | None,
) -> ContractCommandBaseline:
    command = before.command if before is not None else (after.command if after is not None else [])
    before_passed = bool(before.passed) if before is not None else False
    after_passed = bool(after.passed) if after is not None else False
    return ContractCommandBaseline(
        command=command,
        before_passed=before_passed,
        after_passed=after_passed,
        before_return_code=before.return_code if before is not None else 127,
        after_return_code=after.return_code if after is not None else 127,
        regression=before_passed and not after_passed,
        stdout_before=before.stdout if before is not None else "",
        stderr_before=before.stderr if before is not None else "missing before validation result",
        stdout_after=after.stdout if after is not None else "",
        stderr_after=after.stderr if after is not None else "missing after validation result",
    )


def _command_text(command: list[str]) -> str:
    return " ".join(command) if command else "<missing command>"


def _truncate(text: str, max_length: int) -> str:
    normalized = text.replace("\n", " ").strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."
