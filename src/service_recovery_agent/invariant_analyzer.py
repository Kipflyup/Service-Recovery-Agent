"""Infer review-only hidden invariant hints from deterministic code context.

The analyzer in this module is deliberately conservative.  It does not create
hard gates and it does not claim semantic certainty.  Instead, it extracts
review hints from evidence that is cheap and deterministic to inspect:

- crash function annotations, docstring, and name;
- direct callers and route decorators found by ``CodeContext``;
- nearby caller return behavior;
- tests that exercise the crash function directly or exercise affected routes.

The resulting ``DeepImpactAssessment`` can be formatted into prompt evidence or
review notes.  It should remain advisory until stronger validation proves the
candidate invariant.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .code_context import CodeContext, DEFAULT_EXCLUDED_DIRS, iter_python_files


@dataclass(frozen=True)
class InvariantCandidate:
    """A review-only hidden invariant candidate inferred from static evidence."""

    kind: str
    confidence: str
    source: str
    description: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeepImpactAssessment:
    """Advisory deeper impact assessment for prompt/review use."""

    propagation_depth: int
    transitive_call_count: int
    invariant_candidates: list[InvariantCandidate] = field(default_factory=list)
    hidden_risk_level: str = "low"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "propagation_depth": self.propagation_depth,
            "transitive_call_count": self.transitive_call_count,
            "invariant_candidates": [candidate.to_dict() for candidate in self.invariant_candidates],
            "hidden_risk_level": self.hidden_risk_level,
            "reasons": self.reasons,
        }


def analyze_hidden_invariants(
    context: CodeContext,
    *,
    project_root: str | os.PathLike[str] | None = None,
    excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
    max_candidates: int = 20,
) -> DeepImpactAssessment:
    """Infer advisory hidden invariant candidates from ``CodeContext``.

    The function is fully local/offline: it only reads Python files under the
    project root, does not invoke Git, does not call an LLM, and does not send
    any network notifications.
    """

    root = Path(project_root or context.project_root).resolve()
    candidates: list[InvariantCandidate] = []
    candidates.extend(_return_type_candidates(context))
    candidates.extend(_docstring_candidates(context))
    candidates.extend(_name_candidates(context))
    candidates.extend(_caller_candidates(context, root))
    candidates.extend(_test_candidates(context, root, excluded_dirs=excluded_dirs))

    deduplicated = _deduplicate_candidates(candidates)[:max_candidates]
    transitive_call_count = _transitive_call_count(context, root, excluded_dirs=excluded_dirs)
    propagation_depth = 2 if transitive_call_count else (1 if context.incoming_calls else 0)
    hidden_risk_level = _hidden_risk_level(context, deduplicated, transitive_call_count)
    reasons = _build_reasons(context, deduplicated, propagation_depth, transitive_call_count)

    return DeepImpactAssessment(
        propagation_depth=propagation_depth,
        transitive_call_count=transitive_call_count,
        invariant_candidates=deduplicated,
        hidden_risk_level=hidden_risk_level,
        reasons=reasons,
    )


def format_hidden_invariant_evidence(assessment: DeepImpactAssessment) -> str:
    """Render hidden invariant evidence for prompts or human review."""

    lines: list[str] = []
    lines.append("# Hidden Invariant Evidence")
    lines.append("")
    lines.append(f"- Hidden risk level: {assessment.hidden_risk_level}")
    lines.append(f"- Propagation depth: {assessment.propagation_depth}")
    lines.append(f"- Transitive caller count: {assessment.transitive_call_count}")
    lines.append("- Scope: advisory prompt/review evidence only; this is not a hard validation gate.")

    lines.append("")
    lines.append("## Reasons")
    if assessment.reasons:
        for reason in assessment.reasons:
            lines.append(f"- {reason}")
    else:
        lines.append("- No additional hidden-impact reasons inferred.")

    lines.append("")
    lines.append("## Invariant Candidates")
    if not assessment.invariant_candidates:
        lines.append("- No hidden invariant candidates inferred.")
    for candidate in assessment.invariant_candidates:
        lines.append(
            f"- [{candidate.confidence}] **{candidate.kind}** "
            f"from {candidate.source}: {candidate.description}"
        )
        for evidence in candidate.evidence:
            lines.append(f"  - evidence: `{evidence.strip()}`")

    return "\n".join(lines)


def _return_type_candidates(context: CodeContext) -> list[InvariantCandidate]:
    function = context.crash_function
    if function is None or not function.returns:
        return []

    return [
        InvariantCandidate(
            kind="return_type_contract",
            confidence="medium",
            source="type_hint",
            description=(
                f"`{function.qualname}` is annotated as returning `{function.returns}`; "
                "repairs should preserve a compatible return type unless explicitly reviewed."
            ),
            evidence=[f"{function.file}:{function.start_line} returns {function.returns}"],
        )
    ]


def _docstring_candidates(context: CodeContext) -> list[InvariantCandidate]:
    function = context.crash_function
    if function is None or not function.docstring:
        return []

    docstring = " ".join(function.docstring.split())
    lower = docstring.lower()
    candidates: list[InvariantCandidate] = []

    def add(kind: str, description: str, confidence: str = "medium") -> None:
        candidates.append(
            InvariantCandidate(
                kind=kind,
                confidence=confidence,
                source="docstring",
                description=description,
                evidence=[f"{function.qualname} docstring: {docstring}"],
            )
        )

    if "non-negative" in lower or "nonnegative" in lower or ">= 0" in lower:
        add("return_non_negative", "Docstring suggests callers may rely on a non-negative result.")
    if "sorted" in lower:
        add("sorted_collection_contract", "Docstring suggests returned or mutated collections should remain sorted.")
    if "lowercase" in lower or "lower-case" in lower:
        add("lowercase_key_contract", "Docstring suggests key/string casing is part of the contract.")
    if "non-null" in lower or "nonnull" in lower or "not none" in lower:
        add("return_non_null", "Docstring suggests callers may rely on a non-null result.")

    if not candidates and any(marker in lower for marker in ("always", "never", "must", "expects", "returns")):
        add("docstring_behavior_contract", "Docstring contains behavioral language that may encode an implicit contract.", "low")

    return candidates


def _name_candidates(context: CodeContext) -> list[InvariantCandidate]:
    function = context.crash_function
    if function is None:
        return []

    name = function.name
    lower = name.lower()
    candidates: list[InvariantCandidate] = []
    evidence = [f"function name `{function.qualname}`"]

    if lower.startswith(("is_", "has_", "can_", "should_")):
        confidence = "medium" if function.returns == "bool" else "low"
        candidates.append(
            InvariantCandidate(
                kind="boolean_return_contract",
                confidence=confidence,
                source="name",
                description=(
                    f"`{name}` is named like a predicate; callers may expect boolean-like semantics."
                ),
                evidence=[*evidence, f"return annotation: {function.returns or '<missing>'}"],
            )
        )

    if any(token in lower for token in ("divide", "ratio", "percent", "average", "score", "amount", "total", "count")):
        confidence = "medium" if function.returns in {"int", "float"} else "low"
        candidates.append(
            InvariantCandidate(
                kind="numeric_result_contract",
                confidence=confidence,
                source="name",
                description=(
                    f"`{name}` looks numeric; repairs should preserve numeric happy-path semantics."
                ),
                evidence=[*evidence, f"return annotation: {function.returns or '<missing>'}"],
            )
        )

    if lower.startswith(("get_", "load_", "fetch_", "find_")):
        candidates.append(
            InvariantCandidate(
                kind="lookup_result_contract",
                confidence="low",
                source="name",
                description=(
                    f"`{name}` looks like a lookup/load helper; callers may rely on existing missing-value semantics."
                ),
                evidence=evidence,
            )
        )

    return candidates


def _caller_candidates(context: CodeContext, root: Path) -> list[InvariantCandidate]:
    candidates: list[InvariantCandidate] = []
    for call in context.incoming_calls:
        if call.is_route_handler:
            route_paths = _route_paths_from_decorators(call.caller_decorators)
            route_text = ", ".join(route_paths) if route_paths else "route handler"
            candidates.append(
                InvariantCandidate(
                    kind="route_handler_contract",
                    confidence="medium",
                    source="caller",
                    description=(
                        f"`{call.caller_function or '<module>'}` is a route handler for {route_text}; "
                        "preserve externally visible response semantics unless explicitly validated."
                    ),
                    evidence=[
                        f"{call.file}:{call.line_number} {call.code.strip()}",
                        *call.caller_decorators,
                    ],
                )
            )

        if _call_result_is_assigned(call.code):
            candidates.append(
                InvariantCandidate(
                    kind="caller_result_usage_contract",
                    confidence="low",
                    source="caller",
                    description=(
                        f"`{call.caller_function or '<module>'}` stores the crash-function result; "
                        "downstream code may rely on its shape/type."
                    ),
                    evidence=[f"{call.file}:{call.line_number} {call.code.strip()}"],
                )
            )

        caller_return_evidence = _caller_return_evidence(root, call.file, call.caller_function)
        if caller_return_evidence:
            candidates.append(
                InvariantCandidate(
                    kind="caller_return_shape_contract",
                    confidence="medium" if call.is_route_handler else "low",
                    source="caller",
                    description=(
                        f"`{call.caller_function or '<module>'}` returns a structured value/status after the call; "
                        "repair should not silently change this response shape."
                    ),
                    evidence=caller_return_evidence,
                )
            )

    return candidates


def _test_candidates(
    context: CodeContext,
    root: Path,
    *,
    excluded_dirs: Iterable[str],
) -> list[InvariantCandidate]:
    function = context.crash_function
    if function is None:
        return []

    route_paths = sorted(
        {
            route_path
            for call in context.incoming_calls
            for route_path in _route_paths_from_decorators(call.caller_decorators)
        }
    )
    function_name = function.name

    candidates: list[InvariantCandidate] = []
    for file_path in iter_python_files(root, excluded_dirs=excluded_dirs):
        relative = _relative_or_absolute(file_path, root)
        if not _looks_like_test_file(file_path):
            continue

        source = _read_text(file_path)
        tree = _parse_python(source, file_path)
        if tree is None:
            continue
        if _module_has_seed_failure_pytestmark(tree):
            continue
        source_lines = source.splitlines()

        for test_function in _test_functions(tree):
            if _has_seed_failure_decorator(test_function):
                continue

            start = getattr(test_function, "lineno", 1)
            end = getattr(test_function, "end_lineno", start)
            function_lines = source_lines[start - 1:end]
            function_source = "\n".join(function_lines)
            matched_routes = [route_path for route_path in route_paths if route_path in function_source]
            direct_call = f"{function_name}(" in function_source
            if not matched_routes and not direct_call:
                continue

            status_evidence = _matching_assertion_lines(
                function_lines,
                relative,
                start,
                include_markers=("status_code",),
            )
            if matched_routes and status_evidence:
                candidates.append(
                    InvariantCandidate(
                        kind="tested_route_status_contract",
                        confidence="high",
                        source="test",
                        description=(
                            f"Tests exercise affected route(s) {', '.join(matched_routes)} and assert status-code behavior."
                        ),
                        evidence=status_evidence,
                    )
                )

            response_shape_evidence = _matching_assertion_lines(
                function_lines,
                relative,
                start,
                include_markers=("get_json", "[\"result\"]", "['result']"),
            )
            if matched_routes and response_shape_evidence:
                candidates.append(
                    InvariantCandidate(
                        kind="tested_response_shape_contract",
                        confidence="high",
                        source="test",
                        description=(
                            f"Tests exercise affected route(s) {', '.join(matched_routes)} and assert response payload shape/value."
                        ),
                        evidence=response_shape_evidence,
                    )
                )

            direct_function_evidence = _matching_assertion_lines(
                function_lines,
                relative,
                start,
                include_markers=(function_name,),
            )
            if direct_call and direct_function_evidence:
                candidates.append(
                    InvariantCandidate(
                        kind="tested_function_behavior_contract",
                        confidence="high",
                        source="test",
                        description=(
                            f"Tests call `{function_name}` directly; preserve the tested happy-path behavior."
                        ),
                        evidence=direct_function_evidence,
                    )
                )

    return candidates


def _transitive_call_count(
    context: CodeContext,
    root: Path,
    *,
    excluded_dirs: Iterable[str],
) -> int:
    direct_callers = {
        call.caller_function
        for call in context.incoming_calls
        if call.caller_function is not None
    }
    if not direct_callers:
        return 0

    direct_basenames = {caller.rsplit(".", maxsplit=1)[-1] for caller in direct_callers}
    transitive_callers: set[str] = set()
    for file_path in iter_python_files(root, excluded_dirs=excluded_dirs):
        source = _read_text(file_path)
        tree = _parse_python(source, file_path)
        if tree is None:
            continue
        visitor = _CallGraphVisitor()
        visitor.visit(tree)
        for caller, callees in visitor.calls_by_function.items():
            if caller in direct_callers:
                continue
            if any(_callee_matches(callee, direct_basenames) for callee in callees):
                transitive_callers.add(caller)
    return len(transitive_callers)


def _hidden_risk_level(
    context: CodeContext,
    candidates: list[InvariantCandidate],
    transitive_call_count: int,
) -> str:
    high_confidence_count = sum(1 for candidate in candidates if candidate.confidence == "high")
    if context.impact.risk_level == "high" or transitive_call_count > 3 or high_confidence_count >= 3:
        return "high"
    if context.impact.risk_level == "medium" or context.incoming_calls or high_confidence_count or len(candidates) >= 3:
        return "medium"
    return "low"


def _build_reasons(
    context: CodeContext,
    candidates: list[InvariantCandidate],
    propagation_depth: int,
    transitive_call_count: int,
) -> list[str]:
    reasons = [
        f"{len(candidates)} hidden invariant candidate(s) inferred from type hints, docstrings, names, callers, or tests.",
        f"Propagation depth is {propagation_depth}; {len(context.incoming_calls)} direct caller(s), {transitive_call_count} transitive caller(s).",
        "Candidates are advisory prompt/review hints only and are not hard gates.",
    ]
    if any(candidate.source == "test" for candidate in candidates):
        reasons.append("At least one candidate is backed by tests; preserve tested behavior unless explicitly changing the contract.")
    if any(candidate.kind == "route_handler_contract" for candidate in candidates):
        reasons.append("At least one direct caller is a route handler; externally visible API semantics may be affected.")
    return reasons


def _route_paths_from_decorators(decorators: list[str]) -> list[str]:
    route_paths: list[str] = []
    for decorator in decorators:
        for match in re.finditer(r"""['"](/[^'"]*)['"]""", decorator):
            path = match.group(1)
            if path not in route_paths:
                route_paths.append(path)
    return route_paths


def _call_result_is_assigned(code: str) -> bool:
    stripped = code.strip()
    return "=" in stripped and "==" not in stripped and not stripped.startswith("return ")


def _caller_return_evidence(root: Path, relative_file: str, caller_function: str | None) -> list[str]:
    if caller_function is None:
        return []
    file_path = root / relative_file
    source = _read_text(file_path)
    tree = _parse_python(source, file_path)
    if tree is None:
        return []

    function_node = _find_function_by_qualname(tree, caller_function)
    if function_node is None:
        return []

    source_lines = source.splitlines()
    evidence: list[str] = []
    for node in ast.walk(function_node):
        if not isinstance(node, ast.Return):
            continue
        lineno = getattr(node, "lineno", 0)
        line = _line_at(source_lines, lineno)
        if line and _looks_like_contract_return(line):
            evidence.append(f"{relative_file}:{lineno} {line.strip()}")
    return evidence[:3]


def _looks_like_contract_return(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("return ") and (
        ", 200" in stripped
        or "jsonify(" in stripped
        or "{\"" in stripped
        or "{'" in stripped
        or "result" in stripped
    )


def _matching_assertion_lines(
    function_lines: list[str],
    relative_file: str,
    start_line: int,
    *,
    include_markers: tuple[str, ...],
) -> list[str]:
    evidence: list[str] = []
    for offset, line in enumerate(function_lines):
        stripped = line.strip()
        if not stripped.startswith("assert "):
            continue
        if any(marker in stripped for marker in include_markers):
            evidence.append(f"{relative_file}:{start_line + offset} {stripped}")
    return evidence[:5]


def _looks_like_test_file(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or "tests" in path.parts


def _test_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            functions.append(node)
    return sorted(functions, key=lambda node: getattr(node, "lineno", 0))


def _has_seed_failure_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any("seed_failure" in _unparse(decorator) for decorator in node.decorator_list)


def _module_has_seed_failure_pytestmark(tree: ast.AST) -> bool:
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets):
            continue
        if "seed_failure" in _unparse(node.value):
            return True
    return False


def _deduplicate_candidates(candidates: list[InvariantCandidate]) -> list[InvariantCandidate]:
    deduplicated: list[InvariantCandidate] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    for candidate in sorted(
        candidates,
        key=lambda item: (
            confidence_order.get(item.confidence, 3),
            item.source,
            item.kind,
            item.description,
            item.evidence,
        ),
    ):
        key = (candidate.kind, candidate.source, candidate.description, tuple(candidate.evidence))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return deduplicated


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


class _CallGraphVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.calls_by_function: dict[str, set[str]] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if self.stack:
            callee = _callee_name(node.func)
            if callee:
                caller = ".".join(self.stack)
                self.calls_by_function.setdefault(caller, set()).add(callee)
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = ".".join([*self.stack, node.name]) if self.stack else node.name
        self.stack.append(node.name)
        self.calls_by_function.setdefault(qualname, set())
        self.generic_visit(node)
        self.stack.pop()


def _callee_matches(callee: str, target_basenames: set[str]) -> bool:
    return callee in target_basenames or any(callee.endswith(f".{name}") for name in target_basenames)


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


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _line_at(lines: list[str], line_number: int) -> str | None:
    if line_number <= 0 or line_number > len(lines):
        return None
    return lines[line_number - 1]


def _parse_python(source: str, file: Path | str) -> ast.AST | None:
    if not source:
        return None
    try:
        return ast.parse(source, filename=str(file))
    except SyntaxError:
        return None


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__
