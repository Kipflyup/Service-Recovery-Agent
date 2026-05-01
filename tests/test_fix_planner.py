from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402
from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.failure_feedback import RepairFailureFeedback  # noqa: E402
from service_recovery_agent.fault_diagnosis import diagnose_fault  # noqa: E402
from service_recovery_agent.fix_planner import (  # noqa: E402
    build_fix_prompt,
    format_fix_proposal_report,
    parse_fix_proposal,
    propose_fix,
)
from service_recovery_agent.git_diff_correlation import (  # noqa: E402
    FOUND,
    DiffCorrelationCandidate,
    DiffHunk,
    GitDiffCorrelationResult,
    RecentCommit,
)
from service_recovery_agent.llm_client import MockLLMClient  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402
from service_recovery_agent.sbfl import PASS, SBFLResult, SuspiciousLine  # noqa: E402


def _demo_context(tmp_path: Path):
    log_path = tmp_path / "app.log"
    app = create_app(log_path=log_path, testing=True)
    response = app.test_client().get("/divide?x=0")
    assert response.status_code == 500
    for handler in app.logger.handlers:
        handler.flush()

    event = read_latest_traceback(log_path)
    assert event is not None
    return build_code_context(event, project_root=PROJECT_ROOT)


class RecordingLLMClient(MockLLMClient):
    def __init__(self) -> None:
        self.prompt = ""
        self.system = ""

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.prompt = prompt
        self.system = system or ""
        return super().complete(prompt, system=system)


def _git_diff_correlation_result() -> GitDiffCorrelationResult:
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
        summary="Recent diff modified crash site.",
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
                reason="crash file and line overlap recent diff",
                evidence=[
                    "crash file `demo_service/app.py` was modified in the recent diff",
                    "crash line 31 overlaps 1 diff hunk(s)",
                    "crash function `unsafe_divide` appears in diff hunk text",
                ],
                hunks=[hunk],
            )
        ],
    )


def _sbfl_result() -> SBFLResult:
    return SBFLResult(
        status=PASS,
        summary="SBFL ranked suspicious lines from one failing and one passing pytest run.",
        total_failed=1,
        total_passed=1,
        suspicious_lines=[
            SuspiciousLine(
                file="demo_service/app.py",
                line_number=31,
                score=1.0,
                failed_and_covered=1,
                passed_and_covered=0,
                total_failed=1,
                source_line="return numerator / denominator",
            ),
            SuspiciousLine(
                file="demo_service/app.py",
                line_number=30,
                score=0.7071,
                failed_and_covered=1,
                passed_and_covered=1,
                total_failed=1,
                source_line="# denominator == 0 currently raises ZeroDivisionError.",
            ),
        ],
    )


@pytest.mark.seed_failure
def test_build_fix_prompt_contains_context_and_safety_constraints(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)

    prompt = build_fix_prompt(context)

    assert "unsafe_divide" in prompt
    assert "return numerator / denominator" in prompt
    assert "不要修改函数签名" in prompt
    assert "不要在 JSON 响应中引入 NaN" in prompt
    assert "patch_draft" in prompt


