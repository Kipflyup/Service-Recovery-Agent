"""Generate fix-analysis reports and patch drafts from CodeContext."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .code_context import CodeContext, format_code_context_report
from .failure_feedback import RepairFailureFeedback
from .fault_diagnosis import FaultDiagnosis, format_fault_diagnosis_report
from .git_diff_correlation import (
    GitDiffCorrelationResult,
    format_git_diff_correlation_evidence,
)
from .invariant_analyzer import DeepImpactAssessment, format_hidden_invariant_evidence
from .llm_client import LLMClient
from .sbfl import SBFLResult, format_sbfl_result


SYSTEM_PROMPT = """你是 Service Recovery Agent 的修复规划器。
你的任务是基于确定性的 CodeContext 生成“分析报告 + patch 草案”。
你不能声称已经修改代码，不能执行命令，不能提交 PR。
你必须优先保持外部契约不变，避免改函数签名、返回类型、异常语义和路由注册方式。
这是 Web API 服务场景：除非 CodeContext 明确说明业务允许，否则不要把非法输入转换为 NaN、Infinity 或 -Infinity 这类非标准 JSON 特殊浮点值；对用户非法输入应优先规划为明确的 4xx 错误或受控业务错误。
如果用户 prompt 中包含 Deterministic Fault Diagnosis，它是确定性程序分析结果，优先级高于普通模型推断；必须优先满足其中的 repair_constraints，特别是 avoid_special_float 和 web_api_error_semantics。
如果用户 prompt 中包含 Git Diff Correlation Evidence，它是只读 Git history / diff 的确定性证据，只能作为根因定位辅助；不要把“最近修改过”直接等同于“必然根因”，但必须在 root_cause / fix_strategy 中合理利用高分候选。
如果用户 prompt 中包含 SBFL Result，它是测试覆盖频谱的确定性 suspiciousness evidence；不要把高分行直接等同于必然根因，但必须优先审视高分且与 traceback / diagnosis / recent diff 交叉支持的行。
如果用户 prompt 中包含 Hidden Invariant Evidence，它只是提示词证据和人工 review hint，不是 hard gate；应优先保持高置信度的类型、测试、调用方和路由契约，除非验证证据明确要求改变。
如果 Fault Diagnosis 指出多个调用方或对抗测试提示，例如 /divide 和 /bug，patch 草案必须覆盖所有相关调用路径，不能只修复单一路由入口。
输出必须是一个合法 JSON 对象，不要输出 Markdown，不要包裹 ```json 代码块。"""


@dataclass(frozen=True)
class FixProposal:
    root_cause: str
    fix_strategy: str
    change_type: str
    risk_level: str
    confidence: str
    contract_constraints: list[str] = field(default_factory=list)
    patch_draft: str = ""
    tests_to_run: list[str] = field(default_factory=list)
    manual_review_notes: str = ""
    raw_response: str = ""
    provider: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FixProposalParseError(RuntimeError):
    """Raised when an LLM response cannot be parsed into a FixProposal."""


def build_fix_prompt(
    context: CodeContext,
    diagnosis: FaultDiagnosis | None = None,
    failure_feedback: RepairFailureFeedback | None = None,
    git_diff_correlation: GitDiffCorrelationResult | None = None,
    sbfl_result: SBFLResult | None = None,
    hidden_invariants: DeepImpactAssessment | None = None,
) -> str:
    """Build the prompt sent to Doubao / Mock LLM."""

    context_report = format_code_context_report(context)
    context_payload = _compact_context(context)
    if diagnosis is not None:
        context_payload["fault_diagnosis"] = diagnosis.to_dict()
    if git_diff_correlation is not None:
        context_payload["git_diff_correlation"] = git_diff_correlation.to_dict()
    if sbfl_result is not None:
        context_payload["sbfl_result"] = sbfl_result.to_dict()
    if hidden_invariants is not None:
        context_payload["hidden_invariants"] = hidden_invariants.to_dict()
    if failure_feedback is not None:
        context_payload["failure_feedback"] = failure_feedback.to_dict()
    context_json = json.dumps(context_payload, ensure_ascii=False, indent=2)

    diagnosis_section = ""
    if diagnosis is not None:
        diagnosis_section = f"""
## Deterministic Fault Diagnosis

{format_fault_diagnosis_report(diagnosis)}
"""

    git_diff_section = ""
    if git_diff_correlation is not None:
        git_diff_section = f"""
{format_git_diff_correlation_evidence(git_diff_correlation)}
"""

    sbfl_section = ""
    if sbfl_result is not None:
        sbfl_section = f"""
{format_sbfl_result(sbfl_result)}
"""

    hidden_invariant_section = ""
    if hidden_invariants is not None:
        hidden_invariant_section = f"""
{format_hidden_invariant_evidence(hidden_invariants)}
"""

    failure_feedback_section = ""
    if failure_feedback is not None:
        failure_feedback_section = f"""
## Previous Repair Failure Feedback

The previous patch attempt failed after validation. Use this feedback to revise the next patch draft:

```text
{failure_feedback.prompt_feedback.rstrip()}
```
"""

    return f"""请基于下面的 CodeContext 生成修复建议。

重要约束：
1. 现在只允许输出“分析报告 + patch 草案”，不要修改任何文件。
2. patch_draft 必须是 unified diff 风格文本，但它只是草案。
3. 优先选择函数内部兼容修复。
4. 如果影响域为 medium/high，不要修改函数签名、参数含义、返回类型或路由注册方式。
5. 明确列出需要保持的契约和需要运行的测试。
6. Web API 语义约束：
   - 不要在 JSON 响应中引入 NaN、Infinity、-Infinity、float('nan')、float('inf')、math.nan、math.inf；
   - 不要把运行时崩溃静默转换成特殊浮点值；
   - 对用户非法输入优先规划为 4xx 响应或受控业务错误；
   - 如果 patch 需要改变 HTTP 状态码、错误响应格式或异常语义，必须在 manual_review_notes 中标记需要人工确认。
7. 如果下面提供了 Deterministic Fault Diagnosis：
   - 必须优先满足其中的 Repair Constraints；
   - 必须覆盖其中指出的故障变量、触发条件和调用方；
   - 必须优先满足 avoid_special_float 和 web_api_error_semantics；
   - 必须参考 Adversarial Hints 规划需要运行的测试，尤其是 /divide?x=0、/divide?x=-1、/divide、/bug 和重复触发场景。
8. 如果下面提供了 Git Diff Correlation Evidence：
   - 把它作为 recent-change root-cause evidence，而不是唯一真相；
   - 优先关注 score 高、同时命中 crash file、crash line、crash function 的候选；
   - root_cause 中应说明 recent diff evidence 是否支持当前修复判断；
   - 不要为了匹配 recent diff 而扩大修改范围。
9. 如果下面提供了 SBFL Result：
   - 把它作为测试覆盖频谱 suspiciousness evidence，而不是唯一真相；
   - 优先审视 Top Suspicious Lines，尤其是与 traceback crash site、Fault Diagnosis 或 Git Diff Correlation 同时指向的行；
   - 如果 SBFL 状态为 PARTIAL / UNAVAILABLE，不要过度依赖它；
   - 不要为了迎合 SBFL 分数而扩大修改范围。
10. 如果下面提供了 Hidden Invariant Evidence：
   - 只把它作为 prompt evidence / review hint，不要当成 hard gate；
   - 优先保持高置信度 test/type/caller/route 契约；
   - 如果修复策略可能改变这些隐含契约，必须在 manual_review_notes 中说明。
11. 如果下面提供了 Previous Repair Failure Feedback：
   - 必须显式修正上一轮失败的 probe；
   - patch_draft 必须覆盖 failed_probes 中列出的请求路径和实际失败原因；
   - manual_review_notes 中必须说明上一轮失败如何被本轮 patch 处理。
12. change_type 只能是 A/B/C/D/E：
   - A: 纯增量防护，例如输入校验、空值保护、边界保护；
   - B: 函数内部逻辑修正；
   - C: 签名兼容重构；
   - D: 接口/契约变更，需要人工；
   - E: 跨文件大范围变更，需要人工。

请严格输出如下 JSON schema，不要额外输出任何解释文字：
{{
  "root_cause": "string",
  "fix_strategy": "string",
  "change_type": "A|B|C|D|E",
  "risk_level": "low|medium|high",
  "confidence": "low|medium|high",
  "contract_constraints": ["string"],
  "patch_draft": "unified diff string",
  "tests_to_run": ["string"],
  "manual_review_notes": "string"
}}

## Human-readable CodeContext

{context_report}
{diagnosis_section}
{git_diff_section}
{sbfl_section}
{hidden_invariant_section}
{failure_feedback_section}

## Machine-readable CodeContext

```json
{context_json}
```
"""


def propose_fix(
    context: CodeContext,
    client: LLMClient,
    diagnosis: FaultDiagnosis | None = None,
    failure_feedback: RepairFailureFeedback | None = None,
    git_diff_correlation: GitDiffCorrelationResult | None = None,
    sbfl_result: SBFLResult | None = None,
    hidden_invariants: DeepImpactAssessment | None = None,
) -> FixProposal:
    """Ask the LLM client for a fix proposal and parse it."""

    prompt = build_fix_prompt(
        context,
        diagnosis=diagnosis,
        failure_feedback=failure_feedback,
        git_diff_correlation=git_diff_correlation,
        sbfl_result=sbfl_result,
        hidden_invariants=hidden_invariants,
    )
    raw_response = client.complete(prompt, system=SYSTEM_PROMPT)
    proposal = parse_fix_proposal(
        raw_response,
        provider=getattr(client, "provider", ""),
        model=getattr(client, "model", ""),
    )
    return proposal


def parse_fix_proposal(
    raw_response: str,
    *,
    provider: str = "",
    model: str = "",
) -> FixProposal:
    """Parse a JSON fix proposal from model output."""

    data = _extract_json_object(raw_response)
    return FixProposal(
        root_cause=str(data.get("root_cause", "")).strip(),
        fix_strategy=str(data.get("fix_strategy", "")).strip(),
        change_type=str(data.get("change_type", "")).strip() or "B",
        risk_level=str(data.get("risk_level", "")).strip() or "medium",
        confidence=str(data.get("confidence", "")).strip() or "medium",
        contract_constraints=_string_list(data.get("contract_constraints")),
        patch_draft=str(data.get("patch_draft", "")).strip(),
        tests_to_run=_string_list(data.get("tests_to_run")),
        manual_review_notes=str(data.get("manual_review_notes", "")).strip(),
        raw_response=raw_response,
        provider=provider,
        model=model,
    )


def format_fix_proposal_report(proposal: FixProposal) -> str:
    """Render a human-readable report."""

    lines: list[str] = []
    lines.append("# Fix Proposal")
    lines.append("")
    if proposal.provider or proposal.model:
        lines.append(f"- Provider: {proposal.provider or 'unknown'}")
        lines.append(f"- Model: {proposal.model or 'unknown'}")
        lines.append("")

    lines.append("## Root Cause")
    lines.append(proposal.root_cause or "-")

    lines.append("")
    lines.append("## Fix Strategy")
    lines.append(proposal.fix_strategy or "-")

    lines.append("")
    lines.append("## Safety Classification")
    lines.append(f"- Change type: {proposal.change_type}")
    lines.append(f"- Risk level: {proposal.risk_level}")
    lines.append(f"- Confidence: {proposal.confidence}")

    lines.append("")
    lines.append("## Contract Constraints")
    if proposal.contract_constraints:
        for constraint in proposal.contract_constraints:
            lines.append(f"- {constraint}")
    else:
        lines.append("- No explicit constraints returned.")

    lines.append("")
    lines.append("## Patch Draft")
    if proposal.patch_draft:
        lines.append("```diff")
        lines.append(proposal.patch_draft)
        lines.append("```")
    else:
        lines.append("- No patch draft returned.")

    lines.append("")
    lines.append("## Tests To Run")
    if proposal.tests_to_run:
        for test in proposal.tests_to_run:
            lines.append(f"- `{test}`")
    else:
        lines.append("- No tests returned.")

    lines.append("")
    lines.append("## Manual Review Notes")
    lines.append(proposal.manual_review_notes or "-")
    lines.append("")
    lines.append("> 注意：这是修复建议和 patch 草案，当前步骤不会修改文件。")
    return "\n".join(lines)


