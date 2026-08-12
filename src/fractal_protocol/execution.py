from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

from .decomposition import validate_decomposition_proposal
from .errors import DomainError, require
from .gates import evaluate_result
from .materialization import decomposition_execution_failures
from .protocol import (
    canonical_json,
    content_digest,
    require_object,
    require_string,
    validate_accept_spec,
)
from .schema import validate_instance, validate_schema_definition


EXECUTION_PLAN_KIND = "execution_plan"
EXECUTION_RECORD_KIND = "execution_run_record"
MODEL_REQUEST_KIND = "model_execution_request"
MAX_EXECUTION_TASKS = 64
MAX_EXECUTION_PLAN_BYTES = 512 * 1024
MAX_MODEL_REQUEST_BYTES = 512 * 1024
MAX_MODEL_RESPONSE_BYTES = 512 * 1024
MAX_EXECUTION_RECORD_BYTES = 2 * 1024 * 1024
MAX_INLINE_GATE_VALUE_BYTES = 4 * 1024
MAX_IDENTIFIER_LENGTH = 200
RUN_STATUSES = {"completed", "rejected", "failed", "budget_exceeded"}
RUN_STOP_REASONS = {"completed", "quality", "adapter", "budget", "record"}
FINISH_REASONS = {"completed", "length", "content_filter", "error"}
EXECUTION_EVENT_TYPES = {
    "run_rejected",
    "call_blocked",
    "call_preparation_failed",
    "backend_execution_started",
    "backend_execution_failed",
    "backend_response_invalid",
    "backend_response_rejected",
    "backend_response_incomplete",
    "backend_execution_completed",
    "task_contract_evaluated",
    "synthesis_contract_evaluated",
    "record_capacity_exceeded",
    "run_completed",
}


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _bounded_string(value: Any, field: str, *, maximum: int = 10_000) -> str:
    checked = require_string(value, field)
    require(
        len(checked) <= maximum,
        "invalid_execution",
        f"{field} is too long",
        field=field,
        maximum=maximum,
    )
    return checked


def _identifier(value: Any, field: str) -> str:
    return _bounded_string(value, field, maximum=MAX_IDENTIFIER_LENGTH)


def _nonnegative_integer(value: Any, field: str) -> int:
    valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    require(
        valid,
        "invalid_execution",
        f"{field} must be a non-negative integer",
        field=field,
    )
    return value


def _sha256_digest(value: Any, field: str) -> str:
    digest = require_string(value, field)
    suffix = digest.removeprefix("sha256:")
    require(
        digest.startswith("sha256:")
        and len(suffix) == 64
        and all(character in "0123456789abcdef" for character in suffix),
        "invalid_execution",
        f"{field} must be a full lowercase SHA-256 digest",
        field=field,
    )
    return digest


@dataclass(frozen=True)
class ExecutionLimits:
    """Hard per-run ceilings for Solo execution.

    The adapter must report complete usage after every call. Token and monetary
    ceilings are therefore detected immediately after a response; the remaining
    entitlement and per-call output cap are also sent to the adapter so a real
    provider integration can enforce them before generation.
    """

    model_calls: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    monetary_microunits: int
    max_output_tokens_per_call: int
    currency: str = "USD"

    def normalized(self) -> dict[str, Any]:
        values = asdict(self)
        for field in (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "monetary_microunits",
        ):
            _nonnegative_integer(values[field], f"limits.{field}")
        require(
            isinstance(self.max_output_tokens_per_call, int)
            and not isinstance(self.max_output_tokens_per_call, bool)
            and self.max_output_tokens_per_call > 0,
            "invalid_execution",
            "limits.max_output_tokens_per_call must be a positive integer",
        )
        currency = require_string(self.currency, "limits.currency").upper()
        require(
            len(currency) == 3 and currency.isalpha(),
            "invalid_execution",
            "limits.currency must be a three-letter alphabetic code",
        )
        values["currency"] = currency
        return values


class ModelAdapter(Protocol):
    """Provider-neutral structured-output boundary used by Solo mode."""

    identity: dict[str, Any]

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]: ...


class ExecutionBackend(Protocol):
    """Contract shared by Solo mode and a future coordinator-backed mode."""

    identity: dict[str, Any]

    def execute(self, plan_value: Any, *, run_id: str) -> dict[str, Any]: ...


@dataclass
class FunctionModelAdapter:
    """Small adapter for local runtimes, examples, and deterministic tests."""

    identity: dict[str, Any]
    function: Callable[[dict[str, Any]], dict[str, Any]]

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.function(request)


def _normalize_model_identity(value: Any) -> dict[str, str]:
    identity = require_object(value, "model_adapter.identity")
    return {
        "adapter_id": _bounded_string(
            identity.get("adapter_id"), "model_adapter.identity.adapter_id", maximum=500
        ),
        "adapter_version": _bounded_string(
            identity.get("adapter_version"),
            "model_adapter.identity.adapter_version",
            maximum=100,
        ),
        "model": _bounded_string(
            identity.get("model"), "model_adapter.identity.model", maximum=500
        ),
    }


def _normalize_usage(value: Any, field: str = "usage") -> dict[str, int]:
    usage = require_object(value, field)
    require(
        usage.get("complete") is True,
        "incomplete_execution_usage",
        f"{field} must attest complete provider usage",
    )
    return {
        name: _nonnegative_integer(usage.get(name), f"{field}.{name}")
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "monetary_microunits",
        )
    }


def _empty_usage() -> dict[str, int]:
    return {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "monetary_microunits": 0,
    }


def _add_call_usage(total: dict[str, int], call: dict[str, int]) -> dict[str, int]:
    return {
        "model_calls": total["model_calls"] + 1,
        "input_tokens": total["input_tokens"] + call["input_tokens"],
        "output_tokens": total["output_tokens"] + call["output_tokens"],
        "reasoning_tokens": total["reasoning_tokens"] + call["reasoning_tokens"],
        "monetary_microunits": (
            total["monetary_microunits"] + call["monetary_microunits"]
        ),
    }


def _normalize_model_response(
    value: Any, *, normalized_usage: dict[str, int] | None = None
) -> dict[str, Any]:
    response = require_object(value, "model_response")
    usage = normalized_usage or _normalize_usage(
        response.get("usage"), "model_response.usage"
    )
    output = _json_copy(require_object(response.get("output"), "model_response.output"))
    finish_reason = require_string(
        response.get("finish_reason"), "model_response.finish_reason"
    )
    require(
        finish_reason in FINISH_REASONS,
        "invalid_model_response",
        "model_response.finish_reason is unsupported",
        finish_reason=finish_reason,
    )
    response_id = response.get("response_id")
    if response_id is not None:
        response_id = _identifier(response_id, "model_response.response_id")
    normalized = {
        "output": output,
        "finish_reason": finish_reason,
        "response_id": response_id,
        "usage": usage,
    }
    require(
        len(canonical_json(normalized).encode("utf-8")) <= MAX_MODEL_RESPONSE_BYTES,
        "model_response_too_large",
        f"A model response must be at most {MAX_MODEL_RESPONSE_BYTES} bytes",
    )
    return normalized


def _provider_accept_spec(value: dict[str, Any]) -> dict[str, Any]:
    """Expose declared criteria while withholding hidden expected values."""

    spec = _json_copy(value)
    for clause in spec["clauses"]:
        if clause["disclosure"] == "hidden":
            clause.pop("expected", None)
    return spec


def _compact_gate_value(value: Any, seen_digests: set[str]) -> Any:
    encoded = canonical_json(value).encode("utf-8")
    digest = content_digest(value)
    if len(encoded) <= MAX_INLINE_GATE_VALUE_BYTES and digest not in seen_digests:
        seen_digests.add(digest)
        return _json_copy(value)
    return {
        "kind": "content_digest_reference",
        "digest": digest,
        "canonical_bytes": len(encoded),
    }


def _content_digest_reference(value: Any) -> dict[str, Any]:
    encoded = canonical_json(value).encode("utf-8")
    return {
        "kind": "content_digest_reference",
        "digest": content_digest(value),
        "canonical_bytes": len(encoded),
    }


def _normalize_content_reference(value: Any, field: str) -> dict[str, Any]:
    reference = _json_copy(require_object(value, field))
    require(
        reference.get("kind") == "content_digest_reference",
        "invalid_execution",
        f"{field}.kind is unsupported",
    )
    reference["digest"] = _sha256_digest(
        reference.get("digest"), f"{field}.digest"
    )
    reference["canonical_bytes"] = _nonnegative_integer(
        reference.get("canonical_bytes"), f"{field}.canonical_bytes"
    )
    return reference


def _compact_verification(value: dict[str, Any]) -> dict[str, Any]:
    """Bound repeated gate observations while retaining content identity."""

    verification = _json_copy(value)
    compact_clauses: list[dict[str, Any]] = []
    seen_gate_values: set[str] = set()
    for raw_clause in verification["clauses"]:
        clause = dict(raw_clause)
        if "expected" in clause:
            clause["expected"] = _compact_gate_value(
                clause["expected"], seen_gate_values
            )
        if "observed" in clause:
            clause["observed"] = _compact_gate_value(
                clause["observed"], seen_gate_values
            )
        compact_clauses.append(clause)
    verification["clauses"] = compact_clauses
    if verification["failure_trace"] is not None:
        rejected_by_id = {
            clause["clause_id"]: clause
            for clause in compact_clauses
            if not clause["passed"]
        }
        verification["failure_trace"]["violations"] = [
            rejected_by_id[item["clause_id"]]
            for item in verification["failure_trace"]["violations"]
        ]
    return verification


