# Service Recovery Agent

基于 Agent 的服务自动化修复系统原型。目标链路是：

```text
Web 服务报错
  → 读取 Traceback 日志
  → 结构化程序分析
  → LLM 生成修复
  → 跑测试与安全验证
  → 创建 Commit / PR
  → 飞书卡片通知开发者 Review
```

## 当前状态

已具备：

- 飞书 OpenAPI 配置读取；
- 获取 `tenant_access_token`；
- 发送交互式卡片的可复用模块；
- 发送示例修复通知卡片的脚本。
- Demo Web 服务与可复现异常；
- Traceback 日志读取和结构化解析；
- CodeContext 生成：崩溃源码、调用方、影响域；
- Doubao 2.0 / 火山方舟 LLM Client；
- MockLLMClient 离线修复建议；
- 基于 CodeContext 的分析报告和 patch 草案生成。
- Patch Safety Review：对 patch 草案做 PASS/WARN/FAIL 静态安全审查。
- Patch dry-run preview：确认 patch 是否能干净应用；
- 受控 apply：只有安全审查 PASS 且 dry-run 成功才允许真正改文件；
- 修复后测试验证：默认运行 `python -m pytest -q`；
- 验证失败自动回滚：恢复 apply 前的文件快照。
- Repair Validation Profile：区分“故障种子测试”和“修复后业务验证”，已能跑出 mock patch 正向闭环。
- FaultDiagnosis：确定性诊断已完成 Phase I 第一版，支持 `ZeroDivisionError` / `KeyError` / `AttributeError` / `TypeError` / `ValueError` 等 taxonomy、AST crash expression、变量流、修复约束和 adversarial hints，并已注入 FixPlanner / RecoveryAgent prompt。
- Adversarial Validation Agent：基于 FaultDiagnosis 生成边界 probe，输出结构化 PASS / FAIL / PARTIAL，并可作为 `RecoveryAgent.run_once()` 的 apply 后质量门禁。
- Contract / Counterfactual Validation：支持 command-level 契约保持验证，以及 before_patch FAIL / after_patch PASS 反事实验证。
- Decision Engine：基于 A/B/C/D/E、风险、置信度和各门禁结果输出交付 / 降级 / 人工决策。
- 分阶段 RepairFailureFeedback + 有限 revise loop：safety / dry-run / apply / validation / contract / counterfactual / adversarial 失败都可生成结构化反馈；retryable 阶段可按 `max_repair_attempts` 生成下一轮修复。
- Feishu 结果卡片 dry-run builder + Terminal Notification：输入 `RecoveryRunResult` + `DecisionResult`，输出 success / report-only / failure 三类卡片 JSON；`scripts/run_recovery_once.py --emit-feishu-card-json` 可导出卡片 JSON，`--send-feishu-card` 可在修复/验证/决策结束后显式发送终态飞书卡片。
- Review Artifact / PR 描述草稿：输入 `RecoveryRunResult` + `DecisionResult`，输出未来 PR 可直接使用的 Markdown 和 rollback / success / failure criteria，不执行 Git/PR。
- RecoveryAgent watch 模式：`recovery_watcher.py` 支持 watch loop 调用 `RecoveryAgent.run_once()`，默认 preview-only，并提供 traceback fingerprint 去重、cooldown、max_events；不执行 Git/PR，不真实发送飞书。
- Git Diff Correlation：`git_diff_correlation.py` 只读检查最近 Git history / diff，将 changed files / hunks 与 traceback crash file / line / function 做 deterministic correlation evidence，并已注入 FixPlanner / RecoveryAgent prompt；不创建 commit，不 push，不 PR。
- SBFL 频谱故障定位：`sbfl.py` 支持输入 pytest nodeid / pytest command，采集通过/失败测试覆盖行，使用 Ochiai 公式输出 top suspicious lines；`FixPlanner` 已支持 `sbfl_result` 可选 prompt / JSON 注入；默认不接入 RecoveryAgent 主链路。
- Hidden Invariant / Deep Impact Analyzer：`invariant_analyzer.py` 从 crash function 的 return type、docstring、name、callers、tests 中提取 `InvariantCandidate` / `DeepImpactAssessment`，作为 FixPlanner prompt evidence / review hint；不是 hard gate，不执行 Git/PR，不发送飞书。

尚未完成：

- 本地 Git branch / commit 交付层；
- GitHub PR 创建；
- Git/PR 之后将真实 PR URL / branch / commit SHA 回填到 success 飞书卡片；当前 `run_recovery_once.py --send-feishu-card` 已支持修复/验证/决策后的终态飞书通知，但尚未接入 Git/PR。
  

## 当前模块结构 Mermaid 图

```mermaid
flowchart LR
    subgraph InputLayer["输入/触发层"]
        Web["demo_service Flask App"]
        Logs["logs/app.log"]
        Watcher["log_watcher.py<br/>recovery_watcher.py"]
    end

    subgraph ContextLayer["上下文与诊断层"]
        TB["traceback_parser.py<br/>TracebackEvent"]
        CC["code_context.py<br/>CodeContext"]
        FD["fault_diagnosis.py<br/>FaultDiagnosis"]
        GDC["git_diff_correlation.py<br/>GitDiffCorrelationResult"]
        SBFL["sbfl.py<br/>SBFLResult 可选"]
        INV["invariant_analyzer.py<br/>DeepImpactAssessment 可选"]
    end

    subgraph PlanningLayer["LLM 修复规划层"]
        FP["fix_planner.py<br/>FixProposal"]
        LLM["llm_client.py<br/>Doubao / MockLLMClient"]
    end

    subgraph PatchLayer["Patch 安全与执行层"]
        PS["patch_safety.py<br/>PatchSafetyReview"]
        Patcher["patcher.py<br/>preview/apply/snapshot/restore"]
    end

    subgraph GateLayer["验证门禁层"]
        Val["validator.py<br/>ValidationResult"]
        CV["contract_validator.py"]
        CF["counterfactual_validator.py"]
        ADV["adversarial_validator.py"]
    end

    subgraph LoopDecision["失败反馈与决策层"]
        FB["failure_feedback.py<br/>RepairFailureFeedback"]
        RA["recovery_agent.py<br/>RecoveryRunResult"]
        DE["decision.py<br/>DecisionResult"]
    end

    subgraph NotifyDelivery["通知与交付规划层"]
        Cards["feishu_cards.py<br/>success/report-only/failure JSON"]
        Notifier["feishu_notifier.py<br/>Feishu terminal notification"]
        Feishu["feishu.py<br/>Feishu OpenAPI"]
        Review["review_artifact.py<br/>未来 PR Markdown"]
        GitSafe["git_safety.py<br/>GitDeliveryPlan dry-run"]
    end

    Web --> Logs --> Watcher --> TB --> CC
    CC --> FD
    CC --> GDC
    CC -.-> SBFL
    CC -.-> INV
    FD --> FP
    GDC --> FP
    SBFL -.-> FP
    INV -.-> FP
    FP --> LLM --> FP
    FP --> PS --> Patcher
    Patcher --> Val --> CV --> CF --> ADV
    ADV --> RA
    Val --> RA
    CV --> RA
    CF --> RA
    PS --> RA
    RA --> FB
    FB --> FP
    RA --> DE
    DE --> Cards --> Notifier --> Feishu
    RA --> Review
    DE --> GitSafe
    RA --> GitSafe
```

