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

当前仓库已先补齐最小工程骨架，并优先打通 **飞书交互式卡片通知** 这一环节。

## 当前状态

已具备：

- 飞书 OpenAPI 配置读取；
- 获取 `tenant_access_token`；
- 发送普通文本消息的旧测试脚本；
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

尚未完成：

- GitHub PR 创建；
- 将最终修复结果、测试结果、PR 链接整合成飞书卡片。

## 项目结构

```text
.
├── README.md
├── Plan.md
├── requirements.txt
├── .env.example
├── demo_service/
│   ├── app.py
│   └── tests/
│       └── test_app.py
├── scripts/
│   ├── check_feishu_card.py
│   ├── apply_patch.py
│   ├── inspect_latest_error.py
│   ├── preview_patch.py
│   ├── propose_fix.py
│   ├── review_patch.py
│   └── run_demo_service.py
├── src/
│   └── service_recovery_agent/
│       ├── __init__.py
│       ├── code_context.py
│       ├── feishu.py
│       ├── fix_planner.py
│       ├── llm_client.py
│       ├── log_watcher.py
│       ├── patcher.py
│       ├── patch_safety.py
│       ├── traceback_parser.py
│       └── validator.py
├── tests/
│   ├── test_code_context.py
│   ├── test_fix_planner.py
│   ├── test_llm_client.py
│   ├── test_patcher.py
│   ├── test_patch_safety.py
│   └── test_validator.py
├── test_feishu_all.py
├── test_feishu_requests.py
└── 飞书ai自动修复课题要求.md
```

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
CodeContext → LLM 修复建议 → patch 草案
```

如需查看发送给模型的 prompt：

```bash
python scripts/propose_fix.py --print-prompt
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

当前 demo 的离线 mock patch 会把 `ZeroDivisionError` 改成 `ValueError`。它能通过安全审查和 dry-run，但默认全量 pytest 会失败，因为现有 demo 测试刻意断言“服务仍会产生 ZeroDivisionError Traceback”来保证后续 Agent 链路有稳定错误入口。这时脚本会输出 `FAIL_ROLLED_BACK`，说明“应用 + 测试 + 回滚”门禁生效。

## 验证飞书卡片

### 1. 只打印卡片 JSON，不发送

```bash
python scripts/check_feishu_card.py --dry-run
```

这一步用于确认卡片结构是否符合预期，不会调用飞书 API。

### 2. 真实发送飞书卡片

```bash
python scripts/check_feishu_card.py
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
6. `fix_planner.py`：基于 CodeContext 生成分析报告和 patch 草案；
7. `patch_safety.py`：对 patch 草案做 PASS/WARN/FAIL 安全审查；
8. `patcher.py`：对通过审查的 patch 草案执行 dry-run preview；
9. `patcher.py`：真正 apply patch，并支持 apply 前文件快照与失败回滚；
10. `validator.py`：运行修复后验证命令，默认执行 pytest；
11. `scripts/apply_patch.py`：串起“日志 Traceback → CodeContext → Patch Review → dry-run → apply → tests → rollback”；
12. `feishu.py`：发送飞书交互式修复通知卡片。

接下来建议按以下顺序推进：

1. 增加 `github_client.py`，创建分支、提交 commit、打开 PR；
2. 将 Traceback 摘要、CodeContext、Patch Safety Review、Patch Preview、验证结果、PR URL 通过 `feishu.py` 发成卡片；
3. 把 `scripts/apply_patch.py` 封装成更完整的日志监听 worker，让它持续监听服务日志并按策略触发修复链路；
4. 拆分 demo 测试：把“故障入口稳定性测试”和“修复后业务回归测试”分成不同验证 profile。

## 安全策略方向

本项目后续不应让 LLM 直接无约束修改代码。建议实现以下门禁：

- 改动类型分级：纯增量改动、函数内修正、签名兼容重构、接口变更、跨文件改动；
- 影响域评估：被修改函数的调用方、传播深度、风险等级；
- 契约保持验证：修复前通过的测试，修复后必须仍然通过；
- 对抗性验证：边界值、反事实测试、重复调用、异常路径；
- 置信度决策：高置信度自动 PR，低置信度只发分析报告；
- PR 说明必须包含：原始错误、修复位置、测试结果、回滚命令、成功/失败标准。
