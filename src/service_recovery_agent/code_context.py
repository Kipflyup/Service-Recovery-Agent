"""Build structured code context from a parsed traceback.

The Traceback parser tells the Agent *where* the crash happened.  This module
adds the next layer of deterministic context:

- what source code surrounds the crash line;
- which function/class owns that line;
- who calls the crashed function inside the repository;
- what the likely blast radius is if that function changes.

The implementation uses Python's built-in ``ast`` module.  That keeps this
phase deterministic and dependency-free while still giving the LLM a much more
grounded prompt than a raw log string.
"""

from __future__ import annotations

import ast
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .traceback_parser import StackFrame, TracebackEvent, summarize_traceback


DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".trae",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "logs",
    "node_modules",
    "venv",
}


@dataclass(frozen=True)
class SourceSnippet:
    file: str
    start_line: int
    end_line: int
    code: str
    highlighted_line: int | None = None

    def numbered_code(self) -> str:
        lines = self.code.splitlines()
        rendered: list[str] = []
        for offset, line in enumerate(lines, start=self.start_line):
            marker = ">>" if offset == self.highlighted_line else "  "
            rendered.append(f"{marker} {offset:4d} | {line}")
        return "\n".join(rendered)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FunctionContext:
    file: str
    name: str
    qualname: str
    start_line: int
    end_line: int
    source: str
    decorators: list[str]
    parameters: list[str]
    returns: str | None
    docstring: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CallSite:
    file: str
    line_number: int
    caller_function: str | None
    caller_decorators: list[str]
    callee: str
    code: str
    is_route_handler: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImpactAssessment:
    risk_level: str
    direct_call_count: int
    affected_file_count: int
    route_handler_count: int
    project_traceback_frame_count: int
    affected_functions: list[str]
    affected_files: list[str]
    reasons: list[str]
    recommended_change_scope: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodeContext:
    traceback: TracebackEvent
    project_root: str
    crash_file: str
    crash_file_relative: str
    crash_line_number: int
    crash_line_code: str | None
    surrounding_source: SourceSnippet | None
    crash_function: FunctionContext | None
    project_traceback_frames: list[StackFrame]
    incoming_calls: list[CallSite]
    outgoing_calls: list[str]
    impact: ImpactAssessment

    def to_dict(self) -> dict[str, Any]:
        return {
            "traceback": self.traceback.to_dict(),
            "project_root": self.project_root,
            "crash_file": self.crash_file,
            "crash_file_relative": self.crash_file_relative,
            "crash_line_number": self.crash_line_number,
            "crash_line_code": self.crash_line_code,
            "surrounding_source": self.surrounding_source.to_dict() if self.surrounding_source else None,
            "crash_function": self.crash_function.to_dict() if self.crash_function else None,
            "project_traceback_frames": [frame.to_dict() for frame in self.project_traceback_frames],
            "incoming_calls": [call.to_dict() for call in self.incoming_calls],
            "outgoing_calls": self.outgoing_calls,
            "impact": self.impact.to_dict(),
        }


@dataclass(frozen=True)
class _FunctionRecord:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    context: FunctionContext


def build_code_context(
    traceback_event: TracebackEvent,
    *,
    project_root: str | os.PathLike[str] = ".",
    context_lines: int = 6,
    excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
) -> CodeContext:
    """Build deterministic code context for the crash frame in a traceback."""

    crash_frame = traceback_event.crash_frame
    if crash_frame is None:
        raise ValueError("TracebackEvent does not contain any stack frames.")

    root = Path(project_root).resolve()
    crash_file = _resolve_frame_file(crash_frame.file, root)
    crash_file_relative = _relative_or_absolute(crash_file, root)

    source = _read_text(crash_file)
    source_lines = source.splitlines()
    crash_line_code = _line_at(source_lines, crash_frame.line_number)
    surrounding_source = _build_source_snippet(
        file=crash_file_relative,
        lines=source_lines,
        center_line=crash_frame.line_number,
        context_lines=context_lines,
    )

    crash_function: FunctionContext | None = None
    outgoing_calls: list[str] = []
    if source:
        tree = _parse_python(source, crash_file)
        if tree is not None:
            records = _collect_function_records(tree, source, crash_file_relative)
            crash_record = _find_enclosing_function(records, crash_frame.line_number)
            if crash_record is not None:
                crash_function = crash_record.context
                outgoing_calls = sorted(_collect_outgoing_calls(crash_record.node))

    excluded = set(excluded_dirs)
    project_traceback_frames = [
        frame for frame in traceback_event.frames
        if _is_project_source_path(_resolve_frame_file(frame.file, root), root, excluded)
    ]

    incoming_calls: list[CallSite] = []
    if crash_function is not None:
        incoming_calls = find_callers(
            crash_function.name,
            project_root=root,
            excluded_dirs=excluded_dirs,
            exclude_function_qualname=crash_function.qualname,
        )

    impact = assess_impact(
        crash_function=crash_function,
        incoming_calls=incoming_calls,
        project_traceback_frames=project_traceback_frames,
    )

    return CodeContext(
        traceback=traceback_event,
        project_root=str(root),
        crash_file=str(crash_file),
        crash_file_relative=crash_file_relative,
        crash_line_number=crash_frame.line_number,
        crash_line_code=crash_line_code,
        surrounding_source=surrounding_source,
        crash_function=crash_function,
        project_traceback_frames=project_traceback_frames,
        incoming_calls=incoming_calls,
        outgoing_calls=outgoing_calls,
        impact=impact,
    )


