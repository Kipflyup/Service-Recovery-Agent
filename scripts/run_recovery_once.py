#!/usr/bin/env python3
"""Run RecoveryAgent once from the latest service traceback.

This is the high-level Agent entrypoint for the current phase.  By default it
only previews the repair.  Pass ``--yes`` to actually apply the patch and run
validation.
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


AUTO_REPAIR_DEMO_VALIDATION_COMMANDS = [
    "python -m pytest -q -m 'not seed_failure'",
    "python scripts/validate_demo_repair.py",
]

from service_recovery_agent.llm_client import LLMClientError  # noqa: E402
from service_recovery_agent.decision import (  # noqa: E402
    evaluate_recovery_decision,
    format_decision_result,
)
from service_recovery_agent.feishu_cards import (  # noqa: E402
    build_recovery_result_card,
    classify_recovery_card,
)
from service_recovery_agent.feishu_notifier import (  # noqa: E402
    FeishuNotificationResult,
    format_feishu_notification_result,
    send_recovery_result_card,
)
from service_recovery_agent.git_safety import (  # noqa: E402
    DEFAULT_BASE_REF,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_WORKTREE_ROOT,
    GitDeliveryPlan,
    build_git_delivery_plan,
    format_git_delivery_plan,
)
from service_recovery_agent.recovery_agent import (  # noqa: E402
    RecoveryAgent,
    RecoveryAgentConfig,
    format_recovery_run_result,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行一次 RecoveryAgent：日志 → CodeContext → 修复建议 → 安全审查 → dry-run/apply → 验证。"
    )
    parser.add_argument("--dotenv", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--provider", choices=["mock", "doubao"], help="不传则读取 LLM_PROVIDER。")
    parser.add_argument("--log-path", help="日志路径，默认读取 WEB_SERVICE_LOG_PATH 或 logs/app.log。")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--context-lines", type=int, default=6)
    parser.add_argument("--wait", type=float, default=0.0, help="等待新 Traceback 秒数；0 表示读取当前最新 Traceback。")
    parser.add_argument("--include-existing", action="store_true", help="与 --wait 搭配：等待前也解析已有日志。")
    parser.add_argument("--proposal-json", help="读取 propose_fix.py --json 输出的 FixProposal JSON。")
    parser.add_argument("--patch-file", help="直接使用指定 unified diff 文件，优先级高于 proposal/LLM。")
    parser.add_argument("--allow-file", action="append", default=[], help="额外允许修改的文件；默认只允许崩溃文件。")
    parser.add_argument("--strip", type=int, default=1)
    parser.add_argument("--validation-command", action="append", default=[], help="验证命令；可重复传入。默认 python -m pytest -q。")
    parser.add_argument("--validation-timeout", type=float, default=120.0)
    parser.add_argument("--skip-validation", action="store_true", help="跳过验证，不推荐。")
    parser.add_argument("--no-rollback-on-fail", action="store_true", help="验证失败时不自动回滚，不推荐。")
    parser.add_argument(
        "--run-contract-validation",
        action="store_true",
        help="apply 前后运行同一组命令，确保修复前已通过的命令修复后不回归。",
    )
    parser.add_argument(
        "--contract-validation-command",
        action="append",
        default=[],
        help="契约保持验证命令；可重复传入。默认复用 --validation-command。",
    )
    parser.add_argument(
        "--run-adversarial-validation",
        action="store_true",
        help="apply 且普通验证通过后，运行 FaultDiagnosis 驱动的对抗 probe 作为额外质量门禁。",
    )
    parser.add_argument(
        "--run-counterfactual-validation",
        action="store_true",
        help="apply 前后分别运行对抗 probe，要求 before FAIL 且 after PASS。",
    )
    parser.add_argument(
        "--adversarial-log-path",
        default=str(PROJECT_ROOT / "logs" / "adversarial_validation.log"),
        help="运行对抗 probe 时 demo app 写入的日志路径。",
    )
    parser.add_argument(
        "--no-git-diff-correlation",
        action="store_true",
        help="不收集/注入只读 Git Diff Correlation evidence。默认会只读 git log/git show。",
    )
    parser.add_argument(
        "--git-diff-max-commits",
        type=int,
        default=5,
        help="Git Diff Correlation 检查最近多少个 commit；默认 5。",
    )
    parser.add_argument(
        "--max-repair-attempts",
        type=int,
        default=None,
        help=(
            "最多自动修复尝试次数。默认 1；大于 1 时，仅在 LLM 生成的 patch "
            "产生 RepairFailureFeedback 且已回滚后，才会用该反馈生成下一轮 patch。"
            "使用 --auto-repair-demo 且未显式传入时默认 2。"
        ),
    )
    parser.add_argument(
        "--auto-repair-demo",
        action="store_true",
        help=(
            "比赛演示一键自动修复模式：自动启用 apply、repair validation profile、"
            "contract/counterfactual/adversarial gates、2 次 revise、Decision Engine、"
            "终态飞书发送和通知成功要求。不会执行 Git/PR。"
        ),
    )
    parser.add_argument("--yes", action="store_true", help="确认真正修改文件。没有该参数时只 preview。")
    parser.add_argument(
        "--evaluate-decision",
        action="store_true",
        help="基于 RecoveryRunResult 运行 Decision Engine，输出 CREATE_PR_READY/REPORT_ONLY/BLOCKED 等决策。",
    )
    parser.add_argument(
        "--allow-auto-delivery-decision",
        action="store_true",
        help="仅影响 DecisionResult.can_auto_deliver；不会执行 git/PR/通知。",
    )
    parser.add_argument(
        "--emit-feishu-card-json",
        metavar="PATH_OR_-",
        help=(
            "将 RecoveryRunResult + DecisionResult 转成飞书交互式卡片 JSON。"
            "传文件路径则写入该文件；传 '-' 则只把卡片 JSON 输出到 stdout。"
            "该参数只做 dry-run JSON，不会真实发送网络请求。"
        ),
    )
    parser.add_argument(
        "--card-service-name",
        default="Service Recovery Agent",
        help="生成飞书卡片时展示的服务名。",
    )
    parser.add_argument("--card-repo", help="生成飞书卡片时展示的仓库名。")
    parser.add_argument("--card-branch", help="生成飞书卡片时展示的分支名。")
    parser.add_argument("--card-pr-url", help="生成飞书卡片时展示的未来 PR URL。")
    parser.add_argument(
        "--card-rollback-command",
        help="生成飞书卡片时展示的回滚命令；不会执行该命令。",
    )
    parser.add_argument(
        "--send-feishu-card",
        action="store_true",
        help=(
            "在 RecoveryAgent 完成修复/验证/决策后，真实发送一张终态飞书卡片。"
            "这是显式 opt-in；不会执行 Git/PR。"
        ),
    )
    parser.add_argument(
        "--require-feishu-notification",
        action="store_true",
        help="与 --send-feishu-card 搭配：飞书发送失败时用专用非 0 退出码标记整条链路失败。",
    )
    parser.add_argument(
        "--feishu-card-json-out",
        metavar="PATH_OR_-",
        help=(
            "额外把即将发送/已构造的终态飞书卡片 JSON 写入文件；传 '-' 输出到 stdout。"
            "不会单独触发发送，真实发送仍需 --send-feishu-card。"
        ),
    )
    parser.add_argument("--feishu-receiver-id", help="覆盖 FEISHU_NOTICE_RECEIVER。")
    parser.add_argument(
        "--feishu-receive-id-type",
        choices=["open_id", "user_id", "union_id", "email", "chat_id"],
        help="覆盖 FEISHU_RECEIVE_ID_TYPE。",
    )
    parser.add_argument(
        "--git-delivery-dry-run",
        action="store_true",
        help=(
            "只生成 GitDeliveryPlan，不创建 worktree/branch，不 git add，不 commit，不 push，不 PR。"
            "计划只使用已验证 patch / safety review 的文件范围。"
        ),
    )
    parser.add_argument("--git-base-ref", default=DEFAULT_BASE_REF, help="Git delivery plan 的 base ref，默认 main。")
    parser.add_argument("--git-branch-prefix", default=DEFAULT_BRANCH_PREFIX, help="Git delivery plan 的分支前缀。")
    parser.add_argument(
        "--git-worktree-root",
        default=str(DEFAULT_WORKTREE_ROOT),
        help="Git delivery plan 中展示的隔离 worktree 根目录；dry-run 不会创建该目录。",
    )
    parser.set_defaults(use_git_worktree=True)
    parser.add_argument(
        "--use-git-worktree",
        dest="use_git_worktree",
        action="store_true",
        help="Git delivery plan 使用 isolated worktree 路线（默认）。",
    )
    parser.add_argument(
        "--no-use-git-worktree",
        dest="use_git_worktree",
        action="store_false",
        help="Git delivery plan 不使用 worktree 路线；仅用于调试计划。",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON。")
    args = parser.parse_args()
    if args.auto_repair_demo:
        _apply_auto_repair_demo_defaults(args, parser)
    if args.max_repair_attempts is None:
        args.max_repair_attempts = 1
    if args.emit_feishu_card_json == "-" and args.json:
        parser.error("--emit-feishu-card-json - 不能与 --json 同时使用，避免 stdout 混合两个 JSON payload。")
    if args.feishu_card_json_out == "-" and args.json:
        parser.error("--feishu-card-json-out - 不能与 --json 同时使用，避免 stdout 混合两个 JSON payload。")
    if args.require_feishu_notification and not args.send_feishu_card:
        parser.error("--require-feishu-notification 需要同时传入 --send-feishu-card。")
    return args


def _apply_auto_repair_demo_defaults(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Expand ``--auto-repair-demo`` into the validated terminal-notification demo profile."""

    if args.skip_validation:
        parser.error("--auto-repair-demo 不能与 --skip-validation 搭配；演示模式必须保留验证。")
    if args.no_rollback_on_fail:
        parser.error("--auto-repair-demo 不能与 --no-rollback-on-fail 搭配；演示模式必须失败回滚。")

    args.yes = True
    args.run_contract_validation = True
    args.run_counterfactual_validation = True
    args.run_adversarial_validation = True
    args.evaluate_decision = True
    args.send_feishu_card = True
    args.require_feishu_notification = True
    if args.max_repair_attempts is None:
        args.max_repair_attempts = 2
    existing_commands = list(args.validation_command or [])
    args.validation_command = [
        *AUTO_REPAIR_DEMO_VALIDATION_COMMANDS,
        *(command for command in existing_commands if command not in AUTO_REPAIR_DEMO_VALIDATION_COMMANDS),
    ]


