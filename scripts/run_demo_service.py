#!/usr/bin/env python3
"""Run the demo web service.

Example:
    python scripts/run_demo_service.py
    curl http://127.0.0.1:5001/divide?x=0
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动用于 Service Recovery Agent 演示的 Flask 服务。")
    parser.add_argument("--host", default=os.getenv("DEMO_SERVICE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DEMO_SERVICE_PORT", "5001")))
    parser.add_argument(
        "--log-path",
        default=os.getenv("WEB_SERVICE_LOG_PATH", str(PROJECT_ROOT / "logs" / "app.log")),
        help="服务异常日志路径，默认读取 WEB_SERVICE_LOG_PATH 或 logs/app.log。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=os.getenv("DEMO_SERVICE_DEBUG", "false").lower() in {"1", "true", "yes", "on"},
        help="开启 Flask debug 模式。演示自动修复链路时建议关闭。",
    )
    parser.add_argument(
        "--dotenv",
        default=str(PROJECT_ROOT / ".env"),
        help="环境变量文件路径，默认使用项目根目录 .env。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dotenv:
        load_dotenv(dotenv_path=args.dotenv)

    log_path = Path(args.log_path)
    app = create_app(log_path=log_path)

    base_url = f"http://{args.host}:{args.port}"
    print("=" * 72)
    print("Demo Web Service is starting")
    print("=" * 72)
    print(f"Health check : {base_url}/health")
    print(f"Happy path   : {base_url}/divide?x=4")
    print(f"Trigger bug  : {base_url}/divide?x=0")
    print(f"Bug shortcut : {base_url}/bug")
    print(f"Log path     : {log_path}")
    print("=" * 72)
    print("After triggering the bug, parse the latest traceback with:")
    print("PYTHONPATH=src python - <<'PY'")
    print("from service_recovery_agent.log_watcher import read_latest_traceback")
    print(f"event = read_latest_traceback({str(log_path)!r})")
    print("print(event.to_dict() if event else 'no traceback found')")
    print("PY")
    print("=" * 72)

    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