def _normalize_verification(
    value: Any, field: str, *, expected_outcome: str | None = None
) -> dict[str, Any]:
    verification = _json_copy(require_object(value, field))
    outcome = require_string(verification.get("outcome"), f"{field}.outcome")
    require(
        outcome in {"pass", "reject"}
        and (expected_outcome is None or outcome == expected_outcome),
        "invalid_execution",
        f"{field}.outcome is inconsistent",
    )
    require(
        verification.get("seam") == "hard",
        "invalid_execution",
        f"{field}.seam must be hard",
    )
    minimum_pass_rate = verification.get("minimum_pass_rate")
    require(
        isinstance(minimum_pass_rate, (int, float))
        and not isinstance(minimum_pass_rate, bool)
        and 0 <= minimum_pass_rate <= 1,
        "invalid_execution",
        f"{field}.minimum_pass_rate must be between zero and one",
    )
    pass_rate = verification.get("pass_rate")
    require(
        isinstance(pass_rate, (int, float))
        and not isinstance(pass_rate, bool)
        and 0 <= pass_rate <= 1,
        "invalid_execution",
        f"{field}.pass_rate must be between zero and one",
    )
    clauses = verification.get("clauses")
    require(
        isinstance(clauses, list) and bool(clauses),
        "invalid_execution",
        f"{field}.clauses must be a non-empty list",
    )
    checked_clauses: list[dict[str, Any]] = []
    checked_clause_ids: list[str] = []
    for index, raw_clause in enumerate(clauses):
        clause = require_object(raw_clause, f"{field}.clauses[{index}]")
        clause_id = _identifier(
            clause.get("clause_id"), f"{field}.clauses[{index}].clause_id"
        )
        checked_clause_ids.append(clause_id)
        require(
            isinstance(clause.get("critical"), bool)
            and isinstance(clause.get("passed"), bool)
            and isinstance(clause.get("observed_missing"), bool),
            "invalid_execution",
            f"{field}.clauses[{index}] has invalid boolean fields",
        )
        require(
            clause.get("disclosure") in {"public", "hidden"},
            "invalid_execution",
            f"{field}.clauses[{index}].disclosure is unsupported",
        )
        path = require_string(
            clause.get("path"),
            f"{field}.clauses[{index}].path",
            nonempty=False,
        )
        require(
            len(path) <= 10_000,
            "invalid_execution",
            f"{field}.clauses[{index}].path is too long",
        )
        checked_clauses.append(
            {
                "clause_id": clause_id,
                "path": path,
                "operator": _bounded_string(
                    clause.get("operator"),
                    f"{field}.clauses[{index}].operator",
                    maximum=100,
                ),
                "critical": clause["critical"],
                "disclosure": clause["disclosure"],
                "passed": clause["passed"],
                "expected": _json_copy(clause.get("expected")),
                "observed": _json_copy(clause.get("observed")),
                "observed_missing": clause["observed_missing"],
            }
        )
    require(
        len(checked_clause_ids) == len(set(checked_clause_ids)),
        "invalid_execution",
        f"{field}.clauses must have unique ids",
    )
    derived_pass_rate = sum(
        bool(clause["passed"]) for clause in checked_clauses
    ) / len(checked_clauses)
    require(
        abs(float(pass_rate) - derived_pass_rate) <= 1e-12,
        "invalid_execution",
        f"{field}.pass_rate does not match its clause outcomes",
    )
    evaluator = _bounded_string(
        verification.get("evaluator"), f"{field}.evaluator", maximum=500
    )
    failed_clauses = [
        clause for clause in checked_clauses if not clause["passed"]
    ]
    critical_failed = any(
        clause["critical"] and not clause["passed"] for clause in checked_clauses
    )
    derived_outcome = (
        "pass"
        if not critical_failed and float(pass_rate) >= float(minimum_pass_rate)
        else "reject"
    )
    require(
        outcome == derived_outcome,
        "invalid_execution",
        f"{field}.outcome does not satisfy its declared acceptance threshold",
    )
    failure_trace = verification.get("failure_trace")
    if outcome == "pass":
        require(
            failure_trace is None and not critical_failed,
            "invalid_execution",
            f"{field} cannot pass with a failure trace or failed critical clause",
        )
        checked_failure_trace = None
    else:
        trace = require_object(failure_trace, f"{field}.failure_trace")
        require(
            trace.get("kind") == "accept_spec_violation",
            "invalid_execution",
            f"{field}.failure_trace.kind is unsupported",
        )
        trace_evaluator = require_string(
            trace.get("evaluator"), f"{field}.failure_trace.evaluator"
        )
        violations = trace.get("violations")
        require(
            isinstance(violations, list)
            and bool(violations)
            and trace_evaluator == evaluator
            and canonical_json(violations) == canonical_json(failed_clauses),
            "invalid_execution",
            f"{field}.failure_trace must exactly match failed clauses and evaluator",
        )
        checked_failure_trace = {
            "kind": "accept_spec_violation",
            "violations": _json_copy(failed_clauses),
            "evaluator": evaluator,
        }
    return {
        "outcome": outcome,
        "seam": "hard",
        "minimum_pass_rate": float(minimum_pass_rate),
        "pass_rate": float(pass_rate),
        "clauses": checked_clauses,
        "failure_trace": checked_failure_trace,
        "evaluator": evaluator,
    }


def _gate_value_matches(value: Any, expected: Any, field: str) -> bool:
    if canonical_json(value) == canonical_json(expected):
        return True
    if isinstance(value, dict) and value.get("kind") == "content_digest_reference":
        return _normalize_content_reference(value, field) == _content_digest_reference(
            expected
        )
    return False


def _validate_verification_against_work(
    verification: dict[str, Any],
    work: dict[str, Any],
    field: str,
    *,
    output: dict[str, Any] | None,
) -> None:
    """Bind a gate trace to the exact contract committed by the supplied plan."""

    spec = work["accept_spec"]
    require(
        verification["evaluator"] == "coordinator:deterministic-v1"
        and verification["minimum_pass_rate"] == spec["minimum_pass_rate"],
        "invalid_execution",
        f"{field} is not bound to the plan's deterministic accept spec",
    )
    if output is not None:
        expected = _normalize_verification(
            _compact_verification(
                evaluate_result(
                    spec,
                    outputs=output,
                    status="success",
                    stop_reason="completed",
                    output_schema_errors=validate_instance(
                        output, work["output_schema"]
                    ),
                )
            ),
            f"{field}.expected",
        )
        require(
            canonical_json(verification) == canonical_json(expected),
            "invalid_execution",
            f"{field} does not match deterministic evaluation of the retained output",
        )
        return

    clauses = verification["clauses"]
    protocol_clauses = [
        clause for clause in clauses if clause["clause_id"].startswith("protocol:")
    ]
    require(
        len(protocol_clauses) <= 1
        and (
            not protocol_clauses
            or (
                clauses[0]["clause_id"] == "protocol:output-schema"
                and protocol_clauses[0]["path"] == ""
                and protocol_clauses[0]["operator"] == "schema"
                and protocol_clauses[0]["critical"] is True
                and protocol_clauses[0]["disclosure"] == "public"
                and protocol_clauses[0]["passed"] is False
                and protocol_clauses[0]["observed_missing"] is False
                and (
                    (
                        isinstance(protocol_clauses[0]["observed"], list)
                        and bool(protocol_clauses[0]["observed"])
                    )
                    or (
                        isinstance(protocol_clauses[0]["observed"], dict)
                        and protocol_clauses[0]["observed"].get("kind")
                        == "content_digest_reference"
                    )
                )
                and _gate_value_matches(
                    protocol_clauses[0]["expected"],
                    "manifest output_schema",
                    f"{field}.clauses[0].expected",
                )
            )
        ),
        "invalid_execution",
        f"{field} contains an unsupported coordinator clause",
    )
    declared = clauses[len(protocol_clauses) :]
    require(
        len(declared) == len(spec["clauses"]),
        "invalid_execution",
        f"{field} does not contain the plan's exact accept clause set",
    )
    for index, (clause, expected_clause) in enumerate(
        zip(declared, spec["clauses"], strict=True)
    ):
        expected_value = expected_clause.get("expected")
        require(
            clause["clause_id"] == expected_clause["id"]
            and clause["path"] == expected_clause["path"]
            and clause["operator"] == expected_clause["operator"]
            and clause["critical"] == expected_clause["critical"]
            and clause["disclosure"] == expected_clause["disclosure"]
            and _gate_value_matches(
                clause["expected"],
                expected_value,
                f"{field}.clauses[{index + len(protocol_clauses)}].expected",
            ),
            "invalid_execution",
            f"{field} clause does not match the plan's accept spec",
            clause_id=expected_clause["id"],
        )


def _normalize_problem(value: Any) -> dict[str, Any]:
    problem = require_object(value, "problem")
    return {
        "statement": _bounded_string(problem.get("statement"), "problem.statement"),
        "context": _json_copy(require_object(problem.get("context", {}), "problem.context")),
    }


