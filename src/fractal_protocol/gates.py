from __future__ import annotations

from typing import Any

from .protocol import canonical_json, validate_accept_spec


_MISSING = object()


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    current = value
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return _MISSING
            if index < 0 or index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _is_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _evaluate_clause(clause: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    observed = _json_pointer(outputs, clause["path"])
    operator = clause["operator"]
    expected = clause.get("expected")

    if operator == "exists":
        passed = observed is not _MISSING
    elif observed is _MISSING:
        passed = False
    elif operator == "equals":
        passed = canonical_json(observed) == canonical_json(expected)
    elif operator == "type":
        passed = _is_json_type(observed, expected)
    elif operator == "minimum":
        numeric = isinstance(observed, (int, float)) and not isinstance(observed, bool)
        passed = numeric and observed >= expected
    elif operator == "maximum":
        numeric = isinstance(observed, (int, float)) and not isinstance(observed, bool)
        passed = numeric and observed <= expected
    elif operator == "contains":
        if isinstance(observed, str) and isinstance(expected, str):
            passed = expected in observed
        elif isinstance(observed, list):
            passed = any(
                canonical_json(item) == canonical_json(expected) for item in observed
            )
        elif isinstance(observed, dict) and isinstance(expected, str):
            passed = expected in observed
        else:
            passed = False
    else:  # validate_accept_spec prevents this path.
        passed = False

    return {
        "clause_id": clause["id"],
        "path": clause["path"],
        "operator": operator,
        "critical": clause["critical"],
        "disclosure": clause["disclosure"],
        "passed": passed,
        "expected": expected if "expected" in clause else None,
        "observed": None if observed is _MISSING else observed,
        "observed_missing": observed is _MISSING,
    }


def evaluate_result(
    accept_spec: dict[str, Any],
    *,
    outputs: dict[str, Any],
    status: str,
    stop_reason: str,
    output_schema_errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    spec = validate_accept_spec(accept_spec)
    outcomes: list[dict[str, Any]] = []

    if output_schema_errors:
        outcomes.append(
            {
                "clause_id": "protocol:output-schema",
                "path": "",
                "operator": "schema",
                "critical": True,
                "disclosure": "public",
                "passed": False,
                "expected": "manifest output_schema",
                "observed": output_schema_errors,
                "observed_missing": False,
            }
        )

    execution_ok = status == "success" and stop_reason == "completed"
    if not execution_ok:
        outcomes.append(
            {
                "clause_id": "protocol:completed-successfully",
                "path": "",
                "operator": "protocol",
                "critical": True,
                "disclosure": "public",
                "passed": False,
                "expected": {"status": "success", "stop_reason": "completed"},
                "observed": {"status": status, "stop_reason": stop_reason},
                "observed_missing": False,
            }
        )
    outcomes.extend(_evaluate_clause(clause, outputs) for clause in spec["clauses"])

    critical_failed = any(item["critical"] and not item["passed"] for item in outcomes)
    passed_count = sum(1 for item in outcomes if item["passed"])
    pass_rate = passed_count / len(outcomes)
    accepted = not critical_failed and pass_rate >= spec["minimum_pass_rate"]
    violations = [item for item in outcomes if not item["passed"]]

    failure_trace = None
    if not accepted:
        failure_trace = {
            "kind": "accept_spec_violation",
            "violations": violations,
            "evaluator": "coordinator:deterministic-v1",
        }

    return {
        "outcome": "pass" if accepted else "reject",
        "seam": spec["seam"],
        "minimum_pass_rate": spec["minimum_pass_rate"],
        "pass_rate": pass_rate,
        "clauses": outcomes,
        "failure_trace": failure_trace,
        "evaluator": "coordinator:deterministic-v1",
    }