```text

最近一次测试，实际跑通了：

Traceback → RecoveryAgent → MockLLMClient 修复 → Patch Safety → dry-run → apply →
validation → contract validation → counterfactual validation → adversarial validation →
Decision Engine → Feishu terminal notification → GitDeliveryPlan dry-run

飞书真实交互式卡片已发送成功。
卡片类型：success
卡片标题：Service Recovery Agent：修复已通过门禁
卡片包含：查看 PR 按钮
PR URL：模拟地址 https://github.com/orion/Service-Recovery-Agent/pull/426501
真实 Git/PR 没有执行。

当前已完成：
1. 自动读取 traceback；
2. 自动诊断；
3. 自动生成修复；
4. 自动安全审查；
5. 自动 apply；
6. 自动验证；
7. 自动对抗验证；
8. 自动决策；
9. 自动发送飞书终态卡；
10. 自动生成 GitDeliveryPlan dry-run。

目前未完成：
1. 真实 Git worktree local commit；
2. 真实 push branch；
3. 真实 GitHub PR；
4. PR 创建后把真实 PR URL / commit SHA 回填飞书卡片；
5. Watch 模式下自动触发完整 auto repair + PR + Feishu；
6. 连续失败 10 次后的自动降级策略；
7. Doubao 2.0 真实 provider 的端到端稳定性测试。

```

## 项目结构

```text
.
├── README.md
├── Plan.md
├── pytest.ini
├── requirements.txt
├── .env.example
├── demo_service/
│   ├── app.py
│   └── tests/
│       └── test_app.py
├── scripts/
│   ├── check_feishu_card.py
│   ├── adversarial_validate.py
│   ├── apply_patch.py
│   ├── diagnose_latest_error.py
│   ├── inspect_latest_error.py
│   ├── preview_patch.py
│   ├── propose_fix.py
│   ├── review_patch.py
│   ├── run_demo_service.py
│   ├── run_recovery_once.py
│   └── validate_demo_repair.py
├── src/
│   └── service_recovery_agent/
│       ├── __init__.py
│       ├── adversarial_validator.py
│       ├── code_context.py
│       ├── contract_validator.py
│       ├── counterfactual_validator.py
│       ├── decision.py
│       ├── feishu.py
│       ├── feishu_cards.py
│       ├── failure_feedback.py
│       ├── fault_diagnosis.py
│       ├── fix_planner.py
│       ├── git_diff_correlation.py
│       ├── invariant_analyzer.py
│       ├── llm_client.py
│       ├── log_watcher.py
│       ├── patcher.py
│       ├── patch_safety.py
│       ├── recovery_agent.py
│       ├── recovery_watcher.py
│       ├── review_artifact.py
│       ├── sbfl.py
│       ├── traceback_parser.py
│       └── validator.py
├── tests/
│   ├── test_code_context.py
│   ├── test_adversarial_validator.py
│   ├── test_contract_validator.py
│   ├── test_counterfactual_validator.py
│   ├── test_decision.py
│   ├── test_fault_diagnosis.py
│   ├── test_fix_planner.py
│   ├── test_feishu_cards.py
│   ├── test_git_diff_correlation.py
│   ├── test_invariant_analyzer.py
│   ├── test_llm_client.py
│   ├── test_patcher.py
│   ├── test_patch_safety.py
│   ├── test_recovery_agent.py
│   ├── test_recovery_watcher.py
│   ├── test_review_artifact.py
│   ├── test_sbfl.py
│   └── test_validator.py

```

## 飞书卡片内容
- 卡片类型：success
- 服务：demo-web-service（链路测试：PR URL 为模拟）
- 运行状态：PASS
- Decision：CREATE_PR_READY
- can_create_pr：True
- rolled_back：False
- Attempts：1/2
- 风险等级：medium
- 置信度：score=1.00, proposal=high
- Safety：PASS
- Dry-run can_apply：True
- Apply：True
- Validation：PASS
- Contract：PASS
- Counterfactual：PASS
- Adversarial：PASS
- 仓库：Service-Recovery-Agent
- 分支：agent/fix-zero-division-demo
- 查看 PR 按钮

## 环境准备

建议使用 Python 虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

复制环境变量示例：

```bash
cp .env.example .env
```

然后编辑 `.env`：

```env
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_NOTICE_RECEIVER=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_RECEIVE_ID_TYPE=open_id

LLM_PROVIDER=doubao
DOUBAO_API_KEY=your_volcengine_ark_api_key
DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
DOUBAO_MODEL=ep-xxxxxxxxxxxxxxxx
DOUBAO_TIMEOUT_SECONDS=60
DOUBAO_MAX_TOKENS=4096
DOUBAO_TEMPERATURE=0.2
DOUBAO_MAX_RETRIES=1
DOUBAO_RETRY_BACKOFF_SECONDS=2

GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_REPO_OWNER=your_username
GITHUB_REPO_NAME=your_repo

WEB_SERVICE_LOG_PATH=./logs/app.log
AUTO_FIX_ENABLED=true
AUTO_PR_ENABLED=true
```

> 安全提醒：`.env`、`.trae/mcp.json` 等可能包含真实密钥，已经被 `.gitignore` 忽略，不要提交到 Git。

## 运行 Demo Web 服务

启动服务：

```bash
python scripts/run_demo_service.py
```

默认监听：

```text
http://127.0.0.1:5001
```

健康检查：

```bash
curl http://127.0.0.1:5001/health
```

正常请求：

```bash
curl "http://127.0.0.1:5001/divide?x=4"
```

触发预埋 Bug：

```bash
curl "http://127.0.0.1:5001/divide?x=0"
```

触发后，服务会把完整 Python Traceback 写入：

```text
logs/app.log
```

解析最新 Traceback：

```bash
PYTHONPATH=src python - <<'PY'
from service_recovery_agent.log_watcher import read_latest_traceback

event = read_latest_traceback("logs/app.log")
print(event.to_dict() if event else "no traceback found")
PY
```

这一步打通的是：

```text
服务报错 → 产生日志 → Agent 读取并结构化解析 Traceback
```

## 生成代码上下文 CodeContext

当日志中已经有 Traceback 后，可以继续把错误事件扩展成代码上下文：

```bash
python scripts/inspect_latest_error.py
```

这个脚本会读取最新 Traceback，并输出：

- 异常类型与异常信息；
- 崩溃文件、行号、函数；
- 崩溃行附近源码；
- 崩溃函数完整源码；
- 项目内直接调用方；
- 出站调用；
- 启发式影响域评估。

也可以输出 JSON，供后续 Agent/LLM 使用：

```bash
python scripts/inspect_latest_error.py --json
```

如果希望 Agent 等待新的异常出现：

```bash
python scripts/inspect_latest_error.py --wait 30
```

当前 demo 中，`/divide?x=0` 会被定位到：

```text
demo_service/app.py:31
function: unsafe_divide
code: return numerator / denominator
```

并能静态扫描出直接调用方，例如：

```text
create_app.divide
create_app.bug
```