@pytest.mark.seed_failure
def test_build_fix_prompt_includes_fault_diagnosis(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    diagnosis = diagnose_fault(context)

    prompt = build_fix_prompt(context, diagnosis=diagnosis)

    assert "Deterministic Fault Diagnosis" in prompt
    assert "Fault Diagnosis" in prompt
    assert "arithmetic_boundary" in prompt
    assert "denominator == 0" in prompt
    assert "query parameter denominator or x" in prompt
    assert "avoid_special_float" in prompt
    assert "web_api_error_semantics" in prompt
    assert '"fault_diagnosis"' in prompt


@pytest.mark.seed_failure
def test_build_fix_prompt_includes_git_diff_correlation_evidence(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    diagnosis = diagnose_fault(context)
    git_diff_correlation = _git_diff_correlation_result()

    prompt = build_fix_prompt(
        context,
        diagnosis=diagnosis,
        git_diff_correlation=git_diff_correlation,
    )

    assert "Git Diff Correlation Evidence" in prompt
    assert "score=0.90" in prompt
    assert "abc1234" in prompt
    assert "introduce divide bug" in prompt
    assert "crash line 31 overlaps" in prompt
    assert '"git_diff_correlation"' in prompt
    assert '"status": "FOUND"' in prompt


@pytest.mark.seed_failure
def test_build_fix_prompt_includes_sbfl_result_evidence(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    diagnosis = diagnose_fault(context)

    prompt = build_fix_prompt(
        context,
        diagnosis=diagnosis,
        sbfl_result=_sbfl_result(),
    )

    assert "SBFL Result" in prompt
    assert "Top Suspicious Lines" in prompt
    assert "demo_service/app.py:31" in prompt
    assert "score=1.0000" in prompt
    assert "return numerator / denominator" in prompt
    assert "测试覆盖频谱 suspiciousness evidence" in prompt
    assert '"sbfl_result"' in prompt
    assert '"suspicious_lines"' in prompt


@pytest.mark.seed_failure
def test_mock_llm_generates_parseable_fix_proposal(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)

    proposal = propose_fix(context, MockLLMClient())

    assert proposal.provider == "mock"
    assert proposal.model == "mock-fix-planner"
    assert "ZeroDivisionError" in proposal.root_cause
    assert "unsafe_divide" in proposal.patch_draft
    assert "denominator == 0" in proposal.patch_draft

    report = format_fix_proposal_report(proposal)
    assert "Fix Proposal" in report
    assert "Patch Draft" in report


@pytest.mark.seed_failure
def test_propose_fix_sends_fault_diagnosis_to_llm(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    diagnosis = diagnose_fault(context)
    client = RecordingLLMClient()

    proposal = propose_fix(context, client, diagnosis=diagnosis)

    assert proposal.provider == "mock"
    assert "Deterministic Fault Diagnosis" in client.prompt
    assert "arithmetic_boundary" in client.prompt
    assert "denominator == 0" in client.prompt
    assert "avoid_special_float" in client.prompt
    assert "web_api_error_semantics" in client.prompt


@pytest.mark.seed_failure
def test_propose_fix_sends_git_diff_correlation_to_llm(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    diagnosis = diagnose_fault(context)
    client = RecordingLLMClient()

    proposal = propose_fix(
        context,
        client,
        diagnosis=diagnosis,
        git_diff_correlation=_git_diff_correlation_result(),
    )

    assert proposal.provider == "mock"
    assert "Git Diff Correlation Evidence" in client.prompt
    assert "recent-change root-cause evidence" in client.prompt
    assert "demo_service/app.py" in client.prompt
    assert "score=0.90" in client.prompt
    assert '"git_diff_correlation"' in client.prompt


@pytest.mark.seed_failure
def test_propose_fix_sends_sbfl_result_to_llm(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    diagnosis = diagnose_fault(context)
    client = RecordingLLMClient()

    proposal = propose_fix(
        context,
        client,
        diagnosis=diagnosis,
        sbfl_result=_sbfl_result(),
    )

    assert proposal.provider == "mock"
    assert "SBFL Result" in client.prompt
    assert "Top Suspicious Lines" in client.prompt
    assert "demo_service/app.py:31" in client.prompt
    assert "score=1.0000" in client.prompt
    assert '"sbfl_result"' in client.prompt


@pytest.mark.seed_failure
def test_build_fix_prompt_includes_failure_feedback_for_next_repair(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    diagnosis = diagnose_fault(context)
    feedback = RepairFailureFeedback(
        failed_stage="adversarial_validation",
        failed_probes=["bug_shortcut"],
        summary="bug_shortcut returned 500",
        prompt_feedback=(
            "Patch passed normal validation but failed adversarial probe:\n"
            "- probe: bug_shortcut\n"
            "- request: GET /bug\n"
            "- expected: 400 or 422\n"
            "- actual: 500 internal_server_error\n"
            "Please revise the patch to handle all callers of unsafe_divide, including /bug."
        ),
    )

    prompt = build_fix_prompt(context, diagnosis=diagnosis, failure_feedback=feedback)

    assert "Previous Repair Failure Feedback" in prompt
    assert "bug_shortcut" in prompt
    assert "GET /bug" in prompt
    assert "500 internal_server_error" in prompt
    assert '"failure_feedback"' in prompt
    assert "manual_review_notes" in prompt


def test_parse_fix_proposal_accepts_fenced_json() -> None:
    raw = """```json
{
  "root_cause": "cause",
  "fix_strategy": "strategy",
  "change_type": "B",
  "risk_level": "medium",
  "confidence": "high",
  "contract_constraints": ["keep signature"],
  "patch_draft": "--- a/x.py\\n+++ b/x.py",
  "tests_to_run": ["pytest"],
  "manual_review_notes": "review"
}
```"""

    proposal = parse_fix_proposal(raw, provider="mock", model="test")

    assert proposal.root_cause == "cause"
    assert proposal.contract_constraints == ["keep signature"]
    assert proposal.tests_to_run == ["pytest"]