def _compact_context(context: CodeContext) -> dict[str, Any]:
    crash_function = context.crash_function.to_dict() if context.crash_function else None
    surrounding_source = (
        {
            **context.surrounding_source.to_dict(),
            "numbered_code": context.surrounding_source.numbered_code(),
        }
        if context.surrounding_source
        else None
    )

    return {
        "exception_type": context.traceback.exception_type,
        "exception_message": context.traceback.exception_message,
        "crash_file": context.crash_file_relative,
        "crash_line_number": context.crash_line_number,
        "crash_line_code": context.crash_line_code,
        "crash_function": crash_function,
        "surrounding_source": surrounding_source,
        "incoming_calls": [call.to_dict() for call in context.incoming_calls],
        "outgoing_calls": context.outgoing_calls,
        "impact": context.impact.to_dict(),
    }


def _extract_json_object(raw_response: str) -> Mapping[str, Any]:
    text = raw_response.strip()
    if not text:
        raise FixProposalParseError("LLM returned an empty response.")

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = json.loads(_first_balanced_json_object(text))

    if not isinstance(parsed, Mapping):
        raise FixProposalParseError("LLM response JSON is not an object.")
    return parsed


def _first_balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        raise FixProposalParseError("Could not find JSON object in LLM response.")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]

    raise FixProposalParseError("Could not find a balanced JSON object in LLM response.")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