这一步打通的是：

```text
TracebackEvent → CodeContext
```

也就是让 Agent 不只知道“哪里报错”，还知道“那里的代码长什么样、调用方是谁、修复影响范围有多大”。

## 生成确定性故障诊断 FaultDiagnosis

在 `CodeContext` 之后，可以进一步生成确定性的故障诊断：

```bash
python scripts/diagnose_latest_error.py
```

这个脚本会读取最新 Traceback，构建 `CodeContext`，再通过规则化诊断层输出：

- 异常分类，例如 `ZeroDivisionError → arithmetic_boundary`；
- 主故障候选位置，例如 `demo_service/app.py:31`；
- 可疑变量，例如 `denominator`；
- 触发条件，例如 `denominator == 0`；
- 变量来源，例如 `query parameter denominator or x via _float_arg`；
- 修复约束，例如保持函数签名、保持 happy path、不要返回 NaN/Infinity、Web API 非法输入要转成受控 4xx；
- 后续对抗测试提示，例如 `/divide?x=0`、`/divide?x=-1`、`/divide`、`/bug`、重复触发 `/divide?x=0`。

也可以输出 JSON，供后续 FixPlanner / RecoveryAgent 使用：

```bash
python scripts/diagnose_latest_error.py --json
```

如果希望等待新的异常出现：

```bash
python scripts/diagnose_latest_error.py --wait 30
```

当前 `FaultDiagnosis` 是确定性规则实现，Phase I 第一版已经从 demo 专用除零诊断扩展为通用 taxonomy / dataflow 层：

```text
ZeroDivisionError → arithmetic_boundary
KeyError          → missing_key
AttributeError    → null_or_type_mismatch
TypeError         → type_mismatch
ValueError        → invalid_value
```

它会基于 traceback crash line 和 crash function source 做 AST 分析，例如：

```text
return numerator / denominator   → BinOp(op=Div), right operand denominator
return payload["user_id"]        → Subscript, mapping payload, key 'user_id'
return user.name                 → Attribute, base object user, attribute name
return value + 1                 → BinOp(op=Add), operands value / 1
return int(raw_age)              → Call, callee int, argument raw_age
```

进一步推断：

```text
fault variable / suspicious input
trigger condition
variable flow / local assignment source
repair constraints
adversarial hints
```

`FaultDiagnosis` 当前已经接入修复规划链路：

- `scripts/propose_fix.py` 默认会自动生成 diagnosis，并把 `Deterministic Fault Diagnosis` 注入 Doubao / Mock prompt；
- `scripts/propose_fix.py --no-diagnosis` 可用于调试旧 prompt；
- `RecoveryAgent.run_once()` 会在 `CodeContext` 后自动生成 `diagnosis`，并把它放入 `RecoveryRunResult.to_dict()`；
- 当 `FaultDiagnosis` 给出 `repair_constraints` 时，FixPlanner prompt 会要求模型优先满足确定性约束。当前约束已覆盖除零边界、missing key、attribute None/type mismatch、type mismatch、invalid value parsing 等类别。


## Git Diff Correlation 只读变更关联证据

在 `FaultDiagnosis` 之后，当前新增了只读 Git Diff Correlation 模块，用于回答：最近提交是否刚好修改了 traceback crash site 附近的文件 / 行 / 函数。

```text
src/service_recovery_agent/git_diff_correlation.py
tests/test_git_diff_correlation.py
```

核心 API：

```python
from service_recovery_agent.git_diff_correlation import (
    correlate_traceback_with_recent_diff,
    format_git_diff_correlation_evidence,
)

result = correlate_traceback_with_recent_diff(
    traceback_event_or_code_context,
    project_root=".",
    max_commits=5,
)

print(format_git_diff_correlation_evidence(result))
```

第一版评分策略：

```text
+0.60 crash_file 在最近 diff 中出现
+0.20 crash_line 落在某个 hunk 的 new range 内
+0.10 crash_function 名称出现在 hunk header / body 中
+0.10 traceback 中其他 frame 文件也在同一 commit diff 中出现
```

输出状态：

```text
FOUND       找到与 crash site 相关的最近 diff candidate
NOT_FOUND   最近 N 个 commit 没有修改 crash file
UNAVAILABLE 当前目录不是 Git repo / Git 不可用 / 没有可检查的 commit
```

安全约束：

```text
1. 模块只执行 git log / git show；
2. 不创建 branch；
3. 不 git add；
4. 不 git commit；
5. 不 git push；
6. 不调用 gh / GitHub PR。
```

当前已经完成 Phase G.5：`GitDiffCorrelationResult.to_dict()` 会进入 FixPlanner 的 machine-readable JSON，`format_git_diff_correlation_evidence(...)` 会进入 human-readable prompt；`RecoveryAgent.run_once()` 和 `scripts/propose_fix.py` 默认会只读收集并注入该 evidence。若需要调试旧链路，可使用 `--no-git-diff-correlation` 关闭。


## SBFL 频谱故障定位

当前新增了 SBFL（Spectrum-Based Fault Localization）第一版模块，用于从测试覆盖角度补充 suspiciousness evidence：

```text
src/service_recovery_agent/sbfl.py
tests/test_sbfl.py
```

核心 API：

```python
from service_recovery_agent.sbfl import run_sbfl, format_sbfl_result

result = run_sbfl(
    project_root=".",
    pytest_nodeids=[
        "tests/test_example.py::test_pass",
        "tests/test_example.py::test_fail",
    ],
    top_k=10,
)

print(format_sbfl_result(result))
```

也支持 pytest command 输入：

```python
result = run_sbfl(
    project_root=".",
    commands=[
        "python -m pytest -q tests/test_example.py::test_pass",
        "python -m pytest -q tests/test_example.py::test_fail",
    ],
)
```

第一版使用 Python 标准库 `trace` 在子进程中采集每次 pytest run 的执行行，然后使用 Ochiai 公式：

```text
score(line) = failed_and_covered / sqrt(total_failed × (failed_and_covered + passed_and_covered))
```

输出状态：

```text
PASS        已观察到失败测试，并输出 suspicious lines
PARTIAL     没有失败测试，或失败测试没有采集到项目源码覆盖行
UNAVAILABLE 没有输入 pytest nodeid/command，或 command 不是 pytest command
```

安全与性能边界：

```text
1. 默认不接入 RecoveryAgent.run_once() 主链路，避免拖慢自动修复；
2. 不执行 Git/PR；
3. 不调用 LLM；
4. 不发送飞书；
5. 离线测试可运行，不依赖 coverage.py 第三方包。
```

`FixPlanner` 已支持把 `SBFLResult` 作为可选 prompt evidence 注入：

```python
from service_recovery_agent.fix_planner import build_fix_prompt

prompt = build_fix_prompt(
    code_context,
    diagnosis=diagnosis,
    sbfl_result=sbfl_result,
)
```

注入后会同时出现在：

```text
1. human-readable prompt section: # SBFL Result
2. machine-readable JSON: "sbfl_result": {...}
```

