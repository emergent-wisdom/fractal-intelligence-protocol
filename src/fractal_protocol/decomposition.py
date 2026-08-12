from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Decimal,
    DecimalException,
    InvalidOperation,
    localcontext,
)
from typing import Any, Callable, Protocol

from .errors import DomainError, require
from .protocol import canonical_json, content_digest, require_object, require_string


PROPOSAL_KIND = "conceptual_decomposition_proposal"
ASSESSMENT_BOUNDARY = "model_judgments_are_estimates_not_proof"
ALGORITHM_ID = "fractal_conceptual_decomposition"
ALGORITHM_VERSION = "1"
MAX_PROPOSAL_BYTES = 512 * 1024
MAX_DECIMAL_DIGITS = 100
MAX_DECIMAL_ABS_EXPONENT = 1_000
DECIMAL_CONTEXT_PRECISION = 200
_NODE_STATUSES = {"structurally_clean", "warning", "incomplete", "budget_exhausted"}
_NO_VALUE = object()


@dataclass(frozen=True)
class DecompositionConfig:
    """Hard limits for one conceptual-decomposition run.

    ``max_depth`` counts the root as depth zero. The defaults follow the paper's
    three completeness retries and three-task routing sanity check while keeping
    an experimental run bounded.
    """

    candidate_limit: int = 8
    completeness_retries: int = 3
    routing_probe_count: int = 3
    marginal_value_threshold: float = 0.25
    marginal_value_value_unit: str = "normalized_outcome_value"
    marginal_value_cost_unit: str = "normalized_compute_cost"
    max_depth: int = 2
    max_nodes: int = 64
    max_oracle_calls: int = 128

    def normalized(self) -> dict[str, Any]:
        require(
            isinstance(self.candidate_limit, int)
            and not isinstance(self.candidate_limit, bool)
            and 1 <= self.candidate_limit <= 50,
            "invalid_decomposition_config",
            "candidate_limit must be between 1 and 50",
        )
        require(
            isinstance(self.completeness_retries, int)
            and not isinstance(self.completeness_retries, bool)
            and 0 <= self.completeness_retries <= 10,
            "invalid_decomposition_config",
            "completeness_retries must be between 0 and 10",
        )
        require(
            isinstance(self.routing_probe_count, int)
            and not isinstance(self.routing_probe_count, bool)
            and 1 <= self.routing_probe_count <= 20,
            "invalid_decomposition_config",
            "routing_probe_count must be between 1 and 20",
        )
        threshold_number: float | None = None
        if isinstance(self.marginal_value_threshold, (int, float)) and not isinstance(
            self.marginal_value_threshold, bool
        ):
            try:
                threshold_number = float(self.marginal_value_threshold)
            except (OverflowError, ValueError):
                threshold_number = None
        valid_threshold = (
            threshold_number is not None
            and math.isfinite(threshold_number)
            and threshold_number >= 0
        )
        require(
            valid_threshold,
            "invalid_decomposition_config",
            "marginal_value_threshold must be finite and non-negative",
        )
        for field, value in (
            ("marginal_value_value_unit", self.marginal_value_value_unit),
            ("marginal_value_cost_unit", self.marginal_value_cost_unit),
        ):
            require(
                isinstance(value, str) and bool(value.strip()) and len(value) <= 200,
                "invalid_decomposition_config",
                f"{field} must be a non-empty string of at most 200 characters",
            )
        require(
            isinstance(self.max_depth, int)
            and not isinstance(self.max_depth, bool)
            and 0 <= self.max_depth <= 10,
            "invalid_decomposition_config",
            "max_depth must be between 0 and 10",
        )
        require(
            isinstance(self.max_nodes, int)
            and not isinstance(self.max_nodes, bool)
            and 1 <= self.max_nodes <= 1_000,
            "invalid_decomposition_config",
            "max_nodes must be between 1 and 1000",
        )
        require(
            isinstance(self.max_oracle_calls, int)
            and not isinstance(self.max_oracle_calls, bool)
            and 1 <= self.max_oracle_calls <= 10_000,
            "invalid_decomposition_config",
            "max_oracle_calls must be between 1 and 10000",
        )
        normalized = asdict(self)
        normalized["marginal_value_threshold"] = threshold_number
        return normalized


