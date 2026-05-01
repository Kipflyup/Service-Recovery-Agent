from __future__ import annotations

from service_recovery_agent.contract_validator import (
    build_contract_validation_result,
    format_contract_validation_result,
)
from service_recovery_agent.validator import ValidationCommandResult, ValidationResult


def _command_result(command: list[str], passed: bool, *, stderr: str = "") -> ValidationCommandResult:
    return ValidationCommandResult(
        command=command,
        return_code=0 if passed else 1,
        stdout="ok" if passed else "failed",
        stderr=stderr,
        duration_seconds=0.01,
    )


def _validation(*results: ValidationCommandResult) -> ValidationResult:
    return ValidationResult(
        status="PASS" if all(result.passed for result in results) else "FAIL",
        command_results=list(results),
    )


def test_contract_validation_passes_when_previously_passing_command_still_passes() -> None:
    result = build_contract_validation_result(
        _validation(_command_result(["pytest", "-q"], True)),
        _validation(_command_result(["pytest", "-q"], True)),
    )

    assert result.status == "PASS"
    assert result.passed is True
    assert result.command_baselines[0].regression is False
    assert result.to_dict()["regression_count"] == 0


def test_contract_validation_fails_on_regression() -> None:
    result = build_contract_validation_result(
        _validation(_command_result(["pytest", "-q"], True)),
        _validation(_command_result(["pytest", "-q"], False, stderr="boom")),
    )

    assert result.status == "FAIL"
    assert result.passed is False
    assert result.command_baselines[0].regression is True
    report = format_contract_validation_result(result)
    assert "Contract Validation" in report
    assert "Regression: YES" in report
    assert "boom" in report


def test_contract_validation_allows_previously_failing_command_to_pass() -> None:
    result = build_contract_validation_result(
        _validation(_command_result(["python", "scripts/validate_demo_repair.py"], False)),
        _validation(_command_result(["python", "scripts/validate_demo_repair.py"], True)),
    )

    assert result.status == "PASS"
    assert result.command_baselines[0].before_passed is False
    assert result.command_baselines[0].after_passed is True
    assert result.command_baselines[0].regression is False


def test_contract_validation_partial_when_no_regression_but_command_still_fails() -> None:
    result = build_contract_validation_result(
        _validation(_command_result(["python", "repair_check.py"], False)),
        _validation(_command_result(["python", "repair_check.py"], False)),
    )

    assert result.status == "PARTIAL"
    assert result.passed is False
    assert result.command_baselines[0].regression is False


def test_contract_validation_partial_for_missing_baselines() -> None:
    result = build_contract_validation_result(
        ValidationResult(status="PASS", command_results=[]),
        ValidationResult(status="PASS", command_results=[]),
    )

    assert result.status == "PARTIAL"
    assert result.command_baselines == []
