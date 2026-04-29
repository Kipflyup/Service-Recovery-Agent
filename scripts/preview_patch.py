#!/usr/bin/env python3
"""Preview a FixProposal patch draft with safety review + patch --dry-run."""

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
from service_recovery_agent.fix_planner import propose_fix  # noqa: E402
from service_recovery_agent.llm_client import LLMClientError, create_llm_client  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback, wait_for_traceback  # noqa: E402
from service_recovery_agent.patcher import (  # noqa: E402
    format_patch_preview_result,
    load_fix_proposal,
    preview_fix_proposal,
    preview_patch,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="预览 LLM patch 草案：安全审查 + patch --dry-run，不修改文件。"
    )
    parser.add_argument("--dotenv", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--provider", choices=["mock", "doubao"], help="不传则读取 LLM_PROVIDER。")
    parser.add_argument("--log-path", help="日志路径，默认读取 WEB_SERVICE_LOG_PATH 或 logs/app.log。")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--context-lines", type=int, default=6)
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--include-existing", action="store_true")
    parser.add_argument(
        "--proposal-json",
        help="读取 propose_fix.py --json 输出的 FixProposal JSON；推荐用法，避免重复调用 LLM。",
    )
    parser.add_argument(
        "--patch-file",
        help="直接预览指定 unified diff 文件；优先级高于 --proposal-json 和 LLM 生成。",
    )
    parser.add_argument(
        "--allow-file",
        action="append",
        default=[],
        help="额外允许修改的文件。默认只允许崩溃文件；可重复传入。",
    )
    parser.add_argument(
        "--strip",
        type=int,
        default=1,
        help="传给 patch 的 -p 层级。a/demo_service/app.py 这种路径通常使用 1。",
    )
    parser.add_argument(
        "--allow-warn",
        action="store_true",
        help="安全审查为 WARN 时仍继续 dry-run；FAIL 仍会阻止 dry-run。",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON。")
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="开启后非 PASS 或 dry-run 失败返回非 0。",
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
    allowed_files = [context.crash_file_relative, *args.allow_file]
    require_safety_pass = not args.allow_warn

    try:
        if args.patch_file:
            patch_text = Path(args.patch_file).read_text(encoding="utf-8")
            result = preview_patch(
                patch_text,
                context,
                project_root=args.project_root,
                allowed_files=allowed_files,
                strip=args.strip,
                require_safety_pass=require_safety_pass,
            )
        else:
            if args.proposal_json:
                proposal = load_fix_proposal(args.proposal_json)
            else:
                client = create_llm_client(provider=args.provider, dotenv_path=args.dotenv)
                proposal = propose_fix(context, client)

            result = preview_fix_proposal(
                proposal,
                context,
                project_root=args.project_root,
                allowed_files=allowed_files,
                strip=args.strip,
                require_safety_pass=require_safety_pass,
            )
    except LLMClientError as exc:
        print(f"LLM client error: {exc}", file=sys.stderr)
        print("如果只是本地验证链路，可运行：python scripts/preview_patch.py --provider mock", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_patch_preview_result(result))

    if args.strict_exit:
        if result.status != "PASS":
            return 20
        if result.dry_run is None or not result.dry_run.can_apply:
            return 21
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

