from __future__ import annotations

import json
from typing import Any

from fractal_protocol.benchmark import (
    MATCHED_BUDGET_ARMS,
    BenchmarkCase,
    BudgetLimits,
    BudgetMeter,
    Evaluation,
    FunctionStrategy,
    MatchedBudgetHarness,
    Usage,
)
from fractal_protocol.protocol import content_digest


def strategy(arm: str) -> FunctionStrategy:
    def solve(public_input: dict[str, Any], meter: BudgetMeter, seed: int) -> Any:
        # A real adapter reserves before calling its provider and settles the
        # provider's complete authoritative usage afterward.
        reservation = meter.reserve(
            Usage(
                model_calls=1,
                input_tokens=20,
                output_tokens=10,
                reasoning_tokens=10,
                monetary_microunits=200,
            ),
            operation="scripted-fixture",
            metadata={"seed": seed},
        )
        meter.settle(
            reservation,
            Usage(
                model_calls=1,
                input_tokens=8,
                output_tokens=3,
                reasoning_tokens=2,
                monetary_microunits=75,
            ),
        )
        return {"answer": sum(public_input["numbers"])}

    return FunctionStrategy(
        identity={
            "arm": arm,
            "implementation_id": f"scripted-demo:{arm}",
            "version": "1",
            "model": "no-model-scripted-fixture",
            "prompt_digest": content_digest({"demo_template": arm}),
        },
        function=solve,
    )


def main() -> None:
    case = BenchmarkCase(
        case_id="sum-fixture-1",
        problem_class="exact_arithmetic",
        public_input={"numbers": [3, 5, 8]},
        evaluate=lambda output: Evaluation(
            score=1.0 if output == {"answer": 16} else 0.0,
            passed=output == {"answer": 16},
            metrics={"exact": output == {"answer": 16}},
        ),
    )
    harness = MatchedBudgetHarness(
        [strategy(arm) for arm in MATCHED_BUDGET_ARMS],
        budget=BudgetLimits(
            model_calls=4,
            input_tokens=100,
            output_tokens=50,
            reasoning_tokens=50,
            monetary_microunits=1_000,
        ),
        dataset_digest=content_digest({"dataset": "scripted-demo-v1"}),
        evaluator_id="exact-json-evaluator",
        evaluator_version="1",
        evaluator_digest=content_digest({"evaluator": "exact-json-evaluator-v1"}),
        currency="USD",
        pricing_snapshot_digest=content_digest({"pricing": "scripted-demo-v1"}),
        tokenizer_id="scripted-tokenizer-v1",
        seed=2026,
    )
    report = harness.run([case])
    print(
        json.dumps(
            {
                "report_digest": report["report_digest"],
                "execution_order": report["execution_order"],
                "aggregates": report["aggregates"],
                "note": "Scripted arms validate the harness; they are not a performance result.",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
