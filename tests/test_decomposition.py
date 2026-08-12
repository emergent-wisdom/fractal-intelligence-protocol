from __future__ import annotations

import unittest
from decimal import localcontext
from typing import Any

from fractal_protocol.decomposition import (
    ConceptualDecompositionEngine,
    DecompositionConfig,
    validate_decomposition_proposal,
)
from fractal_protocol.errors import DomainError
from fractal_protocol.materialization import (
    build_materialization_plan,
    delegation_payload_from_plan,
)


def candidate(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"The independent {name} dimension",
        "bounds": [f"responsibility-{name}"],
        "not_in_scope": [f"implementation-{name}"],
    }


def concept(name: str = "Reliable work") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Work that reaches a checkable accepted outcome",
        "context": {"domain": "test"},
    }


def fixed_routing_probes() -> list[dict[str, Any]]:
    return [
        {"probe_id": f"probe-{index}", "task": f"held-out task {index}"}
        for index in range(3)
    ]


def task(capability: str) -> dict[str, Any]:
    return {
        "required_capability": capability,
        "operation": "solve",
        "inputs": {"problem": "fixture"},
        "constraints": {},
        "accept_spec": {
            "seam": "hard",
            "minimum_pass_rate": 1.0,
            "clauses": [
                {
                    "id": "answer",
                    "path": "/answer",
                    "operator": "equals",
                    "expected": 42,
                    "critical": True,
                    "disclosure": "hidden",
                }
            ],
        },
        "reward_minor": 100,
        "delegation_budget_minor": 0,
        "max_attempts": 2,
    }


