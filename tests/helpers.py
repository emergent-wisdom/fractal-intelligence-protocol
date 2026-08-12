from __future__ import annotations

from typing import Any


class MutableClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def manifest(concept: str, name: str, operation: str) -> dict[str, Any]:
    return {
        "concept_ref": concept,
        "name": name,
        "description": f"A deterministic {name} test Solver",
        "cognitive_mode": "convergent",
        "operations": [operation],
        "surfaces": ["manifest", "execute"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }


def exact_accept_spec(path: str, expected: Any) -> dict[str, Any]:
    return {
        "seam": "hard",
        "minimum_pass_rate": 1.0,
        "clauses": [
            {
                "id": "expected-value",
                "path": path,
                "operator": "equals",
                "expected": expected,
                "critical": True,
                "disclosure": "hidden",
            }
        ],
    }


def task_spec(
    capability: str,
    operation: str,
    *,
    inputs: dict[str, Any],
    expected_path: str,
    expected: Any,
    reward: int,
    delegation_budget: int = 0,
    constraints: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    return {
        "required_capability": capability,
        "operation": operation,
        "inputs": inputs,
        "constraints": constraints or {},
        "reward_minor": reward,
        "delegation_budget_minor": delegation_budget,
        "max_attempts": max_attempts,
        "accept_spec": exact_accept_spec(expected_path, expected),
    }