注意：SBFL evidence 是测试覆盖频谱 suspiciousness evidence，不是唯一根因；prompt 会要求模型优先审视 top suspicious lines，尤其是与 traceback / FaultDiagnosis / GitDiffCorrelation 同时指向的行，但不能为了迎合分数扩大修改范围。`RecoveryAgent.run_once()` 仍未默认运行 SBFL，以避免拖慢主链路。

## Hidden Invariant / Deep Impact Analyzer

当前新增了更深影响域 / Hidden Invariant 分析第一版模块，用于从静态、离线、确定性证据中提取“可能被 patch 破坏但测试/调用方隐含依赖”的 review hints：

```text
src/service_recovery_agent/invariant_analyzer.py
tests/test_invariant_analyzer.py
```

核心 API：

```python
from service_recovery_agent.invariant_analyzer import (
    analyze_hidden_invariants,
    format_hidden_invariant_evidence,
)

assessment = analyze_hidden_invariants(code_context)
print(format_hidden_invariant_evidence(assessment))
```

输出结构：

```text
InvariantCandidate
DeepImpactAssessment
```

第一版会从以下证据中提取候选 invariant：

```text
1. crash function return type annotation；
2. crash function docstring；
3. crash function name，例如 is_* / has_* / divide / score；
4. direct callers / route decorators；
5. caller return shape / status code；
6. tests 中对受影响 route 或 crash function 的断言。
```

安全边界：

```text
1. 只作为 FixPlanner prompt evidence / review hint；
2. 不是 hard gate，不参与 can_create_pr 阻断；
3. 不执行 Git/PR；
4. 不调用 LLM；
5. 不真实发送飞书；
6. seed_failure 测试不会被当作 happy-path 契约。
```

`fix_planner.build_fix_prompt(..., hidden_invariants=assessment)` 已支持可选注入该 evidence。当前尚未默认接入 `RecoveryAgent.run_once()` 主链路，避免把启发式 invariant 误用为强门禁。

## 生成 LLM 修复建议和 patch 草案

当 CodeContext 可以正常生成后，可以进入“只规划、不改文件”的修复建议阶段：

```bash
python scripts/propose_fix.py --provider mock
```

`mock` 模式不会调用外部模型，适合本地验证链路和跑测试。

如果要调用 Doubao 2.0 / 火山方舟，请先确认 `.env` 中已经配置：

```env
LLM_PROVIDER=doubao
DOUBAO_API_KEY=your_volcengine_ark_api_key
DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
DOUBAO_MODEL=ep-xxxxxxxxxxxxxxxx
```

> 注意：`DOUBAO_MODEL` 请填火山方舟控制台“调用示例”里的 `model` 值。很多情况下它是推理接入点 ID，例如 `ep-xxxx`，不是页面展示名。

如果偶发 `Read timed out`，可以适当调大超时并保留一次超时重试：

```env
DOUBAO_TIMEOUT_SECONDS=180
DOUBAO_MAX_RETRIES=1
DOUBAO_RETRY_BACKOFF_SECONDS=2
```

如果遇到 `RateLimitExceeded.EndpointTPMExceeded` / HTTP 429，这是接入点 TPM 限流，不会立即自动重试；请等待配额窗口恢复，或降低 `DOUBAO_MAX_TOKENS`、缩短 prompt、避免重复调用。

然后运行：

```bash
python scripts/propose_fix.py --provider doubao
```

也可以直接使用 `.env` 中的 `LLM_PROVIDER`：

```bash
python scripts/propose_fix.py
```

输出内容包括：

- 根因分析；
- 修复策略；
- 改动类型分级；
- 风险等级；
- 需要保持的契约；
- unified diff 风格 patch 草案；
- 建议运行的测试；
- 人工 Review 注意事项。

这一步不会修改任何文件。它只完成：

```text
CodeContext → FaultDiagnosis → Git Diff Correlation Evidence → LLM 修复建议 → patch 草案
```

如需查看发送给模型的 prompt：

```bash
python scripts/propose_fix.py --print-prompt
```

默认 prompt 中应包含：

```text
Deterministic Fault Diagnosis
arithmetic_boundary
denominator == 0
query parameter denominator or x
avoid_special_float
web_api_error_semantics
```

如果需要对比旧版只含 `CodeContext` 的 prompt：

```bash
python scripts/propose_fix.py --print-prompt --no-diagnosis
```

如果需要关闭只读 Git Diff Correlation evidence 注入：

```bash
python scripts/propose_fix.py --print-prompt --no-git-diff-correlation
```

默认会检查最近 5 个 commit；可调整：

```bash
python scripts/propose_fix.py --print-prompt --git-diff-max-commits 10
```

如需 JSON 输出：

```bash
python scripts/propose_fix.py --json
```

## 审查 patch 草案安全性

生成 FixProposal 之后，可以先做 patch 安全审查。这个步骤只读 patch 草案，不会修改文件：

```bash
python scripts/review_patch.py --provider mock
```

真实调用 Doubao 并审查其 patch 草案：

```bash
python scripts/review_patch.py --provider doubao
```

如果已经保存了 `propose_fix.py --json` 的输出：

```bash
python scripts/propose_fix.py --provider doubao --json > /tmp/fix_proposal.json
python scripts/review_patch.py --proposal-json /tmp/fix_proposal.json
```

如果只想审查某个 unified diff 文件：

```bash
python scripts/review_patch.py --patch-file /tmp/patch.diff
```

审查项包括：

- patch 是否为空或格式不合法；
- 是否只修改允许文件；
- 是否跨文件修改；
- 是否创建/删除文件；
- 是否引入 `NaN`、`Infinity`、`float('inf')`、`math.nan` 等 Web API 不安全特殊浮点语义；
- 是否修改崩溃函数签名；
- 是否引入 `eval`、`exec`、`os.system`、`subprocess`、`rm -rf` 等危险操作。

输出会给出：

```text
PASS / WARN / FAIL
```

例如，如果模型把除零修成返回 `float('inf')` / `float('nan')`，审查会给出 `FAIL`，因为这会把 Web API 崩溃转换为非标准 JSON 特殊浮点语义，不能自动应用。

JSON 输出：

```bash
python scripts/review_patch.py --provider mock --json
```

## 预览 patch dry-run

通过安全审查后，可以继续做 patch dry-run，确认 patch 是否能干净应用到当前工作区。这个步骤仍然不会修改文件：

```bash
python scripts/preview_patch.py --proposal-json /tmp/fix_proposal.json
```

它会自动完成：

```text
读取 FixProposal JSON
  → 提取 patch_draft
  → Patch Safety Review
  → 写入临时 patch 文件
  → patch -p1 --dry-run
  → 输出 preview 结果
```

如果要离线验证：

```bash
python scripts/preview_patch.py --provider mock
```

如果要直接预览某个 diff 文件：

```bash
python scripts/preview_patch.py --patch-file /tmp/patch.diff
```

JSON 输出：

```bash
python scripts/preview_patch.py --proposal-json /tmp/fix_proposal.json --json
```

如果 patch 中路径是 `a/demo_service/app.py` / `b/demo_service/app.py`，默认 `--strip 1` 即可；这等价于 `patch -p1 --dry-run`。

## 应用 patch 并运行测试验证

当 patch 已经通过安全审查和 dry-run 后，可以进入真正修改文件的阶段。

