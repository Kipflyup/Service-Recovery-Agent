from __future__ import annotations

from service_recovery_agent.adversarial_validator import AdversarialValidationResult
from service_recovery_agent.contract_validator import ContractValidationResult
from service_recovery_agent.counterfactual_validator import CounterfactualValidationResult
from service_recovery_agent.decision import (
    DecisionAction,
    evaluate_change_type_policy,
    evaluate_recovery_decision,
    format_decision_result,
)
from service_recovery_agent.fix_planner import FixProposal
from service_recovery_agent.patch_safety import PatchSafetyFinding, PatchSafetyReview
from service_recovery_agent.patcher import PatchApplyResult, PatchDryRunResult, PatchPreviewResult
from service_recovery_agent.recovery_agent import RecoveryRunResult
from service_recovery_agent.validator import ValidationCommandResult, ValidationResult


_DEFAULT = object()


def _proposal(
    *,
    change_type: str = "A",
    risk_level: str = "low",
    confidence: str = "high",
) -> FixProposal:
    return FixProposal(
        root_cause="root cause",
        fix_strategy="strategy",
        change_type=change_type,
        risk_level=risk_level,
        confidence=confidence,
        patch_draft="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
    )


def _preview(status: str = "PASS") -> PatchPreviewResult:
    finding = PatchSafetyFinding(
        check="synthetic",
        status=status,
        message=f"synthetic {status.lower()} finding",
        evidence=["demo_service/app.py"] if status != "PASS" else [],
    )
    review = PatchSafetyReview(
        status=status,
        summary=f"synthetic {status.lower()} review",
        modified_files=["demo_service/app.py"],
        allowed_files=["demo_service/app.py"],
        findings=[finding],
    )
    dry_run = None
    if status != "FAIL":
        dry_run = PatchDryRunResult(
            command=["patch", "--dry-run"],
            return_code=0,
            stdout="",
            stderr="",
            can_apply=True,
        )
    return PatchPreviewResult(
        safety_review=review,
        dry_run=dry_run,
        patch_text="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
    )


def _apply(applied: bool = True) -> PatchApplyResult:
    return PatchApplyResult(
        command=["patch"],
        return_code=0 if applied else 1,
        stdout="",
        stderr="" if applied else "apply failed",
        applied=applied,
    )


def _validation(passed: bool = True) -> ValidationResult:
    return ValidationResult(
        status="PASS" if passed else "FAIL",
        command_results=[
            ValidationCommandResult(
                command=["pytest", "-q"],
                return_code=0 if passed else 1,
                stdout="ok" if passed else "failed",
                stderr="" if passed else "assertion failed",
                duration_seconds=0.01,
            )
        ],
    )


def _adversarial(passed: bool = True) -> AdversarialValidationResult:
    return AdversarialValidationResult(
        status="PASS" if passed else "FAIL",
        summary="all probes passed" if passed else "bug_shortcut failed",
        probe_results=[],
    )


def _contract(passed: bool = True) -> ContractValidationResult:
    return ContractValidationResult(
        status="PASS" if passed else "FAIL",
        summary="contract ok" if passed else "contract regression",
        command_baselines=[],
    )


def _counterfactual(passed: bool = True) -> CounterfactualValidationResult:
    return CounterfactualValidationResult(
        status="PASS" if passed else "FAIL",
        summary="counterfactual ok" if passed else "counterfactual failed",
        before_patch_result=_adversarial(False),
        after_patch_result=_adversarial(passed),
    )


def _result(
    *,
    status: str = "PASS",
    rolled_back: bool = False,
    proposal: FixProposal | None = None,
    preview: PatchPreviewResult | None = None,
    validation: ValidationResult | None | object = _DEFAULT,
    contract: ContractValidationResult | None | object = _DEFAULT,
    adversarial: AdversarialValidationResult | None | object = _DEFAULT,
    counterfactual: CounterfactualValidationResult | None | object = _DEFAULT,
    applied: bool = True,
    attempt_number: int = 1,
    max_repair_attempts: int = 1,
) -> RecoveryRunResult:
    return RecoveryRunResult(
        status=status,
        reason="synthetic",
        rolled_back=rolled_back,
        attempt_number=attempt_number,
        max_repair_attempts=max_repair_attempts,
        proposal=proposal if proposal is not None else _proposal(),
        preview=preview if preview is not None else _preview(),
        apply_result=_apply(applied),
        validation_result=_validation() if validation is _DEFAULT else validation,
        contract_result=None if contract is _DEFAULT else contract,
        adversarial_result=_adversarial() if adversarial is _DEFAULT else adversarial,
        counterfactual_result=None if counterfactual is _DEFAULT else counterfactual,
    )


