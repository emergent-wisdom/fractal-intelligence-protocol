from __future__ import annotations

import json
import unittest
from typing import Any

import fractal_protocol.execution as execution_module
from fractal_protocol.decomposition import (
    ConceptualDecompositionEngine,
    DecompositionConfig,
)
from fractal_protocol.errors import DomainError
from fractal_protocol.execution import (
    ExecutionLimits,
    FunctionModelAdapter,
    SingleModelBackend,
    create_execution_plan,
    execution_plan_from_decomposition,
    validate_execution_plan,
    validate_execution_record,
)
from fractal_protocol.protocol import canonical_json, content_digest

from tests.test_decomposition import CleanOracle, concept, fixed_routing_probes


OBJECT_SCHEMA = {"type": "object"}
VALUE_SCHEMA = {
    "type": "object",
    "required": ["value"],
    "properties": {"value": {"type": "integer"}},
    "additionalProperties": False,
}
ANSWER_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}


def exists_spec(path: str) -> dict[str, Any]:
    return {
        "seam": "hard",
        "minimum_pass_rate": 1.0,
        "clauses": [
            {
                "id": "required-output",
                "path": path,
                "operator": "exists",
                "critical": True,
                "disclosure": "public",
            }
        ],
    }


def equals_spec(path: str, expected: Any) -> dict[str, Any]:
    return {
        "seam": "hard",
        "minimum_pass_rate": 1.0,
        "clauses": [
            {
                "id": "public-required-output",
                "path": path,
                "operator": "exists",
                "critical": True,
                "disclosure": "public",
            },
            {
                "id": "hidden-answer",
                "path": path,
                "operator": "equals",
                "expected": expected,
                "critical": True,
                "disclosure": "hidden",
            }
        ],
    }


def task(
    task_id: str,
    *,
    depends_on: list[str] | None = None,
    expected: int | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "role": f"role-{task_id}",
        "objective": f"Solve isolated work {task_id}",
        "context": {"fixture": task_id},
        "depends_on": depends_on or [],
        "output_schema": VALUE_SCHEMA,
        "accept_spec": (
            equals_spec("/value", expected)
            if expected is not None
            else exists_spec("/value")
        ),
    }


def plan(tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return create_execution_plan(
        {
            "statement": "Produce a checked answer",
            "context": {"customer": "fixture"},
        },
        tasks or [task("analysis", expected=1)],
        {
            "objective": "Synthesize only the supplied branch results",
            "context": {},
            "output_schema": ANSWER_SCHEMA,
            "accept_spec": exists_spec("/answer"),
        },
        source={"type": "test"},
        seed=73,
    )


def response(
    output: dict[str, Any],
    *,
    response_id: str,
    input_tokens: int = 10,
    output_tokens: int = 5,
    reasoning_tokens: int = 2,
    monetary_microunits: int = 100,
    complete: bool = True,
    finish_reason: str = "completed",
) -> dict[str, Any]:
    return {
        "output": output,
        "finish_reason": finish_reason,
        "response_id": response_id,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "monetary_microunits": monetary_microunits,
            "complete": complete,
        },
    }


def limits(**overrides: int) -> ExecutionLimits:
    values = {
        "model_calls": 10,
        "input_tokens": 1_000,
        "output_tokens": 1_000,
        "reasoning_tokens": 1_000,
        "monetary_microunits": 1_000_000,
        "max_output_tokens_per_call": 200,
    }
    values.update(overrides)
    return ExecutionLimits(**values)


def rehash(record: dict[str, Any]) -> dict[str, Any]:
    core = {key: value for key, value in record.items() if key != "record_digest"}
    record["record_digest"] = content_digest(core)
    return record


