from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402
from service_recovery_agent.code_context import build_code_context  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402
from service_recovery_agent.patch_safety import review_patch_safety  # noqa: E402


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


SAFE_PATCH = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@
 def unsafe_divide(numerator: float, denominator: float) -> float:
     \"\"\"Deliberately unsafe helper used to produce a reproducible demo bug.\"\"\"
 
+    if denominator == 0:
+        raise ValueError("denominator must not be zero")
+
     return numerator / denominator
"""


def test_safe_single_file_patch_passes(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)

    review = review_patch_safety(SAFE_PATCH, context)

    assert review.status == "PASS"
    assert review.modified_files == ["demo_service/app.py"]


def test_nan_or_infinity_patch_fails(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    patch = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@
+    if denominator == 0.0:
+        return float('inf') if numerator > 0 else float('-inf') if numerator < 0 else float('nan')
"""

    review = review_patch_safety(patch, context)

    assert review.status == "FAIL"
    assert any(finding.check == "nan_or_infinity" and finding.status == "FAIL" for finding in review.findings)


def test_function_signature_change_fails(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    patch = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@
-def unsafe_divide(numerator: float, denominator: float) -> float:
+def unsafe_divide(numerator: float, denominator: float, default: float = 0) -> float:
     return numerator / denominator
"""

    review = review_patch_safety(patch, context)

    assert review.status == "FAIL"
    assert any(finding.check == "function_signature" and finding.status == "FAIL" for finding in review.findings)


def test_cross_file_patch_warns_when_files_are_allowed(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    patch = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@
+# safe app change
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@
+# safe test change
"""

    review = review_patch_safety(
        patch,
        context,
        allowed_files=["demo_service/app.py", "tests/test_app.py"],
    )

    assert review.status == "WARN"
    assert any(finding.check == "cross_file_change" and finding.status == "WARN" for finding in review.findings)


def test_dangerous_operation_fails(tmp_path: Path) -> None:
    context = _demo_context(tmp_path)
    patch = """--- a/demo_service/app.py
+++ b/demo_service/app.py
@@
+    os.system("rm -rf /tmp/service-recovery-demo")
"""

    review = review_patch_safety(patch, context)

    assert review.status == "FAIL"
    assert any(finding.check == "dangerous_operations" and finding.status == "FAIL" for finding in review.findings)

