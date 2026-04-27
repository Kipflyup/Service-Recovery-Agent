# FeiShuHttpPush

Service Recovery Agent — 自动接收 Java/Spring Boot 运行时异常，调用豆包大模型分析并修复代码，提交后通过飞书 webhook 推送修复结果卡片。

## 架构

- **send.py** — Flask HTTP 服务入口。监听 `POST /api/error`，接收异常信息后异步调用 agent 处理。
- **agent.py** — 核心逻辑。`SimpleDevAgent` 类封装了：
  - 调用火山引擎 ARK（豆包大模型）分析异常并生成修复代码
  - 通过 git 自动提交并推送修复
  - 通过飞书机器人 webhook 发送交互式卡片通知

## 运行

```bash
uv run python FeiShuHttpPush/send.py
```

服务启动在 `http://0.0.0.0:5000`。

## API

### POST /api/error

请求体 JSON：
```json
{
  "filePath": "src/main/java/com/example/Service.java",
  "errorMessage": "NullPointerException: ...",
  "stackTrace": "com.example.Service.line(Service.java:42)\n..."
}
```

## 关键依赖

- **flask** — HTTP 服务
- **requests** — 飞书 webhook 调用
- **volcengine-python-sdk[ark]** — 豆包大模型 API（火山引擎 ARK）

## 注意事项

- agent.py 中硬编码了 ARK API Key 和飞书 Webhook URL，生产环境应改为环境变量注入
- `PROJECT_ROOT` 路径当前指向 Windows 路径，跨平台使用时需修改
- git 操作依赖目标项目目录下的 git 配置
