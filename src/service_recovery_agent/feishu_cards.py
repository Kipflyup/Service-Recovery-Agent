"""Dry-run Feishu card builders for recovery results.

This module is intentionally deterministic and network-free.  It converts a
``RecoveryRunResult`` plus an optional ``DecisionResult`` into Feishu
interactive-card JSON that can be printed, tested, or later passed to
``FeishuClient.send_interactive_card`` by an explicit caller.

It does **not** read ``.env``, request Feishu tokens, send messages, create Git
branches, push commits, or open PRs.  The first version only builds three card
families:

- success / review-ready;
- report-only / low-confidence analysis;
- failure / degraded human-escalation notification.
"""

from __future__ import annotations

from typing import Any, Mapping

from .decision import DecisionAction, DecisionResult


DEFAULT_SERVICE_NAME = "Service Recovery Agent"
TEXT_LIMIT = 900
FIELD_LIMIT = 260
MAX_FAILED_ITEMS = 6
MAX_REASON_ITEMS = 5


def build_recovery_result_card(
    result: Any,
    *,
    decision: DecisionResult | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    pr_url: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    rollback_command: str | None = None,
) -> dict[str, Any]:
    """Build the most appropriate dry-run Feishu card for a recovery run.

    Selection is conservative:

    - ``success`` when the run passed and the decision is ``CREATE_PR_READY``
      or no blocking decision is present;
    - ``report-only`` when the decision explicitly says ``REPORT_ONLY`` or the
      run is preview-only;
    - ``failure`` for blocked, retry, escalated, rolled-back, or failed runs.
    """

    kind = classify_recovery_card(result, decision=decision)
    if kind == "success":
        return build_success_recovery_card(
            result,
            decision=decision,
            service_name=service_name,
            pr_url=pr_url,
            repo=repo,
            branch=branch,
            rollback_command=rollback_command,
        )
    if kind == "report_only":
        return build_report_only_recovery_card(
            result,
            decision=decision,
            service_name=service_name,
            repo=repo,
            branch=branch,
        )
    return build_failure_recovery_card(
        result,
        decision=decision,
        service_name=service_name,
        repo=repo,
        branch=branch,
        rollback_command=rollback_command,
    )


def classify_recovery_card(result: Any, *, decision: DecisionResult | None = None) -> str:
    """Return ``success``, ``report_only``, or ``failure`` for card routing."""

    action = _decision_action(decision)
    status = _status(result)
    rolled_back = bool(getattr(result, "rolled_back", False))

    if action == DecisionAction.REPORT_ONLY.value:
        return "report_only"
    if status == "PREVIEW_ONLY":
        return "report_only"
    if action in {
        DecisionAction.BLOCKED.value,
        DecisionAction.RETRY_REPAIR.value,
        DecisionAction.ESCALATE_HUMAN.value,
    }:
        return "failure"
    if status == "PASS" and not rolled_back:
        return "success"
    return "failure"


def build_success_recovery_card(
    result: Any,
    *,
    decision: DecisionResult | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    pr_url: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    rollback_command: str | None = None,
) -> dict[str, Any]:
    """Build a success / review-ready card."""

    title = "Service Recovery Agent：修复已通过门禁"
    summary = _join_non_empty(
        [
            "**我发现了一个 Bug，并已生成通过当前质量门禁的修复。**",
            _main_summary(result, decision),
            _diagnosis_summary(result),
        ],
        sep="\n\n",
    )

    elements = [
        _markdown(summary),
        _fields_block(
            [
                _field("卡片类型", "success"),
                _field("服务", service_name),
                _field("运行状态", _status(result)),
                _field("Decision", _decision_action(decision) or "未提供"),
                _field("can_create_pr", str(bool(getattr(result, "can_create_pr", False)))),
                _field("rolled_back", str(bool(getattr(result, "rolled_back", False)))),
                _field("Attempts", _attempt_text(result)),
                _field("风险等级", _risk_text(result, decision)),
                _field("置信度", _confidence_text(result, decision)),
            ]
        ),
        _gates_block(result),
    ]

    proposal_summary = _proposal_summary(result)
    if proposal_summary:
        elements.extend([_hr(), _markdown(proposal_summary)])

    if repo or branch:
        elements.append(
            _fields_block(
                [
                    _field("仓库", repo or "未提供"),
                    _field("分支", branch or "未创建"),
                ]
            )
        )

    if rollback_command:
        elements.extend([_hr(), _markdown(f"**回滚方案：**\n`{_truncate(rollback_command, FIELD_LIMIT)}`")])

    if pr_url:
        elements.extend([_hr(), _pr_action(pr_url)])
    else:
        elements.extend([_hr(), _markdown("**下一步建议：**\n创建本地修复分支 / commit，并由人工确认后再 push 或创建 PR。")])

    return _card(title=title, template="green", elements=elements)


