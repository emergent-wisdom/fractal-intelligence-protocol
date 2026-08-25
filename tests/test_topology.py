from __future__ import annotations

import json
import unittest
from typing import Any

from fractal_protocol.decomposition import (
    ConceptualDecompositionEngine,
    DecompositionConfig,
)
from fractal_protocol.errors import DomainError
from fractal_protocol.execution import (
    ExecutionLimits,
    FunctionModelAdapter,
    SingleModelBackend,
    validate_execution_plan,
)
from fractal_protocol.protocol import canonical_json
from fractal_protocol.topology import (
    TopologyConstructionEngine,
    apply_fractal_case_proposal,
    create_concept_graph_snapshot,
    create_concept_node,
    create_fractal_case_proposal,
    create_topology_edge,
    create_topology_patch,
    execution_plan_from_fi_case,
    validate_concept_graph_snapshot,
    validate_fractal_case_proposal,
)


def copied(value: Any) -> Any:
    return json.loads(canonical_json(value))


def root_node() -> dict[str, Any]:
    return create_concept_node(
        name="RootSolver",
        description="Universal problem-solving boundary",
        kind="composite",
        accepts="Problems within this deployment",
        produces="Reviewed problem resolutions",
        boundary="Highest configured abstraction",
        excludes="Claims outside the configured deployment",
    )


def node(
    name: str,
    *,
    kind: str = "solver",
    supersedes: list[str] | None = None,
    produces: str | None = None,
) -> dict[str, Any]:
    return create_concept_node(
        name=name,
        description=f"The bounded {name} capability",
        kind=kind,
        accepts=f"Inputs requiring {name}",
        produces=produces or f"A {name} contribution",
        boundary=f"Owns only {name}",
        excludes=f"Other responsibilities outside {name}",
        supersedes=supersedes,
    )


