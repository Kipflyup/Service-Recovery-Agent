"""Review artifact / PR description draft builders.

This module turns a ``RecoveryRunResult`` plus an optional ``DecisionResult``
into a deterministic Markdown artifact that can be copied into a future pull
request description or human review ticket.

It intentionally performs no Git or GitHub operations: no branch creation, no
staging, no commit, no push, and no PR creation.  It only formats the evidence
that the recovery pipeline already produced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .decision import DecisionAction, DecisionResult


TEXT_LIMIT = 600
COMMAND_OUTPUT_LIMIT = 240
MAX_ATTEMPTS_IN_MARKDOWN = 8
MAX_DECISION_REASONS = 8


@dataclass(frozen=True)
class RollbackPlan:
    """Rollback instructions for a future delivered repair."""

    strategy: str
    command: str | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewCriteria:
    """Success and failure criteria for human review."""

    success_criteria: list[str]
    failure_criteria: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewArtifact:
    """A future-PR-ready Markdown review artifact."""

    title: str
    summary: str
    markdown: str
    rollback_plan: RollbackPlan
    criteria: ReviewCriteria

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "markdown": self.markdown,
            "rollback_plan": self.rollback_plan.to_dict(),
            "criteria": self.criteria.to_dict(),
        }


def build_review_artifact(
    result: Any,
    *,
    decision: DecisionResult | None = None,
    title: str | None = None,
    commit_sha: str | None = None,
    rollback_command: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
) -> ReviewArtifact:
    """Build a Markdown artifact for a future PR or human review.

    Args:
        result: A ``RecoveryRunResult``-like object.
        decision: Optional ``DecisionResult`` from the decision engine.
        title: Optional Markdown title. Defaults to a Service Recovery title.
        commit_sha: Optional future commit sha, used only to format rollback
            text. This function never creates or inspects commits.
        rollback_command: Optional explicit rollback command. If omitted and
            ``commit_sha`` is provided, the command becomes ``git revert``.
        branch/pr_url: Optional future delivery metadata to display.
    """

    artifact_title = title or _default_title(result)
    rollback_plan = build_rollback_plan(
        result,
        commit_sha=commit_sha,
        rollback_command=rollback_command,
    )
    criteria = build_review_criteria(result)
    summary = _artifact_summary(result, decision)
    markdown = _format_markdown(
        result,
        decision=decision,
        title=artifact_title,
        summary=summary,
        rollback_plan=rollback_plan,
        criteria=criteria,
        branch=branch,
        pr_url=pr_url,
    )
    return ReviewArtifact(
        title=artifact_title,
        summary=summary,
        markdown=markdown,
        rollback_plan=rollback_plan,
        criteria=criteria,
    )


def build_pr_description_markdown(
    result: Any,
    *,
    decision: DecisionResult | None = None,
    title: str | None = None,
    commit_sha: str | None = None,
    rollback_command: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
) -> str:
    """Convenience wrapper returning only Markdown."""

    return build_review_artifact(
        result,
        decision=decision,
        title=title,
        commit_sha=commit_sha,
        rollback_command=rollback_command,
        branch=branch,
        pr_url=pr_url,
    ).markdown


def build_rollback_plan(
    result: Any,
    *,
    commit_sha: str | None = None,
    rollback_command: str | None = None,
) -> RollbackPlan:
    """Build a rollback plan without executing Git."""

    notes: list[str] = [
        "This artifact builder does not execute Git commands or create a PR.",
    ]
    if bool(getattr(result, "rolled_back", False)):
        notes.append("The current recovery run already rolled back the attempted patch.")
    else:
        notes.append("If the patch is not committed yet, discard or restore the modified files from the local snapshot/VCS.")

    if rollback_command:
        return RollbackPlan(
            strategy="Use the explicitly provided rollback command.",
            command=rollback_command,
            notes=notes,
        )
    if commit_sha:
        return RollbackPlan(
            strategy="Revert the future repair commit if it has already been merged or applied.",
            command=f"git revert {commit_sha}",
            notes=notes,
        )

    notes.append("After a real commit exists, replace <commit_sha> with the repair commit and run: git revert <commit_sha>.")
    return RollbackPlan(
        strategy="No commit has been created by this builder; use VCS restore before commit, or git revert after commit.",
        command=None,
        notes=notes,
    )


def build_review_criteria(result: Any) -> ReviewCriteria:
    """Derive success/failure criteria from available recovery evidence."""

    success: list[str] = []
    failure: list[str] = []

    diagnosis = getattr(result, "diagnosis", None)
    category = _diagnosis_category(diagnosis)
    suspected_variables = set(getattr(diagnosis, "suspected_variables", []) or [])

    if category == "arithmetic_boundary" or "denominator" in suspected_variables:
        success.extend(
            [
                "GET /divide?x=4 returns 200 with result 25.0.",
                "GET /divide?x=0 returns a controlled 400/422 response, not a 500.",
                "GET /bug returns a controlled 400/422 response, not a 500.",
            ]
        )
        failure.extend(
            [
                "GET /divide?x=4 regresses or returns 500.",
                "Zero denominator input leaks ZeroDivisionError as a 500 response.",
                "The alternate /bug caller still returns 500.",
            ]
        )

    success.append("All configured ordinary validation commands pass.")
    failure.append("Any configured ordinary validation command fails.")

    if getattr(result, "contract_result", None) is not None:
        success.append("Contract validation reports no regression for commands that passed before the patch.")
        failure.append("Any previously passing contract validation command regresses after the patch.")

    if getattr(result, "counterfactual_result", None) is not None:
        success.append("Counterfactual validation proves before_patch FAIL and after_patch PASS.")
        failure.append("Counterfactual validation cannot prove before_patch FAIL and after_patch PASS.")

    if getattr(result, "adversarial_result", None) is not None:
        success.append("All generated adversarial probes pass.")
        failure.append("Any generated adversarial probe fails or returns a forbidden 500 response.")

    success.append("Patch safety review and patch dry-run remain PASS before delivery.")
    failure.append("Patch safety review fails, dry-run fails, or the patch requires broad/manual contract changes.")

    return ReviewCriteria(
        success_criteria=_dedupe(success),
        failure_criteria=_dedupe(failure),
    )


def _format_markdown(
    result: Any,
    *,
    decision: DecisionResult | None,
    title: str,
    summary: str,
    rollback_plan: RollbackPlan,
    criteria: ReviewCriteria,
    branch: str | None,
    pr_url: str | None,
) -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append("## Summary")
    lines.append(summary)
    lines.append("")

    _append_bug_summary(lines, result)
    _append_root_cause(lines, result)
    _append_fault_diagnosis(lines, result)
    _append_fix_strategy(lines, result)
    _append_safety_review(lines, result)
    _append_validation_evidence(lines, result)
    _append_repair_attempts(lines, result)
    _append_decision(lines, decision)
    _append_delivery_metadata(lines, branch=branch, pr_url=pr_url)
    _append_criteria(lines, criteria)
    _append_rollback_plan(lines, rollback_plan)
    _append_notes(lines)

    return "\n".join(lines).rstrip() + "\n"


def _append_bug_summary(lines: list[str], result: Any) -> None:
    lines.append("## Bug Summary")
    event = getattr(result, "traceback_event", None)
    diagnosis = getattr(result, "diagnosis", None)
    classification = getattr(diagnosis, "classification", None) if diagnosis is not None else None
    exception_type = getattr(event, "exception_type", "") or getattr(classification, "exception_type", "") or "unknown"
    exception_message = getattr(event, "exception_message", "") or ""
    crash_frame = getattr(event, "crash_frame", None) if event is not None else None
    candidate = getattr(diagnosis, "primary_candidate", None) if diagnosis is not None else None

    lines.append(f"- Exception: `{exception_type}`")
    if exception_message:
        lines.append(f"- Message: `{_truncate(exception_message, TEXT_LIMIT)}`")
    if crash_frame is not None:
        lines.append(
            f"- Crash site: `{getattr(crash_frame, 'file', '')}:{getattr(crash_frame, 'line_number', '')}` "
            f"in `{getattr(crash_frame, 'function', '')}`"
        )
    elif candidate is not None:
        lines.append(
            f"- Crash site: `{getattr(candidate, 'file', '')}:{getattr(candidate, 'line_number', '')}` "
            f"in `{getattr(candidate, 'function', '') or 'unknown'}`"
        )

    triggers = _trigger_summary(result)
    if triggers:
        lines.append("- Known reproduction / trigger hints:")
        for trigger in triggers:
            lines.append(f"  - `{trigger}`")
    lines.append("")


def _append_root_cause(lines: list[str], result: Any) -> None:
    lines.append("## Root Cause")
    proposal = getattr(result, "proposal", None)
    root_cause = getattr(proposal, "root_cause", "") if proposal is not None else ""
    if root_cause:
        lines.append(_truncate(root_cause, TEXT_LIMIT))
    else:
        diagnosis = getattr(result, "diagnosis", None)
        classification = getattr(diagnosis, "classification", None) if diagnosis is not None else None
        hint = getattr(classification, "strategy_hint", "") if classification is not None else ""
        lines.append(_truncate(hint, TEXT_LIMIT) if hint else "No root-cause proposal was recorded.")
    lines.append("")


def _append_fault_diagnosis(lines: list[str], result: Any) -> None:
    lines.append("## Fault Diagnosis")
    diagnosis = getattr(result, "diagnosis", None)
    if diagnosis is None:
        lines.append("- No deterministic fault diagnosis was recorded.")
        lines.append("")
        return

    classification = diagnosis.classification
    lines.append(f"- Category: `{classification.category}`")
    lines.append(f"- Diagnosis confidence: `{classification.confidence}`")
    if diagnosis.suspected_variables:
        lines.append(f"- Suspected variables: `{', '.join(diagnosis.suspected_variables)}`")
    if diagnosis.trigger_conditions:
        lines.append("- Trigger conditions:")
        for condition in diagnosis.trigger_conditions:
            lines.append(f"  - `{condition}`")
    if diagnosis.variable_flows:
        lines.append("- Variable flows:")
        for flow in diagnosis.variable_flows:
            lines.append(f"  - `{flow.variable}` ← {_truncate(flow.source, TEXT_LIMIT)}")
    if diagnosis.repair_constraints:
        lines.append("- Repair constraints:")
        for constraint in diagnosis.repair_constraints:
            lines.append(f"  - **{constraint.kind}**: {_truncate(constraint.description, TEXT_LIMIT)}")
    lines.append("")


def _append_fix_strategy(lines: list[str], result: Any) -> None:
    lines.append("## Fix Summary")
    proposal = getattr(result, "proposal", None)
    if proposal is None:
        lines.append("- No FixProposal was recorded.")
        lines.append("")
        return

    lines.append(f"- Strategy: {_truncate(getattr(proposal, 'fix_strategy', '') or '-', TEXT_LIMIT)}")
    lines.append(f"- Change type: `{getattr(proposal, 'change_type', '') or 'unknown'}`")
    lines.append(f"- Risk level: `{getattr(proposal, 'risk_level', '') or 'unknown'}`")
    lines.append(f"- Proposal confidence: `{getattr(proposal, 'confidence', '') or 'unknown'}`")
    constraints = getattr(proposal, "contract_constraints", []) or []
    if constraints:
        lines.append("- Contract constraints from proposal:")
        for constraint in constraints:
            lines.append(f"  - {_truncate(str(constraint), TEXT_LIMIT)}")
    notes = getattr(proposal, "manual_review_notes", "") or ""
    if notes:
        lines.append(f"- Manual review notes: {_truncate(notes, TEXT_LIMIT)}")
    lines.append("")


def _append_safety_review(lines: list[str], result: Any) -> None:
    lines.append("## Safety Review")
    preview = getattr(result, "preview", None)
    review = getattr(preview, "safety_review", None) if preview is not None else None
    if review is None:
        lines.append("- Patch safety review: not recorded.")
        lines.append("")
        return

    lines.append(f"- Patch safety: `{review.status}`")
    lines.append(f"- Summary: {_truncate(review.summary, TEXT_LIMIT)}")
    lines.append("- Modified files:")
    if review.modified_files:
        for file in review.modified_files:
            lines.append(f"  - `{file}`")
    else:
        lines.append("  - No modified files recorded.")
    dry_run = getattr(preview, "dry_run", None)
    if dry_run is not None:
        lines.append(f"- Patch dry-run can apply: `{dry_run.can_apply}` (rc={dry_run.return_code})")
    lines.append("")


def _append_validation_evidence(lines: list[str], result: Any) -> None:
    lines.append("## Validation Evidence")
    _append_validation_result(lines, "Ordinary validation", getattr(result, "validation_result", None))

    contract = getattr(result, "contract_result", None)
    if contract is None:
        lines.append("- Contract validation: not run.")
    else:
        lines.append(f"- Contract validation: `{contract.status}` — {_truncate(contract.summary, TEXT_LIMIT)}")
        regressions = [baseline for baseline in contract.command_baselines if baseline.regression]
        if regressions:
            lines.append("  - Regressed commands:")
            for baseline in regressions:
                lines.append(f"    - `{_command_text(baseline.command)}`")

    counterfactual = getattr(result, "counterfactual_result", None)
    if counterfactual is None:
        lines.append("- Counterfactual validation: not run.")
    else:
        lines.append(f"- Counterfactual validation: `{counterfactual.status}` — {_truncate(counterfactual.summary, TEXT_LIMIT)}")

    adversarial = getattr(result, "adversarial_result", None)
    if adversarial is None:
        lines.append("- Adversarial validation: not run.")
    else:
        lines.append(f"- Adversarial validation: `{adversarial.status}` — {_truncate(adversarial.summary, TEXT_LIMIT)}")
        failed_probes = [probe for probe in adversarial.probe_results if not probe.passed]
        if failed_probes:
            lines.append("  - Failed probes:")
            for probe in failed_probes:
                lines.append(f"    - `{probe.case.name}` `{probe.case.method.upper()} {probe.case.path}`")
    lines.append("")


def _append_validation_result(lines: list[str], label: str, validation: Any) -> None:
    if validation is None:
        lines.append(f"- {label}: not run.")
        return
    lines.append(f"- {label}: `{validation.status}`")
    for command in getattr(validation, "command_results", []) or []:
        lines.append(
            f"  - `{_command_text(command.command)}`: "
            f"{'PASS' if command.passed else 'FAIL'} (rc={command.return_code}, {command.duration_seconds:.2f}s)"
        )
        if not command.passed:
            excerpt = _truncate(command.stderr or command.stdout, COMMAND_OUTPUT_LIMIT)
            if excerpt:
                lines.append(f"    - Failure excerpt: `{excerpt}`")


def _append_repair_attempts(lines: list[str], result: Any) -> None:
    lines.append("## Repair Attempts")
    attempts = getattr(result, "repair_attempts", []) or []
    if not attempts:
        attempt_number = int(getattr(result, "attempt_number", 0) or 0)
        if attempt_number:
            lines.append(f"- Attempt {attempt_number}: `{getattr(result, 'status', 'UNKNOWN')}` — {_truncate(getattr(result, 'reason', ''), TEXT_LIMIT)}")
        else:
            lines.append("- No repair attempts were recorded.")
        lines.append("")
        return

    for attempt in attempts[:MAX_ATTEMPTS_IN_MARKDOWN]:
        lines.append(
            f"- Attempt {attempt.attempt_number}: `{attempt.status}`, "
            f"rolled_back={attempt.rolled_back} — {_truncate(attempt.reason, TEXT_LIMIT)}"
        )
        feedback = attempt.failure_feedback
        if feedback is not None:
            failed = ", ".join(feedback.failed_probes) or "unknown"
            lines.append(f"  - Failure feedback: `{feedback.failed_stage}` / `{_truncate(failed, TEXT_LIMIT)}`")
    if len(attempts) > MAX_ATTEMPTS_IN_MARKDOWN:
        lines.append(f"- ... {len(attempts) - MAX_ATTEMPTS_IN_MARKDOWN} additional attempts omitted.")
    lines.append("")


def _append_decision(lines: list[str], decision: DecisionResult | None) -> None:
    lines.append("## Decision")
    if decision is None:
        lines.append("- DecisionResult was not provided.")
        lines.append("")
        return

    lines.append(f"- Action: `{_action_value(decision.action)}`")
    lines.append(f"- Summary: {_truncate(decision.summary, TEXT_LIMIT)}")
    lines.append(f"- Confidence score: `{decision.confidence_score:.2f}`")
    lines.append(f"- Risk level: `{decision.risk_level}`")
    lines.append(f"- Can auto deliver: `{decision.can_auto_deliver}`")
    if decision.reasons:
        lines.append("- Reasons:")
        for reason in decision.reasons[:MAX_DECISION_REASONS]:
            lines.append(f"  - **{reason.severity}** `{reason.code}`: {_truncate(reason.message, TEXT_LIMIT)}")
        if len(decision.reasons) > MAX_DECISION_REASONS:
            lines.append(f"  - ... {len(decision.reasons) - MAX_DECISION_REASONS} additional reasons omitted.")
    lines.append("")


def _append_delivery_metadata(lines: list[str], *, branch: str | None, pr_url: str | None) -> None:
    if not branch and not pr_url:
        return
    lines.append("## Delivery Metadata")
    if branch:
        lines.append(f"- Future branch: `{branch}`")
    if pr_url:
        lines.append(f"- Future PR URL: {pr_url}")
    lines.append("")


def _append_criteria(lines: list[str], criteria: ReviewCriteria) -> None:
    lines.append("## Success Criteria")
    for item in criteria.success_criteria:
        lines.append(f"- [ ] {item}")
    lines.append("")
    lines.append("## Failure Criteria")
    for item in criteria.failure_criteria:
        lines.append(f"- [ ] {item}")
    lines.append("")


def _append_rollback_plan(lines: list[str], rollback_plan: RollbackPlan) -> None:
    lines.append("## Rollback Plan")
    lines.append(f"- Strategy: {_truncate(rollback_plan.strategy, TEXT_LIMIT)}")
    if rollback_plan.command:
        lines.append(f"- Command: `{rollback_plan.command}`")
    else:
        lines.append("- Command: not available until a real commit exists. Use `git revert <commit_sha>` after delivery.")
    if rollback_plan.notes:
        lines.append("- Notes:")
        for note in rollback_plan.notes:
            lines.append(f"  - {_truncate(note, TEXT_LIMIT)}")
    lines.append("")


def _append_notes(lines: list[str]) -> None:
    lines.append("## Artifact Safety Notes")
    lines.append("- This Markdown was generated as a dry-run review artifact.")
    lines.append("- No Git branch, commit, push, GitHub PR, or Feishu message was created by this builder.")
    lines.append("- Full logs, raw LLM prompts, secret environment files, and large command outputs are intentionally omitted or truncated.")


def _default_title(result: Any) -> str:
    proposal = getattr(result, "proposal", None)
    change_type = getattr(proposal, "change_type", "") if proposal is not None else ""
    suffix = f" ({change_type})" if change_type else ""
    return f"Service Recovery Agent 修复说明{suffix}"


def _artifact_summary(result: Any, decision: DecisionResult | None) -> str:
    parts = [
        f"Recovery status `{getattr(result, 'status', 'UNKNOWN')}`",
        f"rolled_back=`{bool(getattr(result, 'rolled_back', False))}`",
        f"can_create_pr=`{bool(getattr(result, 'can_create_pr', False))}`",
    ]
    if decision is not None:
        parts.append(f"decision=`{_action_value(decision.action)}`")
    reason = getattr(result, "reason", "") or ""
    if reason:
        parts.append(_truncate(reason, TEXT_LIMIT))
    return "; ".join(parts) + "."


def _trigger_summary(result: Any) -> list[str]:
    diagnosis = getattr(result, "diagnosis", None)
    triggers: list[str] = []
    if diagnosis is not None:
        triggers.extend(str(item) for item in getattr(diagnosis, "trigger_conditions", []) or [])
        for hint in getattr(diagnosis, "adversarial_hints", []) or []:
            if "/divide" in hint or "/bug" in hint:
                triggers.append(str(hint))
    if not triggers and _diagnosis_category(diagnosis) == "arithmetic_boundary":
        triggers.extend(["GET /divide?x=0", "GET /bug"])
    return _dedupe(triggers)[:8]


def _diagnosis_category(diagnosis: Any) -> str:
    classification = getattr(diagnosis, "classification", None) if diagnosis is not None else None
    return str(getattr(classification, "category", "") or "")


def _command_text(command: list[str]) -> str:
    return _sanitize_text(" ".join(str(part) for part in command)) if command else "<missing command>"


def _action_value(action: Any) -> str:
    if isinstance(action, DecisionAction):
        return action.value
    return str(action or "")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = str(item).strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _truncate(value: str, limit: int) -> str:
    normalized = _sanitize_text(" ".join(str(value or "").split()))
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _sanitize_text(value: str) -> str:
    return (
        value.replace(".env", "[redacted-env]")
        .replace("BEGIN PRIVATE KEY", "[redacted-private-key]")
        .replace("DOUBAO_API_KEY", "[redacted-api-key-name]")
        .replace("FEISHU_APP_SECRET", "[redacted-feishu-secret-name]")
    )
