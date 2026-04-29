from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from service_recovery_agent.llm_client import DoubaoClient, LLMClientError, LLMConfig  # noqa: E402


class FakeResponse:
    def __init__(self, *, status_code: int = 200, data: dict | None = None) -> None:
        self.status_code = status_code
        self._data = data or {
            "choices": [
                {
                    "message": {
                        "content": "ok",
                    }
                }
            ]
        }
        self.text = str(self._data)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._data


def test_doubao_retries_once_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.ReadTimeout("first timeout")
        return FakeResponse()

    monkeypatch.setattr("service_recovery_agent.llm_client.requests.post", fake_post)
    monkeypatch.setattr("service_recovery_agent.llm_client.time.sleep", lambda seconds: None)

    client = DoubaoClient(
        LLMConfig(
            provider="doubao",
            api_key="test-key",
            model="ep-test",
            max_retries=1,
            retry_backoff_seconds=0,
        )
    )

    assert client.complete("hello") == "ok"
    assert calls["count"] == 2


def test_doubao_timeout_exhaustion_reports_attempt_count(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(*args, **kwargs):
        calls["count"] += 1
        raise requests.ReadTimeout("timeout")

    monkeypatch.setattr("service_recovery_agent.llm_client.requests.post", fake_post)
    monkeypatch.setattr("service_recovery_agent.llm_client.time.sleep", lambda seconds: None)

    client = DoubaoClient(
        LLMConfig(
            provider="doubao",
            api_key="test-key",
            model="ep-test",
            max_retries=1,
            retry_backoff_seconds=0,
        )
    )

    with pytest.raises(LLMClientError, match=r"after 2 attempt"):
        client.complete("hello")

    assert calls["count"] == 2


def test_doubao_does_not_retry_429(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(*args, **kwargs):
        calls["count"] += 1
        return FakeResponse(
            status_code=429,
            data={
                "error": {
                    "code": "RateLimitExceeded.EndpointTPMExceeded",
                    "message": "TPM exceeded",
                }
            },
        )

    monkeypatch.setattr("service_recovery_agent.llm_client.requests.post", fake_post)

    client = DoubaoClient(
        LLMConfig(
            provider="doubao",
            api_key="test-key",
            model="ep-test",
            max_retries=1,
        )
    )

    with pytest.raises(LLMClientError, match="rate limited"):
        client.complete("hello")

    assert calls["count"] == 1

