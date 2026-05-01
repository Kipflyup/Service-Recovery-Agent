from __future__ import annotations

from service_recovery_agent.decision import DecisionAction, DecisionReason, DecisionResult
from service_recovery_agent.failure_feedback import RepairFailureFeedback
from service_recovery_agent.feishu_cards import (
    build_failure_recovery_card,
    build_recovery_result_card,
    build_report_only_recovery_card,
    build_success_recovery_card,
    classify_recovery_card,
)
from service_recovery_agent.fix_planner import FixProposal
from service_recovery_agent.recovery_agent import RecoveryRunResult
from service_recovery_agent.validator import ValidationCommandResult, ValidationResult


def _proposal(*, change_type: str = "A", risk_level: str = "low", confidence: str = "high") -> FixProposal:
    return FixProposal(
        root_cause="unsafe_divide did not handle zero denominator.",
        fix_strategy="Return a controlled 400 response for zero denominator while preserving happy path.",
        change_type=change_type,
        risk_level=risk_level,
        confidence=confidence,
        contract_constraints=["Preserve /divide?x=4."],
        patch_draft="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
        tests_to_run=["python -m pytest -q"],
        manual_review_notes="synthetic proposal for card tests",
    )


def _decision(action: DecisionAction, *, can_auto_deliver: bool = False) -> DecisionResult:
    return DecisionResult(
        action=action,
        confidence_score=0.91 if action is DecisionAction.CREATE_PR_READY else 0.55,
        risk_level="low" if action is DecisionAction.CREATE_PR_READY else "medium",
        can_auto_deliver=can_auto_deliver,
        reasons=[
            DecisionReason(
                code="synthetic_reason",
                severity="info" if action is DecisionAction.CREATE_PR_READY else "warning",
                message=f"Synthetic decision reason for {action.value}.",
                evidence=["unit-test"],
            )
        ],
        summary=f"Decision summary: {action.value}.",
    )


def _pass_validation() -> ValidationResult:
    return ValidationResult(
        status="PASS",
        command_results=[
            ValidationCommandResult(
                command=["python", "-c", "print('ok')"],
                return_code=0,
                stdout="ok\n",
                stderr="",
                duration_seconds=0.01,
            )
        ],
    )