def find_callers(
    target_function_name: str,
    *,
    project_root: str | os.PathLike[str] = ".",
    excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
    exclude_function_qualname: str | None = None,
) -> list[CallSite]:
    """Find direct call sites for ``target_function_name`` in Python files."""

    root = Path(project_root).resolve()
    call_sites: list[CallSite] = []

    for file_path in iter_python_files(root, excluded_dirs=excluded_dirs):
        source = _read_text(file_path)
        tree = _parse_python(source, file_path)
        if tree is None:
            continue

        relative_file = _relative_or_absolute(file_path, root)
        visitor = _CallSiteVisitor(
            target_function_name=target_function_name,
            file=relative_file,
            source_lines=source.splitlines(),
            exclude_function_qualname=exclude_function_qualname,
        )
        visitor.visit(tree)
        call_sites.extend(visitor.call_sites)

    return sorted(call_sites, key=lambda item: (item.file, item.line_number, item.caller_function or ""))


def iter_python_files(
    project_root: str | os.PathLike[str],
    *,
    excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
) -> Iterable[Path]:
    """Yield Python files under ``project_root`` while skipping local artifacts."""

    root = Path(project_root).resolve()
    excluded = set(excluded_dirs)
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in excluded]
        current = Path(current_root)
        for filename in filenames:
            if filename.endswith(".py"):
                yield current / filename


def assess_impact(
    *,
    crash_function: FunctionContext | None,
    incoming_calls: list[CallSite],
    project_traceback_frames: list[StackFrame],
) -> ImpactAssessment:
    """Return a small heuristic blast-radius assessment.

    This is intentionally conservative.  It does not claim semantic certainty;
    it gives the Agent and reviewer a deterministic risk estimate before any LLM
    patch is generated.
    """

    affected_files = sorted({call.file for call in incoming_calls})
    affected_functions = sorted(
        {
            call.caller_function
            for call in incoming_calls
            if call.caller_function is not None
        }
    )
    route_handler_count = sum(1 for call in incoming_calls if call.is_route_handler)
    direct_call_count = len(incoming_calls)

    reasons = [
        f"{direct_call_count} direct call site(s) found across {len(affected_files)} file(s).",
        f"{len(project_traceback_frames)} traceback frame(s) are inside the project root.",
    ]
    if crash_function:
        reasons.append(
            f"Crash line is inside function {crash_function.qualname} "
            f"({crash_function.file}:{crash_function.start_line}-{crash_function.end_line})."
        )
    else:
        reasons.append("Could not map the crash line to an enclosing Python function.")

    if route_handler_count:
        reasons.append(f"{route_handler_count} direct caller(s) look like web route handlers.")

    if direct_call_count == 0:
        risk_level = "low"
        recommended_change_scope = "Function-local change is likely safe, but verify because no static callers were found."
    elif route_handler_count >= 3 or direct_call_count > 5 or len(affected_files) > 3:
        risk_level = "high"
        recommended_change_scope = (
            "Avoid signature/contract changes. Prefer a narrow internal guard and require full regression tests."
        )
    elif route_handler_count > 0 or direct_call_count > 2 or len(affected_files) > 1:
        risk_level = "medium"
        recommended_change_scope = (
            "Prefer a function-internal compatible fix. Do not change parameters, return type, or exception semantics "
            "without explicit validation."
        )
    else:
        risk_level = "low"
        recommended_change_scope = "Prefer a minimal function-internal fix and keep the public contract unchanged."

    return ImpactAssessment(
        risk_level=risk_level,
        direct_call_count=direct_call_count,
        affected_file_count=len(affected_files),
        route_handler_count=route_handler_count,
        project_traceback_frame_count=len(project_traceback_frames),
        affected_functions=affected_functions,
        affected_files=affected_files,
        reasons=reasons,
        recommended_change_scope=recommended_change_scope,
    )