> 这一阶段是当前链路里第一个会修改工作区文件的步骤，所以脚本默认仍然只 preview。必须显式传入 `--yes` 才会真正 apply。

推荐先把 LLM 结果保存下来，避免因为重复调用 Doubao 触发 TPM 限流：

```bash
python scripts/propose_fix.py --provider doubao --json > /tmp/fix_proposal.json
python scripts/preview_patch.py --proposal-json /tmp/fix_proposal.json
```

确认 preview 为 PASS 后，先运行不带 `--yes` 的 apply 脚本：

```bash
python scripts/apply_patch.py --proposal-json /tmp/fix_proposal.json
```

输出 `PREVIEW_ONLY` 说明：

```text
读取最新 Traceback 日志
  → 构建 CodeContext
  → 读取 FixProposal.patch_draft
  → Patch Safety Review
  → patch -p1 --dry-run
  → 等待人工确认 --yes
```

真正应用并验证：

```bash
python scripts/apply_patch.py --proposal-json /tmp/fix_proposal.json --yes
```

脚本会执行：

```text
读取最新 Traceback 日志
  → 构建 CodeContext
  → Patch Safety Review 必须 PASS
  → patch --dry-run 必须成功
  → 对将被修改的文件做内存快照
  → patch -p1 真正 apply
  → 默认运行 python -m pytest -q
  → 测试失败则自动 restore 快照回滚
```

默认验证命令是当前 Python 解释器下的：

```bash
python -m pytest -q
```

如果要临时指定验证命令，可以重复传入 `--validation-command`：

```bash
python scripts/apply_patch.py \
  --proposal-json /tmp/fix_proposal.json \
  --yes \
  --validation-command "python -m pytest demo_service/tests -q"
```

### Repair Validation Profile

当前 demo 同时需要两类测试：

- `seed_failure`：故障种子测试，刻意证明 `/divide?x=0` 仍能产生 `ZeroDivisionError` Traceback，保证 Agent 有稳定错误入口；
- repair validation：修复后业务验证，证明同一个输入不再触发 500，而是返回受控 Web API 错误。

因此 `pytest.ini` 中注册了 `seed_failure` marker，故障种子测试会被标记为：

```python
@pytest.mark.seed_failure
```

修复后验证时，应该排除故障种子测试，并额外运行 repair validation 脚本：

```bash
python scripts/apply_patch.py \
  --provider mock \
  --yes \
  --validation-command "python -m pytest -q -m 'not seed_failure'" \
  --validation-command "python scripts/validate_demo_repair.py"
```

这条命令使用 MockLLMClient 生成一个 Web API 语义修复：保持 `unsafe_divide` 函数签名不变，但将 `denominator == 0` 转换为受控 `400 invalid_request` 响应，避免继续返回 `500 internal_server_error`。

验证通过时会看到类似：

```text
Status: PASS
Rolled back: False

Validation Result
- python -m pytest -q -m 'not seed_failure': 10 passed, 12 deselected
- python scripts/validate_demo_repair.py: demo repair validation passed
```

> 注意：这条命令会真正 apply patch。验证通过后文件会保留在修复后的状态；如果只是想保留 demo 的故障入口用于后续演示，可以在验证完正向闭环后恢复 `demo_service/app.py` 到故障种子版本。

如果希望保持日志监听模式，等待新的服务异常出现后再进入 apply 链路：

```bash
python scripts/apply_patch.py --proposal-json /tmp/fix_proposal.json --wait 30 --yes
```

默认 `--wait 30` 会从当前日志末尾开始等待 30 秒内的新 Traceback；如果希望也允许使用已有日志，可以加：

```bash
python scripts/apply_patch.py --proposal-json /tmp/fix_proposal.json --wait 30 --include-existing --yes
```

状态含义：

- `PREVIEW_ONLY`：安全审查和 dry-run 已跑完，但未传 `--yes`，未修改文件；
- `PASS`：patch 已应用，验证通过，文件保留在修复后的状态；
- `FAIL_ROLLED_BACK`：patch 曾经成功应用，但测试失败，脚本已回滚到 apply 前状态；
- `FAIL` / `ABORTED`：安全审查、dry-run、apply 或验证失败。

默认全量 pytest 仍会运行 `seed_failure` 测试，用来确认故障入口可复现；修复后验证请使用上面的 Repair Validation Profile。

## RecoveryAgent 单次运行入口

在 `scripts/apply_patch.py` 的基础上，当前已经新增 `RecoveryAgent` 封装：

```text
src/service_recovery_agent/recovery_agent.py
scripts/run_recovery_once.py
```

`RecoveryAgent.run_once()` 把之前散落在多个 CLI 中的主链路封装为一个结构化的一次性 Agent 执行：

```text
读取/等待 Traceback
  → 构建 CodeContext
  → 生成 FaultDiagnosis
  → 获取 FixProposal 或 patch 文件
  → Patch Safety Review
  → patch dry-run
  → 默认 PREVIEW_ONLY
  → --yes 后真正 apply
  → 运行 validation
  → 可选运行 contract / counterfactual / adversarial validation
  → 失败自动 rollback
  → 可选 bounded revise patch loop
  → 输出 RecoveryRunResult（包含 diagnosis / validation / contract / counterfactual / adversarial / failure_feedback）
```

默认只 preview，不修改文件：

```bash
python scripts/run_recovery_once.py --provider mock
```

如果要输出 JSON：

```bash
python scripts/run_recovery_once.py --provider mock --json
```

如果要把本次 RecoveryAgent 运行结果导出为飞书卡片 dry-run JSON（不会发送网络请求）：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --emit-feishu-card-json /tmp/recovery_card.json \
  --card-service-name demo-web-service \
  --card-repo Service-Recovery-Agent \
  --card-branch dry-run/feishu-card-demo
```

如果希望只把卡片 JSON 输出到 stdout，可传 `--emit-feishu-card-json -`。该模式会抑制普通人类可读报告，保持 stdout 是单一 JSON payload；为避免混淆，不能与 `--json` 同时使用。

如果要在 RecoveryAgent 完成修复/验证/决策之后，真实发送一张终态飞书卡片，可以显式传入：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --send-feishu-card \
  --feishu-card-json-out /tmp/terminal_recovery_card.json \
  --card-service-name demo-web-service
```

如果希望飞书发送失败也让 CLI 以专用非 0 退出码失败，可增加：

```bash
--require-feishu-notification
```

注意：`--send-feishu-card` 是终态通知层，应该在 apply / validation / contract / counterfactual / adversarial / revise loop 完成后使用；它不会在 patch 刚生成时伪装成 success card，也不会执行 Git/PR。

如果要真正应用并使用 Repair Validation Profile 验证：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --yes \
  --validation-command "python -m pytest -q -m 'not seed_failure'" \
  --validation-command "python scripts/validate_demo_repair.py"
```

如果要在普通验证通过后继续运行对抗验证门禁：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --yes \
  --validation-command "python -m pytest -q -m 'not seed_failure'" \
  --validation-command "python scripts/validate_demo_repair.py" \
  --run-adversarial-validation
```

如果要同时启用契约保持验证 / 反事实验证，可以增加：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --yes \
  --validation-command "python -m pytest -q -m 'not seed_failure'" \
  --validation-command "python scripts/validate_demo_repair.py" \
  --run-contract-validation \
  --run-counterfactual-validation \
  --run-adversarial-validation
