"""Patch preview utilities.

This module wraps the fragile shell dance of:

FixProposal JSON -> patch_draft -> temporary .patch file -> patch --dry-run

It intentionally defaults to preview/dry-run only.  Applying patches is a later
step and should be gated by safety review and tests.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .code_context import CodeContext
from .fix_planner import FixProposal
from .patch_safety import (
    PatchSafetyReview,
    format_patch_safety_review,
    review_fix_proposal,
    review_patch_safety,
)


class PatchPreviewError(RuntimeError):
    """Raised when patch preview cannot be performed."""


@dataclass(frozen=True)
class PatchDryRunResult:
    command: list[str]
    return_code: int
    stdout: str
    stderr: str
    can_apply: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchApplyResult:
    command: list[str]
    return_code: int
    stdout: str
    stderr: str
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchPreviewResult:
    safety_review: PatchSafetyReview
    dry_run: PatchDryRunResult | None
    patch_text: str
    patch_file: str | None = None

    @property
    def status(self) -> str:
        if self.safety_review.status == "FAIL":
            return "FAIL"
        if self.dry_run is None:
            return self.safety_review.status
        if not self.dry_run.can_apply:
            return "FAIL"
        return self.safety_review.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "safety_review": self.safety_review.to_dict(),
            "dry_run": self.dry_run.to_dict() if self.dry_run else None,
            "patch_file": self.patch_file,
            "patch_text": self.patch_text,
        }


@dataclass(frozen=True)
class FileSnapshot:
    files: dict[str, str | None]

    def to_dict(self) -> dict[str, Any]:
        return {"files": {path: content is not None for path, content in self.files.items()}}


def load_fix_proposal(path: str | Path) -> FixProposal:
    """Load a FixProposal JSON file produced by ``propose_fix.py --json``."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return FixProposal(
        root_cause=str(data.get("root_cause", "")),
        fix_strategy=str(data.get("fix_strategy", "")),
        change_type=str(data.get("change_type", "")),
        risk_level=str(data.get("risk_level", "")),
        confidence=str(data.get("confidence", "")),
        contract_constraints=[str(item) for item in data.get("contract_constraints", [])],
        patch_draft=str(data.get("patch_draft", "")),
        tests_to_run=[str(item) for item in data.get("tests_to_run", [])],
        manual_review_notes=str(data.get("manual_review_notes", "")),
        raw_response=str(data.get("raw_response", "")),
        provider=str(data.get("provider", "")),
        model=str(data.get("model", "")),
    )


def write_patch_file(patch_text: str, path: str | Path) -> Path:
    """Write patch text to a file with a final newline."""

    patch_path = Path(path)
    patch_path.write_text(patch_text.rstrip() + "\n", encoding="utf-8")
    return patch_path


