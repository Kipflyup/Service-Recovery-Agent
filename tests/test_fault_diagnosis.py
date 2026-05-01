from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402
from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.fault_diagnosis import (  # noqa: E402
    classify_error,
    diagnose_fault,
    format_fault_diagnosis_report,
)
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402
from service_recovery_agent.traceback_parser import StackFrame, TracebackEvent  # noqa: E402


def _demo_diagnosis(tmp_path: Path):
    log_path = tmp_path / "app.log"
    app = create_app(log_path=log_path, testing=True)
    response = app.test_client().get("/divide?x=0")
    assert response.status_code == 500
    for handler in app.logger.handlers:
        handler.flush()

    event = read_latest_traceback(log_path)
    assert event is not None
    context = build_code_context(event, project_root=PROJECT_ROOT)
    return diagnose_fault(context)


def _diagnosis_for_source(
    tmp_path: Path,
    source: str,
    *,
    exception_type: str,
    exception_message: str,
    line_number: int,
    function: str = "handler",
    code: str | None = None,
):
    module_path = tmp_path / "app.py"
    module_source = textwrap.dedent(source).lstrip()
    module_path.write_text(module_source, encoding="utf-8")
    source_lines = module_source.splitlines()
    crash_code = code or source_lines[line_number - 1].strip()

    event = TracebackEvent(
        exception_type=exception_type,
        exception_message=exception_message,
        frames=[
            StackFrame(
                file="app.py",
                line_number=line_number,
                function=function,
                code=crash_code,
            )
        ],
        raw=(
            "Traceback (most recent call last):\n"
            f'  File "app.py", line {line_number}, in {function}\n'
            f"    {crash_code}\n"
            f"{exception_type}: {exception_message}"
        ),
    )
    context = build_code_context(event, project_root=tmp_path)
    return diagnose_fault(context)


def test_classify_error_uses_first_rule_based_taxonomy() -> None:
    zero_division = classify_error("ZeroDivisionError")
    assert zero_division.category == "arithmetic_boundary"
    assert zero_division.confidence == "high"
    assert "division" in zero_division.strategy_hint.lower()

    key_error = classify_error("builtins.KeyError")
    assert key_error.category == "missing_key"

    unknown = classify_error("CustomRuntimeFailure")
    assert unknown.category == "unknown_runtime_error"
    assert unknown.confidence == "low"


def test_diagnose_key_error_identifies_mapping_key_and_constraints(tmp_path: Path) -> None:
    diagnosis = _diagnosis_for_source(
        tmp_path,
        """
        def handler(payload):
            return payload["user_id"]
        """,
        exception_type="KeyError",
        exception_message="'user_id'",
        line_number=2,
    )

    assert diagnosis.classification.category == "missing_key"
    assert diagnosis.primary_candidate is not None
    assert diagnosis.primary_candidate.suspiciousness == 0.95
    assert "payload" in diagnosis.suspected_variables
    assert any("AST node: Subscript" in evidence for evidence in diagnosis.primary_candidate.evidence)
    assert "Mapping expression: payload" in diagnosis.primary_candidate.evidence
    assert "Key expression: 'user_id'" in diagnosis.primary_candidate.evidence
    assert diagnosis.trigger_conditions == ["key 'user_id' missing from payload"]

    flow_sources = [flow.source for flow in diagnosis.variable_flows]
    assert any("function parameter `payload`" in source for source in flow_sources)

    constraint_kinds = {constraint.kind for constraint in diagnosis.repair_constraints}
    assert "validate_required_keys" in constraint_kinds
    assert "preserve_payload_schema" in constraint_kinds
    assert "controlled_missing_key_error" in constraint_kinds

    hints = "\n".join(diagnosis.adversarial_hints)
    assert "missing the required key" in hints
    assert "empty payload" in hints
    assert "required key present" in hints


