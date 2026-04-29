"""Demo web service for Service Recovery Agent.

The service intentionally contains one small bug:

``GET /divide?x=0`` raises ``ZeroDivisionError``.

This gives the Agent a deterministic starting point for the main demo chain:

service error -> traceback log -> Agent reads/parses traceback -> later auto-fix.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException


DEFAULT_LOG_PATH = Path("logs/app.log")


def unsafe_divide(numerator: float, denominator: float) -> float:
    """Deliberately unsafe helper used to produce a reproducible demo bug."""

    # Intentional bug for the recovery demo:
    # denominator == 0 currently raises ZeroDivisionError.
    return numerator / denominator


def create_app(
    *,
    log_path: str | os.PathLike[str] | None = None,
    testing: bool = False,
) -> Flask:
    """Create the demo Flask application."""

    app = Flask(__name__)
    app.config["TESTING"] = testing
    # Keep exception handling active in tests so the traceback is written to the
    # configured log file and the client receives a deterministic 500 response.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    configured_log_path = Path(
        log_path
        or os.getenv("WEB_SERVICE_LOG_PATH")
        or DEFAULT_LOG_PATH
    )
    app.config["WEB_SERVICE_LOG_PATH"] = str(configured_log_path)
    _configure_file_logging(app, configured_log_path)

    @app.get("/")
    def index() -> tuple[dict[str, Any], int]:
        return {
            "service": "demo-web-service",
            "status": "running",
            "try_bug": "/divide?x=0",
        }, 200

    @app.get("/health")
    def health() -> tuple[dict[str, str], int]:
        return {"status": "ok"}, 200

    @app.get("/divide")
    def divide() -> tuple[Any, int]:
        numerator = _float_arg("numerator", default=100.0)
        # ``x`` is intentionally supported because it makes the demo URL short:
        # http://127.0.0.1:5001/divide?x=0
        denominator = _float_arg("denominator", fallback_name="x", default=1.0)

        result = unsafe_divide(numerator, denominator)
        return {
            "numerator": numerator,
            "denominator": denominator,
            "result": result,
        }, 200

    @app.get("/bug")
    def bug() -> tuple[Any, int]:
        """Shortcut endpoint that always triggers the intentional bug."""

        result = unsafe_divide(100.0, 0.0)
        return {"result": result}, 200

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> tuple[Any, int]:
        if isinstance(error, HTTPException):
            return {
                "error": error.name,
                "message": error.description,
            }, error.code or 500

        app.logger.exception(
            "Unhandled exception while handling %s %s",
            request.method,
            request.path,
        )
        return {
            "error": "internal_server_error",
            "exception_type": type(error).__name__,
            "message": str(error),
            "log_path": app.config["WEB_SERVICE_LOG_PATH"],
        }, 500

    return app


def _float_arg(
    name: str,
    *,
    fallback_name: str | None = None,
    default: float,
) -> float:
    raw_value = request.args.get(name)
    if raw_value is None and fallback_name is not None:
        raw_value = request.args.get(fallback_name)
    if raw_value is None:
        return default
    return float(raw_value)


def _configure_file_logging(app: Flask, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = app.logger
    logger.setLevel(logging.INFO)

    # Remove old demo file handlers. This keeps repeated create_app() calls in
    # tests from writing to stale temporary files or duplicating log lines.
    for handler in list(logger.handlers):
        if getattr(handler, "_service_recovery_demo_handler", False):
            logger.removeHandler(handler)
            handler.close()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    file_handler._service_recovery_demo_handler = True  # type: ignore[attr-defined]
    logger.addHandler(file_handler)


def main() -> None:
    app = create_app()
    host = os.getenv("DEMO_SERVICE_HOST", "127.0.0.1")
    port = int(os.getenv("DEMO_SERVICE_PORT", "5001"))
    debug = os.getenv("DEMO_SERVICE_DEBUG", "false").lower() in {"1", "true", "yes", "on"}

    print(f"Demo service log path: {app.config['WEB_SERVICE_LOG_PATH']}")
    print(f"Health check: http://{host}:{port}/health")
    print(f"Trigger bug:  http://{host}:{port}/divide?x=0")
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()

