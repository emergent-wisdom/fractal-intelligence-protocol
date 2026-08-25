from __future__ import annotations

import json
from typing import Any, Protocol

from .decomposition import validate_decomposition_proposal
from .errors import require
from .execution import create_execution_plan
from .materialization import decomposition_execution_failures
from .protocol import canonical_json, content_digest, require_object, require_string


SNAPSHOT_KIND = "fi_concept_graph_snapshot"
PATCH_KIND = "fi_topology_patch"
CASE_KIND = "fi_case_proposal"
ASSESSMENT_BOUNDARY = "model_judgments_are_estimates_not_proof"
SCHEMA_VERSION = "1"

NODE_KINDS = {"solver", "composite", "abstraction_parent", "passive"}
EDGE_RELATIONS = {"specialization", "composition"}
FIT_DECISIONS = {
    "exact_reuse",
    "new_node",
    "new_version",
    "shared_parent",
    "split",
}
MAX_NODES = 1_000
MAX_EDGES = 4_000
MAX_PATCH_OPERATIONS = 1_000
MAX_ROUTE_LENGTH = 64
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_CASE_BYTES = 4 * 1024 * 1024
MAX_IDENTIFIER_LENGTH = 200


class TopologyOracle(Protocol):
    """One broad semantic proposal boundary for the experimental FI profile.

    The oracle may be an LLM, a human process, an ensemble, or a deterministic
    fixture. It proposes semantic structure; this module only validates the
    public artifact and graph mechanics.
    """

    identity: dict[str, Any]

    def propose_case(
        self, problem: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any]: ...


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _bounded_string(value: Any, field: str, *, maximum: int = 10_000) -> str:
    checked = require_string(value, field)
    require(
        len(checked) <= maximum,
        "invalid_topology",
        f"{field} is too long",
        field=field,
        maximum=maximum,
    )
    return checked


def _identifier(value: Any, field: str) -> str:
    return _bounded_string(value, field, maximum=MAX_IDENTIFIER_LENGTH)


def _require_exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    require(
        set(value) == expected,
        "invalid_topology",
        f"{field} contains unsupported or missing fields",
        missing=sorted(expected - set(value)),
        unsupported=sorted(set(value) - expected),
    )


def _sha256_digest(value: Any, field: str) -> str:
    digest = require_string(value, field)
    suffix = digest.removeprefix("sha256:")
    require(
        digest.startswith("sha256:")
        and len(suffix) == 64
        and all(character in "0123456789abcdef" for character in suffix),
        "invalid_topology",
        f"{field} must be a full lowercase SHA-256 digest",
        field=field,
    )
    return digest


def _normalize_problem(value: Any, field: str = "problem") -> dict[str, Any]:
    problem = require_object(value, field)
    normalized = {
        "statement": _bounded_string(
            problem.get("statement"), f"{field}.statement", maximum=10_000
        ),
        "context": _json_copy(
            require_object(problem.get("context", {}), f"{field}.context")
        ),
    }
    require(
        len(canonical_json(normalized).encode("utf-8")) <= 128 * 1024,
        "invalid_topology",
        f"{field} is too large",
    )
    return normalized


def _normalize_problem_framing(value: Any, field: str) -> dict[str, str]:
    framing = require_object(value, field)
    normalized = {
        "subject_ref": _sha256_digest(
            framing.get("subject_ref"), f"{field}.subject_ref"
        ),
        "why_this_capability": _bounded_string(
            framing.get("why_this_capability"), f"{field}.why_this_capability"
        ),
        "abstraction_effect": _bounded_string(
            framing.get("abstraction_effect"), f"{field}.abstraction_effect"
        ),
    }
    require(
        canonical_json(framing) == canonical_json(normalized),
        "invalid_topology",
        f"{field} contains unsupported or non-canonical fields",
    )
    return normalized


def _normalize_provenance(value: Any, field: str = "provenance") -> dict[str, str]:
    provenance = require_object(value, field)
    return {
        "adapter_id": _bounded_string(
            provenance.get("adapter_id"), f"{field}.adapter_id", maximum=500
        ),
        "adapter_version": _bounded_string(
            provenance.get("adapter_version"),
            f"{field}.adapter_version",
            maximum=100,
        ),
        "model": _bounded_string(
            provenance.get("model"), f"{field}.model", maximum=500
        ),
    }


def _normalize_usage(value: Any, field: str = "usage") -> dict[str, Any]:
    usage = require_object(value, field)
    require(
        usage.get("complete") is True,
        "invalid_topology",
        f"{field} must attest complete adapter usage",
    )
    normalized: dict[str, Any] = {"complete": True}
    for name in (
        "model_calls",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "monetary_microunits",
    ):
        item = usage.get(name)
        require(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0,
            "invalid_topology",
            f"{field}.{name} must be a non-negative integer",
        )
        normalized[name] = item
    return normalized


