from __future__ import annotations

from service_recovery_agent.adversarial_validator import AdversarialValidationResult
from service_recovery_agent.contract_validator import ContractCommandBaseline, ContractValidationResult
from service_recovery_agent.counterfactual_validator import CounterfactualValidationResult
from service_recovery_agent.decision import DecisionAction, DecisionReason, DecisionResult
from service_recovery_agent.failure_feedback import RepairFailureFeedback
from service_recovery_agent.fault_diagnosis import (
    ErrorClassification,
    FaultCandidate,
    FaultDiagnosis,
    RepairConstraint,
    VariableFlow,
)
from service_recovery_agent.fix_planner import FixProposal
from service_recovery_agent.patch_safety import PatchSafetyFinding, PatchSafetyReview
from service_recovery_agent.patcher import PatchDryRunResult, PatchPreviewResult
from service_recovery_agent.recovery_agent import RecoveryRunResult, RepairAttemptSummary
from service_recovery_agent.review_artifact import (
    ReviewArtifact,
    build_pr_description_markdown,
    build_review_artifact,
    build_review_criteria,
    build_rollback_plan,
)
from service_recovery_agent.traceback_parser import StackFrame, TracebackEvent
from service_recovery_agent.validator import ValidationCommandResult, ValidationResult


def _traceback_event() -> TracebackEvent:
    return TracebackEvent(
        exception_type="ZeroDivisionError",
        exception_message="float division by zero",
        frames=[
            StackFrame(
                file="demo_service/app.py",
                line_number=31,
                function="unsafe_divide",
                code="return numerator / denominator",
            )
        ],
        raw="Traceback omitted by test fixture",
        source_path="logs/app.log",
    )


def _diagnosis() -> FaultDiagnosis:
    return FaultDiagnosis(
        classification=ErrorClassification(
            exception_type="ZeroDivisionError",
            category="arithmetic_boundary",
            strategy_hint="Validate zero denominator before division.",
            confidence="high",
        ),
        primary_candidate=FaultCandidate(
            file="demo_service/app.py",
            line_number=31,
            function="unsafe_divide",
            code="return numerator / denominator",
            suspiciousness=0.95,
            reason="Right operand denominator may be zero.",
        ),
        candidates=[],
        suspected_variables=["denominator"],
        variable_flows=[
            VariableFlow(
                variable="denominator",
                source="query parameter denominator or x via _float_arg, or literal 0.0 from /bug",
            )
        ],
        repair_constraints=[
            RepairConstraint(
                kind="preserve_signature",
                description="Do not change unsafe_divide(numerator, denominator) signature.",
            )
        ],
        adversarial_hints=["GET /divide?x=0", "GET /bug", "GET /divide?x=4"],
        trigger_conditions=["denominator == 0"],
    )


def _proposal() -> FixProposal:
    return FixProposal(
        root_cause="unsafe_divide directly divides by denominator without guarding zero.",
        fix_strategy="Raise a controlled input error for zero denominator and convert it to HTTP 400.",
        change_type="B",
        risk_level="medium",
        confidence="high",
        contract_constraints=["Preserve /divide?x=4 result 25.0."],
        patch_draft="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n... intentionally omitted ...",
        tests_to_run=["python -m pytest -q -m 'not seed_failure'"],
        manual_review_notes="Web API error semantics change from 500 to 400 for invalid zero denominator.",
    )


def _preview() -> PatchPreviewResult:
    return PatchPreviewResult(
        safety_review=PatchSafetyReview(
            status="PASS",
            summary="Patch draft passed all configured static safety checks.",
            modified_files=["demo_service/app.py"],
            allowed_files=["demo_service/app.py"],
            findings=[PatchSafetyFinding(check="patch_present", status="PASS", message="ok")],
        ),
        dry_run=PatchDryRunResult(
            command=["patch", "-p1", "--dry-run", "-i", "<tempfile>"],
            return_code=0,
            stdout="checking file demo_service/app.py\n",
            stderr="",
            can_apply=True,
        ),
        patch_text="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
    )