def _normalize_work(value: Any, field: str, *, task: bool) -> dict[str, Any]:
    work = require_object(value, field)
    output_schema = _json_copy(
        validate_schema_definition(work.get("output_schema"))
    )
    require(
        output_schema.get("type") == "object",
        "invalid_execution",
        f"{field}.output_schema must declare an object root",
    )
    normalized: dict[str, Any] = {
        "objective": _bounded_string(work.get("objective"), f"{field}.objective"),
        "context": _json_copy(
            require_object(work.get("context", {}), f"{field}.context")
        ),
        "output_schema": output_schema,
        "accept_spec": validate_accept_spec(work.get("accept_spec")),
    }
    if task:
        normalized = {
            "task_id": _identifier(work.get("task_id"), f"{field}.task_id"),
            "role": _bounded_string(work.get("role"), f"{field}.role", maximum=500),
            **normalized,
        }
        dependencies = work.get("depends_on", [])
        require(
            isinstance(dependencies, list),
            "invalid_execution",
            f"{field}.depends_on must be a list",
        )
        checked_dependencies = [
            _identifier(item, f"{field}.depends_on[]") for item in dependencies
        ]
        require(
            len(checked_dependencies) == len(set(checked_dependencies)),
            "invalid_execution",
            f"{field}.depends_on must be unique",
        )
        normalized["depends_on"] = checked_dependencies
        source_axis_id = work.get("source_axis_id")
        if source_axis_id is not None:
            normalized["source_axis_id"] = _identifier(
                source_axis_id, f"{field}.source_axis_id"
            )
    return normalized


def _topological_task_order(tasks: list[dict[str, Any]]) -> list[str]:
    task_ids = [task["task_id"] for task in tasks]
    identifiers = set(task_ids)
    for task in tasks:
        unknown = [
            dependency
            for dependency in task["depends_on"]
            if dependency not in identifiers
        ]
        require(
            not unknown,
            "invalid_execution",
            "A task depends on an unknown task",
            task_id=task["task_id"],
            unknown=unknown,
        )
        require(
            task["task_id"] not in task["depends_on"],
            "invalid_execution",
            "A task cannot depend on itself",
            task_id=task["task_id"],
        )

    remaining = {task["task_id"]: set(task["depends_on"]) for task in tasks}
    ordered: list[str] = []
    while remaining:
        ready = [task_id for task_id in task_ids if task_id in remaining and not remaining[task_id]]
        require(
            bool(ready),
            "invalid_execution",
            "The execution task graph contains a cycle",
            task_ids=sorted(remaining),
        )
        for task_id in ready:
            ordered.append(task_id)
            remaining.pop(task_id)
            for dependencies in remaining.values():
                dependencies.discard(task_id)
    return ordered


def create_execution_plan(
    problem: Any,
    tasks: Any,
    synthesis: Any,
    *,
    source: Any | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    """Create a content-addressed backend-neutral execution plan."""

    require(
        isinstance(tasks, list) and 1 <= len(tasks) <= MAX_EXECUTION_TASKS,
        "invalid_execution",
        f"tasks must contain between 1 and {MAX_EXECUTION_TASKS} items",
    )
    checked_tasks = [
        _normalize_work(item, f"tasks[{index}]", task=True)
        for index, item in enumerate(tasks)
    ]
    task_ids = [task["task_id"] for task in checked_tasks]
    require(
        len(task_ids) == len(set(task_ids)),
        "invalid_execution",
        "Execution task ids must be unique",
    )
    order = _topological_task_order(checked_tasks)
    require(
        isinstance(seed, int)
        and not isinstance(seed, bool)
        and 0 <= seed <= 2**63 - 1,
        "invalid_execution",
        "seed must be an integer between zero and 2^63-1",
    )
    core = {
        "protocol_version": "1",
        "kind": EXECUTION_PLAN_KIND,
        "problem": _normalize_problem(problem),
        "tasks": checked_tasks,
        "task_order": order,
        "synthesis": _normalize_work(synthesis, "synthesis", task=False),
        "source": _json_copy(
            require_object(source if source is not None else {}, "source")
        ),
        "seed": seed,
    }
    require(
        len(canonical_json(core).encode("utf-8")) <= MAX_EXECUTION_PLAN_BYTES,
        "execution_plan_too_large",
        f"An execution plan must be at most {MAX_EXECUTION_PLAN_BYTES} bytes",
    )
    return {**core, "plan_digest": content_digest(core)}


def validate_execution_plan(value: Any) -> dict[str, Any]:
    plan = require_object(value, "execution_plan")
    raw_core = {
        key: _json_copy(item)
        for key, item in plan.items()
        if key != "plan_digest"
    }
    require(
        len(canonical_json(raw_core).encode("utf-8")) <= MAX_EXECUTION_PLAN_BYTES,
        "execution_plan_too_large",
        f"An execution plan must be at most {MAX_EXECUTION_PLAN_BYTES} bytes",
    )
    require(
        plan.get("protocol_version") == "1"
        and plan.get("kind") == EXECUTION_PLAN_KIND,
        "invalid_execution",
        "Unsupported execution plan envelope",
    )
    rebuilt = create_execution_plan(
        plan.get("problem"),
        plan.get("tasks"),
        plan.get("synthesis"),
        source=plan.get("source", {}),
        seed=plan.get("seed"),
    )
    require(
        plan.get("task_order") == rebuilt["task_order"],
        "invalid_execution",
        "task_order does not match the task dependency graph",
    )
    require(
        plan.get("plan_digest") == rebuilt["plan_digest"],
        "invalid_execution",
        "plan_digest does not match the execution plan content",
    )
    rebuilt_core = {
        key: item for key, item in rebuilt.items() if key != "plan_digest"
    }
    require(
        canonical_json(raw_core) == canonical_json(rebuilt_core),
        "invalid_execution",
        "The execution plan contains non-canonical or unsupported fields",
    )
    return rebuilt


def execution_plan_from_decomposition(
    proposal_value: Any,
    *,
    problem: Any,
    axis_bindings: Any,
    synthesis: Any,
    review: Any,
    seed: int = 0,
) -> dict[str, Any]:
    """Compile reviewed root axes into a Solo/network-neutral task graph.

    ``review`` is an explicit provenance assertion, not an authenticated approval
    or a payable coordinator gate. The helper deliberately requires independently
    supplied routing probes and a structurally clean root before execution.
    """

    proposal = validate_decomposition_proposal(proposal_value)
    root = proposal["root"]
    require(
        root["status"] == "structurally_clean"
        and root["completeness"]["passed"]
        and root["routing"]["passed"],
        "decomposition_not_execution_ready",
        "The root decomposition is not structurally clean",
    )
    require(
        root["routing"]["probe_source"] == "provided",
        "decomposition_not_execution_ready",
        "Execution requires caller-supplied routing probes",
    )
    structural_failures = decomposition_execution_failures(proposal)
    require(
        not structural_failures,
        "decomposition_not_execution_ready",
        "The decomposition has unresolved execution-readiness failures",
        structural_failures=structural_failures,
    )
    review_value = require_object(review, "review")
    require(
        review_value.get("decision") == "approved",
        "decomposition_review_required",
        "An explicit approved review is required before execution",
    )
    checked_review = {
        "decision": "approved",
        "proposal_digest": _sha256_digest(
            review_value.get("proposal_digest"), "review.proposal_digest"
        ),
        "reviewer": _bounded_string(
            review_value.get("reviewer"), "review.reviewer", maximum=500
        ),
        "basis": _bounded_string(review_value.get("basis"), "review.basis"),
    }
    require(
        checked_review["proposal_digest"] == proposal["proposal_digest"],
        "decomposition_review_required",
        "The review must approve this exact decomposition proposal digest",
    )
    require(
        isinstance(axis_bindings, list),
        "invalid_execution",
        "axis_bindings must be a list",
    )
    root_axes = root["axes"]
    root_axis_ids = [axis["axis_id"] for axis in root_axes]
    axis_by_id = {axis["axis_id"]: axis for axis in root_axes}
    bindings: dict[str, dict[str, Any]] = {}
    for index, raw_binding in enumerate(axis_bindings):
        binding = require_object(raw_binding, f"axis_bindings[{index}]")
        axis_id = _identifier(binding.get("axis_id"), f"axis_bindings[{index}].axis_id")
        require(
            axis_id in axis_by_id,
            "invalid_execution",
            "Only a root conceptual axis can be bound at this execution boundary",
            axis_id=axis_id,
        )
        require(
            axis_id not in bindings,
            "invalid_execution",
            "A conceptual axis can be bound at most once",
            axis_id=axis_id,
        )
        bindings[axis_id] = binding
    missing = [axis_id for axis_id in root_axis_ids if axis_id not in bindings]
    require(
        not missing,
        "invalid_execution",
        "Every root conceptual axis requires an execution binding",
        missing_axis_ids=missing,
    )

    tasks: list[dict[str, Any]] = []
    planning_limitations: list[str] = []
    for axis_id in root_axis_ids:
        axis = axis_by_id[axis_id]
        binding = bindings[axis_id]
        marginal = axis["marginal_value"]
        if marginal is not None and marginal["recursion_decision"] != "leaf":
            planning_limitations.append(
                "root_axis_depth_not_executed:"
                f"{axis_id}:{marginal['recursion_decision']}"
            )
        if axis["decomposition"] is not None:
            planning_limitations.append(
                f"recursive_decomposition_flattened:{axis_id}"
            )
        tasks.append(
            {
                "task_id": axis_id,
                "source_axis_id": axis_id,
                "role": axis["candidate"]["name"],
                "objective": binding.get("objective"),
                "context": binding.get("context", {}),
                "depends_on": [],
                "output_schema": binding.get("output_schema"),
                "accept_spec": binding.get("accept_spec"),
            }
        )
    return create_execution_plan(
        problem,
        tasks,
        synthesis,
        source={
            "type": "conceptual_decomposition",
            "proposal_digest": proposal["proposal_digest"],
            "assessment_boundary": proposal["assessment_boundary"],
            "review": checked_review,
            "planning_scope": "reviewed_root_axes_only",
            "planning_limitations": [
                *planning_limitations,
                *(f"root_warning:{warning}" for warning in root["warnings"]),
                *(
                    f"proposal_warning:{warning}"
                    for warning in proposal["warnings"]
                ),
            ],
        },
        seed=seed,
    )


def _call_id(run_id: str, purpose: str, work_id: str) -> str:
    encoded = canonical_json([run_id, purpose, work_id]).encode("utf-8")
    return f"call-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _remaining(limits: dict[str, Any], usage: dict[str, int]) -> dict[str, int]:
    return {
        name: limits[name] - usage[name]
        for name in (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "monetary_microunits",
        )
    }


def _budget_dimensions(limits: dict[str, Any], usage: dict[str, int]) -> list[str]:
    return [
        name
        for name in (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "monetary_microunits",
        )
        if usage[name] > limits[name]
    ]


def _record_core(
    *,
    run_id: str,
    status: str,
    stop_reason: str,
    backend: dict[str, Any],
    plan_digest: str,
    planned_task_ids: list[str],
    task_results: list[dict[str, Any]],
    synthesis_result: dict[str, Any] | None,
    final_output: dict[str, Any] | None,
    final_verification: dict[str, Any] | None,
    usage: dict[str, int],
    limits: dict[str, Any],
    events: list[dict[str, Any]],
    error: dict[str, Any] | None,
    accounting_complete: bool,
) -> dict[str, Any]:
    return {
        "protocol_version": "1",
        "kind": EXECUTION_RECORD_KIND,
        "run_id": run_id,
        "status": status,
        "stop_reason": stop_reason,
        "backend": _json_copy(backend),
        "plan_digest": plan_digest,
        "planned_task_ids": list(planned_task_ids),
        "task_results": _json_copy(task_results),
        "synthesis_result": (
            _json_copy(synthesis_result) if synthesis_result is not None else None
        ),
        "final_output": _json_copy(final_output) if final_output is not None else None,
        "final_verification": (
            _json_copy(final_verification) if final_verification is not None else None
        ),
        "usage": dict(usage),
        "accounting_complete": accounting_complete,
        "verification_boundary": "deterministic_schema_and_accept_spec_only",
        "semantic_verification": "unverified",
        "limits": _json_copy(limits),
        "events": _json_copy(events),
        "error": _json_copy(error) if error is not None else None,
    }


def _finish_record(**values: Any) -> dict[str, Any]:
    core = _record_core(**values)
    require(
        len(canonical_json(core).encode("utf-8")) <= MAX_EXECUTION_RECORD_BYTES,
        "execution_record_too_large",
        f"An execution record must be at most {MAX_EXECUTION_RECORD_BYTES} bytes",
    )
    return {**core, "record_digest": content_digest(core)}


def _record_call_usage(value: Any, field: str) -> dict[str, int]:
    usage = require_object(value, field)
    return {
        name: _nonnegative_integer(usage.get(name), f"{field}.{name}")
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "monetary_microunits",
        )
    }


