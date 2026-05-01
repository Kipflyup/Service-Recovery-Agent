from __future__ import annotations

from service_recovery_agent.adversarial_validator import (
    AdversarialValidationResult,
    ProbeAttemptResult,
    ProbeCase,
    ProbeResult,
)
from service_recovery_agent.contract_validator import ContractCommandBaseline, ContractValidationResult
from service_recovery_agent.counterfactual_validator import CounterfactualValidationResult
from service_recovery_agent.failure_feedback import (
    build_apply_failure_feedback,
    build_contract_failure_feedback,
    build_counterfactual_failure_feedback,
    build_dry_run_failure_feedback,
    build_safety_failure_feedback,
    build_validation_failure_feedback,
    format_repair_failure_feedback_report,
)
from service_recovery_agent.patch_safety import PatchSafetyFinding, PatchSafetyReview
from service_recovery_agent.patcher import PatchApplyResult, PatchDryRunResult, PatchPreviewResult
from service_recovery_agent.validator import ValidationCommandResult, ValidationResult


def _review(status: str, findings: list[PatchSafetyFinding]) -> PatchSafetyReview:
    return PatchSafetyReview(
        status=status,
        summary=f"synthetic {status} safety review",
        modified_files=["demo_service/app.py"],
        allowed_files=["demo_service/app.py"],
        findings=findings,
    )


def test_safety_failure_feedback_includes_findings_and_details() -> None:
    preview = PatchPreviewResult(
        safety_review=_review(
            "FAIL",
            [
                PatchSafetyFinding(
                    check="dangerous_operations",
                    status="FAIL",
                    message="Patch introduces dangerous runtime operations.",
                    evidence=["os.system('rm -rf /')"],
                ),
                PatchSafetyFinding(
                    check="patch_present",
                    status="PASS",
                    message="Patch exists.",
                ),
            ],
        ),
        dry_run=None,
        patch_text="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
    )

    feedback = build_safety_failure_feedback(preview)

    assert feedback is not None
    assert feedback.failed_stage == "safety_review"
    assert feedback.failed_probes == ["dangerous_operations"]
    assert feedback.details["safety_status"] == "FAIL"
    assert feedback.details["non_passing_findings"][0]["check"] == "dangerous_operations"
    assert "Do not bypass dangerous_operations" in feedback.prompt_feedback

    report = format_repair_failure_feedback_report(feedback)
    assert "Structured Details" in report
    assert "dangerous_operations" in report


def test_dry_run_failure_feedback_includes_stderr_excerpt_and_truncates_logs() -> None:
    long_stderr = "dry-run mismatch " + ("x" * 2_000)
    preview = PatchPreviewResult(
        safety_review=_review(
            "PASS",
            [PatchSafetyFinding(check="patch_present", status="PASS", message="Patch exists.")],
        ),
        dry_run=PatchDryRunResult(
            command=["patch", "-p1", "--dry-run", "-i", "<tempfile>"],
            return_code=1,
            stdout="",
            stderr=long_stderr,
            can_apply=False,
        ),
        patch_text="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n@@ -999,1 +999,1 @@\n-missing\n+replacement\n",
    )

    feedback = build_dry_run_failure_feedback(preview)

    assert feedback is not None
    assert feedback.failed_stage == "patch_dry_run"
    assert feedback.failed_probes == ["patch_dry_run"]
    assert feedback.details["return_code"] == 1
    assert len(feedback.details["stderr_excerpt"]) <= 800
    assert feedback.details["stderr_excerpt"].endswith("...")
    assert "Dry-run command: patch -p1 --dry-run -i <tempfile>" in feedback.prompt_feedback
    assert "regenerate context lines" in feedback.prompt_feedback


def test_apply_failure_feedback_contains_apply_command_and_error() -> None:
    apply_result = PatchApplyResult(
        command=["patch", "-p1", "-i", "<tempfile>"],
        return_code=2,
        stdout="checking file demo_service/app.py",
        stderr="patch unexpectedly ends in middle of line",
        applied=False,
    )

    feedback = build_apply_failure_feedback(apply_result)

    assert feedback is not None
    assert feedback.failed_stage == "patch_apply"
    assert feedback.details["return_code"] == 2
    assert "patch unexpectedly ends" in feedback.prompt_feedback
    assert "restores the file snapshot" in feedback.prompt_feedback


