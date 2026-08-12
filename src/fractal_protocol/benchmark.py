from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass
from functools import wraps
from threading import RLock
from typing import Any, Callable, Protocol

from .errors import DomainError, require
from .protocol import canonical_json, content_digest, require_object, require_string


MATCHED_BUDGET_ARMS = (
    "frontier_model",
    "role_based_team",
    "conceptual_decomposition",
    "scrambled_decomposition",
    "role_based_team_with_typed_gates",
)
MAX_TRIAL_OUTPUT_BYTES = 512 * 1024


@dataclass(frozen=True)
class BudgetLimits:
    """Comparable per-trial resource ceilings.

    Monetary values use micro-units of a declared currency so small inference
    charges remain integral. Wall-clock time is intentionally absent from the
    deterministic report; production runners should enforce timeouts outside the
    strategy process and record them in a separate operational trace.
    """

    model_calls: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    monetary_microunits: int

    def normalized(self) -> dict[str, int]:
        normalized = asdict(self)
        for name, value in normalized.items():
            require(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0,
                "invalid_benchmark_budget",
                f"{name} must be a non-negative integer",
            )
        require(
            any(value > 0 for value in normalized.values()),
            "invalid_benchmark_budget",
            "At least one benchmark budget dimension must be positive",
        )
        return normalized


@dataclass(frozen=True)
class Usage:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    monetary_microunits: int = 0
    complete: bool = True

    def normalized(self) -> dict[str, int]:
        require(
            isinstance(self.complete, bool) and self.complete,
            "incomplete_benchmark_usage",
            "Usage must be complete, including hidden reasoning tokens when applicable",
        )
        normalized = {
            key: value for key, value in asdict(self).items() if key != "complete"
        }
        for name, value in normalized.items():
            require(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0,
                "invalid_benchmark_usage",
                f"{name} must be a non-negative integer",
            )
        require(
            any(value > 0 for value in normalized.values()),
            "invalid_benchmark_usage",
            "A usage charge cannot be empty",
        )
        return normalized