```

如果希望 retryable failure 之后自动进行有限次数的下一轮修复规划，可以增加：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --yes \
  --validation-command "python -m pytest -q -m 'not seed_failure'" \
  --validation-command "python scripts/validate_demo_repair.py" \
  --run-adversarial-validation \
  --max-repair-attempts 2
```

`--max-repair-attempts` 默认是 `1`。只有在本轮 patch 产生 retryable `RepairFailureFeedback` 后，Agent 才会把该 feedback 注入下一轮 `propose_fix(..., failure_feedback=...)`；dry-run 失败发生在 apply 前，可在未修改工作区的前提下 retry；apply / validation / contract / counterfactual / adversarial 等 apply 后失败必须先回滚到 apply 前快照才会 retry。每一轮都会重新经过 safety review、dry-run、apply、validation，以及已启用的 contract / counterfactual / adversarial gates。
`RecoveryRunResult.repair_attempts` 会记录每一轮的 proposal、preview、apply、validation、contract、counterfactual、adversarial 和 feedback 摘要；最终成功时，上一轮失败反馈会保存在对应 attempt 的 `failure_feedback` 与下一轮 attempt 的 `used_failure_feedback` 中。

如果要使用比赛演示级一键自动修复模式，可以传入：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --auto-repair-demo
```

`--auto-repair-demo` 会自动展开为：

```text
--yes
--validation-command "python -m pytest -q -m 'not seed_failure'"
--validation-command "python scripts/validate_demo_repair.py"
--run-contract-validation
--run-counterfactual-validation
--run-adversarial-validation
--max-repair-attempts 2
--evaluate-decision
--send-feishu-card
--require-feishu-notification
```

也就是说，它会真正 apply patch、运行修复后验证 profile、运行 contract/counterfactual/adversarial gates、最多自动 revise 2 次，并在最终状态发送飞书终态卡片。该模式不会执行 Git/PR；如果 `.env` 中缺少飞书配置或飞书发送失败，CLI 会使用通知失败专用退出码。

如果要先查看未来 Git/PR 交付会如何 stage 文件，但不执行任何 Git 写操作，可以使用 Git delivery dry-run：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --git-delivery-dry-run \
  --use-git-worktree \
  --json
```

`--git-delivery-dry-run` 只生成 `GitDeliveryPlan`：

```text
不会创建 worktree
不会创建 branch
不会 git add
不会 commit
不会 push
不会创建 PR
```

它会根据已经通过 patch safety review 的 verified modified files 计算未来可 stage 的文件，并执行 denylist / secret scan 等安全检查；如果 `DecisionResult.action != CREATE_PR_READY`、验证未通过、缺少 stage files 或命中敏感文件，计划会标记为 `BLOCKED`。

`RecoveryAgent.run_once()` 默认还会只读运行 Git Diff Correlation，并把 evidence 注入 FixPlanner prompt 与 `RecoveryRunResult.git_diff_correlation`。CLI 可用以下参数控制：

```bash
python scripts/run_recovery_once.py --provider mock --no-git-diff-correlation
python scripts/run_recovery_once.py --provider mock --git-diff-max-commits 10
```

成功时会看到：

```text
Status: PASS
Can create PR: True
Validation Result: PASS
Adversarial Validation: PASS
```

如果对抗验证失败，`RecoveryAgent` 会将本次修复判为失败；默认会和普通验证失败一样恢复 apply 前的文件快照并设置 `can_create_pr=False`。
同时，`RecoveryRunResult.failure_feedback` 会生成下一轮修复可用的结构化反馈，例如：

```text
Patch passed the earlier repair pipeline stage but failed adversarial validation.
- probe: bug_shortcut
  request: GET /bug
  expected: status in [400, 422]; forbid status [500]
  actual: status 500, body {'error': 'internal_server_error', ...}

Please revise the patch so that all failed probes pass while preserving the previous repair constraints.
```

> 注意：`run_recovery_once.py --yes` 验证通过后会保留修复后的文件状态；如果还需要继续使用 demo 的故障种子入口，请在演示后恢复 `demo_service/app.py`。


## RecoveryAgent watch 模式

`RecoveryAgent.run_once()` 是单次执行入口；当前第一版 watch orchestration 已封装为：

```text
src/service_recovery_agent/recovery_watcher.py
tests/test_recovery_watcher.py
```

核心 API：

```python
from service_recovery_agent.recovery_watcher import RecoveryWatchConfig, watch_recovery_agent

result = watch_recovery_agent(
    agent,
    config=RecoveryWatchConfig(
        max_events=1,
        start_at_end=False,
        apply=False,  # 默认就是 False；watch 第一版默认 preview-only
    ),
)
```

第一版行为：

```text
1. 监听到 TracebackEvent 后调用 RecoveryAgent.run_once()；
2. 默认 apply=False，只 preview，不自动修改工作区；
3. 基于 exception_type、exception_message、crash file、line、function、code 生成稳定 fingerprint；
4. 相同 fingerprint 在 cooldown_seconds 内重复出现时跳过，避免同一错误反复触发；
5. max_events 支持测试和演示自动退出；
6. 可注入 event_source / clock / sleep，便于离线测试；
7. 没有 Git branch / commit / push / PR 行为；
8. 不真实发送飞书。
```

如果要在测试中绕过真实日志轮询，可以传入 `event_source=[TracebackEvent(...)]`；如果不传 `event_source`，watcher 会通过 `agent.config.resolved_log_path()` 或 `agent.config.log_path` 轮询日志文件。

> 注意：watch 第一版没有新增真实 CLI，也没有把 DecisionResult / Feishu card 放进长驻循环；这些属于后续增强。当前目标是安全地补齐“服务报错后自动触发单次 RecoveryAgent”的核心能力。

## 对抗性验证 Adversarial Validation Agent

在普通 pytest / repair validation 之外，当前新增了一个确定性对抗验证模块，并已接入 `RecoveryAgent.run_once(..., run_adversarial_validation=True)`：

```text
src/service_recovery_agent/adversarial_validator.py
scripts/adversarial_validate.py
tests/test_adversarial_validator.py
```

它会基于 `FaultDiagnosis` 生成一组固定 probe，并用 Flask test client 攻击当前 demo app：

| Probe | 请求 | 期望 | 目的 |
|---|---|---|---|
| `happy_path` | `/divide?x=4` | `200`, `result=25.0` | 正常路径不能被修坏 |
| `zero_boundary` | `/divide?x=0` | `400/422`，禁止 `500` | 核心除零修复必须生效 |
| `negative_denominator` | `/divide?x=-1` | `200`, `result=-100.0` | 负数不能被误判为非法 |
| `missing_query` | `/divide` | `200`, `result=100.0` | 默认 denominator 仍可用 |
| `bug_shortcut` | `/bug` | `400/422`，禁止 `500` | 另一个调用方也必须被覆盖 |
| `repeated_zero` | `/divide?x=0` 重复 3 次 | 每次 `400/422`，禁止 `500` | 重复触发时结果稳定 |

运行方式：

