#!/usr/bin/env python3
"""Validate the demo service after an auto-repair patch is applied.

This script is intentionally different from the seed-failure tests.

Seed-failure tests prove the original bug is reproducible so the Agent has a
stable error input.  This repair validation proves the patched service now
handles the same input as a controlled Web API error instead of a 500 crash.
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from demo_service.app import create_app  # noqa: E402


def main() -> int:
    app = create_app(
        log_path=PROJECT_ROOT / "logs" / "repair_validation.log",
        testing=True,
    )
    client = app.test_client()

    happy_response = client.get("/divide?x=4")
    if happy_response.status_code != 200:
        print(
            f"expected /divide?x=4 status 200, got {happy_response.status_code}",
            file=sys.stderr,
        )
        return 1

    happy_body = happy_response.get_json() or {}
    if happy_body.get("result") != 25.0:
        print(f"expected /divide?x=4 result 25.0, got {happy_body}", file=sys.stderr)
        return 1

    zero_response = client.get("/divide?x=0")
    zero_body = zero_response.get_json() or {}

    if zero_response.status_code >= 500:
        print(
            "expected /divide?x=0 to be handled as a controlled client error, "
            f"got status {zero_response.status_code} with body {zero_body}",
            file=sys.stderr,
        )
        return 1

    if zero_response.status_code not in {400, 422}:
        print(
            f"expected /divide?x=0 status 400 or 422, got {zero_response.status_code}",
            file=sys.stderr,
        )
        return 1

    if zero_body.get("error") in {None, "internal_server_error"}:
        print(f"expected controlled error response, got {zero_body}", file=sys.stderr)
        return 1

    if not zero_body.get("message"):
        print(f"expected error response message, got {zero_body}", file=sys.stderr)
        return 1

    print("demo repair validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
