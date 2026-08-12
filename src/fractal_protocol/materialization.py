from __future__ import annotations

from typing import Any

from .decomposition import validate_decomposition_proposal
from .errors import require
from .protocol import (
    canonical_json,
    content_digest,
    require_object,
    require_string,
    validate_task_spec,
)


MAX_MATERIALIZED_CHILDREN = 20
MAX_DELEGATION_PAYLOAD_BYTES = 512 * 1024


def _normalize_admitted_manifest_digests(value: Any) -> list[str]:
    require(
        isinstance(value, (set, list, tuple)),
        "invalid_materialization",
        "admitted_manifest_digests must be a set, list, or tuple",
    )
    admitted = sorted(
        {require_string(item, "admitted_manifest_digests[]") for item in value}
    )
    for digest in admitted:
        suffix = digest.removeprefix("sha256:")
        require(
            digest.startswith("sha256:")
            and len(suffix) == 64
            and all(character in "0123456789abcdef" for character in suffix),
            "invalid_materialization",
            "Every admitted manifest digest must be a full lowercase SHA-256 digest",
        )
    return admitted


def decomposition_execution_failures(proposal_value: Any) -> list[str]:
    """Return structural blockers shared by local and network execution.

    Passing this check does not authenticate the semantic oracle, prove that
    caller-supplied probes were independently created, or authorize payment.
    """

    proposal = validate_decomposition_proposal(proposal_value)
    failures: list[str] = []

    def visit(node: dict[str, Any], path: str) -> None:
        if node["status"] == "budget_exhausted":
            failures.append(f"oracle_budget_exhausted:{path}")
        if not node["completeness"]["passed"]:
            failures.append(f"completeness_not_established:{path}")
        if not node["routing"]["passed"]:
            failures.append(f"routing_sanity_not_established:{path}")
        if node["routing"]["probe_source"] != "provided":
            failures.append(f"independent_routing_probes_missing:{path}")
        for axis in node["axes"]:
            marginal = axis["marginal_value"]
            if marginal is None:
                failures.append(f"marginal_value_missing:{axis['axis_id']}")
            elif marginal["recursion_decision"] == "probe":
                failures.append(f"marginal_value_probe_required:{axis['axis_id']}")
            elif marginal["recursion_decision"] == "node_budget_exhausted":
                failures.append(f"node_budget_exhausted:{axis['axis_id']}")
            elif marginal["recursion_decision"] == "oracle_budget_exhausted":
                failures.append(f"oracle_budget_exhausted:{axis['axis_id']}")
            if axis["decomposition"] is not None:
                visit(axis["decomposition"], axis["axis_id"])

    visit(proposal["root"], "root")
    return failures


