from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402
from service_recovery_agent import recovery_agent as recovery_agent_module  # noqa: E402
from service_recovery_agent.adversarial_validator import (  # noqa: E402
    AdversarialValidationResult,
    ProbeAttemptResult,
    ProbeCase,
    ProbeResult,
)
from service_recovery_agent.recovery_agent import (  # noqa: E402
    RecoveryAgent,
    RecoveryAgentConfig,
    format_recovery_run_result,
)
from service_recovery_agent.fix_planner import FixProposal  # noqa: E402
from service_recovery_agent.git_diff_correlation import (  # noqa: E402
    FOUND,
    DiffCorrelationCandidate,
    DiffHunk,
    GitDiffCorrelationResult,
    RecentCommit,
)
from service_recovery_agent.llm_client import MockLLMClient  # noqa: E402
from service_recovery_agent.patcher import restore_snapshot, snapshot_files  # noqa: E402


def _write_seed_traceback(log_path: Path) -> None:
    app = create_app(log_path=log_path, testing=True)
    response = app.test_client().get("/divide?x=0")
    assert response.status_code == 500
    for handler in app.logger.handlers:
        handler.flush()


def _route_only_zero_guard_patch() -> str:
    return (
        "--- a/demo_service/app.py\n"
        "+++ b/demo_service/app.py\n"
        "@@ -70,6 +70,12 @@\n"
        "         # http://127.0.0.1:5001/divide?x=0\n"
        "         denominator = _float_arg(\"denominator\", fallback_name=\"x\", default=1.0)\n"
        " \n"
        "+        if denominator == 0:\n"
        "+            return {\n"
        "+                \"error\": \"invalid_request\",\n"
        "+                \"message\": \"denominator must not be zero\",\n"
        "+            }, 400\n"
        "+\n"
        "         result = unsafe_divide(numerator, denominator)\n"
        "         return {\n"
        "             \"numerator\": numerator,\n"
    )


class FeedbackAwareLLMClient:
    provider = "mock"
    model = "feedback-aware-mock"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        if "## Previous Repair Failure Feedback" in prompt:
            return MockLLMClient().complete(prompt, system=system)
        return json.dumps(
            {
                "root_cause": "The divide route passes zero denominator to unsafe_divide.",
                "fix_strategy": "Guard only the /divide route zero input.",
                "change_type": "A",
                "risk_level": "medium",
                "confidence": "medium",
                "contract_constraints": [
                    "Preserve /divide?x=4.",
                    "Convert /divide?x=0 into 400.",
                ],
                "tests_to_run": ["python scripts/validate_demo_repair.py"],
                "manual_review_notes": "This intentionally incomplete patch is used by tests.",
                "patch_draft": _route_only_zero_guard_patch(),
            },
            ensure_ascii=False,
        )


def _dangerous_patch_proposal() -> FixProposal:
    return FixProposal(
        root_cause="synthetic unsafe patch",
        fix_strategy="intentionally unsafe for safety feedback coverage",
        change_type="B",
        risk_level="high",
        confidence="low",
        contract_constraints=[],
        tests_to_run=[],
        manual_review_notes="test fixture",
        patch_draft=(
            "--- a/demo_service/app.py\n"
            "+++ b/demo_service/app.py\n"
            "@@ -23,6 +23,7 @@\n"
            " DEFAULT_LOG_PATH = Path(\"logs/app.log\")\n"
            " \n"
            " \n"
            "+import os; os.system('echo unsafe')\n"
            " def unsafe_divide(numerator: float, denominator: float) -> float:\n"
        ),
    )


def _bad_hunk_patch() -> str:
    return (
        "--- a/demo_service/app.py\n"
        "+++ b/demo_service/app.py\n"
        "@@ -999,3 +999,3 @@\n"
        "-definitely_missing_line_for_dry_run_feedback\n"
        "+replacement_line_for_dry_run_feedback\n"
    )


class DryRunRetryLLMClient:
    provider = "mock"
    model = "dry-run-retry-mock"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return json.dumps(
                {
                    "root_cause": "The first patch has stale hunk context.",
                    "fix_strategy": "Return a patch with a hunk that cannot apply.",
                    "change_type": "A",
                    "risk_level": "low",
                    "confidence": "medium",
                    "contract_constraints": [],
                    "tests_to_run": [],
                    "manual_review_notes": "dry-run retry fixture",
                    "patch_draft": _bad_hunk_patch(),
                },
                ensure_ascii=False,
            )
        return MockLLMClient().complete(prompt, system=system)


