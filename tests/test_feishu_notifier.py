from __future__ import annotations

from typing import Any

from service_recovery_agent.decision import DecisionAction, DecisionReason, DecisionResult
from service_recovery_agent.feishu import FeishuAPIError
from service_recovery_agent.feishu_notifier import (
    format_feishu_notification_result,
    send_recovery_result_card,
)
from service_recovery_agent.fix_planner import FixProposal
from service_recovery_agent.recovery_agent import RecoveryRunResult
from service_recovery_agent.validator import ValidationCommandResult, ValidationResult


class RecordingFeishuClient:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response or {"code": 0, "data": {"message_id": "om_test", "chat_id": "oc_test"}}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def send_interactive_card(
        self,
        card: dict[str, Any],
        *,
        receiver_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "card": card,
                "receiver_id": receiver_id,
                "receive_id_type": receive_id_type,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


def _proposal() -> FixProposal:
    return FixProposal(
        root_cause="unsafe_divide directly divides by zero denominator.",
        fix_strategy="Guard zero denominator and return controlled API error.",
        change_type="A",
        risk_level="low",
        confidence="high",
        contract_constraints=["Preserve happy path."],
        patch_draft="--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
        tests_to_run=["python -m pytest -q"],
        manual_review_notes="unit test proposal",
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


def _decision(action: DecisionAction) -> DecisionResult:
    return DecisionResult(
        action=action,
        confidence_score=0.91 if action is DecisionAction.CREATE_PR_READY else 0.50,
        risk_level="low" if action is DecisionAction.CREATE_PR_READY else "medium",
        can_auto_deliver=False,
        reasons=[
            DecisionReason(
                code="unit_test",
                severity="info",
                message="Synthetic decision for notifier tests.",
            )
        ],
        summary=f"Decision summary: {action.value}",
    )


def test_send_recovery_result_card_sends_terminal_card_with_metadata() -> None:
    result = RecoveryRunResult(
        status="PASS",
        reason="Patch applied; validation passed.",
        rolled_back=False,
        attempt_number=1,
        max_repair_attempts=1,
        proposal=_proposal(),
        validation_result=_pass_validation(),
    )
    decision = _decision(DecisionAction.CREATE_PR_READY)
    client = RecordingFeishuClient()

    notification = send_recovery_result_card(
        result,
        decision,
        client=client,
        service_name="demo-service",
        repo="Service-Recovery-Agent",
        branch="agent/fix-demo",
        pr_url="https://example.test/pr/1",
        rollback_command="git revert HEAD",
        receiver_id="ou_test",
        receive_id_type="open_id",
    )

    assert notification.attempted is True
    assert notification.sent is True
    assert notification.card_kind == "success"
    assert notification.message_id == "om_test"
    assert notification.chat_id == "oc_test"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["receiver_id"] == "ou_test"
    assert call["receive_id_type"] == "open_id"
    assert call["card"]["header"]["template"] == "green"
    assert "demo-service" in str(call["card"])
    assert "https://example.test/pr/1" in str(call["card"])


def test_send_recovery_result_card_returns_structured_api_failure() -> None:
    result = RecoveryRunResult(
        status="FAIL_ROLLED_BACK",
        reason="Validation failed; snapshot restored.",
        rolled_back=True,
        attempt_number=2,
        max_repair_attempts=2,
        proposal=_proposal(),
    )
    decision = _decision(DecisionAction.BLOCKED)
    client = RecordingFeishuClient(
        error=FeishuAPIError(
            "business error",
            code=999,
            response={"code": 999, "msg": "no permission"},
        )
    )

    notification = send_recovery_result_card(result, decision, client=client)

    assert notification.attempted is True
    assert notification.sent is False
    assert notification.card_kind == "failure"
    assert "business error" in (notification.error or "")
    assert notification.response == {"code": 999, "msg": "no permission"}


def test_format_feishu_notification_result_is_concise() -> None:
    notification = send_recovery_result_card(
        RecoveryRunResult(status="PREVIEW_ONLY", reason="preview", rolled_back=False),
        _decision(DecisionAction.REPORT_ONLY),
        client=RecordingFeishuClient(),
    )

    report = format_feishu_notification_result(notification)

    assert "# Feishu Notification" in report
    assert "Sent: True" in report
    assert "Card kind: report_only" in report
    assert "om_test" in report
