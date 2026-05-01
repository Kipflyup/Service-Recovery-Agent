from __future__ import annotations

import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.fix_planner import build_fix_prompt  # noqa: E402
from service_recovery_agent.invariant_analyzer import (  # noqa: E402
    analyze_hidden_invariants,
    format_hidden_invariant_evidence,
)
from service_recovery_agent.traceback_parser import StackFrame, TracebackEvent  # noqa: E402


def _context_for_source(
    tmp_path: Path,
    source: str,
    *,
    line_marker: str,
    function: str,
    exception_type: str = "ZeroDivisionError",
    exception_message: str = "division by zero",
):
    app_path = tmp_path / "app.py"
    module_source = textwrap.dedent(source).lstrip()
    app_path.write_text(module_source, encoding="utf-8")

    source_lines = module_source.splitlines()
    line_number = next(
        index
        for index, line in enumerate(source_lines, start=1)
        if line_marker in line
    )
    crash_code = source_lines[line_number - 1].strip()
    event = TracebackEvent(
        exception_type=exception_type,
        exception_message=exception_message,
        frames=[StackFrame(file="app.py", line_number=line_number, function=function, code=crash_code)],
        raw=(
            "Traceback (most recent call last):\n"
            f'  File "app.py", line {line_number}, in {function}\n'
            f"    {crash_code}\n"
            f"{exception_type}: {exception_message}"
        ),
    )
    return build_code_context(event, project_root=tmp_path)


def test_analyze_hidden_invariants_from_return_docstring_name_callers_and_tests(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(
        textwrap.dedent(
            """
            def test_divide_happy_path(client):
                response = client.get("/divide?x=4")
                assert response.status_code == 200
                assert response.get_json()["result"] == 25.0
            """
        ).lstrip(),
        encoding="utf-8",
    )
    context = _context_for_source(
        tmp_path,
        """
        app = object()

        def unsafe_divide(numerator: float, denominator: float) -> float:
            \"\"\"Return a non-negative quotient for valid positive inputs.\"\"\"
            return numerator / denominator

        @app.get("/divide")
        def divide():
            result = unsafe_divide(100.0, 4.0)
            return {"result": result}, 200
        """,
        line_marker="return numerator / denominator",
        function="unsafe_divide",
    )

    assessment = analyze_hidden_invariants(context)

    kinds = {candidate.kind for candidate in assessment.invariant_candidates}
    assert "return_type_contract" in kinds
    assert "return_non_negative" in kinds
    assert "numeric_result_contract" in kinds
    assert "route_handler_contract" in kinds
    assert "caller_return_shape_contract" in kinds
    assert "tested_route_status_contract" in kinds
    assert "tested_response_shape_contract" in kinds
    assert assessment.propagation_depth >= 1
    assert assessment.hidden_risk_level in {"medium", "high"}
    assert any(candidate.source == "test" for candidate in assessment.invariant_candidates)

    data = assessment.to_dict()
    assert data["invariant_candidates"]
    assert data["hidden_risk_level"] == assessment.hidden_risk_level

    report = format_hidden_invariant_evidence(assessment)
    assert "Hidden Invariant Evidence" in report
    assert "advisory prompt/review evidence only" in report
    assert "not a hard validation gate" in report
    assert "/divide" in report
    assert "status_code == 200" in report


def test_seed_failure_tests_are_not_treated_as_happy_path_contracts(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(
        textwrap.dedent(
            """
            import pytest

            pytestmark = pytest.mark.seed_failure

            @pytest.mark.seed_failure
            def test_divide_seed_failure(client):
                response = client.get("/divide?x=0")
                assert response.status_code == 500
            """
        ).lstrip(),
        encoding="utf-8",
    )
    context = _context_for_source(
        tmp_path,
        """
        app = object()

        def unsafe_divide(numerator: float, denominator: float) -> float:
            return numerator / denominator

        @app.get("/divide")
        def divide():
            return unsafe_divide(100.0, 0.0), 200
        """,
        line_marker="return numerator / denominator",
        function="unsafe_divide",
    )

    assessment = analyze_hidden_invariants(context)

    test_evidence = [
        evidence
        for candidate in assessment.invariant_candidates
        if candidate.source == "test"
        for evidence in candidate.evidence
    ]
    assert not test_evidence
    assert not any("500" in evidence for evidence in test_evidence)


def test_function_name_can_create_boolean_review_hint_without_hard_gate(tmp_path: Path) -> None:
    context = _context_for_source(
        tmp_path,
        """
        def is_enabled(config):
            return config["enabled"]
        """,
        line_marker='return config["enabled"]',
        function="is_enabled",
        exception_type="KeyError",
        exception_message="'enabled'",
    )

    assessment = analyze_hidden_invariants(context)

    assert assessment.hidden_risk_level == "low"
    assert assessment.propagation_depth == 0
    candidate = next(
        candidate for candidate in assessment.invariant_candidates
        if candidate.kind == "boolean_return_contract"
    )
    assert candidate.source == "name"
    assert candidate.confidence == "low"
    assert "predicate" in candidate.description


def test_no_evidence_returns_low_risk_empty_candidates(tmp_path: Path) -> None:
    context = _context_for_source(
        tmp_path,
        """
        def f(values):
            return values[0]
        """,
        line_marker="return values[0]",
        function="f",
        exception_type="IndexError",
        exception_message="list index out of range",
    )

    assessment = analyze_hidden_invariants(context)

    assert assessment.hidden_risk_level == "low"
    assert assessment.propagation_depth == 0
    assert assessment.transitive_call_count == 0
    assert assessment.invariant_candidates == []


def test_build_fix_prompt_can_include_hidden_invariants_as_review_hint(tmp_path: Path) -> None:
    context = _context_for_source(
        tmp_path,
        """
        def unsafe_divide(numerator: float, denominator: float) -> float:
            return numerator / denominator
        """,
        line_marker="return numerator / denominator",
        function="unsafe_divide",
    )
    assessment = analyze_hidden_invariants(context)

    prompt = build_fix_prompt(context, hidden_invariants=assessment)

    assert "Hidden Invariant Evidence" in prompt
    assert "prompt evidence / review hint" in prompt
    assert "hard gate" in prompt
    assert '"hidden_invariants"' in prompt
    assert '"return_type_contract"' in prompt
