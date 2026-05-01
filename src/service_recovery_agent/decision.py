"""Decision engine for recovery results.

The recovery pipeline already produces structured evidence: fix proposal,
patch safety review, dry-run/apply results, ordinary validation, adversarial
validation, rollback state, and repair-attempt history.  This module turns that
raw evidence into a conservative delivery decision.

It intentionally does **not** create commits, push branches, open PRs, or send
notifications.  It only answers: given the evidence, is the repair ready for a
future delivery step, should it be reported only, retried, blocked, or escalated
to a human?
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DecisionAction(str, Enum):
    """High-level action recommended by the decision engine."""

    CREATE_PR_READY = "CREATE_PR_READY"
    REPORT_ONLY = "REPORT_ONLY"
    RETRY_REPAIR = "RETRY_REPAIR"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class DecisionReason:
    """A human-readable, machine-testable reason for a decision."""

    code: str
    severity: str
    message: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionResult:
    """Structured delivery decision for a recovery run."""

    action: DecisionAction
    confidence_score: float
    risk_level: str
    can_auto_deliver: bool
    reasons: list[DecisionReason]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "confidence_score": self.confidence_score,
            "risk_level": self.risk_level,
            "can_auto_deliver": self.can_auto_deliver,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "summary": self.summary,
        }


ERROR = "error"
WARNING = "warning"
INFO = "info"


LOW_CONFIDENCE_THRESHOLD = 0.50
CREATE_PR_READY_THRESHOLD = 0.80
VALID_CHANGE_TYPES = {"A", "B", "C", "D", "E"}
HIGH_RISK_LEVELS = {"high", "critical"}
LOW_CONFIDENCE_LEVELS = {"low"}


def evaluate_recovery_decision(
    result: Any,
    *,
    allow_auto_delivery: bool = False,
) -> DecisionResult:
    """Evaluate whether a ``RecoveryRunResult`` is ready for delivery.

    ``allow_auto_delivery`` only controls the boolean ``can_auto_deliver`` in
    the returned result.  The action ``CREATE_PR_READY`` means the quality gates
    are ready for a future Git/PR layer; it does not perform delivery itself.
    """

    reasons: list[DecisionReason] = []
    proposal = getattr(result, "proposal", None)
    preview = getattr(result, "preview", None)
    validation_result = getattr(result, "validation_result", None)
    contract_result = getattr(result, "contract_result", None)
    adversarial_result = getattr(result, "adversarial_result", None)
    counterfactual_result = getattr(result, "counterfactual_result", None)

    safety_status = _safety_status(preview)
    validation_passed = _passed_or_none(validation_result)
    contract_passed = _passed_or_none(contract_result)
    adversarial_passed = _passed_or_none(adversarial_result)
    counterfactual_passed = _passed_or_none(counterfactual_result)
    retry_candidate = _can_retry_from_feedback(result)

    status = str(getattr(result, "status", "") or "UNKNOWN")
    rolled_back = bool(getattr(result, "rolled_back", False))

    hard_block = False
    report_only = False
    escalate_human = False
    retry_repair = False

    if status == "PASS":
        reasons.append(
            DecisionReason(
                code="status_pass",
                severity=INFO,
                message="Recovery run status is PASS.",
            )
        )
    elif retry_candidate:
        retry_repair = True
        reasons.append(
            DecisionReason(
                code="retry_available",
                severity=WARNING,
                message=(
                    "Recovery run failed but produced repair feedback and still has remaining "
                    "attempt budget. A revised repair attempt is recommended."
                ),
                evidence=[_attempt_budget(result)],
            )
        )
    elif status == "PREVIEW_ONLY":
        report_only = True
        reasons.append(
            DecisionReason(
                code="preview_only",
                severity=WARNING,
                message="Patch was only previewed and has not been applied or validated as a repair.",
            )
        )
    elif status == "NO_TRACEBACK":
        hard_block = True
        reasons.append(
            DecisionReason(
                code="no_traceback",
                severity=ERROR,
                message="No traceback was found, so no repair can be delivered.",
            )
        )
    else:
        hard_block = True
        reasons.append(
            DecisionReason(
                code="status_not_pass",
                severity=ERROR,
                message=f"Recovery run status is {status}, not PASS.",
            )
        )

    if rolled_back and not retry_candidate:
        hard_block = True
        reasons.append(
            DecisionReason(
                code="rolled_back",
                severity=ERROR,
                message="Recovery run rolled back the applied patch; rolled-back repairs cannot be delivered.",
            )
        )

    apply_result = getattr(result, "apply_result", None)
    if apply_result is not None and not bool(getattr(apply_result, "applied", False)):
        hard_block = True
        reasons.append(
            DecisionReason(
                code="apply_failed",
                severity=ERROR,
                message="Patch apply result indicates that the patch was not applied.",
            )
        )

    if safety_status == "PASS":
        reasons.append(
            DecisionReason(
                code="safety_pass",
                severity=INFO,
                message="Patch safety review passed.",
            )
        )
    elif safety_status == "WARN":
        report_only = True
        reasons.append(
            DecisionReason(
                code="safety_warn",
                severity=WARNING,
                message="Patch safety review emitted warnings; keep this repair in report-only mode unless manually approved.",
                evidence=_safety_finding_summaries(preview),
            )
        )
    elif safety_status == "FAIL":
        hard_block = True
        reasons.append(
            DecisionReason(
                code="safety_failed",
                severity=ERROR,
                message="Patch safety review failed.",
                evidence=_safety_finding_summaries(preview),
            )
        )
    else:
        report_only = True
        reasons.append(
            DecisionReason(
                code="safety_missing",
                severity=WARNING,
                message="Patch safety review evidence is missing.",
            )
        )

    if validation_passed is True:
        reasons.append(
            DecisionReason(
                code="validation_pass",
                severity=INFO,
                message="Ordinary validation passed.",
            )
        )
    elif validation_passed is False:
        hard_block = True
        reasons.append(
            DecisionReason(
                code="validation_failed",
                severity=ERROR,
                message="Ordinary validation failed.",
                evidence=_validation_failure_summaries(validation_result),
            )
        )
    else:
        report_only = True
        reasons.append(
            DecisionReason(
                code="validation_missing",
                severity=WARNING,
                message="Ordinary validation was not run or is missing; do not auto-deliver without validation evidence.",
            )
        )

    if contract_passed is True:
        reasons.append(
            DecisionReason(
                code="contract_pass",
                severity=INFO,
                message="Contract validation passed; no command-level regression was detected.",
            )
        )
    elif contract_passed is False:
        hard_block = True
        reasons.append(
            DecisionReason(
                code="contract_failed",
                severity=ERROR,
                message="Contract validation failed; a previously passing command regressed or contract evidence is incomplete.",
            )
        )

    if adversarial_passed is True:
        reasons.append(
            DecisionReason(
                code="adversarial_pass",
                severity=INFO,
                message="Adversarial validation passed.",
            )
        )
    elif adversarial_passed is False:
        if retry_candidate:
            retry_repair = True
            reasons.append(
                DecisionReason(
                    code="adversarial_failed_retryable",
                    severity=WARNING,
                    message="Adversarial validation failed, but repair feedback can be used for another attempt.",
                    evidence=_adversarial_failure_summaries(adversarial_result),
                )
            )
        else:
            hard_block = True
            reasons.append(
                DecisionReason(
                    code="adversarial_failed",
                    severity=ERROR,
                    message="Adversarial validation failed.",
                    evidence=_adversarial_failure_summaries(adversarial_result),
                )
            )
    else:
        report_only = True
        reasons.append(
            DecisionReason(
                code="adversarial_missing",
                severity=WARNING,
                message="Adversarial validation evidence is missing; keep this repair in report-only mode.",
            )
        )

    if counterfactual_passed is True:
        reasons.append(
            DecisionReason(
                code="counterfactual_pass",
                severity=INFO,
                message="Counterfactual validation passed: the buggy state failed and the patched state passed.",
            )
        )
    elif counterfactual_passed is False:
        hard_block = True
        reasons.append(
            DecisionReason(
                code="counterfactual_failed",
                severity=ERROR,
                message="Counterfactual validation failed; before/after evidence does not prove the repair.",
            )
        )

    policy_reasons = evaluate_change_type_policy(
        proposal,
        safety_status=safety_status,
        validation_passed=validation_passed,
        adversarial_passed=adversarial_passed,
    )
    reasons.extend(policy_reasons)

    if any(reason.code in {"change_type_d", "change_type_e"} for reason in policy_reasons):
        escalate_human = True
    if any(reason.severity == ERROR for reason in policy_reasons):
        hard_block = True
    if any(reason.severity == WARNING for reason in policy_reasons):
        report_only = True

    proposal_confidence = _normalized(getattr(proposal, "confidence", "")) if proposal is not None else ""
    if proposal_confidence in LOW_CONFIDENCE_LEVELS:
        report_only = True
        reasons.append(
            DecisionReason(
                code="proposal_low_confidence",
                severity=WARNING,
                message="FixProposal confidence is low.",
                evidence=[f"confidence={proposal_confidence}"],
            )
        )

    risk_level = _combined_risk_level(result)
    if risk_level in HIGH_RISK_LEVELS:
        report_only = True
        reasons.append(
            DecisionReason(
                code="high_risk",
                severity=WARNING,
                message="Risk level is high; prefer report-only or human review.",
                evidence=[f"risk_level={risk_level}"],
            )
        )

    if int(getattr(result, "attempt_number", 0) or 0) > 1:
        reasons.append(
            DecisionReason(
                code="repaired_after_retry",
                severity=INFO,
                message="Repair passed after one or more revised attempts; confidence is slightly reduced but not blocked.",
                evidence=[_attempt_budget(result)],
            )
        )

    confidence_score = _score_result(result, safety_status=safety_status)

    if retry_repair:
        action = DecisionAction.RETRY_REPAIR
    elif escalate_human:
        action = DecisionAction.ESCALATE_HUMAN
    elif hard_block:
        action = DecisionAction.BLOCKED
    elif report_only or confidence_score < CREATE_PR_READY_THRESHOLD:
        action = DecisionAction.REPORT_ONLY if confidence_score >= LOW_CONFIDENCE_THRESHOLD else DecisionAction.ESCALATE_HUMAN
    else:
        action = DecisionAction.CREATE_PR_READY

    can_auto_deliver = bool(allow_auto_delivery and action is DecisionAction.CREATE_PR_READY)
    summary = _summary_for_action(action, confidence_score, risk_level, can_auto_deliver)
    return DecisionResult(
        action=action,
        confidence_score=confidence_score,
        risk_level=risk_level,
        can_auto_deliver=can_auto_deliver,
        reasons=reasons,
        summary=summary,
    )


def evaluate_change_type_policy(
    proposal: Any,
    *,
    safety_status: str | None,
    validation_passed: bool | None,
    adversarial_passed: bool | None,
) -> list[DecisionReason]:
    """Evaluate A/B/C/D/E change-type policy from the fix proposal."""

    if proposal is None:
        return [
            DecisionReason(
                code="proposal_missing",
                severity=WARNING,
                message="FixProposal evidence is missing, so change-type policy cannot be fully evaluated.",
            )
        ]

    change_type = _change_type(proposal)
    if not change_type:
        return [
            DecisionReason(
                code="change_type_missing",
                severity=WARNING,
                message="FixProposal did not provide a change_type.",
            )
        ]

    if change_type not in VALID_CHANGE_TYPES:
        return [
            DecisionReason(
                code="change_type_unknown",
                severity=WARNING,
                message=f"FixProposal change_type={change_type!r} is not one of A/B/C/D/E.",
            )
        ]

    if change_type == "A":
        return [
            DecisionReason(
                code="change_type_a",
                severity=INFO,
                message="Change type A is a pure incremental guard and is eligible for delivery if all gates pass.",
            )
        ]

    if change_type == "B":
        return [
            DecisionReason(
                code="change_type_b",
                severity=INFO,
                message="Change type B is an internal logic fix and requires validation and adversarial gates to pass.",
            )
        ]

    if change_type == "C":
        severity = WARNING
        message = (
            "Change type C is a signature-compatible refactor. It should remain report-only until "
            "contract/counterfactual validation is available and passing."
        )
        evidence: list[str] = []
        if safety_status:
            evidence.append(f"safety={safety_status}")
        if validation_passed is not None:
            evidence.append(f"validation_passed={validation_passed}")
        if adversarial_passed is not None:
            evidence.append(f"adversarial_passed={adversarial_passed}")
        return [DecisionReason(code="change_type_c_requires_contract", severity=severity, message=message, evidence=evidence)]

    if change_type == "D":
        return [
            DecisionReason(
                code="change_type_d",
                severity=ERROR,
                message="Change type D changes interface/contract semantics and must be escalated to a human.",
            )
        ]

    return [
        DecisionReason(
            code="change_type_e",
            severity=ERROR,
            message="Change type E is a broad cross-file change and must be escalated to a human.",
        )
    ]


def format_decision_result(result: DecisionResult) -> str:
    """Render a human-readable decision report."""

    lines: list[str] = []
    lines.append("# Decision Result")
    lines.append("")
    lines.append(f"- Action: **{result.action.value}**")
    lines.append(f"- Summary: {result.summary}")
    lines.append(f"- Confidence score: {result.confidence_score:.2f}")
    lines.append(f"- Risk level: {result.risk_level}")
    lines.append(f"- Can auto deliver: {result.can_auto_deliver}")
    lines.append("")
    lines.append("## Reasons")
    if not result.reasons:
        lines.append("- No decision reasons were recorded.")
    else:
        for reason in result.reasons:
            lines.append(f"- **{reason.severity.upper()}** `{reason.code}`: {reason.message}")
            for item in reason.evidence:
                lines.append(f"  - {item}")
    return "\n".join(lines).rstrip()


def _safety_status(preview: Any) -> str | None:
    review = getattr(preview, "safety_review", None)
    status = getattr(review, "status", None)
    return str(status).upper() if status else None


def _passed_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    passed = getattr(value, "passed", None)
    if passed is None:
        return None
    return bool(passed)


def _change_type(proposal: Any) -> str:
    return str(getattr(proposal, "change_type", "") or "").strip().upper()


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _combined_risk_level(result: Any) -> str:
    risk_values: list[str] = []
    proposal = getattr(result, "proposal", None)
    if proposal is not None:
        risk_values.append(_normalized(getattr(proposal, "risk_level", "")))
    context = getattr(result, "context", None)
    impact = getattr(context, "impact", None)
    if impact is not None:
        risk_values.append(_normalized(getattr(impact, "risk_level", "")))

    if any(value in HIGH_RISK_LEVELS for value in risk_values):
        return "high"
    if any(value == "medium" for value in risk_values):
        return "medium"
    if any(value == "low" for value in risk_values):
        return "low"
    return "unknown"


def _can_retry_from_feedback(result: Any) -> bool:
    return (
        getattr(result, "failure_feedback", None) is not None
        and bool(getattr(result, "rolled_back", False))
        and int(getattr(result, "attempt_number", 0) or 0) < int(getattr(result, "max_repair_attempts", 1) or 1)
    )


def _attempt_budget(result: Any) -> str:
    return f"attempt={getattr(result, 'attempt_number', 0)}/{getattr(result, 'max_repair_attempts', 1)}"


def _score_result(result: Any, *, safety_status: str | None) -> float:
    score = 0.50

    if getattr(result, "status", None) == "PASS":
        score += 0.05
    if not bool(getattr(result, "rolled_back", False)):
        score += 0.05

    validation_result = getattr(result, "validation_result", None)
    if _passed_or_none(validation_result) is True:
        score += 0.15
    elif _passed_or_none(validation_result) is False:
        score -= 0.30
    else:
        score -= 0.10

    contract_result = getattr(result, "contract_result", None)
    if _passed_or_none(contract_result) is True:
        score += 0.05
    elif _passed_or_none(contract_result) is False:
        score -= 0.30

    adversarial_result = getattr(result, "adversarial_result", None)
    if _passed_or_none(adversarial_result) is True:
        score += 0.15
    elif _passed_or_none(adversarial_result) is False:
        score -= 0.30
    else:
        score -= 0.10

    counterfactual_result = getattr(result, "counterfactual_result", None)
    if _passed_or_none(counterfactual_result) is True:
        score += 0.05
    elif _passed_or_none(counterfactual_result) is False:
        score -= 0.30

    if safety_status == "PASS":
        score += 0.10
    elif safety_status == "WARN":
        score -= 0.10
    elif safety_status == "FAIL":
        score -= 0.30
    else:
        score -= 0.10

    proposal = getattr(result, "proposal", None)
    if proposal is not None:
        confidence = _normalized(getattr(proposal, "confidence", ""))
        if confidence == "high":
            score += 0.05
        elif confidence == "low":
            score -= 0.20

        change_type = _change_type(proposal)
        if change_type == "C":
            score -= 0.15
        elif change_type in {"D", "E"}:
            score -= 0.30
    else:
        score -= 0.10

    diagnosis = getattr(result, "diagnosis", None)
    classification = getattr(diagnosis, "classification", None)
    diagnosis_confidence = _normalized(getattr(classification, "confidence", ""))
    if diagnosis_confidence == "high":
        score += 0.05
    elif diagnosis_confidence == "low":
        score -= 0.05

    risk_level = _combined_risk_level(result)
    if risk_level == "high":
        score -= 0.20
    elif risk_level == "medium":
        score -= 0.05

    if int(getattr(result, "attempt_number", 0) or 0) > 1:
        score -= 0.10

    return round(max(0.0, min(1.0, score)), 2)


def _summary_for_action(
    action: DecisionAction,
    confidence_score: float,
    risk_level: str,
    can_auto_deliver: bool,
) -> str:
    if action is DecisionAction.CREATE_PR_READY:
        if can_auto_deliver:
            return (
                f"Repair passed required gates with confidence score {confidence_score:.2f} "
                f"and risk level {risk_level}; auto-delivery is allowed by configuration."
            )
        return (
            f"Repair passed required gates with confidence score {confidence_score:.2f} "
            f"and risk level {risk_level}; it is ready for a future Git/PR delivery step."
        )
    if action is DecisionAction.REPORT_ONLY:
        return (
            f"Repair evidence is useful but not strong enough for automatic delivery "
            f"(score {confidence_score:.2f}, risk {risk_level}); report only."
        )
    if action is DecisionAction.RETRY_REPAIR:
        return "Repair should be revised using the generated failure feedback before delivery."
    if action is DecisionAction.ESCALATE_HUMAN:
        return (
            f"Repair requires human review or intervention "
            f"(score {confidence_score:.2f}, risk {risk_level})."
        )
    return "Repair is blocked by failed or missing safety/validation evidence."


def _safety_finding_summaries(preview: Any) -> list[str]:
    review = getattr(preview, "safety_review", None)
    findings = getattr(review, "findings", []) if review is not None else []
    evidence: list[str] = []
    for finding in findings:
        status = getattr(finding, "status", "")
        if status not in {"WARN", "FAIL"}:
            continue
        check = getattr(finding, "check", "unknown")
        message = getattr(finding, "message", "")
        evidence.append(f"{status} {check}: {message}")
        for item in getattr(finding, "evidence", [])[:3]:
            evidence.append(f"evidence: {item}")
    return evidence[:8]


def _validation_failure_summaries(validation_result: Any) -> list[str]:
    command_results = getattr(validation_result, "command_results", []) if validation_result is not None else []
    evidence: list[str] = []
    for command_result in command_results:
        if getattr(command_result, "passed", False):
            continue
        command = " ".join(getattr(command_result, "command", []) or [])
        return_code = getattr(command_result, "return_code", "unknown")
        evidence.append(f"command `{command}` failed with return code {return_code}")
        stderr = str(getattr(command_result, "stderr", "") or "").strip()
        stdout = str(getattr(command_result, "stdout", "") or "").strip()
        excerpt = stderr or stdout
        if excerpt:
            evidence.append(_truncate(excerpt, 240))
    return evidence[:8]


def _adversarial_failure_summaries(adversarial_result: Any) -> list[str]:
    probe_results = getattr(adversarial_result, "probe_results", []) if adversarial_result is not None else []
    evidence: list[str] = []
    for probe_result in probe_results:
        if getattr(probe_result, "passed", False):
            continue
        case = getattr(probe_result, "case", None)
        name = getattr(case, "name", "unknown")
        method = getattr(case, "method", "GET")
        path = getattr(case, "path", "")
        evidence.append(f"probe {name}: {method} {path}")
        attempts = getattr(probe_result, "attempts", [])
        for attempt in attempts[:1]:
            evidence.append(
                f"status={getattr(attempt, 'status_code', 'unknown')}, reason={getattr(attempt, 'reason', '')}"
            )
    return evidence[:8]


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."