class CleanOracle:
    identity = {
        "adapter_id": "fixture:clean-oracle",
        "adapter_version": "1",
        "generator": "scripted-generator-v1",
        "judge": "scripted-judge-v1",
        "router": "scripted-router-v1",
        "marginal_value_source": "scripted-paired-fixtures-v1",
    }

    def __init__(self, *, conflict: bool = False, recurse: bool = False) -> None:
        self.conflict = conflict
        self.recurse = recurse

    def generate_candidates(
        self, concept_value: dict[str, Any], *, limit: int
    ) -> list[dict[str, Any]]:
        if concept_value["name"] == "Reliable work":
            return [candidate("execution"), candidate("verification")]
        return [candidate("substructure")]

    def evaluate_candidate(
        self,
        concept_value: dict[str, Any],
        candidate_value: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            name: {"passed": True, "rationale": f"{name} witness"}
            for name in ("necessity", "independence", "universality")
        }

    def evaluate_completeness(
        self, concept_value: dict[str, Any], accepted_axes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        required = 2 if concept_value["name"] == "Reliable work" else 1
        return {
            "passed": len(accepted_axes) >= required,
            "rationale": "All declared responsibilities are covered",
            "missing_dimension": None,
        }

    def search_missing_dimension(
        self,
        concept_value: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
        *,
        missing_dimension: str | None,
    ) -> dict[str, Any] | None:
        return None

    def generate_routing_probes(
        self, concept_value: dict[str, Any], *, count: int
    ) -> list[dict[str, Any]]:
        return [
            {"probe_id": f"probe-{index}", "task": f"atomic obligation {index}"}
            for index in range(count)
        ]

    def route_probe(
        self,
        concept_value: dict[str, Any],
        probe: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        index = int(probe["probe_id"].split("-")[-1])
        axis_ids = [accepted_axes[index % len(accepted_axes)]["axis_id"]]
        if self.conflict and index == 0 and len(accepted_axes) > 1:
            axis_ids = [axis["axis_id"] for axis in accepted_axes[:2]]
        return {"axis_ids": axis_ids, "rationale": "Deterministic fixture route"}

    def estimate_marginal_value(
        self, concept_value: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        deep_value = (
            1.0
            if self.recurse and depth == 0 and axis["name"] == "execution"
            else 0.3
        )
        return {
            "evidence_status": "sufficient",
            "shallow_value": 0.2,
            "deep_value": deep_value,
            "exploration_credit": 0,
            "shallow_cost": 1,
            "deep_cost": 2,
            "value_unit": "normalized_outcome_value",
            "cost_unit": "normalized_compute_cost",
            "sample_count": 3,
            "uncertainty": 0,
            "basis": "scripted_fixture",
            "rationale": "Known fixture depth value",
        }


class FixedProbeProvider:
    provider_id = "fixture:independent-probe-corpus-v1"

    def __call__(
        self,
        concept_value: dict[str, Any],
        *,
        depth: int,
        path: str,
        count: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "probe_id": f"probe-{index}",
                "task": f"independent corpus task {index} for {path}",
            }
            for index in range(count)
        ]


class RepairOracle(CleanOracle):
    def generate_candidates(
        self, concept_value: dict[str, Any], *, limit: int
    ) -> list[dict[str, Any]]:
        return [candidate("execution"), candidate("write a unit test")]

    def evaluate_candidate(
        self,
        concept_value: dict[str, Any],
        candidate_value: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        universal = candidate_value["name"] != "write a unit test"
        return {
            "necessity": {"passed": True, "rationale": "Required"},
            "independence": {"passed": True, "rationale": "Can vary"},
            "universality": {
                "passed": universal,
                "rationale": "Universal axis" if universal else "Instance-specific task",
            },
        }

    def evaluate_completeness(
        self, concept_value: dict[str, Any], accepted_axes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        passed = {axis["name"] for axis in accepted_axes} >= {
            "execution",
            "verification",
        }
        return {
            "passed": passed,
            "rationale": "Verification is required" if not passed else "Coverage complete",
            "missing_dimension": None if passed else "verification",
        }

    def search_missing_dimension(
        self,
        concept_value: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
        *,
        missing_dimension: str | None,
    ) -> dict[str, Any] | None:
        return candidate("verification")


class ColdStartOracle(CleanOracle):
    def estimate_marginal_value(
        self, concept_value: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        return {
            "evidence_status": "insufficient",
            "basis": "cold_start",
            "rationale": "No paired shallow/deep observations exist",
        }


class ThresholdOracle(CleanOracle):
    def estimate_marginal_value(
        self, concept_value: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        return {
            "evidence_status": "sufficient",
            "shallow_value": 0.2,
            "deep_value": 0.45,
            "exploration_credit": 0,
            "shallow_cost": 1,
            "deep_cost": 2,
            "value_unit": "normalized_outcome_value",
            "cost_unit": "normalized_compute_cost",
            "sample_count": 3,
            "uncertainty": 0,
            "basis": "scripted_fixture",
            "rationale": "Exactly equal to the configured threshold",
        }


class RankingOracle(CleanOracle):
    def estimate_marginal_value(
        self, concept_value: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        if depth > 0:
            deep_value = 0.3
        else:
            deep_value = 1.0 if axis["name"] == "verification" else 0.6
        return {
            "evidence_status": "sufficient",
            "shallow_value": 0.2,
            "deep_value": deep_value,
            "exploration_credit": 0,
            "shallow_cost": 1,
            "deep_cost": 2,
            "value_unit": "normalized_outcome_value",
            "cost_unit": "normalized_compute_cost",
            "sample_count": 3,
            "uncertainty": 0,
            "basis": "scripted_fixture",
            "rationale": "Fixture for sibling allocation order",
        }


class RepeatingRatioOracle(CleanOracle):
    def estimate_marginal_value(
        self, concept_value: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        return {
            "evidence_status": "sufficient",
            "shallow_value": 0,
            "deep_value": 1,
            "exploration_credit": 0,
            "shallow_cost": 0,
            "deep_cost": 3,
            "value_unit": "normalized_outcome_value",
            "cost_unit": "normalized_compute_cost",
            "sample_count": 3,
            "uncertainty": 0,
            "basis": "scripted_fixture",
            "rationale": "Repeating ratio for decimal-context isolation",
        }


class ExtremeDecimalOracle(RepeatingRatioOracle):
    def estimate_marginal_value(
        self, concept_value: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        estimate = super().estimate_marginal_value(concept_value, axis, depth=depth)
        estimate["shallow_value"] = "1e1000000"
        estimate["deep_value"] = "2e1000000"
        return estimate


class ManyAxisOracle(CleanOracle):
    def generate_candidates(
        self, concept_value: dict[str, Any], *, limit: int
    ) -> list[dict[str, Any]]:
        return [candidate(f"axis-{index}") for index in range(21)]

    def evaluate_completeness(
        self, concept_value: dict[str, Any], accepted_axes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "passed": len(accepted_axes) == 21,
            "rationale": "Fixture has 21 axes",
            "missing_dimension": None,
        }


class DecompositionTests(unittest.TestCase):
    def test_clean_algorithm_records_all_four_tests_routing_and_mvr(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(), config=DecompositionConfig(max_depth=0)
        ).decompose(concept(), routing_probes=fixed_routing_probes())

        self.assertEqual("review_required", proposal["status"])
        self.assertEqual("structurally_clean", proposal["root"]["status"])
        self.assertEqual(2, len(proposal["root"]["axes"]))
        self.assertTrue(proposal["root"]["completeness"]["passed"])
        self.assertEqual(3, len(proposal["root"]["routing"]["probes"]))
        self.assertTrue(all(item["clean"] for item in proposal["root"]["routing"]["probes"]))
        self.assertTrue(
            all(
                axis["assessment"][test]["passed"]
                for axis in proposal["root"]["axes"]
                for test in ("necessity", "independence", "universality")
            )
        )
        self.assertEqual(proposal, validate_decomposition_proposal(proposal))

    def test_failed_universality_is_rejected_and_completeness_search_is_retested(self) -> None:
        proposal = ConceptualDecompositionEngine(
            RepairOracle(), config=DecompositionConfig(max_depth=0)
        ).decompose(concept())

        self.assertEqual(
            ["execution", "verification"],
            [axis["candidate"]["name"] for axis in proposal["root"]["axes"]],
        )
        self.assertEqual(
            "write a unit test",
            proposal["root"]["rejected_candidates"][0]["candidate"]["name"],
        )
        self.assertFalse(
            proposal["root"]["rejected_candidates"][0]["assessment"]["universality"]["passed"]
        )
        self.assertEqual(1, proposal["root"]["completeness"]["retries_used"])

    def test_routing_conflict_is_review_warning_not_structural_success(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(conflict=True), config=DecompositionConfig(max_depth=0)
        ).decompose(concept())

        self.assertEqual("warning", proposal["root"]["status"])
        self.assertFalse(proposal["root"]["routing"]["passed"])
        self.assertIn("routing_sanity_warning", proposal["warnings"])

    def test_fixed_held_out_routing_probes_bypass_probe_generation(self) -> None:
        generated = ConceptualDecompositionEngine(
            CleanOracle(), config=DecompositionConfig(max_depth=0)
        ).decompose(concept())
        provided = ConceptualDecompositionEngine(
            CleanOracle(), config=DecompositionConfig(max_depth=0)
        ).decompose(
            concept(),
            routing_probes=[
                {"probe_id": f"probe-{index}", "task": f"held-out task {index}"}
                for index in range(3)
            ],
        )

        self.assertEqual("provided", provided["root"]["routing"]["probe_source"])
        self.assertEqual(
            generated["usage"]["oracle_calls"] - 1,
            provided["usage"]["oracle_calls"],
        )

    def test_marginal_value_recurses_only_above_threshold(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(recurse=True),
            config=DecompositionConfig(max_depth=1, marginal_value_threshold=0.25),
        ).decompose(concept())

        axes = proposal["root"]["axes"]
        self.assertEqual("recurse", axes[0]["marginal_value"]["recursion_decision"])
        self.assertIsNotNone(axes[0]["decomposition"])
        self.assertEqual("leaf", axes[1]["marginal_value"]["recursion_decision"])
        self.assertEqual(2, proposal["usage"]["decomposition_nodes"])

    def test_call_budget_yields_a_valid_partial_review_artifact(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(), config=DecompositionConfig(max_oracle_calls=1)
        ).decompose(concept())

        self.assertEqual("budget_exhausted", proposal["root"]["status"])
        self.assertTrue(proposal["usage"]["budget_exhausted"])
        self.assertEqual(1, proposal["usage"]["oracle_calls"])
        self.assertEqual(proposal, validate_decomposition_proposal(proposal))

    def test_missing_marginal_value_evidence_requests_probe_instead_of_guessing(self) -> None:
        proposal = ConceptualDecompositionEngine(
            ColdStartOracle(), config=DecompositionConfig(max_depth=1)
        ).decompose(concept())

        for axis in proposal["root"]["axes"]:
            self.assertEqual("probe", axis["marginal_value"]["mvr_decision"])
            self.assertEqual("probe", axis["marginal_value"]["recursion_decision"])
            self.assertIsNone(axis["decomposition"])

    def test_marginal_value_uses_strict_greater_than_threshold(self) -> None:
        proposal = ConceptualDecompositionEngine(
            ThresholdOracle(),
            config=DecompositionConfig(max_depth=1, marginal_value_threshold=0.25),
        ).decompose(concept())

        for axis in proposal["root"]["axes"]:
            self.assertEqual("0.25", axis["marginal_value"]["marginal_ratio"])
            self.assertEqual("leaf", axis["marginal_value"]["mvr_decision"])
            self.assertIsNone(axis["decomposition"])

    def test_node_budget_is_allocated_to_highest_marginal_value_first(self) -> None:
        proposal = ConceptualDecompositionEngine(
            RankingOracle(),
            config=DecompositionConfig(max_depth=1, max_nodes=2),
        ).decompose(concept())
        by_name = {
            axis["candidate"]["name"]: axis for axis in proposal["root"]["axes"]
        }

        self.assertEqual(
            "recurse",
            by_name["verification"]["marginal_value"]["recursion_decision"],
        )
        self.assertIsNotNone(by_name["verification"]["decomposition"])
        self.assertEqual(
            "node_budget_exhausted",
            by_name["execution"]["marginal_value"]["recursion_decision"],
        )

    def test_marginal_value_is_independent_of_ambient_decimal_context(self) -> None:
        with localcontext() as context_value:
            context_value.prec = 6
            low_precision = ConceptualDecompositionEngine(
                RepeatingRatioOracle(), config=DecompositionConfig(max_depth=0)
            ).decompose(concept())
        with localcontext() as context_value:
            context_value.prec = 80
            high_precision = ConceptualDecompositionEngine(
                RepeatingRatioOracle(), config=DecompositionConfig(max_depth=0)
            ).decompose(concept())

        self.assertEqual(low_precision, high_precision)

    def test_extreme_decimal_and_threshold_inputs_fail_as_domain_errors(self) -> None:
        with self.assertRaises(DomainError):
            ConceptualDecompositionEngine(
                ExtremeDecimalOracle(), config=DecompositionConfig(max_depth=0)
            ).decompose(concept())
        with self.assertRaises(DomainError):
            DecompositionConfig(
                marginal_value_threshold=10**10_000
            ).normalized()

    def test_content_tampering_invalidates_proposal_digest(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(), config=DecompositionConfig(max_depth=0)
        ).decompose(concept())
        proposal["root"]["concept"]["description"] = "tampered"

        with self.assertRaisesRegex(DomainError, "proposal_digest"):
            validate_decomposition_proposal(proposal)

    def test_materialization_requires_clean_structure_and_admitted_capabilities(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(), config=DecompositionConfig(max_depth=0)
        ).decompose(concept(), routing_probes=fixed_routing_probes())
        capability_a = f"sha256:{'a' * 64}"
        capability_b = f"sha256:{'b' * 64}"
        axis_tasks = [
            {"axis_id": axis["axis_id"], "task": task(capability)}
            for axis, capability in zip(
                proposal["root"]["axes"],
                (capability_a, capability_b),
                strict=True,
            )
        ]
        plan = build_materialization_plan(
            proposal,
            axis_tasks,
            admitted_manifest_digests={capability_a, capability_b},
        )

        self.assertEqual("ready", plan["status"])
        payload = delegation_payload_from_plan(
            plan,
            idempotency_key="decomposition-1",
            lease_token="secret",
            admitted_manifest_digests={capability_a, capability_b},
        )
        self.assertEqual(2, len(payload["children"]))
        self.assertNotIn("proposal_digest", payload)
        with self.assertRaisesRegex(DomainError, "idempotency_key is too long"):
            delegation_payload_from_plan(
                plan,
                idempotency_key="x" * 201,
                lease_token="secret",
                admitted_manifest_digests={capability_a, capability_b},
            )
        with self.assertRaisesRegex(DomainError, "no longer admitted"):
            delegation_payload_from_plan(
                plan,
                idempotency_key="decomposition-1",
                lease_token="secret",
                admitted_manifest_digests={capability_a},
            )
        payload_with_unrelated_addition = delegation_payload_from_plan(
            plan,
            idempotency_key="decomposition-2",
            lease_token="secret",
            admitted_manifest_digests={
                capability_a,
                capability_b,
                f"sha256:{'e' * 64}",
            },
        )
        self.assertEqual(2, len(payload_with_unrelated_addition["children"]))

    def test_recursive_materialization_accepts_independent_probes_at_every_depth(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(recurse=True), config=DecompositionConfig(max_depth=1)
        ).decompose(concept(), routing_probe_provider=FixedProbeProvider())
        capability = f"sha256:{'f' * 64}"
        plan = build_materialization_plan(
            proposal,
            [
                {"axis_id": axis["axis_id"], "task": task(capability)}
                for axis in proposal["root"]["axes"]
            ],
            admitted_manifest_digests={capability},
        )

        self.assertEqual("ready", plan["status"])
        self.assertEqual(
            "provided",
            proposal["root"]["axes"][0]["decomposition"]["routing"]["probe_source"],
        )

    def test_materialization_blocks_unresolved_or_conflicting_axes(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(conflict=True), config=DecompositionConfig(max_depth=0)
        ).decompose(concept(), routing_probes=fixed_routing_probes())
        capability = f"sha256:{'c' * 64}"
        plan = build_materialization_plan(
            proposal,
            [
                {"axis_id": axis["axis_id"], "task": task(capability)}
                for axis in proposal["root"]["axes"]
            ],
            admitted_manifest_digests=set(),
        )

        self.assertEqual("blocked", plan["status"])
        self.assertTrue(plan["unresolved_capabilities"])
        self.assertEqual(
            ["routing_sanity_not_established:root"],
            plan["structural_failures"],
        )
        with self.assertRaises(DomainError):
            delegation_payload_from_plan(
                plan,
                idempotency_key="blocked",
                lease_token="secret",
                admitted_manifest_digests=set(),
            )

    def test_oracle_budget_exhaustion_cannot_materialize(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(), config=DecompositionConfig(max_oracle_calls=7)
        ).decompose(concept(), routing_probes=fixed_routing_probes())
        capability = f"sha256:{'d' * 64}"
        plan = build_materialization_plan(
            proposal,
            [
                {"axis_id": axis["axis_id"], "task": task(capability)}
                for axis in proposal["root"]["axes"]
            ],
            admitted_manifest_digests={capability},
        )

        self.assertEqual("budget_exhausted", proposal["root"]["status"])
        self.assertEqual("blocked", plan["status"])
        self.assertIn("oracle_budget_exhausted:root", plan["structural_failures"])
        self.assertTrue(
            any(
                failure.startswith("marginal_value_missing:")
                for failure in plan["structural_failures"]
            )
        )

    def test_materialization_blocks_more_children_than_coordinator_accepts(self) -> None:
        proposal = ConceptualDecompositionEngine(
            ManyAxisOracle(),
            config=DecompositionConfig(candidate_limit=21, max_depth=0),
        ).decompose(concept(), routing_probes=fixed_routing_probes())
        capability = f"sha256:{'9' * 64}"
        plan = build_materialization_plan(
            proposal,
            [
                {"axis_id": axis["axis_id"], "task": task(capability)}
                for axis in proposal["root"]["axes"]
            ],
            admitted_manifest_digests={capability},
        )

        self.assertEqual("blocked", plan["status"])
        self.assertIn(
            "coordinator_child_limit_exceeded", plan["structural_failures"]
        )


if __name__ == "__main__":
    unittest.main()
