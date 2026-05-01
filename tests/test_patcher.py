from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402
from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402
from service_recovery_agent.patcher import (  # noqa: E402
    apply_patch_text,
    dry_run_patch,
    preview_patch,
    restore_snapshot,
    snapshot_files,
)


VALID_PATCH = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@ -28,4 +28,6 @@ def unsafe_divide(numerator: float, denominator: float) -> float:
     # Intentional bug for the recovery demo:
     # denominator == 0 currently raises ZeroDivisionError.
+    if denominator == 0.0:
+        raise ValueError("Denominator cannot be zero, invalid input")
     return numerator / denominator
"""


INVALID_PATCH = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@ -999,4 +999,6 @@ def unsafe_divide(numerator: float, denominator: float) -> float:
+    impossible_context = True
"""


UNSAFE_PATCH = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@
+    if denominator == 0.0:
+        return float('inf')
"""


def _demo_context(tmp_path: Path):
    log_path = tmp_path / "app.log"
    app = create_app(log_path=log_path, testing=True)
    response = app.test_client().get("/divide?x=0")
    assert response.status_code == 500
    for handler in app.logger.handlers:
        handler.flush()

    event = read_latest_traceback(log_path)
    assert event is not None
    return build_code_context(event, project_root=PROJECT_ROOT)


@pytest.mark.seed_failure
def test_dry_run_patch_can_apply_valid_patch() -> None:
    result = dry_run_patch(VALID_PATCH, project_root=PROJECT_ROOT)

    assert result.can_apply is True
    assert result.return_code == 0
    assert "demo_service/app.py" in result.stdout


def test_dry_run_patch_rejects_bad_context() -> None:
    result = dry_run_patch(INVALID_PATCH, project_root=PROJECT_ROOT)

    assert result.can_apply is False
    assert result.return_code != 0


@pytest.mark.seed_failure
def test_preview_patch_skips_dry_run_when_safety_fails(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)

    result = preview_patch(UNSAFE_PATCH, context, project_root=PROJECT_ROOT)

    assert result.status == "FAIL"
    assert result.dry_run is None


def test_apply_patch_text_modifies_file_and_snapshot_can_restore(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    patch = """--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-old
+new
"""
    snapshot = snapshot_files(tmp_path, ["file.txt"])

    result = apply_patch_text(patch, project_root=tmp_path)

    assert result.applied is True
    assert result.return_code == 0
    assert target.read_text(encoding="utf-8") == "new\n"

    restore_snapshot(tmp_path, snapshot)

    assert target.read_text(encoding="utf-8") == "old\n"