```bash
python scripts/adversarial_validate.py
python scripts/adversarial_validate.py --json
```

这个脚本会：

```text
读取/等待 Traceback
  → 构建 CodeContext
  → 生成 FaultDiagnosis
  → generate_probe_cases(diagnosis)
  → 针对当前 demo_service Flask app 运行 probes
  → 输出 AdversarialValidationResult
```

在原始 buggy app 上，`zero_boundary`、`bug_shortcut` 和 `repeated_zero` 应该失败，因此整体结果为 `FAIL`。在 mock patch 正确应用后，同一组 probes 应该全部通过并输出 `PASS`。这为后续反事实验证提供基础：

```text
buggy version 必须 FAIL
fixed version 必须 PASS
```

## 失败反馈与下一轮修复 Failure Feedback

当修复链路在 safety / dry-run / apply / validation / contract / counterfactual / adversarial 任一关键阶段失败时，系统现在不会只输出 `FAIL`，还会生成：

```text
RepairFailureFeedback
```

结构包括：

```python
failed_stage: str
failed_probes: list[str]
summary: str
prompt_feedback: str
details: dict[str, Any]
```

其中 `prompt_feedback` 是可以直接喂给下一轮修复规划的文本，包含：

- 失败 stage；
- 失败 check / command / probe 名称；
- safety finding 或 patch dry-run / apply 错误；
- validation / contract command、return code、stdout/stderr 截断摘要；
- adversarial / counterfactual 请求路径，例如 `GET /bug`；
- 期望状态码 / 禁止状态码、实际状态码和响应体；
- 针对下一轮 patch 的修复提示。

`FixPlanner` 也已经支持接收上一轮失败反馈：

```python
proposal = propose_fix(
    context,
    client,
    diagnosis=diagnosis,
    failure_feedback=result.failure_feedback,
)
```

当传入 `failure_feedback` 时，prompt 会新增：

```text
## Previous Repair Failure Feedback
```

并要求下一轮 patch 明确修复上一轮失败的 checks / commands / probes。

`RecoveryAgent.run_once()` 也已经支持有限次数自动 revise loop：

```python
result = agent.run_once(
    apply=True,
    run_adversarial_validation=True,
    max_repair_attempts=2,
)
```

约束：

- 默认 `max_repair_attempts=1`，不会自动多轮；
- 只有 LLM 生成的 patch 会自动 revise；如果传入固定 `--patch-file` / `--proposal-json`，Agent 不会擅自改用 LLM 继续重试；
- dry-run failure 可在 apply 前直接 retry，因为工作区未被修改；
- apply / validation / contract / counterfactual / adversarial failure 只有产生 `RepairFailureFeedback` 且已经 rollback 后，才会进入下一轮；
- safety review failure 只生成反馈，不自动 retry，避免绕过安全护栏；
- 每轮都必须重新通过 safety review、dry-run、apply、普通 validation，以及已启用的 contract / counterfactual / adversarial validation。

## PR 描述草稿 / 可逆性报告 Review Artifact

当前已新增一个不执行 Git/PR 操作的 review artifact builder：

```text
src/service_recovery_agent/review_artifact.py
tests/test_review_artifact.py
```

它可以把 `RecoveryRunResult` + `DecisionResult` 转换为未来 PR 可直接使用的 Markdown：

```python
from service_recovery_agent.review_artifact import build_review_artifact

artifact = build_review_artifact(
    recovery_result,
    decision=decision_result,
    commit_sha=None,
    branch=\"recovery/demo-fix\",
)

print(artifact.markdown)
```

Markdown 包含：

```text
Bug Summary
Root Cause
Fault Diagnosis
Fix Summary
Safety Review
Validation Evidence
Repair Attempts
Decision
Success Criteria
Failure Criteria
Rollback Plan
Artifact Safety Notes
```

同时提供结构化对象：

```text
RollbackPlan
ReviewCriteria
ReviewArtifact
```

注意：该 builder 只生成 Markdown / dry-run artifact，不会创建分支、不会 stage、不会 commit、不会 push、不会创建 GitHub PR。

## 验证飞书卡片

### 1. RecoveryResult dry-run 卡片 builder

当前 `feishu_cards.py` 可以把 `RecoveryRunResult` + `DecisionResult` 转成三类 Feishu interactive card JSON：

```python
from service_recovery_agent.feishu_cards import build_recovery_result_card

card = build_recovery_result_card(
    recovery_result,
    decision=decision_result,
    branch=\"recovery/demo-fix\",
    pr_url=None,
)
```

支持：

```text
success card
report-only card
failure / degraded card
```

其中 failure card 会展示：

```text
failed_stage
failed_probes
summary
rolled_back
下一步建议
```

这一步只返回 JSON，不读取 `.env`，不获取 tenant_access_token，不发送网络请求。

### 2. 只打印示例卡片 JSON，不发送

```bash
python scripts/check_feishu_card.py --dry-run
```

这一步用于确认卡片结构是否符合预期，不会调用飞书 API。

### 3. 从 RecoveryAgent 真实运行结果生成卡片 JSON，不发送

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --emit-feishu-card-json /tmp/recovery_card.json \
  --card-service-name demo-web-service \
  --card-repo Service-Recovery-Agent \
  --card-branch dry-run/feishu-card-demo
```

也可以先用发送脚本 dry-run 检查这份卡片：

```bash
python scripts/check_feishu_card.py \
  --from-card-json /tmp/recovery_card.json \
  --dry-run
```

### 4. 真实发送示例飞书卡片

```bash
python scripts/check_feishu_card.py
```

如果要真实发送上一步由 `run_recovery_once.py --emit-feishu-card-json` 生成的 RecoveryResult 卡片：

```bash
python scripts/check_feishu_card.py \
  --from-card-json /tmp/recovery_card.json
```

如果希望不经过 `check_feishu_card.py`，而是在 `run_recovery_once.py` 结束时直接发送终态卡片：

```bash
python scripts/run_recovery_once.py \
  --provider mock \
  --send-feishu-card \
  --feishu-card-json-out /tmp/terminal_recovery_card.json \
  --card-service-name demo-web-service
```

成功后会在终端看到类似：

```text
🎉 飞书卡片发送成功！请到飞书客户端查看。
message_id: ...
chat_id: ...
```

如果要发给群，请把 `.env` 中的：

```env
FEISHU_RECEIVE_ID_TYPE=chat_id
FEISHU_NOTICE_RECEIVER=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

如果要发给个人，通常使用：

```env
FEISHU_RECEIVE_ID_TYPE=open_id
FEISHU_NOTICE_RECEIVER=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Feishu 模块用法

```python
from service_recovery_agent.feishu import FeishuClient

client = FeishuClient.from_env(".env")