class ScriptedAdapter:
    identity = {
        "adapter_id": "fixture:scripted-json",
        "adapter_version": "1",
        "model": "fixture-model-v1",
    }

    def __init__(self, script: list[Any]) -> None:
        self.identity = dict(type(self).identity)
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def invoke(self, request_value: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(json.loads(canonical_json(request_value)))
        if not self.script:
            raise AssertionError("Unexpected extra model call")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ExecutionTests(unittest.TestCase):
    def test_single_model_executes_graph_and_synthesis_with_one_identity(self) -> None:
        execution_plan = plan(
            [
                task("second", depends_on=["first"], expected=2),
                task("first", expected=1),
            ]
        )
        adapter = ScriptedAdapter(
            [
                response({"value": 1}, response_id="response-first"),
                response({"value": 2}, response_id="response-second"),
                response({"answer": "checked"}, response_id="response-synthesis"),
            ]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            execution_plan, run_id="run-success"
        )

        self.assertEqual("completed", record["status"])
        self.assertEqual({"answer": "checked"}, record["final_output"])
        self.assertEqual(["first", "second"], execution_plan["task_order"])
        self.assertEqual(["first"], list(adapter.requests[1]["dependencies"]))
        self.assertEqual(
            ["first", "second"], list(adapter.requests[2]["dependencies"])
        )
        self.assertTrue(all(request["seed"] == 73 for request in adapter.requests))
        self.assertTrue(
            all(
                task_result["executor"]
                == {
                    "kind": "model_adapter",
                    "identity": ScriptedAdapter.identity,
                }
                and task_result["status"] == "contract_valid"
                for task_result in record["task_results"]
            )
        )
        self.assertEqual(
            record["backend"]["model_adapter"],
            record["synthesis_result"]["executor"]["identity"],
        )
        self.assertEqual(3, record["usage"]["model_calls"])
        self.assertEqual(30, record["usage"]["input_tokens"])
        self.assertTrue(record["accounting_complete"])
        self.assertEqual("unverified", record["semantic_verification"])
        self.assertEqual(
            record, validate_execution_record(record, plan_value=execution_plan)
        )

    def test_public_acceptance_arrives_but_hidden_expected_value_is_redacted(self) -> None:
        adapter = ScriptedAdapter(
            [
                response({"value": 1}, response_id="branch"),
                response({"answer": "done"}, response_id="synthesis"),
            ]
        )
        execution_plan = plan([task("hidden", expected=1)])

        SingleModelBackend(adapter, limits=limits()).execute(
            execution_plan, run_id="run-hidden"
        )

        exposed_spec = adapter.requests[0]["work"]["accept_spec"]
        public_clause = next(
            clause
            for clause in exposed_spec["clauses"]
            if clause["id"] == "public-required-output"
        )
        hidden_clause = next(
            clause
            for clause in exposed_spec["clauses"]
            if clause["id"] == "hidden-answer"
        )
        self.assertEqual("/value", public_clause["path"])
        self.assertNotIn("expected", hidden_clause)
        self.assertIn("output_schema", adapter.requests[0]["work"])

    def test_plan_rejects_cycles_unknown_dependencies_and_digest_tampering(self) -> None:
        with self.assertRaisesRegex(DomainError, "cycle"):
            plan(
                [
                    task("left", depends_on=["right"]),
                    task("right", depends_on=["left"]),
                ]
            )
        with self.assertRaisesRegex(DomainError, "unknown task"):
            plan([task("left", depends_on=["missing"])])

        execution_plan = plan()
        execution_plan["problem"]["statement"] = "tampered"
        with self.assertRaisesRegex(DomainError, "plan_digest"):
            validate_execution_plan(execution_plan)

        unsupported_plan = plan()
        unsupported_plan["payment_authorized"] = {"asset": "USDC"}
        with self.assertRaisesRegex(DomainError, "unsupported fields"):
            validate_execution_plan(unsupported_plan)

    def test_task_contract_rejection_stops_before_synthesis(self) -> None:
        adapter = ScriptedAdapter(
            [response({"value": 999}, response_id="wrong-branch")]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            plan([task("checked", expected=1)]), run_id="run-rejected"
        )

        self.assertEqual("rejected", record["status"])
        self.assertEqual("quality", record["stop_reason"])
        self.assertEqual("contract_rejected", record["task_results"][0]["status"])
        self.assertIsNone(record["final_output"])
        self.assertEqual(1, len(adapter.requests))

    def test_output_schema_rejection_is_a_deterministic_contract_failure(self) -> None:
        adapter = ScriptedAdapter(
            [response({"value": "not-an-integer"}, response_id="bad-shape")]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            plan([task("schema")]), run_id="run-schema-rejected"
        )

        self.assertEqual("rejected", record["status"])
        violations = record["task_results"][0]["verification"]["failure_trace"][
            "violations"
        ]
        self.assertEqual("protocol:output-schema", violations[0]["clause_id"])

    def test_call_budget_is_checked_before_any_model_access(self) -> None:
        adapter = ScriptedAdapter([])

        record = SingleModelBackend(
            adapter, limits=limits(model_calls=1)
        ).execute(plan(), run_id="run-no-budget")

        self.assertEqual("budget_exceeded", record["status"])
        self.assertEqual([], adapter.requests)
        self.assertEqual(0, record["usage"]["model_calls"])
        self.assertTrue(record["accounting_complete"])

    def test_provider_overrun_records_actual_usage_and_discards_output(self) -> None:
        adapter = ScriptedAdapter(
            [
                response(
                    {"value": 1, "secret": "must-not-be-exposed"},
                    response_id="overrun",
                    output_tokens=11,
                )
            ]
        )

        record = SingleModelBackend(
            adapter,
            limits=limits(output_tokens=10, max_output_tokens_per_call=10),
        ).execute(plan(), run_id="run-overrun")

        self.assertEqual("budget_exceeded", record["status"])
        self.assertEqual(11, record["usage"]["output_tokens"])
        self.assertEqual([], record["task_results"])
        self.assertNotIn("must-not-be-exposed", canonical_json(record))
        self.assertTrue(record["accounting_complete"])

    def test_per_call_output_ceiling_is_enforced_below_aggregate_budget(self) -> None:
        adapter = ScriptedAdapter(
            [
                response(
                    {"value": 1},
                    response_id="per-call-overrun",
                    output_tokens=11,
                )
            ]
        )

        record = SingleModelBackend(
            adapter,
            limits=limits(
                output_tokens=1_000,
                max_output_tokens_per_call=10,
            ),
        ).execute(plan(), run_id="run-per-call-overrun")

        self.assertEqual("budget_exceeded", record["status"])
        self.assertEqual(11, record["usage"]["output_tokens"])
        self.assertIn(
            "max_output_tokens_per_call", record["error"]["dimensions"]
        )

    def test_large_synthesis_request_returns_failed_record_after_paid_branches(self) -> None:
        large_schema = {
            "type": "object",
            "required": ["blob"],
            "properties": {"blob": {"type": "string"}},
            "additionalProperties": False,
        }
        large_tasks = [
            {
                "task_id": task_id,
                "role": "large-branch",
                "objective": "Return a large contract-valid fixture",
                "context": {},
                "depends_on": [],
                "output_schema": large_schema,
                "accept_spec": exists_spec("/blob"),
            }
            for task_id in ("large-one", "large-two")
        ]
        execution_plan = create_execution_plan(
            {"statement": "Exercise request bounds", "context": {}},
            large_tasks,
            {
                "objective": "Synthesize both large outputs",
                "context": {},
                "output_schema": ANSWER_SCHEMA,
                "accept_spec": exists_spec("/answer"),
            },
        )
        adapter = ScriptedAdapter(
            [
                response(
                    {"blob": "a" * 300_000},
                    response_id="large-one",
                    output_tokens=1,
                ),
                response(
                    {"blob": "b" * 300_000},
                    response_id="large-two",
                    output_tokens=1,
                ),
            ]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            execution_plan, run_id="run-large-synthesis"
        )

        self.assertEqual("failed", record["status"])
        self.assertEqual("model_request_too_large", record["error"]["code"])
        self.assertEqual(2, record["usage"]["model_calls"])
        self.assertTrue(record["accounting_complete"])
        self.assertEqual(
            record, validate_execution_record(record, plan_value=execution_plan)
        )

    def test_cumulative_large_outputs_stop_before_record_capacity_is_exhausted(self) -> None:
        large_schema = {
            "type": "object",
            "required": ["blob"],
            "properties": {"blob": {"type": "string"}},
            "additionalProperties": False,
        }
        execution_plan = create_execution_plan(
            {"statement": "Bound cumulative retained outputs", "context": {}},
            [
                {
                    "task_id": f"large-{index}",
                    "role": "large-branch",
                    "objective": "Return one large fixture",
                    "context": {},
                    "depends_on": [],
                    "output_schema": large_schema,
                    "accept_spec": exists_spec("/blob"),
                }
                for index in range(8)
            ],
            {
                "objective": "Synthesize every branch",
                "context": {},
                "output_schema": ANSWER_SCHEMA,
                "accept_spec": exists_spec("/answer"),
            },
        )
        adapter = ScriptedAdapter(
            [
                response(
                    {"blob": str(index) * 300_000},
                    response_id=f"large-{index}",
                    output_tokens=1,
                )
                for index in range(8)
            ]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            execution_plan, run_id="run-cumulative-large"
        )

        self.assertEqual("failed", record["status"])
        self.assertEqual("model_request_too_large", record["error"]["code"])
        self.assertLess(len(record["task_results"]), 8)
        self.assertEqual(len(record["task_results"]), record["usage"]["model_calls"])
        self.assertLess(len(canonical_json(record).encode("utf-8")), 2 * 1024 * 1024)
        self.assertEqual(
            record, validate_execution_record(record, plan_value=execution_plan)
        )

    def test_record_capacity_fallback_keeps_accounting_and_digest_only_outputs(self) -> None:
        blob_schema = {
            "type": "object",
            "required": ["blob"],
            "properties": {"blob": {"type": "string"}},
            "additionalProperties": False,
        }
        execution_plan = create_execution_plan(
            {"statement": "Exercise record fallback", "context": {}},
            [
                {
                    "task_id": "capacity-task",
                    "role": "fixture",
                    "objective": "Return a retained output",
                    "context": {},
                    "depends_on": [],
                    "output_schema": blob_schema,
                    "accept_spec": exists_spec("/blob"),
                }
            ],
            {
                "objective": "Return a compact synthesis",
                "context": {},
                "output_schema": ANSWER_SCHEMA,
                "accept_spec": exists_spec("/answer"),
            },
        )
        adapter = ScriptedAdapter(
            [
                response(
                    {"blob": "x" * 20_000},
                    response_id="capacity-task",
                    output_tokens=1,
                ),
                response({"answer": "done"}, response_id="capacity-synthesis"),
            ]
        )
        original_limit = execution_module.MAX_EXECUTION_RECORD_BYTES
        try:
            execution_module.MAX_EXECUTION_RECORD_BYTES = 15_000
            record = SingleModelBackend(adapter, limits=limits()).execute(
                execution_plan, run_id="run-record-capacity"
            )
        finally:
            execution_module.MAX_EXECUTION_RECORD_BYTES = original_limit

        self.assertEqual("failed", record["status"])
        self.assertEqual("record", record["stop_reason"])
        self.assertEqual(
            "execution_record_capacity_exceeded", record["error"]["code"]
        )
        self.assertEqual("digest_only", record["task_results"][0]["output_retention"])
        self.assertEqual(2, record["usage"]["model_calls"])
        self.assertIsNone(record["final_output"])
        self.assertEqual(
            record, validate_execution_record(record, plan_value=execution_plan)
        )

        forged_reference = json.loads(canonical_json(record))
        forged_reference["task_results"][0]["output"]["digest"] = (
            "sha256:" + "f" * 64
        )
        with self.assertRaises(DomainError):
            validate_execution_record(
                rehash(forged_reference), plan_value=execution_plan
            )

    def test_record_capacity_fallback_can_retain_terminal_rejection(self) -> None:
        blob_schema = {
            "type": "object",
            "required": ["blob"],
            "properties": {"blob": {"type": "string"}},
            "additionalProperties": False,
        }
        execution_plan = create_execution_plan(
            {"statement": "Retain a bounded rejection trace", "context": {}},
            [
                {
                    "task_id": "rejected-capacity-task",
                    "role": "fixture",
                    "objective": "Return the wrong large value",
                    "context": {},
                    "depends_on": [],
                    "output_schema": blob_schema,
                    "accept_spec": equals_spec("/blob", "expected"),
                }
            ],
            {
                "objective": "Must not run",
                "context": {},
                "output_schema": ANSWER_SCHEMA,
                "accept_spec": exists_spec("/answer"),
            },
        )
        adapter = ScriptedAdapter(
            [
                response(
                    {"blob": "wrong" * 4_000},
                    response_id="rejected-capacity-task",
                    output_tokens=1,
                )
            ]
        )
        original_limit = execution_module.MAX_EXECUTION_RECORD_BYTES
        try:
            execution_module.MAX_EXECUTION_RECORD_BYTES = 15_000
            record = SingleModelBackend(adapter, limits=limits()).execute(
                execution_plan, run_id="run-rejected-record-capacity"
            )
            validated = validate_execution_record(
                record, plan_value=execution_plan
            )
        finally:
            execution_module.MAX_EXECUTION_RECORD_BYTES = original_limit

        self.assertEqual("failed", validated["status"])
        self.assertEqual("record", validated["stop_reason"])
        self.assertEqual(
            "contract_rejected", validated["task_results"][-1]["status"]
        )
        self.assertEqual(
            "digest_only", validated["task_results"][-1]["output_retention"]
        )

    def test_digest_only_gate_binding_handles_literal_reference_shaped_values(self) -> None:
        literal_reference = {
            "kind": "content_digest_reference",
            "digest": "sha256:" + "a" * 64,
            "canonical_bytes": 123,
        }
        execution_plan = create_execution_plan(
            {"statement": "Preserve a literal reference-shaped value", "context": {}},
            [
                {
                    "task_id": "literal-reference-task",
                    "role": "fixture",
                    "objective": "Return the exact literal plus a large payload",
                    "context": {},
                    "depends_on": [],
                    "output_schema": {
                        "type": "object",
                        "required": ["proof", "blob"],
                        "properties": {
                            "proof": {"type": "object"},
                            "blob": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    "accept_spec": {
                        "seam": "hard",
                        "minimum_pass_rate": 1.0,
                        "clauses": [
                            {
                                "id": "literal-proof",
                                "path": "/proof",
                                "operator": "equals",
                                "expected": literal_reference,
                                "critical": True,
                                "disclosure": "hidden",
                            }
                        ],
                    },
                }
            ],
            {
                "objective": "Return the answer",
                "context": {},
                "output_schema": ANSWER_SCHEMA,
                "accept_spec": exists_spec("/answer"),
            },
        )
        adapter = ScriptedAdapter(
            [
                response(
                    {"proof": literal_reference, "blob": "x" * 20_000},
                    response_id="literal-reference-task",
                    output_tokens=1,
                ),
                response({"answer": "done"}, response_id="literal-reference-final"),
            ]
        )
        original_limit = execution_module.MAX_EXECUTION_RECORD_BYTES
        try:
            execution_module.MAX_EXECUTION_RECORD_BYTES = 15_000
            record = SingleModelBackend(adapter, limits=limits()).execute(
                execution_plan, run_id="run-literal-reference-capacity"
            )
            validated = validate_execution_record(record, plan_value=execution_plan)
        finally:
            execution_module.MAX_EXECUTION_RECORD_BYTES = original_limit

        self.assertEqual("failed", validated["status"])
        self.assertEqual("record", validated["stop_reason"])

    def test_incomplete_usage_fails_closed_and_marks_accounting_unknown(self) -> None:
        adapter = ScriptedAdapter(
            [
                response(
                    {"value": 1},
                    response_id="incomplete-usage",
                    complete=False,
                )
            ]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            plan(), run_id="run-incomplete-usage"
        )

        self.assertEqual("failed", record["status"])
        self.assertEqual("incomplete_execution_usage", record["error"]["code"])
        self.assertFalse(record["accounting_complete"])
        self.assertEqual(0, record["usage"]["model_calls"])

    def test_adapter_exception_is_sanitized(self) -> None:
        adapter = ScriptedAdapter(
            [RuntimeError("sk-test-secret should never reach the run record")]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            plan(), run_id="run-adapter-error"
        )

        self.assertEqual("failed", record["status"])
        self.assertFalse(record["accounting_complete"])
        self.assertEqual("RuntimeError", record["error"]["error_type"])
        self.assertNotIn("sk-test-secret", canonical_json(record))

        domain_adapter = ScriptedAdapter(
            [DomainError("provider_error", "sk-domain-secret must remain private")]
        )
        domain_record = SingleModelBackend(
            domain_adapter, limits=limits()
        ).execute(plan(), run_id="run-domain-adapter-error")
        self.assertEqual("model_adapter_error", domain_record["error"]["code"])
        self.assertNotIn("sk-domain-secret", canonical_json(domain_record))

    def test_malformed_output_with_valid_usage_retains_known_cost(self) -> None:
        malformed = response({"value": 1}, response_id="malformed")
        malformed["output"] = "not-an-object"
        adapter = ScriptedAdapter([malformed])

        record = SingleModelBackend(adapter, limits=limits()).execute(
            plan(), run_id="run-malformed-output"
        )

        self.assertEqual("failed", record["status"])
        self.assertEqual("invalid_field", record["error"]["code"])
        self.assertEqual(1, record["usage"]["model_calls"])
        self.assertTrue(record["accounting_complete"])
        self.assertEqual(record, validate_execution_record(record, plan_value=plan()))

    def test_malformed_over_budget_response_terminates_as_budget_failure(self) -> None:
        malformed = response(
            {"value": 1},
            response_id="malformed-overrun",
            output_tokens=2_000,
        )
        malformed["output"] = "not-an-object"
        adapter = ScriptedAdapter([malformed])

        record = SingleModelBackend(
            adapter, limits=limits(output_tokens=1_000)
        ).execute(plan(), run_id="run-malformed-overrun")

        self.assertEqual("budget_exceeded", record["status"])
        self.assertEqual(2_000, record["usage"]["output_tokens"])
        self.assertTrue(record["accounting_complete"])
        self.assertEqual(record, validate_execution_record(record, plan_value=plan()))

    def test_record_is_content_addressed_and_defensively_copied(self) -> None:
        branch_response = response({"value": 1}, response_id="branch")
        final_response = response({"answer": "stable"}, response_id="final")
        adapter = ScriptedAdapter([branch_response, final_response])
        execution_plan = plan()
        record = SingleModelBackend(adapter, limits=limits()).execute(
            execution_plan, run_id="run-copy"
        )

        branch_response["output"]["value"] = 999
        final_response["output"]["answer"] = "mutated"
        adapter.identity["model"] = "mutated-model"
        execution_plan["problem"]["context"]["customer"] = "mutated"
        self.assertEqual(1, record["task_results"][0]["output"]["value"])
        self.assertEqual("stable", record["final_output"]["answer"])
        self.assertEqual("fixture-model-v1", record["backend"]["model_adapter"]["model"])

        record["final_output"]["answer"] = "tampered"
        with self.assertRaises(DomainError):
            validate_execution_record(record, plan_value=plan())

    def test_unknown_record_fields_are_not_silently_dropped_or_unbounded(self) -> None:
        execution_plan = plan()

        def completed_record(run_id: str) -> dict[str, Any]:
            return SingleModelBackend(
                ScriptedAdapter(
                    [
                        response({"value": 1}, response_id="branch"),
                        response({"answer": "stable"}, response_id="final"),
                    ]
                ),
                limits=limits(),
            ).execute(execution_plan, run_id=run_id)

        mutations = [
            lambda value: value.update(
                {"payment_authorized": {"asset": "USDC"}}
            ),
            lambda value: value["task_results"][0].update(
                {"payment_authorized": True}
            ),
            lambda value: value["task_results"][0]["verification"].update(
                {"payment_authorized": True}
            ),
            lambda value: value["task_results"][0]["verification"]["clauses"][
                0
            ].update({"payment_authorized": True}),
        ]
        for index, mutate in enumerate(mutations):
            record = completed_record(f"run-unknown-field-{index}")
            mutate(record)
            with self.assertRaisesRegex(DomainError, "unsupported fields"):
                validate_execution_record(record, plan_value=execution_plan)

        oversized = completed_record("run-oversized-unknown-field")
        oversized["ignored"] = "x" * execution_module.MAX_EXECUTION_RECORD_BYTES
        with self.assertRaisesRegex(DomainError, "at most"):
            validate_execution_record(oversized, plan_value=execution_plan)

    def test_public_backend_identity_cannot_rewrite_executor_provenance(self) -> None:
        adapter = ScriptedAdapter(
            [
                response({"value": 1}, response_id="branch"),
                response({"answer": "stable"}, response_id="final"),
            ]
        )
        backend = SingleModelBackend(adapter, limits=limits())
        exposed_identity = backend.identity
        exposed_identity["model_adapter"]["model"] = "mutated-public-copy"
        adapter.identity["model"] = "mutated-adapter-after-init"

        record = backend.execute(plan(), run_id="run-identity-copy")

        self.assertEqual(
            "fixture-model-v1", record["backend"]["model_adapter"]["model"]
        )
        self.assertTrue(
            all(
                item["executor"]["identity"]["model"] == "fixture-model-v1"
                for item in record["task_results"]
            )
        )

    def test_rehashed_impossible_records_fail_semantic_validation(self) -> None:
        def completed_record(run_id: str) -> dict[str, Any]:
            return SingleModelBackend(
                ScriptedAdapter(
                    [
                        response({"value": 1}, response_id="branch"),
                        response({"answer": "stable"}, response_id="final"),
                    ]
                ),
                limits=limits(),
            ).execute(plan(), run_id=run_id)

        missing_tasks = json.loads(canonical_json(completed_record("run-missing")))
        missing_tasks["task_results"] = []
        with self.assertRaises(DomainError):
            validate_execution_record(rehash(missing_tasks), plan_value=plan())

        wrong_pair = json.loads(canonical_json(completed_record("run-pair")))
        wrong_pair["status"] = "budget_exceeded"
        with self.assertRaisesRegex(DomainError, "inconsistent"):
            validate_execution_record(rehash(wrong_pair), plan_value=plan())

        fake_usage = json.loads(canonical_json(completed_record("run-usage")))
        fake_usage["usage"]["input_tokens"] = 999_999
        with self.assertRaisesRegex(DomainError, "Aggregate usage"):
            validate_execution_record(rehash(fake_usage), plan_value=plan())

        task_usage = json.loads(canonical_json(completed_record("run-task-usage")))
        task_usage["task_results"][0]["usage"]["input_tokens"] = 999
        with self.assertRaises(DomainError):
            validate_execution_record(rehash(task_usage), plan_value=plan())

        missing_events = json.loads(canonical_json(completed_record("run-events")))
        missing_events["events"] = []
        missing_events["usage"] = {
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "monetary_microunits": 0,
        }
        with self.assertRaises(DomainError):
            validate_execution_record(rehash(missing_events), plan_value=plan())

        shallow_gate = json.loads(canonical_json(completed_record("run-gate")))
        shallow_gate["final_verification"] = {"outcome": "pass"}
        with self.assertRaisesRegex(DomainError, "seam"):
            validate_execution_record(rehash(shallow_gate), plan_value=plan())

        contradictory_gate = json.loads(
            canonical_json(completed_record("run-contradictory-gate"))
        )
        verification = contradictory_gate["final_verification"]
        verification["clauses"][0]["passed"] = False
        verification["pass_rate"] = 0.0
        with self.assertRaises(DomainError):
            validate_execution_record(rehash(contradictory_gate), plan_value=plan())

    def test_record_validation_binds_gates_to_the_trusted_plan(self) -> None:
        execution_plan = plan()

        def completed_record(run_id: str) -> dict[str, Any]:
            return SingleModelBackend(
                ScriptedAdapter(
                    [
                        response({"value": 1}, response_id="branch"),
                        response({"answer": "stable"}, response_id="final"),
                    ]
                ),
                limits=limits(),
            ).execute(execution_plan, run_id=run_id)

        below_threshold = completed_record("run-below-threshold")
        verification = below_threshold["final_verification"]
        verification["clauses"].append(
            {
                "clause_id": "invented-noncritical-check",
                "path": "/answer",
                "operator": "exists",
                "critical": False,
                "disclosure": "public",
                "passed": False,
                "expected": None,
                "observed": None,
                "observed_missing": True,
            }
        )
        verification["pass_rate"] = 0.5
        with self.assertRaises(DomainError):
            validate_execution_record(
                rehash(below_threshold), plan_value=execution_plan
            )

        replaced_clause = completed_record("run-replaced-clause")
        replaced_clause["task_results"][0]["verification"]["clauses"][0][
            "clause_id"
        ] = "invented-easier-clause"
        with self.assertRaises(DomainError):
            validate_execution_record(
                rehash(replaced_clause), plan_value=execution_plan
            )

        unrelated_plan = plan()
        unrelated_plan["seed"] = 74
        unrelated_plan = create_execution_plan(
            unrelated_plan["problem"],
            unrelated_plan["tasks"],
            unrelated_plan["synthesis"],
            source=unrelated_plan["source"],
            seed=74,
        )
        with self.assertRaisesRegex(DomainError, "supplied plan"):
            validate_execution_record(
                completed_record("run-wrong-plan"), plan_value=unrelated_plan
            )

    def test_execution_trace_is_bound_to_plan_work_and_order(self) -> None:
        execution_plan = plan()

        def completed_record(run_id: str) -> dict[str, Any]:
            return SingleModelBackend(
                ScriptedAdapter(
                    [
                        response({"value": 1}, response_id="branch"),
                        response({"answer": "stable"}, response_id="final"),
                    ]
                ),
                limits=limits(),
            ).execute(execution_plan, run_id=run_id)

        ghost_call = completed_record("run-ghost-call")
        task_start = next(
            event
            for event in ghost_call["events"]
            if event["event"] == "backend_execution_started"
            and event["purpose"] == "task"
        )
        task_completed = next(
            event
            for event in ghost_call["events"]
            if event["event"] == "backend_execution_completed"
            and event["purpose"] == "task"
        )
        forged_start = json.loads(canonical_json(task_start))
        forged_completed = json.loads(canonical_json(task_completed))
        forged_start["work_id"] = "ghost"
        forged_completed["work_id"] = "ghost"
        ghost_call["events"][-1:-1] = [forged_start, forged_completed]
        for sequence, event in enumerate(ghost_call["events"], start=1):
            event["sequence"] = sequence
        ghost_call["usage"]["model_calls"] += 1
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "monetary_microunits",
        ):
            ghost_call["usage"][name] += forged_completed["usage"][name]
        with self.assertRaisesRegex(DomainError, "outside the supplied plan"):
            validate_execution_record(rehash(ghost_call), plan_value=execution_plan)

        synthesis_first = completed_record("run-synthesis-first")
        task_events = [
            event
            for event in synthesis_first["events"]
            if event.get("purpose") == "task"
            or event["event"] == "task_contract_evaluated"
        ]
        synthesis_events = [
            event
            for event in synthesis_first["events"]
            if event.get("purpose") == "synthesis"
            or event["event"] == "synthesis_contract_evaluated"
        ]
        completion_events = [
            event
            for event in synthesis_first["events"]
            if event["event"] == "run_completed"
        ]
        synthesis_first["events"] = [
            *synthesis_events,
            *task_events,
            *completion_events,
        ]
        for sequence, event in enumerate(synthesis_first["events"], start=1):
            event["sequence"] = sequence
        with self.assertRaises(DomainError):
            validate_execution_record(
                rehash(synthesis_first), plan_value=execution_plan
            )

        dependency_plan = plan(
            [
                task("second", depends_on=["first"], expected=2),
                task("first", expected=1),
            ]
        )
        dependency_record = SingleModelBackend(
            ScriptedAdapter(
                [
                    response({"value": 1}, response_id="first"),
                    response({"value": 2}, response_id="second"),
                    response({"answer": "stable"}, response_id="final"),
                ]
            ),
            limits=limits(),
        ).execute(dependency_plan, run_id="run-dependent-task-started-early")
        second_start = next(
            event
            for event in dependency_record["events"]
            if event["event"] == "backend_execution_started"
            and event["work_id"] == "second"
        )
        dependency_record["events"].remove(second_start)
        first_start_index = next(
            index
            for index, event in enumerate(dependency_record["events"])
            if event["event"] == "backend_execution_started"
            and event["work_id"] == "first"
        )
        dependency_record["events"].insert(first_start_index + 1, second_start)
        for sequence, event in enumerate(dependency_record["events"], start=1):
            event["sequence"] = sequence
        with self.assertRaisesRegex(DomainError, "preceding planned contract"):
            validate_execution_record(
                rehash(dependency_record), plan_value=dependency_plan
            )

        missing_completion = completed_record("run-missing-completion-event")
        missing_completion["events"] = [
            event
            for event in missing_completion["events"]
            if event["event"] != "run_completed"
        ]
        for sequence, event in enumerate(missing_completion["events"], start=1):
            event["sequence"] = sequence
        with self.assertRaisesRegex(DomainError, "run-completed"):
            validate_execution_record(
                rehash(missing_completion), plan_value=execution_plan
            )

    def test_unmetered_failure_cannot_claim_complete_accounting(self) -> None:
        execution_plan = plan()
        record = SingleModelBackend(
            ScriptedAdapter([RuntimeError("provider unavailable")]),
            limits=limits(),
        ).execute(execution_plan, run_id="run-accounting-forgery")
        self.assertFalse(record["accounting_complete"])

        record["accounting_complete"] = True
        with self.assertRaisesRegex(DomainError, "incomplete accounting"):
            validate_execution_record(rehash(record), plan_value=execution_plan)

    def test_scalar_output_schema_is_rejected_at_plan_creation(self) -> None:
        invalid_task = task("scalar")
        invalid_task["output_schema"] = {"type": "string"}
        with self.assertRaisesRegex(DomainError, "object root"):
            plan([invalid_task])

    def test_protocol_clause_ids_are_reserved_for_task_and_synthesis_checks(self) -> None:
        reserved_spec = {
            "clauses": [
                {
                    "id": "protocol:output-schema",
                    "path": "/value",
                    "operator": "exists",
                    "critical": True,
                    "disclosure": "public",
                }
            ]
        }
        invalid_task = task("reserved-task")
        invalid_task["accept_spec"] = reserved_spec
        with self.assertRaisesRegex(DomainError, "reserved"):
            plan([invalid_task])

        with self.assertRaisesRegex(DomainError, "reserved"):
            create_execution_plan(
                {"statement": "Reserved synthesis clause", "context": {}},
                [task("valid-task")],
                {
                    "objective": "Invalid synthesis gate",
                    "context": {},
                    "output_schema": ANSWER_SCHEMA,
                    "accept_spec": reserved_spec,
                },
            )

    def test_accept_spec_bounds_match_execution_record_bounds(self) -> None:
        long_id_task = task("long-id")
        long_id_task["accept_spec"] = {
            "clauses": [
                {
                    "id": "x" * 201,
                    "path": "/value",
                    "operator": "exists",
                }
            ]
        }
        with self.assertRaisesRegex(DomainError, "at most 200"):
            plan([long_id_task])

        with self.assertRaisesRegex(DomainError, "at most 10000"):
            create_execution_plan(
                {"statement": "Reject an unrecordable path", "context": {}},
                [task("bounded")],
                {
                    "objective": "Invalid synthesis gate path",
                    "context": {},
                    "output_schema": ANSWER_SCHEMA,
                    "accept_spec": exists_spec("/" + "x" * 10_000),
                },
            )

    def test_scripted_runs_are_byte_reproducible(self) -> None:
        def run() -> dict[str, Any]:
            return SingleModelBackend(
                ScriptedAdapter(
                    [
                        response({"value": 1}, response_id="branch"),
                        response({"answer": "stable"}, response_id="final"),
                    ]
                ),
                limits=limits(),
            ).execute(plan(), run_id="run-reproducible")

        self.assertEqual(run(), run())

    def test_common_record_accepts_network_executor_provenance(self) -> None:
        execution_plan = plan()
        record = SingleModelBackend(
            ScriptedAdapter(
                [
                    response({"value": 1}, response_id="branch"),
                    response({"answer": "stable"}, response_id="final"),
                ]
            ),
            limits=limits(),
        ).execute(execution_plan, run_id="run-network-envelope")
        record["backend"] = {
            "backend_id": "coordinator_network",
            "backend_version": "1",
            "mode": "network",
        }
        record["limits"] = {"currency": "USD", "funded_amount_minor": 500}

        task_executor = {
            "kind": "network_node",
            "identity": {
                "node_id": "node-fixture",
                "offering_id": "offering-fixture",
                "capability_digest": "sha256:" + "1" * 64,
            },
        }
        synthesis_executor = {
            "kind": "network_node",
            "identity": {
                "node_id": "node-synthesis",
                "offering_id": "offering-synthesis",
                "capability_digest": "sha256:" + "2" * 64,
            },
        }
        task_reference = {
            "kind": "coordinator_submission",
            "problem_id": "problem-fixture",
            "task_id": "task-fixture",
            "submission_id": "submission-fixture",
        }
        synthesis_reference = {
            "kind": "coordinator_submission",
            "problem_id": "problem-synthesis",
            "task_id": "task-synthesis",
            "submission_id": "submission-synthesis",
        }
        record["task_results"][0]["executor"] = task_executor
        record["task_results"][0]["backend_reference"] = task_reference
        record["synthesis_result"]["executor"] = synthesis_executor
        record["synthesis_result"]["backend_reference"] = synthesis_reference
        for event in record["events"]:
            if not event["event"].startswith("backend_"):
                continue
            if event["purpose"] == "task":
                event["executor"] = task_executor
                event["backend_reference"] = task_reference
            else:
                event["executor"] = synthesis_executor
                event["backend_reference"] = synthesis_reference

        network_record = validate_execution_record(
            rehash(record), plan_value=execution_plan
        )
        self.assertEqual("network", network_record["backend"]["mode"])
        self.assertEqual(
            "node-fixture",
            network_record["task_results"][0]["executor"]["identity"]["node_id"],
        )

        forged_task_output = json.loads(canonical_json(network_record))
        forged_task_output["task_results"][0]["output"]["value"] = 999
        with self.assertRaises(DomainError):
            validate_execution_record(
                rehash(forged_task_output), plan_value=execution_plan
            )

        forged_final_output = json.loads(canonical_json(network_record))
        forged_final_output["final_output"]["answer"] = "rewritten"
        with self.assertRaises(DomainError):
            validate_execution_record(
                rehash(forged_final_output), plan_value=execution_plan
            )

    def test_synthesis_contract_rejection_is_terminal(self) -> None:
        execution_plan = create_execution_plan(
            {"statement": "Check synthesis", "context": {}},
            [task("analysis", expected=1)],
            {
                "objective": "Produce an exact final answer",
                "context": {},
                "output_schema": ANSWER_SCHEMA,
                "accept_spec": equals_spec("/answer", "expected"),
            },
        )
        adapter = ScriptedAdapter(
            [
                response({"value": 1}, response_id="branch"),
                response({"answer": "wrong"}, response_id="final"),
            ]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            execution_plan, run_id="run-synthesis-rejected"
        )

        self.assertEqual("rejected", record["status"])
        self.assertEqual("reject", record["final_verification"]["outcome"])
        self.assertEqual(
            record, validate_execution_record(record, plan_value=execution_plan)
        )

    def test_large_repeated_gate_observations_are_compacted_into_bounded_record(self) -> None:
        blob_schema = {
            "type": "object",
            "required": ["blob"],
            "properties": {"blob": {"type": "string"}},
            "additionalProperties": False,
        }
        repeated_gate = {
            "seam": "hard",
            "minimum_pass_rate": 1.0,
            "clauses": [
                {
                    "id": f"whole-object-{index}",
                    "path": "",
                    "operator": "exists",
                    "critical": True,
                    "disclosure": "public",
                }
                for index in range(7)
            ],
        }
        execution_plan = create_execution_plan(
            {"statement": "Bound repeated gate evidence", "context": {}},
            [
                {
                    "task_id": "large-gate",
                    "role": "fixture",
                    "objective": "Return a large object",
                    "context": {},
                    "depends_on": [],
                    "output_schema": blob_schema,
                    "accept_spec": repeated_gate,
                }
            ],
            {
                "objective": "Return a compact answer",
                "context": {},
                "output_schema": ANSWER_SCHEMA,
                "accept_spec": exists_spec("/answer"),
            },
        )
        adapter = ScriptedAdapter(
            [
                response(
                    {"blob": "x" * 300_000},
                    response_id="large-gate-response",
                    output_tokens=1,
                ),
                response({"answer": "bounded"}, response_id="bounded-synthesis"),
            ]
        )

        record = SingleModelBackend(adapter, limits=limits()).execute(
            execution_plan, run_id="run-large-gate"
        )

        self.assertEqual("completed", record["status"])
        observed = record["task_results"][0]["verification"]["clauses"][0][
            "observed"
        ]
        self.assertEqual("content_digest_reference", observed["kind"])
        self.assertLess(len(canonical_json(record).encode("utf-8")), 2 * 1024 * 1024)
        self.assertEqual(
            record, validate_execution_record(record, plan_value=execution_plan)
        )

    def test_function_adapter_is_a_valid_provider_neutral_boundary(self) -> None:
        calls = 0

        def invoke(request_value: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            output = {"value": 1} if request_value["purpose"] == "task" else {"answer": "ok"}
            return response(output, response_id=f"function-{calls}")

        adapter = FunctionModelAdapter(
            identity={
                "adapter_id": "fixture:function",
                "adapter_version": "1",
                "model": "callable-model",
            },
            function=invoke,
        )
        record = SingleModelBackend(adapter, limits=limits()).execute(
            plan(), run_id="run-function"
        )
        self.assertEqual("completed", record["status"])
        self.assertEqual(2, calls)

    def test_decomposition_compiler_requires_review_of_exact_clean_proposal(self) -> None:
        proposal = ConceptualDecompositionEngine(
            CleanOracle(),
            config=DecompositionConfig(max_depth=0),
        ).decompose(concept(), routing_probes=fixed_routing_probes())
        bindings = [
            {
                "axis_id": axis["axis_id"],
                "objective": f"Work the {axis['candidate']['name']} dimension",
                "context": {},
                "output_schema": OBJECT_SCHEMA,
                "accept_spec": exists_spec(""),
            }
            for axis in proposal["root"]["axes"]
        ]
        synthesis = {
            "objective": "Synthesize the dimensions",
            "context": {},
            "output_schema": ANSWER_SCHEMA,
            "accept_spec": exists_spec("/answer"),
        }
        review = {
            "decision": "approved",
            "proposal_digest": proposal["proposal_digest"],
            "reviewer": "fixture:human-reviewer",
            "basis": "Inspected the exact root axes and held-out routes",
        }

        execution_plan = execution_plan_from_decomposition(
            proposal,
            problem={"statement": "Reliable work", "context": {}},
            axis_bindings=bindings,
            synthesis=synthesis,
            review=review,
            seed=7,
        )
        self.assertEqual(
            proposal["proposal_digest"],
            execution_plan["source"]["proposal_digest"],
        )
        self.assertEqual(
            [axis["axis_id"] for axis in proposal["root"]["axes"]],
            execution_plan["task_order"],
        )
        self.assertEqual(
            "reviewed_root_axes_only",
            execution_plan["source"]["planning_scope"],
        )

        wrong_review = dict(review, proposal_digest="sha256:" + "0" * 64)
        with self.assertRaisesRegex(DomainError, "exact decomposition proposal"):
            execution_plan_from_decomposition(
                proposal,
                problem={"statement": "Reliable work", "context": {}},
                axis_bindings=bindings,
                synthesis=synthesis,
                review=wrong_review,
            )

        generated_probe_proposal = ConceptualDecompositionEngine(
            CleanOracle(),
            config=DecompositionConfig(max_depth=0),
        ).decompose(concept())
        generated_review = dict(
            review, proposal_digest=generated_probe_proposal["proposal_digest"]
        )
        with self.assertRaisesRegex(DomainError, "caller-supplied"):
            execution_plan_from_decomposition(
                generated_probe_proposal,
                problem={"statement": "Reliable work", "context": {}},
                axis_bindings=[
                    dict(binding, axis_id=axis["axis_id"])
                    for binding, axis in zip(
                        bindings, generated_probe_proposal["root"]["axes"]
                    )
                ],
                synthesis=synthesis,
                review=generated_review,
            )

        depth_limited_proposal = ConceptualDecompositionEngine(
            CleanOracle(recurse=True),
            config=DecompositionConfig(max_depth=0),
        ).decompose(concept(), routing_probes=fixed_routing_probes())
        depth_limited_bindings = [
            dict(binding, axis_id=axis["axis_id"])
            for binding, axis in zip(
                bindings, depth_limited_proposal["root"]["axes"]
            )
        ]
        depth_limited_plan = execution_plan_from_decomposition(
            depth_limited_proposal,
            problem={"statement": "Reliable work", "context": {}},
            axis_bindings=depth_limited_bindings,
            synthesis=synthesis,
            review=dict(
                review, proposal_digest=depth_limited_proposal["proposal_digest"]
            ),
        )
        self.assertTrue(
            any(
                limitation.endswith(":maximum_depth")
                for limitation in depth_limited_plan["source"][
                    "planning_limitations"
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
