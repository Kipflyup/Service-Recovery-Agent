"""Deterministic adversarial validation probes for repaired services.

The normal validation profile answers "do the configured tests pass?".  This
module adds a small adversarial layer that turns ``FaultDiagnosis`` into
targeted HTTP probes and checks whether the current app still leaks the
original crash as a 500 response.

The first implementation is intentionally deterministic and demo-focused.  It
does not call an LLM; it uses the arithmetic-boundary diagnosis for
``unsafe_divide`` to exercise the zero boundary, happy paths, alternate
callers, and repeated requests.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask

from .fault_diagnosis import FaultDiagnosis


PASS = "PASS"
FAIL = "FAIL"
PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class ProbeCase:
    """A single adversarial HTTP probe case."""

    name: str
    method: str
    path: str
    expected_statuses: set[int] = field(default_factory=set)
    forbidden_statuses: set[int] = field(default_factory=set)
    expected_json: dict[str, Any] = field(default_factory=dict)
    forbidden_json: dict[str, set[Any]] = field(default_factory=dict)
    repeat: int = 1
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "expected_statuses": sorted(self.expected_statuses),
            "forbidden_statuses": sorted(self.forbidden_statuses),
            "expected_json": self.expected_json,
            "forbidden_json": {
                key: sorted(values, key=lambda item: str(item))
                for key, values in self.forbidden_json.items()
            },
            "repeat": self.repeat,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProbeAttemptResult:
    """Result from one execution attempt of a probe case."""

    status_code: int
    response_json: dict[str, Any] | None
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "response_json": self.response_json,
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProbeResult:
    """All attempts for a probe case."""

    case: ProbeCase
    attempts: list[ProbeAttemptResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.attempts) and all(attempt.passed for attempt in self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "passed": self.passed,
        }


@dataclass(frozen=True)
class AdversarialValidationResult:
    """Structured PASS/FAIL/PARTIAL result for adversarial probes."""

    status: str
    summary: str
    probe_results: list[ProbeResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "passed": self.passed,
            "probe_results": [result.to_dict() for result in self.probe_results],
            "passed_probe_count": sum(1 for result in self.probe_results if result.passed),
            "total_probe_count": len(self.probe_results),
        }


def generate_probe_cases(diagnosis: FaultDiagnosis) -> list[ProbeCase]:
    """Generate deterministic adversarial probes from ``FaultDiagnosis``."""

    classification = diagnosis.classification
    suspected_variables = set(diagnosis.suspected_variables)
    if classification.category != "arithmetic_boundary":
        return [
            ProbeCase(
                name="original_failure_path",
                method="GET",
                path="/divide?x=0",
                forbidden_statuses={500},
                forbidden_json={
                    "error": {"internal_server_error"},
                    "exception_type": {classification.exception_type},
                },
                description="Conservative fallback: original failing request should not return 500 after repair.",
            ),
            ProbeCase(
                name="happy_path",
                method="GET",
                path="/divide?x=4",
                expected_statuses={200},
                forbidden_statuses={500},
                expected_json={"result": 25.0},
                description="Conservative fallback: preserve the known happy path.",
            ),
        ]

    if suspected_variables and "denominator" not in suspected_variables:
        # The first deterministic probe set is demo-specific.  For other
        # arithmetic variables, keep the fallback rather than guessing routes.
        return [
            ProbeCase(
                name="original_failure_path",
                method="GET",
                path="/divide?x=0",
                forbidden_statuses={500},
                forbidden_json={
                    "error": {"internal_server_error"},
                    "exception_type": {classification.exception_type},
                },
                description="Arithmetic fallback: original failing request should not return 500 after repair.",
            ),
            ProbeCase(
                name="happy_path",
                method="GET",
                path="/divide?x=4",
                expected_statuses={200},
                forbidden_statuses={500},
                expected_json={"result": 25.0},
                description="Arithmetic fallback: preserve the known happy path.",
            ),
        ]

    return [
        ProbeCase(
            name="happy_path",
            method="GET",
            path="/divide?x=4",
            expected_statuses={200},
            forbidden_statuses={500},
            expected_json={"result": 25.0},
            description="Preserve the normal divide path.",
        ),
        ProbeCase(
            name="zero_boundary",
            method="GET",
            path="/divide?x=0",
            expected_statuses={400, 422},
            forbidden_statuses={500},
            forbidden_json=_forbidden_zero_division_json(),
            description="Core repair check: zero denominator must become a controlled client error.",
        ),
        ProbeCase(
            name="negative_denominator",
            method="GET",
            path="/divide?x=-1",
            expected_statuses={200},
            forbidden_statuses={500},
            expected_json={"result": -100.0},
            description="Negative non-zero denominator should not be rejected as invalid.",
        ),
        ProbeCase(
            name="missing_query",
            method="GET",
            path="/divide",
            expected_statuses={200},
            forbidden_statuses={500},
            expected_json={"result": 100.0},
            description="Missing denominator should still use the route default.",
        ),
        ProbeCase(
            name="bug_shortcut",
            method="GET",
            path="/bug",
            expected_statuses={400, 422},
            forbidden_statuses={500},
            forbidden_json=_forbidden_zero_division_json(),
            description="Alternate caller of unsafe_divide must also be covered by the repair.",
        ),
        ProbeCase(
            name="repeated_zero",
            method="GET",
            path="/divide?x=0",
            expected_statuses={400, 422},
            forbidden_statuses={500},
            forbidden_json=_forbidden_zero_division_json(),
            repeat=3,
            description="Repeated zero-boundary calls should be stable and non-500.",
        ),
    ]


def run_probe_cases(app: Flask, cases: list[ProbeCase]) -> AdversarialValidationResult:
    """Run probe cases against a Flask app using its test client."""

    if not cases:
        return AdversarialValidationResult(
            status=PARTIAL,
            summary="No adversarial probe cases were generated.",
            probe_results=[],
        )

    client = app.test_client()
    probe_results: list[ProbeResult] = []
    for case in cases:
        attempts: list[ProbeAttemptResult] = []
        repeat = max(1, case.repeat)
        for _ in range(repeat):
            attempts.append(_run_single_attempt(client, case))
        probe_results.append(ProbeResult(case=case, attempts=attempts))

    passed_cases = sum(1 for result in probe_results if result.passed)
    total_cases = len(probe_results)
    passed_attempts = sum(1 for result in probe_results for attempt in result.attempts if attempt.passed)
    total_attempts = sum(len(result.attempts) for result in probe_results)
    failed_names = [result.case.name for result in probe_results if not result.passed]

    if passed_cases == total_cases:
        status = PASS
        summary = f"All {total_cases} adversarial probe case(s) passed ({passed_attempts}/{total_attempts} attempts)."
    else:
        status = FAIL
        summary = (
            f"{passed_cases}/{total_cases} adversarial probe case(s) passed "
            f"({passed_attempts}/{total_attempts} attempts). Failed probes: {', '.join(failed_names)}."
        )

    return AdversarialValidationResult(
        status=status,
        summary=summary,
        probe_results=probe_results,
    )


def validate_demo_app_after_repair(
    diagnosis: FaultDiagnosis,
    *,
    log_path: str | Path | None = None,
) -> AdversarialValidationResult:
    """Create the current demo Flask app and run diagnosis-derived probes."""

    # ``RecoveryAgent`` applies patches in-process.  If tests or a caller have
    # already imported ``demo_service.app`` before the patch is applied, a plain
    # import would keep stale function objects.  Reloading here makes the
    # adversarial gate validate the current on-disk patched code.
    app_module = importlib.import_module("demo_service.app")
    app_module = importlib.reload(app_module)
    create_app = app_module.create_app

    app = create_app(log_path=log_path, testing=True)
    return run_probe_cases(app, generate_probe_cases(diagnosis))


def _forbidden_zero_division_json() -> dict[str, set[Any]]:
    return {
        "error": {"internal_server_error"},
        "exception_type": {"ZeroDivisionError"},
    }


def format_adversarial_validation_result(result: AdversarialValidationResult) -> str:
    """Render a human-readable adversarial validation report."""

    lines: list[str] = []
    lines.append("# Adversarial Validation")
    lines.append("")
    lines.append(f"- Status: **{result.status}**")
    lines.append(f"- Summary: {result.summary}")

    if not result.probe_results:
        return "\n".join(lines)

    lines.append("")
    lines.append("## Probe Results")
    for probe_result in result.probe_results:
        case = probe_result.case
        status = "PASS" if probe_result.passed else "FAIL"
        lines.append(f"- **{case.name}** `{case.method.upper()} {case.path}`: {status}")
        if case.description:
            lines.append(f"  - Purpose: {case.description}")
        for index, attempt in enumerate(probe_result.attempts, start=1):
            attempt_status = "PASS" if attempt.passed else "FAIL"
            lines.append(
                f"  - Attempt {index}: {attempt_status}, status={attempt.status_code}, reason={attempt.reason}"
            )
            if attempt.response_json is not None:
                lines.append(f"    response_json={attempt.response_json}")

    return "\n".join(lines)


def _run_single_attempt(client: Any, case: ProbeCase) -> ProbeAttemptResult:
    try:
        response = client.open(path=case.path, method=case.method.upper())
    except Exception as exc:  # pragma: no cover - defensive for custom Flask apps.
        return ProbeAttemptResult(
            status_code=0,
            response_json=None,
            passed=False,
            reason=f"request raised {type(exc).__name__}: {exc}",
        )

    response_json = response.get_json(silent=True)
    if response_json is not None and not isinstance(response_json, dict):
        response_json = {"_json": response_json}

    passed, reason = _evaluate_response(case, response.status_code, response_json)
    return ProbeAttemptResult(
        status_code=response.status_code,
        response_json=response_json,
        passed=passed,
        reason=reason,
    )


def _evaluate_response(
    case: ProbeCase,
    status_code: int,
    response_json: dict[str, Any] | None,
) -> tuple[bool, str]:
    if status_code in case.forbidden_statuses:
        return False, f"status {status_code} is forbidden"

    if case.expected_statuses and status_code not in case.expected_statuses:
        return False, f"status {status_code} not in expected {sorted(case.expected_statuses)}"

    body = response_json or {}
    for key, expected_value in case.expected_json.items():
        if key not in body:
            return False, f"expected JSON field {key!r} is missing"
        if not _values_match(body[key], expected_value):
            return False, f"expected JSON field {key!r}={expected_value!r}, got {body[key]!r}"

    for key, forbidden_values in case.forbidden_json.items():
        if key not in body:
            continue
        if any(_values_match(body[key], forbidden_value) for forbidden_value in forbidden_values):
            return False, f"JSON field {key!r} has forbidden value {body[key]!r}"

    return True, "matched expected adversarial constraints"


def _values_match(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) < 1e-9
    return actual == expected
