from __future__ import annotations

from service_recovery_agent.adversarial_validator import AdversarialValidationResult
from service_recovery_agent.counterfactual_validator import (
    build_counterfactual_validation_result,
    format_counterfactual_validation_result,
)


def _adversarial(status: str) -> AdversarialValidationResult:
    return AdversarialValidationResult(
        status=status,
        summary=f"synthetic {status.lower()}",
        probe_results=[],
    )


def test_counterfactual_passes_when_before_fails_and_after_passes() -> None:
    result = build_counterfactual_validation_result(
        before_patch_result=_adversarial("FAIL"),
        after_patch_result=_adversarial("PASS"),
    )

    assert result.status == "PASS"
    assert result.passed is True
    assert "buggy state failed" in result.summary
    assert result.to_dict()["before_patch_result"]["status"] == "FAIL"


def test_counterfactual_fails_when_before_already_passes() -> None:
    result = build_counterfactual_validation_result(
        before_patch_result=_adversarial("PASS"),
        after_patch_result=_adversarial("PASS"),
    )

    assert result.status == "FAIL"
    assert "before_patch did not fail" in result.summary


def test_counterfactual_fails_when_after_still_fails() -> None:
    result = build_counterfactual_validation_result(
        before_patch_result=_adversarial("FAIL"),
        after_patch_result=_adversarial("FAIL"),
    )

    assert result.status == "FAIL"
    assert "after_patch did not pass" in result.summary


def test_counterfactual_partial_when_result_missing() -> None:
    result = build_counterfactual_validation_result(
        before_patch_result=None,
        after_patch_result=_adversarial("PASS"),
    )

    assert result.status == "PARTIAL"
    assert result.passed is False
    report = format_counterfactual_validation_result(result)
    assert "Counterfactual Validation" in report
    assert "Before patch adversarial status: missing" in report
