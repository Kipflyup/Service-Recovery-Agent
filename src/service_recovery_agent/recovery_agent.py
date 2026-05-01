"""High-level RecoveryAgent orchestration.

The lower-level modules in this package expose individual tools:

- read logs;
- parse traceback;
- build code context;
- call LLM;
- review and dry-run patch;
- apply patch;
- run validation;
- restore files on failure.

``RecoveryAgent`` is the first cohesive agent façade over those tools.  It is
still intentionally conservative: by default ``run_once`` only previews the
patch and never modifies files.  Callers must pass ``apply=True`` to actually
patch the workspace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from .adversarial_validator import (
    AdversarialValidationResult,
    format_adversarial_validation_result,
    validate_demo_app_after_repair,
)
from .code_context import CodeContext, build_code_context
from .contract_validator import (
    ContractValidationResult,
    build_contract_validation_result,
    format_contract_validation_result,
    run_contract_baseline,
)
from .counterfactual_validator import (
    CounterfactualValidationResult,
    build_counterfactual_validation_result,
    format_counterfactual_validation_result,
)
from .failure_feedback import (
    RepairFailureFeedback,
    build_adversarial_failure_feedback,
    build_apply_failure_feedback,
    build_contract_failure_feedback,
    build_counterfactual_failure_feedback,
    build_dry_run_failure_feedback,
    build_safety_failure_feedback,
    build_validation_failure_feedback,
    format_repair_failure_feedback_report,
)
from .fault_diagnosis import FaultDiagnosis, diagnose_fault
from .fix_planner import FixProposal, format_fix_proposal_report, propose_fix
from .git_diff_correlation import (
    GitDiffCorrelationResult,
    correlate_traceback_with_recent_diff,
    format_git_diff_correlation_evidence,
)
from .llm_client import LLMClient, create_llm_client
from .log_watcher import read_latest_traceback, wait_for_traceback
from .patcher import (
    PatchApplyResult,
    PatchPreviewResult,
    apply_patch_text,
    format_patch_preview_result,
    load_fix_proposal,
    preview_fix_proposal,
    preview_patch,
    restore_snapshot,
    snapshot_files,
)
from .traceback_parser import TracebackEvent, summarize_traceback
from .validator import ValidationResult, format_validation_result, validate_project


@dataclass(frozen=True)
class RecoveryAgentConfig:
    """Configuration shared by RecoveryAgent runs."""

    project_root: str | Path = "."
    log_path: str | Path | None = None
    dotenv_path: str | Path | None = None
    provider: str | None = None
    context_lines: int = 6
    strip: int = 1

    def resolved_project_root(self) -> Path:
        return Path(self.project_root).resolve()

    def resolved_log_path(self) -> Path:
        if self.log_path is not None:
            return Path(self.log_path)
        return Path(os.getenv("WEB_SERVICE_LOG_PATH") or self.resolved_project_root() / "logs" / "app.log")


@dataclass(frozen=True)
class RepairAttemptSummary:
    """Structured summary for one repair attempt in a bounded revise loop."""

    attempt_number: int
    status: str
    reason: str
    rolled_back: bool
    used_failure_feedback: RepairFailureFeedback | None = None
    proposal: FixProposal | None = None
    preview: PatchPreviewResult | None = None
    apply_result: PatchApplyResult | None = None
    validation_result: ValidationResult | None = None
    contract_result: ContractValidationResult | None = None
    adversarial_result: AdversarialValidationResult | None = None
    counterfactual_result: CounterfactualValidationResult | None = None
    failure_feedback: RepairFailureFeedback | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "status": self.status,
            "reason": self.reason,
            "rolled_back": self.rolled_back,
            "succeeded": self.succeeded,
            "used_failure_feedback": (
                self.used_failure_feedback.to_dict() if self.used_failure_feedback else None
            ),
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "preview": self.preview.to_dict() if self.preview else None,
            "apply": self.apply_result.to_dict() if self.apply_result else None,
            "validation": self.validation_result.to_dict() if self.validation_result else None,
            "contract_validation": self.contract_result.to_dict() if self.contract_result else None,
            "adversarial_validation": (
                self.adversarial_result.to_dict() if self.adversarial_result else None
            ),
            "counterfactual_validation": (
                self.counterfactual_result.to_dict() if self.counterfactual_result else None
            ),
            "failure_feedback": self.failure_feedback.to_dict() if self.failure_feedback else None,
        }


@dataclass(frozen=True)
class RecoveryRunResult:
    """Structured result from a single RecoveryAgent run."""

    status: str
    reason: str
    rolled_back: bool
    attempt_number: int = 0
    max_repair_attempts: int = 1
    repair_attempts: list[RepairAttemptSummary] = field(default_factory=list)
    traceback_event: TracebackEvent | None = None
    context: CodeContext | None = None
    diagnosis: FaultDiagnosis | None = None
    git_diff_correlation: GitDiffCorrelationResult | None = None
    proposal: FixProposal | None = None
    preview: PatchPreviewResult | None = None
    apply_result: PatchApplyResult | None = None
    validation_result: ValidationResult | None = None
    contract_result: ContractValidationResult | None = None
    adversarial_result: AdversarialValidationResult | None = None
    counterfactual_result: CounterfactualValidationResult | None = None
    failure_feedback: RepairFailureFeedback | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "PASS"

    @property
    def can_create_pr(self) -> bool:
        if self.status != "PASS" or self.rolled_back:
            return False
        if self.validation_result is not None and not self.validation_result.passed:
            return False
        if self.contract_result is not None and not self.contract_result.passed:
            return False
        if self.adversarial_result is not None and not self.adversarial_result.passed:
            return False
        if self.counterfactual_result is not None and not self.counterfactual_result.passed:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "rolled_back": self.rolled_back,
            "attempt_number": self.attempt_number,
            "max_repair_attempts": self.max_repair_attempts,
            "succeeded": self.succeeded,
            "can_create_pr": self.can_create_pr,
            "repair_attempts": [attempt.to_dict() for attempt in self.repair_attempts],
            "traceback_event": self.traceback_event.to_dict() if self.traceback_event else None,
            "context": self.context.to_dict() if self.context else None,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "git_diff_correlation": (
                self.git_diff_correlation.to_dict() if self.git_diff_correlation else None
            ),
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "preview": self.preview.to_dict() if self.preview else None,
            "apply": self.apply_result.to_dict() if self.apply_result else None,
            "validation": self.validation_result.to_dict() if self.validation_result else None,
            "contract_validation": self.contract_result.to_dict() if self.contract_result else None,
            "adversarial_validation": (
                self.adversarial_result.to_dict() if self.adversarial_result else None
            ),
            "counterfactual_validation": (
                self.counterfactual_result.to_dict() if self.counterfactual_result else None
            ),
            "failure_feedback": self.failure_feedback.to_dict() if self.failure_feedback else None,
        }


class RecoveryAgent:
    """Single-service recovery agent façade.

    The current agent supports a one-shot run.  A later ``watch`` loop can call
    ``run_once`` whenever ``log_watcher`` detects a new traceback.
    """

    def __init__(
        self,
        config: RecoveryAgentConfig | None = None,
        *,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.config = config or RecoveryAgentConfig()
        self.llm_client = llm_client

        if self.config.dotenv_path is not None and Path(self.config.dotenv_path).exists():
            load_dotenv(dotenv_path=self.config.dotenv_path)

    def run_once(
        self,
        *,
        wait_seconds: float = 0.0,
        include_existing: bool = False,
        proposal: FixProposal | None = None,
        proposal_json: str | Path | None = None,
        patch_file: str | Path | None = None,
        allow_files: Iterable[str] | None = None,
        apply: bool = False,
        validation_commands: Iterable[str | Iterable[str]] | None = None,
        validation_timeout_seconds: float = 120.0,
        skip_validation: bool = False,
        rollback_on_validation_fail: bool = True,
        run_contract_validation: bool = False,
        contract_validation_commands: Iterable[str | Iterable[str]] | None = None,
        run_adversarial_validation: bool = False,
        run_counterfactual_validation: bool = False,
        adversarial_log_path: str | Path | None = None,
        run_git_diff_correlation: bool = True,
        git_diff_max_commits: int = 5,
        max_repair_attempts: int = 1,
    ) -> RecoveryRunResult:
        """Run the recovery chain once.

        Args:
            wait_seconds: When > 0, wait for a traceback before running.
            include_existing: With wait enabled, also consider existing log
                content before waiting for new lines.
            proposal/proposal_json/patch_file: Optional patch sources.  If none
                is provided, the agent calls the configured LLM provider.
            allow_files: Extra files the patch may modify in addition to the
                crash file.
            apply: Actually modify files.  Defaults to false for safety.
            validation_commands: Commands to run after apply.  Defaults to
                ``python -m pytest -q`` when omitted.
            skip_validation: Apply without tests.  Not recommended.
            rollback_on_validation_fail: Restore modified files if validation
                fails.
            run_contract_validation: Compare selected validation commands
                before and after patch apply; any command that passed before
                must not fail after the patch.
            contract_validation_commands: Optional commands for contract
                validation. Defaults to ``validation_commands`` when omitted.
            run_adversarial_validation: After apply and normal validation pass,
                run diagnosis-derived adversarial probes as an additional
                quality gate.
            run_counterfactual_validation: Run adversarial probes before and
                after patch apply, expecting the buggy state to fail and the
                patched state to pass.
            adversarial_log_path: Optional log path used by the demo app while
                adversarial probes run.
            run_git_diff_correlation: Collect read-only recent Git diff
                correlation evidence and inject it into LLM repair prompts.
                This only uses ``git log`` / ``git show`` and never commits,
                pushes, or creates PRs.
            git_diff_max_commits: Number of recent commits to inspect when
                ``run_git_diff_correlation`` is enabled.
            max_repair_attempts: Maximum number of LLM-generated repair
                attempts. Defaults to 1. Extra attempts are only used for
                retryable ``RepairFailureFeedback`` stages.  Pre-apply dry-run
                failures can retry because the workspace was not modified;
                post-apply failures retry only after rollback restores a clean
                snapshot.
        """

        max_attempts = max(1, int(max_repair_attempts))
        event = self._read_traceback(wait_seconds=wait_seconds, include_existing=include_existing)
        if event is None:
            return RecoveryRunResult(
                status="NO_TRACEBACK",
                reason=f"No traceback found in {self.config.resolved_log_path()}.",
                rolled_back=False,
                max_repair_attempts=max_attempts,
            )

        context = build_code_context(
            event,
            project_root=self.config.resolved_project_root(),
            context_lines=self.config.context_lines,
        )
        diagnosis = diagnose_fault(context)
        git_diff_correlation = (
            correlate_traceback_with_recent_diff(
                context,
                project_root=self.config.resolved_project_root(),
                max_commits=git_diff_max_commits,
            )
            if run_git_diff_correlation
            else None
        )
        allowed_files = [context.crash_file_relative, *(allow_files or [])]
        can_auto_revise = proposal is None and proposal_json is None and patch_file is None
        attempt_summaries: list[RepairAttemptSummary] = []
        failure_feedback_for_next: RepairFailureFeedback | None = None

        for attempt_number in range(1, max_attempts + 1):
            proposal_to_use: FixProposal | None = proposal if attempt_number == 1 else None
            used_failure_feedback = failure_feedback_for_next

            if patch_file and attempt_number == 1:
                patch_text = Path(patch_file).read_text(encoding="utf-8")
                preview = preview_patch(
                    patch_text,
                    context,
                    project_root=self.config.resolved_project_root(),
                    allowed_files=allowed_files,
                    strip=self.config.strip,
                )
            else:
                if attempt_number == 1 and proposal_to_use is None and proposal_json is not None:
                    proposal_to_use = load_fix_proposal(proposal_json)
                if proposal_to_use is None:
                    proposal_to_use = propose_fix(
                        context,
                        self._client(),
                        diagnosis=diagnosis,
                        failure_feedback=used_failure_feedback,
                        git_diff_correlation=git_diff_correlation,
                    )

                patch_text = proposal_to_use.patch_draft
                preview = preview_fix_proposal(
                    proposal_to_use,
                    context,
                    project_root=self.config.resolved_project_root(),
                    allowed_files=allowed_files,
                    strip=self.config.strip,
                )

            if preview.status != "PASS" or preview.dry_run is None or not preview.dry_run.can_apply:
                status = "ABORTED"
                reason = "Patch did not pass safety review or dry-run."
                failure_feedback = _build_preview_failure_feedback(
                    preview,
                    diagnosis=diagnosis,
                )
                attempt_summaries.append(
                    RepairAttemptSummary(
                        attempt_number=attempt_number,
                        status=status,
                        reason=reason,
                        rolled_back=False,
                        used_failure_feedback=used_failure_feedback,
                        proposal=proposal_to_use,
                        preview=preview,
                        failure_feedback=failure_feedback,
                    )
                )
                can_retry = (
                    can_auto_revise
                    and _can_retry_with_failure_feedback(
                        failure_feedback,
                        rolled_back=False,
                        rollback_on_validation_fail=rollback_on_validation_fail,
                    )
                    and attempt_number < max_attempts
                )
                if can_retry:
                    failure_feedback_for_next = failure_feedback
                    continue

                return RecoveryRunResult(
                    status=status,
                    reason=reason,
                    rolled_back=False,
                    attempt_number=attempt_number,
                    max_repair_attempts=max_attempts,
                    repair_attempts=attempt_summaries,
                    traceback_event=event,
                    context=context,
                    diagnosis=diagnosis,
                    git_diff_correlation=git_diff_correlation,
                    proposal=proposal_to_use,
                    preview=preview,
                    failure_feedback=failure_feedback,
                )

            if not apply:
                status = "PREVIEW_ONLY"
                reason = "Patch passed safety review and dry-run. Pass apply=True to modify files."
                attempt_summaries.append(
                    RepairAttemptSummary(
                        attempt_number=attempt_number,
                        status=status,
                        reason=reason,
                        rolled_back=False,
                        used_failure_feedback=used_failure_feedback,
                        proposal=proposal_to_use,
                        preview=preview,
                    )
                )
                return RecoveryRunResult(
                    status=status,
                    reason=reason,
                    rolled_back=False,
                    attempt_number=attempt_number,
                    max_repair_attempts=max_attempts,
                    repair_attempts=attempt_summaries,
                    traceback_event=event,
                    context=context,
                    diagnosis=diagnosis,
                    git_diff_correlation=git_diff_correlation,
                    proposal=proposal_to_use,
                    preview=preview,
                )

            contract_commands = contract_validation_commands if contract_validation_commands is not None else validation_commands
            before_contract_validation_result: ValidationResult | None = None
            if run_contract_validation and not skip_validation:
                before_contract_validation_result = run_contract_baseline(
                    project_root=self.config.resolved_project_root(),
                    commands=contract_commands,
                    timeout_seconds=validation_timeout_seconds,
                )

            before_counterfactual_adversarial: AdversarialValidationResult | None = None
            if run_counterfactual_validation:
                before_counterfactual_adversarial = validate_demo_app_after_repair(
                    diagnosis,
                    log_path=adversarial_log_path,
                )

            snapshot = snapshot_files(self.config.resolved_project_root(), preview.safety_review.modified_files)
            apply_result = apply_patch_text(
                patch_text,
                project_root=self.config.resolved_project_root(),
                strip=self.config.strip,
            )

            validation_result: ValidationResult | None = None
            contract_result: ContractValidationResult | None = None
            adversarial_result: AdversarialValidationResult | None = None
            counterfactual_result: CounterfactualValidationResult | None = None
            failure_feedback: RepairFailureFeedback | None = None
            rolled_back = False
            if not apply_result.applied:
                restore_snapshot(self.config.resolved_project_root(), snapshot)
                rolled_back = True
                failure_feedback = build_apply_failure_feedback(
                    apply_result,
                    diagnosis=diagnosis,
                )
            elif not skip_validation:
                validation_result = validate_project(
                    project_root=self.config.resolved_project_root(),
                    commands=validation_commands,
                    timeout_seconds=validation_timeout_seconds,
                )
                if not validation_result.passed:
                    failure_feedback = build_validation_failure_feedback(
                        validation_result,
                        diagnosis=diagnosis,
                    )
                    if rollback_on_validation_fail:
                        restore_snapshot(self.config.resolved_project_root(), snapshot)
                        rolled_back = True

            should_run_contract = (
                run_contract_validation
                and apply_result.applied
                and not skip_validation
                and not rolled_back
                and before_contract_validation_result is not None
                and (validation_result is None or validation_result.passed)
            )
            if should_run_contract:
                if contract_validation_commands is None and validation_result is not None:
                    after_contract_validation_result = validation_result
                else:
                    after_contract_validation_result = validate_project(
                        project_root=self.config.resolved_project_root(),
                        commands=contract_commands,
                        timeout_seconds=validation_timeout_seconds,
                    )
                contract_result = build_contract_validation_result(
                    before_contract_validation_result,
                    after_contract_validation_result,
                )
                if not contract_result.passed:
                    failure_feedback = build_contract_failure_feedback(
                        contract_result,
                        diagnosis=diagnosis,
                    )
                    if rollback_on_validation_fail:
                        restore_snapshot(self.config.resolved_project_root(), snapshot)
                        rolled_back = True

            should_run_adversarial = (
                (run_adversarial_validation or run_counterfactual_validation)
                and apply_result.applied
                and not rolled_back
                and (validation_result is None or validation_result.passed)
                and (contract_result is None or contract_result.passed)
            )
            if should_run_adversarial:
                adversarial_result = validate_demo_app_after_repair(
                    diagnosis,
                    log_path=adversarial_log_path,
                )
                if run_counterfactual_validation:
                    counterfactual_result = build_counterfactual_validation_result(
                        before_patch_result=before_counterfactual_adversarial,
                        after_patch_result=adversarial_result,
                    )
                if not adversarial_result.passed:
                    failure_feedback = build_adversarial_failure_feedback(
                        adversarial_result,
                        diagnosis=diagnosis,
                    )
                    if rollback_on_validation_fail:
                        restore_snapshot(self.config.resolved_project_root(), snapshot)
                        rolled_back = True
                elif counterfactual_result is not None and not counterfactual_result.passed:
                    failure_feedback = build_counterfactual_failure_feedback(
                        counterfactual_result,
                        diagnosis=diagnosis,
                    )
                    if rollback_on_validation_fail:
                        restore_snapshot(self.config.resolved_project_root(), snapshot)
                        rolled_back = True

            status = "PASS"
            reason = "Patch applied and validation passed."
            if not apply_result.applied:
                status = "FAIL"
                reason = "Patch apply failed; snapshot restored."
            elif validation_result is not None and not validation_result.passed:
                status = "FAIL_ROLLED_BACK" if rolled_back else "FAIL"
                reason = "Validation failed; snapshot restored." if rolled_back else "Validation failed."
            elif contract_result is not None and not contract_result.passed:
                status = "FAIL_ROLLED_BACK" if rolled_back else "FAIL"
                reason = (
                    "Contract validation failed; snapshot restored."
                    if rolled_back
                    else "Contract validation failed."
                )
            elif adversarial_result is not None and not adversarial_result.passed:
                status = "FAIL_ROLLED_BACK" if rolled_back else "FAIL"
                reason = (
                    "Adversarial validation failed; snapshot restored."
                    if rolled_back
                    else "Adversarial validation failed."
                )
            elif counterfactual_result is not None and not counterfactual_result.passed:
                status = "FAIL_ROLLED_BACK" if rolled_back else "FAIL"
                reason = (
                    "Counterfactual validation failed; snapshot restored."
                    if rolled_back
                    else "Counterfactual validation failed."
                )
            elif counterfactual_result is not None and counterfactual_result.passed:
                reason = "Patch applied; validation, adversarial validation, and counterfactual validation passed."
            elif contract_result is not None and contract_result.passed and adversarial_result is not None and adversarial_result.passed:
                reason = "Patch applied; validation, contract validation, and adversarial validation passed."
            elif contract_result is not None and contract_result.passed:
                reason = "Patch applied; validation and contract validation passed."
            elif adversarial_result is not None and adversarial_result.passed:
                reason = (
                    "Patch applied; validation skipped; adversarial validation passed."
                    if validation_result is None and skip_validation
                    else "Patch applied; validation and adversarial validation passed."
                )
            elif validation_result is None and skip_validation:
                reason = "Patch applied; validation skipped."

            attempt_summaries.append(
                RepairAttemptSummary(
                    attempt_number=attempt_number,
                    status=status,
                    reason=reason,
                    rolled_back=rolled_back,
                    used_failure_feedback=used_failure_feedback,
                    proposal=proposal_to_use,
                    preview=preview,
                    apply_result=apply_result,
                    validation_result=validation_result,
                    contract_result=contract_result,
                    adversarial_result=adversarial_result,
                    counterfactual_result=counterfactual_result,
                    failure_feedback=failure_feedback,
                )
            )

            if status == "PASS":
                return RecoveryRunResult(
                    status=status,
                    reason=reason,
                    rolled_back=rolled_back,
                    attempt_number=attempt_number,
                    max_repair_attempts=max_attempts,
                    repair_attempts=attempt_summaries,
                    traceback_event=event,
                    context=context,
                    diagnosis=diagnosis,
                    git_diff_correlation=git_diff_correlation,
                    proposal=proposal_to_use,
                    preview=preview,
                    apply_result=apply_result,
                    validation_result=validation_result,
                    contract_result=contract_result,
                    adversarial_result=adversarial_result,
                    counterfactual_result=counterfactual_result,
                    failure_feedback=failure_feedback,
                )

            can_retry = (
                can_auto_revise
                and _can_retry_with_failure_feedback(
                    failure_feedback,
                    rolled_back=rolled_back,
                    rollback_on_validation_fail=rollback_on_validation_fail,
                )
                and attempt_number < max_attempts
            )
            if can_retry:
                failure_feedback_for_next = failure_feedback
                continue

            return RecoveryRunResult(
                status=status,
                reason=reason,
                rolled_back=rolled_back,
                attempt_number=attempt_number,
                max_repair_attempts=max_attempts,
                repair_attempts=attempt_summaries,
                traceback_event=event,
                context=context,
                diagnosis=diagnosis,
                git_diff_correlation=git_diff_correlation,
                proposal=proposal_to_use,
                preview=preview,
                apply_result=apply_result,
                validation_result=validation_result,
                contract_result=contract_result,
                adversarial_result=adversarial_result,
                counterfactual_result=counterfactual_result,
                failure_feedback=failure_feedback,
            )

        # Defensive fallback: the loop always returns from inside.  Keep this
        # branch to satisfy static checkers and future refactors.
        return RecoveryRunResult(
            status="FAIL",
            reason="Repair loop ended without producing a result.",
            rolled_back=False,
            attempt_number=len(attempt_summaries),
            max_repair_attempts=max_attempts,
            repair_attempts=attempt_summaries,
            traceback_event=event,
            context=context,
            diagnosis=diagnosis,
            git_diff_correlation=git_diff_correlation,
        )

    def _read_traceback(self, *, wait_seconds: float, include_existing: bool) -> TracebackEvent | None:
        log_path = self.config.resolved_log_path()
        if wait_seconds > 0:
            return wait_for_traceback(
                log_path,
                timeout_seconds=wait_seconds,
                start_at_end=not include_existing,
            )
        return read_latest_traceback(log_path)

    def _client(self) -> LLMClient:
        if self.llm_client is not None:
            return self.llm_client
        self.llm_client = create_llm_client(
            provider=self.config.provider,
            dotenv_path=self.config.dotenv_path,
        )
        return self.llm_client


def _build_preview_failure_feedback(
    preview: PatchPreviewResult,
    *,
    diagnosis: FaultDiagnosis | None,
) -> RepairFailureFeedback | None:
    if preview.safety_review.status != "PASS":
        return build_safety_failure_feedback(preview, diagnosis=diagnosis)
    return build_dry_run_failure_feedback(preview, diagnosis=diagnosis)


def _can_retry_with_failure_feedback(
    feedback: RepairFailureFeedback | None,
    *,
    rolled_back: bool,
    rollback_on_validation_fail: bool,
) -> bool:
    """Return whether a failed attempt can safely feed a bounded revise retry."""

    if feedback is None:
        return False

    stage = feedback.failed_stage
    if stage == "patch_dry_run":
        # Dry-run failure happens before any source modification, so the next
        # LLM-generated patch can retry from a clean workspace without rollback.
        return True
    if stage == "patch_apply":
        # Apply failure snapshots are restored unconditionally in run_once.
        return rolled_back
    if stage in {
        "validation",
        "contract_validation",
        "counterfactual_validation",
        "adversarial_validation",
    }:
        return rollback_on_validation_fail and rolled_back

    # Safety review failures are intentionally not retried in the first staged
    # feedback implementation: dangerous operations, disallowed files, special
    # floats, or signature changes should be surfaced instead of automatically
    # nudging the model to bypass the guardrail.
    return False


def format_recovery_run_result(result: RecoveryRunResult) -> str:
    """Render a human-readable RecoveryAgent run report."""

    lines: list[str] = []
    lines.append("# Recovery Agent Run")
    lines.append("")
    lines.append(f"- Status: **{result.status}**")
    lines.append(f"- Reason: {result.reason}")
    lines.append(f"- Rolled back: {result.rolled_back}")
    lines.append(f"- Can create PR: {result.can_create_pr}")
    if result.attempt_number:
        lines.append(f"- Attempt: {result.attempt_number}/{result.max_repair_attempts}")

    if result.repair_attempts:
        lines.append("")
        lines.append("## Repair Attempts")
        for attempt in result.repair_attempts:
            lines.append(
                f"- Attempt {attempt.attempt_number}: **{attempt.status}**, "
                f"rolled_back={attempt.rolled_back}, reason={attempt.reason}"
            )
            if attempt.used_failure_feedback is not None:
                failed = ", ".join(attempt.used_failure_feedback.failed_probes) or "unknown"
                lines.append(f"  - Used feedback from failed probes: {failed}")
            if attempt.failure_feedback is not None:
                failed = ", ".join(attempt.failure_feedback.failed_probes) or "unknown"
                lines.append(f"  - Produced feedback for failed probes: {failed}")

    if result.traceback_event is not None:
        lines.append("")
        lines.append("## Traceback")
        lines.append(f"- Summary: {summarize_traceback(result.traceback_event)}")
        crash_frame = result.traceback_event.crash_frame
        if crash_frame is not None:
            lines.append(f"- Crash site: {crash_frame.file}:{crash_frame.line_number} in {crash_frame.function}")

    if result.context is not None:
        lines.append("")
        lines.append("## Code Context Summary")
        lines.append(f"- Crash file: `{result.context.crash_file_relative}`")
        lines.append(f"- Crash line: {result.context.crash_line_number}")
        if result.context.crash_line_code:
            lines.append(f"- Crash code: `{result.context.crash_line_code.strip()}`")
        if result.context.crash_function:
            lines.append(f"- Crash function: `{result.context.crash_function.qualname}`")
        lines.append(f"- Impact risk: **{result.context.impact.risk_level}**")
        lines.append(f"- Direct callers: {result.context.impact.direct_call_count}")

    if result.diagnosis is not None:
        lines.append("")
        lines.append("## Fault Diagnosis Summary")
        classification = result.diagnosis.classification
        lines.append(f"- Category: **{classification.category}**")
        lines.append(f"- Confidence: {classification.confidence}")
        if result.diagnosis.primary_candidate is not None:
            candidate = result.diagnosis.primary_candidate
            lines.append(f"- Primary candidate: `{candidate.file}:{candidate.line_number}`")
            if candidate.function:
                lines.append(f"- Candidate function: `{candidate.function}`")
        if result.diagnosis.suspected_variables:
            lines.append(f"- Suspected variables: {', '.join(result.diagnosis.suspected_variables)}")
        if result.diagnosis.trigger_conditions:
            lines.append(f"- Trigger conditions: {', '.join(result.diagnosis.trigger_conditions)}")
        if result.diagnosis.repair_constraints:
            lines.append("- Repair constraints:")
            for constraint in result.diagnosis.repair_constraints:
                lines.append(f"  - **{constraint.kind}**: {constraint.description}")

    if result.git_diff_correlation is not None:
        lines.append("")
        lines.append("## Git Diff Correlation Summary")
        lines.append(format_git_diff_correlation_evidence(result.git_diff_correlation))

    if result.proposal is not None:
        lines.append("")
        lines.append(format_fix_proposal_report(result.proposal).rstrip())

    if result.preview is not None:
        lines.append("")
        lines.append(format_patch_preview_result(result.preview))

    lines.append("")
    lines.append("## Patch Apply")
    if result.apply_result is None:
        lines.append("- Skipped. This run did not modify files.")
    else:
        lines.append(f"- Applied: {result.apply_result.applied}")
        lines.append(f"- Return code: {result.apply_result.return_code}")
        lines.append(f"- Command: `{' '.join(result.apply_result.command)}`")
        if result.apply_result.stdout.strip():
            lines.extend(["", "stdout:", "```text", result.apply_result.stdout.rstrip(), "```"])
        if result.apply_result.stderr.strip():
            lines.extend(["", "stderr:", "```text", result.apply_result.stderr.rstrip(), "```"])

    lines.append("")
    if result.validation_result is None:
        lines.append("## Validation Result")
        lines.append("- Skipped.")
    else:
        lines.append(format_validation_result(result.validation_result))

    lines.append("")
    if result.contract_result is None:
        lines.append("## Contract Validation")
        lines.append("- Skipped.")
    else:
        lines.append(format_contract_validation_result(result.contract_result))

    lines.append("")
    if result.adversarial_result is None:
        lines.append("## Adversarial Validation")
        lines.append("- Skipped.")
    else:
        lines.append(format_adversarial_validation_result(result.adversarial_result))

    lines.append("")
    if result.counterfactual_result is None:
        lines.append("## Counterfactual Validation")
        lines.append("- Skipped.")
    else:
        lines.append(format_counterfactual_validation_result(result.counterfactual_result))

    if result.failure_feedback is not None:
        lines.append("")
        lines.append(format_repair_failure_feedback_report(result.failure_feedback))

    return "\n".join(lines).rstrip()