def format_code_context_report(context: CodeContext) -> str:
    """Render a readable report for terminal demos and LLM prompt inspection."""

    lines: list[str] = []
    lines.append("# Code Context Report")
    lines.append("")
    lines.append("## Traceback")
    lines.append(f"- Summary: {summarize_traceback(context.traceback)}")
    lines.append(f"- Exception: {context.traceback.exception_type}: {context.traceback.exception_message}")
    lines.append(f"- Crash site: {context.crash_file_relative}:{context.crash_line_number}")
    if context.crash_line_code:
        lines.append(f"- Crash line: `{context.crash_line_code.strip()}`")

    lines.append("")
    lines.append("## Enclosing Function")
    if context.crash_function:
        function = context.crash_function
        lines.append(f"- Function: `{function.qualname}`")
        lines.append(f"- Location: {function.file}:{function.start_line}-{function.end_line}")
        if function.parameters:
            lines.append(f"- Parameters: {', '.join(function.parameters)}")
        if function.returns:
            lines.append(f"- Returns: {function.returns}")
        if function.decorators:
            lines.append(f"- Decorators: {', '.join(function.decorators)}")
        lines.append("")
        lines.append("```python")
        lines.append(function.source)
        lines.append("```")
    else:
        lines.append("- No enclosing Python function found.")

    lines.append("")
    lines.append("## Source Around Crash")
    if context.surrounding_source:
        lines.append("```text")
        lines.append(context.surrounding_source.numbered_code())
        lines.append("```")
    else:
        lines.append("- Source file could not be read.")

    lines.append("")
    lines.append("## Direct Callers")
    if context.incoming_calls:
        for call in context.incoming_calls:
            route_marker = " route-handler" if call.is_route_handler else ""
            caller = call.caller_function or "<module>"
            lines.append(
                f"- `{caller}`{route_marker} at {call.file}:{call.line_number} "
                f"calls `{call.callee}`: `{call.code.strip()}`"
            )
            if call.caller_decorators:
                lines.append(f"  - decorators: {', '.join(call.caller_decorators)}")
    else:
        lines.append("- No direct static callers found in project Python files.")

    lines.append("")
    lines.append("## Outgoing Calls From Crashed Function")
    if context.outgoing_calls:
        lines.append("- " + ", ".join(f"`{call}`" for call in context.outgoing_calls))
    else:
        lines.append("- No outgoing calls detected.")

    lines.append("")
    lines.append("## Impact Assessment")
    lines.append(f"- Risk level: **{context.impact.risk_level}**")
    lines.append(f"- Direct call sites: {context.impact.direct_call_count}")
    lines.append(f"- Affected files: {context.impact.affected_file_count}")
    lines.append(f"- Route handlers affected: {context.impact.route_handler_count}")
    lines.append(f"- Recommended scope: {context.impact.recommended_change_scope}")
    lines.append("- Reasons:")
    for reason in context.impact.reasons:
        lines.append(f"  - {reason}")

    return "\n".join(lines)


class _FunctionRecordVisitor(ast.NodeVisitor):
    def __init__(self, source: str, file: str) -> None:
        self.source = source
        self.source_lines = source.splitlines()
        self.file = file
        self.stack: list[str] = []
        self.records: list[_FunctionRecord] = []

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
        context = _function_context_from_node(node, self.source, self.source_lines, self.file, qualname)
        self.records.append(_FunctionRecord(node=node, context=context))

        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