def create_concept_node(
    *,
    name: str,
    description: str,
    kind: str,
    accepts: str,
    produces: str,
    boundary: str,
    excludes: str,
    supersedes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Create one immutable, content-addressed conceptual capability."""

    checked_kind = require_string(kind, "node.kind")
    require(
        checked_kind in NODE_KINDS,
        "invalid_topology",
        "node.kind is unsupported",
        value=checked_kind,
    )
    raw_supersedes = [] if supersedes is None else supersedes
    require(
        isinstance(raw_supersedes, (list, tuple)) and len(raw_supersedes) <= 20,
        "invalid_topology",
        "node.supersedes must contain at most 20 references",
    )
    checked_supersedes = sorted(
        {_sha256_digest(item, "node.supersedes[]") for item in raw_supersedes}
    )
    require(
        len(checked_supersedes) == len(raw_supersedes),
        "invalid_topology",
        "node.supersedes must be unique",
    )
    core = {
        "name": _bounded_string(name, "node.name", maximum=500),
        "description": _bounded_string(description, "node.description"),
        "kind": checked_kind,
        "accepts": _bounded_string(accepts, "node.accepts"),
        "produces": _bounded_string(produces, "node.produces"),
        "boundary": _bounded_string(boundary, "node.boundary"),
        "excludes": _bounded_string(excludes, "node.excludes"),
        "supersedes": checked_supersedes,
    }
    node_ref = content_digest(core)
    require(
        node_ref not in checked_supersedes,
        "invalid_topology",
        "A concept node cannot supersede itself",
    )
    return {**core, "node_ref": node_ref}


def validate_concept_node(value: Any, field: str = "node") -> dict[str, Any]:
    node = require_object(value, field)
    _require_exact_keys(
        node,
        {
            "name",
            "description",
            "kind",
            "accepts",
            "produces",
            "boundary",
            "excludes",
            "supersedes",
            "node_ref",
        },
        field,
    )
    supersedes = node.get("supersedes", [])
    require(
        isinstance(supersedes, list),
        "invalid_topology",
        f"{field}.supersedes must be a list",
    )
    rebuilt = create_concept_node(
        name=node.get("name"),
        description=node.get("description"),
        kind=node.get("kind"),
        accepts=node.get("accepts"),
        produces=node.get("produces"),
        boundary=node.get("boundary"),
        excludes=node.get("excludes"),
        supersedes=supersedes,
    )
    require(
        node.get("node_ref") == rebuilt["node_ref"],
        "invalid_topology",
        f"{field}.node_ref does not match the immutable contract",
    )
    require(
        canonical_json(node) == canonical_json(rebuilt),
        "invalid_topology",
        f"{field} contains unsupported or non-canonical fields",
    )
    return rebuilt


def create_topology_edge(
    *,
    parent_ref: str,
    child_ref: str,
    relation: str,
    role: str,
    rationale: str,
) -> dict[str, Any]:
    checked_relation = require_string(relation, "edge.relation")
    require(
        checked_relation in EDGE_RELATIONS,
        "invalid_topology",
        "edge.relation is unsupported",
        value=checked_relation,
    )
    core = {
        "parent_ref": _sha256_digest(parent_ref, "edge.parent_ref"),
        "child_ref": _sha256_digest(child_ref, "edge.child_ref"),
        "relation": checked_relation,
        "role": _bounded_string(role, "edge.role", maximum=500),
        "rationale": _bounded_string(rationale, "edge.rationale"),
    }
    require(
        core["parent_ref"] != core["child_ref"],
        "invalid_topology",
        "A topology edge cannot be a self-edge",
    )
    return {**core, "edge_ref": content_digest(core)}


def validate_topology_edge(value: Any, field: str = "edge") -> dict[str, Any]:
    edge = require_object(value, field)
    _require_exact_keys(
        edge,
        {"parent_ref", "child_ref", "relation", "role", "rationale", "edge_ref"},
        field,
    )
    rebuilt = create_topology_edge(
        parent_ref=edge.get("parent_ref"),
        child_ref=edge.get("child_ref"),
        relation=edge.get("relation"),
        role=edge.get("role"),
        rationale=edge.get("rationale"),
    )
    require(
        edge.get("edge_ref") == rebuilt["edge_ref"],
        "invalid_topology",
        f"{field}.edge_ref does not match the edge content",
    )
    require(
        canonical_json(edge) == canonical_json(rebuilt),
        "invalid_topology",
        f"{field} contains unsupported or non-canonical fields",
    )
    return rebuilt


def _validate_graph(
    root_ref: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    require(
        1 <= len(nodes) <= MAX_NODES,
        "invalid_topology",
        f"A snapshot must contain between 1 and {MAX_NODES} nodes",
    )
    require(
        len(edges) <= MAX_EDGES,
        "invalid_topology",
        f"A snapshot may contain at most {MAX_EDGES} edges",
    )
    node_refs = [node["node_ref"] for node in nodes]
    require(
        len(node_refs) == len(set(node_refs)),
        "invalid_topology",
        "Concept node references must be unique",
    )
    names = [node["name"].casefold() for node in nodes]
    require(
        len(names) == len(set(names)),
        "invalid_topology",
        "Active concept node names must be unique",
    )
    identifiers = set(node_refs)
    node_by_ref = {node["node_ref"]: node for node in nodes}
    require(
        root_ref in identifiers,
        "invalid_topology",
        "root_ref must identify an active concept node",
    )
    root_node = next(node for node in nodes if node["node_ref"] == root_ref)
    require(
        root_node["name"] == "RootSolver" and root_node["kind"] == "composite",
        "invalid_topology",
        "The configured universal root must be a composite named RootSolver",
    )
    edge_refs = [edge["edge_ref"] for edge in edges]
    require(
        len(edge_refs) == len(set(edge_refs)),
        "invalid_topology",
        "Topology edge references must be unique",
    )
    pairs: set[tuple[str, str]] = set()
    incoming: dict[str, int] = {node_ref: 0 for node_ref in node_refs}
    children: dict[str, list[str]] = {node_ref: [] for node_ref in node_refs}
    for edge in edges:
        parent_ref = edge["parent_ref"]
        child_ref = edge["child_ref"]
        require(
            parent_ref in identifiers and child_ref in identifiers,
            "invalid_topology",
            "Every topology edge endpoint must be active",
            edge_ref=edge["edge_ref"],
        )
        pair = (parent_ref, child_ref)
        require(
            pair not in pairs,
            "invalid_topology",
            "One ordered node pair cannot carry multiple topology relations",
            parent_ref=parent_ref,
            child_ref=child_ref,
        )
        pairs.add(pair)
        if edge["relation"] == "composition":
            require(
                node_by_ref[parent_ref]["kind"] == "composite",
                "invalid_topology",
                "A composition parent must be a composite node",
                parent_ref=parent_ref,
            )
            require(
                node_by_ref[child_ref]["kind"] in {"solver", "composite"},
                "invalid_topology",
                "A composition child must be an executable solver or composite",
                child_ref=child_ref,
            )
        incoming[child_ref] += 1
        children[parent_ref].append(child_ref)
    require(
        incoming[root_ref] == 0,
        "invalid_topology",
        "RootSolver cannot be a topology child",
    )
    require(
        {ref for ref, count in incoming.items() if count == 0} == {root_ref},
        "invalid_topology",
        "RootSolver must be the only zero-incoming active node",
    )

    remaining_incoming = dict(incoming)
    ready = [root_ref]
    visited: set[str] = set()
    while ready:
        node_ref = ready.pop()
        if node_ref in visited:
            continue
        visited.add(node_ref)
        for child_ref in children[node_ref]:
            remaining_incoming[child_ref] -= 1
            if remaining_incoming[child_ref] == 0:
                ready.append(child_ref)
    require(
        visited == identifiers,
        "invalid_topology",
        "The concept graph contains a cycle",
        node_refs=sorted(identifiers - visited),
    )


def create_concept_graph_snapshot(
    *,
    root_ref: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    predecessor_snapshot_digest: str | None = None,
) -> dict[str, Any]:
    checked_root = _sha256_digest(root_ref, "snapshot.root_ref")
    require(isinstance(nodes, list), "invalid_topology", "snapshot.nodes must be a list")
    require(isinstance(edges, list), "invalid_topology", "snapshot.edges must be a list")
    require(
        1 <= len(nodes) <= MAX_NODES,
        "invalid_topology",
        f"A snapshot must contain between 1 and {MAX_NODES} nodes",
    )
    require(
        len(edges) <= MAX_EDGES,
        "invalid_topology",
        f"A snapshot may contain at most {MAX_EDGES} edges",
    )
    checked_nodes = sorted(
        (validate_concept_node(item, f"snapshot.nodes[{index}]") for index, item in enumerate(nodes)),
        key=lambda item: item["node_ref"],
    )
    checked_edges = sorted(
        (validate_topology_edge(item, f"snapshot.edges[{index}]") for index, item in enumerate(edges)),
        key=lambda item: item["edge_ref"],
    )
    _validate_graph(checked_root, checked_nodes, checked_edges)
    predecessor = (
        None
        if predecessor_snapshot_digest is None
        else _sha256_digest(
            predecessor_snapshot_digest, "snapshot.predecessor_snapshot_digest"
        )
    )
    core = {
        "protocol_version": "1",
        "kind": SNAPSHOT_KIND,
        "schema_version": SCHEMA_VERSION,
        "assessment_boundary": ASSESSMENT_BOUNDARY,
        "root_ref": checked_root,
        "nodes": checked_nodes,
        "edges": checked_edges,
        "predecessor_snapshot_digest": predecessor,
    }
    require(
        len(canonical_json(core).encode("utf-8")) <= MAX_SNAPSHOT_BYTES,
        "topology_too_large",
        f"A concept graph snapshot must be at most {MAX_SNAPSHOT_BYTES} bytes",
    )
    return {**core, "snapshot_digest": content_digest(core)}


def validate_concept_graph_snapshot(value: Any) -> dict[str, Any]:
    snapshot = require_object(value, "snapshot")
    _require_exact_keys(
        snapshot,
        {
            "protocol_version",
            "kind",
            "schema_version",
            "assessment_boundary",
            "root_ref",
            "nodes",
            "edges",
            "predecessor_snapshot_digest",
            "snapshot_digest",
        },
        "snapshot",
    )
    require(
        len(canonical_json(snapshot).encode("utf-8")) <= MAX_SNAPSHOT_BYTES + 512,
        "topology_too_large",
        f"A concept graph snapshot must be at most {MAX_SNAPSHOT_BYTES} bytes",
    )
    require(
        snapshot.get("protocol_version") == "1"
        and snapshot.get("kind") == SNAPSHOT_KIND
        and snapshot.get("schema_version") == SCHEMA_VERSION
        and snapshot.get("assessment_boundary") == ASSESSMENT_BOUNDARY,
        "invalid_topology",
        "Unsupported concept graph snapshot envelope",
    )
    rebuilt = create_concept_graph_snapshot(
        root_ref=snapshot.get("root_ref"),
        nodes=snapshot.get("nodes"),
        edges=snapshot.get("edges"),
        predecessor_snapshot_digest=snapshot.get("predecessor_snapshot_digest"),
    )
    require(
        snapshot.get("snapshot_digest") == rebuilt["snapshot_digest"],
        "invalid_topology",
        "snapshot_digest does not match the concept graph content",
    )
    require(
        canonical_json(snapshot) == canonical_json(rebuilt),
        "invalid_topology",
        "The concept graph snapshot contains unsupported or non-canonical fields",
    )
    return rebuilt


def _normalize_patch_operation(value: Any, field: str) -> dict[str, Any]:
    operation = require_object(value, field)
    action = require_string(operation.get("op"), f"{field}.op")
    if action == "add_node":
        normalized = {
            "op": action,
            "node": validate_concept_node(operation.get("node"), f"{field}.node"),
        }
    elif action == "add_edge":
        normalized = {
            "op": action,
            "edge": validate_topology_edge(operation.get("edge"), f"{field}.edge"),
        }
    elif action == "retire_edge":
        normalized = {
            "op": action,
            "edge_ref": _sha256_digest(
                operation.get("edge_ref"), f"{field}.edge_ref"
            ),
        }
    elif action == "retire_node":
        normalized = {
            "op": action,
            "node_ref": _sha256_digest(
                operation.get("node_ref"), f"{field}.node_ref"
            ),
        }
    else:
        require(False, "invalid_topology", f"{field}.op is unsupported", value=action)
        raise AssertionError("unreachable")
    require(
        canonical_json(operation) == canonical_json(normalized),
        "invalid_topology",
        f"{field} contains unsupported or non-canonical fields",
    )
    return normalized


def create_topology_patch(
    *,
    base_snapshot_digest: str,
    operations: list[dict[str, Any]],
    rationale: str,
) -> dict[str, Any]:
    require(
        isinstance(operations, list) and len(operations) <= MAX_PATCH_OPERATIONS,
        "invalid_topology",
        f"patch.operations must contain at most {MAX_PATCH_OPERATIONS} items",
    )
    checked_operations = [
        _normalize_patch_operation(item, f"patch.operations[{index}]")
        for index, item in enumerate(operations)
    ]
    core = {
        "protocol_version": "1",
        "kind": PATCH_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "review_required",
        "base_snapshot_digest": _sha256_digest(
            base_snapshot_digest, "patch.base_snapshot_digest"
        ),
        "operations": checked_operations,
        "rationale": _bounded_string(rationale, "patch.rationale", maximum=20_000),
    }
    require(
        len(canonical_json(core).encode("utf-8")) <= MAX_PATCH_BYTES,
        "topology_too_large",
        f"A topology patch must be at most {MAX_PATCH_BYTES} bytes",
    )
    return {**core, "patch_digest": content_digest(core)}


def validate_topology_patch(value: Any) -> dict[str, Any]:
    patch = require_object(value, "patch")
    _require_exact_keys(
        patch,
        {
            "protocol_version",
            "kind",
            "schema_version",
            "status",
            "base_snapshot_digest",
            "operations",
            "rationale",
            "patch_digest",
        },
        "patch",
    )
    require(
        len(canonical_json(patch).encode("utf-8")) <= MAX_PATCH_BYTES + 512,
        "topology_too_large",
        f"A topology patch must be at most {MAX_PATCH_BYTES} bytes",
    )
    require(
        patch.get("protocol_version") == "1"
        and patch.get("kind") == PATCH_KIND
        and patch.get("schema_version") == SCHEMA_VERSION
        and patch.get("status") == "review_required",
        "invalid_topology",
        "Unsupported topology patch envelope",
    )
    rebuilt = create_topology_patch(
        base_snapshot_digest=patch.get("base_snapshot_digest"),
        operations=patch.get("operations"),
        rationale=patch.get("rationale"),
    )
    require(
        patch.get("patch_digest") == rebuilt["patch_digest"],
        "invalid_topology",
        "patch_digest does not match the topology patch content",
    )
    require(
        canonical_json(patch) == canonical_json(rebuilt),
        "invalid_topology",
        "The topology patch contains unsupported or non-canonical fields",
    )
    return rebuilt


def _apply_patch_unreviewed(
    snapshot_value: Any, patch_value: Any
) -> dict[str, Any]:
    snapshot = validate_concept_graph_snapshot(snapshot_value)
    patch = validate_topology_patch(patch_value)
    require(
        patch["base_snapshot_digest"] == snapshot["snapshot_digest"],
        "stale_topology_snapshot",
        "The topology patch targets a different snapshot",
        expected=patch["base_snapshot_digest"],
        actual=snapshot["snapshot_digest"],
    )
    if not patch["operations"]:
        return snapshot
    nodes = {node["node_ref"]: node for node in snapshot["nodes"]}
    edges = {edge["edge_ref"]: edge for edge in snapshot["edges"]}
    base_node_refs = set(nodes)
    touched: set[tuple[str, str]] = set()
    for index, operation in enumerate(patch["operations"]):
        action = operation["op"]
        target_kind = "node" if action.endswith("node") else "edge"
        target_ref = (
            operation[target_kind][f"{target_kind}_ref"]
            if action.startswith("add_")
            else operation[f"{target_kind}_ref"]
        )
        marker = (target_kind, target_ref)
        require(
            marker not in touched,
            "invalid_topology",
            "A topology patch may touch each reference at most once",
            operation=index,
            target_ref=target_ref,
        )
        touched.add(marker)
        if action == "add_node":
            node = operation["node"]
            require(
                target_ref not in nodes,
                "invalid_topology",
                "A topology patch cannot add an existing node",
                node_ref=target_ref,
            )
            require(
                all(item in base_node_refs for item in node["supersedes"]),
                "invalid_topology",
                "A new contract may supersede only nodes in the exact base snapshot",
                node_ref=target_ref,
            )
            require(
                snapshot["root_ref"] not in node["supersedes"],
                "invalid_topology",
                "A topology node cannot supersede the configured RootSolver",
                node_ref=target_ref,
            )
            nodes[target_ref] = node
        elif action == "add_edge":
            edge = operation["edge"]
            require(
                target_ref not in edges,
                "invalid_topology",
                "A topology patch cannot add an existing edge",
                edge_ref=target_ref,
            )
            require(
                edge["parent_ref"] in nodes and edge["child_ref"] in nodes,
                "invalid_topology",
                "An added edge must follow its added nodes in patch order",
                edge_ref=target_ref,
            )
            edges[target_ref] = edge
        elif action == "retire_edge":
            require(
                target_ref in edges,
                "invalid_topology",
                "A topology patch cannot retire an unknown edge",
                edge_ref=target_ref,
            )
            edges.pop(target_ref)
        elif action == "retire_node":
            require(
                target_ref in nodes,
                "invalid_topology",
                "A topology patch cannot retire an unknown node",
                node_ref=target_ref,
            )
            require(
                target_ref != snapshot["root_ref"],
                "invalid_topology",
                "RootSolver cannot be retired",
            )
            incident = [
                edge_ref
                for edge_ref, edge in edges.items()
                if target_ref in {edge["parent_ref"], edge["child_ref"]}
            ]
            require(
                not incident,
                "invalid_topology",
                "A node can be retired only after all incident edges",
                node_ref=target_ref,
                incident_edge_refs=sorted(incident),
            )
            nodes.pop(target_ref)
    return create_concept_graph_snapshot(
        root_ref=snapshot["root_ref"],
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        predecessor_snapshot_digest=snapshot["snapshot_digest"],
    )


def _normalize_fit(value: Any, field: str) -> dict[str, str]:
    fit = require_object(value, field)
    decision = require_string(fit.get("decision"), f"{field}.decision")
    require(
        decision in FIT_DECISIONS,
        "invalid_topology",
        f"{field}.decision is unsupported",
        value=decision,
    )
    normalized = {
        "decision": decision,
        "rationale": _bounded_string(fit.get("rationale"), f"{field}.rationale"),
    }
    require(
        canonical_json(fit) == canonical_json(normalized),
        "invalid_topology",
        f"{field} contains unsupported or non-canonical fields",
    )
    return normalized


def _normalize_sibling_classification(value: Any, field: str) -> dict[str, Any]:
    classification = require_object(value, field)
    normalized = {
        "child_ref": _sha256_digest(
            classification.get("child_ref"), f"{field}.child_ref"
        ),
        "why_parent_is_more_general": _bounded_string(
            classification.get("why_parent_is_more_general"),
            f"{field}.why_parent_is_more_general",
        ),
        "reframing_effect": _bounded_string(
            classification.get("reframing_effect"), f"{field}.reframing_effect"
        ),
        "differentia": _bounded_string(
            classification.get("differentia"), f"{field}.differentia"
        ),
        "contract_fit": _normalize_fit(
            classification.get("contract_fit"), f"{field}.contract_fit"
        ),
    }
    require(
        canonical_json(classification) == canonical_json(normalized),
        "invalid_topology",
        f"{field} contains unsupported or non-canonical fields",
    )
    return normalized


def _normalize_ascent_assessment(value: Any, field: str) -> dict[str, Any]:
    assessment = require_object(value, field)
    _require_exact_keys(
        assessment,
        {
            "child_ref",
            "parent_ref",
            "genus_rationale",
            "intermediate_parent_search",
            "sibling_search",
            "partition_status",
            "sibling_classifications",
            "sibling_independence",
        },
        field,
    )
    sibling_classifications = assessment.get("sibling_classifications")
    require(
        isinstance(sibling_classifications, list) and sibling_classifications,
        "invalid_topology",
        f"{field}.sibling_classifications must be a non-empty list",
    )
    checked_siblings = sorted(
        (
            _normalize_sibling_classification(
                item, f"{field}.sibling_classifications[{index}]"
            )
            for index, item in enumerate(sibling_classifications)
        ),
        key=lambda item: item["child_ref"],
    )
    sibling_refs = [item["child_ref"] for item in checked_siblings]
    require(
        len(sibling_refs) == len(set(sibling_refs)),
        "invalid_topology",
        f"{field}.sibling_classifications must identify unique children",
    )
    independence = require_object(
        assessment.get("sibling_independence"), f"{field}.sibling_independence"
    )
    independence_status = require_string(
        independence.get("status"), f"{field}.sibling_independence.status"
    )
    require(
        independence_status in {"passed", "uncertain"},
        "invalid_topology",
        "An accepted open sibling set may be passed or uncertain, never failed",
    )
    checked_independence = {
        "status": independence_status,
        "rationale": _bounded_string(
            independence.get("rationale"),
            f"{field}.sibling_independence.rationale",
        ),
    }
    normalized = {
        "child_ref": _sha256_digest(
            assessment.get("child_ref"), f"{field}.child_ref"
        ),
        "parent_ref": _sha256_digest(
            assessment.get("parent_ref"), f"{field}.parent_ref"
        ),
        "genus_rationale": _bounded_string(
            assessment.get("genus_rationale"), f"{field}.genus_rationale"
        ),
        "intermediate_parent_search": _bounded_string(
            assessment.get("intermediate_parent_search"),
            f"{field}.intermediate_parent_search",
        ),
        "sibling_search": _bounded_string(
            assessment.get("sibling_search"), f"{field}.sibling_search"
        ),
        # Specialization is open-world in schema v1. Completeness belongs to the
        # constitutive four-test decomposition, not to unseen possible species.
        "partition_status": require_string(
            assessment.get("partition_status"), f"{field}.partition_status"
        ),
        "sibling_classifications": checked_siblings,
        "sibling_independence": checked_independence,
    }
    require(
        normalized["partition_status"] == "open",
        "invalid_topology",
        "Schema v1 does not permit an exhaustive specialization claim",
    )
    return normalized


def _normalize_axis_binding(value: Any, field: str) -> dict[str, Any]:
    binding = require_object(value, field)
    normalized = {
        "axis_id": _identifier(binding.get("axis_id"), f"{field}.axis_id"),
        "child_ref": _sha256_digest(
            binding.get("child_ref"), f"{field}.child_ref"
        ),
        "edge_ref": _sha256_digest(binding.get("edge_ref"), f"{field}.edge_ref"),
        "contract_fit": _normalize_fit(
            binding.get("contract_fit"), f"{field}.contract_fit"
        ),
    }
    require(
        canonical_json(binding) == canonical_json(normalized),
        "invalid_topology",
        f"{field} contains unsupported or non-canonical fields",
    )
    return normalized


def _normalize_parent_synthesis(value: Any, field: str) -> dict[str, Any]:
    synthesis = require_object(value, field)
    _require_exact_keys(
        synthesis,
        {"child_refs", "outward_capability", "integration"},
        field,
    )
    child_refs = synthesis.get("child_refs")
    require(
        isinstance(child_refs, list) and child_refs,
        "invalid_topology",
        f"{field}.child_refs must be a non-empty list",
    )
    checked_children = sorted(
        _sha256_digest(item, f"{field}.child_refs[]") for item in child_refs
    )
    require(
        len(checked_children) == len(set(checked_children)),
        "invalid_topology",
        f"{field}.child_refs must be unique",
    )
    normalized = {
        "child_refs": checked_children,
        "outward_capability": _bounded_string(
            synthesis.get("outward_capability"),
            f"{field}.outward_capability",
            maximum=10_000,
        ),
        "integration": _bounded_string(
            synthesis.get("integration"), f"{field}.integration", maximum=4_000
        ),
    }
    return normalized


def _work_id(axis_id: str, edge_ref: str) -> str:
    suffix = content_digest(
        {"axis_id": axis_id, "edge_ref": edge_ref}
    ).removeprefix("sha256:")[:20]
    return f"fi-work-{suffix}"


def create_fractal_case_proposal(
    snapshot_value: Any,
    *,
    problem: Any,
    problem_framing: Any,
    patch: Any,
    abstraction_ascent: list[str],
    downward_route: list[str],
    ascent_assessments: list[dict[str, Any]],
    subject_decomposition: Any,
    axis_bindings: list[dict[str, Any]],
    parent_synthesis: Any,
    provenance: Any,
    usage: Any,
) -> dict[str, Any]:
    """Build a review-only mandatory-ascent FI planning artifact."""

    snapshot = validate_concept_graph_snapshot(snapshot_value)
    checked_patch = validate_topology_patch(patch)
    proposed_snapshot = _apply_patch_unreviewed(snapshot, checked_patch)
    require(
        isinstance(abstraction_ascent, list)
        and 2 <= len(abstraction_ascent) <= MAX_ROUTE_LENGTH,
        "invalid_topology",
        f"abstraction_ascent must contain 2 to {MAX_ROUTE_LENGTH} node references",
    )
    ascent = [
        _sha256_digest(item, f"abstraction_ascent[{index}]")
        for index, item in enumerate(abstraction_ascent)
    ]
    require(
        len(ascent) == len(set(ascent)),
        "invalid_topology",
        "abstraction_ascent cannot repeat a node",
    )
    require(
        isinstance(downward_route, list),
        "invalid_topology",
        "downward_route must be a list",
    )
    descent = [
        _sha256_digest(item, f"downward_route[{index}]")
        for index, item in enumerate(downward_route)
    ]
    require(
        descent == list(reversed(ascent)),
        "invalid_topology",
        "downward_route must exactly reverse abstraction_ascent",
    )
    require(
        ascent[-1] == proposed_snapshot["root_ref"],
        "invalid_topology",
        "abstraction_ascent must end at RootSolver",
    )
    node_by_ref = {node["node_ref"]: node for node in proposed_snapshot["nodes"]}
    edge_by_ref = {edge["edge_ref"]: edge for edge in proposed_snapshot["edges"]}
    added_node_refs = {
        operation["node"]["node_ref"]
        for operation in checked_patch["operations"]
        if operation["op"] == "add_node"
    }
    base_node_refs = {node["node_ref"] for node in snapshot["nodes"]}
    base_children_by_slot: dict[tuple[str, str], set[str]] = {}
    for edge in snapshot["edges"]:
        base_children_by_slot.setdefault(
            (edge["parent_ref"], edge["relation"]), set()
        ).add(edge["child_ref"])
    reviewed_superseded_refs: set[str] = set()
    specialization_by_pair = {
        (edge["parent_ref"], edge["child_ref"]): edge
        for edge in proposed_snapshot["edges"]
        if edge["relation"] == "specialization"
    }
    specialization_children: dict[str, set[str]] = {}
    for edge in proposed_snapshot["edges"]:
        if edge["relation"] == "specialization":
            specialization_children.setdefault(edge["parent_ref"], set()).add(
                edge["child_ref"]
            )
    require(
        all(node_ref in node_by_ref for node_ref in ascent),
        "invalid_topology",
        "Every abstraction route node must exist in the proposed snapshot",
    )
    for child_ref, parent_ref in zip(ascent, ascent[1:]):
        require(
            (parent_ref, child_ref) in specialization_by_pair,
            "invalid_topology",
            "Every ascent step must follow one adjacent specialization edge",
            child_ref=child_ref,
            parent_ref=parent_ref,
        )

    require(
        isinstance(ascent_assessments, list)
        and len(ascent_assessments) == len(ascent) - 1,
        "invalid_topology",
        "Every ascent edge requires one public classification assessment",
    )
    checked_assessments = [
        _normalize_ascent_assessment(item, f"ascent_assessments[{index}]")
        for index, item in enumerate(ascent_assessments)
    ]
    for index, assessment in enumerate(checked_assessments):
        child_ref = ascent[index]
        parent_ref = ascent[index + 1]
        sibling_classifications = assessment["sibling_classifications"]
        sibling_refs = [item["child_ref"] for item in sibling_classifications]
        require(
            (assessment["child_ref"], assessment["parent_ref"])
            == (child_ref, parent_ref),
            "invalid_topology",
            "Ascent assessments must follow route order",
            index=index,
        )
        require(
            set(sibling_refs) == specialization_children.get(parent_ref, set()),
            "invalid_topology",
            "An ascent assessment must inspect every active specialization sibling",
            parent_ref=parent_ref,
        )
        require(
            child_ref in sibling_refs,
            "invalid_topology",
            "An ascent assessment must include its selected child",
        )
        for classification in sibling_classifications:
            classified_ref = classification["child_ref"]
            decision = classification["contract_fit"]["decision"]
            if decision == "exact_reuse":
                require(
                    classified_ref in base_node_refs,
                    "invalid_topology",
                    "An ascent exact_reuse must cite a base-snapshot contract",
                )
            elif decision in {"new_node", "new_version", "split"}:
                require(
                    classified_ref in added_node_refs,
                    "invalid_topology",
                    f"An ascent {decision} must cite a patch-added contract",
                )
                supersedes = node_by_ref[classified_ref]["supersedes"]
                require(
                    (decision == "new_node" and not supersedes)
                    or (decision in {"new_version", "split"} and bool(supersedes)),
                    "invalid_topology",
                    "The ascent fit decision does not match revision lineage",
                )
                if decision in {"new_version", "split"}:
                    allowed_predecessors = base_children_by_slot.get(
                        (parent_ref, "specialization"), set()
                    )
                    require(
                        set(supersedes) <= allowed_predecessors,
                        "invalid_topology",
                        "An ascent revision may supersede only base children in the same specialization slot",
                        unrelated_predecessor_refs=sorted(
                            set(supersedes) - allowed_predecessors
                        ),
                    )
                    reviewed_superseded_refs.update(supersedes)
            elif decision == "shared_parent":
                require(
                    parent_ref in added_node_refs
                    and len(sibling_refs) >= 2
                    and classified_ref in base_node_refs,
                    "invalid_topology",
                    "shared_parent requires a new parent over multiple reused siblings",
                )

    decomposition = validate_decomposition_proposal(subject_decomposition)
    subject_ref = ascent[0]
    subject_node = node_by_ref[subject_ref]
    framing = _normalize_problem_framing(problem_framing, "problem_framing")
    require(
        framing["subject_ref"] == subject_ref,
        "invalid_topology",
        "Problem framing must identify the route's specific subject",
    )
    root = decomposition["root"]
    require(
        root["concept"]["name"] == subject_node["name"]
        and root["concept"]["description"] == subject_node["description"]
        and root["concept"]["context"].get("subject_ref") == subject_ref,
        "invalid_topology",
        "The four-test decomposition must bind the route's exact subject contract",
    )
    require(
        root["status"] == "structurally_clean"
        and root["completeness"]["passed"]
        and root["routing"]["passed"],
        "invalid_topology",
        "The subject decomposition must pass its recorded four-test structure and routing",
    )
    axes = root["axes"]
    require(
        len(axes) >= 2,
        "invalid_topology",
        "A decomposed FI subject requires at least two differentiated constituents",
    )
    require(
        isinstance(axis_bindings, list) and len(axis_bindings) == len(axes),
        "invalid_topology",
        "Every accepted subject axis requires one topology binding",
    )
    checked_bindings = [
        _normalize_axis_binding(item, f"axis_bindings[{index}]")
        for index, item in enumerate(axis_bindings)
    ]
    axis_ids = [axis["axis_id"] for axis in axes]
    binding_axis_ids = [binding["axis_id"] for binding in checked_bindings]
    require(
        len(binding_axis_ids) == len(set(binding_axis_ids))
        and set(binding_axis_ids) == set(axis_ids),
        "invalid_topology",
        "Axis bindings must cover every accepted axis exactly once",
    )
    axis_position = {axis_id: index for index, axis_id in enumerate(axis_ids)}
    checked_bindings.sort(key=lambda binding: axis_position[binding["axis_id"]])
    child_refs = [binding["child_ref"] for binding in checked_bindings]
    require(
        len(child_refs) == len(set(child_refs)),
        "invalid_topology",
        "Independent composition axes must bind to distinct concept nodes",
    )
    active_composition_child_refs = {
        edge["child_ref"]
        for edge in proposed_snapshot["edges"]
        if edge["relation"] == "composition" and edge["parent_ref"] == subject_ref
    }
    require(
        set(child_refs) == active_composition_child_refs,
        "invalid_topology",
        "The subject's active composition children must exactly match its accepted axes",
        unbound_child_refs=sorted(active_composition_child_refs - set(child_refs)),
        missing_child_refs=sorted(set(child_refs) - active_composition_child_refs),
    )
    work_items: list[dict[str, Any]] = []
    axis_by_id = {axis["axis_id"]: axis for axis in axes}
    for binding in checked_bindings:
        edge = edge_by_ref.get(binding["edge_ref"])
        require(
            edge is not None
            and edge["relation"] == "composition"
            and edge["parent_ref"] == subject_ref
            and edge["child_ref"] == binding["child_ref"],
            "invalid_topology",
            "Every axis binding must identify its active subject composition edge",
            axis_id=binding["axis_id"],
        )
        decision = binding["contract_fit"]["decision"]
        if decision == "exact_reuse":
            require(
                binding["child_ref"] in base_node_refs,
                "invalid_topology",
                "exact_reuse must cite an immutable node from the base snapshot",
            )
        elif decision in {"new_node", "new_version", "split"}:
            require(
                binding["child_ref"] in added_node_refs,
                "invalid_topology",
                f"{decision} must cite a node introduced by this patch",
            )
            supersedes = node_by_ref[binding["child_ref"]]["supersedes"]
            require(
                (decision == "new_node" and not supersedes)
                or (decision in {"new_version", "split"} and bool(supersedes)),
                "invalid_topology",
                "The fit decision does not match the node's revision lineage",
            )
            if decision in {"new_version", "split"}:
                allowed_predecessors = base_children_by_slot.get(
                    (subject_ref, "composition"), set()
                )
                require(
                    set(supersedes) <= allowed_predecessors,
                    "invalid_topology",
                    "A composition revision may supersede only base children in the same parent slot",
                    unrelated_predecessor_refs=sorted(
                        set(supersedes) - allowed_predecessors
                    ),
                )
                reviewed_superseded_refs.update(supersedes)
        elif decision == "shared_parent":
            require(
                subject_ref in added_node_refs
                and binding["child_ref"] in base_node_refs,
                "invalid_topology",
                "shared_parent requires a new subject parent over reused children",
            )
        axis = axis_by_id[binding["axis_id"]]
        work_items.append(
            {
                "work_id": _work_id(binding["axis_id"], binding["edge_ref"]),
                "axis_id": binding["axis_id"],
                "parent_ref": subject_ref,
                "node_ref": binding["child_ref"],
                "edge_ref": binding["edge_ref"],
                "role": edge["role"],
                "depends_on": [],
            }
        )
    work_items.sort(key=lambda item: axis_ids.index(item["axis_id"]))

    synthesis = _normalize_parent_synthesis(parent_synthesis, "parent_synthesis")
    require(
        set(synthesis["child_refs"]) == set(child_refs),
        "invalid_topology",
        "Parent synthesis must name exactly the invoked composition children",
    )
    require(
        synthesis["outward_capability"] == subject_node["produces"],
        "invalid_topology",
        "Parent synthesis must realize the subject's immutable outward capability",
    )
    assessed_sibling_refs = {
        classification["child_ref"]
        for assessment in checked_assessments
        for classification in assessment["sibling_classifications"]
    }
    relevant_node_refs = set(ascent) | set(child_refs) | assessed_sibling_refs
    require(
        added_node_refs <= relevant_node_refs,
        "invalid_topology",
        "Every patch-added node must participate in the reviewed route, sibling set, or composition",
        unassessed_node_refs=sorted(added_node_refs - relevant_node_refs),
    )
    assessed_edge_refs = {
        specialization_by_pair[(ascent[index + 1], ascent[index])]["edge_ref"]
        for index in range(len(ascent) - 1)
    }
    for assessment in checked_assessments:
        parent_ref = assessment["parent_ref"]
        assessed_edge_refs.update(
            specialization_by_pair[(parent_ref, classification["child_ref"])][
                "edge_ref"
            ]
            for classification in assessment["sibling_classifications"]
        )
    assessed_edge_refs.update(binding["edge_ref"] for binding in checked_bindings)
    added_edge_refs = {
        operation["edge"]["edge_ref"]
        for operation in checked_patch["operations"]
        if operation["op"] == "add_edge"
    }
    require(
        added_edge_refs <= assessed_edge_refs,
        "invalid_topology",
        "Every patch-added edge must participate in reviewed routing or composition",
        unassessed_edge_refs=sorted(added_edge_refs - assessed_edge_refs),
    )
    superseded_refs = reviewed_superseded_refs
    retired_node_refs = {
        operation["node_ref"]
        for operation in checked_patch["operations"]
        if operation["op"] == "retire_node"
    }
    require(
        retired_node_refs <= superseded_refs,
        "invalid_topology",
        "A case may retire only a contract explicitly superseded by this patch",
        unassessed_node_refs=sorted(retired_node_refs - superseded_refs),
    )
    base_edge_by_ref = {edge["edge_ref"]: edge for edge in snapshot["edges"]}
    retired_edge_refs = {
        operation["edge_ref"]
        for operation in checked_patch["operations"]
        if operation["op"] == "retire_edge"
    }
    unrelated_retired_edges = [
        edge_ref
        for edge_ref in retired_edge_refs
        if not {
            base_edge_by_ref[edge_ref]["parent_ref"],
            base_edge_by_ref[edge_ref]["child_ref"],
        }
        <= (relevant_node_refs | superseded_refs)
    ]
    require(
        not unrelated_retired_edges,
        "invalid_topology",
        "A case may retire only edges within its reviewed topology closure",
        unassessed_edge_refs=sorted(unrelated_retired_edges),
    )
    core = {
        "protocol_version": "1",
        "kind": CASE_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "review_required",
        "assessment_boundary": ASSESSMENT_BOUNDARY,
        "base_snapshot_digest": snapshot["snapshot_digest"],
        "proposed_snapshot_digest": proposed_snapshot["snapshot_digest"],
        "problem": _normalize_problem(problem),
        "problem_framing": framing,
        "patch": checked_patch,
        "abstraction_ascent": ascent,
        "downward_route": descent,
        "ascent_assessments": checked_assessments,
        "subject_ref": subject_ref,
        "subject_decomposition": decomposition,
        "axis_bindings": checked_bindings,
        "work_items": work_items,
        "parent_synthesis": synthesis,
        "provenance": _normalize_provenance(provenance),
        "usage": _normalize_usage(usage),
    }
    require(
        len(canonical_json(core).encode("utf-8")) <= MAX_CASE_BYTES,
        "topology_too_large",
        f"An FI case proposal must be at most {MAX_CASE_BYTES} bytes",
    )
    return {**core, "case_proposal_digest": content_digest(core)}


def validate_fractal_case_proposal(
    value: Any, *, base_snapshot_value: Any
) -> dict[str, Any]:
    proposal = require_object(value, "case_proposal")
    _require_exact_keys(
        proposal,
        {
            "protocol_version",
            "kind",
            "schema_version",
            "status",
            "assessment_boundary",
            "base_snapshot_digest",
            "proposed_snapshot_digest",
            "problem",
            "problem_framing",
            "patch",
            "abstraction_ascent",
            "downward_route",
            "ascent_assessments",
            "subject_ref",
            "subject_decomposition",
            "axis_bindings",
            "work_items",
            "parent_synthesis",
            "provenance",
            "usage",
            "case_proposal_digest",
        },
        "case_proposal",
    )
    require(
        len(canonical_json(proposal).encode("utf-8")) <= MAX_CASE_BYTES + 512,
        "topology_too_large",
        f"An FI case proposal must be at most {MAX_CASE_BYTES} bytes",
    )
    require(
        proposal.get("protocol_version") == "1"
        and proposal.get("kind") == CASE_KIND
        and proposal.get("schema_version") == SCHEMA_VERSION
        and proposal.get("status") == "review_required"
        and proposal.get("assessment_boundary") == ASSESSMENT_BOUNDARY,
        "invalid_topology",
        "Unsupported FI case proposal envelope",
    )
    rebuilt = create_fractal_case_proposal(
        base_snapshot_value,
        problem=proposal.get("problem"),
        problem_framing=proposal.get("problem_framing"),
        patch=proposal.get("patch"),
        abstraction_ascent=proposal.get("abstraction_ascent"),
        downward_route=proposal.get("downward_route"),
        ascent_assessments=proposal.get("ascent_assessments"),
        subject_decomposition=proposal.get("subject_decomposition"),
        axis_bindings=proposal.get("axis_bindings"),
        parent_synthesis=proposal.get("parent_synthesis"),
        provenance=proposal.get("provenance"),
        usage=proposal.get("usage"),
    )
    require(
        proposal.get("case_proposal_digest") == rebuilt["case_proposal_digest"],
        "invalid_topology",
        "case_proposal_digest does not match the FI case content",
    )
    require(
        canonical_json(proposal) == canonical_json(rebuilt),
        "invalid_topology",
        "The FI case proposal contains unsupported or non-canonical fields",
    )
    return rebuilt


def _normalize_review(value: Any, proposal_digest: str) -> dict[str, str]:
    review = require_object(value, "review")
    require(
        review.get("decision") == "approved",
        "topology_review_required",
        "An explicit approved review is required",
    )
    normalized = {
        "decision": "approved",
        "case_proposal_digest": _sha256_digest(
            review.get("case_proposal_digest"), "review.case_proposal_digest"
        ),
        "reviewer": _bounded_string(
            review.get("reviewer"), "review.reviewer", maximum=500
        ),
        "basis": _bounded_string(review.get("basis"), "review.basis"),
    }
    require(
        normalized["case_proposal_digest"] == proposal_digest,
        "topology_review_required",
        "The review must approve this exact FI case proposal digest",
    )
    require(
        canonical_json(review) == canonical_json(normalized),
        "invalid_topology",
        "The review contains unsupported or non-canonical fields",
    )
    return normalized


def apply_fractal_case_proposal(
    snapshot_value: Any, proposal_value: Any, *, review: Any
) -> dict[str, Any]:
    """Return the all-or-nothing pure transition for one reviewed FI case.

    A persistent graph store must still compare-and-swap its current snapshot
    digest transactionally; this function neither stores state nor authenticates
    the reviewer assertion.
    """

    snapshot = validate_concept_graph_snapshot(snapshot_value)
    proposal = validate_fractal_case_proposal(
        proposal_value, base_snapshot_value=snapshot
    )
    _normalize_review(review, proposal["case_proposal_digest"])
    result = _apply_patch_unreviewed(snapshot, proposal["patch"])
    require(
        result["snapshot_digest"] == proposal["proposed_snapshot_digest"],
        "invalid_topology",
        "The reviewed patch does not produce its committed snapshot",
    )
    return result


class TopologyConstructionEngine:
    """Call one semantic oracle and wrap its answer in a strict FI artifact."""

    def __init__(self, oracle: TopologyOracle) -> None:
        self.oracle = oracle
        self.provenance = _normalize_provenance(oracle.identity, "oracle.identity")

    def propose(self, problem: Any, snapshot_value: Any) -> dict[str, Any]:
        snapshot = validate_concept_graph_snapshot(snapshot_value)
        checked_problem = _normalize_problem(problem)
        raw = require_object(
            self.oracle.propose_case(_json_copy(checked_problem), _json_copy(snapshot)),
            "oracle.proposal",
        )
        return create_fractal_case_proposal(
            snapshot,
            problem=checked_problem,
            problem_framing=raw.get("problem_framing"),
            patch=raw.get("patch"),
            abstraction_ascent=raw.get("abstraction_ascent"),
            downward_route=raw.get("downward_route"),
            ascent_assessments=raw.get("ascent_assessments"),
            subject_decomposition=raw.get("subject_decomposition"),
            axis_bindings=raw.get("axis_bindings"),
            parent_synthesis=raw.get("parent_synthesis"),
            provenance=self.provenance,
            usage=raw.get("usage"),
        )


def execution_plan_from_fi_case(
    case_value: Any,
    *,
    base_snapshot_value: Any,
    active_snapshot_value: Any,
    work_bindings: Any,
    synthesis: Any,
    review: Any,
    seed: int = 0,
) -> dict[str, Any]:
    """Compile reviewed composition work into the existing neutral plan.

    Specialization nodes are framing context, never model calls. This helper does
    not admit a Solver, create coordinator work, authorize payment, or weaken an
    execution AcceptSpec.
    """

    base_snapshot = validate_concept_graph_snapshot(base_snapshot_value)
    case = validate_fractal_case_proposal(
        case_value, base_snapshot_value=base_snapshot
    )
    checked_review = _normalize_review(review, case["case_proposal_digest"])
    active_snapshot = validate_concept_graph_snapshot(active_snapshot_value)
    require(
        active_snapshot["snapshot_digest"] == case["proposed_snapshot_digest"],
        "stale_topology_snapshot",
        "Execution requires the exact reviewed post-patch concept graph snapshot",
    )
    structural_failures = decomposition_execution_failures(
        case["subject_decomposition"]
    )
    require(
        not structural_failures,
        "decomposition_not_execution_ready",
        "The subject decomposition has unresolved execution-readiness failures",
        structural_failures=structural_failures,
    )
    require(
        isinstance(work_bindings, list),
        "invalid_topology",
        "work_bindings must be a list",
    )
    node_by_ref = {node["node_ref"]: node for node in active_snapshot["nodes"]}
    edge_by_ref = {edge["edge_ref"]: edge for edge in active_snapshot["edges"]}
    axis_by_id = {
        axis["axis_id"]: axis
        for axis in case["subject_decomposition"]["root"]["axes"]
    }
    abstraction_context = {
        "problem_framing": case["problem_framing"],
        "downward_route": [
            node_by_ref[node_ref] for node_ref in case["downward_route"]
        ],
        "ascent_assessments": case["ascent_assessments"],
    }
    work_by_id = {item["work_id"]: item for item in case["work_items"]}
    bindings: dict[str, dict[str, Any]] = {}
    for index, raw_binding in enumerate(work_bindings):
        binding = require_object(raw_binding, f"work_bindings[{index}]")
        work_id = _identifier(binding.get("work_id"), f"work_bindings[{index}].work_id")
        require(
            work_id in work_by_id,
            "invalid_topology",
            "A work binding refers to an unknown FI composition invocation",
            work_id=work_id,
        )
        require(
            work_id not in bindings,
            "invalid_topology",
            "An FI work item can be bound at most once",
            work_id=work_id,
        )
        normalized = {
            "work_id": work_id,
            "objective": _bounded_string(
                binding.get("objective"),
                f"work_bindings[{index}].objective",
                maximum=8_000,
            ),
            "context": _json_copy(
                require_object(
                    binding.get("context", {}), f"work_bindings[{index}].context"
                )
            ),
            "output_schema": _json_copy(
                require_object(
                    binding.get("output_schema"),
                    f"work_bindings[{index}].output_schema",
                )
            ),
            "accept_spec": _json_copy(
                require_object(
                    binding.get("accept_spec"),
                    f"work_bindings[{index}].accept_spec",
                )
            ),
        }
        require(
            canonical_json(binding) == canonical_json(normalized),
            "invalid_topology",
            "A work binding cannot override reviewed identity, role, or dependencies",
        )
        bindings[work_id] = normalized
    missing = [work_id for work_id in work_by_id if work_id not in bindings]
    require(
        not missing,
        "invalid_topology",
        "Every reviewed FI work item requires one execution binding",
        missing_work_ids=missing,
    )
    tasks = []
    source_bindings = []
    for work_item in case["work_items"]:
        binding = bindings[work_item["work_id"]]
        parent = node_by_ref[work_item["parent_ref"]]
        child = node_by_ref[work_item["node_ref"]]
        topology_edge = edge_by_ref[work_item["edge_ref"]]
        axis = axis_by_id[work_item["axis_id"]]
        tasks.append(
            {
                "task_id": work_item["work_id"],
                "source_axis_id": work_item["axis_id"],
                "role": work_item["role"],
                "objective": (
                    f"{topology_edge['role']}. Produce the reviewed contribution "
                    f"under the attached immutable contract. {binding['objective']}"
                ),
                "context": {
                    "fi_contract": {
                        "abstraction": abstraction_context,
                        "axis": {
                            "axis_id": axis["axis_id"],
                            "candidate": axis["candidate"],
                        },
                        "parent": parent,
                        "child": child,
                        "edge": topology_edge,
                    },
                    "operational_context": binding["context"],
                },
                "depends_on": work_item["depends_on"],
                "output_schema": binding["output_schema"],
                "accept_spec": binding["accept_spec"],
            }
        )
        source_bindings.append(
            {
                "work_id": work_item["work_id"],
                "axis_id": work_item["axis_id"],
                "parent_ref": work_item["parent_ref"],
                "node_ref": work_item["node_ref"],
                "edge_ref": work_item["edge_ref"],
            }
        )
    synthesis_binding = require_object(synthesis, "synthesis")
    checked_synthesis_binding = {
        "context": _json_copy(
            require_object(synthesis_binding.get("context", {}), "synthesis.context")
        ),
        "output_schema": _json_copy(
            require_object(
                synthesis_binding.get("output_schema"), "synthesis.output_schema"
            )
        ),
        "accept_spec": _json_copy(
            require_object(
                synthesis_binding.get("accept_spec"), "synthesis.accept_spec"
            )
        ),
    }
    require(
        canonical_json(synthesis_binding) == canonical_json(checked_synthesis_binding),
        "invalid_topology",
        "FI synthesis objective and conceptual context come from the reviewed case",
    )
    reviewed_synthesis = case["parent_synthesis"]
    return create_execution_plan(
        case["problem"],
        tasks,
        {
            "objective": (
                f"Realize this reviewed outward capability: "
                f"{reviewed_synthesis['outward_capability']} Integration: "
                f"{reviewed_synthesis['integration']}"
            ),
            "context": {
                "fi_abstraction": abstraction_context,
                "fi_parent_synthesis": reviewed_synthesis,
                "operational_context": checked_synthesis_binding["context"],
            },
            "output_schema": checked_synthesis_binding["output_schema"],
            "accept_spec": checked_synthesis_binding["accept_spec"],
        },
        source={
            "type": "fractal_intelligence_case",
            "case_proposal_digest": case["case_proposal_digest"],
            "graph_snapshot_digest": active_snapshot["snapshot_digest"],
            "assessment_boundary": case["assessment_boundary"],
            "review": checked_review,
            "review_scope": "topology_route_contracts_and_parent_synthesis",
            "planning_scope": "reviewed_topology_with_operational_bindings",
            "bindings": source_bindings,
            "abstraction_route": case["downward_route"],
            "planning_limitations": [
                "semantic_abstraction_and_contract_fit_remain_reviewed_estimates",
                "specialization_nodes_are_nonexecuting_routing_context",
                "recursive_subject_axes_are_not_expanded_by_this_compiler",
                "case_review_does_not_cover_operational_objectives_context_schemas_or_gates",
                "a_separate_final_plan_review_is_required_for_authoritative_use",
                "execution_accept_specs_remain_authoritative",
            ],
        },
        seed=seed,
    )
