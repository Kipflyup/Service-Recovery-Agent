from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from service_recovery_agent.validator import validate_project  # noqa: E402


def test_validate_project_passes_successful_command(tmp_path: Path) -> None:
    result = validate_project(
        project_root=tmp_path,
        commands=[[sys.executable, "-c", "print('ok')"]],
    )

    assert result.status == "PASS"
    assert result.passed is True
    assert result.command_results[0].passed is True
    assert result.command_results[0].stdout.strip() == "ok"


def test_validate_project_fails_unsuccessful_command(tmp_path: Path) -> None:
    result = validate_project(
        project_root=tmp_path,
        commands=[[sys.executable, "-c", "raise SystemExit(7)"]],
    )

    assert result.status == "FAIL"
    assert result.passed is False
    assert result.command_results[0].passed is False
    assert result.command_results[0].return_code == 7
