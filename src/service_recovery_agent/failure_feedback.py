"""Failure feedback helpers for repair iteration.

A repair attempt can fail at several different pipeline stages: static patch
safety review, patch dry-run, real patch apply, ordinary validation, contract
preservation, counterfactual validation, or adversarial probes.  This module
turns those failures into small, structured feedback objects that can be:

- rendered in a human report;
- embedded into the next LLM repair prompt;
- inspected by the bounded revise loop without parsing natural language.

The feedback intentionally stores only concise excerpts of command output.  It
must not become a full log archive, because it is designed to be copied into a
future model prompt and possibly a review artifact.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .adversarial_validator import AdversarialValidationResult, ProbeAttemptResult, ProbeCase, ProbeResult
from .fault_diagnosis import FaultDiagnosis

if TYPE_CHECKING:
    from .contract_validator import ContractCommandBaseline, ContractValidationResult
    from .counterfactual_validator import CounterfactualValidationResult
    from .patch_safety import PatchSafetyFinding
    from .patcher import PatchApplyResult, PatchPreviewResult
    from .validator import ValidationCommandResult, ValidationResult


EXCERPT_LIMIT = 800
REPORT_DETAILS_LIMIT = 2_000


@dataclass(frozen=True)
class RepairFailureFeedback:
    """Structured feedback that explains why a repair attempt failed.

    ``failed_probes`` is retained for backwards compatibility with the first
    adversarial-only implementation.  For non-adversarial stages it contains
    the failed checks or failed commands, while richer stage-specific evidence
    is stored in ``details``.
    """

    failed_stage: str
    failed_probes: list[str]
    summary: str
    prompt_feedback: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_safety_failure_feedback(
    preview: PatchPreviewResult,
    *,
    diagnosis: FaultDiagnosis | None = None,
) -> RepairFailureFeedback | None:
    """Build feedback for a non-PASS static patch safety review."""

    review = preview.safety_review
    if review.status == "PASS":
        return None

    non_passing_findings = [finding for finding in review.findings if finding.status != "PASS"]
    failed_checks = [finding.check for finding in non_passing_findings] or ["safety_review"]
    summary = f"Patch safety review {review.status}: {review.summary}"
    details = {
        "safety_status": review.status,
        "modified_files": list(review.modified_files),
        "allowed_files": list(review.allowed_files),
        "non_passing_findings": [_finding_details(finding) for finding in non_passing_findings],
    }

    return RepairFailureFeedback(
        failed_stage="safety_review",
        failed_probes=failed_checks,
        summary=summary,
        prompt_feedback=_format_safety_prompt_feedback(
            preview,
            non_passing_findings=non_passing_findings,
            diagnosis=diagnosis,
        ),
        details=details,
    )


def build_dry_run_failure_feedback(
    preview: PatchPreviewResult,
    *,
    diagnosis: FaultDiagnosis | None = None,
) -> RepairFailureFeedback | None:
    """Build feedback for a patch that passed safety but failed dry-run."""

    dry_run = preview.dry_run
    if dry_run is None or dry_run.can_apply:
        return None

    summary = f"Patch dry-run failed with return code {dry_run.return_code}."
    details = {
        "command": list(dry_run.command),
        "return_code": dry_run.return_code,
        "can_apply": dry_run.can_apply,
        "stdout_excerpt": _excerpt(dry_run.stdout),
        "stderr_excerpt": _excerpt(dry_run.stderr),
        "patch_excerpt": _excerpt(preview.patch_text),
    }

    return RepairFailureFeedback(
        failed_stage="patch_dry_run",
        failed_probes=["patch_dry_run"],
        summary=summary,
        prompt_feedback=_format_dry_run_prompt_feedback(preview, diagnosis=diagnosis),
        details=details,
    )


def build_apply_failure_feedback(
    apply_result: PatchApplyResult,
    *,
    diagnosis: FaultDiagnosis | None = None,
) -> RepairFailureFeedback | None:
    """Build feedback for a real patch apply failure."""

    if apply_result.applied:
        return None

    summary = f"Patch apply failed with return code {apply_result.return_code}."
    details = {
        "command": list(apply_result.command),
        "return_code": apply_result.return_code,
        "applied": apply_result.applied,
        "stdout_excerpt": _excerpt(apply_result.stdout),
        "stderr_excerpt": _excerpt(apply_result.stderr),
    }

    return RepairFailureFeedback(
        failed_stage="patch_apply",
        failed_probes=["patch_apply"],
        summary=summary,
        prompt_feedback=_format_apply_prompt_feedback(apply_result, diagnosis=diagnosis),
        details=details,
    )


def build_validation_failure_feedback(
    validation_result: ValidationResult,
    *,
    diagnosis: FaultDiagnosis | None = None,
) -> RepairFailureFeedback | None:
    """Build feedback for failing ordinary validation commands."""

    if validation_result.passed:
        return None

    failed_commands = [command for command in validation_result.command_results if not command.passed]
    failed_command_names = [_command_text(command.command) for command in failed_commands] or ["validation"]
    summary = f"Validation failed: {len(failed_commands)}/{len(validation_result.command_results)} command(s) failed."
    details = {
        "validation_status": validation_result.status,
        "failed_commands": [_command_result_details(command) for command in failed_commands],
        "total_command_count": len(validation_result.command_results),
    }

    return RepairFailureFeedback(
        failed_stage="validation",
        failed_probes=failed_command_names,
        summary=summary,
        prompt_feedback=_format_validation_prompt_feedback(
            validation_result,
            failed_commands=failed_commands,
            diagnosis=diagnosis,
        ),
        details=details,
    )


def build_contract_failure_feedback(
    contract_result: ContractValidationResult,
    *,
    diagnosis: FaultDiagnosis | None = None,
) -> RepairFailureFeedback | None:
    """Build feedback for contract-preservation failures or partial results."""

    if contract_result.passed:
        return None

    problematic_baselines = [
        baseline
        for baseline in contract_result.command_baselines
        if baseline.regression or not baseline.after_passed
    ]
    failed_commands = [_command_text(baseline.command) for baseline in problematic_baselines] or ["contract_validation"]
    details = {
        "contract_status": contract_result.status,
        "summary": contract_result.summary,
        "problematic_baselines": [_contract_baseline_details(baseline) for baseline in problematic_baselines],
        "regression_count": sum(1 for baseline in contract_result.command_baselines if baseline.regression),
        "total_command_count": len(contract_result.command_baselines),
    }

    return RepairFailureFeedback(
        failed_stage="contract_validation",
        failed_probes=failed_commands,
        summary=f"Contract validation {contract_result.status}: {contract_result.summary}",
        prompt_feedback=_format_contract_prompt_feedback(
            contract_result,
            problematic_baselines=problematic_baselines,
            diagnosis=diagnosis,
        ),
        details=details,
    )


def build_counterfactual_failure_feedback(
    counterfactual_result: CounterfactualValidationResult,
    *,
    diagnosis: FaultDiagnosis | None = None,
) -> RepairFailureFeedback | None:
    """Build feedback for failed before/after counterfactual validation."""

    if counterfactual_result.passed:
        return None

    failed_conditions = _counterfactual_failed_conditions(counterfactual_result)
    details = {
        "counterfactual_status": counterfactual_result.status,
        "summary": counterfactual_result.summary,
        "expected_before_fail": counterfactual_result.expected_before_fail,
        "expected_after_pass": counterfactual_result.expected_after_pass,
        "before_patch_status": (
            counterfactual_result.before_patch_result.status
            if counterfactual_result.before_patch_result is not None
            else None
        ),
        "before_patch_summary": (
            counterfactual_result.before_patch_result.summary
            if counterfactual_result.before_patch_result is not None
            else None
        ),
        "after_patch_status": (
            counterfactual_result.after_patch_result.status
            if counterfactual_result.after_patch_result is not None
            else None
        ),
        "after_patch_summary": (
            counterfactual_result.after_patch_result.summary
            if counterfactual_result.after_patch_result is not None
            else None
        ),
        "failed_conditions": failed_conditions,
    }

    return RepairFailureFeedback(
        failed_stage="counterfactual_validation",
        failed_probes=failed_conditions or ["counterfactual_validation"],
        summary=f"Counterfactual validation {counterfactual_result.status}: {counterfactual_result.summary}",
        prompt_feedback=_format_counterfactual_prompt_feedback(counterfactual_result, diagnosis=diagnosis),
        details=details,
    )


def build_adversarial_failure_feedback(
    result: AdversarialValidationResult,
    *,
    diagnosis: FaultDiagnosis | None = None,
) -> RepairFailureFeedback | None:
    """Build LLM-ready feedback from a failed adversarial validation result."""

    if result.passed:
        return None

    failed_results = [probe_result for probe_result in result.probe_results if not probe_result.passed]
    failed_probe_names = [probe_result.case.name for probe_result in failed_results]
    summary = result.summary or "Adversarial validation failed."
    details = {
        "adversarial_status": result.status,
        "summary": result.summary,
        "failed_probes": [_probe_result_details(probe_result) for probe_result in failed_results],
        "total_probe_count": len(result.probe_results),
    }
    prompt_feedback = _format_adversarial_prompt_feedback(
        result,
        failed_results=failed_results,
        diagnosis=diagnosis,
    )

    return RepairFailureFeedback(
        failed_stage="adversarial_validation",
        failed_probes=failed_probe_names,
        summary=summary,
        prompt_feedback=prompt_feedback,
        details=details,
    )


def format_repair_failure_feedback_report(feedback: RepairFailureFeedback) -> str:
    """Render failure feedback for terminal reports."""

    lines: list[str] = []
    lines.append("# Repair Failure Feedback")
    lines.append("")
    lines.append(f"- Failed stage: **{feedback.failed_stage}**")
    lines.append(f"- Summary: {feedback.summary}")
    lines.append("- Failed checks/probes:")
    if feedback.failed_probes:
        for probe in feedback.failed_probes:
            lines.append(f"  - {probe}")
    else:
        lines.append("  - No individual failed check/probe was reported.")

    if feedback.details:
        rendered_details = json.dumps(feedback.details, ensure_ascii=False, indent=2, default=str)
        lines.append("")
        lines.append("## Structured Details")
        lines.append("```text")
        lines.append(_excerpt(rendered_details, limit=REPORT_DETAILS_LIMIT).rstrip())
        lines.append("```")

    lines.append("")
    lines.append("## Prompt Feedback For Next Repair Attempt")
    lines.append("```text")
    lines.append(feedback.prompt_feedback.rstrip())
    lines.append("```")
    return "\n".join(lines)


def _format_safety_prompt_feedback(
    preview: PatchPreviewResult,
    *,
    non_passing_findings: list[PatchSafetyFinding],
    diagnosis: FaultDiagnosis | None,
) -> str:
    review = preview.safety_review
    lines: list[str] = []
    lines.append("Patch was blocked before dry-run by static patch safety review.")
    lines.append(f"Safety status: {review.status}")
    lines.append(f"Safety summary: {review.summary}")
    lines.append(f"Modified files: {', '.join(review.modified_files) if review.modified_files else 'none detected'}")
    lines.append(f"Allowed files: {', '.join(review.allowed_files) if review.allowed_files else 'none configured'}")
    _append_diagnosis_context(lines, diagnosis)

    if non_passing_findings:
        lines.append("")
        lines.append("Non-passing safety findings:")
        for finding in non_passing_findings:
            lines.append(f"- check: {finding.check}")
            lines.append(f"  status: {finding.status}")
            lines.append(f"  message: {finding.message}")
            if finding.evidence:
                lines.append(f"  evidence: {_stringify_list_excerpt(finding.evidence)}")
    else:
        lines.append("")
        lines.append("No individual non-passing safety finding was recorded; inspect the safety summary.")

    lines.append("")
    lines.append("Please generate a safer minimal patch that passes static safety review before any dry-run/apply step.")
    lines.append("Do not bypass dangerous_operations, allowed_files, nan_or_infinity, or function_signature findings.")
    lines.append("Keep the repair inside the diagnosed crash file unless explicit allowed_files evidence is provided.")
    return "\n".join(lines)


def _format_dry_run_prompt_feedback(
    preview: PatchPreviewResult,
    *,
    diagnosis: FaultDiagnosis | None,
) -> str:
    dry_run = preview.dry_run
    lines: list[str] = []
    lines.append("Patch passed static safety review but failed patch dry-run, so it was not applied.")
    if dry_run is None:
        lines.append("Dry-run result is missing; regenerate a valid unified diff against the current repository state.")
    else:
        lines.append(f"Dry-run command: {_command_text(dry_run.command)}")
        lines.append(f"Return code: {dry_run.return_code}")
        _append_output_excerpt(lines, "stdout", dry_run.stdout)
        _append_output_excerpt(lines, "stderr", dry_run.stderr)
    _append_diagnosis_context(lines, diagnosis)
    lines.append("")
    lines.append("Please revise the patch hunks so they apply cleanly to the current file content.")
    lines.append("Keep the same repair intent, but regenerate context lines and hunk offsets from the current source.")
    return "\n".join(lines)


def _format_apply_prompt_feedback(
    apply_result: PatchApplyResult,
    *,
    diagnosis: FaultDiagnosis | None,
) -> str:
    lines: list[str] = []
    lines.append("Patch passed preview but failed during real patch apply.")
    lines.append("The agent restores the file snapshot when rollback is enabled; the next attempt must start from the original source.")
    lines.append(f"Apply command: {_command_text(apply_result.command)}")
    lines.append(f"Return code: {apply_result.return_code}")
    _append_output_excerpt(lines, "stdout", apply_result.stdout)
    _append_output_excerpt(lines, "stderr", apply_result.stderr)
    _append_diagnosis_context(lines, diagnosis)
    lines.append("")
    lines.append("Please generate a clean unified diff that can be applied exactly once after a successful dry-run.")
    return "\n".join(lines)


def _format_validation_prompt_feedback(
    validation_result: ValidationResult,
    *,
    failed_commands: list[ValidationCommandResult],
    diagnosis: FaultDiagnosis | None,
) -> str:
    lines: list[str] = []
    lines.append("Patch applied but ordinary validation failed.")
    lines.append(f"Validation status: {validation_result.status}")
    _append_diagnosis_context(lines, diagnosis)

    if failed_commands:
        lines.append("")
        lines.append("Failed validation commands:")
        for command in failed_commands:
            _append_command_failure(lines, command)
    else:
        lines.append("")
        lines.append("Validation result was non-PASS, but no individual failing command was recorded.")

    lines.append("")
    lines.append("Please revise the patch so the failed validation commands pass while preserving the original repair constraints.")
    lines.append("Do not remove or weaken tests to make validation pass.")
    return "\n".join(lines)


def _format_contract_prompt_feedback(
    contract_result: ContractValidationResult,
    *,
    problematic_baselines: list[ContractCommandBaseline],
    diagnosis: FaultDiagnosis | None,
) -> str:
    lines: list[str] = []
    lines.append("Patch passed the earlier gate but failed contract-preservation validation.")
    lines.append(f"Contract status: {contract_result.status}")
    lines.append(f"Contract summary: {contract_result.summary}")
    _append_diagnosis_context(lines, diagnosis)

    if problematic_baselines:
        lines.append("")
        lines.append("Problematic command baselines:")
        for baseline in problematic_baselines:
            lines.append(f"- command: {_command_text(baseline.command)}")
            lines.append(f"  before_passed: {baseline.before_passed} (rc={baseline.before_return_code})")
            lines.append(f"  after_passed: {baseline.after_passed} (rc={baseline.after_return_code})")
            lines.append(f"  regression: {baseline.regression}")
            _append_output_excerpt(lines, "  after stdout", baseline.stdout_after)
            _append_output_excerpt(lines, "  after stderr", baseline.stderr_after)
    else:
        lines.append("")
        lines.append("No individual contract baseline was recorded; inspect the contract summary.")

    lines.append("")
    lines.append("Please revise the patch so previously passing behavior remains passing after the fix.")
    lines.append("If the contract must change, mark it as a D-type interface/contract change requiring human review instead of hiding the regression.")
    return "\n".join(lines)


def _format_counterfactual_prompt_feedback(
    result: CounterfactualValidationResult,
    *,
    diagnosis: FaultDiagnosis | None,
) -> str:
    lines: list[str] = []
    lines.append("Counterfactual validation failed.")
    lines.append(f"Counterfactual status: {result.status}")
    lines.append(f"Counterfactual summary: {result.summary}")
    lines.append(f"Expected before_patch fail: {result.expected_before_fail}")
    lines.append(f"Expected after_patch pass: {result.expected_after_pass}")
    _append_diagnosis_context(lines, diagnosis)

    if result.before_patch_result is None:
        lines.append("- before_patch adversarial result: missing")
    else:
        lines.append(f"- before_patch adversarial status: {result.before_patch_result.status}")
        lines.append(f"  summary: {result.before_patch_result.summary}")
    if result.after_patch_result is None:
        lines.append("- after_patch adversarial result: missing")
    else:
        lines.append(f"- after_patch adversarial status: {result.after_patch_result.status}")
        lines.append(f"  summary: {result.after_patch_result.summary}")

    failed_conditions = _counterfactual_failed_conditions(result)
    if failed_conditions:
        lines.append("")
        lines.append("Failed counterfactual conditions:")
        for condition in failed_conditions:
            lines.append(f"- {condition}")

    after_result = result.after_patch_result
    if after_result is not None and not after_result.passed:
        failed_after = [probe_result for probe_result in after_result.probe_results if not probe_result.passed]
        if failed_after:
            lines.append("")
            lines.append("Failed after_patch probes:")
            for probe_result in failed_after:
                lines.extend(_format_failed_probe(probe_result.case, _first_failed_attempt(probe_result)))

    lines.append("")
    lines.append("Please revise the patch so the original buggy state is demonstrably failing and the patched state passes the same probes.")
    lines.append("If before_patch unexpectedly passes, do not claim the bug is fixed; re-check the traceback, reproduction path, and test fixture.")
    return "\n".join(lines)


def _format_adversarial_prompt_feedback(
    result: AdversarialValidationResult,
    *,
    failed_results: list[ProbeResult],
    diagnosis: FaultDiagnosis | None,
) -> str:
    lines: list[str] = []
    lines.append("Patch passed the earlier repair pipeline stage but failed adversarial validation.")
    lines.append(f"Adversarial summary: {result.summary or result.status}")

    _append_diagnosis_context(lines, diagnosis)

    if failed_results:
        lines.append("")
        lines.append("Failed adversarial probes:")
        for probe_result in failed_results:
            case = probe_result.case
            attempt = _first_failed_attempt(probe_result)
            lines.extend(_format_failed_probe(case, attempt))
    else:
        lines.append("")
        lines.append("No individual probe details were available; inspect the adversarial validation summary.")

    lines.append("")
    lines.append("Please revise the patch so that all failed probes pass while preserving the previous repair constraints.")
    if any(probe_result.case.name == "bug_shortcut" for probe_result in failed_results):
        lines.append("The revised patch must cover all callers of the crashed helper, including GET /bug.")
    if any(probe_result.case.name in {"zero_boundary", "repeated_zero"} for probe_result in failed_results):
        lines.append("The revised patch must convert zero denominator input into a controlled 400/422 response, not a 500.")
    lines.append("Do not use NaN, Infinity, -Infinity, float('nan'), float('inf'), math.nan, or math.inf as the repair.")
    lines.append("Keep the known happy path intact, especially GET /divide?x=4 returning result 25.0.")

    return "\n".join(lines)


def _append_diagnosis_context(lines: list[str], diagnosis: FaultDiagnosis | None) -> None:
    if diagnosis is None:
        return

    classification = diagnosis.classification
    lines.append("")
    lines.append("Deterministic diagnosis context:")
    lines.append(f"- exception: {classification.exception_type}")
    lines.append(f"- category: {classification.category}")
    if diagnosis.suspected_variables:
        lines.append(f"- suspected variables: {', '.join(diagnosis.suspected_variables)}")
    if diagnosis.trigger_conditions:
        lines.append(f"- trigger conditions: {', '.join(diagnosis.trigger_conditions)}")
    if diagnosis.primary_candidate is not None:
        candidate = diagnosis.primary_candidate
        lines.append(f"- primary candidate: {candidate.file}:{candidate.line_number} {candidate.code.strip()}")


def _append_command_failure(lines: list[str], command: ValidationCommandResult) -> None:
    lines.append(f"- command: {_command_text(command.command)}")
    lines.append(f"  return_code: {command.return_code}")
    lines.append(f"  duration_seconds: {command.duration_seconds:.2f}")
    _append_output_excerpt(lines, "  stdout", command.stdout)
    _append_output_excerpt(lines, "  stderr", command.stderr)


def _append_output_excerpt(lines: list[str], label: str, value: str) -> None:
    if value.strip():
        lines.append(f"{label} excerpt: {_excerpt(value)}")


def _first_failed_attempt(probe_result: ProbeResult) -> ProbeAttemptResult | None:
    for attempt in probe_result.attempts:
        if not attempt.passed:
            return attempt
    return probe_result.attempts[0] if probe_result.attempts else None


def _format_failed_probe(case: ProbeCase, attempt: ProbeAttemptResult | None) -> list[str]:
    lines = [
        f"- probe: {case.name}",
        f"  request: {case.method.upper()} {case.path}",
    ]

    expected_parts: list[str] = []
    if case.expected_statuses:
        expected_parts.append(f"status in {sorted(case.expected_statuses)}")
    if case.forbidden_statuses:
        expected_parts.append(f"forbid status {sorted(case.forbidden_statuses)}")
    if case.expected_json:
        expected_parts.append(f"expected_json={case.expected_json}")
    if case.forbidden_json:
        expected_parts.append(f"forbidden_json={_stringify_forbidden_json(case.forbidden_json)}")
    lines.append(f"  expected: {'; '.join(expected_parts) if expected_parts else 'no explicit expectation'}")

    if attempt is None:
        lines.append("  actual: no attempt result was recorded")
        return lines

    actual_body = attempt.response_json if attempt.response_json is not None else "<non-json response>"
    lines.append(f"  actual: status {attempt.status_code}, body {actual_body}")
    lines.append(f"  failure_reason: {attempt.reason}")
    return lines


def _counterfactual_failed_conditions(result: CounterfactualValidationResult) -> list[str]:
    failed_conditions: list[str] = []
    before = result.before_patch_result
    after = result.after_patch_result

    if before is None:
        failed_conditions.append("before_patch_result_missing")
    else:
        before_ok = (not before.passed) if result.expected_before_fail else before.passed
        if not before_ok:
            failed_conditions.append(
                "before_patch_expected_fail" if result.expected_before_fail else "before_patch_expected_pass"
            )

    if after is None:
        failed_conditions.append("after_patch_result_missing")
    else:
        after_ok = after.passed if result.expected_after_pass else not after.passed
        if not after_ok:
            failed_conditions.append(
                "after_patch_expected_pass" if result.expected_after_pass else "after_patch_expected_fail"
            )

    return failed_conditions


def _finding_details(finding: PatchSafetyFinding) -> dict[str, Any]:
    return {
        "check": finding.check,
        "status": finding.status,
        "message": finding.message,
        "evidence": [_excerpt(str(item), limit=240) for item in finding.evidence],
    }


def _command_result_details(command: ValidationCommandResult) -> dict[str, Any]:
    return {
        "command": list(command.command),
        "return_code": command.return_code,
        "duration_seconds": command.duration_seconds,
        "stdout_excerpt": _excerpt(command.stdout),
        "stderr_excerpt": _excerpt(command.stderr),
    }


def _contract_baseline_details(baseline: ContractCommandBaseline) -> dict[str, Any]:
    return {
        "command": list(baseline.command),
        "before_passed": baseline.before_passed,
        "after_passed": baseline.after_passed,
        "before_return_code": baseline.before_return_code,
        "after_return_code": baseline.after_return_code,
        "regression": baseline.regression,
        "stdout_before_excerpt": _excerpt(baseline.stdout_before),
        "stderr_before_excerpt": _excerpt(baseline.stderr_before),
        "stdout_after_excerpt": _excerpt(baseline.stdout_after),
        "stderr_after_excerpt": _excerpt(baseline.stderr_after),
    }


def _probe_result_details(probe_result: ProbeResult) -> dict[str, Any]:
    attempt = _first_failed_attempt(probe_result)
    return {
        "name": probe_result.case.name,
        "method": probe_result.case.method,
        "path": probe_result.case.path,
        "passed": probe_result.passed,
        "first_failed_attempt": {
            "status_code": attempt.status_code,
            "response_json": attempt.response_json,
            "passed": attempt.passed,
            "reason": attempt.reason,
        }
        if attempt is not None
        else None,
    }


def _command_text(command: list[str]) -> str:
    return " ".join(str(part) for part in command) if command else "<missing command>"


def _stringify_list_excerpt(values: list[Any], *, limit: int = 240) -> str:
    return ", ".join(_excerpt(str(value), limit=limit) for value in values)


def _excerpt(text: str, *, limit: int = EXCERPT_LIMIT) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _stringify_forbidden_json(forbidden_json: dict[str, set[Any]]) -> dict[str, list[Any]]:
    return {
        key: sorted(values, key=lambda item: str(item))
        for key, values in forbidden_json.items()
    }