def _validation(status: str = "PASS", *, noisy_failure: bool = False) -> ValidationResult:
    if status == "PASS":
        return ValidationResult(
            status="PASS",
            command_results=[
                ValidationCommandResult(
                    command=["python", "-m", "pytest", "-q", "-m", "not seed_failure"],
                    return_code=0,
                    stdout="46 passed\n",
                    stderr="",
                    duration_seconds=0.2,
                ),
                ValidationCommandResult(
                    command=["python", "scripts/validate_demo_repair.py"],
                    return_code=0,
                    stdout="demo repair validation passed\n",
                    stderr="",
                    duration_seconds=0.1,
                ),
            ],
        )

    noisy = "failure from .env " + ("x" * 1000) if noisy_failure else "AssertionError: expected 400"
    return ValidationResult(
        status="FAIL",
        command_results=[
            ValidationCommandResult(
                command=["python", "-m", "pytest", "tests/test_demo.py"],
                return_code=1,
                stdout="",
                stderr=noisy,
                duration_seconds=0.5,
            )
        ],
    )


def _contract() -> ContractValidationResult:
    return ContractValidationResult(
        status="PASS",
        summary="No contract regression detected across 1 validation command(s).",
        command_baselines=[
            ContractCommandBaseline(
                command=["python", "-m", "pytest", "-q", "-m", "not seed_failure"],
                before_passed=True,
                after_passed=True,
                before_return_code=0,
                after_return_code=0,
                regression=False,
            )
        ],
    )


def _adversarial() -> AdversarialValidationResult:
    return AdversarialValidationResult(
        status="PASS",
        summary="All 6 adversarial probe(s) passed.",
        probe_results=[],
    )


def _counterfactual() -> CounterfactualValidationResult:
    before = AdversarialValidationResult(status="FAIL", summary="buggy state failed as expected", probe_results=[])
    after = AdversarialValidationResult(status="PASS", summary="patched state passed", probe_results=[])
    return CounterfactualValidationResult(
        status="PASS",
        summary="Counterfactual validation passed: buggy state failed and patched state passed.",
        before_patch_result=before,
        after_patch_result=after,
    )


def _decision(action: DecisionAction = DecisionAction.CREATE_PR_READY) -> DecisionResult:
    return DecisionResult(
        action=action,
        confidence_score=0.92 if action is DecisionAction.CREATE_PR_READY else 0.41,
        risk_level="medium",
        can_auto_deliver=False,
        reasons=[
            DecisionReason(
                code="status_pass" if action is DecisionAction.CREATE_PR_READY else "status_not_pass",
                severity="info" if action is DecisionAction.CREATE_PR_READY else "error",
                message=f"Synthetic decision reason for {action.value}.",
            )
        ],
        summary=f"Decision summary for {action.value}.",
    )


def _success_result() -> RecoveryRunResult:
    first_feedback = RepairFailureFeedback(
        failed_stage="adversarial_validation",
        failed_probes=["bug_shortcut"],
        summary="bug_shortcut returned 500 in first attempt",
        prompt_feedback="Handle all callers including /bug.",
    )
    attempts = [
        RepairAttemptSummary(
            attempt_number=1,
            status="FAIL_ROLLED_BACK",
            reason="Adversarial validation failed; snapshot restored.",
            rolled_back=True,
            failure_feedback=first_feedback,
        ),
        RepairAttemptSummary(
            attempt_number=2,
            status="PASS",
            reason="Patch applied; all gates passed.",
            rolled_back=False,
        ),
    ]
    return RecoveryRunResult(
        status="PASS",
        reason="Patch applied; validation, contract, counterfactual, and adversarial validation passed.",
        rolled_back=False,
        attempt_number=2,
        max_repair_attempts=2,
        repair_attempts=attempts,
        traceback_event=_traceback_event(),
        diagnosis=_diagnosis(),
        proposal=_proposal(),
        preview=_preview(),
        validation_result=_validation(),
        contract_result=_contract(),
        adversarial_result=_adversarial(),
        counterfactual_result=_counterfactual(),
    )


