"""Deterministic fault diagnosis from CodeContext.

``CodeContext`` answers *where* the service crashed.  This module adds a
diagnosis layer that tries to explain *why* it crashed, which variable is most
likely responsible, where that variable came from, and which constraints should
guide the repair.

The implementation is intentionally rule-based and conservative.  It keeps the
current demo's ``ZeroDivisionError`` path deterministic, and also exposes a
small but extensible taxonomy/dataflow layer for common Python runtime errors so
later planner/validator phases can consume richer evidence without changing the
caller contract.
"""

from __future__ import annotations

import ast
import keyword
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .code_context import CodeContext


@dataclass(frozen=True)
class ErrorClassification:
    """High-level error category and repair strategy hint."""

    exception_type: str
    category: str
    strategy_hint: str
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FaultCandidate:
    """A suspicious source location that may be responsible for the crash."""

    file: str
    line_number: int
    function: str | None
    code: str
    suspiciousness: float
    reason: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VariableFlow:
    """A lightweight explanation of how a suspicious variable is populated."""

    variable: str
    source: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepairConstraint:
    """A deterministic constraint that the repair planner should respect."""

    kind: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FaultDiagnosis:
    """Structured fault diagnosis derived from traceback and code context."""

    classification: ErrorClassification
    primary_candidate: FaultCandidate | None
    candidates: list[FaultCandidate] = field(default_factory=list)
    suspected_variables: list[str] = field(default_factory=list)
    variable_flows: list[VariableFlow] = field(default_factory=list)
    repair_constraints: list[RepairConstraint] = field(default_factory=list)
    adversarial_hints: list[str] = field(default_factory=list)
    trigger_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.to_dict(),
            "primary_candidate": self.primary_candidate.to_dict() if self.primary_candidate else None,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "suspected_variables": self.suspected_variables,
            "variable_flows": [flow.to_dict() for flow in self.variable_flows],
            "repair_constraints": [constraint.to_dict() for constraint in self.repair_constraints],
            "adversarial_hints": self.adversarial_hints,
            "trigger_conditions": self.trigger_conditions,
        }


ERROR_TAXONOMY: dict[str, tuple[str, str, str]] = {
    "ZeroDivisionError": (
        "arithmetic_boundary",
        "Inspect division, floor-division, and modulo right operands; validate the zero boundary before the arithmetic operation.",
        "high",
    ),
    "IndexError": (
        "collection_boundary",
        "Inspect index calculations, collection length, empty collections, and fallback behavior.",
        "medium",
    ),
    "KeyError": (
        "missing_key",
        "Inspect dictionary key access, request fields, config keys, and default-value handling.",
        "medium",
    ),
    "AttributeError": (
        "null_or_type_mismatch",
        "Inspect None values, object types, and attribute existence before dereferencing.",
        "medium",
    ),
    "TypeError": (
        "type_mismatch",
        "Inspect argument types, implicit conversions, request parsing, and function call contracts.",
        "medium",
    ),
    "ValueError": (
        "invalid_value",
        "Inspect input parsing, formats, ranges, and controlled validation errors.",
        "medium",
    ),
    "JSONDecodeError": (
        "invalid_value",
        "Inspect JSON parsing, malformed payloads, empty bodies, and controlled validation errors.",
        "medium",
    ),
    "UnicodeDecodeError": (
        "invalid_value",
        "Inspect byte/string decoding boundaries, encodings, malformed input, and controlled validation errors.",
        "medium",
    ),
    "AssertionError": (
        "contract_violation",
        "Inspect violated invariants, preconditions, postconditions, and tests that encode the expected contract.",
        "medium",
    ),
    "FileNotFoundError": (
        "missing_resource",
        "Inspect file paths, configuration, working directory, and fallback behavior for missing resources.",
        "medium",
    ),
    "PermissionError": (
        "permission_error",
        "Inspect filesystem or network permissions and prefer controlled failure over broad privilege changes.",
        "medium",
    ),
    "ConnectionError": (
        "external_dependency_error",
        "Inspect external dependency availability, connection configuration, retries, and fallback behavior.",
        "medium",
    ),
    "ImportError": (
        "dependency_error",
        "Inspect installed dependencies, package names, import paths, and runtime environment.",
        "medium",
    ),
    "ModuleNotFoundError": (
        "dependency_error",
        "Inspect installed dependencies, package names, import paths, and runtime environment.",
        "medium",
    ),
    "TimeoutError": (
        "external_dependency_timeout",
        "Inspect external calls, timeout configuration, retries, and fallback behavior.",
        "medium",
    ),
}


DIVISION_OPERATIONS = {
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
}


def classify_error(exception_type: str) -> ErrorClassification:
    """Return a deterministic taxonomy classification for an exception type."""

    short_name = exception_type.rsplit(".", maxsplit=1)[-1]
    category, strategy_hint, confidence = ERROR_TAXONOMY.get(
        short_name,
        (
            "unknown_runtime_error",
            "Conservatively inspect the crash line and direct callers; avoid broad changes unless evidence is strong.",
            "low",
        ),
    )
    return ErrorClassification(
        exception_type=exception_type,
        category=category,
        strategy_hint=strategy_hint,
        confidence=confidence,
    )


