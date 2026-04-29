"""Feishu notification helpers. 正式的 Feishu 通知模块

This module intentionally uses Feishu's REST OpenAPI directly instead of
requiring the lark-oapi SDK.  The goal is to keep the first runnable demo small:

1. read app credentials from environment variables;
2. obtain a tenant_access_token;
3. send a text message or an interactive card.

Required environment variables:

- FEISHU_APP_ID
- FEISHU_APP_SECRET
- FEISHU_NOTICE_RECEIVER

Optional environment variables:

- FEISHU_RECEIVE_ID_TYPE: open_id, user_id, union_id, email, chat_id
- FEISHU_BASE_URL: defaults to https://open.feishu.cn
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import requests
from dotenv import load_dotenv


DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn"
DEFAULT_RECEIVE_ID_TYPE = "open_id"


class FeishuConfigError(RuntimeError):
    """Raised when local Feishu configuration is missing or invalid."""


class FeishuAPIError(RuntimeError):
    """Raised when Feishu OpenAPI returns an HTTP or business error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
        request_id: str | None = None,
        response: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.response = dict(response or {})


@dataclass(frozen=True)
class FeishuConfig:
    """Configuration needed to call Feishu OpenAPI."""

    app_id: str
    app_secret: str
    receiver_id: str
    receive_id_type: str = DEFAULT_RECEIVE_ID_TYPE
    base_url: str = DEFAULT_FEISHU_BASE_URL
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls, dotenv_path: str | os.PathLike[str] | None = None) -> "FeishuConfig":
        """Create config from environment variables. 

        Passing an explicit ``dotenv_path`` avoids python-dotenv's auto-discovery
        edge cases in some Python 3.14 stdin/repl executions and makes scripts
        deterministic.
        """

        if dotenv_path is not None:
            load_dotenv(dotenv_path=dotenv_path)
        else:
            default_dotenv = Path.cwd() / ".env"
            if default_dotenv.exists():
                load_dotenv(dotenv_path=default_dotenv)
            else:
                load_dotenv()

        app_id = _required_env("FEISHU_APP_ID")
        app_secret = _required_env("FEISHU_APP_SECRET")
        receiver_id = _required_env("FEISHU_NOTICE_RECEIVER")

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            receiver_id=receiver_id,
            receive_id_type=os.getenv("FEISHU_RECEIVE_ID_TYPE", DEFAULT_RECEIVE_ID_TYPE).strip(),
            base_url=os.getenv("FEISHU_BASE_URL", DEFAULT_FEISHU_BASE_URL).rstrip("/"),
            timeout_seconds=float(os.getenv("FEISHU_TIMEOUT_SECONDS", "15")),
        )


