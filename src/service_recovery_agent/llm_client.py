"""LLM client abstractions for Service Recovery Agent.

The project currently targets Doubao 2.0 through Volcengine Ark.  A
``MockLLMClient`` is kept intentionally so local tests and demos can run without
network access or API spend.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

import requests
from dotenv import load_dotenv


DEFAULT_LLM_PROVIDER = "mock"
DEFAULT_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_DOUBAO_MODEL = "doubao-seed-2.0-code"
PLACEHOLDER_VALUES = {
    "",
    "your_doubao_api_key",
    "your_volcengine_ark_api_key",
    "your_api_key",
    "sk-xxxxxxxx",
}


class LLMClientError(RuntimeError):
    """Raised when an LLM client cannot be configured or called."""


class LLMClient(Protocol):
    """Minimal interface used by the fix planner."""

    provider: str
    model: str

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """Return model text for a single prompt."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str = DEFAULT_LLM_PROVIDER
    api_key: str | None = None
    base_url: str = DEFAULT_DOUBAO_BASE_URL
    model: str = DEFAULT_DOUBAO_MODEL
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    max_tokens: int = 4096
    max_retries: int = 1
    retry_backoff_seconds: float = 2.0

    @classmethod
    def from_env(
        cls,
        *,
        provider: str | None = None,
        dotenv_path: str | os.PathLike[str] | None = None,
    ) -> "LLMConfig":
        """Load LLM config from environment variables."""

        _load_dotenv(dotenv_path)
        selected_provider = (provider or os.getenv("LLM_PROVIDER") or DEFAULT_LLM_PROVIDER).strip().lower()

        if selected_provider in {"doubao", "ark", "volcengine", "volcengine-ark"}:
            raw_api_key = (
                os.getenv("DOUBAO_API_KEY")
                or os.getenv("ARK_API_KEY")
                or os.getenv("VOLCENGINE_API_KEY")
            )
            model = os.getenv("DOUBAO_MODEL") or os.getenv("ARK_MODEL") or DEFAULT_DOUBAO_MODEL
            return cls(
                provider="doubao",
                api_key=_normalize_secret(raw_api_key),
                base_url=(os.getenv("DOUBAO_BASE_URL") or os.getenv("ARK_BASE_URL") or DEFAULT_DOUBAO_BASE_URL).rstrip("/"),
                model=model.strip(),
                timeout_seconds=float(os.getenv("DOUBAO_TIMEOUT_SECONDS", "60")),
                temperature=float(os.getenv("DOUBAO_TEMPERATURE", "0.2")),
                max_tokens=int(os.getenv("DOUBAO_MAX_TOKENS", "4096")),
                max_retries=max(0, int(os.getenv("DOUBAO_MAX_RETRIES", "1"))),
                retry_backoff_seconds=max(0.0, float(os.getenv("DOUBAO_RETRY_BACKOFF_SECONDS", "2"))),
            )

        if selected_provider == "mock":
            return cls(provider="mock", model="mock-fix-planner")

        raise LLMClientError(
            f"Unsupported LLM_PROVIDER={selected_provider!r}. Supported providers: mock, doubao."
        )

    def to_safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data.get("api_key"):
            data["api_key"] = "***REDACTED***"
        return data


class DoubaoClient:
    """OpenAI-compatible Chat Completions client for Volcengine Ark."""

    provider = "doubao"

    def __init__(self, config: LLMConfig) -> None:
        if not config.api_key:
            raise LLMClientError(
                "Missing DOUBAO_API_KEY. Add it to .env or run with --provider mock for offline demos."
            )
        if not config.model:
            raise LLMClientError("Missing DOUBAO_MODEL. Use the Model ID / Endpoint ID from Volcengine Ark.")

        self.config = config
        self.model = config.model

    @classmethod
    def from_env(
        cls,
        *,
        dotenv_path: str | os.PathLike[str] | None = None,
    ) -> "DoubaoClient":
        return cls(LLMConfig.from_env(provider="doubao", dotenv_path=dotenv_path))

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        url = _chat_completions_url(self.config.base_url)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }

        response = self._post_with_timeout_retry(url, payload)

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMClientError(
                f"Doubao returned non-JSON response with HTTP {response.status_code}: {response.text[:500]}"
            ) from exc

        if not response.ok:
            if response.status_code == 429:
                raise LLMClientError(
                    "Doubao HTTP error 429 (rate limited, for example TPM exceeded). "
                    "Wait for the quota window to reset or reduce prompt/max_tokens; "
                    "the client does not immediately retry 429 to avoid worsening rate limits. "
                    f"Response: {json.dumps(data, ensure_ascii=False)[:1000]}"
                )
            raise LLMClientError(
                f"Doubao HTTP error {response.status_code}: {json.dumps(data, ensure_ascii=False)[:1000]}"
            )

        if data.get("error"):
            raise LLMClientError(f"Doubao API error: {json.dumps(data['error'], ensure_ascii=False)}")

        return _extract_chat_content(data)

    def _post_with_timeout_retry(self, url: str, payload: Mapping[str, Any]) -> requests.Response:
        attempts = self.config.max_retries + 1
        last_timeout: requests.Timeout | None = None

        for attempt_index in range(attempts):
            try:
                return requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
            except requests.Timeout as exc:
                last_timeout = exc
                if attempt_index >= self.config.max_retries:
                    break
                time.sleep(self.config.retry_backoff_seconds * (attempt_index + 1))
            except requests.RequestException as exc:
                raise LLMClientError(f"Failed to call Doubao / Volcengine Ark: {exc}") from exc

        raise LLMClientError(
            "Failed to call Doubao / Volcengine Ark after "
            f"{attempts} attempt(s) due to timeout: {last_timeout}"
        )


