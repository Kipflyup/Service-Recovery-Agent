from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402


def _seed_traceback_log(log_path: Path) -> None:
    app = create_app(log_path=log_path, testing=True)

    response = app.test_client().get("/divide?x=0")

    assert response.status_code == 500
    for handler in app.logger.handlers:
        handler.flush()
    assert "Traceback (most recent call last):" in log_path.read_text(encoding="utf-8")


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    for name in list(env):
        if name.startswith("FEISHU_"):
            env.pop(name)
    return env


@pytest.mark.seed_failure
def test_run_recovery_once_emits_feishu_card_json_file(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    card_path = tmp_path / "cards" / "recovery_card.json"
    _seed_traceback_log(log_path)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_recovery_once.py",
            "--provider",
            "mock",
            "--log-path",
            str(log_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--emit-feishu-card-json",
            str(card_path),
            "--card-service-name",
            "cli-demo-service",
            "--card-repo",
            "Service-Recovery-Agent",
            "--card-branch",
            "dry-run/recovery-card",
        ],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 3, completed.stderr + completed.stdout
    assert card_path.exists()

    card = json.loads(card_path.read_text(encoding="utf-8"))
    card_text = json.dumps(card, ensure_ascii=False)
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["template"] == "yellow"
    assert card["header"]["title"]["content"] == "Service Recovery Agent：发现问题，当前仅建议人工 Review"
    assert "cli-demo-service" in card_text
    assert "Service-Recovery-Agent" in card_text
    assert "dry-run/recovery-card" in card_text
    assert "REPORT_ONLY" in card_text
    assert "Feishu card dry-run JSON written to" in completed.stdout
    assert "not sent" in completed.stdout


def test_check_feishu_card_dry_run_can_print_card_json_file(tmp_path: Path) -> None:
    card_path = tmp_path / "card.json"
    card_path.write_text(
        json.dumps(
            {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "yellow",
                    "title": {"tag": "plain_text", "content": "dry-run card"},
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "**hello**"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_feishu_card.py",
            "--from-card-json",
            str(card_path),
            "--dry-run",
        ],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    printed = json.loads(completed.stdout)
    assert printed["header"]["title"]["content"] == "dry-run card"
    assert printed["elements"][0]["text"]["content"] == "**hello**"


@pytest.mark.seed_failure
def test_run_recovery_once_send_feishu_card_is_terminal_opt_in_without_required_notification(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    card_path = tmp_path / "terminal_card.json"
    _seed_traceback_log(log_path)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_recovery_once.py",
            "--provider",
            "mock",
            "--dotenv",
            str(tmp_path / "missing.env"),
            "--log-path",
            str(log_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--send-feishu-card",
            "--feishu-card-json-out",
            str(card_path),
            "--card-service-name",
            "terminal-demo-service",
        ],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 3, completed.stderr + completed.stdout
    assert card_path.exists()
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["header"]["template"] == "yellow"
    assert "# Feishu Notification" in completed.stdout
    assert "Sent: False" in completed.stdout
    assert "Feishu configuration error" in completed.stdout
    assert "terminal-demo-service" in json.dumps(card, ensure_ascii=False)


def test_run_recovery_once_require_feishu_notification_uses_dedicated_exit_code(tmp_path: Path) -> None:
    log_path = tmp_path / "empty.log"
    log_path.write_text("", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_recovery_once.py",
            "--provider",
            "mock",
            "--dotenv",
            str(tmp_path / "missing.env"),
            "--log-path",
            str(log_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--send-feishu-card",
            "--require-feishu-notification",
        ],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 31, completed.stderr + completed.stdout
    assert "Status: **NO_TRACEBACK**" in completed.stdout
    assert "# Feishu Notification" in completed.stdout
    assert "Sent: False" in completed.stdout


def test_run_recovery_once_auto_repair_demo_expands_terminal_demo_profile(tmp_path: Path) -> None:
    log_path = tmp_path / "empty.log"
    log_path.write_text("", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_recovery_once.py",
            "--provider",
            "mock",
            "--dotenv",
            str(tmp_path / "missing.env"),
            "--log-path",
            str(log_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--auto-repair-demo",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 31, completed.stderr + completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["recovery"]["status"] == "NO_TRACEBACK"
    assert payload["recovery"]["max_repair_attempts"] == 2
    assert payload["decision"]["action"] == "BLOCKED"
    assert payload["feishu_notification"]["attempted"] is True
    assert payload["feishu_notification"]["sent"] is False
    assert payload["feishu_notification"]["card_kind"] == "failure"
    assert "FEISHU_APP_ID" in payload["feishu_notification"]["error"]


def test_run_recovery_once_auto_repair_demo_rejects_skip_validation(tmp_path: Path) -> None:
    log_path = tmp_path / "empty.log"
    log_path.write_text("", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_recovery_once.py",
            "--provider",
            "mock",
            "--log-path",
            str(log_path),
            "--auto-repair-demo",
            "--skip-validation",
        ],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "--auto-repair-demo 不能与 --skip-validation 搭配" in completed.stderr


def test_run_recovery_once_git_delivery_dry_run_outputs_blocked_plan(tmp_path: Path) -> None:
    log_path = tmp_path / "empty.log"
    log_path.write_text("", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_recovery_once.py",
            "--provider",
            "mock",
            "--log-path",
            str(log_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--git-delivery-dry-run",
            "--use-git-worktree",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        env=_cli_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr + completed.stdout
    payload = json.loads(completed.stdout)
    plan = payload["git_delivery_plan"]
    assert plan["status"] == "BLOCKED"
    assert plan["ready"] is False
    assert plan["use_worktree"] is True
    assert plan["worktree_path"].endswith("agent-fix-recovery")
    assert plan["stage_files"] == []
    assert plan["would_push"] is False
    assert plan["would_create_pr"] is False
    assert any("not CREATE_PR_READY" in reason for reason in plan["denied_reasons"])