def _card_text(card: dict[str, object]) -> str:
    chunks: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "content" and isinstance(item, str):
                    chunks.append(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(card)
    return "\n".join(chunks)


def test_build_success_recovery_card_contains_review_ready_summary() -> None:
    result = RecoveryRunResult(
        status="PASS",
        reason="Patch applied; validation, contract validation, and adversarial validation passed.",
        rolled_back=False,
        attempt_number=1,
        max_repair_attempts=1,
        proposal=_proposal(),
        validation_result=_pass_validation(),
    )
    decision = _decision(DecisionAction.CREATE_PR_READY, can_auto_deliver=False)

    card = build_success_recovery_card(
        result,
        decision=decision,
        service_name="demo-service",
        repo="Service-Recovery-Agent",
        branch="recovery/demo-fix",
        pr_url="https://example.test/pr/1",
        rollback_command="git revert HEAD",
    )
    text = _card_text(card)

    assert card["header"]["template"] == "green"
    assert "Service Recovery Agent：修复已通过门禁" in text
    assert "卡片类型：" in text
    assert "success" in text
    assert "CREATE_PR_READY" in text
    assert "demo-service" in text
    assert "Validation" in text
    assert "PASS" in text
    assert "查看 PR" in text
    assert card["config"]["wide_screen_mode"] is True


def test_build_report_only_recovery_card_contains_decision_reasons_and_next_step() -> None:
    result = RecoveryRunResult(
        status="PREVIEW_ONLY",
        reason="Patch passed safety review and dry-run. Pass apply=True to modify files.",
        rolled_back=False,
        attempt_number=1,
        max_repair_attempts=1,
        proposal=_proposal(change_type="C", risk_level="medium", confidence="medium"),
    )
    decision = _decision(DecisionAction.REPORT_ONLY)

    card = build_report_only_recovery_card(result, decision=decision, service_name="demo-service")
    text = _card_text(card)

    assert card["header"]["template"] == "yellow"
    assert "卡片类型：" in text
    assert "report_only" in text
    assert "REPORT_ONLY" in text
    assert "决策原因" in text
    assert "synthetic_reason" in text
    assert "下一步建议" in text
    assert "人工检查诊断" in text


def test_build_failure_recovery_card_includes_required_failure_fields() -> None:
    feedback = RepairFailureFeedback(
        failed_stage="contract_validation",
        failed_probes=["python -m pytest tests/test_contract.py"],
        summary="Contract validation failed: previously passing command regressed.",
        prompt_feedback="Patch regressed a contract command; preserve previously passing behavior.",
        details={"regression_count": 1},
    )
    result = RecoveryRunResult(
        status="FAIL_ROLLED_BACK",
        reason="Contract validation failed; snapshot restored.",
        rolled_back=True,
        attempt_number=1,
        max_repair_attempts=2,
        proposal=_proposal(),
        failure_feedback=feedback,
    )
    decision = _decision(DecisionAction.RETRY_REPAIR)

    card = build_failure_recovery_card(result, decision=decision, service_name="demo-service")
    text = _card_text(card)

    assert card["header"]["template"] == "red"
    assert "卡片类型：" in text
    assert "failure" in text
    assert "failed_stage" in text
    assert "contract_validation" in text
    assert "failed_probes" in text
    assert "python -m pytest tests/test_contract.py" in text
    assert "summary" in text
    assert "previously passing command regressed" in text
    assert "rolled_back" in text
    assert "True" in text
    assert "下一步建议" in text
    assert "下一轮 revise patch" in text
    assert "Failure feedback 摘要" in text


def test_build_recovery_result_card_routes_to_expected_card_types() -> None:
    success = RecoveryRunResult(
        status="PASS",
        reason="ok",
        rolled_back=False,
        attempt_number=1,
        max_repair_attempts=1,
        proposal=_proposal(),
        validation_result=_pass_validation(),
    )
    preview = RecoveryRunResult(
        status="PREVIEW_ONLY",
        reason="preview only",
        rolled_back=False,
        attempt_number=1,
        max_repair_attempts=1,
        proposal=_proposal(),
    )
    failure = RecoveryRunResult(
        status="FAIL_ROLLED_BACK",
        reason="failed",
        rolled_back=True,
        attempt_number=1,
        max_repair_attempts=1,
        failure_feedback=RepairFailureFeedback(
            failed_stage="validation",
            failed_probes=["python -m pytest"],
            summary="validation failed",
            prompt_feedback="fix validation failure",
        ),
    )

    assert classify_recovery_card(success, decision=_decision(DecisionAction.CREATE_PR_READY)) == "success"
    assert classify_recovery_card(preview, decision=_decision(DecisionAction.REPORT_ONLY)) == "report_only"
    assert classify_recovery_card(failure, decision=_decision(DecisionAction.BLOCKED)) == "failure"

    assert "success" in _card_text(build_recovery_result_card(success, decision=_decision(DecisionAction.CREATE_PR_READY)))
    assert "report_only" in _card_text(build_recovery_result_card(preview, decision=_decision(DecisionAction.REPORT_ONLY)))
    assert "failure" in _card_text(build_recovery_result_card(failure, decision=_decision(DecisionAction.BLOCKED)))


def test_build_recovery_result_card_is_dry_run_json_without_network_payload() -> None:
    result = RecoveryRunResult(
        status="PASS",
        reason="ok",
        rolled_back=False,
        attempt_number=1,
        max_repair_attempts=1,
        proposal=_proposal(),
    )

    card = build_recovery_result_card(result, decision=_decision(DecisionAction.CREATE_PR_READY))

    assert set(card) == {"config", "header", "elements"}
    assert "receive_id" not in card
    assert "msg_type" not in card
    assert "content" not in card
    assert isinstance(card["elements"], list)
