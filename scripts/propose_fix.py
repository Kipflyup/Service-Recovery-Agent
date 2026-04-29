#!/usr/bin/env python3
"""Generate an LLM-backed fix proposal from the latest service traceback.

This script only produces an analysis report and patch draft.  It never applies
the patch and never modifies source files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.fix_planner import (  # noqa: E402
    build_fix_prompt,
    format_fix_proposal_report,
    propose_fix,
)
from service_recovery_agent.llm_client import LLMClientError, create_llm_client  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback, wait_for_traceback  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于最新 Traceback + CodeContext 生成 LLM 修复建议和 patch 草案。"
    )
    parser.add_argument(
        "--dotenv",
        default=str(PROJECT_ROOT / ".env"),
        help="环境变量文件路径，默认使用项目根目录 .env。",
    )
    parser.add_argument(
        "--provider",
        choices=["mock", "doubao"],
        help="LLM provider。默认读取 LLM_PROVIDER；离线演示可用 mock。",
    )
    parser.add_argument(
        "--log-path",
        help="日志路径。默认读取 WEB_SERVICE_LOG_PATH，若未配置则使用 logs/app.log。",
    )
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="项目根目录。",
    )
    parser.add_argument(
        "--context-lines",
        type=int,
        default=6,
        help="崩溃行上下文行数。",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help="等待新的 Traceback 秒数；0 表示直接读取当前最新 Traceback。",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="与 --wait 搭配使用：等待前也解析已有日志内容。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON。",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="只打印将发送给 LLM 的 prompt，不调用 LLM。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dotenv and Path(args.dotenv).exists():
        load_dotenv(dotenv_path=args.dotenv)

    log_path = Path(
        args.log_path
        or os.getenv("WEB_SERVICE_LOG_PATH")
        or PROJECT_ROOT / "logs" / "app.log"
    )

    if args.wait > 0:
        event = wait_for_traceback(
            log_path,
            timeout_seconds=args.wait,
            start_at_end=not args.include_existing,
        )
    else:
        event = read_latest_traceback(log_path)

    if event is None:
        print(f"no traceback found in {log_path}", file=sys.stderr)
        return 1

    context = build_code_context(
        event,
        project_root=args.project_root,
        context_lines=args.context_lines,
    )

    if args.print_prompt:
        print(build_fix_prompt(context))
        return 0

    try:
        client = create_llm_client(provider=args.provider, dotenv_path=args.dotenv)
        proposal = propose_fix(context, client)
    except LLMClientError as exc:
        print(f"LLM client error: {exc}", file=sys.stderr)
        print("如果只是本地验证链路，可运行：python scripts/propose_fix.py --provider mock", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_fix_proposal_report(proposal))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

