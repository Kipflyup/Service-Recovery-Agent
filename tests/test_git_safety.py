from __future__ import annotations

from service_recovery_agent.decision import DecisionAction, DecisionReason, DecisionResult
from service_recovery_agent.fix_planner import FixProposal
from service_recovery_agent.git_safety import (
    BLOCKED,
    READY,
    build_git_delivery_plan,
    format_git_delivery_plan,
    is_denied_path,
    scan_patch_for_secrets,
)
from service_recovery_agent.patch_safety import PatchSafetyFinding, PatchSafetyReview
from service_recovery_agent.patcher import PatchDryRunResult, PatchPreviewResult
from service_recovery_agent.recovery_agent import RecoveryRunResult
from service_recovery_agent.traceback_parser import StackFrame, TracebackEvent
from service_recovery_agent.validator import ValidationCommandResult, ValidationResult


def _proposal(patch_text: str | None = None) -> FixProposal:
    return FixProposal(
        root_cause="unsafe_divide did not guard zero denominator.",
        fix_strategy="Guard zero denominator while preserving happy path.",
        change_type="A",
        risk_level="low",
        confidence="high",
        contract_constraints=["Preserve /divide?x=4."],
        patch_draft=patch_text or "--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
        tests_to_run=["python -m pytest -q -m 'not seed_failure'"],
        manual_review_notes="unit-test proposal",
    )


def _decision(action: DecisionAction = DecisionAction.CREATE_PR_READY) -> DecisionResult:
    return DecisionResult(
        action=action,
        confidence_score=0.91 if action is DecisionAction.CREATE_PR_READY else 0.50,
        risk_level="low" if action is DecisionAction.CREATE_PR_READY else "medium",
        can_auto_deliver=False,
        reasons=[DecisionReason(code="unit_test", severity="info", message="test")],
        summary=f"Decision summary: {action.value}",
    )


def _preview(*, files: list[str], allowed: list[str] | None = None, patch_text: str | None = None) -> PatchPreviewResult:
    return PatchPreviewResult(
        safety_review=PatchSafetyReview(
            status="PASS",
            summary="Patch draft passed static safety checks.",
            modified_files=files,
            allowed_files=allowed or files,
            findings=[PatchSafetyFinding(check="unit", status="PASS", message="ok")],
        ),
        dry_run=PatchDryRunResult(
            command=["patch", "-p1", "--dry-run"],
            return_code=0,
            stdout="patching file\n",
            stderr="",
            can_apply=True,
        ),
        patch_text=patch_text or "--- a/demo_service/app.py\n+++ b/demo_service/app.py\n",
    )


def _validation() -> ValidationResult:
    return ValidationResult(
        status="PASS",
        command_results=[
            ValidationCommandResult(
                command=["python", "-m", "pytest", "-q"],
                return_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.01,
            )
        ],
    )


def _traceback_event() -> TracebackEvent:
    frame = StackFrame(
        file="/repo/demo_service/app.py",
        line_number=31,
        function="unsafe_divide",
        code="return numerator / denominator",
    )
    return TracebackEvent(
        exception_type="ZeroDivisionError",
        exception_message="division by zero",
        frames=[frame],
        raw="Traceback ...",
    )


def _ready_result(*, preview: PatchPreviewResult | None = None, proposal: FixProposal | None = None) -> RecoveryRunResult:
    return RecoveryRunResult(
        status="PASS",
        reason="Patch applied; validation passed.",
        rolled_back=False,
        attempt_number=1,
        max_repair_attempts=1,
        traceback_event=_traceback_event(),
        proposal=proposal or _proposal(),
        preview=preview or _preview(files=["demo_service/app.py"]),
        validation_result=_validation(),
    )


def test_build_git_delivery_plan_ready_uses_verified_stage_files_and_worktree() -> None:
    plan = build_git_delivery_plan(
        _ready_result(),
        _decision(),
        base_ref="main",
        branch_prefix="agent/fix",
        use_worktree=True,
        worktree_root="/tmp/sra-worktrees",
    )

    assert plan.status == READY
    assert plan.ready is True
    assert plan.stage_files == ["demo_service/app.py"]
    assert plan.branch == "agent/fix/zerodivisionerror"
    assert plan.worktree_path == "/tmp/sra-worktrees/agent-fix-zerodivisionerror"
    assert plan.would_push is False
    assert plan.would_create_pr is False
    assert plan.can_create_pr is True
    assert "Fix ZeroDivisionError" in plan.commit_message
    assert plan.to_dict()["ready"] is True


def test_build_git_delivery_plan_blocks_non_create_pr_decision() -> None:
    plan = build_git_delivery_plan(
        _ready_result(),
        _decision(DecisionAction.REPORT_ONLY),
    )

    assert plan.status == BLOCKED
    assert any("not CREATE_PR_READY" in reason for reason in plan.denied_reasons)
    assert plan.can_create_pr is False


def test_build_git_delivery_plan_blocks_sensitive_stage_files() -> None:
    preview = _preview(files=[".env"], allowed=[".env"])
    plan = build_git_delivery_plan(_ready_result(preview=preview), _decision())

    assert plan.status == BLOCKED
    assert plan.blocked_files == [".env"]
    assert any("denied/sensitive" in reason for reason in plan.denied_reasons)


def test_build_git_delivery_plan_blocks_secret_like_patch_text() -> None:
    patch_text = "--- a/demo_service/app.py\n+++ b/demo_service/app.py\n+FEISHU_APP_SECRET='secret'\n"
    preview = _preview(files=["demo_service/app.py"], patch_text=patch_text)
    plan = build_git_delivery_plan(
        _ready_result(preview=preview, proposal=_proposal(patch_text)),
        _decision(),
    )

    assert plan.status == BLOCKED
    assert any("possible secret" in reason for reason in plan.denied_reasons)
    assert "FEISHU_APP_SECRET" in plan.denied_reasons[-1]


def test_format_git_delivery_plan_mentions_dry_run_only() -> None:
    report = format_git_delivery_plan(build_git_delivery_plan(_ready_result(), _decision()))

    assert "# Git Delivery Plan" in report
    assert "Dry-run only" in report
    assert "demo_service/app.py" in report


def test_denied_path_and_secret_scan_helpers() -> None:
    assert is_denied_path("logs/app.log") is True
    assert is_denied_path("demo_service/app.py") is False
    assert scan_patch_for_secrets("+DOUBAO_API_KEY=abc\n+normal = 1") == ["DOUBAO_API_KEY"]
