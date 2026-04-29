#!/usr/bin/env python3
"""Safely apply a patch draft and run validation.

This script is the first command in the flow that can modify source files.  It
therefore requires ``--yes`` and applies only after:

1. Patch Safety Review is PASS;
2. patch --dry-run succeeds.

If validation fails, it restores modified files by default.
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
from service_recovery_agent.fix_planner import propose_fix  # noqa: E402
from service_recovery_agent.llm_client import LLMClientError, create_llm_client  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback, wait_for_traceback  # noqa: E402
from service_recovery_agent.patcher import (  # noqa: E402
    apply_patch_text,
    format_patch_preview_result,
    load_fix_proposal,
    preview_fix_proposal,
    preview_patch,
    restore_snapshot,
    snapshot_files,
)
from service_recovery_agent.validator import format_validation_result, validate_project  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="安全应用 patch 草案并运行验证；默认验证失败会回滚。"
    )
    parser.add_argument("--dotenv", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--provider", choices=["mock", "doubao"], help="不传则读取 LLM_PROVIDER。")
    parser.add_argument("--log-path", help="日志路径，默认读取 WEB_SERVICE_LOG_PATH 或 logs/app.log。")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--context-lines", type=int, default=6)
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--include-existing", action="store_true")
    parser.add_argument("--proposal-json", help="读取 propose_fix.py --json 输出的 FixProposal JSON。")
    parser.add_argument("--patch-file", help="直接应用指定 unified diff 文件。")
    parser.add_argument("--allow-file", action="append", default=[], help="额外允许修改的文件。")
    parser.add_argument("--strip", type=int, default=1)
    parser.add_argument("--validation-command", action="append", default=[], help="验证命令；可重复传入。默认 python -m pytest -q。")
    parser.add_argument("--validation-timeout", type=float, default=120.0)
    parser.add_argument("--skip-validation", action="store_true", help="跳过验证，不推荐。")
    parser.add_argument("--no-rollback-on-fail", action="store_true", help="验证失败时不自动回滚，不推荐。")
    parser.add_argument("--yes", action="store_true", help="确认真正修改文件。没有该参数时只输出 preview 并退出。")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON。")
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

    event = (
        wait_for_traceback(log_path, timeout_seconds=args.wait, start_at_end=not args.include_existing)
        if args.wait > 0
        else read_latest_traceback(log_path)
    )
    if event is None:
        print(f"no traceback found in {log_path}", file=sys.stderr)
        return 1

    context = build_code_context(event, project_root=args.project_root, context_lines=args.context_lines)
    allowed_files = [context.crash_file_relative, *args.allow_file]

    try:
        if args.patch_file:
            patch_text = Path(args.patch_file).read_text(encoding="utf-8")
            preview = preview_patch(
                patch_text,
                context,
                project_root=args.project_root,
                allowed_files=allowed_files,
                strip=args.strip,
            )
        else:
            if args.proposal_json:
                proposal = load_fix_proposal(args.proposal_json)
            else:
                client = create_llm_client(provider=args.provider, dotenv_path=args.dotenv)
                proposal = propose_fix(context, client)
            patch_text = proposal.patch_draft
            preview = preview_fix_proposal(
                proposal,
                context,
                project_root=args.project_root,
                allowed_files=allowed_files,
                strip=args.strip,
            )
    except LLMClientError as exc:
        print(f"LLM client error: {exc}", file=sys.stderr)
        print("如果只是本地验证链路，可运行：python scripts/apply_patch.py --provider mock --yes", file=sys.stderr)
        return 2

    if preview.status != "PASS" or preview.dry_run is None or not preview.dry_run.can_apply:
        return _emit(
            args,
            {
                "status": "ABORTED",
                "reason": "Patch did not pass safety review or dry-run.",
                "preview": preview.to_dict(),
            },
            "# Apply Patch\n\nAborted: Patch did not pass safety review or dry-run.\n\n"
            + format_patch_preview_result(preview),
            exit_code=20,
        )

    if not args.yes:
        return _emit(
            args,
            {
                "status": "PREVIEW_ONLY",
                "reason": "Pass --yes to actually apply the patch.",
                "preview": preview.to_dict(),
            },
            "# Apply Patch\n\nPreview only. Pass `--yes` to actually apply the patch.\n\n"
            + format_patch_preview_result(preview),
            exit_code=3,
        )

    snapshot = snapshot_files(args.project_root, preview.safety_review.modified_files)
    apply_result = apply_patch_text(patch_text, project_root=args.project_root, strip=args.strip)

    validation_result = None
    rolled_back = False
    if apply_result.applied and not args.skip_validation:
        validation_result = validate_project(
            project_root=args.project_root,
            commands=args.validation_command or None,
            timeout_seconds=args.validation_timeout,
        )
        if not validation_result.passed and not args.no_rollback_on_fail:
            restore_snapshot(args.project_root, snapshot)
            rolled_back = True

    status = "PASS"
    exit_code = 0
    if not apply_result.applied:
        status = "FAIL"
        exit_code = 21
        restore_snapshot(args.project_root, snapshot)
        rolled_back = True
    elif validation_result is not None and not validation_result.passed:
        status = "FAIL_ROLLED_BACK" if rolled_back else "FAIL"
        exit_code = 22

    payload = {
        "status": status,
        "rolled_back": rolled_back,
        "preview": preview.to_dict(),
        "apply": apply_result.to_dict(),
        "validation": validation_result.to_dict() if validation_result else None,
    }
    report = _format_apply_report(status, rolled_back, preview, apply_result, validation_result)
    return _emit(args, payload, report, exit_code=exit_code)


def _format_apply_report(status, rolled_back, preview, apply_result, validation_result) -> str:
    lines = ["# Apply Patch", "", f"- Status: **{status}**", f"- Rolled back: {rolled_back}", ""]
    lines.append(format_patch_preview_result(preview))
    lines.append("")
    lines.append("## Patch Apply")
    lines.append(f"- Applied: {apply_result.applied}")
    lines.append(f"- Return code: {apply_result.return_code}")
    lines.append(f"- Command: `{' '.join(apply_result.command)}`")
    if apply_result.stdout.strip():
        lines.extend(["", "stdout:", "```text", apply_result.stdout.rstrip(), "```"])
    if apply_result.stderr.strip():
        lines.extend(["", "stderr:", "```text", apply_result.stderr.rstrip(), "```"])
    if validation_result is not None:
        lines.extend(["", format_validation_result(validation_result)])
    else:
        lines.append("")
        lines.append("## Validation Result")
        lines.append("- Skipped.")
    return "\n".join(lines).rstrip()


def _emit(args, payload, report, *, exit_code: int) -> int:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