class _CallSiteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        target_function_name: str,
        file: str,
        source_lines: list[str],
        exclude_function_qualname: str | None = None,
    ) -> None:
        self.target_function_name = target_function_name
        self.file = file
        self.source_lines = source_lines
        self.exclude_function_qualname = exclude_function_qualname
        self.stack: list[str] = []
        self.decorator_stack: list[list[str]] = []
        self.call_sites: list[CallSite] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.stack.append(node.name)
        self.decorator_stack.append([])
        self.generic_visit(node)
        self.decorator_stack.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> Any:
        callee = _callee_name(node.func)
        if callee and _call_matches_target(callee, self.target_function_name):
            caller_function = ".".join(self.stack) if self.stack else None
            if caller_function != self.exclude_function_qualname:
                decorators = self.decorator_stack[-1] if self.decorator_stack else []
                self.call_sites.append(
                    CallSite(
                        file=self.file,
                        line_number=getattr(node, "lineno", 0),
                        caller_function=caller_function,
                        caller_decorators=decorators,
                        callee=callee,
                        code=_line_at(self.source_lines, getattr(node, "lineno", 0)) or "",
                        is_route_handler=any(_is_route_decorator(decorator) for decorator in decorators),
                    )
                )

        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        decorators = [_unparse(decorator) for decorator in node.decorator_list]
        self.stack.append(node.name)
        self.decorator_stack.append(decorators)
        self.generic_visit(node)
        self.decorator_stack.pop()
        self.stack.pop()


def _collect_function_records(tree: ast.AST, source: str, file: str) -> list[_FunctionRecord]:
    visitor = _FunctionRecordVisitor(source, file)
    visitor.visit(tree)
    return visitor.records


def _find_enclosing_function(records: list[_FunctionRecord], line_number: int) -> _FunctionRecord | None:
    containing = [
        record for record in records
        if record.context.start_line <= line_number <= record.context.end_line
    ]
    if not containing:
        return None
    return min(containing, key=lambda record: record.context.end_line - record.context.start_line)


def _function_context_from_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    source_lines: list[str],
    file: str,
    qualname: str,
) -> FunctionContext:
    start_line = node.lineno
    end_line = getattr(node, "end_lineno", node.lineno)
    function_source = "\n".join(source_lines[start_line - 1:end_line])
    parameters = _parameters_from_node(node)
    returns = _unparse(node.returns) if node.returns else None
    decorators = [_unparse(decorator) for decorator in node.decorator_list]

    return FunctionContext(
        file=file,
        name=node.name,
        qualname=qualname,
        start_line=start_line,
        end_line=end_line,
        source=function_source,
        decorators=decorators,
        parameters=parameters,
        returns=returns,
        docstring=ast.get_docstring(node),
    )


def _parameters_from_node(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = node.args
    parameters: list[str] = []
    parameters.extend(arg.arg for arg in args.posonlyargs)
    parameters.extend(arg.arg for arg in args.args)
    if args.vararg:
        parameters.append(f"*{args.vararg.arg}")
    parameters.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        parameters.append(f"**{args.kwarg.arg}")
    return parameters


def _collect_outgoing_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    calls: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            callee = _callee_name(child.func)
            if callee:
                calls.add(callee)
    return calls


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


def _call_matches_target(callee: str, target_function_name: str) -> bool:
    return callee == target_function_name or callee.endswith(f".{target_function_name}")


def _is_route_decorator(decorator: str) -> bool:
    return any(
        marker in decorator
        for marker in (
            ".route(",
            ".get(",
            ".post(",
            ".put(",
            ".patch(",
            ".delete(",
        )
    )


def _resolve_frame_file(frame_file: str, project_root: Path) -> Path:
    path = Path(frame_file)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_project_source_path(path: Path, root: Path, excluded_dirs: set[str]) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return not any(part in excluded_dirs for part in relative.parts)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_python(source: str, file: Path | str) -> ast.AST | None:
    if not source:
        return None
    try:
        return ast.parse(source, filename=str(file))
    except SyntaxError:
        return None


def _line_at(lines: list[str], line_number: int) -> str | None:
    if line_number <= 0 or line_number > len(lines):
        return None
    return lines[line_number - 1]


def _build_source_snippet(
    *,
    file: str,
    lines: list[str],
    center_line: int,
    context_lines: int,
) -> SourceSnippet | None:
    if not lines:
        return None

    start_line = max(1, center_line - context_lines)
    end_line = min(len(lines), center_line + context_lines)
    return SourceSnippet(
        file=file,
        start_line=start_line,
        end_line=end_line,
        code="\n".join(lines[start_line - 1:end_line]),
        highlighted_line=center_line,
    )


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__