def diagnose_fault(context: CodeContext) -> FaultDiagnosis:
    """Build a rule-based fault diagnosis from ``CodeContext``."""

    classification = classify_error(context.traceback.exception_type)

    candidates: list[FaultCandidate]
    suspected_variables: list[str]
    if classification.category == "arithmetic_boundary":
        candidates, suspected_variables = _diagnose_arithmetic_boundary(context)
    elif classification.category == "missing_key":
        candidates, suspected_variables = _diagnose_missing_key(context)
    elif classification.category == "null_or_type_mismatch":
        candidates, suspected_variables = _diagnose_attribute_error(context)
    elif classification.category == "type_mismatch":
        candidates, suspected_variables = _diagnose_type_mismatch(context)
    elif classification.category == "invalid_value":
        candidates, suspected_variables = _diagnose_invalid_value(context)
    else:
        candidates = [_generic_crash_candidate(context)]
        suspected_variables = _suspected_names_from_crash_expression(context)

    primary_candidate = candidates[0] if candidates else None
    trigger_conditions = _build_trigger_conditions(classification, suspected_variables, candidates)
    variable_flows = _infer_variable_flows(context, suspected_variables)
    repair_constraints = _build_repair_constraints(context, classification, suspected_variables)
    adversarial_hints = _build_adversarial_hints(classification, suspected_variables, variable_flows)

    return FaultDiagnosis(
        classification=classification,
        primary_candidate=primary_candidate,
        candidates=candidates,
        suspected_variables=suspected_variables,
        variable_flows=variable_flows,
        repair_constraints=repair_constraints,
        adversarial_hints=adversarial_hints,
        trigger_conditions=trigger_conditions,
    )


def format_fault_diagnosis_report(diagnosis: FaultDiagnosis) -> str:
    """Render a human-readable diagnosis report for terminal demos."""

    lines: list[str] = []
    classification = diagnosis.classification

    lines.append("# Fault Diagnosis")
    lines.append("")
    lines.append(f"- Exception: {classification.exception_type}")
    lines.append(f"- Category: {classification.category}")
    lines.append(f"- Confidence: {classification.confidence}")
    lines.append(f"- Strategy hint: {classification.strategy_hint}")

    lines.append("")
    lines.append("## Primary Candidate")
    if diagnosis.primary_candidate is None:
        lines.append("- No primary candidate identified.")
    else:
        candidate = diagnosis.primary_candidate
        lines.append(f"- Location: {candidate.file}:{candidate.line_number}")
        if candidate.function:
            lines.append(f"- Function: `{candidate.function}`")
        lines.append(f"- Code: `{candidate.code.strip()}`")
        lines.append(f"- Suspiciousness: {candidate.suspiciousness:.2f}")
        lines.append(f"- Reason: {candidate.reason}")
        if candidate.evidence:
            lines.append("- Evidence:")
            for evidence in candidate.evidence:
                lines.append(f"  - {evidence}")

    lines.append("")
    lines.append("## Suspected Variables")
    if diagnosis.suspected_variables:
        for variable in diagnosis.suspected_variables:
            lines.append(f"- `{variable}`")
    else:
        lines.append("- No specific variable isolated.")

    lines.append("")
    lines.append("## Trigger Conditions")
    if diagnosis.trigger_conditions:
        for condition in diagnosis.trigger_conditions:
            lines.append(f"- `{condition}`")
    else:
        lines.append("- No deterministic trigger condition inferred.")

    lines.append("")
    lines.append("## Variable Flow")
    if diagnosis.variable_flows:
        for flow in diagnosis.variable_flows:
            lines.append(f"- `{flow.variable}` ← {flow.source}")
            if flow.evidence:
                for evidence in flow.evidence:
                    lines.append(f"  - evidence: `{evidence.strip()}`")
    else:
        lines.append("- No variable flow inferred.")

    lines.append("")
    lines.append("## Repair Constraints")
    if diagnosis.repair_constraints:
        for constraint in diagnosis.repair_constraints:
            lines.append(f"- **{constraint.kind}**: {constraint.description}")
    else:
        lines.append("- No deterministic repair constraints generated.")

    lines.append("")
    lines.append("## Adversarial Hints")
    if diagnosis.adversarial_hints:
        for hint in diagnosis.adversarial_hints:
            lines.append(f"- {hint}")
    else:
        lines.append("- No adversarial hints generated.")

    return "\n".join(lines)


def _diagnose_arithmetic_boundary(context: CodeContext) -> tuple[list[FaultCandidate], list[str]]:
    expressions = _division_expressions_at_crash(context)
    if not expressions:
        candidate = _generic_crash_candidate(
            context,
            suspiciousness=0.75,
            reason="ZeroDivisionError occurred at the crash line, but no division/modulo AST node was isolated.",
            extra_evidence=["Exception type: ZeroDivisionError"],
        )
        guessed_variable = _guess_right_operand_from_line(context.crash_line_code or "")
        return [candidate], [guessed_variable] if guessed_variable else []

    candidates: list[FaultCandidate] = []
    suspected_variables: list[str] = []
    for expression in expressions:
        right_operand = expression["right_operand"]
        if right_operand and right_operand not in suspected_variables:
            suspected_variables.append(right_operand)

        operation = expression["operation"]
        evidence = [
            f"AST node: BinOp(op={expression['operation_name']})",
            f"Left operand: {expression['left_operand']}",
            f"Right operand: {right_operand}",
            "Exception type: ZeroDivisionError",
        ]
        if right_operand:
            evidence.append(f"Trigger condition: {right_operand} == 0")

        candidates.append(
            FaultCandidate(
                file=context.crash_file_relative,
                line_number=context.crash_line_number,
                function=context.crash_function.qualname if context.crash_function else None,
                code=(context.crash_line_code or "").strip(),
                suspiciousness=1.0,
                reason=(
                    f"The crash line performs `{operation}` with right operand `{right_operand}`; "
                    "a zero right operand directly matches ZeroDivisionError semantics."
                ),
                evidence=evidence,
            )
        )

    return candidates, suspected_variables