def _normalize_executor(value: Any, field: str) -> dict[str, Any]:
    executor = require_object(value, field)
    kind = require_string(executor.get("kind"), f"{field}.kind")
    require(
        kind in {"model_adapter", "network_node"},
        "invalid_execution",
        f"{field}.kind is unsupported",
    )
    identity_value = require_object(executor.get("identity"), f"{field}.identity")
    identity = (
        _normalize_model_identity(identity_value)
        if kind == "model_adapter"
        else _json_copy(identity_value)
    )
    require(bool(identity), "invalid_execution", f"{field}.identity cannot be empty")
    return {"kind": kind, "identity": identity}


def _normalize_backend_reference(value: Any, field: str) -> dict[str, Any]:
    reference = _json_copy(require_object(value, field))
    require_string(reference.get("kind"), f"{field}.kind")
    require(
        len(reference) > 1,
        "invalid_execution",
        f"{field} must identify a concrete backend execution",
    )
    return reference


def _normalize_work_result(
    value: Any, field: str, *, task: bool
) -> dict[str, Any]:
    result = require_object(value, field)
    status = require_string(result.get("status"), f"{field}.status")
    require(
        status in {"contract_valid", "contract_rejected"},
        "invalid_execution",
        f"{field}.status is unsupported",
    )
    normalized = {
        "status": status,
        "executor": _normalize_executor(result.get("executor"), f"{field}.executor"),
        "backend_reference": _normalize_backend_reference(
            result.get("backend_reference"), f"{field}.backend_reference"
        ),
        "response_digest": _sha256_digest(
            result.get("response_digest"), f"{field}.response_digest"
        ),
        "output_reference": _normalize_content_reference(
            result.get("output_reference"), f"{field}.output_reference"
        ),
        "usage": _record_call_usage(result.get("usage"), f"{field}.usage"),
    }
    if not task:
        return normalized

    expected_outcome = "pass" if status == "contract_valid" else "reject"
    verification = _normalize_verification(
        result.get("verification"),
        f"{field}.verification",
        expected_outcome=expected_outcome,
    )
    output_retention = require_string(
        result.get("output_retention", "inline"), f"{field}.output_retention"
    )
    require(
        output_retention in {"inline", "digest_only"},
        "invalid_execution",
        f"{field}.output_retention is unsupported",
    )
    output = _json_copy(require_object(result.get("output"), f"{field}.output"))
    if output_retention == "digest_only":
        output = _normalize_content_reference(output, f"{field}.output")
        require(
            output == normalized["output_reference"],
            "invalid_execution",
            f"{field}.output digest reference does not match its backend event commitment",
        )
    else:
        require(
            _content_digest_reference(output) == normalized["output_reference"],
            "invalid_execution",
            f"{field}.inline output does not match its output reference",
        )
    source_axis_id = result.get("source_axis_id")
    if source_axis_id is not None:
        source_axis_id = _identifier(source_axis_id, f"{field}.source_axis_id")
    return {
        "task_id": _identifier(result.get("task_id"), f"{field}.task_id"),
        "source_axis_id": source_axis_id,
        **normalized,
        "output_retention": output_retention,
        "output": output,
        "verification": verification,
    }


def _normalize_task_result(value: Any, field: str) -> dict[str, Any]:
    return _normalize_work_result(value, field, task=True)


