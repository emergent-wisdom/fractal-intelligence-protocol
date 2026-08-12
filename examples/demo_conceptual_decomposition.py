from __future__ import annotations

import json
from typing import Any

from fractal_protocol.decomposition import (
    ConceptualDecompositionEngine,
    DecompositionConfig,
)


class IllustrativeOracle:
    """Deterministic fixture for exercising the algorithm; not a semantic judge."""

    identity = {
        "adapter_id": "example:illustrative-oracle",
        "adapter_version": "1",
        "generator": "hand-authored-fixture-v1",
        "judge": "hand-authored-fixture-v1",
        "router": "keyword-fixture-v1",
        "marginal_value_source": "no-paired-evidence",
    }

    axes = (
        ("interpretation", "Resolve what outcome the requester actually needs"),
        ("execution", "Produce the candidate work product"),
        ("verification", "Determine whether the candidate satisfies its contract"),
        ("settlement", "Allocate payment and refunds from accepted outcomes"),
    )

    def generate_candidates(
        self, concept: dict[str, Any], *, limit: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": description,
                "bounds": [description],
                "not_in_scope": ["A domain-specific implementation step"],
            }
            for name, description in self.axes[:limit]
        ]

    def evaluate_candidate(
        self,
        concept: dict[str, Any],
        candidate: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "necessity": {
                "passed": True,
                "rationale": f"Removing {candidate['name']} breaks the declared service loop",
            },
            "independence": {
                "passed": True,
                "rationale": f"The {candidate['name']} mechanism can vary independently",
            },
            "universality": {
                "passed": True,
                "rationale": f"Every paid problem-solving service instantiates {candidate['name']}",
            },
        }

    def evaluate_completeness(
        self, concept: dict[str, Any], accepted_axes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        complete = {axis["name"] for axis in accepted_axes} == {
            name for name, _ in self.axes
        }
        return {
            "passed": complete,
            "rationale": "The request-to-accepted-payment loop is covered end to end",
            "missing_dimension": None if complete else "an uncovered service responsibility",
        }

    def search_missing_dimension(
        self,
        concept: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
        *,
        missing_dimension: str | None,
    ) -> dict[str, Any] | None:
        return None

    def generate_routing_probes(
        self, concept: dict[str, Any], *, count: int
    ) -> list[dict[str, Any]]:
        raise AssertionError("The example supplies held-out probes")

    def route_probe(
        self,
        concept: dict[str, Any],
        probe: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        task = probe["task"].lower()
        target = next(
            axis_name
            for keyword, axis_name in (
                ("ambiguous", "interpretation"),
                ("produce", "execution"),
                ("check", "verification"),
                ("move", "settlement"),
            )
            if keyword in task
        )
        axis = next(axis for axis in accepted_axes if axis["name"] == target)
        return {
            "axis_ids": [axis["axis_id"]],
            "rationale": f"The obligation belongs to {target}",
        }

    def estimate_marginal_value(
        self, concept: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        return {
            "evidence_status": "insufficient",
            "basis": "cold_start",
            "rationale": "Paired shallow/deep outcome evidence has not been collected",
        }


def main() -> None:
    engine = ConceptualDecompositionEngine(
        IllustrativeOracle(),
        config=DecompositionConfig(max_depth=2, routing_probe_count=4),
    )
    proposal = engine.decompose(
        {
            "name": "Paid agent problem solving",
            "description": "Turn a request into verified work and settle its economic outcome",
            "context": {"trust_model": "central coordinator with provider agents"},
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
    summary = {
        "proposal_digest": proposal["proposal_digest"],
        "proposal_status": proposal["status"],
        "root_status": proposal["root"]["status"],
        "completeness_passed": proposal["root"]["completeness"]["passed"],
        "probe_source": proposal["root"]["routing"]["probe_source"],
        "axes": [
            {
                "axis_id": axis["axis_id"],
                "name": axis["candidate"]["name"],
                "candidate_tests_passed": axis["assessment"]["passed"],
                "depth_decision": axis["marginal_value"]["recursion_decision"],
            }
            for axis in proposal["root"]["axes"]
        ],
        "usage": proposal["usage"],
        "warnings": proposal["warnings"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
