from __future__ import annotations

import tempfile
import unittest
import sqlite3
import threading
from pathlib import Path

from fractal_protocol.database import BASE_SCHEMA, Database
from fractal_protocol.errors import DomainError
from fractal_protocol.protocol import content_digest
from fractal_protocol.service import CoordinatorService, ServiceConfig

from tests.helpers import MutableClock, manifest, task_spec


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.lease_request_counter = 0
        self.service = CoordinatorService(
            Database(Path(self.temporary.name) / "test.db"),
            config=ServiceConfig(
                lease_seconds=30,
                platform_fee_bps=1000,
                max_depth=5,
                max_inflight_per_node=3,
            ),
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register_with_offering(
        self, concept: str, name: str, operation: str
    ) -> tuple[dict, dict]:
        node = self.register_node(name)
        offering = self.publish_offering(
            node["node_token"], manifest(concept, name, operation)
        )
        return node, offering

    def publish_offering(self, node_token: str, solver_manifest: dict) -> dict:
        offering = self.service.publish_offering(node_token, solver_manifest)
        approved = self.service.approve_offering(offering["offering_id"])
        return {**offering, **approved}

    def lease_work(
        self, node_token: str, lease_request_id: str | None = None
    ) -> dict | None:
        if lease_request_id is None:
            self.lease_request_counter += 1
            lease_request_id = f"test-poll-{self.lease_request_counter}"
        return self.service.lease_work(node_token, lease_request_id)

    def register_node(self, name: str) -> dict:
        invite = self.service.create_node_invite(
            {"label": f"invite for {name}", "expires_in_seconds": 3600}
        )
        return self.service.register_node(
            invite["invite_token"],
            {
                "registration_id": f"registration-{name}",
                "operator_name": name,
                "metadata": {},
            },
        )

    def create_problem(
        self,
        capability: str,
        operation: str,
        *,
        expected: object,
        reward: int = 500,
        funded: int = 1000,
        max_attempts: int = 3,
        delegation_budget: int = 0,
    ) -> dict:
        return self.service.create_problem(
            {
                "intent": "Solve the deterministic test",
                "problem_class": "objective.test",
                "funded_amount_minor": funded,
                "currency": "USD",
                "funding_reference": f"funding-{capability}-{self.clock()}",
                "deadline_at": self.clock() + 3600,
                "task": task_spec(
                    capability,
                    operation,
                    inputs={"value": 21},
                    expected_path="/answer",
                    expected=expected,
                    reward=reward,
                    delegation_budget=delegation_budget,
                    constraints={"workflow_scope": "task_only"},
                    max_attempts=max_attempts,
                ),
            }
        )

    def test_reject_retry_accept_and_credit_exactly_once(self) -> None:
        node, offering = self.register_with_offering(
            "urn:test:double", "Double", "double"
        )
        second = self.publish_offering(
            node["node_token"], manifest("urn:test:triple", "Triple", "triple")
        )
        self.assertNotEqual(offering["offering_id"], second["offering_id"])
        alternate = self.register_node("alternate-double")
        alternate_offering = self.publish_offering(
            alternate["node_token"],
            manifest("urn:test:double", "Double", "double"),
        )
        self.assertEqual(
            offering["manifest_digest"], alternate_offering["manifest_digest"]
        )

        problem = self.create_problem(offering["manifest_digest"], "double", expected=42)
        lease = self.lease_work(node["node_token"])
        self.assertEqual(problem["tasks"][0]["task_id"], lease["task"]["task_id"])
        self.assertEqual(
            {
                "currency": "USD",
                "gross_reward_minor": 500,
                "platform_fee_bps": 1000,
                "provider_earning_minor": 450,
            },
            lease["compensation_quote"],
        )
        self.assertNotIn("expected", lease["task"]["accept_spec"]["clauses"][0])
        self.assertEqual(
            "hidden", lease["task"]["accept_spec"]["clauses"][0]["disclosure"]
        )

        rejected_body = {
            "submission_id": "attempt-wrong",
            "lease_token": lease["lease_token"],
            "status": "success",
            "stop_reason": "completed",
            "outputs": {"answer": 41},
            "evidence": {},
            "usage": {},
        }
        rejected = self.service.submit_result(
            node["node_token"],
            lease["task"]["task_id"],
            rejected_body,
        )
        self.assertEqual("reject", rejected["gate"]["outcome"])
        self.assertEqual([], rejected["gate"]["clauses"])
        self.assertEqual(1, rejected["gate"]["hidden_clause_count"])
        self.assertNotIn("pass_rate", rejected["gate"])
        self.assertTrue(rejected["gate"]["failure_trace"]["hidden_details_withheld"])
        self.assertEqual([], rejected["gate"]["failure_trace"]["violations"])
        self.assertEqual("open", rejected["task_state"])
        self.assertEqual(0, rejected["earning_minor"])

        self.assertIsNone(self.lease_work(node["node_token"]))
        retry = self.lease_work(alternate["node_token"])
        accepted_body = {
            "submission_id": "attempt-correct",
            "lease_token": retry["lease_token"],
            "status": "success",
            "stop_reason": "completed",
            "outputs": {"answer": 42},
            "evidence": {"test": "exact"},
            "usage": {"duration_ms": 1},
        }
        accepted = self.service.submit_result(
            alternate["node_token"], retry["task"]["task_id"], accepted_body
        )
        repeated = self.service.submit_result(
            alternate["node_token"], retry["task"]["task_id"], accepted_body
        )
        self.assertEqual("pass", accepted["gate"]["outcome"])
        self.assertEqual(450, accepted["earning_minor"])
        self.assertEqual(accepted, repeated)
        self.assertEqual(
            rejected,
            self.service.submit_result(
                node["node_token"], lease["task"]["task_id"], rejected_body
            ),
        )

        self.assertEqual({}, self.service.get_node_earnings(node["node_token"])["balances"])
        earnings = self.service.get_node_earnings(alternate["node_token"])
        self.assertEqual({"USD": 450}, earnings["balances"])
        self.assertEqual("supplier_payable_not_wallet", earnings["classification"])

        final = self.service.get_problem(problem["problem_id"])
        self.assertEqual("completed", final["status"])
        self.assertEqual(
            {"answer": 42}, final["accepted_result"]["result"]["outputs"]
        )
        self.assertEqual(0, final["escrow_balance_minor"])
        self.assertEqual(500, final["refund_pending_minor"])
        reasons = [transfer["reason"] for transfer in final["ledger_transfers"]]
        self.assertEqual(
            [
                "confirmed_platform_funding",
                "accepted_solver_work",
                "platform_fee",
                "unused_funding_refund_pending",
            ],
            reasons,
        )
        pathways = self.service.pathway_summary(offering["manifest_digest"])
        aggregates = pathways["aggregates"]
        self.assertEqual(2, sum(item["invocation_count"] for item in aggregates))
        self.assertEqual(1, sum(item["pass_count"] for item in aggregates))
        self.assertEqual({"USD"}, {item["currency"] for item in aggregates})
        self.assertEqual(2, final["submission_page"]["total"])
        first_page = self.service.get_problem(
            problem["problem_id"], submission_limit=1
        )
        self.assertEqual(1, first_page["submission_page"]["returned"])
        self.assertIsNotNone(first_page["accepted_result"])
        with self.assertRaisesRegex(DomainError, "submission_offset"):
            self.service.get_problem(
                problem["problem_id"], submission_offset=2**63
            )

        changed = dict(accepted_body)
        changed["outputs"] = {"answer": 99}
        with self.assertRaisesRegex(DomainError, "different Result"):
            self.service.submit_result(
                alternate["node_token"], retry["task"]["task_id"], changed
            )

        boolean_changed = dict(accepted_body)
        boolean_changed["outputs"] = {"answer": True}
        with self.assertRaisesRegex(DomainError, "different Result"):
            self.service.submit_result(
                alternate["node_token"], retry["task"]["task_id"], boolean_changed
            )

    def test_recursive_children_preserve_constraints_and_resume_parent(self) -> None:
        node = self.register_node("recursive")
        parent = self.publish_offering(
            node["node_token"], manifest("urn:test:sum", "Sum", "sum")
        )
        child = self.publish_offering(
            node["node_token"], manifest("urn:test:identity", "Identity", "identity")
        )
        problem = self.create_problem(
            parent["manifest_digest"],
            "sum",
            expected=5,
            reward=200,
            funded=1000,
            delegation_budget=600,
            max_attempts=1,
        )
        parent_lease = self.lease_work(node["node_token"])
        inherited = parent_lease["task"]["constraints"]
        children = [
            task_spec(
                child["manifest_digest"],
                "identity",
                inputs={"value": value},
                expected_path="/answer",
                expected=value,
                reward=300,
                constraints={**inherited, "branch": str(value)},
            )
            for value in (2, 3)
        ]
        delegation = self.service.delegate_children(
            node["node_token"],
            parent_lease["task"]["task_id"],
            {
                "delegation_id": "split-once",
                "lease_token": parent_lease["lease_token"],
                "children": children,
            },
        )
        repeated = self.service.delegate_children(
            node["node_token"],
            parent_lease["task"]["task_id"],
            {
                "delegation_id": "split-once",
                "lease_token": parent_lease["lease_token"],
                "children": children,
            },
        )
        self.assertEqual(delegation, repeated)
        self.assertEqual("proposed", delegation["status"])
        self.assertIsNone(self.lease_work(node["node_token"]))
        pending = self.service.list_delegations("proposed")["delegations"]
        self.assertEqual(["split-once"], [item["idempotency_key"] for item in pending])
        approved = self.service.approve_delegation(
            delegation["delegation_id"], {"allow_self_execution": True}
        )
        self.assertEqual("approved", approved["status"])
        self.assertEqual(2, len(approved["child_task_ids"]))
        self.assertEqual(
            approved,
            self.service.approve_delegation(
                delegation["delegation_id"], {"allow_self_execution": True}
            ),
        )

        for index in range(2):
            lease = self.lease_work(node["node_token"])
            expected = lease["task"]["inputs"]["value"]
            response = self.service.submit_result(
                node["node_token"],
                lease["task"]["task_id"],
                {
                    "submission_id": f"child-{index}",
                    "lease_token": lease["lease_token"],
                    "status": "success",
                    "stop_reason": "completed",
                    "outputs": {"answer": expected},
                    "evidence": {},
                    "usage": {},
                },
            )
            self.assertEqual("pass", response["gate"]["outcome"])
            self.assertEqual("available", response["earning_status"])

        # Economic terms are snapshotted on the order, not read from a restarted
        # coordinator's current configuration.
        self.service = CoordinatorService(
            self.service.database,
            config=ServiceConfig(
                lease_seconds=30,
                platform_fee_bps=2000,
                max_depth=5,
                max_inflight_per_node=3,
            ),
            clock=self.clock,
        )

        resumed = self.lease_work(node["node_token"])
        self.assertEqual(parent_lease["task"]["task_id"], resumed["task"]["task_id"])
        self.assertEqual(2, len(resumed["task"]["accepted_child_results"]))
        for child_outcome in resumed["task"]["accepted_child_results"]:
            self.assertEqual("identity", child_outcome["task"]["operation"])
            self.assertIn("value", child_outcome["task"]["inputs"])
            self.assertIn("result", child_outcome)
        parent_result = self.service.submit_result(
            node["node_token"],
            resumed["task"]["task_id"],
            {
                "submission_id": "parent-synthesis",
                "lease_token": resumed["lease_token"],
                "status": "success",
                "stop_reason": "completed",
                "outputs": {"answer": 5},
                "evidence": {},
                "usage": {},
            },
        )
        self.assertEqual("pass", parent_result["gate"]["outcome"])
        self.assertEqual("available", parent_result["earning_status"])
        self.assertEqual("completed", self.service.get_problem(problem["problem_id"])["status"])
        self.assertEqual({"USD": 720}, self.service.get_node_earnings(node["node_token"])["balances"])

    def test_terminal_sibling_failure_cancels_other_leases_before_refund(self) -> None:
        node = self.register_node("parallel")
        parent = self.publish_offering(
            node["node_token"], manifest("urn:test:parallel", "Parallel", "combine")
        )
        child = self.publish_offering(
            node["node_token"], manifest("urn:test:parallel-child", "Parallel child", "work")
        )
        problem = self.create_problem(
            parent["manifest_digest"],
            "combine",
            expected=True,
            reward=100,
            funded=700,
            delegation_budget=600,
        )
        root = self.lease_work(node["node_token"])
        children = [
            task_spec(
                child["manifest_digest"],
                "work",
                inputs={"branch": branch},
                expected_path="/ok",
                expected=True,
                reward=200,
                constraints=root["task"]["constraints"],
                max_attempts=1,
            )
            for branch in ("a", "b", "c")
        ]
        proposal = self.service.delegate_children(
            node["node_token"],
            root["task"]["task_id"],
            {
                "delegation_id": "parallel-split",
                "lease_token": root["lease_token"],
                "children": children,
            },
        )
        self.service.approve_delegation(
            proposal["delegation_id"], {"allow_self_execution": True}
        )
        first = self.lease_work(node["node_token"])
        second = self.lease_work(node["node_token"])
        third = self.lease_work(node["node_token"])
        approved_child = self.service.submit_result(
            node["node_token"],
            first["task"]["task_id"],
            {
                "submission_id": "parallel-pending",
                "lease_token": first["lease_token"],
                "status": "success",
                "stop_reason": "completed",
                "outputs": {"ok": True},
                "evidence": {},
                "usage": {},
            },
        )
        self.assertEqual("available", approved_child["earning_status"])
        self.assertEqual(
            {"USD": 180}, self.service.get_node_earnings(node["node_token"])["balances"]
        )
        rejected = self.service.submit_result(
            node["node_token"],
            second["task"]["task_id"],
            {
                "submission_id": "parallel-failure",
                "lease_token": second["lease_token"],
                "status": "fail",
                "stop_reason": "quality",
                "outputs": {},
                "evidence": {},
                "usage": {},
            },
        )
        self.assertEqual("failed", rejected["task_state"])
        with self.assertRaisesRegex(DomainError, "no longer active"):
            self.service.submit_result(
                node["node_token"],
                third["task"]["task_id"],
                {
                    "submission_id": "late-success",
                    "lease_token": third["lease_token"],
                    "status": "success",
                    "stop_reason": "completed",
                    "outputs": {"ok": True},
                    "evidence": {},
                    "usage": {},
                },
            )
        final = self.service.get_problem(problem["problem_id"])
        self.assertEqual("blocked", final["status"])
        self.assertEqual(0, final["escrow_balance_minor"])
        self.assertEqual(500, final["refund_pending_minor"])
        earnings = self.service.get_node_earnings(node["node_token"])
        self.assertEqual({"USD": 180}, earnings["balances"])
        self.assertEqual({}, earnings["pending_balances"])

    def test_rejected_delegation_reopens_parent_with_audit_reason(self) -> None:
        node = self.register_node("reframe")
        parent = self.publish_offering(
            node["node_token"], manifest("urn:test:reframe", "Reframe", "solve")
        )
        child = self.publish_offering(
            node["node_token"], manifest("urn:test:bad-child", "Bad child", "work")
        )
        self.create_problem(
            parent["manifest_digest"],
            "solve",
            expected=True,
            reward=100,
            funded=200,
            delegation_budget=100,
        )
        lease = self.lease_work(node["node_token"])
        proposal = self.service.delegate_children(
            node["node_token"],
            lease["task"]["task_id"],
            {
                "delegation_id": "reject-this-plan",
                "lease_token": lease["lease_token"],
                "children": [
                    task_spec(
                        child["manifest_digest"],
                        "work",
                        inputs={},
                        expected_path="/ok",
                        expected=True,
                        reward=100,
                        constraints=lease["task"]["constraints"],
                    )
                ],
            },
        )
        rejected = self.service.reject_delegation(
            proposal["delegation_id"],
            {"reason": "The child gate is not independent"},
        )
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("open", rejected["parent_state"])
        self.assertEqual(
            rejected,
            self.service.reject_delegation(
                proposal["delegation_id"],
                {"reason": "The child gate is not independent"},
            ),
        )
        resumed = self.lease_work(node["node_token"])
        self.assertEqual(lease["task"]["task_id"], resumed["task"]["task_id"])

    def test_constraint_weakening_is_rejected(self) -> None:
        node, offering = self.register_with_offering("urn:test:parent", "Parent", "parent")
        child = self.publish_offering(
            node["node_token"], manifest("urn:test:child", "Child", "child")
        )
        self.create_problem(offering["manifest_digest"], "parent", expected=True)
        lease = self.lease_work(node["node_token"])
        weakened = task_spec(
            child["manifest_digest"],
            "child",
            inputs={},
            expected_path="/answer",
            expected=True,
            reward=100,
            constraints={},
        )
        with self.assertRaisesRegex(DomainError, "preserve every inherited constraint"):
            self.service.delegate_children(
                node["node_token"],
                lease["task"]["task_id"],
                {
                    "delegation_id": "bad-split",
                    "lease_token": lease["lease_token"],
                    "children": [weakened],
                },
            )

    def test_expired_final_attempt_blocks_problem(self) -> None:
        node, offering = self.register_with_offering("urn:test:slow", "Slow", "slow")
        problem = self.create_problem(
            offering["manifest_digest"], "slow", expected=True, max_attempts=1
        )
        self.assertIsNotNone(self.lease_work(node["node_token"]))
        self.clock.advance(31)
        self.assertIsNone(self.lease_work(node["node_token"]))
        final = self.service.get_problem(problem["problem_id"])
        self.assertEqual("blocked", final["status"])
        self.assertEqual("failed", final["tasks"][0]["state"])

    def test_timeout_records_pathway_and_excludes_same_node(self) -> None:
        node, offering = self.register_with_offering(
            "urn:test:timeout", "Timeout", "timeout"
        )
        alternate = self.register_node("timeout-alternate")
        self.publish_offering(
            alternate["node_token"],
            manifest("urn:test:timeout", "Timeout", "timeout"),
        )
        self.create_problem(
            offering["manifest_digest"], "timeout", expected=True, max_attempts=2
        )
        self.assertIsNotNone(self.lease_work(node["node_token"]))
        self.clock.advance(31)
        self.assertIsNone(self.lease_work(node["node_token"]))
        self.assertIsNotNone(self.lease_work(alternate["node_token"]))
        aggregates = self.service.pathway_summary(offering["manifest_digest"])[
            "aggregates"
        ]
        self.assertEqual(1, sum(item["invocation_count"] for item in aggregates))
        self.assertEqual(0, sum(item["pass_count"] for item in aggregates))

    def test_invalid_node_token_is_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "invalid or suspended"):
            self.lease_work("not-a-token")

    def test_node_invites_are_single_use_and_expire(self) -> None:
        invite = self.service.create_node_invite(
            {"label": "one provider", "expires_in_seconds": 60}
        )
        first = self.service.register_node(
            invite["invite_token"],
            {
                "registration_id": "registration-first",
                "operator_name": "first",
                "metadata": {},
            },
        )
        repeated = self.service.register_node(
            invite["invite_token"],
            {
                "registration_id": "registration-first",
                "operator_name": "first",
                "metadata": {},
            },
        )
        self.assertEqual(first, repeated)
        with self.assertRaisesRegex(DomainError, "already used"):
            self.service.register_node(
                invite["invite_token"],
                {
                    "registration_id": "registration-second",
                    "operator_name": "second",
                    "metadata": {},
                },
            )

        expired = self.service.create_node_invite(
            {"label": "expires", "expires_in_seconds": 60}
        )
        self.clock.advance(61)
        with self.assertRaisesRegex(DomainError, "expired"):
            self.service.register_node(
                invite["invite_token"],
                {
                    "registration_id": "registration-first",
                    "operator_name": "first",
                    "metadata": {},
                },
            )
        with self.assertRaisesRegex(DomainError, "expired"):
            self.service.register_node(
                expired["invite_token"],
                {
                    "registration_id": "registration-late",
                    "operator_name": "late",
                    "metadata": {},
                },
            )

    def test_cancel_and_deadline_release_unroutable_funding(self) -> None:
        dormant_node, dormant = self.register_with_offering(
            "urn:test:dormant", "Dormant", "unroutable"
        )
        cancelled = self.create_problem(
            dormant["manifest_digest"],
            "unroutable",
            expected=True,
            funded=400,
            reward=200,
        )
        cancelled_view = self.service.cancel_problem(cancelled["problem_id"])
        self.assertEqual("cancelled", cancelled_view["status"])
        self.assertEqual(0, cancelled_view["escrow_balance_minor"])
        self.assertEqual(400, cancelled_view["refund_pending_minor"])

        expires_at = self.clock() + 10
        expiring = self.service.create_problem(
            {
                "intent": "An unroutable expiring order",
                "problem_class": "objective.test",
                "funded_amount_minor": 300,
                "currency": "USD",
                "funding_reference": "expiring-funding",
                "deadline_at": expires_at,
                "task": task_spec(
                    dormant["manifest_digest"],
                    "unroutable",
                    inputs={},
                    expected_path="/ok",
                    expected=True,
                    reward=300,
                ),
            }
        )
        self.assertIsNone(
            self.lease_work(
                dormant_node["node_token"], "deadline-too-close-for-full-lease"
            )
        )
        self.clock.advance(11)
        expired_view = self.service.get_problem(expiring["problem_id"])
        self.assertEqual("expired", expired_view["status"])
        self.assertEqual(0, expired_view["escrow_balance_minor"])
        self.assertEqual(300, expired_view["refund_pending_minor"])

    def test_lease_response_is_replayable_without_consuming_an_attempt(self) -> None:
        node, offering = self.register_with_offering(
            "urn:test:replayable-lease", "Replayable lease", "solve"
        )
        problem = self.create_problem(
            offering["manifest_digest"], "solve", expected=True, max_attempts=1
        )
        first = self.lease_work(node["node_token"], "poll-after-timeout")
        replayed = self.lease_work(
            node["node_token"], "poll-after-timeout"
        )
        self.assertEqual(first, replayed)
        self.assertEqual(1, replayed["task"]["attempt_count"])
        self.assertEqual(
            1,
            self.service.get_problem(problem["problem_id"])["tasks"][0][
                "attempt_count"
            ],
        )

    def test_lease_time_is_captured_after_waiting_for_the_writer_lock(self) -> None:
        node, offering = self.register_with_offering(
            "urn:test:contended-lease", "Contended lease", "solve"
        )
        self.create_problem(offering["manifest_digest"], "solve", expected=True)

        base_clock = self.service.clock
        contender_read_clock = threading.Event()
        result: dict[str, dict | None] = {}
        errors: list[BaseException] = []

        def observed_clock() -> float:
            if threading.current_thread().name == "contended-lease":
                contender_read_clock.set()
            return base_clock()

        def acquire_lease() -> None:
            try:
                result["lease"] = self.service.lease_work(
                    node["node_token"], "contended-poll"
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        blocker = self.service.database.connect()
        worker = threading.Thread(target=acquire_lease, name="contended-lease")
        try:
            blocker.execute("BEGIN IMMEDIATE")
            self.service.clock = observed_clock
            worker.start()
            contender_read_clock.wait(timeout=0.1)
            self.clock.advance(100)
            blocker.rollback()
            worker.join(timeout=2)
        finally:
            self.service.clock = base_clock
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()

        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        lease = result["lease"]
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(
            self.clock() + self.service.config.lease_seconds,
            lease["lease_expires_at"],
        )

    def test_client_submission_ids_are_scoped_to_a_task_and_node(self) -> None:
        node, offering = self.register_with_offering(
            "urn:test:scoped-submission", "Scoped submission", "answer"
        )
        first_problem = self.create_problem(
            offering["manifest_digest"], "answer", expected=42
        )
        self.clock.advance(1)
        second_problem = self.create_problem(
            offering["manifest_digest"], "answer", expected=42
        )
        receipt_ids = []
        for problem in (first_problem, second_problem):
            lease = self.lease_work(node["node_token"])
            self.assertEqual(problem["problem_id"], lease["problem"]["problem_id"])
            response = self.service.submit_result(
                node["node_token"],
                lease["task"]["task_id"],
                {
                    "submission_id": "ordinary-client-key",
                    "lease_token": lease["lease_token"],
                    "status": "success",
                    "stop_reason": "completed",
                    "outputs": {"answer": 42},
                    "evidence": {},
                    "usage": {},
                },
            )
            receipt_ids.append(response["submission_receipt_id"])
        self.assertEqual(2, len(set(receipt_ids)))

    def test_pending_offering_deadline_and_security_admission_guards(self) -> None:
        node = self.register_node("admission")
        pending = self.service.publish_offering(
            node["node_token"], manifest("urn:test:admission", "Admission", "run")
        )
        self.assertFalse(pending["active"])
        base = {
            "intent": "Only admitted providers see this input",
            "problem_class": "objective.admission",
            "funded_amount_minor": 100,
            "currency": "USD",
            "funding_reference": "admission-pending",
            "deadline_at": self.clock() + 3600,
            "task": task_spec(
                pending["manifest_digest"],
                "run",
                inputs={},
                expected_path="/ok",
                expected=True,
                reward=100,
            ),
        }
        with self.assertRaisesRegex(DomainError, "admitted immutable manifest"):
            self.service.create_problem(base)

        self.service.approve_offering(pending["offering_id"])
        no_deadline = {**base, "funding_reference": "missing-deadline"}
        no_deadline.pop("deadline_at")
        with self.assertRaisesRegex(DomainError, "future deadline_at is required"):
            self.service.create_problem(no_deadline)

        with self.assertRaisesRegex(DomainError, "must be between"):
            self.service.create_problem(
                {
                    **base,
                    "funding_reference": "amount-overflow",
                    "funded_amount_minor": 2**63,
                }
            )

        security_task = dict(base["task"])
        security_task["constraints"] = {"data_residency": "EU"}
        with self.assertRaisesRegex(DomainError, "cannot enforce"):
            self.service.create_problem(
                {
                    **base,
                    "funding_reference": "unsupported-security-constraint",
                    "task": security_task,
                }
            )

        created = self.service.create_problem(base)
        self.clock.advance(3601)
        self.service.reap_expired()
        replayed = self.service.create_problem(base)
        self.assertEqual(created["problem_id"], replayed["problem_id"])
        self.assertEqual("expired", replayed["status"])

    def test_delegation_provenance_and_terminal_voiding(self) -> None:
        proposer = self.register_node("proposer")
        executor = self.register_node("independent-executor")
        parent = self.publish_offering(
            proposer["node_token"],
            manifest("urn:test:provenance-parent", "Provenance parent", "plan"),
        )
        child_manifest = manifest(
            "urn:test:provenance-child", "Provenance child", "execute"
        )
        proposer_child = self.publish_offering(
            proposer["node_token"], child_manifest
        )
        independent_child = self.publish_offering(
            executor["node_token"], child_manifest
        )
        self.assertEqual(
            proposer_child["manifest_digest"], independent_child["manifest_digest"]
        )
        problem = self.create_problem(
            parent["manifest_digest"],
            "plan",
            expected=True,
            funded=200,
            reward=100,
            delegation_budget=100,
        )
        root = self.lease_work(proposer["node_token"])
        child_spec = task_spec(
            proposer_child["manifest_digest"],
            "execute",
            inputs={},
            expected_path="/ok",
            expected=True,
            reward=100,
            constraints=root["task"]["constraints"],
        )
        proposal = self.service.delegate_children(
            proposer["node_token"],
            root["task"]["task_id"],
            {
                "idempotency_key": "shared-local-key",
                "lease_token": root["lease_token"],
                "children": [child_spec],
            },
        )
        for invalid_body in (None, [], False, 0, ""):
            with self.subTest(invalid_body=invalid_body):
                with self.assertRaisesRegex(DomainError, "body must be an object"):
                    self.service.approve_delegation(
                        proposal["delegation_id"], invalid_body
                    )
        approved = self.service.approve_delegation(proposal["delegation_id"])
        self.assertFalse(approved["allow_self_execution"])
        self.assertIsNone(self.lease_work(proposer["node_token"]))
        independent_lease = self.lease_work(executor["node_token"])
        self.assertIsNotNone(independent_lease)
        self.service.cancel_problem(problem["problem_id"])

        self.clock.advance(1)
        void_problem = self.create_problem(
            parent["manifest_digest"],
            "plan",
            expected=True,
            funded=200,
            reward=100,
            delegation_budget=100,
        )
        void_root = self.lease_work(proposer["node_token"])
        void_proposal = self.service.delegate_children(
            proposer["node_token"],
            void_root["task"]["task_id"],
            {
                "idempotency_key": "shared-local-key",
                "lease_token": void_root["lease_token"],
                "children": [
                    {
                        **child_spec,
                        "constraints": void_root["task"]["constraints"],
                    }
                ],
            },
        )
        self.assertNotEqual(proposal["delegation_id"], void_proposal["delegation_id"])
        self.service.cancel_problem(void_problem["problem_id"])
        self.assertEqual([], self.service.list_delegations("proposed")["delegations"])
        void_rows = self.service.list_delegations("void")["delegations"]
        self.assertEqual(void_proposal["delegation_id"], void_rows[0]["delegation_id"])
        self.assertEqual("problem_cancelled", void_rows[0]["decision_reason"])

    def test_delegation_proposal_churn_is_bounded(self) -> None:
        node = self.register_node("bounded-decomposer")
        parent = self.publish_offering(
            node["node_token"],
            manifest("urn:test:bounded-parent", "Bounded parent", "plan"),
        )
        child = self.publish_offering(
            node["node_token"],
            manifest("urn:test:bounded-child", "Bounded child", "execute"),
        )
        self.create_problem(
            parent["manifest_digest"],
            "plan",
            expected=True,
            funded=200,
            reward=100,
            delegation_budget=100,
        )
        child_spec = task_spec(
            child["manifest_digest"],
            "execute",
            inputs={},
            expected_path="/ok",
            expected=True,
            reward=100,
            constraints={"workflow_scope": "task_only"},
        )
        for index in range(3):
            lease = self.lease_work(node["node_token"])
            proposal = self.service.delegate_children(
                node["node_token"],
                lease["task"]["task_id"],
                {
                    "idempotency_key": f"proposal-{index}",
                    "lease_token": lease["lease_token"],
                    "children": [child_spec],
                },
            )
            self.service.reject_delegation(
                proposal["delegation_id"], {"reason": f"revision {index}"}
            )
        final_lease = self.lease_work(node["node_token"])
        with self.assertRaisesRegex(DomainError, "proposal limit"):
            self.service.delegate_children(
                node["node_token"],
                final_lease["task"]["task_id"],
                {
                    "idempotency_key": "proposal-over-limit",
                    "lease_token": final_lease["lease_token"],
                    "children": [child_spec],
                },
            )

    def test_operator_reframe_retains_accepted_work_without_repaying_it(self) -> None:
        node = self.register_node("reframe-provider")
        root_v1 = self.publish_offering(
            node["node_token"],
            manifest("urn:test:root-v1", "Root v1", "synthesize-v1"),
        )
        leaf = self.publish_offering(
            node["node_token"],
            manifest("urn:test:leaf", "Reusable leaf", "solve-leaf"),
        )
        root_v2 = self.publish_offering(
            node["node_token"],
            manifest("urn:test:root-v2", "Root v2", "synthesize-v2"),
        )
        problem = self.create_problem(
            root_v1["manifest_digest"],
            "synthesize-v1",
            expected=42,
            reward=100,
            funded=500,
            delegation_budget=200,
            max_attempts=3,
        )
        first_root_lease = self.lease_work(node["node_token"])
        child = task_spec(
            leaf["manifest_digest"],
            "solve-leaf",
            inputs={"value": 21},
            expected_path="/finding",
            expected="retained reasoning",
            reward=200,
            constraints=first_root_lease["task"]["constraints"],
        )
        proposal = self.service.delegate_children(
            node["node_token"],
            first_root_lease["task"]["task_id"],
            {
                "idempotency_key": "reframe-leaf",
                "lease_token": first_root_lease["lease_token"],
                "children": [child],
            },
        )
        self.service.approve_delegation(
            proposal["delegation_id"], {"allow_self_execution": True}
        )
        child_lease = self.lease_work(node["node_token"])
        child_result = self.service.submit_result(
            node["node_token"],
            child_lease["task"]["task_id"],
            {
                "submission_id": "accepted-reusable-leaf",
                "lease_token": child_lease["lease_token"],
                "status": "success",
                "stop_reason": "completed",
                "outputs": {"finding": "retained reasoning"},
                "evidence": {"review": "source-contract-only"},
                "usage": {"duration_ms": 7},
            },
        )
        artifact_digest = child_result["accepted_artifact_digest"]
        self.assertIsNotNone(artifact_digest)

        resumed_root = self.lease_work(node["node_token"])
        rejected_root = self.service.submit_result(
            node["node_token"],
            resumed_root["task"]["task_id"],
            {
                "submission_id": "root-v1-rejected",
                "lease_token": resumed_root["lease_token"],
                "status": "success",
                "stop_reason": "completed",
                "outputs": {"answer": 0},
                "evidence": {"review": "missing arbitration"},
                "usage": {},
            },
        )
        self.assertEqual("reject", rejected_root["gate"]["outcome"])
        self.assertEqual("open", rejected_root["task_state"])

        unchanged_successor = {
            "intent": problem["intent"],
            "problem_class": problem["problem_class"],
            "funded_amount_minor": 300,
            "currency": "USD",
            "funding_reference": "reframe-unchanged-contract",
            "deadline_at": self.clock() + 3600,
            "task": task_spec(
                root_v1["manifest_digest"],
                "synthesize-v1",
                inputs={"value": 21},
                expected_path="/answer",
                expected=42,
                reward=101,
                delegation_budget=199,
                constraints={"workflow_scope": "task_only"},
                max_attempts=3,
            ),
        }
        base_reframe = {
            "idempotency_key": "root-frame-repair",
            "source_submission_receipt_id": rejected_root[
                "submission_receipt_id"
            ],
            "diagnosis": {
                "kind": "frame_error",
                "summary": "The root contract did not own cross-leaf arbitration.",
                "diagnosed_by": "operator:blind-review",
                "required_changes": [
                    "Add an explicit arbitration decision to the root output"
                ],
                "evidence": {"review_id": "review-1"},
            },
            "retained_artifacts": [
                {
                    "binding": "leaf_finding",
                    "source_submission_receipt_id": child_result[
                        "submission_receipt_id"
                    ],
                }
            ],
        }
        with self.assertRaisesRegex(
            DomainError, "change the structural root task contract"
        ):
            self.service.reframe_problem(
                problem["problem_id"],
                {**base_reframe, "successor_problem": unchanged_successor},
            )
        self.assertEqual(
            "active", self.service.get_problem(problem["problem_id"])["status"]
        )

        successor_request = {
            "intent": problem["intent"],
            "problem_class": problem["problem_class"],
            "funded_amount_minor": 300,
            "currency": "USD",
            "funding_reference": "reframe-successor-funding",
            "deadline_at": self.clock() + 3600,
            "task": task_spec(
                root_v2["manifest_digest"],
                "synthesize-v2",
                inputs={"value": 21, "required_revision": "arbitrate"},
                expected_path="/answer",
                expected=42,
                reward=300,
                constraints={
                    "workflow_scope": "task_only",
                    "root_contract_version": "v2",
                },
                max_attempts=2,
            ),
        }
        request = {**base_reframe, "successor_problem": successor_request}
        reframe = self.service.reframe_problem(problem["problem_id"], request)
        replayed = self.service.reframe_problem(problem["problem_id"], request)
        self.assertEqual(reframe, replayed)
        self.assertEqual("operator_authorized", reframe["detection_mode"])
        self.assertNotEqual(
            reframe["source_root_contract_digest"],
            reframe["successor_root_contract_digest"],
        )
        self.assertEqual(artifact_digest, reframe["retained_artifacts"][0]["artifact_digest"])
        self.assertEqual(
            "source_payable_already_created_no_new_transfer",
            reframe["retained_artifacts"][0]["economic_effect"],
        )

        source = self.service.get_problem(problem["problem_id"])
        self.assertEqual("cancelled", source["status"])
        self.assertEqual(300, source["refund_pending_minor"])
        self.assertEqual(
            reframe["successor_problem_id"],
            source["reframe_lineage"]["successor"]["successor_problem_id"],
        )
        self.assertEqual(
            1,
            sum(
                transfer["reason"] == "accepted_solver_work"
                for transfer in source["ledger_transfers"]
            ),
        )
        successor_before = self.service.get_problem(
            reframe["successor_problem_id"]
        )
        self.assertEqual(1, len(successor_before["tasks"]))
        self.assertEqual([], successor_before["submissions"])
        self.assertEqual(
            ["confirmed_platform_funding"],
            [
                transfer["reason"]
                for transfer in successor_before["ledger_transfers"]
            ],
        )
        audit = self.service.database.connect()
        try:
            committed_contract = self.service._task_contract(
                audit, reframe["successor_root_task_id"]
            )
            self.assertEqual(
                reframe["successor_root_contract_digest"],
                content_digest(committed_contract),
            )
            self.assertEqual(
                [
                    {
                        "binding": "leaf_finding",
                        "artifact_digest": artifact_digest,
                    }
                ],
                committed_contract["retained_artifact_bindings"],
            )
            self.assertEqual(
                2, audit.execute("SELECT COUNT(*) FROM pathway_events").fetchone()[0]
            )
            self.assertEqual(
                1,
                audit.execute("SELECT COUNT(*) FROM accepted_artifacts").fetchone()[0],
            )
            private_commitments = audit.execute(
                """
                SELECT source_contract_digest, gate_digest
                FROM accepted_artifacts WHERE digest = ?
                """,
                (artifact_digest,),
            ).fetchone()
            self.assertTrue(private_commitments["source_contract_digest"])
            self.assertTrue(private_commitments["gate_digest"])
        finally:
            audit.close()

        successor_lease = self.lease_work(node["node_token"])
        self.assertEqual(
            reframe["successor_problem_id"],
            successor_lease["problem"]["problem_id"],
        )
        self.assertEqual([], successor_lease["task"]["accepted_child_results"])
        retained = successor_lease["task"]["retained_artifacts"]
        self.assertEqual(1, len(retained))
        self.assertEqual("leaf_finding", retained[0]["binding"])
        self.assertEqual(artifact_digest, retained[0]["artifact_digest"])
        envelope = retained[0]["artifact"]
        self.assertNotIn("accept_spec", envelope["contract"])
        self.assertNotIn("gate", envelope)
        self.assertNotIn("source_contract_digest", envelope["verification"])
        self.assertNotIn("gate_digest", envelope["verification"])
        self.assertNotIn("usage", envelope["result"])
        self.assertNotIn("node_id", envelope["source"])

        completed = self.service.submit_result(
            node["node_token"],
            successor_lease["task"]["task_id"],
            {
                "submission_id": "root-v2-accepted",
                "lease_token": successor_lease["lease_token"],
                "status": "success",
                "stop_reason": "completed",
                "outputs": {"answer": 42},
                "evidence": {"used_artifact": artifact_digest},
                "usage": {},
            },
        )
        self.assertEqual("pass", completed["gate"]["outcome"])
        self.assertEqual(
            {"USD": 450},
            self.service.get_node_earnings(node["node_token"])["balances"],
        )
        successor = self.service.get_problem(reframe["successor_problem_id"])
        self.assertEqual("completed", successor["status"])
        self.assertEqual(
            problem["problem_id"],
            successor["reframe_lineage"]["predecessor"]["source_problem_id"],
        )
        final_audit = self.service.database.connect()
        try:
            self.assertEqual(
                3,
                final_audit.execute(
                    "SELECT COUNT(*) FROM pathway_events"
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                final_audit.execute(
                    "SELECT COUNT(*) FROM accepted_artifacts"
                ).fetchone()[0],
            )
        finally:
            final_audit.close()

    def test_schema_v1_restart_backfills_passing_submissions(self) -> None:
        node, offering = self.register_with_offering(
            "urn:test:artifact-backfill", "Artifact backfill", "answer"
        )
        problem = self.create_problem(
            offering["manifest_digest"], "answer", expected=42
        )
        lease = self.lease_work(node["node_token"])
        accepted = self.service.submit_result(
            node["node_token"],
            lease["task"]["task_id"],
            {
                "submission_id": "pre-migration-pass",
                "lease_token": lease["lease_token"],
                "status": "success",
                "stop_reason": "completed",
                "outputs": {"answer": 42},
                "evidence": {},
                "usage": {},
            },
        )
        original_digest = accepted["accepted_artifact_digest"]
        connection = self.service.database.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TABLE retained_artifact_bindings")
            connection.execute("DROP TABLE problem_reframes")
            connection.execute("DROP TABLE accepted_artifacts")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()

        self.service = CoordinatorService(
            self.service.database,
            config=self.service.config,
            clock=self.clock,
        )
        final = self.service.get_problem(problem["problem_id"])
        self.assertEqual(
            original_digest, final["submissions"][0]["accepted_artifact_digest"]
        )
        migrated = self.service.database.connect()
        try:
            self.assertEqual(
                1,
                migrated.execute(
                    "SELECT COUNT(*) FROM accepted_artifacts"
                ).fetchone()[0],
            )
        finally:
            migrated.close()

    def test_schema_v1_migrates_additively_to_accepted_artifacts(self) -> None:
        path = Path(self.temporary.name) / "v1.db"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(BASE_SCHEMA)
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()
        Database(path).initialize()
        migrated = sqlite3.connect(path)
        try:
            self.assertEqual(2, migrated.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            migrated.close()
        self.assertTrue(
            {
                "accepted_artifacts",
                "problem_reframes",
                "retained_artifact_bindings",
            }.issubset(tables)
        )

    def test_unversioned_database_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "temporary file"):
            Database(":memory:")
        path = Path(self.temporary.name) / "legacy.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE legacy_state (id TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RuntimeError, "unversioned"):
            Database(path).initialize()

    def test_manifest_schemas_are_enforced_on_inputs_and_outputs(self) -> None:
        node = self.register_node("typed")
        typed_manifest = manifest("urn:test:typed", "Typed", "calculate")
        typed_manifest["input_schema"] = {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
            "additionalProperties": False,
        }
        typed_manifest["output_schema"] = {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "integer"}},
            "additionalProperties": False,
        }
        offering = self.publish_offering(node["node_token"], typed_manifest)
        with self.assertRaisesRegex(DomainError, "inputs do not satisfy"):
            self.service.create_problem(
                {
                    "intent": "Reject bad typed input",
                    "problem_class": "objective.typed",
                    "funded_amount_minor": 100,
                    "currency": "USD",
                    "funding_reference": "bad-typed-input",
                    "deadline_at": self.clock() + 3600,
                    "task": task_spec(
                        offering["manifest_digest"],
                        "calculate",
                        inputs={"value": "not-an-integer"},
                        expected_path="/answer",
                        expected=1,
                        reward=100,
                    ),
                }
            )
        problem = self.service.create_problem(
            {
                "intent": "Reject bad typed output",
                "problem_class": "objective.typed",
                "funded_amount_minor": 100,
                "currency": "USD",
                "funding_reference": "bad-typed-output",
                "deadline_at": self.clock() + 3600,
                "task": task_spec(
                    offering["manifest_digest"],
                    "calculate",
                    inputs={"value": 1},
                    expected_path="/answer",
                    expected="one",
                    reward=100,
                    max_attempts=1,
                ),
            }
        )
        lease = self.lease_work(node["node_token"])
        response = self.service.submit_result(
            node["node_token"],
            lease["task"]["task_id"],
            {
                "submission_id": "bad-typed-result",
                "lease_token": lease["lease_token"],
                "status": "success",
                "stop_reason": "completed",
                "outputs": {"answer": "one"},
                "evidence": {},
                "usage": {},
            },
        )
        self.assertEqual("reject", response["gate"]["outcome"])
        self.assertEqual(
            "protocol:output-schema", response["gate"]["clauses"][0]["clause_id"]
        )
        self.assertEqual("blocked", self.service.get_problem(problem["problem_id"])["status"])


if __name__ == "__main__":
    unittest.main()
