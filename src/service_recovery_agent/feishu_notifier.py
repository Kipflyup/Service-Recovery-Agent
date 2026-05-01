"""Terminal Feishu notification helpers for recovery runs.

``feishu_cards`` is intentionally network-free: it only builds deterministic
interactive-card JSON.  This module is the explicit network boundary.  It sends
one terminal recovery card after the repair / validation / decision lifecycle
has completed.

The module does not run recovery, apply patches, create commits, push branches,
or create PRs.  Callers must pass an already-final ``RecoveryRunResult`` and
``DecisionResult``.  Sending is opt-in and failure is represented as a
structured result so CLI callers can choose whether notification failure should
fail the whole command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .decision import DecisionResult
from .feishu import FeishuAPIError, FeishuClient, FeishuConfigError
from .feishu_cards import (
    DEFAULT_SERVICE_NAME,
    build_recovery_result_card,
    classify_recovery_card,
)


@dataclass(frozen=True)
class FeishuNotificationResult:
    """Structured result for a terminal Feishu notification attempt."""

    attempted: bool
    sent: bool
    card_kind: str
    message_id: str | None = None
    chat_id: str | None = None
    error: str | None = None
    response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "card_kind": self.card_kind,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "error": self.error,
            "response": self.response,
        }


def send_recovery_result_card(
    result: Any,
    decision: DecisionResult,
    *,
    dotenv_path: str | Path | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    repo: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
    rollback_command: str | None = None,
    receiver_id: str | None = None,
    receive_id_type: str | None = None,
    client: Any | None = None,
) -> FeishuNotificationResult:
    """Build and send the terminal recovery card.

    Args:
        result: Final ``RecoveryRunResult`` or compatible object.  It should be
            produced after apply/validation/revise-loop has finished; this
            function does not run validators and must not be used as a
            pre-validation success notification.
        decision: Final delivery decision for ``result``.
        dotenv_path: Optional dotenv path used when constructing
            ``FeishuClient``.
        service_name/repo/branch/pr_url/rollback_command: Card metadata.
        receiver_id/receive_id_type: Optional runtime overrides for Feishu.
        client: Optional injected client for tests.  It must provide
            ``send_interactive_card(card, receiver_id=..., receive_id_type=...)``.
    """

    card = build_recovery_result_card(
        result,
        decision=decision,
        service_name=service_name,
        repo=repo,
        branch=branch,
        pr_url=pr_url,
        rollback_command=rollback_command,
    )
    card_kind = classify_recovery_card(result, decision=decision)

    try:
        actual_client = client if client is not None else FeishuClient.from_env(dotenv_path=dotenv_path)
        response = actual_client.send_interactive_card(
            card,
            receiver_id=receiver_id,
            receive_id_type=receive_id_type,
        )
    except FeishuConfigError as exc:
        return FeishuNotificationResult(
            attempted=True,
            sent=False,
            card_kind=card_kind,
            error=f"Feishu configuration error: {exc}",
        )
    except FeishuAPIError as exc:
        return FeishuNotificationResult(
            attempted=True,
            sent=False,
            card_kind=card_kind,
            error=f"Feishu API error: {exc}",
            response=dict(exc.response or {}),
        )
    except Exception as exc:  # pragma: no cover - defensive terminal boundary
        return FeishuNotificationResult(
            attempted=True,
            sent=False,
            card_kind=card_kind,
            error=f"Unexpected Feishu notification error ({type(exc).__name__}): {exc}",
        )

    data = _mapping(response.get("data")) if isinstance(response, Mapping) else {}
    return FeishuNotificationResult(
        attempted=True,
        sent=True,
        card_kind=card_kind,
        message_id=_string_or_none(data.get("message_id")),
        chat_id=_string_or_none(data.get("chat_id")),
        response=dict(response) if isinstance(response, Mapping) else {},
    )


def format_feishu_notification_result(result: FeishuNotificationResult) -> str:
    """Render a concise human-readable Feishu notification result."""

    lines = ["# Feishu Notification"]
    lines.append(f"- Attempted: {result.attempted}")
    lines.append(f"- Sent: {result.sent}")
    lines.append(f"- Card kind: {result.card_kind}")
    if result.message_id:
        lines.append(f"- Message ID: {result.message_id}")
    if result.chat_id:
        lines.append(f"- Chat ID: {result.chat_id}")
    if result.error:
        lines.append(f"- Error: {result.error}")
    return "\n".join(lines)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