def _diagnose_missing_key(context: CodeContext) -> tuple[list[FaultCandidate], list[str]]:
    """Diagnose ``KeyError`` from direct mapping/subscript access."""

    subscript_nodes = _crash_ast_nodes(context, (ast.Subscript,))
    if not subscript_nodes:
        candidate = _generic_crash_candidate(
            context,
            suspiciousness=0.7,
            reason="KeyError occurred at the crash line, but no subscript AST node was isolated.",
            extra_evidence=["Exception type: KeyError"],
        )
        return [candidate], _suspected_names_from_crash_expression(context)

    candidates: list[FaultCandidate] = []
    suspected_variables: list[str] = []
    for node in subscript_nodes:
        mapping_expression = _unparse(node.value)
        key_expression = _unparse(node.slice)
        for variable in _names_in_node(node.value):
            _append_unique(suspected_variables, variable)
        for variable in _names_in_node(node.slice):
            _append_unique(suspected_variables, variable)

        evidence = [
            "AST node: Subscript",
            f"Mapping expression: {mapping_expression}",
            f"Key expression: {key_expression}",
            "Exception type: KeyError",
        ]
        if context.traceback.exception_message:
            evidence.append(f"Exception message: {context.traceback.exception_message}")

        candidates.append(
            FaultCandidate(
                file=context.crash_file_relative,
                line_number=context.crash_line_number,
                function=context.crash_function.qualname if context.crash_function else None,
                code=(context.crash_line_code or "").strip(),
                suspiciousness=0.95,
                reason=(
                    f"The crash line directly reads `{mapping_expression}[{key_expression}]`; "
                    "a missing mapping key directly matches KeyError semantics."
                ),
                evidence=evidence,
            )
        )

    if not suspected_variables:
        suspected_variables = _suspected_names_from_crash_expression(context)
    return candidates, suspected_variables


def _diagnose_attribute_error(context: CodeContext) -> tuple[list[FaultCandidate], list[str]]:
    """Diagnose ``AttributeError`` from attribute dereference expressions."""

    attribute_nodes = _crash_ast_nodes(context, (ast.Attribute,))
    if not attribute_nodes:
        candidate = _generic_crash_candidate(
            context,
            suspiciousness=0.7,
            reason="AttributeError occurred at the crash line, but no attribute AST node was isolated.",
            extra_evidence=["Exception type: AttributeError"],
        )
        return [candidate], _suspected_names_from_crash_expression(context)

    candidates: list[FaultCandidate] = []
    suspected_variables: list[str] = []
    for node in attribute_nodes:
        base_expression = _unparse(node.value)
        attribute_name = node.attr
        for variable in _names_in_node(node.value):
            _append_unique(suspected_variables, variable)

        evidence = [
            "AST node: Attribute",
            f"Base object: {base_expression}",
            f"Attribute: {attribute_name}",
            "Exception type: AttributeError",
        ]
        if context.traceback.exception_message:
            evidence.append(f"Exception message: {context.traceback.exception_message}")

        candidates.append(
            FaultCandidate(
                file=context.crash_file_relative,
                line_number=context.crash_line_number,
                function=context.crash_function.qualname if context.crash_function else None,
                code=(context.crash_line_code or "").strip(),
                suspiciousness=0.9,
                reason=(
                    f"The crash line dereferences `{base_expression}.{attribute_name}`; "
                    "the base object may be None, a different type, or missing the required attribute."
                ),
                evidence=evidence,
            )
        )

    if not suspected_variables:
        suspected_variables = _suspected_names_from_crash_expression(context)
    return candidates, suspected_variables


def _diagnose_type_mismatch(context: CodeContext) -> tuple[list[FaultCandidate], list[str]]:
    """Diagnose ``TypeError`` from operations, calls, or comparisons."""

    operation_nodes = _crash_ast_nodes(context, (ast.BinOp, ast.Call, ast.Compare))
    if not operation_nodes:
        candidate = _generic_crash_candidate(
            context,
            suspiciousness=0.65,
            reason="TypeError occurred at the crash line; inspect the expression operands and call arguments.",
            extra_evidence=["Exception type: TypeError"],
        )
        return [candidate], _suspected_names_from_crash_expression(context)

    candidates: list[FaultCandidate] = []
    suspected_variables: list[str] = []
    for node in operation_nodes:
        evidence = [_type_error_node_evidence(node), "Exception type: TypeError"]
        if context.traceback.exception_message:
            evidence.append(f"Exception message: {context.traceback.exception_message}")

        for variable in _suspected_names_for_type_node(node):
            _append_unique(suspected_variables, variable)

        candidates.append(
            FaultCandidate(
                file=context.crash_file_relative,
                line_number=context.crash_line_number,
                function=context.crash_function.qualname if context.crash_function else None,
                code=(context.crash_line_code or "").strip(),
                suspiciousness=0.85,
                reason=(
                    f"The crash line evaluates `{_unparse(node)}`; at least one operand or call argument "
                    "likely violates the expected type contract."
                ),
                evidence=evidence,
            )
        )

    if not suspected_variables:
        suspected_variables = _suspected_names_from_crash_expression(context)
    return candidates, suspected_variables


def _diagnose_invalid_value(context: CodeContext) -> tuple[list[FaultCandidate], list[str]]:
    """Diagnose ``ValueError`` from parsing/conversion/explicit validation."""

    value_nodes = _crash_ast_nodes(context, (ast.Call, ast.Raise))
    if not value_nodes:
        candidate = _generic_crash_candidate(
            context,
            suspiciousness=0.65,
            reason="ValueError occurred at the crash line; inspect input parsing, validation, and range checks.",
            extra_evidence=["Exception type: ValueError"],
        )
        return [candidate], _suspected_names_from_crash_expression(context)

    candidates: list[FaultCandidate] = []
    suspected_variables: list[str] = []
    for node in value_nodes:
        evidence = [_value_error_node_evidence(node), "Exception type: ValueError"]
        if context.traceback.exception_message:
            evidence.append(f"Exception message: {context.traceback.exception_message}")

        for variable in _suspected_names_for_value_node(node):
            _append_unique(suspected_variables, variable)

        candidates.append(
            FaultCandidate(
                file=context.crash_file_relative,
                line_number=context.crash_line_number,
                function=context.crash_function.qualname if context.crash_function else None,
                code=(context.crash_line_code or "").strip(),
                suspiciousness=0.85,
                reason=(
                    f"The crash line evaluates `{_unparse(node)}`; a malformed, empty, out-of-range, "
                    "or otherwise invalid value likely reached parsing/validation code."
                ),
                evidence=evidence,
            )
        )

    if not suspected_variables:
        suspected_variables = _suspected_names_from_crash_expression(context)
    return candidates, suspected_variables