def main() -> int:
    args = parse_args()
    if args.dotenv and Path(args.dotenv).exists():
        load_dotenv(dotenv_path=args.dotenv)

    log_path = Path(
        args.log_path
        or os.getenv("WEB_SERVICE_LOG_PATH")
        or PROJECT_ROOT / "logs" / "app.log"
    )

    agent = RecoveryAgent(
        RecoveryAgentConfig(
            project_root=args.project_root,
            log_path=log_path,
            dotenv_path=args.dotenv,
            provider=args.provider,
            context_lines=args.context_lines,
            strip=args.strip,
        )
    )

    try:
        result = agent.run_once(
            wait_seconds=args.wait,
            include_existing=args.include_existing,
            proposal_json=args.proposal_json,
            patch_file=args.patch_file,
            allow_files=args.allow_file,
            apply=args.yes,
            validation_commands=args.validation_command or None,
            validation_timeout_seconds=args.validation_timeout,
            skip_validation=args.skip_validation,
            rollback_on_validation_fail=not args.no_rollback_on_fail,
            run_contract_validation=args.run_contract_validation,
            contract_validation_commands=args.contract_validation_command or None,
            run_adversarial_validation=args.run_adversarial_validation,
            run_counterfactual_validation=args.run_counterfactual_validation,
            adversarial_log_path=args.adversarial_log_path,
            run_git_diff_correlation=not args.no_git_diff_correlation,
            git_diff_max_commits=args.git_diff_max_commits,
            max_repair_attempts=args.max_repair_attempts,
        )
    except LLMClientError as exc:
        print(f"LLM client error: {exc}", file=sys.stderr)
        print("如果只是本地验证链路，可运行：python scripts/run_recovery_once.py --provider mock", file=sys.stderr)
        return 2

    decision = (
        evaluate_recovery_decision(
            result,
            allow_auto_delivery=args.allow_auto_delivery_decision,
        )
        if (
            args.evaluate_decision
            or args.emit_feishu_card_json
            or args.feishu_card_json_out
            or args.send_feishu_card
            or args.git_delivery_dry_run
        )
        else None
    )

    git_delivery_plan: GitDeliveryPlan | None = None
    if args.git_delivery_dry_run:
        git_delivery_plan = build_git_delivery_plan(
            result,
            decision,
            project_root=args.project_root,
            base_ref=args.git_base_ref,
            branch_prefix=args.git_branch_prefix,
            use_worktree=args.use_git_worktree,
            worktree_root=args.git_worktree_root,
            push=False,
            create_pr=False,
        )

    feishu_card: dict[str, object] | None = None
    card_output_targets = [
        target
        for target in (args.emit_feishu_card_json, args.feishu_card_json_out)
        if target
    ]
    if card_output_targets:
        feishu_card = build_recovery_result_card(
            result,
            decision=decision,
            service_name=args.card_service_name,
            repo=args.card_repo,
            branch=args.card_branch,
            pr_url=args.card_pr_url,
            rollback_command=args.card_rollback_command,
        )
        for target in card_output_targets:
            _write_json_or_stdout(feishu_card, target)

    notification: FeishuNotificationResult | None = None
    if args.send_feishu_card:
        notification = send_recovery_result_card(
            result,
            decision,
            dotenv_path=args.dotenv,
            service_name=args.card_service_name,
            repo=args.card_repo,
            branch=args.card_branch,
            pr_url=args.card_pr_url,
            rollback_command=args.card_rollback_command,
            receiver_id=args.feishu_receiver_id,
            receive_id_type=args.feishu_receive_id_type,
        )

    if args.emit_feishu_card_json == "-" or args.feishu_card_json_out == "-":
        # Keep stdout machine-readable: when the caller explicitly asks for
        # card JSON on stdout, do not append the human RecoveryRunResult report.
        return _final_exit_code(result.status, notification, require_notification=args.require_feishu_notification)

    if args.json:
        payload = result.to_dict()
        if decision is not None:
            payload = {
                "recovery": payload,
                "decision": decision.to_dict(),
            }
        if args.emit_feishu_card_json:
            payload = {
                **payload,
                "feishu_card": {
                    "kind": classify_recovery_card(result, decision=decision),
                    "json_path": args.emit_feishu_card_json,
                },
            }
        if args.feishu_card_json_out:
            existing = payload.get("feishu_card") if isinstance(payload, dict) else None
            payload = {
                **payload,
                "feishu_card": {
                    **(existing if isinstance(existing, dict) else {}),
                    "kind": classify_recovery_card(result, decision=decision),
                    "json_out_path": args.feishu_card_json_out,
                },
            }
        if notification is not None:
            payload = {
                **payload,
                "feishu_notification": notification.to_dict(),
            }
        if git_delivery_plan is not None:
            payload = {
                **payload,
                "git_delivery_plan": git_delivery_plan.to_dict(),
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_recovery_run_result(result))
        if decision is not None:
            print()
            print(format_decision_result(decision))
        if git_delivery_plan is not None:
            print()
            print(format_git_delivery_plan(git_delivery_plan))
        if args.emit_feishu_card_json:
            print()
            print(
                "Feishu card dry-run JSON written to "
                f"{args.emit_feishu_card_json} "
                f"(kind={classify_recovery_card(result, decision=decision)}; not sent)."
            )
        if args.feishu_card_json_out:
            print()
            print(
                "Feishu terminal card JSON written to "
                f"{args.feishu_card_json_out} "
                f"(kind={classify_recovery_card(result, decision=decision)})."
            )
        if notification is not None:
            print()
            print(format_feishu_notification_result(notification))

    return _final_exit_code(result.status, notification, require_notification=args.require_feishu_notification)


def _write_json_or_stdout(payload: object, target: str) -> None:
    """Write deterministic JSON to ``target`` or stdout when target is ``-``."""

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if target == "-":
        print(text)
        return

    output_path = Path(target)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")


def _exit_code_for_status(status: str) -> int:
    return {
        "PASS": 0,
        "NO_TRACEBACK": 1,
        "PREVIEW_ONLY": 3,
        "ABORTED": 20,
        "FAIL": 21,
        "FAIL_ROLLED_BACK": 22,
        "FAIL_ADVERSARIAL": 23,
    }.get(status, 1)


def _final_exit_code(
    status: str,
    notification: FeishuNotificationResult | None,
    *,
    require_notification: bool,
) -> int:
    if require_notification and (notification is None or not notification.sent):
        return 31
    return _exit_code_for_status(status)


if __name__ == "__main__":
    raise SystemExit(main())