def build_report_only_recovery_card(
    result: Any,
    *,
    decision: DecisionResult | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    repo: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Build a report-only / low-confidence card."""

    title = "Service Recovery Agent：发现问题，当前仅建议人工 Review"
    summary = _join_non_empty(
        [
            "**发现服务异常或修复建议，但当前证据不足以自动交付。**",
            _main_summary(result, decision),
            _diagnosis_summary(result),
        ],
        sep="\n\n",
    )

    elements = [
        _markdown(summary),
        _fields_block(
            [
                _field("卡片类型", "report_only"),
                _field("服务", service_name),
                _field("运行状态", _status(result)),
                _field("Decision", _decision_action(decision) or "未提供"),
                _field("can_auto_deliver", _decision_auto_deliver_text(decision)),
                _field("can_create_pr", str(bool(getattr(result, "can_create_pr", False)))),
                _field("Attempts", _attempt_text(result)),
                _field("风险等级", _risk_text(result, decision)),
                _field("置信度", _confidence_text(result, decision)),
            ]
        ),
        _gates_block(result),
    ]

    proposal_summary = _proposal_summary(result)
    if proposal_summary:
        elements.extend([_hr(), _markdown(proposal_summary)])

    decision_reasons = _decision_reason_summary(decision)
    if decision_reasons:
        elements.extend([_hr(), _markdown(decision_reasons)])

    if repo or branch:
        elements.append(
            _fields_block(
                [
                    _field("仓库", repo or "未提供"),
                    _field("分支", branch or "未创建"),
                ]
            )
        )

    elements.extend(
        [
            _hr(),
            _markdown(
                "**下一步建议：**\n"
                "人工检查诊断、patch 草案和验证证据；如需继续自动修复，请增加验证证据或提高门禁通过度。"
            ),
        ]
    )
    return _card(title=title, template="yellow", elements=elements)


def build_failure_recovery_card(
    result: Any,
    *,
    decision: DecisionResult | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    repo: str | None = None,
    branch: str | None = None,
    rollback_command: str | None = None,
) -> dict[str, Any]:
    """Build a failure / degraded human-escalation card."""

    feedback = getattr(result, "failure_feedback", None)
    failed_stage = _feedback_stage(feedback)
    failed_items = _feedback_failed_items(feedback)
    failure_summary = _feedback_summary(feedback) or getattr(result, "reason", "") or "修复尝试失败。"
    next_step = _failure_next_step(result, decision=decision, failed_stage=failed_stage)

    elements = [
        _markdown(
            _join_non_empty(
                [
                    "**修复尝试失败或被门禁阻断，已降级为人工处理。**",
                    _main_summary(result, decision),
                    _diagnosis_summary(result),
                ],
                sep="\n\n",
            )
        ),
        _fields_block(
            [
                _field("卡片类型", "failure"),
                _field("服务", service_name),
                _field("运行状态", _status(result)),
                _field("Decision", _decision_action(decision) or "未提供"),
                _field("failed_stage", failed_stage or "未知"),
                _field("rolled_back", str(bool(getattr(result, "rolled_back", False)))),
                _field("Attempts", _attempt_text(result)),
                _field("can_create_pr", str(bool(getattr(result, "can_create_pr", False)))),
            ]
        ),
        _fields_block(
            [
                _field("failed_probes", _failed_items_text(failed_items)),
                _field("summary", failure_summary),
                _field("下一步建议", next_step),
            ]
        ),
        _gates_block(result),
    ]

    feedback_details = _failure_feedback_details(feedback)
    if feedback_details:
        elements.extend([_hr(), _markdown(feedback_details)])

    decision_reasons = _decision_reason_summary(decision)
    if decision_reasons:
        elements.extend([_hr(), _markdown(decision_reasons)])

    if rollback_command:
        elements.extend([_hr(), _markdown(f"**回滚方案：**\n`{_truncate(rollback_command, FIELD_LIMIT)}`")])

    if repo or branch:
        elements.append(
            _fields_block(
                [
                    _field("仓库", repo or "未提供"),
                    _field("分支", branch or "未创建"),
                ]
            )
        )

    return _card(title="Service Recovery Agent：修复失败 / 已降级", template="red", elements=elements)


def _card(*, title: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": title,
            },
        },
        "elements": elements,
    }


def _markdown(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": _truncate(content, TEXT_LIMIT),
        },
    }


def _fields_block(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": "div", "fields": fields}


def _field(label: str, value: Any) -> dict[str, Any]:
    return {
        "is_short": True,
        "text": {
            "tag": "lark_md",
            "content": f"**{label}：**\n{_truncate(_stringify(value), FIELD_LIMIT)}",
        },
    }


def _hr() -> dict[str, Any]:
    return {"tag": "hr"}


def _pr_action(pr_url: str) -> dict[str, Any]:
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看 PR"},
                "type": "primary",
                "url": pr_url,
            }
        ],
    }


def _main_summary(result: Any, decision: DecisionResult | None) -> str:
    lines: list[str] = []
    reason = getattr(result, "reason", "")
    if reason:
        lines.append(f"**运行摘要：** {_truncate(reason, FIELD_LIMIT)}")
    if decision is not None:
        lines.append(f"**决策摘要：** {_truncate(decision.summary, FIELD_LIMIT)}")
    return "\n".join(lines)


def _diagnosis_summary(result: Any) -> str:
    diagnosis = getattr(result, "diagnosis", None)
    if diagnosis is None:
        traceback_event = getattr(result, "traceback_event", None)
        exception_type = getattr(traceback_event, "exception_type", None)
        if exception_type:
            return f"**异常类型：** {exception_type}"
        return ""

    classification = getattr(diagnosis, "classification", None)
    exception_type = getattr(classification, "exception_type", "") if classification is not None else ""
    category = getattr(classification, "category", "") if classification is not None else ""
    candidate = getattr(diagnosis, "primary_candidate", None)
    location = ""
    if candidate is not None:
        location = f"{getattr(candidate, 'file', '')}:{getattr(candidate, 'line_number', '')}"
    parts = [
        f"exception={exception_type}" if exception_type else "",
        f"category={category}" if category else "",
        f"location={location}" if location.strip(":") else "",
    ]
    compact = ", ".join(part for part in parts if part)
    return f"**故障诊断：** {compact}" if compact else ""


def _proposal_summary(result: Any) -> str:
    proposal = getattr(result, "proposal", None)
    if proposal is None:
        return ""
    root_cause = getattr(proposal, "root_cause", "")
    strategy = getattr(proposal, "fix_strategy", "")
    change_type = getattr(proposal, "change_type", "")
    parts = ["**修复建议摘要：**"]
    if root_cause:
        parts.append(f"- Root cause: {_truncate(root_cause, FIELD_LIMIT)}")
    if strategy:
        parts.append(f"- Strategy: {_truncate(strategy, FIELD_LIMIT)}")
    if change_type:
        parts.append(f"- Change type: {change_type}")
    return "\n".join(parts) if len(parts) > 1 else ""


def _gates_block(result: Any) -> dict[str, Any]:
    preview = getattr(result, "preview", None)
    safety = getattr(getattr(preview, "safety_review", None), "status", None)
    dry_run = getattr(preview, "dry_run", None)
    dry_run_text = "未运行" if dry_run is None else str(bool(getattr(dry_run, "can_apply", False)))

    apply_result = getattr(result, "apply_result", None)
    apply_text = "未执行" if apply_result is None else str(bool(getattr(apply_result, "applied", False)))

    return _fields_block(
        [
            _field("Safety", safety or "未运行"),
            _field("Dry-run can_apply", dry_run_text),
            _field("Apply", apply_text),
            _field("Validation", _result_status(getattr(result, "validation_result", None))),
            _field("Contract", _result_status(getattr(result, "contract_result", None))),
            _field("Counterfactual", _result_status(getattr(result, "counterfactual_result", None))),
            _field("Adversarial", _result_status(getattr(result, "adversarial_result", None))),
        ]
    )


def _decision_reason_summary(decision: DecisionResult | None) -> str:
    if decision is None or not decision.reasons:
        return ""
    lines = ["**决策原因：**"]
    for reason in decision.reasons[:MAX_REASON_ITEMS]:
        lines.append(f"- {reason.severity.upper()} `{reason.code}`: {_truncate(reason.message, FIELD_LIMIT)}")
    remaining = len(decision.reasons) - MAX_REASON_ITEMS
    if remaining > 0:
        lines.append(f"- ... 另有 {remaining} 条原因")
    return "\n".join(lines)


def _failure_feedback_details(feedback: Any) -> str:
    if feedback is None:
        return ""
    prompt = getattr(feedback, "prompt_feedback", "")
    if not prompt:
        return ""
    return f"**Failure feedback 摘要：**\n{_truncate(prompt, TEXT_LIMIT)}"


def _failure_next_step(result: Any, *, decision: DecisionResult | None, failed_stage: str) -> str:
    action = _decision_action(decision)
    attempt = int(getattr(result, "attempt_number", 0) or 0)
    max_attempts = int(getattr(result, "max_repair_attempts", 1) or 1)

    if action == DecisionAction.RETRY_REPAIR.value or (attempt and attempt < max_attempts):
        return "仍有尝试预算：使用 RepairFailureFeedback 进入下一轮 revise patch。"
    if failed_stage == "safety_review":
        return "安全审查失败：不要自动绕过护栏，建议人工检查 patch 范围、危险操作和契约风险。"
    if failed_stage == "patch_dry_run":
        return "patch dry-run 失败：重新生成与当前源码匹配的 unified diff hunk。"
    if failed_stage == "validation":
        return "普通验证失败：根据 failing command 的 stdout/stderr 修正 patch，禁止删除或弱化测试。"
    if failed_stage == "contract_validation":
        return "契约验证失败：修复 regression；若必须改变契约，应降级为人工确认。"
    if failed_stage == "counterfactual_validation":
        return "反事实验证失败：重新确认 bug 复现路径，确保 before FAIL 且 after PASS。"
    if failed_stage == "adversarial_validation":
        return "对抗验证失败：根据 failed probes 覆盖边界值、替代调用方和重复触发场景。"
    return "人工检查失败阶段、rollback 状态和日志摘要后决定是否继续修复。"


def _decision_action(decision: DecisionResult | None) -> str:
    if decision is None:
        return ""
    action = decision.action
    if isinstance(action, DecisionAction):
        return action.value
    return str(action or "")


def _decision_auto_deliver_text(decision: DecisionResult | None) -> str:
    if decision is None:
        return "未提供"
    return str(bool(decision.can_auto_deliver))


def _status(result: Any) -> str:
    return str(getattr(result, "status", "UNKNOWN") or "UNKNOWN")


def _attempt_text(result: Any) -> str:
    attempt = int(getattr(result, "attempt_number", 0) or 0)
    max_attempts = int(getattr(result, "max_repair_attempts", 1) or 1)
    if attempt <= 0:
        return f"0/{max_attempts}"
    return f"{attempt}/{max_attempts}"


def _risk_text(result: Any, decision: DecisionResult | None) -> str:
    if decision is not None and decision.risk_level:
        return decision.risk_level
    proposal = getattr(result, "proposal", None)
    if proposal is not None and getattr(proposal, "risk_level", ""):
        return str(getattr(proposal, "risk_level"))
    return "unknown"


def _confidence_text(result: Any, decision: DecisionResult | None) -> str:
    proposal = getattr(result, "proposal", None)
    proposal_confidence = getattr(proposal, "confidence", "") if proposal is not None else ""
    if decision is not None:
        base = f"score={decision.confidence_score:.2f}"
        if proposal_confidence:
            return f"{base}, proposal={proposal_confidence}"
        return base
    return str(proposal_confidence or "unknown")


def _result_status(value: Any) -> str:
    if value is None:
        return "未运行"
    status = getattr(value, "status", None)
    if status:
        return str(status)
    passed = getattr(value, "passed", None)
    if passed is not None:
        return "PASS" if bool(passed) else "FAIL"
    return "unknown"


def _feedback_stage(feedback: Any) -> str:
    return str(getattr(feedback, "failed_stage", "") or "")


def _feedback_failed_items(feedback: Any) -> list[str]:
    if feedback is None:
        return []
    raw = getattr(feedback, "failed_probes", []) or []
    return [str(item) for item in raw]


def _feedback_summary(feedback: Any) -> str:
    return str(getattr(feedback, "summary", "") or "")


def _failed_items_text(items: list[str]) -> str:
    if not items:
        return "未记录"
    shown = items[:MAX_FAILED_ITEMS]
    suffix = f"；另有 {len(items) - MAX_FAILED_ITEMS} 项" if len(items) > MAX_FAILED_ITEMS else ""
    return ", ".join(shown) + suffix


def _join_non_empty(values: list[str], *, sep: str) -> str:
    return sep.join(value for value in values if value)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def _truncate(value: str, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."
