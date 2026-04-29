"""Static safety review for LLM-generated patch drafts.

This module deliberately does **not** apply patches.  It only inspects a patch
draft and produces PASS / WARN / FAIL findings before any source file can be
modified.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable

from .code_context import CodeContext
from .fix_planner import FixProposal


class SafetyStatus(IntEnum):
    PASS = 0
    WARN = 1
    FAIL = 2

    def label(self) -> str:
        return self.name


@dataclass(frozen=True)
class PatchFileChange:
    old_path: str | None
    new_path: str | None

    @property
    def effective_path(self) -> str | None:
        if self.new_path and self.new_path != "/dev/null":
            return self.new_path
        if self.old_path and self.old_path != "/dev/null":
            return self.old_path
        return None

    @property
    def is_delete(self) -> bool:
        return self.new_path == "/dev/null"

    @property
    def is_create(self) -> bool:
        return self.old_path == "/dev/null"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchSafetyFinding:
    check: str
    status: str
    message: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchSafetyReview:
    status: str
    summary: str
    modified_files: list[str]
    allowed_files: list[str]
    findings: list[PatchSafetyFinding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "modified_files": self.modified_files,
            "allowed_files": self.allowed_files,
            "findings": [finding.to_dict() for finding in self.findings],
        }


FUNCTION_DEF_RE = re.compile(r"^\s*(?:async\s+def|def)\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*(?:->\s*[^:]+)?\s*:")
SPECIAL_FLOAT_RE = re.compile(
    r"\b(?:NaN|Infinity|-Infinity)\b|float\s*\(\s*['\"](?:nan|inf|-inf)['\"]\s*\)|math\.(?:nan|inf)\b",
    flags=re.IGNORECASE,
)
DANGEROUS_OPERATION_RE = re.compile(
    r"\b(?:eval|exec)\s*\(|"
    r"\bos\.system\s*\(|"
    r"\bsubprocess\.(?:run|Popen|call|check_call|check_output)\s*\(|"
    r"\bshutil\.rmtree\s*\(|"
    r"\bpickle\.loads\s*\(|"
    r"\bmarshal\.loads\s*\(|"
    r"\brm\s+-rf\b|"
    r"\bcurl\s+.*\|\s*(?:sh|bash)\b",
    flags=re.IGNORECASE,
)


def review_fix_proposal(
    proposal: FixProposal,
    context: CodeContext,
    *,
    allowed_files: Iterable[str] | None = None,
) -> PatchSafetyReview:
    """Review the patch draft inside a ``FixProposal``."""

    return review_patch_safety(
        proposal.patch_draft,
        context,
        allowed_files=allowed_files,
    )


def review_patch_safety(
    patch_text: str,
    context: CodeContext,
    *,
    allowed_files: Iterable[str] | None = None,
) -> PatchSafetyReview:
    """Run deterministic safety checks for a unified-diff patch draft."""

    allowed = sorted({_normalize_repo_path(path) for path in (allowed_files or [context.crash_file_relative])})
    changes = parse_patch_file_changes(patch_text)
    modified_files = sorted({change.effective_path for change in changes if change.effective_path})
    added_lines = _added_lines(patch_text)
    removed_lines = _removed_lines(patch_text)
    findings = [
        _check_patch_present(patch_text, changes),
        _check_allowed_files(modified_files, allowed),
        _check_cross_file(modified_files),
        _check_file_create_delete(changes),
        _check_special_floats(added_lines),
        _check_function_signatures(
            added_lines,
            removed_lines,
            crash_function_name=context.crash_function.name if context.crash_function else None,
        ),
        _check_dangerous_operations(added_lines),
    ]

    status = _overall_status(findings)
    return PatchSafetyReview(
        status=status.label(),
        summary=_summary_for_status(status),
        modified_files=modified_files,
        allowed_files=allowed,
        findings=findings,
    )


def parse_patch_file_changes(patch_text: str) -> list[PatchFileChange]:
    """Parse file changes from unified diff headers."""

    changes: list[PatchFileChange] = []
    pending_old: str | None = None

    for raw_line in patch_text.splitlines():
        if raw_line.startswith("--- "):
            pending_old = _extract_diff_path(raw_line[4:])
        elif raw_line.startswith("+++ "):
            new_path = _extract_diff_path(raw_line[4:])
            changes.append(PatchFileChange(old_path=pending_old, new_path=new_path))
            pending_old = None

    return changes


def format_patch_safety_review(review: PatchSafetyReview) -> str:
    """Render a human-readable patch safety review."""

    lines: list[str] = []
    lines.append("# Patch Safety Review")
    lines.append("")
    lines.append(f"- Status: **{review.status}**")
    lines.append(f"- Summary: {review.summary}")
    lines.append("")
    lines.append("## Modified Files")
    if review.modified_files:
        for file in review.modified_files:
            lines.append(f"- `{file}`")
    else:
        lines.append("- No modified files detected.")

    lines.append("")
    lines.append("## Allowed Files")
    for file in review.allowed_files:
        lines.append(f"- `{file}`")

    lines.append("")
    lines.append("## Findings")
    for finding in review.findings:
        lines.append(f"### {finding.status}: {finding.check}")
        lines.append(finding.message)
        if finding.evidence:
            lines.append("")
            lines.append("Evidence:")
            for item in finding.evidence:
                lines.append(f"- `{item}`")
        lines.append("")

    lines.append("> 注意：本步骤只审查 patch 草案，不会修改任何文件。")
    return "\n".join(lines).rstrip()


def _check_patch_present(patch_text: str, changes: list[PatchFileChange]) -> PatchSafetyFinding:
    if not patch_text.strip():
        return PatchSafetyFinding(
            check="patch_present",
            status="FAIL",
            message="Patch draft is empty.",
        )
    if not changes:
        return PatchSafetyFinding(
            check="patch_present",
            status="FAIL",
            message="Patch draft does not look like a unified diff with --- / +++ file headers.",
        )
    return PatchSafetyFinding(
        check="patch_present",
        status="PASS",
        message="Patch draft contains unified diff file headers.",
    )


def _check_allowed_files(modified_files: list[str], allowed_files: list[str]) -> PatchSafetyFinding:
    if not modified_files:
        return PatchSafetyFinding(
            check="allowed_files",
            status="FAIL",
            message="No modified files were detected, so allowed-file policy cannot be verified.",
        )

    disallowed = [file for file in modified_files if _normalize_repo_path(file) not in allowed_files]
    if disallowed:
        return PatchSafetyFinding(
            check="allowed_files",
            status="FAIL",
            message="Patch modifies files outside the allowed set.",
            evidence=disallowed,
        )

    return PatchSafetyFinding(
        check="allowed_files",
        status="PASS",
        message="All modified files are within the allowed set.",
    )


def _check_cross_file(modified_files: list[str]) -> PatchSafetyFinding:
    if len(modified_files) <= 1:
        return PatchSafetyFinding(
            check="cross_file_change",
            status="PASS",
            message="Patch modifies a single file.",
        )

    return PatchSafetyFinding(
        check="cross_file_change",
        status="WARN",
        message="Patch modifies multiple files. Cross-file fixes need stronger validation before applying.",
        evidence=modified_files,
    )


def _check_file_create_delete(changes: list[PatchFileChange]) -> PatchSafetyFinding:
    risky = [
        f"delete {change.old_path}" if change.is_delete else f"create {change.new_path}"
        for change in changes
        if change.is_delete or change.is_create
    ]
    if risky:
        return PatchSafetyFinding(
            check="file_create_delete",
            status="WARN",
            message="Patch creates or deletes files. This should be reviewed manually before applying.",
            evidence=risky,
        )
    return PatchSafetyFinding(
        check="file_create_delete",
        status="PASS",
        message="Patch does not create or delete files.",
    )


def _check_special_floats(added_lines: list[str]) -> PatchSafetyFinding:
    matches = [line for line in added_lines if SPECIAL_FLOAT_RE.search(line)]
    if matches:
        return PatchSafetyFinding(
            check="nan_or_infinity",
            status="FAIL",
            message=(
                "Patch introduces NaN/Infinity semantics. For Web API responses, special floating values are "
                "not safe JSON contract defaults unless explicitly approved."
            ),
            evidence=matches,
        )
    return PatchSafetyFinding(
        check="nan_or_infinity",
        status="PASS",
        message="Patch does not introduce NaN/Infinity special floating values.",
    )


def _check_function_signatures(
    added_lines: list[str],
    removed_lines: list[str],
    *,
    crash_function_name: str | None,
) -> PatchSafetyFinding:
    added_defs = _function_defs_from_lines(added_lines)
    removed_defs = _function_defs_from_lines(removed_lines)

    evidence: list[str] = []
    if crash_function_name:
        added_crash = [definition for definition in added_defs if definition[0] == crash_function_name]
        removed_crash = [definition for definition in removed_defs if definition[0] == crash_function_name]
        if added_crash or removed_crash:
            added_signatures = {_normalize_signature(signature) for _, signature in added_crash}
            removed_signatures = {_normalize_signature(signature) for _, signature in removed_crash}
            if added_signatures != removed_signatures:
                evidence.extend([signature for _, signature in removed_crash + added_crash])
                return PatchSafetyFinding(
                    check="function_signature",
                    status="FAIL",
                    message=f"Patch appears to change the crashed function signature: {crash_function_name}.",
                    evidence=evidence,
                )

    other_defs = [
        signature
        for name, signature in added_defs + removed_defs
        if crash_function_name is None or name != crash_function_name
    ]
    if other_defs:
        return PatchSafetyFinding(
            check="function_signature",
            status="WARN",
            message="Patch touches function definition lines. Verify signatures and decorators before applying.",
            evidence=other_defs,
        )

    return PatchSafetyFinding(
        check="function_signature",
        status="PASS",
        message="Patch does not modify function definition lines.",
    )


def _check_dangerous_operations(added_lines: list[str]) -> PatchSafetyFinding:
    matches = [line for line in added_lines if DANGEROUS_OPERATION_RE.search(line)]
    if matches:
        return PatchSafetyFinding(
            check="dangerous_operations",
            status="FAIL",
            message="Patch introduces potentially dangerous runtime operations.",
            evidence=matches,
        )
    return PatchSafetyFinding(
        check="dangerous_operations",
        status="PASS",
        message="Patch does not introduce known dangerous operations.",
    )


def _function_defs_from_lines(lines: list[str]) -> list[tuple[str, str]]:
    definitions: list[tuple[str, str]] = []
    for line in lines:
        match = FUNCTION_DEF_RE.match(line)
        if match:
            definitions.append((match.group(1), line.strip()))
    return definitions


def _added_lines(patch_text: str) -> list[str]:
    return [
        line[1:]
        for line in patch_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def _removed_lines(patch_text: str) -> list[str]:
    return [
        line[1:]
        for line in patch_text.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]


def _extract_diff_path(raw_path: str) -> str | None:
    token = raw_path.strip().split("\t", 1)[0].split(" ", 1)[0].strip()
    if not token:
        return None
    if token == "/dev/null":
        return token
    return _normalize_repo_path(token)


def _normalize_repo_path(path: str | Path | None) -> str:
    if path is None:
        return ""
    normalized = str(path).strip().replace("\\", "/")
    if normalized.startswith("a/") or normalized.startswith("b/"):
        normalized = normalized[2:]
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _normalize_signature(signature: str) -> str:
    return re.sub(r"\s+", "", signature)


def _overall_status(findings: list[PatchSafetyFinding]) -> SafetyStatus:
    status = SafetyStatus.PASS
    for finding in findings:
        status = max(status, SafetyStatus[finding.status])
    return status


def _summary_for_status(status: SafetyStatus) -> str:
    if status is SafetyStatus.PASS:
        return "Patch draft passed all configured static safety checks."
    if status is SafetyStatus.WARN:
        return "Patch draft has warnings and should be reviewed before applying."
    return "Patch draft failed one or more safety checks and should not be applied automatically."


def review_to_json(review: PatchSafetyReview) -> str:
    return json.dumps(review.to_dict(), ensure_ascii=False, indent=2)

