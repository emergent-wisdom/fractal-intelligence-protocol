from __future__ import annotations

import json
from typing import Any

from demo_conceptual_decomposition import IllustrativeOracle
from fractal_protocol.decomposition import (
    ConceptualDecompositionEngine,
    DecompositionConfig,
)
from fractal_protocol.execution import (
    ExecutionLimits,
    SingleModelBackend,
    execution_plan_from_decomposition,
    validate_execution_record,
)
from fractal_protocol.protocol import canonical_json


BRANCH_SCHEMA = {
    "type": "object",
    "required": ["analysis", "uncertainties"],
    "properties": {
        "analysis": {"type": "string", "minLength": 1},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

SYNTHESIS_SCHEMA = {
    "type": "object",
    "required": ["answer", "limitations"],
    "properties": {
        "answer": {"type": "string", "minLength": 1},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


class ExecutableIllustrativeOracle(IllustrativeOracle):
    """Wiring fixture with an explicit scripted leaf-depth decision."""

    identity = {
        **IllustrativeOracle.identity,
        "adapter_id": "example:executable-illustrative-oracle",
        "marginal_value_source": "hand-authored-wiring-fixture-v1",
    }

    def estimate_marginal_value(
        self, concept: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        return {
            "evidence_status": "sufficient",
            "shallow_value": 0.5,
            "deep_value": 0.5,
            "exploration_credit": 0,
            "shallow_cost": 1,
            "deep_cost": 2,
            "value_unit": "normalized_outcome_value",
            "cost_unit": "normalized_compute_cost",
            "sample_count": 1,
            "uncertainty": 1,
            "basis": "hand_authored_wiring_fixture",
            "rationale": "Keep the demo root-only; this is not empirical MVR evidence.",
        }


def required(path: str) -> dict[str, Any]:
    return {
        "seam": "hard",
        "minimum_pass_rate": 1.0,
        "clauses": [
            {
                "id": "required-field",
                "path": path,
                "operator": "exists",
                "critical": True,
                "disclosure": "public",
            }
        ],
    }


class ScriptedSoloModel:
    """Deterministic stand-in showing the live provider adapter contract."""

    identity = {
        "adapter_id": "example:scripted-solo-model",
        "adapter_version": "1",
        "model": "scripted-general-model-v1",
    }

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if request["purpose"] == "task":
            role = request["work"]["role"]
            output = {
                "analysis": (
                    f"{role}: address the assigned responsibility while preserving "
                    "the stated customer outcome."
                ),
                "uncertainties": ["This is a scripted fixture, not model evidence."],
            }
        else:
            branch_count = len(request["dependencies"])
            output = {
                "answer": (
                    f"The proposal-reviewed root plan produced {branch_count} contract-valid branch "
                    "results and combined them into one candidate answer."
                ),
                "limitations": [
                    "The demo checks structure and declared gates, not semantic truth.",
                    "Replace this scripted adapter with a live model adapter for real inference.",
                ],
            }
        approximate_input_tokens = max(1, len(canonical_json(request)) // 4)
        approximate_output_tokens = max(1, len(canonical_json(output)) // 4)
        return {
            "output": output,
            "finish_reason": "completed",
            "response_id": f"scripted-response-{self.calls}",
            "usage": {
                "input_tokens": approximate_input_tokens,
                "output_tokens": approximate_output_tokens,
                "reasoning_tokens": 0,
                "monetary_microunits": 0,
                "complete": True,
            },
        }


def main() -> None:
    proposal = ConceptualDecompositionEngine(
        ExecutableIllustrativeOracle(),
        config=DecompositionConfig(max_depth=0, routing_probe_count=4),
    ).decompose(
        {
            "name": "Paid agent problem solving",
            "description": "Turn a request into checked work and settle its outcome",
            "context": {"mode": "solo"},
        },
        routing_probes=[
            {
                "probe_id": "heldout-001",
                "task": "Resolve an ambiguous customer success criterion",
            },
            {
                "probe_id": "heldout-002",
                "task": "Produce a candidate artifact from an accepted specification",
            },
            {
                "probe_id": "heldout-003",
                "task": "Check a submitted artifact against hidden tests",
            },
            {
                "probe_id": "heldout-004",
                "task": "Move accepted reward from escrow to provider payable",
            },
        ],
    )

    axis_bindings = [
        {
            "axis_id": axis["axis_id"],
            "objective": axis["candidate"]["description"],
            "context": {"bounds": axis["candidate"]["bounds"]},
            "output_schema": BRANCH_SCHEMA,
            "accept_spec": required("/analysis"),
        }
        for axis in proposal["root"]["axes"]
    ]
    plan = execution_plan_from_decomposition(
        proposal,
        problem={
            "statement": "Design the smallest honest launch for a paid agent solver",
            "context": {"constraint": "centralized alpha first"},
        },
        axis_bindings=axis_bindings,
        synthesis={
            "objective": "Combine the reviewed branch results into one concise answer",
            "context": {},
            "output_schema": SYNTHESIS_SCHEMA,
            "accept_spec": required("/answer"),
        },
        review={
            "decision": "approved",
            "proposal_digest": proposal["proposal_digest"],
            "reviewer": "example:local-operator",
            "basis": "The operator inspected the exact root axes and caller-supplied route fixtures",
        },
        seed=73,
    )

    model = ScriptedSoloModel()
    record = SingleModelBackend(
        model,
        limits=ExecutionLimits(
            model_calls=5,
            input_tokens=20_000,
            output_tokens=5_000,
            reasoning_tokens=5_000,
            monetary_microunits=0,
            max_output_tokens_per_call=1_000,
            currency="USD",
        ),
    ).execute(plan, run_id="example-solo-run-001")
    record = validate_execution_record(record, plan_value=plan)

    summary = {
        "proposal_digest": proposal["proposal_digest"],
        "plan_digest": plan["plan_digest"],
        "run_digest": record["record_digest"],
        "status": record["status"],
        "task_contracts": [
            {"task_id": item["task_id"], "status": item["status"]}
            for item in record["task_results"]
        ],
        "model_calls": record["usage"]["model_calls"],
        "semantic_verification": record["semantic_verification"],
        "answer": record["final_output"],
        "fixture_notice": "Scripted wiring fixture; no live model, payment, or semantic proof.",
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
