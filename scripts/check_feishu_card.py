#!/usr/bin/env python3
"""Send a sample Service Recovery Agent card to Feishu.用于真实验证飞书卡片链路。

Usage:
    python scripts/check_feishu_card.py --dry-run   只打印卡片 JSON, 不发送
    python scripts/check_feishu_card.py   真实发送飞书卡片
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from service_recovery_agent.feishu import (  # noqa: E402
    FeishuAPIError,
    FeishuClient,
    FeishuConfigError,
    build_recovery_card,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发送一张飞书交互式卡片，用于验证通知链路。")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印卡片 JSON，不真实发送飞书消息。",
    )
    parser.add_argument(
        "--dotenv",
        default=str(PROJECT_ROOT / ".env"),
        help="环境变量文件路径，默认使用项目根目录 .env。",
    )
    parser.add_argument("--receiver-id", help="覆盖 FEISHU_NOTICE_RECEIVER。")
    parser.add_argument(
        "--receive-id-type",
        choices=["open_id", "user_id", "union_id", "email", "chat_id"],
        help="覆盖 FEISHU_RECEIVE_ID_TYPE；发给个人常用 open_id，发给群常用 chat_id。",
    )
    parser.add_argument("--service-name", default="demo-web-service")
    parser.add_argument(
        "--bug-title",
        default="我发现了一个 Bug，并已为您生成修复，请 Review",
    )
    parser.add_argument(
        "--error-summary",
        default="Traceback: ZeroDivisionError: division by zero。Agent 已定位到 demo_service/app.py 并生成候选修复。",
    )
    parser.add_argument("--confidence", default="高")
    parser.add_argument("--risk-level", default="中")
    parser.add_argument("--test-result", default="pytest: 12 passed")
    parser.add_argument("--repo", default="Service-Recovery-Agent")
    parser.add_argument("--branch", default="agent/fix-zero-division")
    parser.add_argument("--pr-url", default="https://github.com/example/Service-Recovery-Agent/pull/1")
    parser.add_argument("--rollback-command", default="git revert <commit_sha>")
    parser.add_argument(
        "--extra-notes",
        default="这是卡片链路验证消息，不代表真实代码已经被修复。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    card = build_recovery_card(
        service_name=args.service_name,
        bug_title=args.bug_title,
        error_summary=args.error_summary,
        confidence=args.confidence,
        risk_level=args.risk_level,
        test_result=args.test_result,
        pr_url=args.pr_url,
        repo=args.repo,
        branch=args.branch,
        rollback_command=args.rollback_command,
        extra_notes=args.extra_notes,
    )

    if args.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    try:
        client = FeishuClient.from_env(dotenv_path=args.dotenv)
        result = client.send_interactive_card(
            card,
            receiver_id=args.receiver_id,
            receive_id_type=args.receive_id_type,
        )
    except FeishuConfigError as exc:
        print(f"❌ 飞书配置错误：{exc}", file=sys.stderr)
        print("请检查 .env 中的 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_NOTICE_RECEIVER。", file=sys.stderr)
        return 2
    except FeishuAPIError as exc:
        print(f"❌ 飞书 API 调用失败：{exc}", file=sys.stderr)
        if exc.request_id:
            print(f"request_id: {exc.request_id}", file=sys.stderr)
        if exc.response:
            print(json.dumps(exc.response, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3

    data = result.get("data", {})
    print("🎉 飞书卡片发送成功！请到飞书客户端查看。")
    if data.get("message_id"):
        print(f"message_id: {data['message_id']}")
    if data.get("chat_id"):
        print(f"chat_id: {data['chat_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