def test_diagnose_attribute_error_identifies_base_object_and_attribute(tmp_path: Path) -> None:
    diagnosis = _diagnosis_for_source(
        tmp_path,
        """
        def handler(user):
            return user.name
        """,
        exception_type="AttributeError",
        exception_message="'NoneType' object has no attribute 'name'",
        line_number=2,
    )

    assert diagnosis.classification.category == "null_or_type_mismatch"
    assert diagnosis.primary_candidate is not None
    assert "user" in diagnosis.suspected_variables
    assert any("AST node: Attribute" in evidence for evidence in diagnosis.primary_candidate.evidence)
    assert "Base object: user" in diagnosis.primary_candidate.evidence
    assert "Attribute: name" in diagnosis.primary_candidate.evidence
    assert diagnosis.trigger_conditions == ["user is None or lacks attribute name"]

    constraint_kinds = {constraint.kind for constraint in diagnosis.repair_constraints}
    assert "validate_none_or_type_before_attribute" in constraint_kinds
    assert "preserve_attribute_contract" in constraint_kinds

    hints = "\n".join(diagnosis.adversarial_hints)
    assert "None/null" in hints
    assert "lacks the required attribute" in hints
    assert "valid object" in hints


def test_diagnose_type_error_identifies_operation_operands(tmp_path: Path) -> None:
    diagnosis = _diagnosis_for_source(
        tmp_path,
        """
        def handler(value):
            return value + 1
        """,
        exception_type="TypeError",
        exception_message="can only concatenate str (not \"int\") to str",
        line_number=2,
    )

    assert diagnosis.classification.category == "type_mismatch"
    assert diagnosis.primary_candidate is not None
    assert "value" in diagnosis.suspected_variables
    assert any("AST node: BinOp" in evidence for evidence in diagnosis.primary_candidate.evidence)
    assert diagnosis.trigger_conditions == ["value has incompatible type for this operation/call"]

    constraint_kinds = {constraint.kind for constraint in diagnosis.repair_constraints}
    assert "validate_or_convert_input_type" in constraint_kinds
    assert "avoid_implicit_broad_cast" in constraint_kinds
    assert "preserve_call_contract" in constraint_kinds

    hints = "\n".join(diagnosis.adversarial_hints)
    assert "string, numeric, list/dict, and null variants" in hints
    assert "happy path" in hints


def test_diagnose_value_error_identifies_conversion_argument(tmp_path: Path) -> None:
    diagnosis = _diagnosis_for_source(
        tmp_path,
        """
        def handler(raw_age):
            return int(raw_age)
        """,
        exception_type="ValueError",
        exception_message="invalid literal for int() with base 10: 'abc'",
        line_number=2,
    )

    assert diagnosis.classification.category == "invalid_value"
    assert diagnosis.primary_candidate is not None
    assert "raw_age" in diagnosis.suspected_variables
    assert any("AST node: Call" in evidence for evidence in diagnosis.primary_candidate.evidence)
    assert any("Callee: int" in evidence for evidence in diagnosis.primary_candidate.evidence)
    assert diagnosis.trigger_conditions == ["raw_age cannot be parsed/validated as the required value"]

    constraint_kinds = {constraint.kind for constraint in diagnosis.repair_constraints}
    assert "controlled_value_validation" in constraint_kinds
    assert "preserve_valid_input_behavior" in constraint_kinds
    assert "validate_parse_boundaries" in constraint_kinds

    hints = "\n".join(diagnosis.adversarial_hints)
    assert "malformed" in hints
    assert "empty-string" in hints
    assert "boundary values" in hints
    assert "valid value" in hints


