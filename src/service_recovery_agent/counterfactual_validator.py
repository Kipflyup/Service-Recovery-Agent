"""Counterfactual before/after validation helpers.

A repair is more convincing when the original buggy state demonstrably fails
and the patched state passes the same adversarial probes.  This module compares
those two probe results and produces a small structured verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adversarial_validator import (
    AdversarialValidationResult,
    validate_demo_app_after_repair,
)
from .fault_diagnosis import FaultDiagnosis


PASS = "PASS"
FAIL = "FAIL"
PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class CounterfactualValidationResult:
    """Structured before/after adversarial validation comparison."""

    status: str
    summary: str
    before_patch_result: AdversarialValidationResult | None
    after_patch_result: AdversarialValidationResult | None
    expected_before_fail: bool = True
    expected_after_pass: bool = True

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "passed": self.passed,
            "expected_before_fail": self.expected_before_fail,
            "expected_after_pass": self.expected_after_pass,
            "before_patch_result": (
                self.before_patch_result.to_dict() if self.before_patch_result else None
            ),
            "after_patch_result": (
                self.after_patch_result.to_dict() if self.after_patch_result else None
            ),
        }


def validate_current_demo_app(
    diagnosis: FaultDiagnosis,
    *,
    log_path: str | Path | None = None,
) -> AdversarialValidationResult:
    """Run adversarial probes against the current on-disk demo app."""

    return validate_demo_app_after_repair(diagnosis, log_path=log_path)


def build_counterfactual_validation_result(
    *,
    before_patch_result: AdversarialValidationResult | None,
    after_patch_result: AdversarialValidationResult | None,
    expected_before_fail: bool = True,
    expected_after_pass: bool = True,
) -> CounterfactualValidationResult:
    """Compare before/after adversarial results against expected outcomes."""

    if before_patch_result is None or after_patch_result is None:
        return CounterfactualValidationResult(
            status=PARTIAL,
            summary="Counterfactual validation is incomplete because before or after probe results are missing.",
            before_patch_result=before_patch_result,
            after_patch_result=after_patch_result,
            expected_before_fail=expected_before_fail,
            expected_after_pass=expected_after_pass,
        )

    before_failed = not before_patch_result.passed
    after_passed = after_patch_result.passed
    before_ok = before_failed if expected_before_fail else before_patch_result.passed
    after_ok = after_passed if expected_after_pass else not after_passed

    if before_ok and after_ok:
        status = PASS
        summary = "Counterfactual validation passed: buggy state failed and patched state passed."
    else:
        status = FAIL
        problems: list[str] = []
        if not before_ok:
            if expected_before_fail:
                problems.append("before_patch did not fail as expected")
            else:
                problems.append("before_patch did not pass as expected")
        if not after_ok:
            if expected_after_pass:
                problems.append("after_patch did not pass as expected")
            else:
                problems.append("after_patch did not fail as expected")
        summary = "Counterfactual validation failed: " + "; ".join(problems) + "."

    return CounterfactualValidationResult(
        status=status,
        summary=summary,
        before_patch_result=before_patch_result,
        after_patch_result=after_patch_result,
        expected_before_fail=expected_before_fail,
        expected_after_pass=expected_after_pass,
    )


def format_counterfactual_validation_result(result: CounterfactualValidationResult) -> str:
    """Render a human-readable counterfactual validation report."""

    lines: list[str] = []
    lines.append("# Counterfactual Validation")
    lines.append("")
    lines.append(f"- Status: **{result.status}**")
    lines.append(f"- Summary: {result.summary}")
    lines.append(f"- Expected before patch fail: {result.expected_before_fail}")
    lines.append(f"- Expected after patch pass: {result.expected_after_pass}")
    if result.before_patch_result is not None:
        lines.append(f"- Before patch adversarial status: {result.before_patch_result.status}")
        lines.append(f"- Before patch summary: {result.before_patch_result.summary}")
    else:
        lines.append("- Before patch adversarial status: missing")
    if result.after_patch_result is not None:
        lines.append(f"- After patch adversarial status: {result.after_patch_result.status}")
        lines.append(f"- After patch summary: {result.after_patch_result.summary}")
    else:
        lines.append("- After patch adversarial status: missing")
    return "\n".join(lines).rstrip()