def test_build_review_artifact_generates_pr_ready_markdown_with_required_sections() -> None:
    artifact = build_review_artifact(
        _success_result(),
        decision=_decision(),
        commit_sha="abc1234",
        branch="recovery/demo-fix",
        pr_url="https://example.test/pr/1",
    )
    markdown = artifact.markdown

    assert isinstance(artifact, ReviewArtifact)
    assert artifact.rollback_plan.command == "git revert abc1234"
    assert "# Service Recovery Agent 修复说明" in markdown
    assert "## Bug Summary" in markdown
    assert "ZeroDivisionError" in markdown
    assert "demo_service/app.py:31" in markdown
    assert "GET /divide?x=0" in markdown
    assert "GET /bug" in markdown
    assert "## Root Cause" in markdown
    assert "unsafe_divide directly divides" in markdown
    assert "## Fix Summary" in markdown
    assert "controlled input error" in markdown
    assert "## Validation Evidence" in markdown
    assert "python -m pytest -q -m not seed_failure" in markdown
    assert "Contract validation: `PASS`" in markdown
    assert "Counterfactual validation: `PASS`" in markdown
    assert "Adversarial validation: `PASS`" in markdown
    assert "## Repair Attempts" in markdown
    assert "Attempt 1" in markdown
    assert "bug_shortcut" in markdown
    assert "## Success Criteria" in markdown
    assert "GET /divide?x=4 returns 200 with result 25.0." in markdown
    assert "## Failure Criteria" in markdown
    assert "Zero denominator input leaks ZeroDivisionError as a 500 response." in markdown
    assert "## Rollback Plan" in markdown
    assert "git revert abc1234" in markdown
    assert "No Git branch, commit, push, GitHub PR, or Feishu message was created" in markdown


def test_build_pr_description_markdown_returns_markdown_only() -> None:
    markdown = build_pr_description_markdown(
        _success_result(),
        decision=_decision(),
        rollback_command="git revert HEAD",
    )

    assert markdown.startswith("# Service Recovery Agent 修复说明")
    assert "Command: `git revert HEAD`" in markdown
    assert "## Success Criteria" in markdown
    assert "## Failure Criteria" in markdown


def test_build_review_artifact_for_failed_result_is_human_review_report_and_truncates_logs() -> None:
    feedback = RepairFailureFeedback(
        failed_stage="validation",
        failed_probes=["python -m pytest tests/test_demo.py"],
        summary="Validation failed after patch apply.",
        prompt_feedback="Use validation stderr to revise patch.",
    )
    result = RecoveryRunResult(
        status="FAIL_ROLLED_BACK",
        reason="Validation failed; snapshot restored.",
        rolled_back=True,
        attempt_number=1,
        max_repair_attempts=1,
        traceback_event=_traceback_event(),
        diagnosis=_diagnosis(),
        proposal=_proposal(),
        preview=_preview(),
        validation_result=_validation("FAIL", noisy_failure=True),
        failure_feedback=feedback,
    )

    artifact = build_review_artifact(result, decision=_decision(DecisionAction.BLOCKED))
    markdown = artifact.markdown

    assert "FAIL_ROLLED_BACK" in markdown
    assert "rolled_back=`True`" in markdown
    assert "Ordinary validation: `FAIL`" in markdown
    assert "Failure excerpt" in markdown
    assert "[redacted-env]" in markdown
    assert ".env" not in markdown
    assert "x" * 300 not in markdown
    assert "## Rollback Plan" in markdown
    assert "The current recovery run already rolled back" in markdown
    assert "## Failure Criteria" in markdown
    assert "Any configured ordinary validation command fails." in markdown


def test_review_criteria_and_rollback_plan_are_structured() -> None:
    result = _success_result()

    criteria = build_review_criteria(result)
    rollback_plan = build_rollback_plan(result)

    assert any("/bug" in item for item in criteria.success_criteria)
    assert any("contract validation" in item.lower() for item in criteria.success_criteria)
    assert any("adversarial probe" in item.lower() for item in criteria.failure_criteria)
    assert rollback_plan.command is None
    assert "git revert <commit_sha>" in " ".join(rollback_plan.notes)


def test_review_artifact_to_dict_contains_markdown_and_structured_sections() -> None:
    artifact = build_review_artifact(_success_result(), decision=_decision())
    data = artifact.to_dict()

    assert data["title"].startswith("Service Recovery Agent 修复说明")
    assert data["markdown"].startswith("# Service Recovery Agent 修复说明")
    assert data["rollback_plan"]["strategy"]
    assert data["criteria"]["success_criteria"]
    assert data["criteria"]["failure_criteria"]
