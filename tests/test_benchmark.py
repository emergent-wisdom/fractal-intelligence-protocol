from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
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
from fractal_protocol.errors import DomainError
from fractal_protocol.protocol import content_digest


def identity(arm: str) -> dict[str, Any]:
    return {
        "arm": arm,
        "implementation_id": f"fixture:{arm}",
        "version": "1",
        "model": "scripted",
        "prompt_digest": content_digest({"arm": arm, "template": "fixture"}),
    }


def successful_strategy(arm: str) -> FunctionStrategy:
    def solve(public_input: dict[str, Any], meter: BudgetMeter, seed: int) -> Any:
        meter.charge(
            Usage(
                model_calls=1,
                input_tokens=10,
                output_tokens=5,
                reasoning_tokens=2,
                monetary_microunits=100,
            ),
            operation="solve",
            metadata={"seed": seed},
        )
        return {"answer": public_input["value"] * 2}

    return FunctionStrategy(identity(arm), solve)


class BenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategies = [successful_strategy(arm) for arm in MATCHED_BUDGET_ARMS]
        self.case = BenchmarkCase(
            case_id="double-7",
            problem_class="exact_arithmetic",
            public_input={"value": 7},
            evaluate=lambda output: Evaluation(
                score=1.0 if output == {"answer": 14} else 0.0,
                passed=output == {"answer": 14},
                metrics={"exact": output == {"answer": 14}},
            ),
        )
        self.budget = BudgetLimits(
            model_calls=2,
            input_tokens=20,
            output_tokens=10,
            reasoning_tokens=10,
            monetary_microunits=200,
        )

    def harness(self, strategies: list[FunctionStrategy] | None = None) -> MatchedBudgetHarness:
        return MatchedBudgetHarness(
            strategies or self.strategies,
            budget=self.budget,
            dataset_digest=content_digest({"fixture_set": "v1"}),
            evaluator_id="exact-json",
            evaluator_version="1",
            evaluator_digest=content_digest({"evaluator": "exact-json-v1"}),
            currency="USD",
            pricing_snapshot_digest=content_digest({"pricing": "scripted-v1"}),
            tokenizer_id="scripted-tokenizer-v1",
            seed=73,
        )

    def test_all_five_arms_run_blind_under_identical_budgets(self) -> None:
        report = self.harness().run([self.case])

        self.assertEqual(5, len(report["trials"]))
        self.assertEqual(set(MATCHED_BUDGET_ARMS), {trial["strategy"]["arm"] for trial in report["trials"]})
        for trial in report["trials"]:
            self.assertEqual("completed", trial["status"])
            self.assertEqual(1.0, trial["evaluation"]["score"])
            self.assertEqual(1, trial["budget"]["used"]["model_calls"])
            self.assertEqual(self.budget.normalized(), trial["budget"]["limits"])

    def test_scripted_run_is_byte_reproducible(self) -> None:
        first = self.harness().run([self.case])
        second = self.harness().run([self.case])

        self.assertEqual(first, second)
        self.assertEqual(first["report_digest"], second["report_digest"])

    def test_over_budget_arm_is_scored_as_failure_without_evaluation(self) -> None:
        evaluations = 0

        def evaluate(output: Any) -> Evaluation:
            nonlocal evaluations
            evaluations += 1
            return Evaluation(score=1.0, passed=True, metrics={})

        case = BenchmarkCase("budget", "accounting", {"value": 7}, evaluate)

        def over_budget(public_input: dict[str, Any], meter: BudgetMeter, seed: int) -> Any:
            meter.charge(
                Usage(model_calls=3), operation="unbounded-call"
            )
            return {"unreachable": True}

        strategies = list(self.strategies)
        strategies[0] = FunctionStrategy(identity(MATCHED_BUDGET_ARMS[0]), over_budget)
        report = self.harness(strategies).run([case])
        failed = next(
            trial
            for trial in report["trials"]
            if trial["strategy"]["arm"] == MATCHED_BUDGET_ARMS[0]
        )

        self.assertEqual("budget_exceeded", failed["status"])
        self.assertIsNone(failed["evaluation"])
        self.assertEqual(4, evaluations)
        aggregate = next(
            item
            for item in report["aggregates"]
            if item["arm"] == MATCHED_BUDGET_ARMS[0]
        )
        self.assertEqual(0.0, aggregate["mean_score"])
        self.assertEqual(0.0, aggregate["pass_rate"])

    def test_missing_required_ablation_is_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "missing required arms"):
            self.harness(self.strategies[:-1]).run([self.case])

    def test_backend_usage_cannot_exceed_its_reservation(self) -> None:
        def overrun(public_input: dict[str, Any], meter: BudgetMeter, seed: int) -> Any:
            reservation = meter.reserve(
                Usage(model_calls=1, output_tokens=1, reasoning_tokens=1),
                operation="model-call",
            )
            meter.settle(
                reservation,
                Usage(model_calls=1, output_tokens=1, reasoning_tokens=2),
            )
            return {"answer": 14}

        strategies = list(self.strategies)
        strategies[0] = FunctionStrategy(identity(MATCHED_BUDGET_ARMS[0]), overrun)
        report = self.harness(strategies).run([self.case])
        failed = next(
            trial
            for trial in report["trials"]
            if trial["strategy"]["arm"] == MATCHED_BUDGET_ARMS[0]
        )

        self.assertEqual("budget_exceeded", failed["status"])
        self.assertEqual("settle", failed["budget"]["violation"]["phase"])
        self.assertEqual(2, failed["budget"]["used"]["reasoning_tokens"])

    def test_strategy_cannot_swallow_a_budget_violation(self) -> None:
        def swallowing(public_input: dict[str, Any], meter: BudgetMeter, seed: int) -> Any:
            try:
                meter.charge(Usage(model_calls=3), operation="caught-overrun")
            except Exception:
                pass
            return {"answer": 14}

        strategies = list(self.strategies)
        strategies[0] = FunctionStrategy(identity(MATCHED_BUDGET_ARMS[0]), swallowing)
        report = self.harness(strategies).run([self.case])
        failed = next(
            trial
            for trial in report["trials"]
            if trial["strategy"]["arm"] == MATCHED_BUDGET_ARMS[0]
        )

        self.assertEqual("budget_exceeded", failed["status"])
        self.assertIsNone(failed["evaluation"])

    def test_unsettled_or_incomplete_usage_invalidates_a_trial(self) -> None:
        def unsettled(public_input: dict[str, Any], meter: BudgetMeter, seed: int) -> Any:
            meter.reserve(Usage(model_calls=1), operation="forgotten-call")
            return {"answer": 14}

        strategies = list(self.strategies)
        strategies[0] = FunctionStrategy(identity(MATCHED_BUDGET_ARMS[0]), unsettled)
        report = self.harness(strategies).run([self.case])
        failed = next(
            trial
            for trial in report["trials"]
            if trial["strategy"]["arm"] == MATCHED_BUDGET_ARMS[0]
        )

        self.assertEqual("invalid_trial", failed["status"])
        self.assertEqual("unsettled_benchmark_reservation", failed["error"]["code"])

    def test_closed_meter_rejects_late_background_work(self) -> None:
        meter = BudgetMeter(self.budget)
        meter.assert_settled()

        with self.assertRaises(DomainError):
            meter.reserve(Usage(model_calls=1), operation="late-work")
        self.assertTrue(meter.report()["sealed"])

    def test_parallel_reservations_cannot_overspend_or_reuse_ids(self) -> None:
        meter = BudgetMeter(
            BudgetLimits(
                model_calls=1,
                input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                monetary_microunits=0,
            )
        )
        barrier = Barrier(2)
        result_lock = Lock()
        reservations: list[str] = []
        failures: list[str] = []

        def reserve() -> None:
            barrier.wait()
            try:
                reservation = meter.reserve(
                    Usage(model_calls=1), operation="parallel-call"
                )
                with result_lock:
                    reservations.append(reservation)
            except Exception as exc:
                with result_lock:
                    failures.append(type(exc).__name__)

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: reserve(), range(2)))
        self.assertEqual(1, len(reservations))
        self.assertEqual(["BudgetExceeded"], failures)
        meter.settle(reservations[0], Usage(model_calls=1))
        report = meter.report()
        self.assertEqual(1, report["used"]["model_calls"])
        self.assertEqual(0, report["reserved"]["model_calls"])

    def test_usage_metadata_and_evaluation_metrics_are_copied(self) -> None:
        meter = BudgetMeter(self.budget)
        metadata = {"nested": {"value": 1}}
        meter.charge(Usage(model_calls=1), operation="copy", metadata=metadata)
        metadata["nested"]["value"] = object()
        self.assertEqual(
            1, meter.report()["events"][0]["metadata"]["nested"]["value"]
        )

        metrics = {"nested": {"value": 1}}
        normalized = Evaluation(score=1, passed=True, metrics=metrics).normalized()
        metrics["nested"]["value"] = 2
        self.assertEqual(1, normalized["metrics"]["nested"]["value"])

    def test_strategy_cannot_mutate_the_input_seen_by_other_arms(self) -> None:
        def mutating(public_input: dict[str, Any], meter: BudgetMeter, seed: int) -> Any:
            public_input["value"] = 999
            meter.charge(Usage(model_calls=1), operation="mutating-fixture")
            return {"answer": 1998}

        strategies = list(self.strategies)
        strategies[0] = FunctionStrategy(identity(MATCHED_BUDGET_ARMS[0]), mutating)
        report = self.harness(strategies).run([self.case])

        self.assertEqual({"value": 7}, report["cases"][0]["public_input"])
        unaffected = [
            trial
            for trial in report["trials"]
            if trial["strategy"]["arm"] != MATCHED_BUDGET_ARMS[0]
        ]
        self.assertTrue(all(trial["output"] == {"answer": 14} for trial in unaffected))

    def test_evaluator_receives_a_copy_and_errors_are_not_strategy_errors(self) -> None:
        def mutating_evaluator(output: Any) -> Evaluation:
            correct = output == {"answer": 14}
            output["answer"] = "mutated"
            return Evaluation(score=1.0 if correct else 0.0, passed=correct, metrics={})

        case = BenchmarkCase("copy", "isolation", {"value": 7}, mutating_evaluator)
        report = self.harness().run([case])
        self.assertTrue(all(trial["output"] == {"answer": 14} for trial in report["trials"]))
        self.assertTrue(
            all(content_digest(trial["output"]) == trial["output_digest"] for trial in report["trials"])
        )

        failing_case = BenchmarkCase(
            "evaluator-error",
            "isolation",
            {"value": 7},
            lambda output: (_ for _ in ()).throw(RuntimeError("arbiter unavailable")),
        )
        failed_report = self.harness().run([failing_case])
        self.assertTrue(
            all(trial["status"] == "evaluator_error" for trial in failed_report["trials"])
        )


if __name__ == "__main__":
    unittest.main()
