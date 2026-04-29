"""Generate fix-analysis reports and patch drafts from CodeContext."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .code_context import CodeContext, format_code_context_report
from .llm_client import LLMClient


SYSTEM_PROMPT = """你是 Service Recovery Agent 的修复规划器。
你的任务是基于确定性的 CodeContext 生成“分析报告 + patch 草案”。
你不能声称已经修改代码，不能执行命令，不能提交 PR。
你必须优先保持外部契约不变，避免改函数签名、返回类型、异常语义和路由注册方式。
这是 Web API 服务场景：除非 CodeContext 明确说明业务允许，否则不要把非法输入转换为 NaN、Infinity 或 -Infinity 这类非标准 JSON 特殊浮点值；对用户非法输入应优先规划为明确的 4xx 错误或受控业务错误。
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


def build_fix_prompt(context: CodeContext) -> str:
    """Build the prompt sent to Doubao / Mock LLM."""

    context_report = format_code_context_report(context)
    context_json = json.dumps(_compact_context(context), ensure_ascii=False, indent=2)

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
7. change_type 只能是 A/B/C/D/E：
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

## Machine-readable CodeContext

```json
{context_json}
```
"""


def propose_fix(context: CodeContext, client: LLMClient) -> FixProposal:
    """Ask the LLM client for a fix proposal and parse it."""

    prompt = build_fix_prompt(context)
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