class BudgetExceeded(Exception):
    def __init__(self, dimensions: list[str]) -> None:
        self.dimensions = dimensions
        super().__init__(f"Benchmark budget exceeded: {', '.join(dimensions)}")


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def locked(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class BudgetMeter:
    """Harness-owned accounting passed to every strategy arm."""

    def __init__(self, limits: BudgetLimits) -> None:
        self._lock = RLock()
        self._limits = limits.normalized()
        self._used = {name: 0 for name in self._limits}
        self._reserved = {name: 0 for name in self._limits}
        self._reservations: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._violation: dict[str, Any] | None = None
        self._sealed = False

    @property
    @_locked
    def limits(self) -> dict[str, int]:
        return dict(self._limits)

    @property
    @_locked
    def used(self) -> dict[str, int]:
        return dict(self._used)

    @property
    @_locked
    def remaining(self) -> dict[str, int]:
        return {
            name: self._limits[name] - self._used[name] - self._reserved[name]
            for name in self._limits
        }

    @_locked
    def reserve(
        self,
        maximum: Usage,
        *,
        operation: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Reserve a call's maximum entitlement before invoking a backend."""

        require(
            not self._sealed and self._violation is None,
            "benchmark_budget_already_exceeded",
            "No further work may be reserved after trial close or a budget violation",
        )
        checked_operation = require_string(operation, "operation")
        checked_usage = maximum.normalized()
        checked_metadata = json.loads(
            canonical_json(require_object(metadata or {}, "metadata"))
        )
        attempted = {
            name: self._used[name] + self._reserved[name] + checked_usage[name]
            for name in self._limits
        }
        exceeded = [
            name for name in self._limits if attempted[name] > self._limits[name]
        ]
        if exceeded:
            self._violation = {
                "operation": checked_operation,
                "maximum": checked_usage,
                "dimensions": exceeded,
                "phase": "reserve",
            }
            raise BudgetExceeded(exceeded)
        reservation_id = f"reservation-{len(self._events) + 1}"
        self._reservations[reservation_id] = {
            "maximum": checked_usage,
            "operation": checked_operation,
            "metadata": checked_metadata,
        }
        self._reserved = {
            name: self._reserved[name] + checked_usage[name] for name in self._limits
        }
        self._events.append(
            {
                "sequence": len(self._events) + 1,
                "event": "reserved",
                "reservation_id": reservation_id,
                "operation": checked_operation,
                "maximum": checked_usage,
                "metadata": checked_metadata,
            }
        )
        return reservation_id

    @_locked
    def settle(self, reservation_id: str, actual: Usage) -> None:
        """Settle complete provider-reported usage against a reservation."""

        reservation = self._reservations.get(reservation_id)
        require(
            not self._sealed and reservation is not None,
            "invalid_benchmark_reservation",
            "Unknown, already-settled, or closed budget reservation",
        )
        checked_usage = actual.normalized()
        exceeded = [
            name
            for name in self._limits
            if checked_usage[name] > reservation["maximum"][name]
        ]
        if exceeded:
            self._reservations.pop(reservation_id)
            self._reserved = {
                name: self._reserved[name] - reservation["maximum"][name]
                for name in self._limits
            }
            self._used = {
                name: self._used[name] + checked_usage[name]
                for name in self._limits
            }
            self._violation = {
                "operation": reservation["operation"],
                "reservation_id": reservation_id,
                "maximum": reservation["maximum"],
                "actual": checked_usage,
                "dimensions": exceeded,
                "phase": "settle",
            }
            self._events.append(
                {
                    "sequence": len(self._events) + 1,
                    "event": "settlement_overrun",
                    "reservation_id": reservation_id,
                    "operation": reservation["operation"],
                    "usage": checked_usage,
                }
            )
            raise BudgetExceeded(exceeded)
        self._reservations.pop(reservation_id)
        self._reserved = {
            name: self._reserved[name] - reservation["maximum"][name]
            for name in self._limits
        }
        self._used = {
            name: self._used[name] + checked_usage[name] for name in self._limits
        }
        self._events.append(
            {
                "sequence": len(self._events) + 1,
                "event": "settled",
                "reservation_id": reservation_id,
                "operation": reservation["operation"],
                "usage": checked_usage,
            }
        )

    @_locked
    def charge(
        self,
        usage: Usage,
        *,
        operation: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Charge actual usage before a model/tool result is released to an arm."""

        require(
            not self._sealed and self._violation is None,
            "benchmark_budget_already_exceeded",
            "No further work may be charged after trial close or a budget violation",
        )
        reservation_id = self.reserve(
            usage, operation=operation, metadata=metadata
        )
        self.settle(reservation_id, usage)

    @_locked
    def assert_settled(self) -> None:
        self._sealed = True
        if self._violation is not None:
            raise BudgetExceeded(list(self._violation["dimensions"]))
        require(
            not self._reservations,
            "unsettled_benchmark_reservation",
            "A strategy returned before settling every budget reservation",
            reservation_ids=sorted(self._reservations),
        )

    @_locked
    def report(self) -> dict[str, Any]:
        report = {
            "limits": self.limits,
            "used": self.used,
            "reserved": dict(self._reserved),
            "remaining": self.remaining,
            "events": list(self._events),
            "sealed": self._sealed,
            "violation": self._violation,
        }
        return json.loads(canonical_json(report))


@dataclass(frozen=True)
class Evaluation:
    score: float
    passed: bool
    metrics: dict[str, Any]

    def normalized(self) -> dict[str, Any]:
        valid_score = (
            isinstance(self.score, (int, float))
            and not isinstance(self.score, bool)
            and 0 <= self.score <= 1
        )
        require(
            valid_score,
            "invalid_benchmark_evaluation",
            "Evaluation score must be between 0 and 1",
        )
        require(
            isinstance(self.passed, bool),
            "invalid_benchmark_evaluation",
            "Evaluation passed must be boolean",
        )
        metrics = json.loads(
            canonical_json(require_object(self.metrics, "evaluation.metrics"))
        )
        return {
            "score": float(self.score),
            "passed": self.passed,
            "metrics": metrics,
        }


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    problem_class: str
    public_input: dict[str, Any]
    evaluate: Callable[[Any], Evaluation]

    def normalized_public_record(self) -> dict[str, Any]:
        case_id = require_string(self.case_id, "case_id")
        problem_class = require_string(self.problem_class, "problem_class")
        public_input = require_object(self.public_input, "public_input")
        normalized_input = json.loads(canonical_json(public_input))
        return {
            "case_id": case_id,
            "problem_class": problem_class,
            "public_input": normalized_input,
        }


class BenchmarkStrategy(Protocol):
    identity: dict[str, Any]

    def solve(
        self,
        public_input: dict[str, Any],
        budget: BudgetMeter,
        *,
        seed: int,
    ) -> Any: ...


@dataclass
class FunctionStrategy:
    """Small adapter useful for deterministic fixtures and local baselines."""

    identity: dict[str, Any]
    function: Callable[[dict[str, Any], BudgetMeter, int], Any]

    def solve(
        self,
        public_input: dict[str, Any],
        budget: BudgetMeter,
        *,
        seed: int,
    ) -> Any:
        return self.function(public_input, budget, seed)


def _require_sha256_digest(value: Any, field: str) -> str:
    digest = require_string(value, field)
    suffix = digest.removeprefix("sha256:")
    require(
        digest.startswith("sha256:")
        and len(suffix) == 64
        and all(character in "0123456789abcdef" for character in suffix),
        "invalid_benchmark",
        f"{field} must be a full lowercase SHA-256 digest",
    )
    return digest


def _normalize_strategy_identity(value: Any) -> dict[str, Any]:
    identity = require_object(value, "strategy.identity")
    normalized = {
        "arm": require_string(identity.get("arm"), "strategy.identity.arm"),
        "implementation_id": require_string(
            identity.get("implementation_id"),
            "strategy.identity.implementation_id",
        ),
        "version": require_string(
            identity.get("version"), "strategy.identity.version"
        ),
        "model": require_string(identity.get("model"), "strategy.identity.model"),
        "prompt_digest": _require_sha256_digest(
            identity.get("prompt_digest"), "strategy.identity.prompt_digest"
        ),
    }
    return normalized


def _stable_seed(*parts: Any) -> int:
    encoded = canonical_json(parts).encode("utf-8")
    return int(hashlib.sha256(encoded).hexdigest()[:16], 16)


class MatchedBudgetHarness:
    """Run blind objective comparisons under identical multidimensional caps.

    The harness randomizes arm order and passes only the final JSON output to the
    evaluator. Strategies must route every billable model/tool call through the
    supplied ``BudgetMeter`` (normally via an inference adapter). The harness can
    detect overcharging; it cannot detect a strategy that deliberately bypasses
    the meter, so production runs must isolate network/model access accordingly.
    """

    def __init__(
        self,
        strategies: list[BenchmarkStrategy],
        *,
        budget: BudgetLimits,
        dataset_digest: str,
        evaluator_id: str,
        evaluator_version: str,
        evaluator_digest: str,
        currency: str,
        pricing_snapshot_digest: str,
        tokenizer_id: str,
        seed: int = 0,
        required_arms: tuple[str, ...] = MATCHED_BUDGET_ARMS,
    ) -> None:
        require(
            isinstance(seed, int) and not isinstance(seed, bool),
            "invalid_benchmark",
            "seed must be an integer",
        )
        self.strategies = strategies
        self.budget = budget
        self.dataset_digest = _require_sha256_digest(
            dataset_digest, "dataset_digest"
        )
        self.evaluator_id = require_string(evaluator_id, "evaluator_id")
        self.evaluator_version = require_string(
            evaluator_version, "evaluator_version"
        )
        self.evaluator_digest = _require_sha256_digest(
            evaluator_digest, "evaluator_digest"
        )
        self.currency = require_string(currency, "currency").upper()
        self.pricing_snapshot_digest = _require_sha256_digest(
            pricing_snapshot_digest, "pricing_snapshot_digest"
        )
        self.tokenizer_id = require_string(tokenizer_id, "tokenizer_id")
        self.seed = seed
        self.required_arms = required_arms

    def run(self, cases: list[BenchmarkCase]) -> dict[str, Any]:
        require(cases, "invalid_benchmark", "At least one benchmark case is required")
        budget = self.budget.normalized()
        identities = [
            _normalize_strategy_identity(strategy.identity)
            for strategy in self.strategies
        ]
        arms = [identity["arm"] for identity in identities]
        require(
            len(set(arms)) == len(arms),
            "invalid_benchmark",
            "Strategy arm names must be unique",
        )
        missing_arms = sorted(set(self.required_arms) - set(arms))
        require(
            not missing_arms,
            "invalid_benchmark",
            "The matched-compute comparison is missing required arms",
            missing_arms=missing_arms,
        )
        case_records = [case.normalized_public_record() for case in cases]
        case_ids = [case["case_id"] for case in case_records]
        require(
            len(set(case_ids)) == len(case_ids),
            "invalid_benchmark",
            "Benchmark case ids must be unique",
        )

        trials: list[dict[str, Any]] = []
        execution_order: list[dict[str, Any]] = []
        by_arm = {
            identity["arm"]: (strategy, identity)
            for strategy, identity in zip(self.strategies, identities, strict=True)
        }
        for case, case_record in zip(cases, case_records, strict=True):
            ordered_arms = list(arms)
            random.Random(_stable_seed(self.seed, case.case_id, "arm_order")).shuffle(
                ordered_arms
            )
            execution_order.append(
                {"case_id": case.case_id, "arms": list(ordered_arms)}
            )
            for arm in ordered_arms:
                strategy, identity = by_arm[arm]
                trial_seed = _stable_seed(self.seed, case.case_id, arm, "trial")
                meter = BudgetMeter(self.budget)
                output: Any = None
                output_digest: str | None = None
                evaluation: dict[str, Any] | None = None
                status = "completed"
                error: dict[str, Any] | None = None
                try:
                    output = strategy.solve(
                        json.loads(canonical_json(case_record["public_input"])),
                        meter,
                        seed=trial_seed,
                    )
                    meter.assert_settled()
                    encoded_text = canonical_json(output)
                    encoded = encoded_text.encode("utf-8")
                    require(
                        len(encoded) <= MAX_TRIAL_OUTPUT_BYTES,
                        "benchmark_output_too_large",
                        f"A trial output must be at most {MAX_TRIAL_OUTPUT_BYTES} bytes",
                    )
                    output = json.loads(encoded_text)
                    output_digest = content_digest(output)
                except BudgetExceeded as exc:
                    status = "budget_exceeded"
                    error = {
                        "code": "budget_exceeded",
                        "dimensions": exc.dimensions,
                    }
                    output = None
                except DomainError as exc:
                    status = "invalid_trial"
                    error = {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                    output = None
                except Exception as exc:  # isolate one failed arm from the experiment
                    status = "strategy_error"
                    error = {
                        "code": "strategy_error",
                        "type": type(exc).__name__,
                        "message": str(exc)[:2_000],
                    }
                    output = None
                if status == "completed":
                    try:
                        # The evaluator receives neither arm identity nor strategy
                        # trace, and receives a copy it cannot mutate in the report.
                        evaluation = case.evaluate(
                            json.loads(canonical_json(output))
                        ).normalized()
                    except Exception as exc:
                        status = "evaluator_error"
                        error = {
                            "code": "evaluator_error",
                            "type": type(exc).__name__,
                            "message": str(exc)[:2_000],
                        }
                trials.append(
                    {
                        "trial_id": content_digest(
                            {
                                "dataset_digest": self.dataset_digest,
                                "case_id": case.case_id,
                                "arm": arm,
                                "seed": trial_seed,
                            }
                        ),
                        "case_id": case.case_id,
                        "problem_class": case.problem_class,
                        "input_digest": content_digest(case_record["public_input"]),
                        "strategy": identity,
                        "seed": trial_seed,
                        "status": status,
                        "output": output,
                        "output_digest": output_digest,
                        "evaluation": evaluation,
                        "budget": meter.report(),
                        "error": error,
                    }
                )

        aggregates = self._aggregate(trials, arms)
        core = {
            "protocol_version": "1",
            "kind": "matched_budget_benchmark_report",
            "dataset_digest": self.dataset_digest,
            "evaluator": {
                "evaluator_id": self.evaluator_id,
                "version": self.evaluator_version,
                "digest": self.evaluator_digest,
            },
            "accounting": {
                "currency": self.currency,
                "pricing_snapshot_digest": self.pricing_snapshot_digest,
                "tokenizer_id": self.tokenizer_id,
            },
            "seed": self.seed,
            "budget_per_trial": budget,
            "required_arms": list(self.required_arms),
            "strategies": identities,
            "cases": case_records,
            "execution_order": execution_order,
            "trials": trials,
            "aggregates": aggregates,
        }
        return {**core, "report_digest": content_digest(core)}

    @staticmethod
    def _aggregate(
        trials: list[dict[str, Any]], arms: list[str]
    ) -> list[dict[str, Any]]:
        aggregates: list[dict[str, Any]] = []
        for arm in arms:
            selected = [trial for trial in trials if trial["strategy"]["arm"] == arm]
            completed = [trial for trial in selected if trial["status"] == "completed"]
            scores = [trial["evaluation"]["score"] for trial in completed]
            passes = [trial["evaluation"]["passed"] for trial in completed]
            used = {
                name: sum(trial["budget"]["used"][name] for trial in selected)
                for name in (
                    "model_calls",
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "monetary_microunits",
                )
            }
            aggregates.append(
                {
                    "arm": arm,
                    "trial_count": len(selected),
                    "completed_count": len(completed),
                    "failure_count": len(selected) - len(completed),
                    "mean_score": (
                        sum(scores) / len(selected) if selected else None
                    ),
                    "pass_rate": (
                        sum(1 for passed in passes if passed) / len(selected)
                        if selected
                        else None
                    ),
                    "completed_mean_score": (
                        sum(scores) / len(scores) if scores else None
                    ),
                    "completed_pass_rate": (
                        sum(1 for passed in passes if passed) / len(passes)
                        if passes
                        else None
                    ),
                    "total_usage": used,
                }
            )
        return aggregates