def dry_run_patch(
    patch_text: str,
    *,
    project_root: str | Path = ".",
    strip: int = 1,
    patch_binary: str = "patch",
) -> PatchDryRunResult:
    """Run ``patch --dry-run`` without modifying files."""

    root = Path(project_root).resolve()
    command = [patch_binary, f"-p{strip}", "--dry-run"]

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as patch_file:
        patch_file.write(patch_text.rstrip() + "\n")
        patch_file_path = Path(patch_file.name)

    try:
        completed = subprocess.run(
            [*command, "-i", str(patch_file_path)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        patch_file_path.unlink(missing_ok=True)

    return PatchDryRunResult(
        command=[*command, "-i", "<tempfile>"],
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        can_apply=completed.returncode == 0,
    )


def apply_patch_text(
    patch_text: str,
    *,
    project_root: str | Path = ".",
    strip: int = 1,
    patch_binary: str = "patch",
) -> PatchApplyResult:
    """Apply a patch for real.

    Callers are responsible for running safety review and dry-run before using
    this function.  Prefer ``scripts/apply_patch.py`` for the guarded workflow.
    """

    root = Path(project_root).resolve()
    command = [patch_binary, f"-p{strip}"]

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as patch_file:
        patch_file.write(patch_text.rstrip() + "\n")
        patch_file_path = Path(patch_file.name)

    try:
        completed = subprocess.run(
            [*command, "-i", str(patch_file_path)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        patch_file_path.unlink(missing_ok=True)

    return PatchApplyResult(
        command=[*command, "-i", "<tempfile>"],
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        applied=completed.returncode == 0,
    )


def snapshot_files(project_root: str | Path, files: Iterable[str]) -> FileSnapshot:
    """Snapshot file contents before applying a patch."""

    root = Path(project_root).resolve()
    snapshots: dict[str, str | None] = {}
    for file in files:
        relative = str(file).replace("\\", "/")
        path = root / relative
        snapshots[relative] = path.read_text(encoding="utf-8") if path.exists() else None
    return FileSnapshot(files=snapshots)


def restore_snapshot(project_root: str | Path, snapshot: FileSnapshot) -> None:
    """Restore files captured by ``snapshot_files``."""

    root = Path(project_root).resolve()
    for relative, content in snapshot.files.items():
        path = root / relative
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def preview_patch(
    patch_text: str,
    context: CodeContext,
    *,
    project_root: str | Path = ".",
    allowed_files: Iterable[str] | None = None,
    strip: int = 1,
    require_safety_pass: bool = True,
    patch_binary: str = "patch",
) -> PatchPreviewResult:
    """Safety-review and dry-run a patch draft without applying it."""

    review = review_patch_safety(patch_text, context, allowed_files=allowed_files)
    if require_safety_pass and review.status != "PASS":
        return PatchPreviewResult(
            safety_review=review,
            dry_run=None,
            patch_text=patch_text,
            patch_file=None,
        )

    dry_run = dry_run_patch(
        patch_text,
        project_root=project_root,
        strip=strip,
        patch_binary=patch_binary,
    )
    return PatchPreviewResult(
        safety_review=review,
        dry_run=dry_run,
        patch_text=patch_text,
        patch_file=None,
    )


def preview_fix_proposal(
    proposal: FixProposal,
    context: CodeContext,
    *,
    project_root: str | Path = ".",
    allowed_files: Iterable[str] | None = None,
    strip: int = 1,
    require_safety_pass: bool = True,
    patch_binary: str = "patch",
) -> PatchPreviewResult:
    """Safety-review and dry-run the patch_draft from a FixProposal."""

    review = review_fix_proposal(proposal, context, allowed_files=allowed_files)
    if require_safety_pass and review.status != "PASS":
        return PatchPreviewResult(
            safety_review=review,
            dry_run=None,
            patch_text=proposal.patch_draft,
            patch_file=None,
        )

    dry_run = dry_run_patch(
        proposal.patch_draft,
        project_root=project_root,
        strip=strip,
        patch_binary=patch_binary,
    )
    return PatchPreviewResult(
        safety_review=review,
        dry_run=dry_run,
        patch_text=proposal.patch_draft,
        patch_file=None,
    )


def format_patch_preview_result(result: PatchPreviewResult) -> str:
    """Render a human-readable patch preview report."""

    lines: list[str] = []
    lines.append("# Patch Preview")
    lines.append("")
    lines.append(f"- Status: **{result.status}**")
    lines.append("")
    lines.append(format_patch_safety_review(result.safety_review))
    lines.append("")
    lines.append("## Patch Dry Run")

    if result.dry_run is None:
        lines.append("- Skipped because safety review did not PASS.")
    else:
        lines.append(f"- Can apply: {result.dry_run.can_apply}")
        lines.append(f"- Return code: {result.dry_run.return_code}")
        lines.append(f"- Command: `{' '.join(result.dry_run.command)}`")
        if result.dry_run.stdout.strip():
            lines.append("")
            lines.append("stdout:")
            lines.append("```text")
            lines.append(result.dry_run.stdout.rstrip())
            lines.append("```")
        if result.dry_run.stderr.strip():
            lines.append("")
            lines.append("stderr:")
            lines.append("```text")
            lines.append(result.dry_run.stderr.rstrip())
            lines.append("```")

    lines.append("")
    lines.append("> 注意：这是 patch dry-run / preview，不会修改任何文件。")
    return "\n".join(lines).rstrip()
