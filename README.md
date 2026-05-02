# Service Recovery Agent

一个面向服务故障的**受控自动修复原型**：从日志/Traceback 中定位错误，生成修复方案，经过安全审查、dry-run、验证和回滚门禁后，再决定是否进入交付/通知流程。

> 定位：`RecoveryAgent` 是有边界的修复编排 Agent；`Log_Collect_Tool` 是日志证据收集工具，不是开放式自治 Agent。

## 核心链路

```text
服务报错
  → Log_Collect_Tool 收集/筛选日志证据
  → Traceback / CodeContext / FaultDiagnosis
  → Doubao 生成修复规划，必要时使用确定性修复 profile
  → Patch Safety Review + dry-run
  → apply + validation + contract/counterfactual/adversarial gates
  → Decision Engine
  → 飞书终态卡片 / 后续人工 Review 或交付
```

## 目录速览

```text
demo_service/                 可复现故障的 Flask demo 服务
scripts/run_recovery_once.py   单次 RecoveryAgent 主入口
scripts/validate_demo_repair.py 修复后业务验证脚本
src/service_recovery_agent/    核心实现
tests/                         单元与集成测试
```


## 环境准备

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` 至少需要按场景配置：

```env
# Doubao / Volcengine Ark
LLM_PROVIDER=doubao
DOUBAO_API_KEY=...
DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
DOUBAO_MODEL=ep-xxxxxxxxxxxxxxxx

# Feishu
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
FEISHU_NOTICE_RECEIVER=...
FEISHU_RECEIVE_ID_TYPE=open_id  # 或 chat_id 等

# Demo log
WEB_SERVICE_LOG_PATH=./logs/app.log
```

注意：

- `DOUBAO_MODEL` 应使用火山方舟推理接入点 ID，通常是 `ep-...`，不要写页面展示名。
- 不要提交 `.env`、logs、venv、`.trae/`、飞书测试脚本、内部材料或任何密钥文件。

## 常用命令

### 1. 跑测试

```bash
PYTHONPATH=src venv/bin/python -m pytest -q
```

### 2. 启动 demo 服务并触发故障

```bash
python scripts/run_demo_service.py
curl "http://127.0.0.1:5001/divide?x=0"
```

故障会写入 `logs/app.log`，供 RecoveryAgent 读取。

### 3. 离线 preview，不改文件

```bash
PYTHONPATH=src venv/bin/python scripts/run_recovery_once.py \
  --provider mock \
  --run-log-agent \
  --recent-trace-count 3 \
  --trace-selection ranked \
  --evaluate-decision
```

### 4. 真实 Doubao + Feishu preview 验收

```bash
PYTHONPATH=src venv/bin/python scripts/run_recovery_once.py \
  --provider doubao \
  --run-log-agent \
  --recent-trace-count 3 \
  --trace-selection ranked \
  --evaluate-decision \
  --send-feishu-card \
  --require-feishu-notification \
  --card-service-name "Service Recovery Agent Live Acceptance" \
  --card-repo Service-Recovery-Agent \
  --json
```

### 5. 完整自动修复 demo

```bash
PYTHONPATH=src venv/bin/python scripts/run_recovery_once.py \
  --provider doubao \
  --auto-repair-demo \
  --run-log-agent \
  --recent-trace-count 3 \
  --trace-selection ranked \
  --json
```

`--auto-repair-demo` 会真实 apply patch、运行验证门禁并发送飞书终态卡片；不会 push、不会创建 PR。演示后如果还要保留预埋故障入口，请恢复 `demo_service/app.py`。

## 安全边界

本项目的原则是“能自动修，但不无约束自动交付”：

- LLM 不能自由选择任意工具；工具调用由 `RecoveryAgent` 和 `ToolPolicy` 控制。
- patch 必须通过静态安全审查和 dry-run 才能 apply。
- apply 前会 snapshot，验证失败会 rollback。
- `NaN`、`Infinity`、`float('inf')` 等特殊浮点修复会被拒绝。
- Git push / PR 不默认执行；如需 Git 交付，必须显式启用并使用 allowlist staging，不能 `git add .`。
- 飞书真实发送是显式 opt-in：需要 `--send-feishu-card`，验收场景建议加 `--require-feishu-notification`。

## 当前未完成的重点

- 真实 GitHub branch push / PR 创建仍未作为默认链路启用。
- PR 创建后的真实 URL / commit SHA 回填飞书卡片仍待接入。
- Watch 模式下的长期自动修复 + 通知 + 交付闭环仍需继续收敛。
- 更多故障类型的 deterministic repair profile 还可以继续扩展。

## 开发约定

- 修改前后优先跑目标测试；共享链路改动后跑全量 `pytest -q`。
- 保留 `MockLLMClient` 供离线测试，但不要用 mock 结果冒充真实 live acceptance。
- 不要提交/推送/PR，除非明确收到指令。
- 不要提交 `.env`、logs、venv、`.pytest_cache`、`__pycache__`、`.trae/`、内部材料或临时验收文件。