def test_variable_flow_traces_local_request_assignment_before_value_error(tmp_path: Path) -> None:
    diagnosis = _diagnosis_for_source(
        tmp_path,
        """
        def handler(request):
            raw_age = request.args.get("age")
            return int(raw_age)
        """,
        exception_type="ValueError",
        exception_message="invalid literal for int() with base 10: 'abc'",
        line_number=3,
    )

    assert diagnosis.classification.category == "invalid_value"
    assert "raw_age" in diagnosis.suspected_variables
    raw_age_flow = next(flow for flow in diagnosis.variable_flows if flow.variable == "raw_age")
    assert "query parameter age via request.args.get" in raw_age_flow.source
    assert any('raw_age = request.args.get("age")' in evidence for evidence in raw_age_flow.evidence)


@pytest.mark.seed_failure
def test_diagnose_demo_zero_division_identifies_primary_fault(tmp_path: Path) -> None:
    diagnosis = _demo_diagnosis(tmp_path)

    assert diagnosis.classification.exception_type == "ZeroDivisionError"
    assert diagnosis.classification.category == "arithmetic_boundary"
    assert diagnosis.primary_candidate is not None
    assert diagnosis.primary_candidate.file == "demo_service/app.py"
    assert diagnosis.primary_candidate.function == "unsafe_divide"
    assert diagnosis.primary_candidate.code == "return numerator / denominator"
    assert diagnosis.primary_candidate.suspiciousness == 1.0
    assert any("BinOp(op=Div)" in evidence for evidence in diagnosis.primary_candidate.evidence)
    assert "Right operand: denominator" in diagnosis.primary_candidate.evidence

    assert diagnosis.suspected_variables == ["denominator"]
    assert diagnosis.trigger_conditions == ["denominator == 0"]


@pytest.mark.seed_failure
def test_diagnose_demo_zero_division_traces_query_parameter_flow(tmp_path: Path) -> None:
    diagnosis = _demo_diagnosis(tmp_path)

    flow_sources = [flow.source for flow in diagnosis.variable_flows]
    assert any("query parameter denominator or x via _float_arg" in source for source in flow_sources)
    assert any("literal 0.0" in source for source in flow_sources)

    query_flow = next(
        flow for flow in diagnosis.variable_flows
        if "query parameter denominator or x via _float_arg" in flow.source
    )
    assert query_flow.variable == "denominator"
    assert any("_float_arg" in evidence for evidence in query_flow.evidence)
    assert any("unsafe_divide(numerator, denominator)" in evidence for evidence in query_flow.evidence)


@pytest.mark.seed_failure
def test_diagnose_demo_zero_division_generates_repair_constraints_and_hints(tmp_path: Path) -> None:
    diagnosis = _demo_diagnosis(tmp_path)

    constraint_kinds = {constraint.kind for constraint in diagnosis.repair_constraints}
    assert "preserve_signature" in constraint_kinds
    assert "preserve_happy_path" in constraint_kinds
    assert "web_api_error_semantics" in constraint_kinds
    assert "avoid_special_float" in constraint_kinds

    constraint_text = "\n".join(constraint.description for constraint in diagnosis.repair_constraints)
    assert "unsafe_divide(numerator, denominator)" in constraint_text
    assert "4xx" in constraint_text
    assert "NaN" in constraint_text
    assert "Infinity" in constraint_text

    hints = "\n".join(diagnosis.adversarial_hints)
    assert "/divide?x=0" in hints
    assert "/divide?x=-1" in hints
    assert "/bug" in hints
    assert "denominator=0" in hints


@pytest.mark.seed_failure
def test_fault_diagnosis_serialization_and_report(tmp_path: Path) -> None:
    diagnosis = _demo_diagnosis(tmp_path)

    data = diagnosis.to_dict()
    assert data["classification"]["category"] == "arithmetic_boundary"
    assert data["primary_candidate"]["file"] == "demo_service/app.py"
    assert data["suspected_variables"] == ["denominator"]
    assert data["variable_flows"]

    report = format_fault_diagnosis_report(diagnosis)
    assert "Fault Diagnosis" in report
    assert "Primary Candidate" in report
    assert "denominator == 0" in report
    assert "query parameter denominator or x" in report
    assert "Repair Constraints" in report
    assert "Adversarial Hints" in report
