from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402
from service_recovery_agent.log_watcher import read_latest_traceback  # noqa: E402


def test_health_endpoint(tmp_path: Path) -> None:
    app = create_app(log_path=tmp_path / "app.log", testing=True)

    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_divide_happy_path(tmp_path: Path) -> None:
    app = create_app(log_path=tmp_path / "app.log", testing=True)

    response = app.test_client().get("/divide?x=4")

    assert response.status_code == 200
    assert response.get_json()["result"] == 25.0


@pytest.mark.seed_failure
def test_divide_by_zero_writes_traceback_log(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    app = create_app(log_path=log_path, testing=True)

    response = app.test_client().get("/divide?x=0")

    assert response.status_code == 500
    body = response.get_json()
    assert body["error"] == "internal_server_error"
    assert body["exception_type"] == "ZeroDivisionError"

    for handler in app.logger.handlers:
        handler.flush()

    log_text = log_path.read_text(encoding="utf-8")
    assert "Traceback (most recent call last):" in log_text
    assert "ZeroDivisionError" in log_text
    assert "demo_service/app.py" in log_text or "demo_service\\app.py" in log_text

    traceback_event = read_latest_traceback(log_path)
    assert traceback_event is not None
    assert traceback_event.exception_type == "ZeroDivisionError"
    assert traceback_event.frames
    assert traceback_event.crash_frame is not None
    assert traceback_event.crash_frame.file.endswith("demo_service/app.py") or traceback_event.crash_frame.file.endswith("demo_service\\app.py")