def test_decision_create_pr_ready_for_low_risk_type_a_pass() -> None:
    decision = evaluate_recovery_decision(_result(), allow_auto_delivery=True)

    assert decision.action is DecisionAction.CREATE_PR_READY
    assert decision.can_auto_deliver is True
    assert decision.confidence_score >= 0.8
    assert any(reason.code == "change_type_a" for reason in decision.reasons)

    report = format_decision_result(decision)
    assert "Decision Result" in report
    assert "CREATE_PR_READY" in report


def test_decision_blocks_validation_failure() -> None:
    decision = evaluate_recovery_decision(_result(validation=_validation(False)))

    assert decision.action is DecisionAction.BLOCKED
    assert decision.can_auto_deliver is False
    assert any(reason.code == "validation_failed" for reason in decision.reasons)


def test_decision_blocks_adversarial_failure_without_retry_budget() -> None:
    decision = evaluate_recovery_decision(_result(adversarial=_adversarial(False)))

    assert decision.action is DecisionAction.BLOCKED
    assert any(reason.code == "adversarial_failed" for reason in decision.reasons)


def test_decision_escalates_change_type_d() -> None:
    result = _result(proposal=_proposal(change_type="D", risk_level="medium", confidence="high"))
    decision = evaluate_recovery_decision(result)

    assert decision.action is DecisionAction.ESCALATE_HUMAN
    assert any(reason.code == "change_type_d" for reason in decision.reasons)


def test_decision_escalates_change_type_e() -> None:
    result = _result(proposal=_proposal(change_type="E", risk_level="medium", confidence="high"))
    decision = evaluate_recovery_decision(result)

    assert decision.action is DecisionAction.ESCALATE_HUMAN
    assert any(reason.code == "change_type_e" for reason in decision.reasons)


def test_decision_reports_only_for_change_type_c_until_contract_validation_exists() -> None:
    result = _result(proposal=_proposal(change_type="C", risk_level="medium", confidence="high"))
    decision = evaluate_recovery_decision(result)

    assert decision.action is DecisionAction.REPORT_ONLY
    assert any(reason.code == "change_type_c_requires_contract" for reason in decision.reasons)


def test_decision_reports_only_for_safety_warning() -> None:
    result = _result(preview=_preview("WARN"), proposal=_proposal(change_type="B"))
    decision = evaluate_recovery_decision(result)

    assert decision.action is DecisionAction.REPORT_ONLY
    assert any(reason.code == "safety_warn" for reason in decision.reasons)


def test_decision_reports_only_when_adversarial_missing() -> None:
    result = _result(adversarial=None)
    decision = evaluate_recovery_decision(result)

    assert decision.action is DecisionAction.REPORT_ONLY
    assert any(reason.code == "adversarial_missing" for reason in decision.reasons)


def test_decision_blocks_contract_failure() -> None:
    decision = evaluate_recovery_decision(_result(contract=_contract(False)))

    assert decision.action is DecisionAction.BLOCKED
    assert any(reason.code == "contract_failed" for reason in decision.reasons)


def test_decision_blocks_counterfactual_failure() -> None:
    decision = evaluate_recovery_decision(_result(counterfactual=_counterfactual(False)))

    assert decision.action is DecisionAction.BLOCKED
    assert any(reason.code == "counterfactual_failed" for reason in decision.reasons)


def test_decision_records_contract_and_counterfactual_pass_reasons() -> None:
    decision = evaluate_recovery_decision(
        _result(
            contract=_contract(True),
            counterfactual=_counterfactual(True),
        )
    )

    assert decision.action is DecisionAction.CREATE_PR_READY
    assert any(reason.code == "contract_pass" for reason in decision.reasons)
    assert any(reason.code == "counterfactual_pass" for reason in decision.reasons)


def test_change_type_policy_warns_when_proposal_missing() -> None:
    reasons = evaluate_change_type_policy(
        None,
        safety_status="PASS",
        validation_passed=True,
        adversarial_passed=True,
    )

    assert [reason.code for reason in reasons] == ["proposal_missing"]
    assert reasons[0].severity == "warning"


def test_decision_to_dict_is_json_ready() -> None:
    data = evaluate_recovery_decision(_result()).to_dict()

    assert data["action"] == "CREATE_PR_READY"
    assert isinstance(data["confidence_score"], float)
    assert data["reasons"]
