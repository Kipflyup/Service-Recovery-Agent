from __future__ import annotations

import math
import sys
from pathlib import Path

from service_recovery_agent.sbfl import (
    PARTIAL,
    PASS,
    UNAVAILABLE,
    format_sbfl_result,
    ochiai_score,
    parse_pytest_command,
    run_sbfl,
)


def _write_sample_project(root: Path) -> dict[str, int]:
    (root / "tests").mkdir()
    (root / "buggy.py").write_text(
        "def risky(value):\n"
        "    if value == 0:\n"
        "        return 10 / value\n"
        "    return 10 / value\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_buggy.py").write_text(
        "from buggy import risky\n\n"
        "def test_pass():\n"
        "    assert risky(2) == 5\n\n"
        "def test_fail():\n"
        "    risky(0)\n",
        encoding="utf-8",
    )
    return {"fault_line": 3, "shared_branch_line": 2, "pass_line": 4}


def test_ochiai_score_formula() -> None:
    assert ochiai_score(failed_and_covered=1, passed_and_covered=0, total_failed=1) == 1.0
    assert ochiai_score(failed_and_covered=0, passed_and_covered=3, total_failed=2) == 0.0
    assert math.isclose(
        ochiai_score(failed_and_covered=1, passed_and_covered=1, total_failed=1),
        1 / math.sqrt(2),
    )


def test_run_sbfl_with_pytest_nodeids_ranks_fault_line_top(tmp_path: Path) -> None:
    lines = _write_sample_project(tmp_path)

    result = run_sbfl(
        project_root=tmp_path,
        pytest_nodeids=[
            "tests/test_buggy.py::test_pass",
            "tests/test_buggy.py::test_fail",
        ],
        top_k=5,
    )

    assert result.status == PASS
    assert result.total_passed == 1
    assert result.total_failed == 1
    assert len(result.test_results) == 2
    assert result.test_results[0].passed is True
    assert result.test_results[1].passed is False
    assert "buggy.py" in result.test_results[1].covered_lines

    top = result.suspicious_lines[0]
    assert top.file == "buggy.py"
    assert top.line_number == lines["fault_line"]
    assert top.failed_and_covered == 1
    assert top.passed_and_covered == 0
    assert top.score == 1.0
    assert "return 10 / value" in (top.source_line or "")

    data = result.to_dict()
    assert data["status"] == PASS
    assert data["suspicious_lines"][0]["file"] == "buggy.py"
    assert data["test_results"][1]["covered_lines"]["buggy.py"]

    report = format_sbfl_result(result)
    assert "SBFL Result" in report
    assert "buggy.py:3" in report
    assert "score=1.0000" in report


def test_run_sbfl_with_pytest_commands_collects_pass_and_fail_spectra(tmp_path: Path) -> None:
    _write_sample_project(tmp_path)

    result = run_sbfl(
        project_root=tmp_path,
        commands=[
            [sys.executable, "-m", "pytest", "-q", "tests/test_buggy.py::test_pass"],
            f"{sys.executable} -m pytest -q tests/test_buggy.py::test_fail",
        ],
        top_k=3,
    )

    assert result.status == PASS
    assert result.total_passed == 1
    assert result.total_failed == 1
    assert result.test_results[0].command[:3] == [sys.executable, "-m", "pytest"]
    assert result.test_results[1].passed is False
    assert result.suspicious_lines[0].file == "buggy.py"


def test_run_sbfl_without_failing_tests_returns_partial(tmp_path: Path) -> None:
    _write_sample_project(tmp_path)

    result = run_sbfl(
        project_root=tmp_path,
        pytest_nodeids=["tests/test_buggy.py::test_pass"],
    )

    assert result.status == PARTIAL
    assert result.total_failed == 0
    assert result.total_passed == 1
    assert result.suspicious_lines == []
    assert "No failing pytest run" in result.summary


def test_run_sbfl_rejects_unsupported_commands(tmp_path: Path) -> None:
    _write_sample_project(tmp_path)

    result = run_sbfl(project_root=tmp_path, commands=[[sys.executable, "-c", "print('not pytest')"]])

    assert result.status == UNAVAILABLE
    assert result.suspicious_lines == []
    assert "only supports pytest commands" in result.summary


def test_parse_pytest_command_variants() -> None:
    assert parse_pytest_command("pytest -q tests/test_x.py::test_y") == [
        "-q",
        "tests/test_x.py::test_y",
    ]
    assert parse_pytest_command([sys.executable, "-m", "pytest", "tests/test_x.py"]) == [
        "tests/test_x.py",
    ]
