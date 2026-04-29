#!/usr/bin/env python3
"""Review a FixProposal patch draft without modifying files."""

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
from service_recovery_agent.fix_planner import FixProposal, propose_fix  # noqa: E402
from service_recovery_agent.llm_client import LLMClientError, create_llm_client  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback, wait_for_traceback  # noqa: E402
from service_recovery_agent.patch_safety import (  # noqa: E402
    format_patch_safety_review,
    review_fix_proposal,
    review_patch_safety,
    review_to_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="审查 LLM patch 草案安全性，只输出 PASS/WARN/FAIL，不修改文件。"
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
        help="读取 propose_fix.py --json 输出的 FixProposal JSON；若不传则现场调用 LLM 生成。",
    )
    parser.add_argument(
        "--patch-file",
        help="直接审查指定 unified diff 文件；优先级高于 --proposal-json 和 LLM 生成。",
    )
    parser.add_argument(
        "--allow-file",
        action="append",
        default=[],
        help="额外允许修改的文件。默认只允许崩溃文件；可重复传入。",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON。")
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="开启后 WARN 返回 10，FAIL 返回 20；默认只在运行错误时非 0。",
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

    if args.patch_file:
        patch_text = Path(args.patch_file).read_text(encoding="utf-8")
        review = review_patch_safety(patch_text, context, allowed_files=allowed_files)
    else:
        try:
            if args.proposal_json:
                proposal = _load_proposal(args.proposal_json)
            else:
                client = create_llm_client(provider=args.provider, dotenv_path=args.dotenv)
                proposal = propose_fix(context, client)
        except LLMClientError as exc:
            print(f"LLM client error: {exc}", file=sys.stderr)
            print("如果只是本地验证链路，可运行：python scripts/review_patch.py --provider mock", file=sys.stderr)
            return 2

        review = review_fix_proposal(proposal, context, allowed_files=allowed_files)

    if args.json:
        print(review_to_json(review))
    else:
        print(format_patch_safety_review(review))

    if args.strict_exit:
        if review.status == "FAIL":
            return 20
        if review.status == "WARN":
            return 10
    return 0


def _load_proposal(path: str) -> FixProposal:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return FixProposal(
        root_cause=str(data.get("root_cause", "")),
        fix_strategy=str(data.get("fix_strategy", "")),
        change_type=str(data.get("change_type", "")),
        risk_level=str(data.get("risk_level", "")),
        confidence=str(data.get("confidence", "")),
        contract_constraints=[str(item) for item in data.get("contract_constraints", [])],
        patch_draft=str(data.get("patch_draft", "")),
        tests_to_run=[str(item) for item in data.get("tests_to_run", [])],
        manual_review_notes=str(data.get("manual_review_notes", "")),
        raw_response=str(data.get("raw_response", "")),
        provider=str(data.get("provider", "")),
        model=str(data.get("model", "")),
    )


if __name__ == "__main__":
    raise SystemExit(main())