def validate_execution_record(value: Any, *, plan_value: Any) -> dict[str, Any]:
    """Validate a run against the exact, separately retained execution plan.

    Record and plan digests provide content identity, not authenticity. Callers
    must supply the trusted plan they accepted before execution so contract
    traces cannot be rewritten under an unrelated plan digest.
    """

    plan = validate_execution_plan(plan_value)
    record = require_object(value, "execution_record")
    raw_core = {
        key: _json_copy(item)
        for key, item in record.items()
        if key != "record_digest"
    }
    require(
        len(canonical_json(raw_core).encode("utf-8")) <= MAX_EXECUTION_RECORD_BYTES,
        "execution_record_too_large",
        f"An execution record must be at most {MAX_EXECUTION_RECORD_BYTES} bytes",
    )
    require(
        record.get("protocol_version") == "1"
        and record.get("kind") == EXECUTION_RECORD_KIND,
        "invalid_execution",
        "Unsupported execution record envelope",
    )
    run_id = _identifier(record.get("run_id"), "execution_record.run_id")
    status = require_string(record.get("status"), "execution_record.status")
    stop_reason = require_string(
        record.get("stop_reason"), "execution_record.stop_reason"
    )
    require(status in RUN_STATUSES, "invalid_execution", "Unsupported run status")
    require(
        stop_reason in RUN_STOP_REASONS,
        "invalid_execution",
        "Unsupported run stop reason",
    )
    backend_value = require_object(
        record.get("backend"), "execution_record.backend"
    )
    backend_id = _bounded_string(
        backend_value.get("backend_id"),
        "execution_record.backend.backend_id",
        maximum=500,
    )
    backend_version = _bounded_string(
        backend_value.get("backend_version"),
        "execution_record.backend.backend_version",
        maximum=100,
    )
    backend_mode = backend_value.get("mode")
    require(
        backend_mode in {"solo", "network"},
        "invalid_execution",
        "execution_record.backend.mode is unsupported",
    )
    backend = {
        "backend_id": backend_id,
        "backend_version": backend_version,
        "mode": backend_mode,
    }
    if backend_mode == "solo":
        backend["model_adapter"] = _normalize_model_identity(
            backend_value.get("model_adapter")
        )
    plan_digest = _sha256_digest(
        record.get("plan_digest"), "execution_record.plan_digest"
    )
    require(
        plan_digest == plan["plan_digest"],
        "invalid_execution",
        "execution_record.plan_digest does not match the supplied plan",
    )
    planned_task_ids_value = record.get("planned_task_ids")
    require(
        isinstance(planned_task_ids_value, list)
        and 1 <= len(planned_task_ids_value) <= MAX_EXECUTION_TASKS,
        "invalid_execution",
        "planned_task_ids must be a bounded non-empty list",
    )
    planned_task_ids = [
        _identifier(item, "execution_record.planned_task_ids[]")
        for item in planned_task_ids_value
    ]
    require(
        len(planned_task_ids) == len(set(planned_task_ids)),
        "invalid_execution",
        "planned_task_ids must be unique",
    )
    require(
        planned_task_ids == plan["task_order"],
        "invalid_execution",
        "planned_task_ids do not match the supplied plan",
    )
    raw_task_results = record.get("task_results")
    require(
        isinstance(raw_task_results, list)
        and len(raw_task_results) <= len(planned_task_ids),
        "invalid_execution",
        "task_results must be a bounded list",
    )
    task_results = [
        _normalize_task_result(item, f"task_results[{index}]")
        for index, item in enumerate(raw_task_results)
    ]
    synthesis_value = record.get("synthesis_result")
    synthesis_result = (
        _normalize_work_result(
            synthesis_value, "execution_record.synthesis_result", task=False
        )
        if synthesis_value is not None
        else None
    )
    result_ids = [item["task_id"] for item in task_results]
    require(
        result_ids == planned_task_ids[: len(result_ids)],
        "invalid_execution",
        "task_results must be a prefix of the planned deterministic task order",
    )
    rejected_positions = [
        index
        for index, item in enumerate(task_results)
        if item["status"] == "contract_rejected"
    ]
    require(
        not rejected_positions
        or rejected_positions == [len(task_results) - 1],
        "invalid_execution",
        "Only the terminal attempted task can be contract-rejected",
    )
    task_by_id = {task["task_id"]: task for task in plan["tasks"]}
    for task_result in task_results:
        work = task_by_id[task_result["task_id"]]
        require(
            task_result["source_axis_id"] == work.get("source_axis_id"),
            "invalid_execution",
            "A task result's source axis does not match the supplied plan",
            task_id=task_result["task_id"],
        )
        _validate_verification_against_work(
            task_result["verification"],
            work,
            f"task_results[{task_result['task_id']}].verification",
            output=(
                task_result["output"]
                if task_result["output_retention"] == "inline"
                else None
            ),
        )

    events_value = record.get("events")
    require(isinstance(events_value, list), "invalid_execution", "events must be a list")
    events: list[dict[str, Any]] = []
    known_event_usage = _empty_usage()
    completed_events: dict[tuple[str, str], dict[str, Any]] = {}
    started_events: dict[tuple[str, str], dict[str, Any]] = {}
    terminal_events: dict[tuple[str, str], dict[str, Any]] = {}
    backend_start_order: list[tuple[str, str]] = []
    task_evaluation_events: dict[str, dict[str, Any]] = {}
    synthesis_evaluation_events: list[dict[str, Any]] = []
    run_completed_events: list[dict[str, Any]] = []
    record_capacity_events: list[dict[str, Any]] = []
    for index, event in enumerate(events_value):
        event_value = _json_copy(require_object(event, f"events[{index}]"))
        require(
            event_value.get("sequence") == index + 1,
            "invalid_execution",
            "Execution event sequences must be consecutive from one",
        )
        event_type = require_string(event_value.get("event"), f"events[{index}].event")
        require(
            event_type in EXECUTION_EVENT_TYPES,
            "invalid_execution",
            "Execution record contains an unsupported event type",
            event=event_type,
        )
        backend_event_types = {
            "backend_execution_started",
            "backend_execution_failed",
            "backend_response_invalid",
            "backend_response_rejected",
            "backend_response_incomplete",
            "backend_execution_completed",
        }
        work_event_types = backend_event_types | {
            "call_blocked",
            "call_preparation_failed",
        }
        purpose = None
        work_id = None
        if event_type in work_event_types:
            purpose = require_string(
                event_value.get("purpose"), f"events[{index}].purpose"
            )
            require(
                purpose in {"task", "synthesis"},
                "invalid_execution",
                f"events[{index}].purpose is unsupported",
            )
            work_id = _identifier(event_value.get("work_id"), f"events[{index}].work_id")
            require(
                (
                    purpose == "task"
                    and work_id in planned_task_ids
                )
                or (purpose == "synthesis" and work_id == "synthesis"),
                "invalid_execution",
                "An execution event references work outside the supplied plan",
                purpose=purpose,
                work_id=work_id,
            )
        if event_type in backend_event_types:
            event_value["executor"] = _normalize_executor(
                event_value.get("executor"), f"events[{index}].executor"
            )
            event_value["backend_reference"] = _normalize_backend_reference(
                event_value.get("backend_reference"),
                f"events[{index}].backend_reference",
            )
        if event_type == "backend_execution_started":
            _sha256_digest(
                event_value.get("request_digest"), f"events[{index}].request_digest"
            )
            assert purpose is not None and work_id is not None
            key = (purpose, work_id)
            require(
                key not in started_events,
                "invalid_execution",
                "A work item cannot have multiple backend start events",
                purpose=purpose,
                work_id=work_id,
            )
            started_events[key] = event_value
            backend_start_order.append(key)
        if event_type in {
            "backend_execution_completed",
            "backend_response_incomplete",
        }:
            event_value["response_digest"] = _sha256_digest(
                event_value.get("response_digest"),
                f"events[{index}].response_digest",
            )
        if (
            event_type == "backend_response_rejected"
            and event_value.get("response_digest") is not None
        ):
            event_value["response_digest"] = _sha256_digest(
                event_value.get("response_digest"),
                f"events[{index}].response_digest",
            )
        if event_type in {
            "backend_execution_completed",
            "backend_response_invalid",
            "backend_response_rejected",
            "backend_response_incomplete",
        }:
            call_usage = _record_call_usage(
                event_value.get("usage"), f"events[{index}].usage"
            )
            event_value["usage"] = call_usage
            known_event_usage = _add_call_usage(known_event_usage, call_usage)
        if event_type == "backend_execution_completed":
            event_value["output_reference"] = _normalize_content_reference(
                event_value.get("output_reference"),
                f"events[{index}].output_reference",
            )
        if event_type in {
            "backend_execution_failed",
            "backend_response_invalid",
            "backend_response_rejected",
            "backend_response_incomplete",
            "backend_execution_completed",
        }:
            assert purpose is not None and work_id is not None
            key = (purpose, work_id)
            started = started_events.get(key)
            require(
                started is not None
                and key not in terminal_events
                and started["executor"] == event_value["executor"]
                and all(
                    event_value["backend_reference"].get(reference_key)
                    == reference_value
                    for reference_key, reference_value in started[
                        "backend_reference"
                    ].items()
                ),
                "invalid_execution",
                "A backend terminal event must match one preceding start event",
                purpose=purpose,
                work_id=work_id,
            )
            terminal_events[key] = event_value
        if event_type == "backend_execution_completed":
            assert purpose is not None and work_id is not None
            key = (purpose, work_id)
            require(
                key not in completed_events,
                "invalid_execution",
                "A work item cannot have multiple completed backend events",
                purpose=purpose,
                work_id=work_id,
            )
            completed_events[key] = event_value
        if event_type == "task_contract_evaluated":
            task_id = _identifier(
                event_value.get("task_id"), f"events[{index}].task_id"
            )
            outcome = require_string(
                event_value.get("outcome"), f"events[{index}].outcome"
            )
            evaluator = require_string(
                event_value.get("evaluator"), f"events[{index}].evaluator"
            )
            require(
                task_id in planned_task_ids
                and task_id not in task_evaluation_events
                and outcome in {"pass", "reject"}
                and evaluator == "coordinator:deterministic-v1",
                "invalid_execution",
                "A task contract event is inconsistent with the supplied plan",
                task_id=task_id,
            )
            task_evaluation_events[task_id] = event_value
        if event_type == "synthesis_contract_evaluated":
            outcome = require_string(
                event_value.get("outcome"), f"events[{index}].outcome"
            )
            evaluator = require_string(
                event_value.get("evaluator"), f"events[{index}].evaluator"
            )
            require(
                not synthesis_evaluation_events
                and outcome in {"pass", "reject"}
                and evaluator == "coordinator:deterministic-v1",
                "invalid_execution",
                "The synthesis contract event is inconsistent",
            )
            synthesis_evaluation_events.append(event_value)
        if event_type == "run_completed":
            event_value["accepted_tasks"] = _nonnegative_integer(
                event_value.get("accepted_tasks"),
                f"events[{index}].accepted_tasks",
            )
            run_completed_events.append(event_value)
        if event_type == "record_capacity_exceeded":
            require(
                event_value.get("original_status") in RUN_STATUSES
                and event_value.get("retained_task_outputs") == "digest_only",
                "invalid_execution",
                "A record-capacity event is malformed",
            )
            record_capacity_events.append(event_value)
        events.append(event_value)
    require(
        started_events.keys() == terminal_events.keys(),
        "invalid_execution",
        "Every started backend execution must have exactly one terminal event",
    )
    task_start_ids: list[str] = []
    synthesis_started = False
    for purpose, work_id in backend_start_order:
        if purpose == "synthesis":
            require(
                not synthesis_started,
                "invalid_execution",
                "Synthesis can start only once",
            )
            synthesis_started = True
            continue
        require(
            not synthesis_started,
            "invalid_execution",
            "A task cannot start after synthesis",
        )
        task_start_ids.append(work_id)
    require(
        task_start_ids == planned_task_ids[: len(task_start_ids)]
        and len(task_start_ids) in {len(result_ids), len(result_ids) + 1},
        "invalid_execution",
        "Backend task executions must follow the plan's deterministic prefix",
    )
    if len(task_start_ids) == len(result_ids) + 1:
        unmatched_key = ("task", task_start_ids[-1])
        require(
            terminal_events[unmatched_key]["event"]
            != "backend_execution_completed",
            "invalid_execution",
            "A completed task execution requires its contract result",
        )
    completed_task_ids = [
        work_id
        for purpose, work_id in completed_events
        if purpose == "task"
    ]
    require(
        completed_task_ids == result_ids
        and list(task_evaluation_events) == result_ids,
        "invalid_execution",
        "Completed task calls and contract events must exactly match task results",
    )
    for task_result in task_results:
        completed_event = completed_events[("task", task_result["task_id"])]
        evaluation_event = task_evaluation_events[task_result["task_id"]]
        require(
            evaluation_event["sequence"] == completed_event["sequence"] + 1
            and evaluation_event["outcome"]
            == task_result["verification"]["outcome"],
            "invalid_execution",
            "A task contract must be evaluated immediately after its backend response",
            task_id=task_result["task_id"],
        )
    result_by_id = {item["task_id"]: item for item in task_results}
    for position, task_id in enumerate(task_start_ids):
        start_event = started_events[("task", task_id)]
        if backend["mode"] == "solo" and position == 0:
            require(
                start_event["sequence"] == 1,
                "invalid_execution",
                "The first planned task must be the first execution event",
            )
            continue
        if backend["mode"] == "solo":
            previous_task_id = planned_task_ids[position - 1]
            previous_result = result_by_id.get(previous_task_id)
            previous_evaluation = task_evaluation_events.get(previous_task_id)
            require(
                previous_result is not None
                and previous_result["status"] == "contract_valid"
                and previous_evaluation is not None
                and previous_evaluation["outcome"] == "pass"
                and start_event["sequence"] == previous_evaluation["sequence"] + 1,
                "invalid_execution",
                "Each Solo task must start after the preceding planned contract passes",
                task_id=task_id,
                previous_task_id=previous_task_id,
            )
            continue
        for dependency_id in task_by_id[task_id]["depends_on"]:
            dependency_result = result_by_id.get(dependency_id)
            dependency_evaluation = task_evaluation_events.get(dependency_id)
            require(
                dependency_result is not None
                and dependency_result["status"] == "contract_valid"
                and dependency_evaluation is not None
                and dependency_evaluation["outcome"] == "pass"
                and dependency_evaluation["sequence"] < start_event["sequence"],
                "invalid_execution",
                "A Network task must start after every dependency contract passes",
                task_id=task_id,
                dependency_id=dependency_id,
            )
    synthesis_completed = ("synthesis", "synthesis") in completed_events
    require(
        not synthesis_started
        or (
            backend_start_order[-1] == ("synthesis", "synthesis")
            and result_ids == planned_task_ids
            and all(
                item["status"] == "contract_valid" for item in task_results
            )
            and (
                (
                    backend["mode"] == "solo"
                    and started_events[("synthesis", "synthesis")]["sequence"]
                    == task_evaluation_events[planned_task_ids[-1]]["sequence"] + 1
                )
                or (
                    backend["mode"] == "network"
                    and started_events[("synthesis", "synthesis")]["sequence"]
                    > max(
                        event["sequence"]
                        for event in task_evaluation_events.values()
                    )
                )
            )
        ),
        "invalid_execution",
        "Synthesis can start only after every planned task contract passes",
    )
    require(
        synthesis_completed == bool(synthesis_evaluation_events),
        "invalid_execution",
        "A completed synthesis call requires exactly one contract event",
    )
    if synthesis_completed:
        require(
            synthesis_evaluation_events[0]["sequence"]
            == completed_events[("synthesis", "synthesis")]["sequence"] + 1,
            "invalid_execution",
            "Synthesis must be evaluated immediately after its backend response",
        )
    usage_value = require_object(record.get("usage"), "execution_record.usage")
    usage = {
        name: _nonnegative_integer(usage_value.get(name), f"execution_record.usage.{name}")
        for name in _empty_usage()
    }
    require(
        usage == known_event_usage,
        "invalid_execution",
        "Aggregate usage does not match the trace's known provider responses",
    )
    limits_value = require_object(record.get("limits"), "execution_record.limits")
    if backend["mode"] == "solo":
        try:
            limits = ExecutionLimits(**limits_value).normalized()
        except TypeError as exc:
            raise DomainError(
                "invalid_execution",
                "execution_record.limits has unsupported fields",
                details={"reason": str(exc)},
            ) from exc
        model_identity = backend["model_adapter"]
        expected_executor = {"kind": "model_adapter", "identity": model_identity}
        require(
            all(item["executor"] == expected_executor for item in task_results)
            and (
                synthesis_result is None
                or synthesis_result["executor"] == expected_executor
            ),
            "invalid_execution",
            "Solo task results must use the backend's one model identity",
        )
        for item in task_results:
            if item["output_retention"] != "inline":
                continue
            normalized_response = {
                "output": item["output"],
                "finish_reason": "completed",
                "response_id": item["backend_reference"].get("response_id"),
                "usage": item["usage"],
            }
            require(
                content_digest(normalized_response) == item["response_digest"],
                "invalid_execution",
                "A Solo task output does not match its model response digest",
                task_id=item["task_id"],
            )
    else:
        limits = _json_copy(limits_value)
    final_output = record.get("final_output")
    final_verification = record.get("final_verification")
    error = record.get("error")
    accounting_complete = record.get("accounting_complete")
    require(
        isinstance(accounting_complete, bool),
        "invalid_execution",
        "execution_record.accounting_complete must be boolean",
    )
    require(
        not accounting_complete
        or not any(
            event["event"] == "backend_execution_failed" for event in events
        ),
        "invalid_execution",
        "Unmetered backend failures require incomplete accounting",
    )
    require(
        record.get("verification_boundary")
        == "deterministic_schema_and_accept_spec_only"
        and record.get("semantic_verification") == "unverified",
        "invalid_execution",
        "The execution record overstates its verification boundary",
    )
    if final_output is not None:
        final_output = _json_copy(require_object(final_output, "execution_record.final_output"))
    if final_verification is not None:
        final_verification = _normalize_verification(
            final_verification, "execution_record.final_verification"
        )
        require(
            final_output is not None,
            "invalid_execution",
            "Final verification requires a retained synthesis output",
        )
        _validate_verification_against_work(
            final_verification,
            plan["synthesis"],
            "execution_record.final_verification",
            output=final_output,
        )
    if error is not None:
        error = _json_copy(require_object(error, "execution_record.error"))
        require_string(error.get("code"), "execution_record.error.code")

    expected_stop_reasons = {
        "completed": {"completed"},
        "rejected": {"quality"},
        "failed": {"adapter", "record"},
        "budget_exceeded": {"budget"},
    }[status]
    require(
        stop_reason in expected_stop_reasons,
        "invalid_execution",
        "Run status and stop reason are inconsistent",
    )
    is_record_capacity_stop = status == "failed" and stop_reason == "record"
    require(
        len(record_capacity_events) == (1 if is_record_capacity_stop else 0),
        "invalid_execution",
        "A record-capacity trace must match the terminal status",
    )
    original_status = status
    if is_record_capacity_stop:
        capacity_event = record_capacity_events[0]
        original_status = capacity_event["original_status"]
        require(
            capacity_event["sequence"] == len(events)
            and all(
                item["output_retention"] == "digest_only"
                for item in task_results
            ),
            "invalid_execution",
            "A record-capacity event must be terminal and retain only digest evidence",
        )
    underlying_completed = original_status == "completed"
    require(
        len(run_completed_events) == (1 if underlying_completed else 0),
        "invalid_execution",
        "The run-completed event does not match the executed terminal path",
    )
    if underlying_completed:
        run_completed_event = run_completed_events[0]
        require(
            synthesis_completed
            and synthesis_evaluation_events[0]["outcome"] == "pass"
            and run_completed_event["accepted_tasks"] == len(planned_task_ids)
            and run_completed_event["sequence"]
            == synthesis_evaluation_events[0]["sequence"] + 1
            and (
                (
                    is_record_capacity_stop
                    and run_completed_event["sequence"] + 1 == len(events)
                )
                or (
                    not is_record_capacity_stop
                    and run_completed_event["sequence"] == len(events)
                )
            ),
            "invalid_execution",
            "A completed trace must end after successful synthesis evaluation",
        )
    require(
        is_record_capacity_stop
        or synthesis_completed == (synthesis_result is not None),
        "invalid_execution",
        "A synthesis result must exactly match a completed synthesis call",
    )
    require(
        not any(
            item["output_retention"] == "digest_only" for item in task_results
        )
        or (status == "failed" and stop_reason == "record"),
        "invalid_execution",
        "Digest-only task evidence is only valid for a record-capacity failure",
    )
    all_tasks_contract_valid = (
        result_ids == planned_task_ids
        and all(item["status"] == "contract_valid" for item in task_results)
    )
    final_passed = (
        final_output is not None
        and final_verification is not None
        and final_verification.get("outcome") == "pass"
    )
    final_rejected = (
        final_output is not None
        and final_verification is not None
        and final_verification.get("outcome") == "reject"
    )
    for task_result in task_results:
        completed_event = completed_events.get(("task", task_result["task_id"]))
        require(
            completed_event is not None
            and completed_event["executor"] == task_result["executor"]
            and completed_event["backend_reference"]
            == task_result["backend_reference"]
            and completed_event["response_digest"]
            == task_result["response_digest"]
            and completed_event["output_reference"]
            == task_result["output_reference"]
            and completed_event["usage"] == task_result["usage"],
            "invalid_execution",
            "A task result does not match its completed backend execution event",
            task_id=task_result["task_id"],
        )
    if synthesis_result is not None:
        completed_event = completed_events.get(("synthesis", "synthesis"))
        require(
            completed_event is not None
            and completed_event["executor"] == synthesis_result["executor"]
            and completed_event["backend_reference"]
            == synthesis_result["backend_reference"]
            and completed_event["response_digest"]
            == synthesis_result["response_digest"]
            and completed_event["output_reference"]
            == synthesis_result["output_reference"]
            and completed_event["usage"] == synthesis_result["usage"],
            "invalid_execution",
            "The synthesis result does not match its completed backend execution event",
        )
        require(
            synthesis_evaluation_events[0]["outcome"]
            == (
                "pass"
                if synthesis_result["status"] == "contract_valid"
                else "reject"
            ),
            "invalid_execution",
            "The synthesis contract event does not match its result",
        )
        expected_synthesis_outcome = (
            "pass"
            if synthesis_result["status"] == "contract_valid"
            else "reject"
        )
        require(
            final_output is not None
            and final_verification is not None
            and final_verification["outcome"] == expected_synthesis_outcome,
            "invalid_execution",
            "Final fields must match the synthesis contract status",
        )
        require(
            _content_digest_reference(final_output)
            == synthesis_result["output_reference"],
            "invalid_execution",
            "The final output does not match its synthesis output commitment",
        )
        if backend["mode"] == "solo":
            normalized_response = {
                "output": final_output,
                "finish_reason": "completed",
                "response_id": synthesis_result["backend_reference"].get(
                    "response_id"
                ),
                "usage": synthesis_result["usage"],
            }
            require(
                content_digest(normalized_response)
                == synthesis_result["response_digest"],
                "invalid_execution",
                "The final output does not match its model response digest",
            )
    else:
        require(
            final_output is None and final_verification is None,
            "invalid_execution",
            "Final fields require a synthesis execution result",
        )
    if status == "completed":
        require(
            all_tasks_contract_valid
            and synthesis_result is not None
            and synthesis_result["status"] == "contract_valid"
            and final_passed
            and error is None,
            "invalid_execution",
            "A completed run requires every planned task and synthesis contract to pass",
        )
        if backend["mode"] == "solo":
            require(
                accounting_complete,
                "invalid_execution",
                "A Solo run cannot complete with unknown provider accounting",
            )
    elif status == "rejected":
        task_rejected = bool(
            task_results
            and task_results[-1]["status"] == "contract_rejected"
        )
        require(
            error is not None
            and (
                (task_rejected and final_output is None and final_verification is None)
                or (
                    all_tasks_contract_valid
                    and synthesis_result is not None
                    and synthesis_result["status"] == "contract_rejected"
                    and final_rejected
                )
            ),
            "invalid_execution",
            "A rejected run requires an actual task or synthesis contract rejection",
        )
    elif status == "failed":
        retained_task_claims_are_valid = all(
            item["status"] == "contract_valid" for item in task_results
        )
        if stop_reason == "record" and task_results:
            retained_task_claims_are_valid = (
                all(
                    item["status"] == "contract_valid"
                    for item in task_results[:-1]
                )
                and task_results[-1]["status"]
                in {"contract_valid", "contract_rejected"}
            )
        require(
            error is not None
            and final_output is None
            and final_verification is None
            and retained_task_claims_are_valid,
            "invalid_execution",
            "A failed run cannot retain a successful or rejected final claim",
        )
        if stop_reason == "record":
            require(
                error.get("code") == "execution_record_capacity_exceeded",
                "invalid_execution",
                "A record-capacity stop requires its matching typed error",
            )
    else:
        require(
            error is not None
            and error.get("code") == "execution_budget_exceeded"
            and final_output is None
            and final_verification is None
            and all(item["status"] == "contract_valid" for item in task_results),
            "invalid_execution",
            "A budget-exceeded run requires a matching typed budget failure",
        )

    if backend["mode"] == "solo" and accounting_complete and status != "budget_exceeded":
        require(
            not _budget_dimensions(limits, usage),
            "invalid_execution",
            "A non-budget Solo terminal record exceeds its declared run limits",
        )
    core = _record_core(
        run_id=run_id,
        status=status,
        stop_reason=stop_reason,
        backend=backend,
        plan_digest=plan_digest,
        planned_task_ids=planned_task_ids,
        task_results=task_results,
        synthesis_result=synthesis_result,
        final_output=final_output,
        final_verification=final_verification,
        usage=usage,
        limits=limits,
        events=events,
        error=error,
        accounting_complete=accounting_complete,
    )
    require(
        len(canonical_json(core).encode("utf-8")) <= MAX_EXECUTION_RECORD_BYTES,
        "execution_record_too_large",
        f"An execution record must be at most {MAX_EXECUTION_RECORD_BYTES} bytes",
    )
    require(
        canonical_json(raw_core) == canonical_json(core),
        "invalid_execution",
        "The execution record contains non-canonical or unsupported fields",
    )
    require(
        record.get("record_digest") == content_digest(core),
        "invalid_execution",
        "record_digest does not match the execution record content",
    )
    return {**core, "record_digest": content_digest(core)}


