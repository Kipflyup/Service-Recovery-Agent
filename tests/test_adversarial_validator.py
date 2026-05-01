from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from werkzeug.exceptions import BadRequest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import demo_service.app as app_module  # noqa: E402
from service_recovery_agent.adversarial_validator import (  # noqa: E402
    FAIL,
    PASS,
    ProbeCase,
    format_adversarial_validation_result,
    generate_probe_cases,
    run_probe_cases,
)
from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.fault_diagnosis import FaultDiagnosis, diagnose_fault  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402


def _demo_diagnosis(tmp_path: Path) -> FaultDiagnosis:
    log_path = tmp_path / "seed.log"
    app = app_module.create_app(log_path=log_path, testing=True)
    response = app.test_client().get("/divide?x=0")
    assert response.status_code == 500
    for handler in app.logger.handlers:
        handler.flush()

    event = read_latest_traceback(log_path)
    assert event is not None
    context = build_code_context(event, project_root=PROJECT_ROOT)
    return diagnose_fault(context)


@pytest.mark.seed_failure
def test_generate_probe_cases_for_arithmetic_boundary(tmp_path: Path) -> None:
    diagnosis = _demo_diagnosis(tmp_path)

    cases = generate_probe_cases(diagnosis)

    names = {case.name for case in cases}
    paths = {case.path for case in cases}
    assert {
        "happy_path",
        "zero_boundary",
        "negative_denominator",
        "missing_query",
        "bug_shortcut",
        "repeated_zero",
    }.issubset(names)
    assert "/divide?x=0" in paths
    assert "/divide?x=-1" in paths
    assert "/bug" in paths
    assert next(case for case in cases if case.name == "repeated_zero").repeat == 3


@pytest.mark.seed_failure
def test_buggy_app_fails_adversarial_validation(tmp_path: Path) -> None:
    diagnosis = _demo_diagnosis(tmp_path)
    app = app_module.create_app(log_path=tmp_path / "buggy_probe.log", testing=True)

    result = run_probe_cases(app, generate_probe_cases(diagnosis))

    assert result.status == FAIL
    failed_names = {probe.case.name for probe in result.probe_results if not probe.passed}
    assert {"zero_boundary", "bug_shortcut", "repeated_zero"}.issubset(failed_names)


@pytest.mark.seed_failure
def test_patched_app_passes_adversarial_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diagnosis = _demo_diagnosis(tmp_path)

    def patched_unsafe_divide(numerator: float, denominator: float) -> float:
        if denominator == 0:
            raise BadRequest("denominator must not be zero")
        return numerator / denominator

    monkeypatch.setattr(app_module, "unsafe_divide", patched_unsafe_divide)
    app = app_module.create_app(log_path=tmp_path / "patched_probe.log", testing=True)

    result = run_probe_cases(app, generate_probe_cases(diagnosis))

    assert result.status == PASS
    assert result.passed is True
    assert all(probe.passed for probe in result.probe_results)


@pytest.mark.seed_failure
def test_repeated_probe_records_all_attempts(tmp_path: Path) -> None:
    diagnosis = _demo_diagnosis(tmp_path)
    repeated_case = next(case for case in generate_probe_cases(diagnosis) if case.name == "repeated_zero")
    app = app_module.create_app(log_path=tmp_path / "repeat_probe.log", testing=True)

    result = run_probe_cases(app, [repeated_case])

    assert result.status == FAIL
    assert len(result.probe_results) == 1
    assert len(result.probe_results[0].attempts) == 3
    assert all(attempt.status_code == 500 for attempt in result.probe_results[0].attempts)


@pytest.mark.seed_failure
def test_forbidden_status_500_fails_probe(tmp_path: Path) -> None:
    app = app_module.create_app(log_path=tmp_path / "forbidden_probe.log", testing=True)
    case = ProbeCase(
        name="forbid_500",
        method="GET",
        path="/divide?x=0",
        forbidden_statuses={500},
    )

    result = run_probe_cases(app, [case])

    assert result.status == FAIL
    attempt = result.probe_results[0].attempts[0]
    assert attempt.passed is False
    assert "forbidden" in attempt.reason


@pytest.mark.seed_failure
def test_adversarial_result_json_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diagnosis = _demo_diagnosis(tmp_path)

    def patched_unsafe_divide(numerator: float, denominator: float) -> float:
        if denominator == 0:
            raise BadRequest("denominator must not be zero")
        return numerator / denominator

    monkeypatch.setattr(app_module, "unsafe_divide", patched_unsafe_divide)
    app = app_module.create_app(log_path=tmp_path / "json_probe.log", testing=True)
    result = run_probe_cases(app, generate_probe_cases(diagnosis))

    dumped = json.dumps(result.to_dict(), ensure_ascii=False)
    report = format_adversarial_validation_result(result)

    assert '"status": "PASS"' in dumped
    assert "Adversarial Validation" in report
    assert "PASS" in report
    assert "zero_boundary" in report
    assert "bug_shortcut" in report