def build_materialization_plan(
    proposal_value: Any,
    axis_tasks: Any,
    *,
    admitted_manifest_digests: set[str] | list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Map proposal root axes to executable task specifications.

    This is a client-side advisory compiler. It never attests review, creates
    tasks, or authorizes money. The caller must obtain the admitted-manifest
    snapshot from the coordinator; the existing delegation review remains the
    economic gate.
    """

    proposal = validate_decomposition_proposal(proposal_value)
    require(
        isinstance(axis_tasks, list),
        "invalid_materialization",
        "axis_tasks must be a list",
    )
    admitted = _normalize_admitted_manifest_digests(admitted_manifest_digests)
    root_axes = proposal["root"]["axes"]
    root_axis_ids = [axis["axis_id"] for axis in root_axes]
    root_axis_set = set(root_axis_ids)
    seen: set[str] = set()
    normalized_bindings: list[dict[str, Any]] = []
    for index, raw in enumerate(axis_tasks):
        binding = require_object(raw, f"axis_tasks[{index}]")
        axis_id = require_string(binding.get("axis_id"), f"axis_tasks[{index}].axis_id")
        require(
            axis_id in root_axis_set,
            "invalid_materialization",
            "Only a root decomposition axis can be materialized at this task boundary",
            axis_id=axis_id,
        )
        require(
            axis_id not in seen,
            "invalid_materialization",
            "A conceptual axis can be materialized at most once",
            axis_id=axis_id,
        )
        seen.add(axis_id)
        task = validate_task_spec(binding.get("task"))
        normalized_bindings.append(
            {
                "axis_id": axis_id,
                "task": task,
                "capability_admitted": task["required_capability"] in admitted,
            }
        )
    binding_by_axis = {item["axis_id"]: item for item in normalized_bindings}
    normalized_bindings = [
        binding_by_axis[axis_id]
        for axis_id in root_axis_ids
        if axis_id in binding_by_axis
    ]
    missing_axis_ids = [axis_id for axis_id in root_axis_ids if axis_id not in seen]
    unresolved_capabilities = [
        {
            "axis_id": item["axis_id"],
            "required_capability": item["task"]["required_capability"],
        }
        for item in normalized_bindings
        if not item["capability_admitted"]
    ]
    structural_failures = decomposition_execution_failures(proposal)
    if len(root_axes) > MAX_MATERIALIZED_CHILDREN:
        structural_failures.append("coordinator_child_limit_exceeded")
    ready = not (
        missing_axis_ids or unresolved_capabilities or structural_failures
    )
    core = {
        "protocol_version": "1",
        "kind": "decomposition_materialization_plan",
        "status": "ready" if ready else "blocked",
        "proposal_digest": proposal["proposal_digest"],
        "admission_snapshot_digest": content_digest(
            sorted(
                {
                    item["task"]["required_capability"]
                    for item in normalized_bindings
                    if item["capability_admitted"]
                }
            )
        ),
        "bindings": normalized_bindings,
        "missing_axis_ids": missing_axis_ids,
        "unresolved_capabilities": unresolved_capabilities,
        "structural_failures": structural_failures,
    }
    return {**core, "plan_digest": content_digest(core)}


def delegation_payload_from_plan(
    plan_value: Any,
    *,
    idempotency_key: str,
    lease_token: str,
    admitted_manifest_digests: set[str] | list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Compile a ready plan into the existing provider delegation request."""

    plan = require_object(plan_value, "materialization_plan")
    digest = plan.get("plan_digest")
    core = {key: value for key, value in plan.items() if key != "plan_digest"}
    require(
        digest == content_digest(core),
        "invalid_materialization",
        "plan_digest does not match the materialization content",
    )
    require(
        plan.get("protocol_version") == "1"
        and plan.get("kind") == "decomposition_materialization_plan",
        "invalid_materialization",
        "Unsupported materialization plan envelope",
    )
    require(
        plan.get("status") == "ready"
        and not plan.get("missing_axis_ids")
        and not plan.get("unresolved_capabilities")
        and not plan.get("structural_failures"),
        "materialization_blocked",
        "Only a complete, clean, capability-resolved plan can become child tasks",
    )
    admitted = _normalize_admitted_manifest_digests(admitted_manifest_digests)
    bindings = plan.get("bindings")
    require(
        isinstance(bindings, list)
        and bindings
        and len(bindings) <= MAX_MATERIALIZED_CHILDREN,
        "invalid_materialization",
        f"A ready plan must contain between 1 and {MAX_MATERIALIZED_CHILDREN} bindings",
    )
    children = []
    planned_capabilities: set[str] = set()
    for index, raw in enumerate(bindings):
        binding = require_object(raw, f"materialization_plan.bindings[{index}]")
        require(
            binding.get("capability_admitted") is True,
            "materialization_blocked",
            "Every materialized capability must be admitted",
        )
        task = validate_task_spec(binding.get("task"))
        planned_capabilities.add(task["required_capability"])
        require(
            task["required_capability"] in admitted,
            "materialization_blocked",
            "A materialized capability is no longer admitted",
        )
        children.append(task)
    require(
        plan.get("admission_snapshot_digest")
        == content_digest(sorted(planned_capabilities)),
        "invalid_materialization",
        "The plan's bound-capability snapshot does not match its tasks",
    )
    checked_idempotency_key = require_string(idempotency_key, "idempotency_key")
    require(
        len(checked_idempotency_key) <= 200,
        "invalid_materialization",
        "idempotency_key is too long for the coordinator",
    )
    payload = {
        "idempotency_key": checked_idempotency_key,
        "lease_token": require_string(lease_token, "lease_token"),
        "children": children,
    }
    encoded = canonical_json(payload).encode("utf-8")
    require(
        len(encoded) <= MAX_DELEGATION_PAYLOAD_BYTES,
        "materialization_payload_too_large",
        f"The delegation payload exceeds {MAX_DELEGATION_PAYLOAD_BYTES} bytes",
    )
    return payload