class DecompositionOracle(Protocol):
    """Model adapter used by :class:`ConceptualDecompositionEngine`.

    A production adapter may call an LLM, a human-review queue, or an ensemble.
    Keeping this boundary model-neutral lets the same control flow and harness
    compare providers without embedding credentials or vendor SDKs in the core.
    """

    identity: dict[str, Any]

    def generate_candidates(
        self, concept: dict[str, Any], *, limit: int
    ) -> list[dict[str, Any]]: ...

    def evaluate_candidate(
        self,
        concept: dict[str, Any],
        candidate: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def evaluate_completeness(
        self, concept: dict[str, Any], accepted_axes: list[dict[str, Any]]
    ) -> dict[str, Any]: ...

    def search_missing_dimension(
        self,
        concept: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
        *,
        missing_dimension: str | None,
    ) -> dict[str, Any] | None: ...

    def generate_routing_probes(
        self, concept: dict[str, Any], *, count: int
    ) -> list[dict[str, Any]]: ...

    def route_probe(
        self,
        concept: dict[str, Any],
        probe: dict[str, Any],
        accepted_axes: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def estimate_marginal_value(
        self, concept: dict[str, Any], axis: dict[str, Any], *, depth: int
    ) -> dict[str, Any]: ...


class RoutingProbeProvider(Protocol):
    provider_id: str

    def __call__(
        self, concept: dict[str, Any], *, depth: int, path: str, count: int
    ) -> list[dict[str, Any]]: ...


def _bounded_string(value: Any, field: str, *, maximum: int = 8_000) -> str:
    checked = require_string(value, field)
    require(
        len(checked) <= maximum,
        "invalid_decomposition",
        f"{field} is too long",
        field=field,
        maximum=maximum,
    )
    return checked


def _normalize_context(value: Any, field: str) -> dict[str, Any]:
    checked = require_object(value, field)
    canonical = canonical_json(checked)
    encoded = canonical.encode("utf-8")
    require(
        len(encoded) <= 64 * 1024,
        "invalid_decomposition",
        f"{field} is too large",
        field=field,
    )
    return json.loads(canonical)


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def normalize_concept(value: Any, field: str = "concept") -> dict[str, Any]:
    concept = require_object(value, field)
    return {
        "name": _bounded_string(concept.get("name"), f"{field}.name", maximum=500),
        "description": _bounded_string(
            concept.get("description"), f"{field}.description"
        ),
        "context": _normalize_context(concept.get("context", {}), f"{field}.context"),
    }


def normalize_candidate(value: Any, field: str = "candidate") -> dict[str, Any]:
    candidate = require_object(value, field)
    bounds = candidate.get("bounds", [])
    not_in_scope = candidate.get("not_in_scope", [])
    require(
        isinstance(bounds, list) and len(bounds) <= 50,
        "invalid_decomposition",
        f"{field}.bounds must be a list with at most 50 items",
    )
    require(
        isinstance(not_in_scope, list) and len(not_in_scope) <= 50,
        "invalid_decomposition",
        f"{field}.not_in_scope must be a list with at most 50 items",
    )
    return {
        "name": _bounded_string(candidate.get("name"), f"{field}.name", maximum=500),
        "description": _bounded_string(
            candidate.get("description"), f"{field}.description"
        ),
        "bounds": [
            _bounded_string(item, f"{field}.bounds[]", maximum=1_000)
            for item in bounds
        ],
        "not_in_scope": [
            _bounded_string(item, f"{field}.not_in_scope[]", maximum=1_000)
            for item in not_in_scope
        ],
    }


def _normalize_judgment(value: Any, field: str) -> dict[str, Any]:
    judgment = require_object(value, field)
    passed = judgment.get("passed")
    require(
        isinstance(passed, bool),
        "invalid_decomposition",
        f"{field}.passed must be boolean",
    )
    return {
        "passed": passed,
        "rationale": _bounded_string(
            judgment.get("rationale"), f"{field}.rationale"
        ),
    }


def _normalize_candidate_assessment(value: Any, field: str) -> dict[str, Any]:
    assessment = require_object(value, field)
    normalized = {
        name: _normalize_judgment(assessment.get(name), f"{field}.{name}")
        for name in ("necessity", "independence", "universality")
    }
    normalized["passed"] = all(
        normalized[name]["passed"]
        for name in ("necessity", "independence", "universality")
    )
    return normalized


def _normalize_completeness(value: Any, field: str) -> dict[str, Any]:
    assessment = require_object(value, field)
    passed = assessment.get("passed")
    require(
        isinstance(passed, bool),
        "invalid_decomposition",
        f"{field}.passed must be boolean",
    )
    missing = assessment.get("missing_dimension")
    if missing is not None:
        missing = _bounded_string(missing, f"{field}.missing_dimension")
    return {
        "passed": passed,
        "rationale": _bounded_string(
            assessment.get("rationale"), f"{field}.rationale"
        ),
        "missing_dimension": missing,
    }


def _normalize_probe(value: Any, field: str) -> dict[str, Any]:
    probe = require_object(value, field)
    return {
        "probe_id": _bounded_string(
            probe.get("probe_id"), f"{field}.probe_id", maximum=200
        ),
        "task": _bounded_string(probe.get("task"), f"{field}.task"),
    }


def _probe_task_key(probe: dict[str, Any]) -> str:
    return " ".join(probe["task"].casefold().split())


def _normalize_route(value: Any, field: str, axis_ids: set[str]) -> dict[str, Any]:
    route = require_object(value, field)
    routed = route.get("axis_ids")
    require(
        isinstance(routed, list),
        "invalid_decomposition",
        f"{field}.axis_ids must be a list",
    )
    checked = [
        _bounded_string(item, f"{field}.axis_ids[]", maximum=100) for item in routed
    ]
    require(
        len(set(checked)) == len(checked),
        "invalid_decomposition",
        f"{field}.axis_ids must be unique",
    )
    require(
        set(checked) <= axis_ids,
        "invalid_decomposition",
        f"{field}.axis_ids contains an unknown axis",
    )
    return {
        "axis_ids": checked,
        "rationale": _bounded_string(route.get("rationale"), f"{field}.rationale"),
        "clean": len(checked) == 1,
    }


def _decimal(value: Any, field: str, *, nonnegative: bool = True) -> Decimal:
    require(
        not isinstance(value, bool) and isinstance(value, (str, int, float)),
        "invalid_decomposition",
        f"{field} must be a decimal number",
    )
    try:
        checked = Decimal(str(value))
    except InvalidOperation as exc:
        raise DomainError(
            "invalid_decomposition",
            f"{field} must be a decimal number",
        ) from exc
    require(
        checked.is_finite() and (not nonnegative or checked >= 0),
        "invalid_decomposition",
        f"{field} must be finite" + (" and non-negative" if nonnegative else ""),
    )
    require(
        len(checked.as_tuple().digits) <= MAX_DECIMAL_DIGITS
        and abs(checked.adjusted()) <= MAX_DECIMAL_ABS_EXPONENT,
        "invalid_decomposition",
        f"{field} exceeds the supported decimal precision or exponent",
    )
    return checked


def _decimal_string(value: Decimal) -> str:
    try:
        require(
            value.is_finite()
            and len(value.as_tuple().digits) <= DECIMAL_CONTEXT_PRECISION
            and abs(value.adjusted()) <= 2 * MAX_DECIMAL_ABS_EXPONENT + 10,
            "invalid_decomposition",
            "A derived marginal-value number exceeds supported decimal bounds",
        )
        with localcontext() as context:
            context.prec = DECIMAL_CONTEXT_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return format(value.normalize(context=context), "f")
    except DecimalException as exc:
        raise DomainError(
            "invalid_decomposition",
            "Marginal-value decimal normalization failed",
            details={"reason": type(exc).__name__},
        ) from exc


def _normalize_marginal_value(
    value: Any,
    field: str,
    *,
    threshold: float,
    value_unit: str,
    cost_unit: str,
) -> dict[str, Any]:
    """Calculate the paper's marginal-value rule from supplied evidence."""

    estimate = require_object(value, field)
    evidence_status = estimate.get("evidence_status", "sufficient")
    require(
        evidence_status in {"sufficient", "insufficient"},
        "invalid_decomposition",
        f"{field}.evidence_status must be sufficient or insufficient",
    )
    basis = _bounded_string(estimate.get("basis"), f"{field}.basis", maximum=500)
    rationale = _bounded_string(
        estimate.get("rationale"), f"{field}.rationale"
    )
    threshold_decimal = _decimal(threshold, "marginal_value_threshold")
    if evidence_status == "insufficient":
        return {
            "evidence_status": "insufficient",
            "basis": basis,
            "rationale": rationale,
            "sample_count": 0,
            "value_unit": value_unit,
            "cost_unit": cost_unit,
            "marginal_ratio": None,
            "threshold": _decimal_string(threshold_decimal),
            "mvr_decision": "probe",
        }

    sample_count = estimate.get("sample_count")
    require(
        isinstance(sample_count, int)
        and not isinstance(sample_count, bool)
        and sample_count >= 1,
        "invalid_decomposition",
        f"{field}.sample_count must be a positive integer",
    )
    shallow_value = _decimal(estimate.get("shallow_value"), f"{field}.shallow_value")
    deep_value = _decimal(estimate.get("deep_value"), f"{field}.deep_value")
    exploration_credit = _decimal(
        estimate.get("exploration_credit", 0), f"{field}.exploration_credit"
    )
    shallow_cost = _decimal(estimate.get("shallow_cost"), f"{field}.shallow_cost")
    deep_cost = _decimal(estimate.get("deep_cost"), f"{field}.deep_cost")
    uncertainty = _decimal(estimate.get("uncertainty", 0), f"{field}.uncertainty")
    try:
        with localcontext() as context:
            context.prec = DECIMAL_CONTEXT_PRECISION
            context.rounding = ROUND_HALF_EVEN
            incremental_cost = deep_cost - shallow_cost
            numerator = deep_value - shallow_value + exploration_credit
            ratio = (
                numerator / incremental_cost
                if incremental_cost != 0
                else Decimal(0)
            )
    except DecimalException as exc:
        raise DomainError(
            "invalid_decomposition",
            "Marginal-value arithmetic failed",
            details={"reason": type(exc).__name__},
        ) from exc
    require(
        incremental_cost > 0,
        "invalid_decomposition",
        f"{field}.deep_cost must be greater than shallow_cost",
    )
    supplied_value_unit = _bounded_string(
        estimate.get("value_unit"), f"{field}.value_unit", maximum=200
    )
    supplied_cost_unit = _bounded_string(
        estimate.get("cost_unit"), f"{field}.cost_unit", maximum=200
    )
    require(
        supplied_value_unit == value_unit and supplied_cost_unit == cost_unit,
        "invalid_decomposition",
        f"{field} units must match the decomposition policy",
        expected_value_unit=value_unit,
        expected_cost_unit=cost_unit,
    )
    return {
        "evidence_status": "sufficient",
        "basis": basis,
        "rationale": rationale,
        "sample_count": sample_count,
        "value_unit": supplied_value_unit,
        "cost_unit": supplied_cost_unit,
        "shallow_value": _decimal_string(shallow_value),
        "deep_value": _decimal_string(deep_value),
        "exploration_credit": _decimal_string(exploration_credit),
        "shallow_cost": _decimal_string(shallow_cost),
        "deep_cost": _decimal_string(deep_cost),
        "uncertainty": _decimal_string(uncertainty),
        "marginal_ratio": _decimal_string(ratio),
        "threshold": _decimal_string(threshold_decimal),
        "mvr_decision": "decompose" if ratio > threshold_decimal else "leaf",
    }


def _normalize_oracle_identity(value: Any) -> dict[str, Any]:
    identity = require_object(value, "oracle.identity")
    return {
        "adapter_id": _bounded_string(
            identity.get("adapter_id"), "oracle.identity.adapter_id", maximum=500
        ),
        "adapter_version": _bounded_string(
            identity.get("adapter_version"),
            "oracle.identity.adapter_version",
            maximum=200,
        ),
        "generator": _bounded_string(
            identity.get("generator"), "oracle.identity.generator", maximum=500
        ),
        "judge": _bounded_string(
            identity.get("judge"), "oracle.identity.judge", maximum=500
        ),
        "router": _bounded_string(
            identity.get("router"), "oracle.identity.router", maximum=500
        ),
        "marginal_value_source": _bounded_string(
            identity.get("marginal_value_source"),
            "oracle.identity.marginal_value_source",
            maximum=500,
        ),
    }


class ConceptualDecompositionEngine:
    """Execute the paper's first conceptual-decomposition algorithm.

    The engine owns deterministic control flow and hard limits. The oracle owns
    semantic judgments. Nothing produced here is allowed to authorize payment;
    the result is deliberately a review-required proposal.
    """

    def __init__(
        self,
        oracle: DecompositionOracle,
        *,
        config: DecompositionConfig | None = None,
    ) -> None:
        self.oracle = oracle
        self.config = config or DecompositionConfig()
        self._policy: dict[str, Any] = {}
        self._oracle_calls = 0
        self._node_count = 0
        self._warnings: list[str] = []
        self._budget_exhausted = False
        self._root_routing_probes: list[dict[str, Any]] | None = None
        self._routing_probe_provider: RoutingProbeProvider | None = None
        self._routing_probe_provider_id: str | None = None

    def decompose(
        self,
        concept: Any,
        *,
        routing_probes: Any | None = None,
        routing_probe_provider: RoutingProbeProvider | None = None,
    ) -> dict[str, Any]:
        self._policy = self.config.normalized()
        self._oracle_calls = 0
        self._node_count = 0
        self._warnings = []
        self._budget_exhausted = False
        self._root_routing_probes = None
        self._routing_probe_provider = None
        self._routing_probe_provider_id = None
        require(
            routing_probes is None or routing_probe_provider is None,
            "invalid_decomposition",
            "Use either routing_probes or routing_probe_provider, not both",
        )
        if routing_probes is not None:
            require(
                isinstance(routing_probes, list)
                and len(routing_probes) == self._policy["routing_probe_count"],
                "invalid_decomposition",
                "A supplied routing probe set must exactly match routing_probe_count",
            )
            self._root_routing_probes = [
                _normalize_probe(item, f"routing_probes[{index}]")
                for index, item in enumerate(routing_probes)
            ]
            require(
                len({item["probe_id"] for item in self._root_routing_probes})
                == len(self._root_routing_probes),
                "invalid_decomposition",
                "Supplied routing probe ids must be unique",
            )
            require(
                len({_probe_task_key(item) for item in self._root_routing_probes})
                == len(self._root_routing_probes),
                "invalid_decomposition",
                "Supplied routing probe tasks must be distinct",
            )
            self._routing_probe_provider_id = "caller_supplied_root_probe_set"
        if routing_probe_provider is not None:
            self._routing_probe_provider = routing_probe_provider
            self._routing_probe_provider_id = _bounded_string(
                getattr(routing_probe_provider, "provider_id", None),
                "routing_probe_provider.provider_id",
                maximum=500,
            )
        normalized_concept = normalize_concept(concept)
        oracle_identity = _normalize_oracle_identity(
            getattr(self.oracle, "identity", None)
        )
        root = self._decompose_node(normalized_concept, depth=0, path="root")
        core = {
            "protocol_version": "1",
            "kind": PROPOSAL_KIND,
            "status": "review_required",
            "assessment_boundary": ASSESSMENT_BOUNDARY,
            "provenance": {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "oracle": oracle_identity,
                "routing_probe_provider_id": self._routing_probe_provider_id,
            },
            "policy": self._policy,
            "root": root,
            "usage": {
                "oracle_calls": self._oracle_calls,
                "decomposition_nodes": self._node_count,
                "budget_exhausted": self._budget_exhausted,
            },
            "warnings": list(dict.fromkeys(self._warnings)),
        }
        proposal = {**core, "proposal_digest": content_digest(core)}
        return validate_decomposition_proposal(proposal)

    def _invoke(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        if self._oracle_calls >= self._policy["max_oracle_calls"]:
            self._budget_exhausted = True
            self._warnings.append("oracle_call_budget_exhausted")
            return _NO_VALUE
        self._oracle_calls += 1
        method = getattr(self.oracle, method_name)
        safe_args = tuple(
            _json_copy(value) if isinstance(value, (dict, list)) else value
            for value in args
        )
        safe_kwargs = {
            key: _json_copy(value) if isinstance(value, (dict, list)) else value
            for key, value in kwargs.items()
        }
        return method(*safe_args, **safe_kwargs)

    def _axis_id(self, path: str, candidate: dict[str, Any]) -> str:
        digest = content_digest({"path": path, "candidate": candidate})
        return f"axis_{digest.removeprefix('sha256:')[:16]}"

    @staticmethod
    def _axis_for_oracle(axis: dict[str, Any]) -> dict[str, Any]:
        return {
            "axis_id": axis["axis_id"],
            **axis["candidate"],
        }

    def _assess_candidate(
        self,
        concept: dict[str, Any],
        candidate: dict[str, Any],
        axes: list[dict[str, Any]],
        *,
        field: str,
    ) -> dict[str, Any] | None:
        raw = self._invoke(
            "evaluate_candidate",
            concept,
            candidate,
            [self._axis_for_oracle(axis) for axis in axes],
        )
        if raw is _NO_VALUE:
            return None
        return _normalize_candidate_assessment(raw, field)

    def _decompose_node(
        self, concept: dict[str, Any], *, depth: int, path: str
    ) -> dict[str, Any]:
        self._node_count += 1
        axes: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        completeness_checks: list[dict[str, Any]] = []
        routing_probes: list[dict[str, Any]] = []
        node_warnings: list[str] = []

        raw_candidates = self._invoke(
            "generate_candidates", concept, limit=self._policy["candidate_limit"]
        )
        candidates: list[dict[str, Any]] = []
        if raw_candidates is _NO_VALUE:
            node_warnings.append("candidate_generation_skipped_by_budget")
        else:
            require(
                isinstance(raw_candidates, list),
                "invalid_decomposition_oracle",
                "generate_candidates must return a list",
            )
            require(
                len(raw_candidates) <= self._policy["candidate_limit"],
                "invalid_decomposition_oracle",
                "generate_candidates exceeded candidate_limit",
            )
            seen: set[str] = set()
            for index, raw in enumerate(raw_candidates):
                candidate = normalize_candidate(raw, f"candidates[{index}]")
                fingerprint = canonical_json(candidate)
                if fingerprint in seen:
                    node_warnings.append("duplicate_candidate_ignored")
                    continue
                seen.add(fingerprint)
                candidates.append(candidate)

        for index, candidate in enumerate(candidates):
            assessment = self._assess_candidate(
                concept,
                candidate,
                axes,
                field=f"candidate_assessments[{index}]",
            )
            if assessment is None:
                node_warnings.append("candidate_assessment_skipped_by_budget")
                break
            record = {"candidate": candidate, "assessment": assessment}
            if assessment["passed"]:
                record = {
                    "axis_id": self._axis_id(path, candidate),
                    **record,
                    "marginal_value": None,
                    "decomposition": None,
                }
                axes.append(record)
            else:
                rejected.append(record)

        raw_complete = self._invoke(
            "evaluate_completeness",
            concept,
            [self._axis_for_oracle(axis) for axis in axes],
        )
        if raw_complete is not _NO_VALUE:
            completeness_checks.append(
                {
                    "attempt": 0,
                    **_normalize_completeness(raw_complete, "completeness[0]"),
                }
            )
        else:
            node_warnings.append("completeness_check_skipped_by_budget")

        retries = 0
        while (
            completeness_checks
            and not completeness_checks[-1]["passed"]
            and retries < self._policy["completeness_retries"]
        ):
            retries += 1
            missing = completeness_checks[-1]["missing_dimension"]
            raw_missing = self._invoke(
                "search_missing_dimension",
                concept,
                [self._axis_for_oracle(axis) for axis in axes],
                missing_dimension=missing,
            )
            if raw_missing is _NO_VALUE:
                node_warnings.append("completeness_search_skipped_by_budget")
                break
            if raw_missing is not None:
                candidate = normalize_candidate(
                    raw_missing, f"completeness_candidates[{retries}]"
                )
                if canonical_json(candidate) not in {
                    canonical_json(axis["candidate"]) for axis in axes
                }:
                    assessment = self._assess_candidate(
                        concept,
                        candidate,
                        axes,
                        field=f"completeness_assessments[{retries}]",
                    )
                    if assessment is None:
                        node_warnings.append(
                            "completeness_candidate_assessment_skipped_by_budget"
                        )
                        break
                    record = {"candidate": candidate, "assessment": assessment}
                    if assessment["passed"]:
                        axes.append(
                            {
                                "axis_id": self._axis_id(
                                    f"{path}.completeness.{retries}", candidate
                                ),
                                **record,
                                "marginal_value": None,
                                "decomposition": None,
                            }
                        )
                    else:
                        rejected.append(record)
                else:
                    node_warnings.append("duplicate_completeness_candidate_ignored")

            raw_complete = self._invoke(
                "evaluate_completeness",
                concept,
                [self._axis_for_oracle(axis) for axis in axes],
            )
            if raw_complete is _NO_VALUE:
                node_warnings.append("completeness_check_skipped_by_budget")
                break
            completeness_checks.append(
                {
                    "attempt": retries,
                    **_normalize_completeness(
                        raw_complete, f"completeness[{retries}]"
                    ),
                }
            )

        completeness_passed = bool(
            completeness_checks and completeness_checks[-1]["passed"]
        )
        if not completeness_passed:
            node_warnings.append("completeness_not_established")

        if axes:
            if self._routing_probe_provider is not None:
                raw_probes = self._routing_probe_provider(
                    _json_copy(concept),
                    depth=depth,
                    path=path,
                    count=self._policy["routing_probe_count"],
                )
                probe_source = "provided"
            elif depth == 0 and self._root_routing_probes is not None:
                raw_probes: Any = self._root_routing_probes
                probe_source = "provided"
            else:
                raw_probes = self._invoke(
                    "generate_routing_probes",
                    concept,
                    count=self._policy["routing_probe_count"],
                )
                probe_source = "oracle_generated"
            if raw_probes is _NO_VALUE:
                node_warnings.append("routing_probes_skipped_by_budget")
            else:
                require(
                    isinstance(raw_probes, list),
                    "invalid_decomposition_oracle",
                    "generate_routing_probes must return a list",
                )
                require(
                    len(raw_probes) <= self._policy["routing_probe_count"],
                    "invalid_decomposition_oracle",
                    "generate_routing_probes exceeded routing_probe_count",
                )
                if len(raw_probes) != self._policy["routing_probe_count"]:
                    node_warnings.append("routing_probe_count_incomplete")
                axis_ids = {axis["axis_id"] for axis in axes}
                seen_probe_ids: set[str] = set()
                seen_probe_tasks: set[str] = set()
                for index, raw_probe in enumerate(raw_probes):
                    probe = _normalize_probe(raw_probe, f"routing_probes[{index}]")
                    require(
                        probe["probe_id"] not in seen_probe_ids,
                        "invalid_decomposition_oracle",
                        "Routing probe ids must be unique",
                    )
                    seen_probe_ids.add(probe["probe_id"])
                    task_key = _probe_task_key(probe)
                    require(
                        task_key not in seen_probe_tasks,
                        "invalid_decomposition_oracle",
                        "Routing probe tasks must be distinct",
                    )
                    seen_probe_tasks.add(task_key)
                    raw_route = self._invoke(
                        "route_probe",
                        concept,
                        probe,
                        [self._axis_for_oracle(axis) for axis in axes],
                    )
                    if raw_route is _NO_VALUE:
                        node_warnings.append("probe_routing_skipped_by_budget")
                        break
                    route = _normalize_route(
                        raw_route, f"routing_results[{index}]", axis_ids
                    )
                    routing_probes.append({**probe, **route})
        else:
            probe_source = (
                "provided"
                if self._routing_probe_provider is not None
                or (depth == 0 and self._root_routing_probes is not None)
                else "oracle_generated"
            )
            node_warnings.append("routing_not_possible_without_axes")

        routing_passed = (
            len(routing_probes) == self._policy["routing_probe_count"]
            and all(probe["clean"] for probe in routing_probes)
        )
        if not routing_passed:
            node_warnings.append("routing_sanity_warning")

        if completeness_passed:
            recursion_candidates: list[dict[str, Any]] = []
            for axis_index, axis in enumerate(axes):
                raw_marginal = self._invoke(
                    "estimate_marginal_value",
                    concept,
                    self._axis_for_oracle(axis),
                    depth=depth,
                )
                if raw_marginal is _NO_VALUE:
                    node_warnings.append("marginal_value_skipped_by_budget")
                    break
                estimate = _normalize_marginal_value(
                    raw_marginal,
                    f"marginal_values[{axis_index}]",
                    threshold=self._policy["marginal_value_threshold"],
                    value_unit=self._policy["marginal_value_value_unit"],
                    cost_unit=self._policy["marginal_value_cost_unit"],
                )
                if estimate["mvr_decision"] == "probe":
                    recursion_decision = "probe"
                elif estimate["mvr_decision"] == "leaf":
                    recursion_decision = "leaf"
                elif depth >= self._policy["max_depth"]:
                    recursion_decision = "maximum_depth"
                else:
                    recursion_decision = "pending_allocation"
                axis["marginal_value"] = {
                    **estimate,
                    "recursion_decision": recursion_decision,
                }
                if recursion_decision == "pending_allocation":
                    recursion_candidates.append(axis)

            # Spend the shared node budget on the highest supplied marginal
            # ratios first rather than allowing generator order to choose depth.
            recursion_candidates.sort(
                key=lambda item: (
                    -Decimal(item["marginal_value"]["marginal_ratio"]),
                    item["axis_id"],
                )
            )
            for axis in recursion_candidates:
                if self._budget_exhausted:
                    axis["marginal_value"]["recursion_decision"] = (
                        "oracle_budget_exhausted"
                    )
                    node_warnings.append("oracle_budget_prevented_recursion")
                    continue
                if self._node_count >= self._policy["max_nodes"]:
                    axis["marginal_value"]["recursion_decision"] = (
                        "node_budget_exhausted"
                    )
                    node_warnings.append("node_budget_prevented_recursion")
                    continue
                axis["marginal_value"]["recursion_decision"] = "recurse"
                child_concept = {
                    "name": axis["candidate"]["name"],
                    "description": axis["candidate"]["description"],
                    "context": {
                        "parent_concept": concept["name"],
                        "bounds": axis["candidate"]["bounds"],
                        "not_in_scope": axis["candidate"]["not_in_scope"],
                    },
                }
                axis["decomposition"] = self._decompose_node(
                    child_concept,
                    depth=depth + 1,
                    path=f"{path}.{axis['axis_id']}",
                )

        self._warnings.extend(node_warnings)
        if self._budget_exhausted:
            status = "budget_exhausted"
        elif not completeness_passed:
            status = "incomplete"
        elif not routing_passed:
            status = "warning"
        else:
            status = "structurally_clean"
        return {
            "depth": depth,
            "concept": concept,
            "status": status,
            "axes": axes,
            "rejected_candidates": rejected,
            "completeness": {
                "passed": completeness_passed,
                "checks": completeness_checks,
                "retries_used": retries,
            },
            "routing": {
                "passed": routing_passed,
                "probe_source": probe_source,
                "probes": routing_probes,
            },
            "warnings": list(dict.fromkeys(node_warnings)),
        }


def _validate_axis_id(value: Any, field: str) -> str:
    axis_id = _bounded_string(value, field, maximum=100)
    suffix = axis_id.removeprefix("axis_")
    require(
        axis_id.startswith("axis_")
        and len(suffix) == 16
        and all(character in "0123456789abcdef" for character in suffix),
        "invalid_decomposition",
        f"{field} must be an axis_ identifier with 16 lowercase hex characters",
    )
    return axis_id


def _normalize_node(
    value: Any,
    field: str,
    *,
    policy: dict[str, Any],
    seen_axis_ids: set[str],
) -> dict[str, Any]:
    node = require_object(value, field)
    depth = node.get("depth")
    require(
        isinstance(depth, int)
        and not isinstance(depth, bool)
        and 0 <= depth <= policy["max_depth"],
        "invalid_decomposition",
        f"{field}.depth is outside policy",
    )
    status = node.get("status")
    require(
        status in _NODE_STATUSES,
        "invalid_decomposition",
        f"{field}.status is unsupported",
    )
    axes = node.get("axes")
    rejected = node.get("rejected_candidates")
    require(isinstance(axes, list), "invalid_decomposition", f"{field}.axes must be a list")
    require(
        isinstance(rejected, list),
        "invalid_decomposition",
        f"{field}.rejected_candidates must be a list",
    )
    require(
        len(axes) <= policy["candidate_limit"] + policy["completeness_retries"],
        "invalid_decomposition",
        f"{field}.axes exceeds policy",
    )
    checked_axes: list[dict[str, Any]] = []
    for index, raw_axis in enumerate(axes):
        axis = require_object(raw_axis, f"{field}.axes[{index}]")
        axis_id = _validate_axis_id(
            axis.get("axis_id"), f"{field}.axes[{index}].axis_id"
        )
        require(
            axis_id not in seen_axis_ids,
            "invalid_decomposition",
            "axis_id values must be globally unique",
            axis_id=axis_id,
        )
        seen_axis_ids.add(axis_id)
        assessment = _normalize_candidate_assessment(
            axis.get("assessment"), f"{field}.axes[{index}].assessment"
        )
        require(
            assessment["passed"],
            "invalid_decomposition",
            "An accepted axis must pass necessity, independence, and universality",
            axis_id=axis_id,
        )
        marginal = axis.get("marginal_value")
        checked_marginal = None
        if marginal is not None:
            estimate = _normalize_marginal_value(
                marginal,
                f"{field}.axes[{index}].marginal_value",
                threshold=policy["marginal_value_threshold"],
                value_unit=policy["marginal_value_value_unit"],
                cost_unit=policy["marginal_value_cost_unit"],
            )
            require(
                all(
                    marginal.get(key) == estimate[key]
                    for key in ("marginal_ratio", "threshold", "mvr_decision")
                ),
                "invalid_decomposition",
                "Marginal-value derived fields do not match the supplied evidence",
            )
            recursion_decision = marginal.get("recursion_decision")
            require(
                recursion_decision
                in {
                    "recurse",
                    "probe",
                    "leaf",
                    "maximum_depth",
                    "node_budget_exhausted",
                    "oracle_budget_exhausted",
                },
                "invalid_decomposition",
                "Unsupported marginal-value recursion decision",
            )
            if recursion_decision == "probe":
                require(
                    estimate["mvr_decision"] == "probe",
                    "invalid_decomposition",
                    "A probe decision requires insufficient evidence",
                )
            elif recursion_decision == "leaf":
                require(
                    estimate["mvr_decision"] == "leaf",
                    "invalid_decomposition",
                    "A leaf decision must be below the marginal-value threshold",
                )
            else:
                require(
                    estimate["mvr_decision"] == "decompose",
                    "invalid_decomposition",
                    "A depth or recursion decision must be above the marginal-value threshold",
                )
            if recursion_decision == "maximum_depth":
                require(
                    depth >= policy["max_depth"],
                    "invalid_decomposition",
                    "maximum_depth is inconsistent with node depth",
                )
            checked_marginal = {**estimate, "recursion_decision": recursion_decision}
        child = axis.get("decomposition")
        checked_child = None
        if child is not None:
            require(
                checked_marginal is not None
                and checked_marginal["recursion_decision"] == "recurse",
                "invalid_decomposition",
                "A child decomposition requires a recurse decision",
            )
            checked_child = _normalize_node(
                child,
                f"{field}.axes[{index}].decomposition",
                policy=policy,
                seen_axis_ids=seen_axis_ids,
            )
            require(
                checked_child["depth"] == depth + 1,
                "invalid_decomposition",
                "Child decomposition depth must increment by one",
            )
        elif checked_marginal is not None:
            require(
                checked_marginal["recursion_decision"] != "recurse",
                "invalid_decomposition",
                "A recurse decision requires a child decomposition",
            )
        checked_axes.append(
            {
                "axis_id": axis_id,
                "candidate": normalize_candidate(
                    axis.get("candidate"), f"{field}.axes[{index}].candidate"
                ),
                "assessment": assessment,
                "marginal_value": checked_marginal,
                "decomposition": checked_child,
            }
        )

    checked_rejected: list[dict[str, Any]] = []
    for index, raw_rejected in enumerate(rejected):
        item = require_object(raw_rejected, f"{field}.rejected_candidates[{index}]")
        assessment = _normalize_candidate_assessment(
            item.get("assessment"),
            f"{field}.rejected_candidates[{index}].assessment",
        )
        require(
            not assessment["passed"],
            "invalid_decomposition",
            "A rejected candidate cannot pass all three tests",
        )
        checked_rejected.append(
            {
                "candidate": normalize_candidate(
                    item.get("candidate"),
                    f"{field}.rejected_candidates[{index}].candidate",
                ),
                "assessment": assessment,
            }
        )

    completeness = require_object(node.get("completeness"), f"{field}.completeness")
    checks = completeness.get("checks")
    require(
        isinstance(checks, list)
        and len(checks) <= policy["completeness_retries"] + 1,
        "invalid_decomposition",
        f"{field}.completeness.checks exceeds policy",
    )
    checked_checks = []
    for index, raw_check in enumerate(checks):
        check = require_object(raw_check, f"{field}.completeness.checks[{index}]")
        attempt = check.get("attempt")
        require(
            isinstance(attempt, int)
            and not isinstance(attempt, bool)
            and attempt == index,
            "invalid_decomposition",
            "Completeness attempts must be consecutive from zero",
        )
        checked_checks.append(
            {
                "attempt": attempt,
                **_normalize_completeness(
                    check, f"{field}.completeness.checks[{index}]"
                ),
            }
        )
    completeness_passed = completeness.get("passed")
    require(
        isinstance(completeness_passed, bool)
        and completeness_passed
        == bool(checked_checks and checked_checks[-1]["passed"]),
        "invalid_decomposition",
        "completeness.passed must match the final check",
    )
    retries_used = completeness.get("retries_used")
    require(
        isinstance(retries_used, int)
        and not isinstance(retries_used, bool)
        and 0 <= retries_used <= policy["completeness_retries"],
        "invalid_decomposition",
        "completeness.retries_used is outside policy",
    )

    routing = require_object(node.get("routing"), f"{field}.routing")
    probe_source = routing.get("probe_source")
    require(
        probe_source in {"provided", "oracle_generated"},
        "invalid_decomposition",
        f"{field}.routing.probe_source is unsupported",
    )
    probes = routing.get("probes")
    require(
        isinstance(probes, list) and len(probes) <= policy["routing_probe_count"],
        "invalid_decomposition",
        f"{field}.routing.probes exceeds policy",
    )
    local_axis_ids = {axis["axis_id"] for axis in checked_axes}
    checked_probes = []
    seen_probe_ids: set[str] = set()
    seen_probe_tasks: set[str] = set()
    for index, raw_probe in enumerate(probes):
        probe = _normalize_probe(raw_probe, f"{field}.routing.probes[{index}]")
        require(
            probe["probe_id"] not in seen_probe_ids,
            "invalid_decomposition",
            f"{field}.routing probe ids must be unique",
        )
        seen_probe_ids.add(probe["probe_id"])
        task_key = _probe_task_key(probe)
        require(
            task_key not in seen_probe_tasks,
            "invalid_decomposition",
            f"{field}.routing probe tasks must be distinct",
        )
        seen_probe_tasks.add(task_key)
        route = _normalize_route(
            raw_probe, f"{field}.routing.probes[{index}]", local_axis_ids
        )
        checked_probes.append({**probe, **route})
    routing_passed = routing.get("passed")
    derived_routing_passed = (
        len(checked_probes) == policy["routing_probe_count"]
        and all(probe["clean"] for probe in checked_probes)
    )
    require(
        isinstance(routing_passed, bool)
        and routing_passed == derived_routing_passed,
        "invalid_decomposition",
        "routing.passed must match the routing probes",
    )
    if status != "budget_exhausted":
        expected_status = (
            "incomplete"
            if not completeness_passed
            else "warning"
            if not routing_passed
            else "structurally_clean"
        )
        require(
            status == expected_status,
            "invalid_decomposition",
            f"{field}.status does not match completeness and routing evidence",
        )
    warnings = node.get("warnings")
    require(
        isinstance(warnings, list) and len(warnings) <= 100,
        "invalid_decomposition",
        f"{field}.warnings must be a bounded list",
    )
    checked_warnings = [
        _bounded_string(item, f"{field}.warnings[]", maximum=500) for item in warnings
    ]
    return {
        "depth": depth,
        "concept": normalize_concept(node.get("concept"), f"{field}.concept"),
        "status": status,
        "axes": checked_axes,
        "rejected_candidates": checked_rejected,
        "completeness": {
            "passed": completeness_passed,
            "checks": checked_checks,
            "retries_used": retries_used,
        },
        "routing": {
            "passed": routing_passed,
            "probe_source": probe_source,
            "probes": checked_probes,
        },
        "warnings": checked_warnings,
    }


def validate_decomposition_proposal(value: Any) -> dict[str, Any]:
    """Validate and normalize a review-only conceptual-decomposition artifact."""

    proposal = require_object(value, "decomposition")
    require(
        proposal.get("protocol_version") == "1"
        and proposal.get("kind") == PROPOSAL_KIND
        and proposal.get("status") == "review_required"
        and proposal.get("assessment_boundary") == ASSESSMENT_BOUNDARY,
        "invalid_decomposition",
        "Unsupported conceptual-decomposition proposal envelope",
    )
    provenance = require_object(
        proposal.get("provenance"), "decomposition.provenance"
    )
    require(
        provenance.get("algorithm_id") == ALGORITHM_ID
        and provenance.get("algorithm_version") == ALGORITHM_VERSION,
        "invalid_decomposition",
        "Unsupported decomposition algorithm provenance",
    )
    checked_provenance = {
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "oracle": _normalize_oracle_identity(provenance.get("oracle")),
        "routing_probe_provider_id": provenance.get("routing_probe_provider_id"),
    }
    if checked_provenance["routing_probe_provider_id"] is not None:
        checked_provenance["routing_probe_provider_id"] = _bounded_string(
            checked_provenance["routing_probe_provider_id"],
            "decomposition.provenance.routing_probe_provider_id",
            maximum=500,
        )
    raw_policy = require_object(proposal.get("policy"), "decomposition.policy")
    try:
        config = DecompositionConfig(**raw_policy)
    except TypeError as exc:
        raise DomainError(
            "invalid_decomposition",
            "decomposition.policy has unsupported fields",
            details={"reason": str(exc)},
        ) from exc
    policy = config.normalized()
    seen_axis_ids: set[str] = set()
    root = _normalize_node(
        proposal.get("root"),
        "decomposition.root",
        policy=policy,
        seen_axis_ids=seen_axis_ids,
    )
    require(
        root["depth"] == 0,
        "invalid_decomposition",
        "The root decomposition must have depth zero",
    )
    usage = require_object(proposal.get("usage"), "decomposition.usage")
    oracle_calls = usage.get("oracle_calls")
    node_count = usage.get("decomposition_nodes")
    budget_exhausted = usage.get("budget_exhausted")
    require(
        isinstance(oracle_calls, int)
        and not isinstance(oracle_calls, bool)
        and 0 <= oracle_calls <= policy["max_oracle_calls"],
        "invalid_decomposition",
        "usage.oracle_calls is outside policy",
    )
    require(
        isinstance(node_count, int)
        and not isinstance(node_count, bool)
        and node_count == _count_nodes(root)
        and node_count <= policy["max_nodes"],
        "invalid_decomposition",
        "usage.decomposition_nodes does not match the tree",
    )
    require(
        isinstance(budget_exhausted, bool),
        "invalid_decomposition",
        "usage.budget_exhausted must be boolean",
    )
    require(
        (root["status"] == "budget_exhausted") == budget_exhausted,
        "invalid_decomposition",
        "The root status must match usage.budget_exhausted",
    )
    warnings = proposal.get("warnings")
    require(
        isinstance(warnings, list) and len(warnings) <= 500,
        "invalid_decomposition",
        "decomposition.warnings must be a bounded list",
    )
    checked_warnings = [
        _bounded_string(item, "decomposition.warnings[]", maximum=500)
        for item in warnings
    ]
    core = {
        "protocol_version": "1",
        "kind": PROPOSAL_KIND,
        "status": "review_required",
        "assessment_boundary": ASSESSMENT_BOUNDARY,
        "provenance": checked_provenance,
        "policy": policy,
        "root": root,
        "usage": {
            "oracle_calls": oracle_calls,
            "decomposition_nodes": node_count,
            "budget_exhausted": budget_exhausted,
        },
        "warnings": checked_warnings,
    }
    expected_digest = content_digest(core)
    require(
        proposal.get("proposal_digest") == expected_digest,
        "invalid_decomposition",
        "proposal_digest does not match the decomposition content",
    )
    normalized = {**core, "proposal_digest": expected_digest}
    encoded = canonical_json(normalized).encode("utf-8")
    require(
        len(encoded) <= MAX_PROPOSAL_BYTES,
        "decomposition_too_large",
        f"A decomposition proposal must be at most {MAX_PROPOSAL_BYTES} bytes",
    )
    return normalized


def _count_nodes(node: dict[str, Any]) -> int:
    return 1 + sum(
        _count_nodes(axis["decomposition"])
        for axis in node["axes"]
        if axis["decomposition"] is not None
    )


def decomposition_axis_ids(proposal: dict[str, Any]) -> set[str]:
    result: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        for axis in node["axes"]:
            result.add(axis["axis_id"])
            if axis["decomposition"] is not None:
                visit(axis["decomposition"])

    visit(proposal["root"])
    return result


def validate_decomposition_bindings(
    value: Any, *, proposal: dict[str, Any], child_count: int
) -> list[dict[str, Any]]:
    """Bind every proposed payable child to one reviewed conceptual axis."""

    require(
        isinstance(value, list) and len(value) == child_count,
        "invalid_decomposition_binding",
        "decomposition_bindings must bind every child exactly once",
    )
    known_axes = decomposition_axis_ids(proposal)
    checked: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        binding = require_object(raw, f"decomposition_bindings[{index}]")
        child_index = binding.get("child_index")
        require(
            isinstance(child_index, int)
            and not isinstance(child_index, bool)
            and 0 <= child_index < child_count,
            "invalid_decomposition_binding",
            "child_index is outside the child list",
        )
        axis_id = _validate_axis_id(
            binding.get("axis_id"), f"decomposition_bindings[{index}].axis_id"
        )
        require(
            axis_id in known_axes,
            "invalid_decomposition_binding",
            "A decomposition binding references an unknown axis",
            axis_id=axis_id,
        )
        checked.append({"child_index": child_index, "axis_id": axis_id})
    require(
        {item["child_index"] for item in checked} == set(range(child_count)),
        "invalid_decomposition_binding",
        "Each child index must be bound exactly once",
    )
    checked.sort(key=lambda item: item["child_index"])
    return checked