class MockLLMClient:
    """Deterministic offline LLM used by tests and demos."""

    provider = "mock"
    model = "mock-fix-planner"

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        # The mock response is intentionally shaped like the real LLM output we
        # ask for, so downstream parsing and reporting are exercised locally.
        return json.dumps(
            {
                "root_cause": (
                    "unsafe_divide 直接执行 numerator / denominator。"
                    "当 denominator 为 0 时，Python 抛出 ZeroDivisionError。"
                ),
                "fix_strategy": (
                    "保持 unsafe_divide 的函数签名和返回类型不变，在函数内部增加 denominator == 0 "
                    "的保护逻辑，并让路由层在后续 patch 阶段把该输入转换为明确的 400 响应。"
                ),
                "change_type": "B",
                "risk_level": "medium",
                "confidence": "high",
                "contract_constraints": [
                    "不要修改 unsafe_divide(numerator: float, denominator: float) -> float 的函数签名。",
                    "不要改变 /divide?x=4 等正常路径的计算结果。",
                    "不要大面积重构 create_app 或 Flask 路由注册方式。",
                    "优先使用函数内部兼容修复，并补充 denominator == 0 的回归测试。",
                ],
                "patch_draft": (
                    "--- a/demo_service/app.py\n"
                    "+++ b/demo_service/app.py\n"
                    "@@ -28,4 +28,7 @@ def unsafe_divide(numerator: float, denominator: float) -> float:\n"
                    "     # Intentional bug for the recovery demo:\n"
                    "     # denominator == 0 currently raises ZeroDivisionError.\n"
                    "+    if denominator == 0:\n"
                    "+        raise ValueError(\"denominator must not be zero\")\n"
                    "+\n"
                    "     return numerator / denominator\n"
                ),
                "tests_to_run": [
                    "pytest",
                    "curl \"http://127.0.0.1:5001/divide?x=4\"",
                    "curl \"http://127.0.0.1:5001/divide?x=0\"",
                ],
                "manual_review_notes": (
                    "这是 patch 草案，不会自动修改文件。真正应用前需要决定 denominator == 0 "
                    "应该在业务契约上返回 400、None，还是抛出 ValueError 并由路由层转换。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )


def create_llm_client(
    *,
    provider: str | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> LLMClient:
    """Create the configured LLM client."""

    config = LLMConfig.from_env(provider=provider, dotenv_path=dotenv_path)
    if config.provider == "mock":
        return MockLLMClient()
    if config.provider == "doubao":
        return DoubaoClient(config)
    raise LLMClientError(f"Unsupported provider: {config.provider}")


def _load_dotenv(dotenv_path: str | os.PathLike[str] | None) -> None:
    if dotenv_path is not None:
        load_dotenv(dotenv_path=dotenv_path)
        return

    default_dotenv = Path.cwd() / ".env"
    if default_dotenv.exists():
        load_dotenv(dotenv_path=default_dotenv)
    else:
        load_dotenv()


def _normalize_secret(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped in PLACEHOLDER_VALUES:
        return None
    return stripped


def _chat_completions_url(base_url: str) -> str:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/chat/completions"):
        return stripped
    return f"{stripped}/chat/completions"


def _extract_chat_content(data: Mapping[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMClientError(f"Doubao response did not include choices: {json.dumps(data, ensure_ascii=False)[:1000]}")

    first = choices[0]
    if not isinstance(first, Mapping):
        raise LLMClientError("Doubao response choices[0] is not an object.")

    message = first.get("message")
    if not isinstance(message, Mapping):
        raise LLMClientError("Doubao response choices[0].message is missing.")

    content = message.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)

    raise LLMClientError("Doubao response message.content is empty or unsupported.")