def _source_guard_contract_command() -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import sys; "
            "source = Path('demo_service/app.py').read_text(); "
            "sys.exit(1 if 'InvalidDivideInput' in source else 0)"
        ),
    ]


def _passing_adversarial_validation(*args: object, **kwargs: object) -> AdversarialValidationResult:
    return AdversarialValidationResult(
        status="PASS",
        summary="synthetic adversarial pass",
        probe_results=[],
    )


def _synthetic_git_diff_correlation() -> GitDiffCorrelationResult:
    hunk = DiffHunk(
        commit_sha="abc123456789",
        file="demo_service/app.py",
        old_start=28,
        old_count=7,
        new_start=28,
        new_count=9,
        header="@@ -28,7 +28,9 @@ def unsafe_divide(numerator, denominator):",
        lines=["+    return numerator / denominator"],
    )
    return GitDiffCorrelationResult(
        status=FOUND,
        summary="Synthetic recent diff evidence.",
        crash_file="demo_service/app.py",
        crash_line_number=31,
        crash_function="unsafe_divide",
        inspected_commits=[
            RecentCommit(
                sha="abc123456789",
                short_sha="abc1234",
                timestamp=1760000000,
                subject="introduce divide bug",
            )
        ],
        changed_files=["demo_service/app.py"],
        candidates=[
            DiffCorrelationCandidate(
                file="demo_service/app.py",
                commit_sha="abc123456789",
                short_sha="abc1234",
                commit_subject="introduce divide bug",
                score=0.90,
                reason="synthetic correlation",
                evidence=["crash line 31 overlaps 1 diff hunk(s)"],
                hunks=[hunk],
            )
        ],
    )


