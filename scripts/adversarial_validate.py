#!/usr/bin/env python3
"""Run deterministic adversarial validation probes for the demo service.

This script reads the latest traceback, builds CodeContext and FaultDiagnosis,
then runs diagnosis-derived probes against the current ``demo_service`` Flask
app.  It is intended to be run after a patch is applied, but on the original
buggy app it should fail, which proves the probes can distinguish buggy and
fixed behavior.
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

from service_recovery_agent.adversarial_validator import (  # noqa: E402
    format_adversarial_validation_result,
    validate_demo_app_after_repair,
)
from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.fault_diagnosis import diagnose_fault  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback, wait_for_traceback  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于最新 Traceback 的 FaultDiagnosis 生成并运行确定性对抗验证 probes。"
    )
    parser.add_argument(
        "--dotenv",
        default=str(PROJECT_ROOT / ".env"),
        help="环境变量文件路径，默认使用项目根目录 .env。",
    )
    parser.add_argument(
        "--log-path",
        help="用于读取原始 Traceback 的日志路径。默认读取 WEB_SERVICE_LOG_PATH，若未配置则使用 logs/app.log。",
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
        "--probe-log-path",
        default=str(PROJECT_ROOT / "logs" / "adversarial_validation.log"),
        help="运行 probes 时 demo app 写入的日志路径，默认 logs/adversarial_validation.log。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON。",
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
    diagnosis = diagnose_fault(context)
    result = validate_demo_app_after_repair(
        diagnosis,
        log_path=Path(args.probe_log_path),
    )

    if args.json:
        payload = {
            "diagnosis": diagnosis.to_dict(),
            "adversarial_validation": result.to_dict(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_adversarial_validation_result(result))

    return 0 if result.status == "PASS" else 20


if __name__ == "__main__":
    raise SystemExit(main())