def edge(
    parent: dict[str, Any],
    child: dict[str, Any],
    relation: str,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    return create_topology_edge(
        parent_ref=parent["node_ref"],
        child_ref=child["node_ref"],
        relation=relation,
        role=role or f"{child['name']} serves {parent['name']}",
        rationale=f"The {relation} boundary is explicit",
    )


class SubjectOracle:
    identity = {
        "adapter_id": "fixture:subject-four-tests",
        "adapter_version": "1",
        "generator": "fixture",
        "judge": "fixture",
        "router": "fixture",
        "marginal_value_source": "fixture",
    }

    def __init__(self, subject: dict[str, Any], children: list[dict[str, Any]]) -> None:
        self.subject = subject
        self.children = children

    def generate_candidates(
        self, concept: dict[str, Any], *, limit: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": child["name"],
                "description": child["description"],
                "bounds": [child["boundary"]],
                "not_in_scope": [child["excludes"]],
            }
            for child in self.children[:limit]
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
                "rationale": f"Removing {candidate['name']} breaks the parent",
            },
            "independence": {
                "passed": True,
                "rationale": f"{candidate['name']} can vary independently",
            },
            "universality": {
                "passed": True,
                "rationale": f"Every {concept['name']} contains this dimension",
            },
        }

    def evaluate_completeness(
        self, concept: dict[str, Any], accepted_axes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        expected = {child["name"] for child in self.children}
        actual = {axis["name"] for axis in accepted_axes}
        return {
            "passed": actual == expected,
            "rationale": "The independent dimensions span the subject",
            "missing_dimension": None if actual == expected else "uncovered dimension",
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
        raise AssertionError("Tests supply independent routing probes")

    def route_probe(
        self,
        concept: dict[str, Any],
        probe: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        index = int(probe["probe_id"].split("-")[-1])
        return {
            "axis_ids": [accepted_axes[index % len(accepted_axes)]["axis_id"]],
            "rationale": "Fixture selects exactly one independent dimension",
        }

    def estimate_marginal_value(
        self, concept: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]:
        return {
            "evidence_status": "sufficient",
            "shallow_value": 0.2,
            "deep_value": 0.3,
            "exploration_credit": 0,
            "shallow_cost": 1,
            "deep_cost": 2,
            "value_unit": "normalized_outcome_value",
            "cost_unit": "normalized_compute_cost",
            "sample_count": 3,
            "uncertainty": 0,
            "basis": "deterministic fixture",
            "rationale": "Further depth is below the configured threshold",
        }


def decomposition(
    subject: dict[str, Any], children: list[dict[str, Any]]
) -> dict[str, Any]:
    return ConceptualDecompositionEngine(
        SubjectOracle(subject, children),
        config=DecompositionConfig(max_depth=0, routing_probe_count=3),
    ).decompose(
        {
            "name": subject["name"],
            "description": subject["description"],
            "context": {"subject_ref": subject["node_ref"]},
        },
        routing_probes=[
            {"probe_id": f"probe-{index}", "task": f"held-out task {index}"}
            for index in range(3)
        ],
    )


def usage() -> dict[str, Any]:
    return {
        "complete": True,
        "model_calls": 1,
        "input_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 25,
        "monetary_microunits": 1_000,
    }


def provenance() -> dict[str, str]:
    return {
        "adapter_id": "fixture:topology-oracle",
        "adapter_version": "1",
        "model": "deterministic-fixture",
    }


def approved(case: dict[str, Any]) -> dict[str, str]:
    return {
        "decision": "approved",
        "case_proposal_digest": case["case_proposal_digest"],
        "reviewer": "fixture-reviewer",
        "basis": "Reviewed route, contracts, and four-test evidence",
    }


def exists_spec(path: str = "/answer") -> dict[str, Any]:
    return {
        "seam": "hard",
        "minimum_pass_rate": 1.0,
        "clauses": [
            {
                "id": f"exists-{path.removeprefix('/').replace('/', '-')}",
                "path": path,
                "operator": "exists",
                "critical": True,
                "disclosure": "public",
            }
        ],
    }


def build_fixture() -> dict[str, Any]:
    root = root_node()
    base = create_concept_graph_snapshot(
        root_ref=root["node_ref"], nodes=[root], edges=[]
    )
    broad = node("System Shaping", kind="abstraction_parent")
    subject = node(
        "Reliable Service Design",
        kind="composite",
        produces="A service design that remains reliable within its declared boundary",
    )
    constraint = node("Constraint Handling")
    verification = node("Outcome Verification")
    root_broad = edge(root, broad, "specialization")
    broad_subject = edge(broad, subject, "specialization")
    subject_constraint = edge(subject, constraint, "composition")
    subject_verification = edge(subject, verification, "composition")
    patch = create_topology_patch(
        base_snapshot_digest=base["snapshot_digest"],
        operations=[
            *({"op": "add_node", "node": item} for item in (broad, subject, constraint, verification)),
            *(
                {"op": "add_edge", "edge": item}
                for item in (
                    root_broad,
                    broad_subject,
                    subject_constraint,
                    subject_verification,
                )
            ),
        ],
        rationale="Locate the problem upward, then bind its constitutive dimensions",
    )
    four_tests = decomposition(subject, [constraint, verification])
    edge_by_child = {
        subject_constraint["child_ref"]: subject_constraint,
        subject_verification["child_ref"]: subject_verification,
    }
    node_by_name = {constraint["name"]: constraint, verification["name"]: verification}
    bindings = []
    for axis in four_tests["root"]["axes"]:
        child = node_by_name[axis["candidate"]["name"]]
        bindings.append(
            {
                "axis_id": axis["axis_id"],
                "child_ref": child["node_ref"],
                "edge_ref": edge_by_child[child["node_ref"]]["edge_ref"],
                "contract_fit": {
                    "decision": "new_node",
                    "rationale": "No prior immutable contract supplies this capability",
                },
            }
        )
    arguments = {
        "problem": {
            "statement": "Design a reliable service under explicit constraints",
            "context": {"domain": "fixture"},
        },
        "problem_framing": {
            "subject_ref": subject["node_ref"],
            "why_this_capability": (
                "The concrete request is an instance of reliable service-system design"
            ),
            "abstraction_effect": (
                "Treating reliability as a capability exposes reusable conceptual dimensions"
            ),
        },
        "patch": patch,
        "abstraction_ascent": [
            subject["node_ref"],
            broad["node_ref"],
            root["node_ref"],
        ],
        "downward_route": [
            root["node_ref"],
            broad["node_ref"],
            subject["node_ref"],
        ],
        "ascent_assessments": [
            {
                "child_ref": subject["node_ref"],
                "parent_ref": broad["node_ref"],
                "genus_rationale": "Both are capabilities for changing a system toward an intended state",
                "intermediate_parent_search": "No more informative active category lies between service-system design and system shaping",
                "sibling_search": "Reviewed active children and searched for differentiated service-system classes",
                "partition_status": "open",
                "sibling_classifications": [
                    {
                        "child_ref": subject["node_ref"],
                        "why_parent_is_more_general": "System shaping includes every kind of designed service system",
                        "reframing_effect": "Reveals interactions, boundaries, and regulation beyond implementation tasks",
                        "differentia": "This child specializes the genus to service reliability",
                        "contract_fit": {
                            "decision": "new_node",
                            "rationale": "This specific capability did not exist in the base graph",
                        },
                    }
                ],
                "sibling_independence": {
                    "status": "uncertain",
                    "rationale": "Additional observed service-system species may refine this sibling set",
                },
            },
            {
                "child_ref": broad["node_ref"],
                "parent_ref": root["node_ref"],
                "genus_rationale": "System shaping is one broad differentiated mode of problem solving",
                "intermediate_parent_search": "System shaping is the highest useful intermediate mode before the universal RootSolver",
                "sibling_search": "Reviewed active RootSolver children and left the open set ready for future broad modes",
                "partition_status": "open",
                "sibling_classifications": [
                    {
                        "child_ref": broad["node_ref"],
                        "why_parent_is_more_general": "RootSolver contains all problem-solving modes",
                        "reframing_effect": "Places system change inside the universal problem-solving boundary",
                        "differentia": "It changes a system rather than only explaining or expressing one",
                        "contract_fit": {
                            "decision": "new_node",
                            "rationale": "The broad mode is introduced by this case",
                        },
                    }
                ],
                "sibling_independence": {
                    "status": "uncertain",
                    "rationale": "The open graph may reveal other broad problem-solving modes",
                },
            },
        ],
        "subject_decomposition": four_tests,
        "axis_bindings": bindings,
        "parent_synthesis": {
            "child_refs": [constraint["node_ref"], verification["node_ref"]],
            "outward_capability": "A service design that remains reliable within its declared boundary",
            "integration": "Constraint handling bounds the design while verification tests its realized outcome",
        },
        "provenance": provenance(),
        "usage": usage(),
    }
    case = create_fractal_case_proposal(base, **arguments)
    return {
        "root": root,
        "base": base,
        "broad": broad,
        "subject": subject,
        "constraint": constraint,
        "verification": verification,
        "arguments": arguments,
        "case": case,
    }


class TopologyTests(unittest.TestCase):
    def test_root_snapshot_is_canonical_and_tamper_evident(self) -> None:
        root = root_node()
        snapshot = create_concept_graph_snapshot(
            root_ref=root["node_ref"], nodes=[root], edges=[]
        )

        self.assertEqual(snapshot, validate_concept_graph_snapshot(snapshot))
        self.assertEqual("RootSolver", snapshot["nodes"][0]["name"])

        tampered = copied(snapshot)
        tampered["nodes"][0]["boundary"] = "changed in place"
        with self.assertRaisesRegex(DomainError, "node_ref"):
            validate_concept_graph_snapshot(tampered)

        unsupported = copied(snapshot)
        unsupported["authority"] = "pay"
        with self.assertRaisesRegex(DomainError, "unsupported"):
            validate_concept_graph_snapshot(unsupported)

    def test_case_combines_mandatory_ascent_with_four_test_composition(self) -> None:
        fixture = build_fixture()
        case = fixture["case"]

        self.assertEqual(
            list(reversed(case["abstraction_ascent"])), case["downward_route"]
        )
        self.assertTrue(
            all(
                assessment["partition_status"] == "open"
                for assessment in case["ascent_assessments"]
            )
        )
        self.assertTrue(case["subject_decomposition"]["root"]["completeness"]["passed"])
        self.assertTrue(
            all(
                axis["assessment"][test]["passed"]
                for axis in case["subject_decomposition"]["root"]["axes"]
                for test in ("necessity", "independence", "universality")
            )
        )
        self.assertEqual(2, len(case["work_items"]))
        self.assertEqual(
            case,
            validate_fractal_case_proposal(
                case, base_snapshot_value=fixture["base"]
            ),
        )

        active = apply_fractal_case_proposal(
            fixture["base"], case, review=approved(case)
        )
        self.assertEqual(case["proposed_snapshot_digest"], active["snapshot_digest"])
        self.assertEqual(fixture["base"]["snapshot_digest"], active["predecessor_snapshot_digest"])

    def test_route_and_open_partition_are_strict(self) -> None:
        fixture = build_fixture()
        arguments = copied(fixture["arguments"])
        arguments["downward_route"] = arguments["downward_route"][:-1]
        with self.assertRaisesRegex(DomainError, "exactly reverse"):
            create_fractal_case_proposal(fixture["base"], **arguments)

        arguments = copied(fixture["arguments"])
        arguments["ascent_assessments"][0]["partition_status"] = "closed"
        with self.assertRaisesRegex(DomainError, "exhaustive specialization"):
            create_fractal_case_proposal(fixture["base"], **arguments)

        arguments = copied(fixture["arguments"])
        arguments["ascent_assessments"][0]["sibling_classifications"][0][
            "child_ref"
        ] = fixture["broad"]["node_ref"]
        with self.assertRaisesRegex(DomainError, "every active specialization sibling"):
            create_fractal_case_proposal(fixture["base"], **arguments)

        arguments = copied(fixture["arguments"])
        arguments["parent_synthesis"]["outward_capability"] = "An unrelated result"
        with self.assertRaisesRegex(DomainError, "immutable outward capability"):
            create_fractal_case_proposal(fixture["base"], **arguments)

    def test_a_precreated_sibling_requires_its_own_classification(self) -> None:
        fixture = build_fixture()
        sibling = node("Alternative Service Design", kind="composite")
        sibling_edge = edge(fixture["broad"], sibling, "specialization")
        arguments = copied(fixture["arguments"])
        arguments["patch"] = create_topology_patch(
            base_snapshot_digest=fixture["base"]["snapshot_digest"],
            operations=[
                *arguments["patch"]["operations"],
                {"op": "add_node", "node": sibling},
                {"op": "add_edge", "edge": sibling_edge},
            ],
            rationale="Create a reviewed sibling suggested by the new abstraction parent",
        )
        with self.assertRaisesRegex(DomainError, "every active specialization sibling"):
            create_fractal_case_proposal(fixture["base"], **arguments)

        arguments["ascent_assessments"][0]["sibling_classifications"].append(
            {
                "child_ref": sibling["node_ref"],
                "why_parent_is_more_general": "System shaping contains this alternative service-design class",
                "reframing_effect": "Exposes another reusable route beneath the same system-level genus",
                "differentia": "This sibling specializes the genus to a distinct service-design class",
                "contract_fit": {
                    "decision": "new_node",
                    "rationale": "The newly observed sibling has no exact base contract",
                },
            }
        )
        case = create_fractal_case_proposal(fixture["base"], **arguments)
        classified_refs = {
            item["child_ref"]
            for item in case["ascent_assessments"][0]["sibling_classifications"]
        }
        self.assertEqual(
            {fixture["subject"]["node_ref"], sibling["node_ref"]},
            classified_refs,
        )

    def test_graph_accepts_multi_parent_reuse_but_rejects_cycles_and_relation_conflicts(self) -> None:
        root = root_node()
        parent_a = node("Parent A", kind="composite")
        parent_b = node("Parent B", kind="composite")
        shared = node("Shared Capability")
        edges = [
            edge(root, parent_a, "specialization"),
            edge(root, parent_b, "specialization"),
            edge(parent_a, shared, "composition", role="Evidence for Parent A"),
            edge(parent_b, shared, "composition", role="Control for Parent B"),
        ]
        snapshot = create_concept_graph_snapshot(
            root_ref=root["node_ref"],
            nodes=[root, parent_a, parent_b, shared],
            edges=edges,
        )
        shared_edges = [item for item in snapshot["edges"] if item["child_ref"] == shared["node_ref"]]
        self.assertEqual(2, len(shared_edges))
        self.assertEqual(2, len({item["role"] for item in shared_edges}))

        conflict = edge(parent_a, shared, "specialization")
        with self.assertRaisesRegex(DomainError, "multiple topology relations"):
            create_concept_graph_snapshot(
                root_ref=root["node_ref"],
                nodes=[root, parent_a, parent_b, shared],
                edges=[*edges, conflict],
            )

        cycle = edge(shared, parent_a, "specialization")
        with self.assertRaisesRegex(DomainError, "cycle"):
            create_concept_graph_snapshot(
                root_ref=root["node_ref"],
                nodes=[root, parent_a, parent_b, shared],
                edges=[*edges, cycle],
            )

        passive = node("Passive Route", kind="passive")
        with self.assertRaisesRegex(DomainError, "executable solver or composite"):
            create_concept_graph_snapshot(
                root_ref=root["node_ref"],
                nodes=[root, parent_a, passive],
                edges=[
                    edge(root, parent_a, "specialization"),
                    edge(parent_a, passive, "composition"),
                ],
            )

    def test_graph_validation_handles_the_declared_thousand_node_limit(self) -> None:
        root = root_node()
        nodes = [root]
        edges = []
        parent = root
        for index in range(999):
            child = node(f"Chain {index}", kind="abstraction_parent")
            nodes.append(child)
            edges.append(edge(parent, child, "specialization"))
            parent = child

        snapshot = create_concept_graph_snapshot(
            root_ref=root["node_ref"], nodes=nodes, edges=edges
        )
        self.assertEqual(1_000, len(snapshot["nodes"]))

    def test_contract_revision_is_a_new_ref_and_stale_application_fails(self) -> None:
        fixture = build_fixture()
        active = apply_fractal_case_proposal(
            fixture["base"], fixture["case"], review=approved(fixture["case"])
        )
        revised = node(
            "Outcome Verification v2",
            supersedes=[fixture["verification"]["node_ref"]],
        )
        self.assertNotEqual(revised["node_ref"], fixture["verification"]["node_ref"])
        self.assertEqual(
            [fixture["verification"]["node_ref"]], revised["supersedes"]
        )

        with self.assertRaisesRegex(DomainError, "different snapshot"):
            apply_fractal_case_proposal(
                active, fixture["case"], review=approved(fixture["case"])
            )

    def test_reviewed_revision_rewires_without_mutating_the_old_contract(self) -> None:
        fixture = build_fixture()
        first_case = fixture["case"]
        active = apply_fractal_case_proposal(
            fixture["base"], first_case, review=approved(first_case)
        )
        revised = node(
            fixture["verification"]["name"],
            supersedes=[fixture["verification"]["node_ref"]],
        )
        old_edge = next(
            item
            for item in active["edges"]
            if item["parent_ref"] == fixture["subject"]["node_ref"]
            and item["child_ref"] == fixture["verification"]["node_ref"]
        )
        revised_edge = edge(fixture["subject"], revised, "composition")
        patch = create_topology_patch(
            base_snapshot_digest=active["snapshot_digest"],
            operations=[
                {"op": "add_node", "node": revised},
                {"op": "add_edge", "edge": revised_edge},
                {"op": "retire_edge", "edge_ref": old_edge["edge_ref"]},
                {
                    "op": "retire_node",
                    "node_ref": fixture["verification"]["node_ref"],
                },
            ],
            rationale="Replace a narrow contract with one explicit successor",
        )
        four_tests = decomposition(
            fixture["subject"], [fixture["constraint"], revised]
        )
        existing_constraint_edge = next(
            item
            for item in active["edges"]
            if item["parent_ref"] == fixture["subject"]["node_ref"]
            and item["child_ref"] == fixture["constraint"]["node_ref"]
        )
        child_by_name = {
            fixture["constraint"]["name"]: (
                fixture["constraint"],
                existing_constraint_edge,
                "exact_reuse",
            ),
            revised["name"]: (revised, revised_edge, "new_version"),
        }
        bindings = []
        for axis in four_tests["root"]["axes"]:
            child, binding_edge, decision = child_by_name[axis["candidate"]["name"]]
            bindings.append(
                {
                    "axis_id": axis["axis_id"],
                    "child_ref": child["node_ref"],
                    "edge_ref": binding_edge["edge_ref"],
                    "contract_fit": {
                        "decision": decision,
                        "rationale": "The exact revision status is explicit",
                    },
                }
            )
        assessments = copied(first_case["ascent_assessments"])
        for assessment in assessments:
            for classification in assessment["sibling_classifications"]:
                classification["contract_fit"] = {
                    "decision": "exact_reuse",
                    "rationale": "The route contract remains byte-identical",
                }
        revision_case = create_fractal_case_proposal(
            active,
            problem=first_case["problem"],
            problem_framing=first_case["problem_framing"],
            patch=patch,
            abstraction_ascent=first_case["abstraction_ascent"],
            downward_route=first_case["downward_route"],
            ascent_assessments=assessments,
            subject_decomposition=four_tests,
            axis_bindings=bindings,
            parent_synthesis={
                "child_refs": [fixture["constraint"]["node_ref"], revised["node_ref"]],
                "outward_capability": first_case["parent_synthesis"]["outward_capability"],
                "integration": first_case["parent_synthesis"]["integration"],
            },
            provenance=provenance(),
            usage=usage(),
        )
        result = apply_fractal_case_proposal(
            active, revision_case, review=approved(revision_case)
        )

        result_refs = {item["node_ref"] for item in result["nodes"]}
        self.assertIn(revised["node_ref"], result_refs)
        self.assertNotIn(fixture["verification"]["node_ref"], result_refs)
        self.assertEqual(
            [fixture["verification"]["node_ref"]], revised["supersedes"]
        )

        unrelated_predecessor = fixture["broad"]["node_ref"]
        malicious = node(
            fixture["verification"]["name"],
            supersedes=[fixture["verification"]["node_ref"], unrelated_predecessor],
        )
        malicious_edge = edge(fixture["subject"], malicious, "composition")
        malicious_patch = create_topology_patch(
            base_snapshot_digest=active["snapshot_digest"],
            operations=[
                {"op": "add_node", "node": malicious},
                {"op": "add_edge", "edge": malicious_edge},
                {"op": "retire_edge", "edge_ref": old_edge["edge_ref"]},
                {
                    "op": "retire_node",
                    "node_ref": fixture["verification"]["node_ref"],
                },
            ],
            rationale="Attempt to smuggle unrelated lineage into a valid revision",
        )
        malicious_bindings = copied(bindings)
        for binding in malicious_bindings:
            if binding["child_ref"] == revised["node_ref"]:
                binding["child_ref"] = malicious["node_ref"]
                binding["edge_ref"] = malicious_edge["edge_ref"]
        with self.assertRaisesRegex(DomainError, "same parent slot"):
            create_fractal_case_proposal(
                active,
                problem=first_case["problem"],
                problem_framing=first_case["problem_framing"],
                patch=malicious_patch,
                abstraction_ascent=first_case["abstraction_ascent"],
                downward_route=first_case["downward_route"],
                ascent_assessments=assessments,
                subject_decomposition=four_tests,
                axis_bindings=malicious_bindings,
                parent_synthesis={
                    "child_refs": [
                        fixture["constraint"]["node_ref"],
                        malicious["node_ref"],
                    ],
                    "outward_capability": first_case["parent_synthesis"]
                    ["outward_capability"],
                    "integration": first_case["parent_synthesis"]["integration"],
                },
                provenance=provenance(),
                usage=usage(),
            )

    def test_exact_reuse_case_uses_a_noop_patch_without_snapshot_churn(self) -> None:
        fixture = build_fixture()
        first_case = fixture["case"]
        active = apply_fractal_case_proposal(
            fixture["base"], first_case, review=approved(first_case)
        )
        arguments = copied(fixture["arguments"])
        arguments["patch"] = create_topology_patch(
            base_snapshot_digest=active["snapshot_digest"],
            operations=[],
            rationale="All exact contracts and edges already exist",
        )
        for assessment in arguments["ascent_assessments"]:
            for classification in assessment["sibling_classifications"]:
                classification["contract_fit"] = {
                    "decision": "exact_reuse",
                    "rationale": "The route reuses the exact active contract",
                }
        for binding in arguments["axis_bindings"]:
            binding["contract_fit"] = {
                "decision": "exact_reuse",
                "rationale": "The composition reuses the exact active contract",
            }
        reuse_case = create_fractal_case_proposal(active, **arguments)
        result = apply_fractal_case_proposal(
            active, reuse_case, review=approved(reuse_case)
        )

        self.assertEqual(active["snapshot_digest"], reuse_case["proposed_snapshot_digest"])
        self.assertEqual(active, result)

    def test_semantically_unordered_case_fields_are_canonical(self) -> None:
        fixture = build_fixture()
        arguments = copied(fixture["arguments"])
        arguments["axis_bindings"].reverse()
        arguments["parent_synthesis"]["child_refs"].reverse()

        reordered = create_fractal_case_proposal(fixture["base"], **arguments)
        self.assertEqual(
            fixture["case"]["case_proposal_digest"],
            reordered["case_proposal_digest"],
        )
        self.assertEqual(fixture["case"], reordered)

    def test_subject_composition_cannot_retain_an_unbound_child(self) -> None:
        fixture = build_fixture()
        extra = node("Unbound Legacy Constituent")
        extra_edge = edge(fixture["subject"], extra, "composition")
        arguments = copied(fixture["arguments"])
        arguments["patch"] = create_topology_patch(
            base_snapshot_digest=fixture["base"]["snapshot_digest"],
            operations=[
                *arguments["patch"]["operations"],
                {"op": "add_node", "node": extra},
                {"op": "add_edge", "edge": extra_edge},
            ],
            rationale="Attempt to retain an unbound composition child",
        )
        with self.assertRaisesRegex(DomainError, "exactly match"):
            create_fractal_case_proposal(fixture["base"], **arguments)

    def test_decomposition_is_bound_to_exact_subject_ref_and_unknown_authority_is_rejected(self) -> None:
        fixture = build_fixture()
        alternate_subject = create_concept_node(
            name=fixture["subject"]["name"],
            description=fixture["subject"]["description"],
            kind=fixture["subject"]["kind"],
            accepts=fixture["subject"]["accepts"],
            produces=fixture["subject"]["produces"],
            boundary="A different immutable subject boundary",
            excludes=fixture["subject"]["excludes"],
        )
        arguments = copied(fixture["arguments"])
        arguments["subject_decomposition"] = decomposition(
            alternate_subject, [fixture["constraint"], fixture["verification"]]
        )
        with self.assertRaisesRegex(DomainError, "exact subject contract"):
            create_fractal_case_proposal(fixture["base"], **arguments)

        forged = copied(fixture["case"])
        forged["payment_authority"] = {
            "lease_token": "not allowed",
            "reward_minor": 1_000,
        }
        with self.assertRaisesRegex(DomainError, "unsupported"):
            validate_fractal_case_proposal(
                forged, base_snapshot_value=fixture["base"]
            )

        oversized = copied(fixture["arguments"])
        oversized["problem"]["statement"] = "x" * 10_001
        with self.assertRaisesRegex(DomainError, "too long"):
            create_fractal_case_proposal(fixture["base"], **oversized)

    def test_review_and_operational_bindings_are_exact_and_complete(self) -> None:
        fixture = build_fixture()
        case = fixture["case"]
        rejected = approved(case)
        rejected["decision"] = "rejected"
        with self.assertRaisesRegex(DomainError, "approved review"):
            apply_fractal_case_proposal(fixture["base"], case, review=rejected)

        wrong = approved(case)
        wrong["case_proposal_digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(DomainError, "exact FI case"):
            apply_fractal_case_proposal(fixture["base"], case, review=wrong)

        extra = approved(case)
        extra["payment_authority"] = "none"
        with self.assertRaisesRegex(DomainError, "unsupported"):
            apply_fractal_case_proposal(fixture["base"], case, review=extra)

        active = apply_fractal_case_proposal(
            fixture["base"], case, review=approved(case)
        )
        bindings = [
            {
                "work_id": item["work_id"],
                "objective": "Produce the bounded contribution",
                "context": {},
                "output_schema": {
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                    "additionalProperties": False,
                },
                "accept_spec": exists_spec(),
            }
            for item in case["work_items"]
        ]
        synthesis = {
            "context": {},
            "output_schema": bindings[0]["output_schema"],
            "accept_spec": exists_spec(),
        }
        common = {
            "base_snapshot_value": fixture["base"],
            "active_snapshot_value": active,
            "synthesis": synthesis,
            "review": approved(case),
        }
        with self.assertRaisesRegex(DomainError, "Every reviewed FI work item"):
            execution_plan_from_fi_case(
                case, work_bindings=bindings[:-1], **common
            )
        with self.assertRaisesRegex(DomainError, "at most once"):
            execution_plan_from_fi_case(
                case, work_bindings=[bindings[0], *bindings], **common
            )
        unknown = copied(bindings)
        unknown[0]["work_id"] = "fi-work-unknown"
        with self.assertRaisesRegex(DomainError, "unknown FI composition"):
            execution_plan_from_fi_case(case, work_bindings=unknown, **common)
        stale = {**common, "active_snapshot_value": fixture["base"]}
        with self.assertRaisesRegex(DomainError, "exact reviewed post-patch"):
            execution_plan_from_fi_case(case, work_bindings=bindings, **stale)

    def test_approved_case_compiles_only_composition_work(self) -> None:
        fixture = build_fixture()
        case = fixture["case"]
        active = apply_fractal_case_proposal(
            fixture["base"], case, review=approved(case)
        )
        work_bindings = [
            {
                "work_id": item["work_id"],
                "objective": f"Produce the {item['role']} contribution",
                "context": {"node_ref": item["node_ref"]},
                "output_schema": {
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                    "additionalProperties": False,
                },
                "accept_spec": exists_spec(),
            }
            for item in case["work_items"]
        ]
        synthesis_binding = {
            "context": {"audience": "fixture reviewer"},
            "output_schema": {
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
                "additionalProperties": False,
            },
            "accept_spec": exists_spec(),
        }
        plan = execution_plan_from_fi_case(
            case,
            base_snapshot_value=fixture["base"],
            active_snapshot_value=active,
            work_bindings=work_bindings,
            synthesis=synthesis_binding,
            review=approved(case),
            seed=7,
        )

        self.assertEqual(plan, validate_execution_plan(plan))
        self.assertEqual("fractal_intelligence_case", plan["source"]["type"])
        self.assertEqual(case["downward_route"], plan["source"]["abstraction_route"])
        self.assertEqual(2, len(plan["tasks"]))
        self.assertFalse(any("RootSolver" == task["role"] for task in plan["tasks"]))
        self.assertFalse(any("System Shaping" == task["role"] for task in plan["tasks"]))
        self.assertTrue(all("source_axis_id" in task for task in plan["tasks"]))
        self.assertTrue(
            all("fi_contract" in task["context"] for task in plan["tasks"])
        )
        self.assertTrue(
            all(
                task["context"]["fi_contract"]["abstraction"]["problem_framing"]
                == case["problem_framing"]
                for task in plan["tasks"]
            )
        )
        self.assertEqual(
            ["RootSolver", "System Shaping", "Reliable Service Design"],
            [
                item["name"]
                for item in plan["tasks"][0]["context"]["fi_contract"]
                ["abstraction"]["downward_route"]
            ],
        )
        self.assertTrue(
            all(
                task["role"] == task["context"]["fi_contract"]["edge"]["role"]
                for task in plan["tasks"]
            )
        )
        self.assertIn(
            case["parent_synthesis"]["outward_capability"],
            plan["synthesis"]["objective"],
        )
        self.assertEqual(
            case["parent_synthesis"],
            plan["synthesis"]["context"]["fi_parent_synthesis"],
        )
        self.assertEqual(
            case["problem_framing"],
            plan["synthesis"]["context"]["fi_abstraction"]["problem_framing"],
        )

        captured_requests: list[dict[str, Any]] = []

        def invoke(request: dict[str, Any]) -> dict[str, Any]:
            captured_requests.append(copied(request))
            return {
                "output": {"answer": "contract-valid fixture output"},
                "finish_reason": "completed",
                "response_id": f"response-{len(captured_requests)}",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "reasoning_tokens": 0,
                    "monetary_microunits": 1,
                    "complete": True,
                },
            }

        adapter = FunctionModelAdapter(
            identity={
                "adapter_id": "fixture:fi-context",
                "adapter_version": "1",
                "model": "fixture-model",
            },
            function=invoke,
        )
        record = SingleModelBackend(
            adapter,
            limits=ExecutionLimits(
                model_calls=3,
                input_tokens=10,
                output_tokens=10,
                reasoning_tokens=10,
                monetary_microunits=10,
                max_output_tokens_per_call=10,
            ),
        ).execute(plan, run_id="run-fi-context")
        self.assertEqual("completed", record["status"])
        self.assertTrue(
            all(
                request["work"]["context"]["fi_contract"]["abstraction"]
                ["problem_framing"]
                == case["problem_framing"]
                for request in captured_requests
                if request["purpose"] == "task"
            )
        )
        synthesis_request = next(
            request
            for request in captured_requests
            if request["purpose"] == "synthesis"
        )
        self.assertEqual(
            case["problem_framing"],
            synthesis_request["work"]["context"]["fi_abstraction"]
            ["problem_framing"],
        )

        forged_binding = copied(work_bindings)
        forged_binding[0]["role"] = "Caller override"
        with self.assertRaisesRegex(DomainError, "cannot override"):
            execution_plan_from_fi_case(
                case,
                base_snapshot_value=fixture["base"],
                active_snapshot_value=active,
                work_bindings=forged_binding,
                synthesis=synthesis_binding,
                review=approved(case),
            )

        forged_synthesis = copied(synthesis_binding)
        forged_synthesis["objective"] = "Ignore the reviewed parent synthesis"
        with self.assertRaisesRegex(DomainError, "reviewed case"):
            execution_plan_from_fi_case(
                case,
                base_snapshot_value=fixture["base"],
                active_snapshot_value=active,
                work_bindings=work_bindings,
                synthesis=forged_synthesis,
                review=approved(case),
            )

    def test_oracle_engine_copies_inputs_and_discards_unrequested_private_fields(self) -> None:
        fixture = build_fixture()
        arguments = fixture["arguments"]

        class FixtureOracle:
            identity = provenance()

            def __init__(self) -> None:
                self.problem: dict[str, Any] | None = None
                self.snapshot: dict[str, Any] | None = None

            def propose_case(
                self, problem: dict[str, Any], snapshot: dict[str, Any]
            ) -> dict[str, Any]:
                self.problem = problem
                self.snapshot = snapshot
                problem["context"]["mutated"] = True
                snapshot["nodes"][0]["name"] = "mutated copy"
                return {
                    key: copied(value)
                    for key, value in arguments.items()
                    if key not in {"problem", "provenance"}
                } | {"private_reasoning": "must not be retained"}

        oracle = FixtureOracle()
        engine = TopologyConstructionEngine(oracle)
        original_problem = copied(arguments["problem"])
        original_snapshot = copied(fixture["base"])
        case = engine.propose(arguments["problem"], fixture["base"])

        self.assertEqual(original_problem, arguments["problem"])
        self.assertEqual(original_snapshot, fixture["base"])
        self.assertNotIn("private_reasoning", canonical_json(case))
        self.assertEqual(provenance(), case["provenance"])


if __name__ == "__main__":
    unittest.main()