class FeishuClient:
    """Small Feishu OpenAPI client for notification messages."""

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self._tenant_access_token: str | None = None

    # 获取 tenant_access_token
    @classmethod
    def from_env(cls, dotenv_path: str | os.PathLike[str] | None = None) -> "FeishuClient":
        return cls(FeishuConfig.from_env(dotenv_path=dotenv_path))

    def get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a tenant_access_token, fetching it when needed."""

        if self._tenant_access_token and not force_refresh:
            return self._tenant_access_token

        url = f"{self.config.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret,
        }
        data = self._request("POST", url, json=payload, auth=False)

        token = data.get("tenant_access_token")
        if not token:
            raise FeishuAPIError("Feishu token response did not include tenant_access_token.", response=data)

        self._tenant_access_token = str(token)
        return self._tenant_access_token

    # 发送普通文本消息
    def send_text(
        self,
        text: str,
        *,
        receiver_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> dict[str, Any]:
        """Send a plain text message."""

        return self._send_message(
            msg_type="text",
            content={"text": text},
            receiver_id=receiver_id,
            receive_id_type=receive_id_type,
        )
    # 发送任意交互式卡片
    def send_interactive_card(
        self,
        card: Mapping[str, Any],
        *,
        receiver_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> dict[str, Any]:
        """Send a Feishu interactive card."""

        return self._send_message(
            msg_type="interactive",
            content=card,
            receiver_id=receiver_id,
            receive_id_type=receive_id_type,
        )

    # 发送标准 Service Recovery Agent 修复通知卡片
    def send_recovery_card(
        self,
        *,
        service_name: str = "Service Recovery Agent",
        bug_title: str = "我发现了一个 Bug 并已为您生成修复",
        error_summary: str = "检测到服务异常，Agent 已生成修复建议。",
        confidence: str = "高",
        risk_level: str = "中",
        test_result: str = "测试通过",
        pr_url: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        rollback_command: str | None = None,
        extra_notes: str | None = None,
        receiver_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> dict[str, Any]:
        """Build and send the standard Service Recovery notification card."""

        card = build_recovery_card(
            service_name=service_name,
            bug_title=bug_title,
            error_summary=error_summary,
            confidence=confidence,
            risk_level=risk_level,
            test_result=test_result,
            pr_url=pr_url,
            repo=repo,
            branch=branch,
            rollback_command=rollback_command,
            extra_notes=extra_notes,
        )
        return self.send_interactive_card(
            card,
            receiver_id=receiver_id,
            receive_id_type=receive_id_type,
        )

    def _send_message(
        self,
        *,
        msg_type: str,
        content: Mapping[str, Any],
        receiver_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> dict[str, Any]:
        actual_receiver = receiver_id or self.config.receiver_id
        actual_receive_id_type = receive_id_type or self.config.receive_id_type
        url = (
            f"{self.config.base_url}/open-apis/im/v1/messages"
            f"?receive_id_type={actual_receive_id_type}"
        )

        payload = {
            "receive_id": actual_receiver,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        token = self.get_tenant_access_token()
        return self._request("POST", url, json=payload, token=token)

    def _request(
        self,
        method: str,
        url: str,
        *,
        auth: bool = True,
        token: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if auth:
            headers["Authorization"] = f"Bearer {token or self.get_tenant_access_token()}"

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=self.config.timeout_seconds,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise FeishuAPIError(f"Failed to request Feishu OpenAPI: {exc}") from exc

        request_id = response.headers.get("X-Request-Id")
        try:
            data = response.json()
        except ValueError as exc:
            raise FeishuAPIError(
                "Feishu OpenAPI returned a non-JSON response.",
                status_code=response.status_code,
                request_id=request_id,
            ) from exc

        if not response.ok:
            raise FeishuAPIError(
                f"Feishu OpenAPI HTTP error: {response.status_code}",
                status_code=response.status_code,
                code=_safe_int(data.get("code")),
                request_id=request_id,
                response=data,
            )

        code = _safe_int(data.get("code"))
        if code not in (None, 0):
            msg = data.get("msg") or data.get("message") or "unknown error"
            raise FeishuAPIError(
                f"Feishu OpenAPI business error {code}: {msg}",
                status_code=response.status_code,
                code=code,
                request_id=request_id,
                response=data,
            )

        return data


def build_recovery_card(
    *,
    service_name: str = "Service Recovery Agent",
    bug_title: str = "我发现了一个 Bug 并已为您生成修复",
    error_summary: str = "检测到服务异常，Agent 已生成修复建议。",
    confidence: str = "高",
    risk_level: str = "中",
    test_result: str = "测试通过",
    pr_url: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    rollback_command: str | None = None,
    extra_notes: str | None = None,
) -> dict[str, Any]:
    """Build the standard Service Recovery Agent Feishu card payload."""

    header_template = _header_template_for_risk(risk_level)
    fields = [
        _field("服务", service_name),
        _field("风险等级", risk_level),
        _field("置信度", confidence),
        _field("验证结果", test_result),
    ]
    if repo:
        fields.append(_field("仓库", repo))
    if branch:
        fields.append(_field("分支", branch))

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{bug_title}**\n\n{error_summary}",
            },
        },
        {
            "tag": "div",
            "fields": fields,
        },
    ]

    if rollback_command:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**回滚方案：**\n`{rollback_command}`",
                    },
                },
            ]
        )

    if extra_notes:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**补充说明：**\n{extra_notes}",
                },
            }
        )

    if pr_url:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "查看 PR",
                            },
                            "type": "primary",
                            "url": pr_url,
                        }
                    ],
                },
            ]
        )

    return {
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "template": header_template,
            "title": {
                "tag": "plain_text",
                "content": "Service Recovery Agent 修复通知",
            },
        },
        "elements": elements,
    }


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise FeishuConfigError(f"Missing required environment variable: {name}")
    return value.strip()


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _field(label: str, value: str) -> dict[str, Any]:
    return {
        "is_short": True,
        "text": {
            "tag": "lark_md",
            "content": f"**{label}：**\n{value}",
        },
    }


def _header_template_for_risk(risk_level: str) -> str:
    risk = risk_level.lower()
    if any(word in risk for word in ("高", "high", "critical", "严重")):
        return "red"
    if any(word in risk for word in ("中", "medium", "warning")):
        return "orange"
    if any(word in risk for word in ("低", "low", "safe")):
        return "green"
    return "blue"

