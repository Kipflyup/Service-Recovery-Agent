from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402
from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.fix_planner import (  # noqa: E402
    build_fix_prompt,
    format_fix_proposal_report,
    parse_fix_proposal,
    propose_fix,
)
from service_recovery_agent.llm_client import MockLLMClient  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402


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


def test_build_fix_prompt_contains_context_and_safety_constraints(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)

    prompt = build_fix_prompt(context)

    assert "unsafe_divide" in prompt
    assert "return numerator / denominator" in prompt
    assert "不要修改函数签名" in prompt
    assert "不要在 JSON 响应中引入 NaN" in prompt
    assert "patch_draft" in prompt


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
