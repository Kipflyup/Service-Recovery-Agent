"""Dry-run Git delivery planning and safety checks.

Phase M.0 deliberately performs no Git writes.  It does not create worktrees,
branches, commits, pushes, or PRs.  It only derives a deterministic delivery
plan from an already-final recovery result and decision.

The plan is intentionally conservative:

- only ``CREATE_PR_READY`` repairs can become READY;
- stage files come from the verified patch preview / safety review, not from
  the LLM directly;
- sensitive paths and obvious secrets block delivery;
- worktree delivery is represented as a planned path, but not created here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .decision import DecisionAction, DecisionResult


READY = "READY"
BLOCKED = "BLOCKED"

DEFAULT_BASE_REF = "main"
DEFAULT_BRANCH_PREFIX = "agent/fix"
DEFAULT_WORKTREE_ROOT = Path("/tmp/service-recovery-agent-worktrees")

DENIED_EXACT_PATHS = {
    ".env",
    ".trae/mcp.json",
    "test_feishu_all.py",
    "test_feishu_requests.py",
    "飞书ai自动修复课题要求.md",
    "飞书自动化修复系统.png",
    "Plan.md",
}
DENIED_PREFIXES = (
    ".env.",
    "logs/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".git/",
    ".trae/",
)
DENIED_SUFFIXES = (
    ".pyc",
    ".log",
    ".orig",
    ".rej",
)
SECRET_RE = re.compile(
    r"\b(?:"
    r"DOUBAO_API_KEY|ARK_API_KEY|VOLCENGINE_API_KEY|"
    r"FEISHU_APP_ID|FEISHU_APP_SECRET|FEISHU_NOTICE_RECEIVER|"
    r"OPENAI_API_KEY|token|secret|password|private_key"
    r")\b|BEGIN\s+PRIVATE\s+KEY",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class GitDeliveryPlan:
    """A dry-run plan for future Git / PR delivery."""

    status: str
    reason: str
    branch: str
    base_ref: str
    worktree_path: str | None
    stage_files: list[str]
    blocked_files: list[str] = field(default_factory=list)
    denied_reasons: list[str] = field(default_factory=list)
    commit_message: str = ""
    pr_title: str = ""
    would_push: bool = False
    would_create_pr: bool = False
    use_worktree: bool = True
    can_create_pr: bool = False

    @property
    def ready(self) -> bool:
        return self.status == READY

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ready"] = self.ready
        return data


def build_git_delivery_plan(
    result: Any,
    decision: DecisionResult,
    *,
    project_root: str | Path = ".",
    base_ref: str = DEFAULT_BASE_REF,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
    use_worktree: bool = True,
    worktree_root: str | Path = DEFAULT_WORKTREE_ROOT,
    push: bool = False,
    create_pr: bool = False,
) -> GitDeliveryPlan:
    """Build a deterministic Git delivery dry-run plan.

    This function is read-only.  It does not inspect remotes, create a branch,
    stage files, commit, push, or call GitHub.  The resulting plan can be shown
    to users or embedded in JSON/Feishu evidence before later GitOps phases.
    """

    normalized_base = str(base_ref or DEFAULT_BASE_REF).strip() or DEFAULT_BASE_REF
    branch = _branch_name(result, branch_prefix=branch_prefix)
    stage_files = _stage_files_from_result(result)
    blocked_files = _blocked_files(stage_files)
    denied_reasons: list[str] = []

    action = _decision_action(decision)
    result_status = str(getattr(result, "status", "") or "UNKNOWN")
    result_can_create_pr = bool(getattr(result, "can_create_pr", False))
    rolled_back = bool(getattr(result, "rolled_back", False))
    preview = getattr(result, "preview", None)
    safety_status = _safety_status(preview)
    patch_text = _patch_text(result)

    if action != DecisionAction.CREATE_PR_READY.value:
        denied_reasons.append(f"Decision action is {action or 'missing'}, not CREATE_PR_READY.")
    if result_status != "PASS":
        denied_reasons.append(f"Recovery status is {result_status}, not PASS.")
    if rolled_back:
        denied_reasons.append("Recovery run rolled back; rolled-back repairs cannot be delivered.")
    if not result_can_create_pr:
        denied_reasons.append("RecoveryRunResult.can_create_pr is False.")
    if safety_status != "PASS":
        denied_reasons.append(f"Patch safety status is {safety_status or 'missing'}, not PASS.")
    if not stage_files:
        denied_reasons.append("No verified stage files were found from the patch safety review.")

    allowed_mismatch = _allowed_mismatch(preview, stage_files)
    if allowed_mismatch:
        denied_reasons.append(
            "Stage files include paths outside the patch safety allowed set: "
            + ", ".join(allowed_mismatch)
        )
    if blocked_files:
        denied_reasons.append("Stage files include denied/sensitive paths: " + ", ".join(blocked_files))

    secret_hits = scan_patch_for_secrets(patch_text)
    if secret_hits:
        denied_reasons.append("Patch text contains possible secret material: " + ", ".join(secret_hits))

    status = BLOCKED if denied_reasons else READY
    reason = "Git delivery plan is ready for a future local-commit phase." if status == READY else "Git delivery plan is blocked by safety checks."
    planned_worktree = _worktree_path(worktree_root, branch) if use_worktree else None
    commit_message = _commit_message(result)
    pr_title = _pr_title(result)

    return GitDeliveryPlan(
        status=status,
        reason=reason,
        branch=branch,
        base_ref=normalized_base,
        worktree_path=str(planned_worktree) if planned_worktree else None,
        stage_files=stage_files,
        blocked_files=blocked_files,
        denied_reasons=denied_reasons,
        commit_message=commit_message,
        pr_title=pr_title,
        would_push=bool(push),
        would_create_pr=bool(create_pr),
        use_worktree=use_worktree,
        can_create_pr=status == READY,
    )


def format_git_delivery_plan(plan: GitDeliveryPlan) -> str:
    """Render a human-readable dry-run Git delivery plan."""

    lines = ["# Git Delivery Plan"]
    lines.append(f"- Status: **{plan.status}**")
    lines.append(f"- Reason: {plan.reason}")
    lines.append(f"- Branch: `{plan.branch}`")
    lines.append(f"- Base ref: `{plan.base_ref}`")
    lines.append(f"- Use worktree: {plan.use_worktree}")
    if plan.worktree_path:
        lines.append(f"- Planned worktree: `{plan.worktree_path}`")
    lines.append(f"- Would push: {plan.would_push}")
    lines.append(f"- Would create PR: {plan.would_create_pr}")
    lines.append(f"- Commit message: {plan.commit_message}")
    lines.append(f"- PR title: {plan.pr_title}")
    lines.append("")
    lines.append("## Stage files")
    if plan.stage_files:
        for file in plan.stage_files:
            lines.append(f"- `{file}`")
    else:
        lines.append("- No files would be staged.")
    if plan.blocked_files:
        lines.append("")
        lines.append("## Blocked files")
        for file in plan.blocked_files:
            lines.append(f"- `{file}`")
    if plan.denied_reasons:
        lines.append("")
        lines.append("## Denied reasons")
        for reason in plan.denied_reasons:
            lines.append(f"- {reason}")
    lines.append("")
    lines.append("> Dry-run only: no git add, commit, push, worktree creation, or PR was executed.")
    return "\n".join(lines).rstrip()


def scan_patch_for_secrets(patch_text: str) -> list[str]:
    """Return unique secret-like tokens found in added/context patch text."""

    hits: list[str] = []
    for line in str(patch_text or "").splitlines():
        for match in SECRET_RE.finditer(line):
            token = match.group(0)
            normalized = " ".join(token.split())
            if normalized not in hits:
                hits.append(normalized)
    return hits


def is_denied_path(path: str) -> bool:
    """Return whether a repository path must never be staged by GitOps."""

    normalized = _normalize_repo_path(path)
    if normalized in DENIED_EXACT_PATHS:
        return True
    if any(normalized.startswith(prefix) for prefix in DENIED_PREFIXES):
        return True
    return any(normalized.endswith(suffix) for suffix in DENIED_SUFFIXES)


def _stage_files_from_result(result: Any) -> list[str]:
    preview = getattr(result, "preview", None)
    safety = getattr(preview, "safety_review", None)
    modified = getattr(safety, "modified_files", []) if safety is not None else []
    return sorted({_normalize_repo_path(file) for file in modified if str(file or "").strip()})


def _blocked_files(files: list[str]) -> list[str]:
    return [file for file in files if is_denied_path(file)]


def _allowed_mismatch(preview: Any, stage_files: list[str]) -> list[str]:
    safety = getattr(preview, "safety_review", None)
    allowed = getattr(safety, "allowed_files", []) if safety is not None else []
    normalized_allowed = {_normalize_repo_path(file) for file in allowed}
    if not normalized_allowed:
        return []
    return [file for file in stage_files if file not in normalized_allowed]


def _patch_text(result: Any) -> str:
    preview = getattr(result, "preview", None)
    patch_text = getattr(preview, "patch_text", "") if preview is not None else ""
    if patch_text:
        return str(patch_text)
    proposal = getattr(result, "proposal", None)
    return str(getattr(proposal, "patch_draft", "") if proposal is not None else "")


def _safety_status(preview: Any) -> str:
    safety = getattr(preview, "safety_review", None)
    status = getattr(safety, "status", "") if safety is not None else ""
    return str(status or "").upper()


def _decision_action(decision: DecisionResult | None) -> str:
    if decision is None:
        return ""
    action = decision.action
    if isinstance(action, DecisionAction):
        return action.value
    return str(action or "")


def _branch_name(result: Any, *, branch_prefix: str) -> str:
    prefix = (branch_prefix or DEFAULT_BRANCH_PREFIX).strip().strip("/") or DEFAULT_BRANCH_PREFIX
    parts: list[str] = []
    traceback_event = getattr(result, "traceback_event", None)
    exception_type = str(getattr(traceback_event, "exception_type", "") or "")
    if exception_type:
        parts.append(exception_type)
    context = getattr(result, "context", None)
    crash_function = getattr(context, "crash_function", None)
    function_name = str(getattr(crash_function, "name", "") or "")
    if function_name:
        parts.append(function_name)
    if not parts:
        proposal = getattr(result, "proposal", None)
        root_cause = str(getattr(proposal, "root_cause", "") or "") if proposal is not None else ""
        if root_cause:
            parts.append(root_cause[:48])
    slug = _slug("-".join(parts) or "recovery")
    return f"{prefix}/{slug}"


def _commit_message(result: Any) -> str:
    traceback_event = getattr(result, "traceback_event", None)
    exception_type = str(getattr(traceback_event, "exception_type", "") or "")
    context = getattr(result, "context", None)
    crash_file = str(getattr(context, "crash_file_relative", "") or "")
    if exception_type and crash_file:
        return f"Fix {exception_type} in {crash_file}"
    if exception_type:
        return f"Fix {exception_type}"
    return "Apply service recovery repair"


def _pr_title(result: Any) -> str:
    return _commit_message(result)


def _worktree_path(root: str | Path, branch: str) -> Path:
    return Path(root) / _slug(branch.replace("/", "-"))


def _normalize_repo_path(path: Any) -> str:
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    text = re.sub(r"-+", "-", text).strip("-._")
    return text or "recovery"