client.send_recovery_card(
    service_name="demo-web-service",
    bug_title="我发现了一个 Bug，并已为您生成修复，请 Review",
    error_summary="Traceback: ZeroDivisionError: division by zero",
    confidence="高",
    risk_level="中",
    test_result="pytest: 12 passed",
    pr_url="https://github.com/example/Service-Recovery-Agent/pull/1",
    rollback_command="git revert <commit_sha>",
)
```

## 飞书应用权限检查

飞书开放平台应用至少需要具备发送消息相关权限，并确保应用已经发布/生效。

常见排查点：

1. `.env` 中的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 是否正确；
2. `FEISHU_NOTICE_RECEIVER` 是否与 `FEISHU_RECEIVE_ID_TYPE` 匹配；
3. 发个人时是否使用用户 `open_id`；
4. 发群时是否使用群 `chat_id`；
5. 飞书应用是否已经拥有发送消息权限；
6. 接收者是否在应用可见范围内。

## 下一步开发计划

已完成：

1. `demo_service/`：提供一个可复现异常的 Flask 服务；
2. `log_watcher.py`：读取日志并支持等待新 Traceback；
3. `traceback_parser.py`：从日志中提取异常类型、文件、行号、调用栈；
4. `code_context.py`：读取崩溃源码、扫描调用方、评估影响域；
5. `llm_client.py`：支持 Doubao 2.0 / 火山方舟和 MockLLMClient；
6. `fault_diagnosis.py`：基于 CodeContext 生成确定性故障分类、AST crash expression evidence、故障变量、触发条件、变量流、修复约束和对抗提示；
7. `fix_planner.py`：基于 CodeContext + FaultDiagnosis + GitDiffCorrelationResult + SBFLResult（可选）+ HiddenInvariant evidence（可选）生成分析报告和 patch 草案；
8. `patch_safety.py`：对 patch 草案做 PASS/WARN/FAIL 安全审查；
9. `patcher.py`：对通过审查的 patch 草案执行 dry-run preview；
10. `patcher.py`：真正 apply patch，并支持 apply 前文件快照与失败回滚；
11. `validator.py`：运行修复后验证命令，默认执行 pytest；
12. `scripts/apply_patch.py`：串起“日志 Traceback → CodeContext → Patch Review → dry-run → apply → tests → rollback”；
13. `recovery_agent.py` + `scripts/run_recovery_once.py`：封装单次 RecoveryAgent 执行入口，输出结构化 `RecoveryRunResult`，包含 `diagnosis`；
14. `pytest.ini` + `scripts/validate_demo_repair.py`：拆分故障种子测试与修复后验证 profile，支持正向修复闭环；
15. `adversarial_validator.py` + `scripts/adversarial_validate.py`：生成并运行对抗 probe，输出 `AdversarialValidationResult`；
16. `RecoveryAgent.run_once(..., run_adversarial_validation=True)` + `scripts/run_recovery_once.py --run-adversarial-validation`：将对抗验证作为 apply 后质量门禁；
17. `contract_validator.py`：command-level before/after 契约保持验证；
18. `counterfactual_validator.py`：before_patch FAIL + after_patch PASS 反事实验证；
19. `decision.py`：结合 A/B/C/D/E、风险、置信度和各门禁结果输出决策；
20. `failure_feedback.py`：将 safety / dry-run / apply / validation / contract / counterfactual / adversarial 失败整理成分阶段 `RepairFailureFeedback`；
21. `RecoveryAgent.run_once(..., max_repair_attempts=N)` + `scripts/run_recovery_once.py --max-repair-attempts N`：支持有限次数自动 revise patch loop；
21.1. `scripts/run_recovery_once.py --auto-repair-demo`：一键启用 apply、repair validation profile、contract/counterfactual/adversarial gates、2 次 revise、Decision Engine 和终态飞书通知；不执行 Git/PR；
21.2. `git_safety.py` + `scripts/run_recovery_once.py --git-delivery-dry-run`：生成未来 Git/PR 交付计划，计算 verified stage files、worktree 路线、denylist 和 secret scan；dry-run only，不执行任何 Git 写操作；
22. `feishu_cards.py` + `scripts/run_recovery_once.py --emit-feishu-card-json`：把 `RecoveryRunResult` + `DecisionResult` 转成 success / report-only / failure 三类 dry-run 卡片 JSON，并可写入文件或输出到 stdout；
22.1. `feishu_notifier.py` + `scripts/run_recovery_once.py --send-feishu-card`：在 RecoveryAgent 完成修复/验证/决策后显式发送终态飞书卡片，并输出结构化 `FeishuNotificationResult`；
23. `review_artifact.py`：把 `RecoveryRunResult` + `DecisionResult` 转成未来 PR 可直接使用的 Markdown / 可逆性报告；
24. `recovery_watcher.py`：提供 preview-only watch 模式、traceback fingerprint 去重、cooldown、max_events；
25. `git_diff_correlation.py`：只读检查最近 Git diff，并输出 crash file / line / function correlation evidence；
26. Phase G.5：`GitDiffCorrelationResult` 已注入 `FixPlanner` prompt、`RecoveryAgent.run_once()` 和 `scripts/propose_fix.py`；
27. `sbfl.py`：支持 pytest nodeid / command 覆盖采集，使用 Ochiai 输出 top suspicious lines；`FixPlanner` 已支持 `sbfl_result` 可选注入；默认不接入 RecoveryAgent 主链路；
28. `invariant_analyzer.py`：从 return type / docstring / name / callers / tests 提取 Hidden Invariant candidates，并可作为 FixPlanner prompt evidence / review hint；不是 hard gate；
29. `feishu.py` + `scripts/check_feishu_card.py --from-card-json`：显式读取卡片 JSON 并发送飞书交互式修复通知；`run_recovery_once.py --send-feishu-card` 已支持主链路终态通知，但仍是显式 opt-in。

接下来建议按以下顺序推进：

1. CLI / 自动修复演示稳定性（修复 --auto-repair-demo 硬编码 python 的问题；捕获 validation command 的 FileNotFoundError；增加 --restore-demo-seed-after-run 演示辅助参数
）
2. Feishu 卡片与终态通知（区分“模拟 PR URL”和“真实 PR URL”；修复 rollback command 中 <mock_commit_sha> 的飞书渲染问题；Feishu 卡片增加真实交付状态区块；Feishu 失败降级通知需要更强；通知失败后的 fallback 策略）
3. Git worktree / commit / PR 真实交付（严守规则：必须使用 isolated worktree；不允许在当前 IDE 工作区 commit；不允许 git add .；只能 git add -- <stage_files>；stage_files 必须来自 Patch Safety Review；commit 前必须再次 secret scan staged diff；local commit 阶段不 push，不 PR。）
4. Validation / FailureFeedback / 失败降级（如果一直验证失败，降级策略？）
5. Doubao 2.0 真实 LLM Provider 端到端测试
6. Watch 模式自动闭环（当前 RecoveryWatcher 仍偏 preview-only；建议watch auto delivery）
7. 安全与敏感文件防护
8. 通用化能力与文档沉淀

## 安全策略方向

本项目不应让 LLM 直接无约束修改代码。拟实现以下门禁：

- 改动类型分级：纯增量改动、函数内修正、签名兼容重构、接口变更、跨文件改动；
- 影响域评估：被修改函数的调用方、传播深度、风险等级；
- 契约保持验证：修复前通过的测试，修复后必须仍然通过；
- 对抗性验证：边界值、反事实测试、重复调用、异常路径；
- 置信度决策：高置信度自动 PR，低置信度只发分析报告；
- PR 说明必须包含：原始错误、修复位置、测试结果、回滚命令、成功/失败标准。