class SingleModelBackend:
    """Execute a reviewed task graph with isolated calls to one model identity."""

    def __init__(self, adapter: ModelAdapter, *, limits: ExecutionLimits) -> None:
        self._adapter = adapter
        self._model_identity = _normalize_model_identity(adapter.identity)
        self._executor = {
            "kind": "model_adapter",
            "identity": _json_copy(self._model_identity),
        }
        self._limits = limits.normalized()
        self._identity = {
            "backend_id": "single_model",
            "backend_version": "1",
            "mode": "solo",
            "model_adapter": _json_copy(self._model_identity),
        }

    @property
    def identity(self) -> dict[str, Any]:
        return _json_copy(self._identity)

    def _request(
        self,
        *,
        run_id: str,
        purpose: str,
        work_id: str,
        problem: dict[str, Any],
        work: dict[str, Any],
        dependencies: dict[str, dict[str, Any]],
        usage: dict[str, int],
        seed: int,
    ) -> dict[str, Any]:
        remaining = _remaining(self._limits, usage)
        checked_work = {
            key: work[key]
            for key in (
                "task_id",
                "role",
                "objective",
                "context",
                "output_schema",
                "accept_spec",
                "source_axis_id",
            )
            if key in work
        }
        if purpose == "synthesis":
            checked_work = {
                "objective": work["objective"],
                "context": work["context"],
                "output_schema": work["output_schema"],
                "accept_spec": _provider_accept_spec(work["accept_spec"]),
            }
        else:
            checked_work["accept_spec"] = _provider_accept_spec(
                work["accept_spec"]
            )
        request = {
            "protocol_version": "1",
            "kind": MODEL_REQUEST_KIND,
            "call_id": _call_id(run_id, purpose, work_id),
            "purpose": purpose,
            "seed": seed,
            "problem": problem,
            "work": checked_work,
            "dependencies": dependencies,
            "budget": {
                "currency": self._limits["currency"],
                "remaining": remaining,
                "max_output_tokens": min(
                    self._limits["max_output_tokens_per_call"],
                    remaining["output_tokens"],
                ),
            },
        }
        require(
            len(canonical_json(request).encode("utf-8")) <= MAX_MODEL_REQUEST_BYTES,
            "model_request_too_large",
            f"A model request must be at most {MAX_MODEL_REQUEST_BYTES} bytes",
        )
        return _json_copy(request)

    def execute(self, plan_value: Any, *, run_id: str) -> dict[str, Any]:
        plan = validate_execution_plan(plan_value)
        checked_run_id = _identifier(run_id, "run_id")
        usage = _empty_usage()
        task_results: list[dict[str, Any]] = []
        outputs: dict[str, dict[str, Any]] = {}
        events: list[dict[str, Any]] = []
        accounting_complete = True

        def event(kind: str, **details: Any) -> None:
            events.append(
                {
                    "sequence": len(events) + 1,
                    "event": kind,
                    **_json_copy(details),
                }
            )

        def finish(
            status: str,
            stop_reason: str,
            *,
            final_output: dict[str, Any] | None = None,
            final_verification: dict[str, Any] | None = None,
            synthesis_result: dict[str, Any] | None = None,
            error: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            values = {
                "run_id": checked_run_id,
                "status": status,
                "stop_reason": stop_reason,
                "backend": self._identity,
                "plan_digest": plan["plan_digest"],
                "planned_task_ids": plan["task_order"],
                "task_results": task_results,
                "synthesis_result": synthesis_result,
                "final_output": final_output,
                "final_verification": final_verification,
                "usage": usage,
                "limits": self._limits,
                "events": events,
                "error": error,
                "accounting_complete": accounting_complete,
            }
            try:
                return _finish_record(**values)
            except DomainError as exc:
                if exc.code != "execution_record_too_large":
                    raise
            compact_task_results: list[dict[str, Any]] = []
            for task_result in task_results:
                compact = _json_copy(task_result)
                if compact.get("output_retention") != "digest_only":
                    compact["output"] = _content_digest_reference(compact["output"])
                    compact["output_retention"] = "digest_only"
                compact_task_results.append(compact)
            event(
                "record_capacity_exceeded",
                original_status=status,
                retained_task_outputs="digest_only",
            )
            return _finish_record(
                run_id=checked_run_id,
                status="failed",
                stop_reason="record",
                backend=self._identity,
                plan_digest=plan["plan_digest"],
                planned_task_ids=plan["task_order"],
                task_results=compact_task_results,
                synthesis_result=None,
                final_output=None,
                final_verification=None,
                usage=usage,
                limits=self._limits,
                events=events,
                error={"code": "execution_record_capacity_exceeded"},
                accounting_complete=accounting_complete,
            )

        required_calls = len(plan["tasks"]) + 1
        if required_calls > self._limits["model_calls"]:
            event(
                "run_rejected",
                reason="model_call_budget",
                required_calls=required_calls,
                available_calls=self._limits["model_calls"],
            )
            return finish(
                "budget_exceeded",
                "budget",
                error={
                    "code": "execution_budget_exceeded",
                    "dimensions": ["model_calls"],
                },
            )

        task_by_id = {task["task_id"]: task for task in plan["tasks"]}

        def invoke(
            purpose: str,
            work_id: str,
            work: dict[str, Any],
            dependencies: dict[str, dict[str, Any]],
        ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            nonlocal usage, accounting_complete
            remaining = _remaining(self._limits, usage)
            if remaining["model_calls"] <= 0 or remaining["output_tokens"] <= 0:
                dimensions = [
                    name
                    for name in ("model_calls", "output_tokens")
                    if remaining[name] <= 0
                ]
                event("call_blocked", purpose=purpose, work_id=work_id, dimensions=dimensions)
                return None, {
                    "code": "execution_budget_exceeded",
                    "dimensions": dimensions,
                }
            try:
                request = self._request(
                    run_id=checked_run_id,
                    purpose=purpose,
                    work_id=work_id,
                    problem=plan["problem"],
                    work=work,
                    dependencies=dependencies,
                    usage=usage,
                    seed=plan["seed"],
                )
            except DomainError as exc:
                event(
                    "call_preparation_failed",
                    purpose=purpose,
                    work_id=work_id,
                    error_code=exc.code,
                )
                return None, {"code": exc.code}
            initial_reference = {
                "kind": "model_call",
                "call_id": request["call_id"],
            }
            event(
                "backend_execution_started",
                purpose=purpose,
                work_id=work_id,
                executor=self._executor,
                backend_reference=initial_reference,
                request_digest=content_digest(request),
            )
            try:
                raw_response = self._adapter.invoke(_json_copy(request))
            except Exception as exc:  # Adapters are an untrusted integration boundary.
                accounting_complete = False
                event(
                    "backend_execution_failed",
                    purpose=purpose,
                    work_id=work_id,
                    executor=self._executor,
                    backend_reference=initial_reference,
                    error_code="model_adapter_error",
                    error_type=type(exc).__name__,
                )
                return None, {
                    "code": "model_adapter_error",
                    "error_type": type(exc).__name__,
                }

            try:
                raw_response_object = require_object(
                    raw_response, "model_response"
                )
                call_usage = _normalize_usage(
                    raw_response_object.get("usage"), "model_response.usage"
                )
            except DomainError as exc:
                accounting_complete = False
                event(
                    "backend_execution_failed",
                    purpose=purpose,
                    work_id=work_id,
                    executor=self._executor,
                    backend_reference=initial_reference,
                    error_code=exc.code,
                )
                return None, {"code": exc.code}

            usage = _add_call_usage(usage, call_usage)
            exceeded = _budget_dimensions(self._limits, usage)
            if call_usage["output_tokens"] > request["budget"]["max_output_tokens"]:
                exceeded.append("max_output_tokens_per_call")
            if exceeded:
                raw_response_digest = None
                try:
                    raw_response_digest = content_digest(raw_response_object)
                except DomainError:
                    pass
                event_details: dict[str, Any] = {
                    "purpose": purpose,
                    "work_id": work_id,
                    "executor": self._executor,
                    "backend_reference": initial_reference,
                    "usage": call_usage,
                    "dimensions": exceeded,
                }
                if raw_response_digest is not None:
                    event_details["response_digest"] = raw_response_digest
                event("backend_response_rejected", **event_details)
                return None, {
                    "code": "execution_budget_exceeded",
                    "dimensions": exceeded,
                }

            try:
                response = _normalize_model_response(
                    raw_response_object, normalized_usage=call_usage
                )
            except DomainError as exc:
                event(
                    "backend_response_invalid",
                    purpose=purpose,
                    work_id=work_id,
                    executor=self._executor,
                    backend_reference=initial_reference,
                    error_code=exc.code,
                    usage=call_usage,
                )
                return None, {"code": exc.code}

            backend_reference = dict(initial_reference)
            if response["response_id"] is not None:
                backend_reference["response_id"] = response["response_id"]
            response_digest = content_digest(response)
            output_reference = _content_digest_reference(response["output"])
            if response["finish_reason"] != "completed":
                event(
                    "backend_response_incomplete",
                    purpose=purpose,
                    work_id=work_id,
                    executor=self._executor,
                    backend_reference=backend_reference,
                    response_digest=response_digest,
                    finish_reason=response["finish_reason"],
                    usage=response["usage"],
                )
                return None, {
                    "code": "incomplete_model_response",
                    "finish_reason": response["finish_reason"],
                }
            event(
                "backend_execution_completed",
                purpose=purpose,
                work_id=work_id,
                executor=self._executor,
                backend_reference=backend_reference,
                response_digest=response_digest,
                output_reference=output_reference,
                usage=response["usage"],
            )
            response["executor"] = _json_copy(self._executor)
            response["backend_reference"] = _json_copy(backend_reference)
            response["response_digest"] = response_digest
            response["output_reference"] = output_reference
            return response, None

        for task_id in plan["task_order"]:
            task = task_by_id[task_id]
            dependencies = {
                dependency: outputs[dependency] for dependency in task["depends_on"]
            }
            response, error = invoke("task", task_id, task, dependencies)
            if error is not None:
                budget = error["code"] == "execution_budget_exceeded"
                return finish(
                    "budget_exceeded" if budget else "failed",
                    "budget" if budget else "adapter",
                    error=error,
                )
            assert response is not None
            verification = _compact_verification(
                evaluate_result(
                    task["accept_spec"],
                    outputs=response["output"],
                    status="success",
                    stop_reason="completed",
                    output_schema_errors=validate_instance(
                        response["output"], task["output_schema"]
                    ),
                )
            )
            task_result = {
                "task_id": task_id,
                "source_axis_id": task.get("source_axis_id"),
                "status": (
                    "contract_valid"
                    if verification["outcome"] == "pass"
                    else "contract_rejected"
                ),
                "executor": response["executor"],
                "backend_reference": response["backend_reference"],
                "response_digest": response["response_digest"],
                "output_reference": response["output_reference"],
                "output_retention": "inline",
                "output": response["output"],
                "verification": verification,
                "usage": response["usage"],
            }
            task_results.append(task_result)
            event(
                "task_contract_evaluated",
                task_id=task_id,
                outcome=verification["outcome"],
                evaluator=verification["evaluator"],
            )
            if verification["outcome"] != "pass":
                return finish(
                    "rejected",
                    "quality",
                    error={"code": "task_accept_spec_rejected", "task_id": task_id},
                )
            outputs[task_id] = response["output"]
            if len(outputs) < len(plan["tasks"]):
                try:
                    self._request(
                        run_id=checked_run_id,
                        purpose="synthesis",
                        work_id="synthesis",
                        problem=plan["problem"],
                        work=plan["synthesis"],
                        dependencies={
                            completed_id: outputs[completed_id]
                            for completed_id in plan["task_order"]
                            if completed_id in outputs
                        },
                        usage=usage,
                        seed=plan["seed"],
                    )
                except DomainError as exc:
                    event(
                        "call_preparation_failed",
                        purpose="synthesis",
                        work_id="synthesis",
                        error_code=exc.code,
                        reason="cumulative_task_outputs",
                    )
                    return finish(
                        "failed",
                        "adapter",
                        error={"code": exc.code},
                    )

        synthesis_response, error = invoke(
            "synthesis",
            "synthesis",
            plan["synthesis"],
            {task_id: outputs[task_id] for task_id in plan["task_order"]},
        )
        if error is not None:
            budget = error["code"] == "execution_budget_exceeded"
            return finish(
                "budget_exceeded" if budget else "failed",
                "budget" if budget else "adapter",
                error=error,
            )
        assert synthesis_response is not None
        final_verification = _compact_verification(
            evaluate_result(
                plan["synthesis"]["accept_spec"],
                outputs=synthesis_response["output"],
                status="success",
                stop_reason="completed",
                output_schema_errors=validate_instance(
                    synthesis_response["output"], plan["synthesis"]["output_schema"]
                ),
            )
        )
        synthesis_result = {
            "status": (
                "contract_valid"
                if final_verification["outcome"] == "pass"
                else "contract_rejected"
            ),
            "executor": synthesis_response["executor"],
            "backend_reference": synthesis_response["backend_reference"],
            "response_digest": synthesis_response["response_digest"],
            "output_reference": synthesis_response["output_reference"],
            "usage": synthesis_response["usage"],
        }
        event(
            "synthesis_contract_evaluated",
            outcome=final_verification["outcome"],
            evaluator=final_verification["evaluator"],
        )
        if final_verification["outcome"] != "pass":
            return finish(
                "rejected",
                "quality",
                final_output=synthesis_response["output"],
                final_verification=final_verification,
                synthesis_result=synthesis_result,
                error={"code": "synthesis_accept_spec_rejected"},
            )
        event("run_completed", accepted_tasks=len(task_results))
        return finish(
            "completed",
            "completed",
            final_output=synthesis_response["output"],
            final_verification=final_verification,
            synthesis_result=synthesis_result,
        )