def test_validation_failure_feedback_lists_failed_commands_and_truncates_output() -> None:
    result = ValidationResult(
        status="FAIL",
        command_results=[
            ValidationCommandResult(
                command=["python", "-c", "print('ok')"],
                return_code=0,
                stdout="ok\n",
                stderr="",
                duration_seconds=0.01,
            ),
            ValidationCommandResult(
                command=["python", "-m", "pytest", "tests/test_demo.py"],
                return_code=1,
                stdout="failure output " + ("y" * 2_000),
                stderr="AssertionError: expected controlled 400 response",
                duration_seconds=1.23,
            ),
        ],
    )

    feedback = build_validation_failure_feedback(result)

    assert feedback is not None
    assert feedback.failed_stage == "validation"
    assert feedback.failed_probes == ["python -m pytest tests/test_demo.py"]
    assert feedback.details["failed_commands"][0]["return_code"] == 1
    assert len(feedback.details["failed_commands"][0]["stdout_excerpt"]) <= 800
    assert "AssertionError: expected controlled 400 response" in feedback.prompt_feedback
    assert "Do not remove or weaken tests" in feedback.prompt_feedback


def test_contract_failure_feedback_describes_regression() -> None:
    contract_result = ContractValidationResult(
        status="FAIL",
        summary="1/1 validation command(s) regressed: python -m pytest tests/test_contract.py.",
        command_baselines=[
            ContractCommandBaseline(
                command=["python", "-m", "pytest", "tests/test_contract.py"],
                before_passed=True,
                after_passed=False,
                before_return_code=0,
                after_return_code=1,
                regression=True,
                stdout_before="1 passed",
                stderr_before="",
                stdout_after="",
                stderr_after="AssertionError: public contract changed",
            )
        ],
    )

    feedback = build_contract_failure_feedback(contract_result)

    assert feedback is not None
    assert feedback.failed_stage == "contract_validation"
    assert feedback.failed_probes == ["python -m pytest tests/test_contract.py"]
    assert feedback.details["regression_count"] == 1
    assert "before_passed: True" in feedback.prompt_feedback
    assert "public contract changed" in feedback.prompt_feedback
    assert "D-type interface/contract change" in feedback.prompt_feedback


def test_counterfactual_failure_feedback_detects_before_patch_unexpected_pass() -> None:
    before_pass = AdversarialValidationResult(
        status="PASS",
        summary="buggy state unexpectedly passed adversarial probes",
        probe_results=[],
    )
    after_pass = AdversarialValidationResult(
        status="PASS",
        summary="patched state passed adversarial probes",
        probe_results=[],
    )
    result = CounterfactualValidationResult(
        status="FAIL",
        summary="Counterfactual validation failed: before_patch did not fail as expected.",
        before_patch_result=before_pass,
        after_patch_result=after_pass,
    )

    feedback = build_counterfactual_failure_feedback(result)

    assert feedback is not None
    assert feedback.failed_stage == "counterfactual_validation"
    assert feedback.failed_probes == ["before_patch_expected_fail"]
    assert feedback.details["before_patch_status"] == "PASS"
    assert "before_patch adversarial status: PASS" in feedback.prompt_feedback
    assert "do not claim the bug is fixed" in feedback.prompt_feedback


def test_counterfactual_failure_feedback_includes_after_failed_probe_details() -> None:
    failed_case = ProbeCase(
        name="zero_boundary",
        method="GET",
        path="/divide?x=0",
        expected_statuses={400, 422},
        forbidden_statuses={500},
    )
    after_fail = AdversarialValidationResult(
        status="FAIL",
        summary="zero boundary still returned 500",
        probe_results=[
            ProbeResult(
                case=failed_case,
                attempts=[
                    ProbeAttemptResult(
                        status_code=500,
                        response_json={"error": "internal_server_error"},
                        passed=False,
                        reason="status 500 is forbidden",
                    )
                ],
            )
        ],
    )
    before_fail = AdversarialValidationResult(status="FAIL", summary="bug reproduced", probe_results=[])
    result = CounterfactualValidationResult(
        status="FAIL",
        summary="Counterfactual validation failed: after_patch did not pass as expected.",
        before_patch_result=before_fail,
        after_patch_result=after_fail,
    )

    feedback = build_counterfactual_failure_feedback(result)

    assert feedback is not None
    assert feedback.failed_probes == ["after_patch_expected_pass"]
    assert "Failed after_patch probes" in feedback.prompt_feedback
    assert "GET /divide?x=0" in feedback.prompt_feedback
    assert "status 500 is forbidden" in feedback.prompt_feedback