def _division_expressions_at_crash(context: CodeContext) -> list[dict[str, str]]:
    if context.crash_function is None:
        return []

    source = textwrap.dedent(context.crash_function.source)
    tree = _parse_python(source)
    if tree is None:
        return []

    relative_line = context.crash_line_number - context.crash_function.start_line + 1
    expressions: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        operation = _division_operation_symbol(node.op)
        if operation is None:
            continue
        if not _node_spans_line(node, relative_line):
            continue

        expressions.append(
            {
                "operation": operation,
                "operation_name": node.op.__class__.__name__,
                "left_operand": _unparse(node.left),
                "right_operand": _unparse(node.right),
            }
        )

    return expressions


def _crash_ast_nodes(
    context: CodeContext,
    node_types: tuple[type[ast.AST], ...] | None = None,
) -> list[ast.AST]:
    """Return deterministic AST nodes whose source span includes the crash line."""

    tree_and_line = _crash_tree_and_relative_line(context)
    if tree_and_line is None:
        return []

    tree, relative_line = tree_and_line
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if node_types is None and isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.arguments, ast.arg),
        ):
            continue
        if node_types is not None and not isinstance(node, node_types):
            continue
        if not _node_spans_line(node, relative_line):
            continue
        nodes.append(node)

    nodes.sort(key=lambda node: (_type_priority(node, node_types), *_node_sort_key(node)))
    return nodes


def _crash_tree_and_relative_line(context: CodeContext) -> tuple[ast.AST, int] | None:
    if context.crash_function is not None:
        source = textwrap.dedent(context.crash_function.source)
        tree = _parse_python(source)
        if tree is None:
            return None
        relative_line = context.crash_line_number - context.crash_function.start_line + 1
        return tree, relative_line

    if not context.crash_line_code:
        return None
    tree = _parse_python(context.crash_line_code.strip())
    if tree is None:
        return None
    return tree, 1


def _type_priority(node: ast.AST, node_types: tuple[type[ast.AST], ...] | None) -> int:
    if node_types is None:
        return 0
    for index, node_type in enumerate(node_types):
        if isinstance(node, node_type):
            return index
    return len(node_types)


def _node_sort_key(node: ast.AST) -> tuple[int, int, int, int, int, str]:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    col = getattr(node, "col_offset", 0)
    end_col = getattr(node, "end_col_offset", col)
    line_span = max(end - start, 0)
    col_span = max(end_col - col, 0)
    return (start, line_span, col, col_span, end_col, node.__class__.__name__)


def _suspected_names_from_crash_expression(context: CodeContext) -> list[str]:
    names: list[str] = []
    for node in _crash_ast_nodes(context):
        for name in _names_in_node(node):
            _append_unique(names, name)
    if names:
        return names

    if context.crash_line_code:
        line_tree = _parse_python(context.crash_line_code.strip())
        if line_tree is not None:
            for name in _names_in_node(line_tree):
                _append_unique(names, name)
    return names


def _names_in_node(node: ast.AST) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            _append_unique(names, child.id)
    return names


def _suspected_names_for_type_node(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Call):
        return _names_in_call_inputs(node)
    return _names_in_node(node)


def _suspected_names_for_value_node(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Call):
        return _names_in_call_inputs(node)
    if isinstance(node, ast.Raise):
        return _names_in_node(node.exc) if node.exc is not None else []
    return _names_in_node(node)


def _names_in_call_inputs(node: ast.Call) -> list[str]:
    names: list[str] = []
    for argument in node.args:
        for name in _names_in_node(argument):
            _append_unique(names, name)
    for keyword_arg in node.keywords:
        for name in _names_in_node(keyword_arg.value):
            _append_unique(names, name)
    return names


def _type_error_node_evidence(node: ast.AST) -> str:
    if isinstance(node, ast.BinOp):
        return (
            f"AST node: BinOp(op={node.op.__class__.__name__}); "
            f"Left operand: {_unparse(node.left)}; Right operand: {_unparse(node.right)}"
        )
    if isinstance(node, ast.Call):
        return (
            "AST node: Call; "
            f"Callee: {_callee_name(node.func) or _unparse(node.func)}; "
            f"Arguments: {_format_call_arguments(node)}"
        )
    if isinstance(node, ast.Compare):
        return (
            "AST node: Compare; "
            f"Left operand: {_unparse(node.left)}; "
            f"Comparators: {', '.join(_unparse(comparator) for comparator in node.comparators)}"
        )
    return f"AST node: {node.__class__.__name__}; Expression: {_unparse(node)}"


