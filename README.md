# Demo Master

一个集成 AI 自动修复与飞书通知的智能开发助手项目。

## 项目概述

本项目包含两个模块：

1. **demo-master** - Java Spring Boot 应用
2. **FeiShuHttpPush** - Python AI Agent 服务

## 模块说明

### demo-master

Spring Boot 示例应用，包含异常捕获与 webhook 推送功能。

**技术栈：**
- Java 17
- Spring Boot 4.0.6
- Maven

**启动方式：**
```bash
cd demo-master
./mvnw spring-boot:run
```

应用默认运行在 `http://localhost:8080`

### FeiShuHttpPush

AI 自动修复服务，监听来自 Spring Boot 应用的错误信息，调用大模型分析并修复代码。

**技术栈：**
- Python 3.11+
- Flask
- 火山引擎 Ark 大模型（豆包 2.0-Pro）

**功能特性：**
- 接收 Java 应用的运行时异常
- 使用 AI 分析错误并生成修复代码
- 自动提交并推送 Git 更改
- 推送飞书卡片通知

**启动方式：**
```bash
cd FeiShuHttpPush
pip install -r requirements.txt
python send.py
```

服务默认运行在 `http://localhost:5000`，监听 `/api/error` 接口。

## 通信流程

```
┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│  Spring Boot    │         │   Flask API     │         │    豆包大模型    │
│  (端口 8080)    │ ──────► │  (端口 5000)    │ ──────► │   (Ark API)     │
│                 │ HTTP    │                 │         │                 │
└─────────────────┘ POST    └─────────────────┘         └─────────────────┘
                                      │
                                      │ Git Commit & Push
                                      ▼
                              ┌─────────────────┐
                              │     飞书群       │
                              │   卡片通知       │
                              └─────────────────┘
```

## 目录结构

```
demo-master/
├── FeiShuHttpPush/
│   ├── agent.py          # AI Agent 核心逻辑
│   └── send.py            # Flask API 服务入口
├── demo-master/
│   ├── src/
│   │   └── main/
│   │       ├── java/
│   │       │   └── com/example/demo/
│   │       │       └── Application.java
│   │       └── resources/
│   │           └── application.properties
│   ├── pom.xml
│   └── mvnw
└── README.md
```

## 注意事项

- 启动顺序：先启动 `FeiShuHttpPush`，再启动 `demo-master`
- 确保 Git 已配置且与远程仓库连接正常
- 飞书 Webhook 地址和 Ark API Key 需根据实际情况配置
