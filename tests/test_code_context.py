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
from service_recovery_agent.code_context import build_code_context, format_code_context_report  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402


pytestmark = pytest.mark.seed_failure


def test_build_code_context_for_demo_zero_division(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    app = create_app(log_path=log_path, testing=True)

    response = app.test_client().get("/divide?x=0")
    assert response.status_code == 500
    for handler in app.logger.handlers:
        handler.flush()

    event = read_latest_traceback(log_path)
    assert event is not None

    context = build_code_context(event, project_root=PROJECT_ROOT)

    assert context.crash_file_relative == "demo_service/app.py"
    assert context.crash_function is not None
    assert context.crash_function.name == "unsafe_divide"
    assert "return numerator / denominator" in context.crash_function.source
    assert context.crash_line_code is not None
    assert context.crash_line_code.strip() == "return numerator / denominator"

    callers = {call.caller_function for call in context.incoming_calls}
    assert "create_app.divide" in callers
    assert "create_app.bug" in callers
    assert context.impact.direct_call_count >= 2
    assert context.impact.route_handler_count >= 2
    assert context.impact.project_traceback_frame_count == 2
    assert context.impact.risk_level in {"medium", "high"}


def test_format_code_context_report_contains_key_sections(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    app = create_app(log_path=log_path, testing=True)

    app.test_client().get("/divide?x=0")
    for handler in app.logger.handlers:
        handler.flush()

    event = read_latest_traceback(log_path)
    assert event is not None
    context = build_code_context(event, project_root=PROJECT_ROOT)
    report = format_code_context_report(context)

    assert "Code Context Report" in report
    assert "unsafe_divide" in report
    assert "Direct Callers" in report
    assert "Impact Assessment" in report