def _value_error_node_evidence(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return (
            "AST node: Call; "
            f"Callee: {_callee_name(node.func) or _unparse(node.func)}; "
            f"Arguments: {_format_call_arguments(node)}"
        )
    if isinstance(node, ast.Raise):
        return f"AST node: Raise; Raise expression: {_unparse(node.exc) if node.exc is not None else '<bare raise>'}"
    return f"AST node: {node.__class__.__name__}; Expression: {_unparse(node)}"


def _format_call_arguments(node: ast.Call) -> str:
    parts = [_unparse(argument) for argument in node.args]
    parts.extend(
        f"{keyword_arg.arg}={_unparse(keyword_arg.value)}"
        if keyword_arg.arg is not None
        else f"**{_unparse(keyword_arg.value)}"
        for keyword_arg in node.keywords
    )
    return ", ".join(parts) if parts else "<none>"


def _append_unique(items: list[str], value: str | None) -> None:
    if value is None:
        return
    stripped = value.strip()
    if not stripped or stripped in items:
        return
    items.append(stripped)


def _first_evidence_value(candidates: list[FaultCandidate] | None, prefix: str) -> str | None:
    if not candidates:
        return None
    normalized_prefix = prefix.rstrip()
    for candidate in candidates:
        for evidence in candidate.evidence:
            if evidence.startswith(normalized_prefix):
                return evidence[len(normalized_prefix):].strip()
    return None


def _generic_crash_candidate(
    context: CodeContext,
    *,
    suspiciousness: float = 0.6,
    reason: str | None = None,
    extra_evidence: Iterable[str] = (),
) -> FaultCandidate:
    evidence = list(extra_evidence)
    if context.traceback.exception_message:
        evidence.append(f"Exception message: {context.traceback.exception_message}")
    if context.crash_line_code:
        evidence.append(f"Crash line: {context.crash_line_code.strip()}")

    return FaultCandidate(
        file=context.crash_file_relative,
        line_number=context.crash_line_number,
        function=context.crash_function.qualname if context.crash_function else None,
        code=(context.crash_line_code or "").strip(),
        suspiciousness=suspiciousness,
        reason=reason or "The traceback innermost frame points to this source line.",
        evidence=evidence,
    )


def _build_trigger_conditions(
    classification: ErrorClassification,
    suspected_variables: list[str],
    candidates: list[FaultCandidate] | None = None,
) -> list[str]:
    if classification.category == "arithmetic_boundary":
        return [f"{variable} == 0" for variable in suspected_variables]

    if classification.category == "missing_key":
        mapping_expression = _first_evidence_value(candidates, "Mapping expression:")
        key_expression = _first_evidence_value(candidates, "Key expression:")
        if mapping_expression and key_expression:
            return [f"key {key_expression} missing from {mapping_expression}"]
        return ["required key missing from mapping/input payload"]

    if classification.category == "null_or_type_mismatch":
        base_object = _first_evidence_value(candidates, "Base object:")
        attribute = _first_evidence_value(candidates, "Attribute:")
        if base_object and attribute:
            return [f"{base_object} is None or lacks attribute {attribute}"]
        if suspected_variables:
            return [f"{variable} is None or lacks the required attribute" for variable in suspected_variables]
        return ["object is None, wrong type, or lacks the required attribute"]

    if classification.category == "type_mismatch":
        if suspected_variables:
            return [f"{variable} has incompatible type for this operation/call" for variable in suspected_variables]
        return ["operation/call received an incompatible value type"]

    if classification.category == "invalid_value":
        if suspected_variables:
            return [f"{variable} cannot be parsed/validated as the required value" for variable in suspected_variables]
        return ["input value is malformed, empty, out of range, or violates validation"]

    return []


def _infer_variable_flows(context: CodeContext, suspected_variables: list[str]) -> list[VariableFlow]:
    if context.crash_function is None or not suspected_variables:
        return []

    project_root = Path(context.project_root)
    parameter_names = context.crash_function.parameters
    flows: list[VariableFlow] = []
    seen: set[tuple[str, str]] = set()

    for variable in suspected_variables:
        if not _is_identifier_name(variable):
            continue

        variable_had_flow = False
        if variable in parameter_names:
            for call_site in context.incoming_calls:
                call_node = _parse_call_from_line(call_site.code, context.crash_function.name)
                if call_node is None:
                    continue

                argument = _argument_for_parameter(call_node, parameter_names, variable)
                if argument is None:
                    continue

                flow = _flow_from_argument(
                    context=context,
                    variable=variable,
                    argument=argument,
                    call_site_file=project_root / call_site.file,
                    caller_function=call_site.caller_function,
                    call_line_number=call_site.line_number,
                    call_code=call_site.code,
                )
                if flow is None:
                    continue

                variable_had_flow = _append_flow(flows, seen, flow) or variable_had_flow

        local_assignment = _find_latest_local_assignment_in_crash_function(context, variable)
        if local_assignment is not None:
            assignment_line, value_node = local_assignment
            input_source = _describe_input_source(value_node)
            if input_source is None:
                input_source = f"local assignment `{assignment_line.strip()}`"
            flow = VariableFlow(variable=variable, source=input_source, evidence=[assignment_line])
            variable_had_flow = _append_flow(flows, seen, flow) or variable_had_flow

        if not variable_had_flow:
            if variable in parameter_names:
                flow = VariableFlow(
                    variable=variable,
                    source=f"function parameter `{variable}` of `{context.crash_function.qualname}`",
                    evidence=[_function_signature_hint(context.crash_function.name, parameter_names)],
                )
                _append_flow(flows, seen, flow)
            elif variable in _suspected_names_from_crash_expression(context):
                flow = VariableFlow(
                    variable=variable,
                    source=f"variable `{variable}` referenced directly in the crash expression",
                    evidence=[(context.crash_line_code or "").strip()],
                )
                _append_flow(flows, seen, flow)

    return flows


def _flow_from_argument(
    *,
    context: CodeContext,
    variable: str,
    argument: ast.AST,
    call_site_file: Path,
    caller_function: str | None,
    call_line_number: int,
    call_code: str,
) -> VariableFlow | None:
    argument_text = _unparse(argument)

    if isinstance(argument, ast.Constant):
        return VariableFlow(
            variable=variable,
            source=f"literal {argument.value!r} passed by {caller_function or '<module>'}",
            evidence=[call_code],
        )

    if not isinstance(argument, ast.Name):
        return VariableFlow(
            variable=variable,
            source=f"expression `{argument_text}` passed by {caller_function or '<module>'}",
            evidence=[call_code],
        )

    caller_variable = argument.id
    assignment = _find_latest_assignment_before_call(
        call_site_file,
        caller_function=caller_function,
        variable=caller_variable,
        before_line=call_line_number,
    )
    if assignment is None:
        if caller_variable == variable:
            source = f"caller variable `{caller_variable}` passed into {context.crash_function.name}"
        else:
            source = f"caller variable `{caller_variable}` mapped to parameter `{variable}`"
        return VariableFlow(variable=variable, source=source, evidence=[call_code])

    assignment_line, value_node = assignment
    input_source = _describe_input_source(value_node)
    if input_source is None:
        input_source = f"caller assignment `{assignment_line.strip()}`"

    return VariableFlow(
        variable=variable,
        source=input_source,
        evidence=[assignment_line, call_code],
    )


def _append_flow(flows: list[VariableFlow], seen: set[tuple[str, str]], flow: VariableFlow) -> bool:
    key = (flow.variable, flow.source)
    if key in seen:
        return False
    seen.add(key)
    flows.append(flow)
    return True


def _find_latest_local_assignment_in_crash_function(
    context: CodeContext,
    variable: str,
) -> tuple[str, ast.AST] | None:
    if context.crash_function is None:
        return None

    source = textwrap.dedent(context.crash_function.source)
    tree = _parse_python(source)
    if tree is None:
        return None

    relative_crash_line = context.crash_line_number - context.crash_function.start_line + 1
    source_lines = source.splitlines()
    matches: list[tuple[int, str, ast.AST]] = []

    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", 0)
        if lineno <= 0 or lineno >= relative_crash_line:
            continue

        value_node: ast.AST | None = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value_node = node.value
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
            value_node = node.value
        elif isinstance(node, ast.NamedExpr):
            targets = [node.target]
            value_node = node.value

        if value_node is None:
            continue
        if any(_target_contains_name(target, variable) for target in targets):
            matches.append((lineno, _line_at(source_lines, lineno) or "", value_node))

    if not matches:
        return None

    _, line, value = max(matches, key=lambda item: item[0])
    return line, value


def _find_latest_assignment_before_call(
    file_path: Path,
    *,
    caller_function: str | None,
    variable: str,
    before_line: int,
) -> tuple[str, ast.AST] | None:
    source = _read_text(file_path)
    if not source:
        return None

    tree = _parse_python(source)
    if tree is None:
        return None

    source_lines = source.splitlines()
    search_root: ast.AST = tree
    if caller_function:
        function_node = _find_function_by_qualname(tree, caller_function)
        if function_node is not None:
            search_root = function_node

    matches: list[tuple[int, str, ast.AST]] = []
    for node in ast.walk(search_root):
        lineno = getattr(node, "lineno", 0)
        if lineno <= 0 or lineno > before_line:
            continue

        value_node: ast.AST | None = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value_node = node.value
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
            value_node = node.value

        if value_node is None:
            continue
        if any(_target_contains_name(target, variable) for target in targets):
            matches.append((lineno, _line_at(source_lines, lineno) or "", value_node))

    if not matches:
        return None

    _, line, value = max(matches, key=lambda item: item[0])
    return line, value


def _describe_input_source(value_node: ast.AST) -> str | None:
    if isinstance(value_node, ast.Subscript):
        return _describe_subscript_source(value_node)

    if not isinstance(value_node, ast.Call):
        return None

    callee = _callee_name(value_node.func)
    if callee is None:
        return None

    if callee.endswith("_float_arg"):
        return _describe_float_arg_call(value_node)

    if callee.endswith(".get"):
        return _describe_get_call(value_node)

    if callee in {"int", "float", "str", "bool", "bytes"} and value_node.args:
        inner_source = _describe_input_source(value_node.args[0])
        argument_text = _unparse(value_node.args[0])
        if inner_source:
            return f"{inner_source} converted by {callee}(...)"
        return f"value `{argument_text}` converted by {callee}(...)"

    if callee.endswith("json.loads") and value_node.args:
        argument_text = _unparse(value_node.args[0])
        return f"JSON text `{argument_text}` parsed by json.loads"

    if callee.endswith(".get_json"):
        receiver = _call_receiver(value_node)
        receiver_text = _unparse(receiver) if receiver is not None else "request"
        return f"JSON body via {receiver_text}.get_json"

    return None


def _describe_float_arg_call(value_node: ast.Call) -> str | None:
    query_name = _literal_string(value_node.args[0]) if value_node.args else None
    fallback_name: str | None = None
    default_value: str | None = None
    for keyword_arg in value_node.keywords:
        if keyword_arg.arg == "fallback_name":
            fallback_name = _literal_string(keyword_arg.value)
        elif keyword_arg.arg == "default":
            default_value = _unparse(keyword_arg.value)

    if query_name and fallback_name:
        source = f"query parameter {query_name} or {fallback_name} via _float_arg"
    elif query_name:
        source = f"query parameter {query_name} via _float_arg"
    else:
        source = "query parameter via _float_arg"

    if default_value is not None:
        source += f" with default {default_value}"
    return source


def _describe_get_call(value_node: ast.Call) -> str | None:
    receiver = _call_receiver(value_node)
    receiver_text = _unparse(receiver) if receiver is not None else None
    key_name = _literal_string(value_node.args[0]) if value_node.args else None
    default_text = _unparse(value_node.args[1]) if len(value_node.args) > 1 else None

    if receiver_text in {"request.args", "flask.request.args"}:
        source = f"query parameter {key_name or '<dynamic>'} via request.args.get"
    elif receiver_text in {"request.form", "flask.request.form"}:
        source = f"form field {key_name or '<dynamic>'} via request.form.get"
    elif receiver_text in {"request.json", "flask.request.json"}:
        source = f"JSON field {key_name or '<dynamic>'} via request.json.get"
    elif receiver_text:
        source = f"key {key_name or '<dynamic>'} from {receiver_text} via {receiver_text}.get"
    else:
        source = f"key {key_name or '<dynamic>'} via mapping get"

    if default_text is not None:
        source += f" with default {default_text}"
    return source


def _describe_subscript_source(value_node: ast.Subscript) -> str | None:
    mapping_expression = _unparse(value_node.value)
    key_expression = _unparse(value_node.slice)
    if mapping_expression in {"request.args", "flask.request.args"}:
        return f"query parameter {key_expression} via request.args[...]"
    if mapping_expression in {"request.form", "flask.request.form"}:
        return f"form field {key_expression} via request.form[...]"
    if mapping_expression in {"request.json", "flask.request.json"}:
        return f"JSON field {key_expression} via request.json[...]"
    return f"key {key_expression} from {mapping_expression}"


def _call_receiver(value_node: ast.Call) -> ast.AST | None:
    if isinstance(value_node.func, ast.Attribute):
        return value_node.func.value
    return None


def _build_repair_constraints(
    context: CodeContext,
    classification: ErrorClassification,
    suspected_variables: list[str],
) -> list[RepairConstraint]:
    constraints: list[RepairConstraint] = []

    if context.crash_function is not None:
        signature = _function_signature_hint(context.crash_function.name, context.crash_function.parameters)
        returns = f" -> {context.crash_function.returns}" if context.crash_function.returns else ""
        constraints.append(
            RepairConstraint(
                kind="preserve_signature",
                description=f"Keep {signature}{returns} unchanged; do not change callers or route registration.",
            )
        )

    if _has_route_call(context, "divide"):
        constraints.append(
            RepairConstraint(
                kind="preserve_happy_path",
                description="Preserve GET /divide?x=4 response semantics: HTTP 200 with result 25.0.",
            )
        )

    if classification.category == "arithmetic_boundary":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the divisor"
        constraints.extend(
            [
                RepairConstraint(
                    kind="web_api_error_semantics",
                    description=(
                        f"Convert invalid zero boundary for {variable_text} into a controlled 4xx Web API response; "
                        "do not leak a 500 internal_server_error."
                    ),
                ),
                RepairConstraint(
                    kind="avoid_special_float",
                    description=(
                        "Do not return NaN, Infinity, -Infinity, float('nan'), float('inf'), math.nan, or math.inf "
                        "as a repair for invalid user input."
                    ),
                ),
                RepairConstraint(
                    kind="avoid_broad_refactor",
                    description="Prefer a narrow compatible guard; avoid broad Flask app factory or route refactors.",
                ),
            ]
        )
    elif classification.category == "missing_key":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the mapping input"
        constraints.extend(
            [
                RepairConstraint(
                    kind="validate_required_keys",
                    description=(
                        f"Validate required key presence near {variable_text} before direct mapping access; "
                        "prefer explicit validation over allowing KeyError to escape."
                    ),
                ),
                RepairConstraint(
                    kind="preserve_payload_schema",
                    description="Preserve the existing payload and response schema for valid inputs with the required key present.",
                ),
                RepairConstraint(
                    kind="controlled_missing_key_error",
                    description="Convert missing-key input into a controlled validation error; do not mask unrelated exceptions.",
                ),
                RepairConstraint(
                    kind="avoid_broad_refactor",
                    description="Prefer a narrow key-presence/default-value guard over broad request/schema refactors.",
                ),
            ]
        )
    elif classification.category == "null_or_type_mismatch":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the dereferenced object"
        constraints.extend(
            [
                RepairConstraint(
                    kind="validate_none_or_type_before_attribute",
                    description=(
                        f"Validate {variable_text} before attribute access; handle None or incompatible object types explicitly."
                    ),
                ),
                RepairConstraint(
                    kind="preserve_attribute_contract",
                    description="Preserve the expected attribute contract and behavior for valid objects.",
                ),
                RepairConstraint(
                    kind="controlled_attribute_error",
                    description="Return or raise a controlled domain/API error for invalid objects instead of leaking AttributeError.",
                ),
                RepairConstraint(
                    kind="avoid_broad_refactor",
                    description="Prefer a local guard or adapter over broad object-model refactors.",
                ),
            ]
        )
    elif classification.category == "type_mismatch":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the operation input"
        constraints.extend(
            [
                RepairConstraint(
                    kind="validate_or_convert_input_type",
                    description=(
                        f"Validate or deliberately convert {variable_text} before the operation/call; preserve the original contract."
                    ),
                ),
                RepairConstraint(
                    kind="avoid_implicit_broad_cast",
                    description="Avoid broad implicit casts that silently accept unrelated input shapes or hide programmer errors.",
                ),
                RepairConstraint(
                    kind="preserve_call_contract",
                    description="Do not change public function signatures or expected argument meanings to work around the TypeError.",
                ),
                RepairConstraint(
                    kind="avoid_broad_refactor",
                    description="Prefer a targeted type guard/conversion near the failing operation.",
                ),
            ]
        )
    elif classification.category == "invalid_value":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the parsed value"
        constraints.extend(
            [
                RepairConstraint(
                    kind="controlled_value_validation",
                    description=(
                        f"Validate malformed, empty, and out-of-range values for {variable_text}; "
                        "surface a controlled validation failure instead of leaking ValueError."
                    ),
                ),
                RepairConstraint(
                    kind="preserve_valid_input_behavior",
                    description="Preserve behavior for valid values, including existing parsing formats and accepted ranges.",
                ),
                RepairConstraint(
                    kind="validate_parse_boundaries",
                    description="Cover parse boundaries such as empty strings, malformed strings, and boundary numeric/date values.",
                ),
                RepairConstraint(
                    kind="avoid_broad_refactor",
                    description="Prefer a local validation/parsing guard over broad endpoint or data-model rewrites.",
                ),
            ]
        )
    else:
        constraints.append(
            RepairConstraint(
                kind="avoid_broad_refactor",
                description="Prefer a minimal localized change until stronger diagnosis evidence is available.",
            )
        )

    return constraints


def _build_adversarial_hints(
    classification: ErrorClassification,
    suspected_variables: list[str],
    variable_flows: list[VariableFlow],
) -> list[str]:
    common_hints = [
        "Re-run the original failing request and at least one happy-path request after applying the patch."
    ]

    if classification.category == "arithmetic_boundary":
        hints = [
            "GET /divide?x=0 should return controlled 400/422, not 500.",
            "GET /divide?x=-1 should still return HTTP 200 with result -100.0.",
            "GET /divide should still use the default denominator and return HTTP 200 with result 100.0.",
            "GET /bug should also be handled without returning 500.",
            "Repeat GET /divide?x=0 multiple times to check stable non-500 behavior.",
        ]

        if suspected_variables:
            hints.insert(0, f"Exercise the zero boundary for `{', '.join(suspected_variables)}`.")

        if any("query parameter" in flow.source for flow in variable_flows):
            hints.append("Probe both query names when supported: denominator=0 and x=0.")

        return hints

    if classification.category == "missing_key":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the mapping payload"
        return [
            f"Test request/payload missing the required key around `{variable_text}`.",
            "Test an empty payload/mapping and verify it fails with a controlled validation response.",
            "Test payload with the required key present and verify the original happy path still succeeds.",
            "Test payload with extra unrelated keys to ensure schema-compatible inputs still work.",
            *common_hints,
        ]

    if classification.category == "null_or_type_mismatch":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the dereferenced object"
        return [
            f"Test `{variable_text}` as None/null before attribute access.",
            "Test an object that lacks the required attribute and verify controlled failure semantics.",
            "Test a valid object with the required attribute and verify unchanged happy-path behavior.",
            "Test a wrong-type object to ensure the guard is explicit and narrow.",
            *common_hints,
        ]

    if classification.category == "type_mismatch":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the suspicious input"
        return [
            f"Test string, numeric, list/dict, and null variants for `{variable_text}`.",
            "Test the original happy path to ensure valid typed input still follows the same contract.",
            "Test boundary values for numeric/string conversions without accepting unrelated shapes silently.",
            *common_hints,
        ]

    if classification.category == "invalid_value":
        variable_text = ", ".join(suspected_variables) if suspected_variables else "the parsed input"
        return [
            f"Test malformed, empty-string, null, and boundary values for `{variable_text}`.",
            "Test at least one valid value and verify parsing/validation behavior is unchanged.",
            "Test repeated invalid values to ensure the repair is stable and does not leak ValueError.",
            *common_hints,
        ]

    return common_hints


def _parse_call_from_line(line: str, target_function_name: str) -> ast.Call | None:
    tree = _parse_python(line.strip())
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = _callee_name(node.func)
            if callee == target_function_name or (callee and callee.endswith(f".{target_function_name}")):
                return node
    return None


def _argument_for_parameter(
    call_node: ast.Call,
    parameter_names: list[str],
    parameter_name: str,
) -> ast.AST | None:
    for keyword in call_node.keywords:
        if keyword.arg == parameter_name:
            return keyword.value

    try:
        index = parameter_names.index(parameter_name)
    except ValueError:
        return None

    if index < len(call_node.args):
        return call_node.args[index]
    return None


def _find_function_by_qualname(tree: ast.AST, qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    visitor = _FunctionQualnameVisitor()
    visitor.visit(tree)
    return visitor.functions.get(qualname)


class _FunctionQualnameVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = ".".join([*self.stack, node.name]) if self.stack else node.name
        self.functions[qualname] = node
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


def _target_contains_name(target: ast.AST, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_contains_name(item, name) for item in target.elts)
    return False


def _is_identifier_name(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value)


def _node_spans_line(node: ast.AST, line_number: int) -> bool:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    return start <= line_number <= end


def _division_operation_symbol(operation: ast.operator) -> str | None:
    for operation_type, symbol in DIVISION_OPERATIONS.items():
        if isinstance(operation, operation_type):
            return symbol
    return None


def _guess_right_operand_from_line(line: str) -> str | None:
    stripped = line.strip()
    for operator in ("//", "/", "%"):
        if operator in stripped:
            right = stripped.rsplit(operator, maxsplit=1)[-1].strip()
            return right or None
    return None


def _function_signature_hint(name: str, parameters: list[str]) -> str:
    return f"{name}({', '.join(parameters)})"


def _has_route_call(context: CodeContext, route_function_name: str) -> bool:
    return any(
        call.is_route_handler and call.caller_function and call.caller_function.endswith(f".{route_function_name}")
        for call in context.incoming_calls
    )


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _callee_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _callee_name(node.func)
    if isinstance(node, ast.Subscript):
        return _callee_name(node.value)
    return None


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _line_at(lines: list[str], line_number: int) -> str | None:
    if line_number <= 0 or line_number > len(lines):
        return None
    return lines[line_number - 1]


def _parse_python(source: str) -> ast.AST | None:
    if not source:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__
