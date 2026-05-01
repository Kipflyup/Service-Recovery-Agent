# Demo Master

一个集成 AI 自动修复与飞书通知的智能开发助手项目。

## 项目概述

本项目包含两个模块：

1. **demo-master** - Java Spring Boot 应用
2. **FeiShuHttpPush** - Python AI Agent 服务

## 模块说明

### demo-master

Spring Boot 示例应用，使用 AOP 实现全局异常自动捕获与 webhook 推送。

**技术栈：**
- Java 8 (1.8.0_161)
- Spring Boot 2.7.18
- Maven

**启动方式：**
```bash
cd demo-master
./mvnw spring-boot:run -DskipTests
```

应用默认运行在 `http://localhost:5001`（可在 `application.properties` 中修改）

**测试接口：**
- `GET /test` - 触发 NullPointerException，测试 AOP 全局异常拦截

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
│  (端口 5001)    │ ──────► │  (端口 5000)    │ ──────► │   (Ark API)     │
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
│   │   ├── main/
│   │   │   ├── java/
│   │   │   │   └── com/example/demo/
│   │   │   │       ├── Application.java       # 主入口 + TestController
│   │   │   │       └── GlobalExceptionAspect.java  # AOP 全局异常切面
│   │   │   └── resources/
│   │   │       └── application.properties
│   │   └── test/
│   │       └── java/
│   │           └── com/example/demo/
│   │               └── DemoApplicationTests.java
│   ├── pom.xml
│   └── mvnw
└── README.md
```

## 关键实现

### AOP 全局异常拦截
- 使用 `@Around` 环绕通知拦截所有包内方法异常
- 从堆栈信息动态提取出错文件路径（支持内部类，自动去除 `$` 后缀）
- JSON 格式发送到 Agent 服务（端口 5000）

### 动态 filePath 提取
```java
// 处理内部类：去掉 $ 及其后面的部分
if (className.contains("$")) {
    className = className.substring(0, className.indexOf("$"));
}
```

## 注意事项

- 启动顺序：先启动 `FeiShuHttpPush`，再启动 `demo-master`
- 确保 Git 已配置且与远程仓库连接正常
- 飞书 Webhook 地址和 Ark API Key 需根据实际情况配置
