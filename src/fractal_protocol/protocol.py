from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import DomainError, require
from .schema import validate_schema_definition


RESULT_STATUSES = {"success", "partial", "fail"}
STOP_REASONS = {"completed", "budget", "quality"}
COGNITIVE_MODES = {"divergent", "convergent", "mixed"}
SURFACES = {"manifest", "execute", "consult", "verify", "feedback"}
CLAUSE_OPERATORS = {
    "exists",
    "equals",
    "type",
    "minimum",
    "maximum",
    "contains",
}
JSON_TYPES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}
UNSUPPORTED_SECURITY_CONSTRAINT_KEYS = {
    "confidentiality",
    "data_residency",
    "data_retention",
    "execution_environment",
    "jurisdiction",
    "region",
    "retention",
    "tee",
    "trusted_execution",
}
MAX_TASK_SPEC_BYTES = 256 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_MINOR_UNITS = 1_000_000_000_000
MAX_ACCEPT_CLAUSE_ID_LENGTH = 200
MAX_ACCEPT_CLAUSE_PATH_LENGTH = 10_000


def _unsupported_security_constraints(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key.lower() in UNSUPPORTED_SECURITY_CONSTRAINT_KEYS:
                found.append(child_path)
            found.extend(_unsupported_security_constraints(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_unsupported_security_constraints(child, f"{path}[{index}]"))
    return found


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "invalid_json",
            "The value is not canonical JSON",
            details={"reason": str(exc)},
        ) from exc


def content_digest(value: Any) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def require_object(value: Any, field: str) -> dict[str, Any]:
    require(isinstance(value, dict), "invalid_field", f"{field} must be an object", field=field)
    return value


def require_string(value: Any, field: str, *, nonempty: bool = True) -> str:
    require(isinstance(value, str), "invalid_field", f"{field} must be a string", field=field)
    if nonempty:
        require(bool(value.strip()), "invalid_field", f"{field} cannot be empty", field=field)
    return value


def require_minor_units(value: Any, field: str, *, allow_zero: bool = False) -> int:
    valid = isinstance(value, int) and not isinstance(value, bool)
    require(valid, "invalid_amount", f"{field} must be an integer number of minor units", field=field)
    minimum = 0 if allow_zero else 1
    require(
        minimum <= value <= MAX_MINOR_UNITS,
        "invalid_amount",
        f"{field} must be between {minimum} and {MAX_MINOR_UNITS}",
        field=field,
    )
    return value


def validate_manifest(value: Any) -> dict[str, Any]:
    manifest = require_object(value, "manifest")
    concept_ref = require_string(manifest.get("concept_ref"), "concept_ref")
    name = require_string(manifest.get("name"), "name")
    description = require_string(manifest.get("description"), "description")
    mode = require_string(manifest.get("cognitive_mode"), "cognitive_mode")
    require(mode in COGNITIVE_MODES, "invalid_manifest", "Unsupported cognitive mode", value=mode)

    operations = manifest.get("operations")
    require(isinstance(operations, list) and operations, "invalid_manifest", "operations must be a non-empty list")
    checked_operations = sorted({require_string(item, "operations[]") for item in operations})
    require(len(checked_operations) == len(operations), "invalid_manifest", "operations must be unique")
    require(
        len(checked_operations) == 1,
        "invalid_manifest",
        "Protocol v1 requires one operation per manifest so schemas are unambiguous",
    )

    surfaces = manifest.get("surfaces", ["manifest", "execute"])
    require(isinstance(surfaces, list), "invalid_manifest", "surfaces must be a list")
    checked_surfaces = sorted({require_string(item, "surfaces[]") for item in surfaces})
    require(set(checked_surfaces) <= SURFACES, "invalid_manifest", "Manifest has an unsupported surface")
    require({"manifest", "execute"} <= set(checked_surfaces), "invalid_manifest", "manifest and execute surfaces are mandatory")
    require(
        set(checked_surfaces) == {"manifest", "execute"},
        "invalid_manifest",
        "Protocol v1 only transports manifest and execute surfaces",
    )

    input_schema = require_object(manifest.get("input_schema", {}), "input_schema")
    output_schema = require_object(manifest.get("output_schema", {}), "output_schema")
    validate_schema_definition(input_schema)
    validate_schema_definition(output_schema)
    require(
        input_schema.get("type") == "object"
        and output_schema.get("type") == "object",
        "invalid_manifest_schema",
        "Protocol v1 input and output schemas must declare object roots",
    )

    normalized = {
        "protocol_version": "1",
        "concept_ref": concept_ref,
        "name": name,
        "description": description,
        "cognitive_mode": mode,
        "operations": checked_operations,
        "surfaces": checked_surfaces,
        "input_schema": input_schema,
        "output_schema": output_schema,
    }
    encoded = canonical_json(normalized).encode("utf-8")
    require(
        len(encoded) <= MAX_MANIFEST_BYTES,
        "manifest_too_large",
        f"A manifest must be at most {MAX_MANIFEST_BYTES} bytes",
    )
    return normalized


def validate_accept_spec(value: Any) -> dict[str, Any]:
    spec = require_object(value, "accept_spec")
    seam = spec.get("seam", "hard")
    require(
        seam == "hard",
        "invalid_accept_spec",
        "Payable protocol v1 supports hard seams only",
    )

    minimum = spec.get("minimum_pass_rate", 1.0)
    valid_minimum = isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
    require(valid_minimum and 0 <= minimum <= 1, "invalid_accept_spec", "minimum_pass_rate must be between 0 and 1")

    clauses = spec.get("clauses")
    require(isinstance(clauses, list) and clauses, "invalid_accept_spec", "clauses must be a non-empty list")
    checked: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(clauses):
        clause = require_object(raw, f"clauses[{index}]")
        identifier = require_string(clause.get("id"), f"clauses[{index}].id")
        require(
            len(identifier) <= MAX_ACCEPT_CLAUSE_ID_LENGTH,
            "invalid_accept_spec",
            f"clause ids must be at most {MAX_ACCEPT_CLAUSE_ID_LENGTH} characters",
            clause_id=identifier,
        )
        require(
            not identifier.startswith("protocol:"),
            "invalid_accept_spec",
            "clause ids beginning with protocol: are reserved for coordinator checks",
            clause_id=identifier,
        )
        require(identifier not in identifiers, "invalid_accept_spec", "clause ids must be unique", clause_id=identifier)
        identifiers.add(identifier)
        path = require_string(clause.get("path"), f"clauses[{index}].path", nonempty=False)
        require(
            len(path) <= MAX_ACCEPT_CLAUSE_PATH_LENGTH,
            "invalid_accept_spec",
            f"clause paths must be at most {MAX_ACCEPT_CLAUSE_PATH_LENGTH} characters",
            clause_id=identifier,
        )
        require(path == "" or path.startswith("/"), "invalid_accept_spec", "clause path must be a JSON Pointer")
        operator = require_string(clause.get("operator"), f"clauses[{index}].operator")
        require(operator in CLAUSE_OPERATORS, "invalid_accept_spec", "unsupported clause operator", operator=operator)
        critical = clause.get("critical", True)
        require(isinstance(critical, bool), "invalid_accept_spec", "critical must be boolean")
        default_disclosure = (
            "hidden" if operator in {"equals", "contains"} else "public"
        )
        disclosure = clause.get("disclosure", default_disclosure)
        require(
            disclosure in {"public", "hidden"},
            "invalid_accept_spec",
            "disclosure must be public or hidden",
        )

        expected = clause.get("expected")
        if operator == "type":
            require(expected in JSON_TYPES, "invalid_accept_spec", "type clause has an unsupported expected type")
        elif operator in {"minimum", "maximum"}:
            numeric = isinstance(expected, (int, float)) and not isinstance(expected, bool)
            require(numeric, "invalid_accept_spec", f"{operator} expected value must be numeric")
        elif operator not in {"exists"}:
            require("expected" in clause, "invalid_accept_spec", f"{operator} clause requires expected")

        normalized_clause = {
            "id": identifier,
            "path": path,
            "operator": operator,
            "critical": critical,
            "disclosure": disclosure,
        }
        if "expected" in clause:
            normalized_clause["expected"] = expected
        checked.append(normalized_clause)

    normalized = {
        "seam": seam,
        "minimum_pass_rate": float(minimum),
        "clauses": checked,
    }
    canonical_json(normalized)
    return normalized


def validate_task_spec(value: Any) -> dict[str, Any]:
    task = require_object(value, "task")
    required_capability = require_string(task.get("required_capability"), "required_capability")
    digest_value = required_capability.removeprefix("sha256:")
    require(
        required_capability.startswith("sha256:")
        and len(digest_value) == 64
        and all(character in "0123456789abcdef" for character in digest_value),
        "invalid_task",
        "required_capability must be a full lowercase SHA-256 manifest digest",
    )
    operation = require_string(task.get("operation"), "operation")
    inputs = require_object(task.get("inputs"), "inputs")
    constraints = require_object(task.get("constraints", {}), "constraints")
    unsupported = sorted(_unsupported_security_constraints(constraints))
    require(
        not unsupported,
        "unsupported_security_constraint",
        "The v1 coordinator cannot enforce these security placement constraints",
        keys=unsupported,
    )
    accept_spec = validate_accept_spec(task.get("accept_spec"))
    reward_minor = require_minor_units(task.get("reward_minor"), "reward_minor")
    delegation_budget_minor = require_minor_units(
        task.get("delegation_budget_minor", 0),
        "delegation_budget_minor",
        allow_zero=True,
    )
    max_attempts = task.get("max_attempts", 3)
    valid_attempts = isinstance(max_attempts, int) and not isinstance(max_attempts, bool)
    require(valid_attempts and 1 <= max_attempts <= 20, "invalid_task", "max_attempts must be between 1 and 20")
    normalized = {
        "required_capability": required_capability,
        "operation": operation,
        "inputs": inputs,
        "constraints": constraints,
        "accept_spec": accept_spec,
        "reward_minor": reward_minor,
        "delegation_budget_minor": delegation_budget_minor,
        "max_attempts": max_attempts,
    }
    encoded = canonical_json(normalized).encode("utf-8")
    require(
        len(encoded) <= MAX_TASK_SPEC_BYTES,
        "task_too_large",
        f"A task specification must be at most {MAX_TASK_SPEC_BYTES} bytes",
    )
    return normalized


def validate_result(value: Any) -> dict[str, Any]:
    result = require_object(value, "result")
    submission_id = require_string(result.get("submission_id"), "submission_id")
    require(len(submission_id) <= 200, "invalid_result", "submission_id is too long")
    lease_token = require_string(result.get("lease_token"), "lease_token")
    status = require_string(result.get("status"), "status")
    require(status in RESULT_STATUSES, "invalid_result", "unsupported result status", value=status)
    stop_reason = require_string(result.get("stop_reason"), "stop_reason")
    require(stop_reason in STOP_REASONS, "invalid_result", "unsupported stop reason", value=stop_reason)
    outputs = require_object(result.get("outputs", {}), "outputs")
    evidence = require_object(result.get("evidence", {}), "evidence")
    usage = require_object(result.get("usage", {}), "usage")
    normalized = {
        "submission_id": submission_id,
        "lease_token": lease_token,
        "status": status,
        "stop_reason": stop_reason,
        "outputs": outputs,
        "evidence": evidence,
        "usage": usage,
    }
    encoded = canonical_json(normalized).encode("utf-8")
    require(
        len(encoded) <= MAX_RESULT_BYTES,
        "result_too_large",
        f"A Result must be at most {MAX_RESULT_BYTES} bytes",
    )
    return normalized


def validate_child_constraints(parent: dict[str, Any], child: dict[str, Any]) -> None:
    """Require exact preservation of inherited constraints.

    The protocol intentionally does not guess whether a changed arbitrary value is
    semantically tighter. Constraint-specific comparators can be added later.
    """

    missing = [key for key in parent if key not in child]
    changed = [key for key in parent if key in child and child[key] != parent[key]]
    if missing or changed:
        raise DomainError(
            "constraint_weakening",
            "A child task must preserve every inherited constraint exactly",
            details={"missing": missing, "changed": changed},
        )