@pytest.mark.seed_failure
def test_recovery_agent_run_once_preview_only_with_mock(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_seed_traceback(log_path)
    agent = RecoveryAgent(
        RecoveryAgentConfig(
            project_root=PROJECT_ROOT,
            log_path=log_path,
            provider="mock",
        )
    )

    result = agent.run_once(apply=False)

    assert result.status == "PREVIEW_ONLY"
    assert result.rolled_back is False
    assert result.can_create_pr is False
    assert result.traceback_event is not None
    assert result.context is not None
    assert result.context.crash_file_relative == "demo_service/app.py"
    assert result.diagnosis is not None
    assert result.diagnosis.classification.category == "arithmetic_boundary"
    assert "denominator" in result.diagnosis.suspected_variables
    assert result.proposal is not None
    assert result.preview is not None
    assert result.preview.status == "PASS"
    assert result.preview.dry_run is not None
    assert result.preview.dry_run.can_apply is True
    assert result.apply_result is None
    assert result.validation_result is None
    assert result.adversarial_result is None

    report = format_recovery_run_result(result)
    assert "Recovery Agent Run" in report
    assert "PREVIEW_ONLY" in report
    assert "Fault Diagnosis Summary" in report
    assert "arithmetic_boundary" in report
    assert "Patch Preview" in report


@pytest.mark.seed_failure
def test_recovery_agent_injects_git_diff_correlation_into_llm_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "app.log"
    _write_seed_traceback(log_path)
    client = FeedbackAwareLLMClient()
    monkeypatch.setattr(
        recovery_agent_module,
        "correlate_traceback_with_recent_diff",
        lambda *args, **kwargs: _synthetic_git_diff_correlation(),
    )
    agent = RecoveryAgent(
        RecoveryAgentConfig(
            project_root=PROJECT_ROOT,
            log_path=log_path,
        ),
        llm_client=client,
    )

    result = agent.run_once(apply=False)

    assert result.status == "PREVIEW_ONLY"
    assert result.git_diff_correlation is not None
    assert result.git_diff_correlation.status == FOUND
    assert result.to_dict()["git_diff_correlation"]["status"] == FOUND
    assert len(client.prompts) == 1
    assert "Git Diff Correlation Evidence" in client.prompts[0]
    assert "score=0.90" in client.prompts[0]
    assert '"git_diff_correlation"' in client.prompts[0]
    report = format_recovery_run_result(result)
    assert "Git Diff Correlation Summary" in report


@pytest.mark.seed_failure
def test_recovery_agent_returns_safety_failure_feedback_for_preview_abort(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_seed_traceback(log_path)
    agent = RecoveryAgent(
        RecoveryAgentConfig(
            project_root=PROJECT_ROOT,
            log_path=log_path,
            provider="mock",
        )
    )

    result = agent.run_once(
        proposal=_dangerous_patch_proposal(),
        apply=False,
        max_repair_attempts=2,
    )

    assert result.status == "ABORTED"
    assert result.attempt_number == 1
    assert result.max_repair_attempts == 2
    assert len(result.repair_attempts) == 1
    assert result.preview is not None
    assert result.preview.status == "FAIL"
    assert result.failure_feedback is not None
    assert result.failure_feedback.failed_stage == "safety_review"
    assert "dangerous_operations" in result.failure_feedback.failed_probes
    assert "Do not bypass dangerous_operations" in result.failure_feedback.prompt_feedback

    report = format_recovery_run_result(result)
    assert "Repair Failure Feedback" in report
    assert "safety_review" in report


@pytest.mark.seed_failure
def test_recovery_agent_retries_dry_run_failure_feedback_before_apply(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_seed_traceback(log_path)
    client = DryRunRetryLLMClient()
    agent = RecoveryAgent(
        RecoveryAgentConfig(
            project_root=PROJECT_ROOT,
            log_path=log_path,
        ),
        llm_client=client,
    )

    result = agent.run_once(
        apply=False,
        max_repair_attempts=2,
    )

    assert result.status == "PREVIEW_ONLY"
    assert result.attempt_number == 2
    assert len(result.repair_attempts) == 2
    assert result.repair_attempts[0].status == "ABORTED"
    assert result.repair_attempts[0].failure_feedback is not None
    assert result.repair_attempts[0].failure_feedback.failed_stage == "patch_dry_run"
    assert result.repair_attempts[0].rolled_back is False
    assert result.repair_attempts[1].status == "PREVIEW_ONLY"
    assert result.repair_attempts[1].used_failure_feedback is not None
    assert result.repair_attempts[1].used_failure_feedback.failed_stage == "patch_dry_run"
    assert len(client.prompts) == 2
    assert "Previous Repair Failure Feedback" in client.prompts[1]
    assert "patch dry-run" in client.prompts[1]


@pytest.mark.seed_failure
def test_recovery_agent_apply_runs_adversarial_validation_gate(tmp_path: Path) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
                provider="mock",
            )
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_adversarial_validation=True,
            adversarial_log_path=tmp_path / "adversarial.log",
        )

        assert result.status == "PASS"
        assert result.rolled_back is False
        assert result.can_create_pr is True
        assert result.apply_result is not None
        assert result.apply_result.applied is True
        assert result.validation_result is not None
        assert result.validation_result.passed is True
        assert result.adversarial_result is not None
        assert result.adversarial_result.status == "PASS"
        assert result.adversarial_result.passed is True

        data = result.to_dict()
        assert data["adversarial_validation"]["status"] == "PASS"

        report = format_recovery_run_result(result)
        assert "Adversarial Validation" in report
        assert "zero_boundary" in report
        assert "bug_shortcut" in report
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_runs_contract_validation_gate(tmp_path: Path) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
                provider="mock",
            )
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_contract_validation=True,
        )

        assert result.status == "PASS"
        assert result.contract_result is not None
        assert result.contract_result.status == "PASS"
        assert result.contract_result.passed is True
        assert result.to_dict()["contract_validation"]["status"] == "PASS"
        report = format_recovery_run_result(result)
        assert "Contract Validation" in report
        assert "No contract regression detected" in report
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_validation_failure_produces_feedback_and_rolls_back(tmp_path: Path) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    original_app_source = snapshot.files["demo_service/app.py"]
    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
                provider="mock",
            )
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[
                [
                    sys.executable,
                    "-c",
                    "import sys; print('synthetic validation failure'); sys.exit(7)",
                ]
            ],
        )

        assert result.status == "FAIL_ROLLED_BACK"
        assert result.rolled_back is True
        assert result.validation_result is not None
        assert result.validation_result.passed is False
        assert result.failure_feedback is not None
        assert result.failure_feedback.failed_stage == "validation"
        assert result.failure_feedback.failed_probes[0].startswith(sys.executable)
        assert "synthetic validation failure" in result.failure_feedback.prompt_feedback
        assert "Do not remove or weaken tests" in result.failure_feedback.prompt_feedback
        assert (PROJECT_ROOT / "demo_service" / "app.py").read_text(encoding="utf-8") == original_app_source
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_contract_failure_produces_feedback_and_rolls_back(tmp_path: Path) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    original_app_source = snapshot.files["demo_service/app.py"]
    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
                provider="mock",
            )
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_contract_validation=True,
            contract_validation_commands=[_source_guard_contract_command()],
        )

        assert result.status == "FAIL_ROLLED_BACK"
        assert result.rolled_back is True
        assert result.contract_result is not None
        assert result.contract_result.status == "FAIL"
        assert result.failure_feedback is not None
        assert result.failure_feedback.failed_stage == "contract_validation"
        assert result.failure_feedback.details["regression_count"] == 1
        assert "Contract status: FAIL" in result.failure_feedback.prompt_feedback
        assert "previously passing behavior remains passing" in result.failure_feedback.prompt_feedback
        assert (PROJECT_ROOT / "demo_service" / "app.py").read_text(encoding="utf-8") == original_app_source
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_runs_counterfactual_validation_gate(tmp_path: Path) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
                provider="mock",
            )
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_counterfactual_validation=True,
            adversarial_log_path=tmp_path / "counterfactual.log",
        )

        assert result.status == "PASS"
        assert result.adversarial_result is not None
        assert result.adversarial_result.status == "PASS"
        assert result.counterfactual_result is not None
        assert result.counterfactual_result.status == "PASS"
        assert result.counterfactual_result.before_patch_result is not None
        assert result.counterfactual_result.before_patch_result.status == "FAIL"
        assert result.counterfactual_result.after_patch_result is not None
        assert result.counterfactual_result.after_patch_result.status == "PASS"
        assert result.to_dict()["counterfactual_validation"]["status"] == "PASS"
        report = format_recovery_run_result(result)
        assert "Counterfactual Validation" in report
        assert "buggy state failed" in report
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_counterfactual_failure_produces_feedback_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    original_app_source = snapshot.files["demo_service/app.py"]
    monkeypatch.setattr(
        recovery_agent_module,
        "validate_demo_app_after_repair",
        _passing_adversarial_validation,
    )

    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
                provider="mock",
            )
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_counterfactual_validation=True,
            adversarial_log_path=tmp_path / "counterfactual.log",
        )

        assert result.status == "FAIL_ROLLED_BACK"
        assert result.rolled_back is True
        assert result.adversarial_result is not None
        assert result.adversarial_result.status == "PASS"
        assert result.counterfactual_result is not None
        assert result.counterfactual_result.status == "FAIL"
        assert result.failure_feedback is not None
        assert result.failure_feedback.failed_stage == "counterfactual_validation"
        assert result.failure_feedback.failed_probes == ["before_patch_expected_fail"]
        assert "before_patch adversarial status: PASS" in result.failure_feedback.prompt_feedback
        assert "do not claim the bug is fixed" in result.failure_feedback.prompt_feedback
        assert (PROJECT_ROOT / "demo_service" / "app.py").read_text(encoding="utf-8") == original_app_source
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_rolls_back_when_adversarial_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    original_app_source = snapshot.files["demo_service/app.py"]

    def fail_adversarial_validation(*args: object, **kwargs: object) -> AdversarialValidationResult:
        failed_case = ProbeCase(
            name="bug_shortcut",
            method="GET",
            path="/bug",
            expected_statuses={400, 422},
            forbidden_statuses={500},
            forbidden_json={
                "error": {"internal_server_error"},
                "exception_type": {"ZeroDivisionError"},
            },
            description="Alternate caller must be handled.",
        )
        return AdversarialValidationResult(
            status="FAIL",
            summary="synthetic adversarial failure: bug_shortcut returned 500",
            probe_results=[
                ProbeResult(
                    case=failed_case,
                    attempts=[
                        ProbeAttemptResult(
                            status_code=500,
                            response_json={
                                "error": "internal_server_error",
                                "exception_type": "ZeroDivisionError",
                            },
                            passed=False,
                            reason="status 500 is forbidden",
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr(
        recovery_agent_module,
        "validate_demo_app_after_repair",
        fail_adversarial_validation,
    )

    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
                provider="mock",
            )
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_adversarial_validation=True,
            adversarial_log_path=tmp_path / "adversarial.log",
        )

        assert result.status == "FAIL_ROLLED_BACK"
        assert result.rolled_back is True
        assert result.can_create_pr is False
        assert result.adversarial_result is not None
        assert result.adversarial_result.status == "FAIL"
        assert result.failure_feedback is not None
        assert result.failure_feedback.failed_stage == "adversarial_validation"
        assert result.failure_feedback.failed_probes == ["bug_shortcut"]
        assert "GET /bug" in result.failure_feedback.prompt_feedback
        assert "status 500" in result.failure_feedback.prompt_feedback
        assert "all callers" in result.failure_feedback.prompt_feedback
        assert "Adversarial validation failed" in result.reason
        assert result.to_dict()["failure_feedback"]["failed_probes"] == ["bug_shortcut"]
        report = format_recovery_run_result(result)
        assert "Repair Failure Feedback" in report
        assert "Prompt Feedback For Next Repair Attempt" in report
        assert (PROJECT_ROOT / "demo_service" / "app.py").read_text(encoding="utf-8") == original_app_source
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_revises_patch_with_failure_feedback(tmp_path: Path) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    client = FeedbackAwareLLMClient()
    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
            ),
            llm_client=client,
        )

        result = agent.run_once(
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_adversarial_validation=True,
            adversarial_log_path=tmp_path / "adversarial.log",
            max_repair_attempts=2,
        )

        assert result.status == "PASS"
        assert result.can_create_pr is True
        assert result.attempt_number == 2
        assert result.max_repair_attempts == 2
        assert len(result.repair_attempts) == 2

        first_attempt = result.repair_attempts[0]
        second_attempt = result.repair_attempts[1]
        assert first_attempt.status == "FAIL_ROLLED_BACK"
        assert first_attempt.failure_feedback is not None
        assert "bug_shortcut" in first_attempt.failure_feedback.failed_probes
        assert second_attempt.status == "PASS"
        assert second_attempt.used_failure_feedback is not None
        assert "bug_shortcut" in second_attempt.used_failure_feedback.failed_probes

        assert len(client.prompts) == 2
        assert "## Previous Repair Failure Feedback" not in client.prompts[0]
        assert "## Previous Repair Failure Feedback" in client.prompts[1]
        assert "GET /bug" in client.prompts[1]

        data = result.to_dict()
        assert data["repair_attempts"][0]["failure_feedback"]["failed_probes"] == ["bug_shortcut"]
        assert data["repair_attempts"][1]["used_failure_feedback"]["failed_probes"] == ["bug_shortcut"]

        report = format_recovery_run_result(result)
        assert "Attempt: 2/2" in report
        assert "Used feedback from failed probes: bug_shortcut" in report
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


@pytest.mark.seed_failure
def test_recovery_agent_does_not_revise_fixed_patch_file_source(tmp_path: Path) -> None:
    snapshot = snapshot_files(PROJECT_ROOT, ["demo_service/app.py"])
    client = FeedbackAwareLLMClient()
    try:
        log_path = tmp_path / "app.log"
        _write_seed_traceback(log_path)
        patch_file = tmp_path / "route_only_fix.patch"
        patch_file.write_text(_route_only_zero_guard_patch(), encoding="utf-8")
        agent = RecoveryAgent(
            RecoveryAgentConfig(
                project_root=PROJECT_ROOT,
                log_path=log_path,
            ),
            llm_client=client,
        )

        result = agent.run_once(
            patch_file=patch_file,
            apply=True,
            validation_commands=[[sys.executable, "-c", "print('validation ok')"]],
            run_adversarial_validation=True,
            adversarial_log_path=tmp_path / "adversarial.log",
            max_repair_attempts=2,
        )

        assert result.status == "FAIL_ROLLED_BACK"
        assert result.can_create_pr is False
        assert result.attempt_number == 1
        assert result.max_repair_attempts == 2
        assert len(result.repair_attempts) == 1
        assert result.failure_feedback is not None
        assert result.failure_feedback.failed_probes == ["bug_shortcut"]
        assert client.prompts == []
    finally:
        restore_snapshot(PROJECT_ROOT, snapshot)
        importlib.reload(importlib.import_module("demo_service.app"))


def test_recovery_agent_reports_no_traceback(tmp_path: Path) -> None:
    agent = RecoveryAgent(
        RecoveryAgentConfig(
            project_root=PROJECT_ROOT,
            log_path=tmp_path / "missing.log",
            provider="mock",
        )
    )

    result = agent.run_once()

    assert result.status == "NO_TRACEBACK"
    assert result.traceback_event is None
    assert result.diagnosis is None
    assert result.preview is None
    assert result.adversarial_result is None
