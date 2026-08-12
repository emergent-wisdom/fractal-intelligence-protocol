from __future__ import annotations

import unittest

from fractal_protocol.errors import DomainError
from fractal_protocol.gates import evaluate_result
from fractal_protocol.protocol import content_digest, validate_accept_spec
from fractal_protocol.protocol import validate_manifest


class GateTests(unittest.TestCase):
    def test_deterministic_operators_and_json_pointer(self) -> None:
        spec = {
            "seam": "hard",
            "minimum_pass_rate": 1,
            "clauses": [
                {"id": "a", "path": "/nested/value", "operator": "exists", "critical": True},
                {"id": "b", "path": "/nested/value", "operator": "equals", "expected": 7, "critical": True},
                {"id": "c", "path": "/nested/value", "operator": "type", "expected": "integer", "critical": True},
                {"id": "d", "path": "/nested/value", "operator": "minimum", "expected": 5, "critical": True},
                {"id": "e", "path": "/nested/value", "operator": "maximum", "expected": 9, "critical": True},
                {"id": "f", "path": "/tags", "operator": "contains", "expected": "verified", "critical": True},
                {"id": "g", "path": "/a~1b/~0key", "operator": "equals", "expected": "ok", "critical": True},
            ],
        }
        decision = evaluate_result(
            spec,
            outputs={
                "nested": {"value": 7},
                "tags": ["verified"],
                "a/b": {"~key": "ok"},
            },
            status="success",
            stop_reason="completed",
        )
        self.assertEqual("pass", decision["outcome"])
        self.assertEqual(1.0, decision["pass_rate"])

    def test_critical_failure_is_non_compensatory(self) -> None:
        decision = evaluate_result(
            {
                "seam": "hard",
                "minimum_pass_rate": 0.1,
                "clauses": [
                    {"id": "critical", "path": "/answer", "operator": "equals", "expected": 42, "critical": True},
                    {"id": "noncritical", "path": "/format", "operator": "equals", "expected": "json", "critical": False},
                ],
            },
            outputs={"answer": 41, "format": "json"},
            status="success",
            stop_reason="completed",
        )
        self.assertEqual("reject", decision["outcome"])
        self.assertEqual("critical", decision["failure_trace"]["violations"][0]["clause_id"])

    def test_json_equality_does_not_conflate_booleans_and_numbers(self) -> None:
        decision = evaluate_result(
            {
                "clauses": [
                    {
                        "id": "nested",
                        "path": "/answer",
                        "operator": "equals",
                        "expected": {"value": 1},
                        "critical": True,
                    }
                ]
            },
            outputs={"answer": {"value": True}},
            status="success",
            stop_reason="completed",
        )
        self.assertEqual("reject", decision["outcome"])

    def test_partial_result_never_earns_automatically(self) -> None:
        decision = evaluate_result(
            {
                "clauses": [
                    {"id": "answer", "path": "/answer", "operator": "equals", "expected": 42, "critical": True}
                ]
            },
            outputs={"answer": 42},
            status="partial",
            stop_reason="budget",
        )
        self.assertEqual("reject", decision["outcome"])
        self.assertEqual("protocol:completed-successfully", decision["clauses"][0]["clause_id"])

    def test_manifest_digest_is_canonical(self) -> None:
        left = {"b": 2, "a": 1}
        right = {"a": 1, "b": 2}
        self.assertEqual(content_digest(left), content_digest(right))

    def test_invalid_type_clause_is_rejected(self) -> None:
        with self.assertRaises(DomainError):
            validate_accept_spec(
                {
                    "clauses": [
                        {"id": "bad", "path": "/x", "operator": "type", "expected": "float", "critical": True}
                    ]
                }
            )

    def test_v1_manifest_has_one_operation_and_supported_schema(self) -> None:
        base = {
            "concept_ref": "urn:test:manifest",
            "name": "Manifest",
            "description": "A test manifest",
            "cognitive_mode": "convergent",
            "operations": ["one", "two"],
            "surfaces": ["manifest", "execute"],
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        }
        with self.assertRaisesRegex(DomainError, "one operation"):
            validate_manifest(base)
        base["operations"] = ["one"]
        base["output_schema"] = {"oneOf": [{"type": "object"}]}
        with self.assertRaisesRegex(DomainError, "does not support"):
            validate_manifest(base)
        base["output_schema"] = {"type": "object"}
        base["surfaces"] = ["manifest", "execute", "verify"]
        with self.assertRaisesRegex(DomainError, "only transports"):
            validate_manifest(base)

    def test_payable_v1_rejects_undefined_soft_seam_semantics(self) -> None:
        with self.assertRaisesRegex(DomainError, "hard seams only"):
            validate_accept_spec(
                {
                    "seam": "soft",
                    "clauses": [
                        {
                            "id": "shape",
                            "path": "/answer",
                            "operator": "exists",
                            "critical": False,
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
