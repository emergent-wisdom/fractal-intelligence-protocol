from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fractal_protocol.api import CoordinatorHTTPServer, _constant_time_token_match
from fractal_protocol.client import CoordinatorClient, CoordinatorClientError
from fractal_protocol.database import Database
from fractal_protocol.service import CoordinatorService, ServiceConfig

from tests.helpers import manifest, task_spec


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        service = CoordinatorService(
            Database(Path(self.temporary.name) / "http.db"),
            config=ServiceConfig(platform_fee_bps=500),
        )
        self.server = CoordinatorHTTPServer(
            ("127.0.0.1", 0),
            service,
            admin_token="admin-test",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_complete_http_flow(self) -> None:
        admin = CoordinatorClient(self.base_url, admin_token="admin-test")
        invite = admin.create_node_invite("http provider")
        bootstrap = CoordinatorClient(
            self.base_url, node_invite_token=invite["invite_token"]
        )
        self.assertEqual("ok", bootstrap.health()["status"])
        node = bootstrap.register_node(
            "http-provider", registration_id="http-provider-registration"
        )
        provider = CoordinatorClient(self.base_url, node_token=node["node_token"])
        offering = provider.publish_offering(manifest("urn:test:http", "HTTP", "answer"))
        pending = admin.list_offerings("pending")["offerings"]
        self.assertEqual("HTTP", pending[0]["manifest"]["name"])
        self.assertEqual(["answer"], pending[0]["manifest"]["operations"])
        self.assertEqual("http-provider", pending[0]["operator_name"])
        self.assertEqual("active", pending[0]["node_status"])
        admin.approve_offering(offering["offering_id"])

        problem = admin.create_problem(
            {
                "intent": "Return an answer through HTTP",
                "problem_class": "objective.http",
                "funded_amount_minor": 100,
                "currency": "USD",
                "funding_reference": "http-funding",
                "deadline_at": time.time() + 3600,
                "task": task_spec(
                    offering["manifest_digest"],
                    "answer",
                    inputs={},
                    expected_path="/answer",
                    expected="yes",
                    reward=100,
                ),
            }
        )
        lease = provider.lease(lease_request_id="http-provider-poll-1")
        decision = provider.submit(
            lease["task"]["task_id"],
            submission_id="http-submission",
            lease_token=lease["lease_token"],
            status="success",
            stop_reason="completed",
            outputs={"answer": "yes"},
        )
        self.assertEqual("pass", decision["gate"]["outcome"])
        self.assertEqual({"USD": 95}, provider.earnings()["balances"])
        final = admin.get_problem(problem["problem_id"])
        self.assertEqual("completed", final["status"])
        self.assertEqual(
            {"answer": "yes"}, final["accepted_result"]["result"]["outputs"]
        )

    def test_admin_route_rejects_node_token(self) -> None:
        admin = CoordinatorClient(self.base_url, admin_token="admin-test")
        invite = admin.create_node_invite("not admin")
        bootstrap = CoordinatorClient(
            self.base_url, node_invite_token=invite["invite_token"]
        )
        node = bootstrap.register_node(
            "not-admin", registration_id="not-admin-registration"
        )
        wrong = CoordinatorClient(self.base_url, admin_token=node["node_token"])
        with self.assertRaises(CoordinatorClientError) as raised:
            wrong.create_problem({})
        self.assertEqual(401, raised.exception.status)
        with self.assertRaises(CoordinatorClientError) as reframe_raised:
            wrong.reframe_problem("not-a-problem", {})
        self.assertEqual(401, reframe_raised.exception.status)

        with self.assertRaises(CoordinatorClientError) as missing:
            admin.approve_delegation("does-not-exist")
        self.assertEqual(404, missing.exception.status)
        self.assertFalse(_constant_time_token_match("admin-å", "admin-test"))

    def test_invalid_utf8_json_is_a_client_error(self) -> None:
        request = Request(
            f"{self.base_url}/v1/problems",
            data=b"\xff",
            headers={
                "Authorization": "Bearer admin-test",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        try:
            self.assertEqual(400, raised.exception.code)
            payload = json.loads(raised.exception.read())
            self.assertEqual("invalid_json", payload["error"]["code"])
        finally:
            raised.exception.close()

    def test_ipv6_loopback_server_can_bind_when_available(self) -> None:
        service = CoordinatorService(
            Database(Path(self.temporary.name) / "ipv6.db")
        )
        try:
            server = CoordinatorHTTPServer(
                ("::1", 0), service, admin_token="admin-test"
            )
        except OSError as error:
            self.skipTest(f"IPv6 loopback is unavailable: {error}")
        try:
            self.assertEqual(socket.AF_INET6, server.address_family)
        finally:
            server.server_close()

    def test_delegation_requires_admin_approval_over_http(self) -> None:
        admin = CoordinatorClient(self.base_url, admin_token="admin-test")
        invite = admin.create_node_invite("http decomposer")
        bootstrap = CoordinatorClient(
            self.base_url, node_invite_token=invite["invite_token"]
        )
        node = bootstrap.register_node(
            "http-decomposer", registration_id="http-decomposer-registration"
        )
        provider = CoordinatorClient(self.base_url, node_token=node["node_token"])
        parent = provider.publish_offering(
            manifest("urn:test:http-parent", "HTTP parent", "plan")
        )
        child = provider.publish_offering(
            manifest("urn:test:http-child", "HTTP child", "execute")
        )
        admin.approve_offering(parent["offering_id"])
        admin.approve_offering(child["offering_id"])
        problem = admin.create_problem(
            {
                "intent": "Approve one child",
                "problem_class": "objective.http.delegation",
                "funded_amount_minor": 200,
                "currency": "USD",
                "funding_reference": "http-delegation-funding",
                "deadline_at": time.time() + 3600,
                "task": task_spec(
                    parent["manifest_digest"],
                    "plan",
                    inputs={},
                    expected_path="/done",
                    expected=True,
                    reward=100,
                    delegation_budget=100,
                    constraints={"workflow_scope": "task_only"},
                ),
            }
        )
        root = provider.lease(lease_request_id="http-decomposer-poll-root")
        proposal = provider.delegate(
            root["task"]["task_id"],
            idempotency_key="http-delegation",
            lease_token=root["lease_token"],
            children=[
                task_spec(
                    child["manifest_digest"],
                    "execute",
                    inputs={},
                    expected_path="/ok",
                    expected=True,
                    reward=100,
                    constraints=root["task"]["constraints"],
                )
            ],
        )
        self.assertEqual("proposed", proposal["status"])
        self.assertIsNone(
            provider.lease(lease_request_id="http-decomposer-poll-before-approval")
        )
        self.assertEqual(
            ["http-delegation"],
            [
                item["idempotency_key"]
                for item in admin.list_delegations("proposed")["delegations"]
            ],
        )
        approved = admin.approve_delegation(
            proposal["delegation_id"], allow_self_execution=True
        )
        self.assertEqual("approved", approved["status"])
        self.assertIsNotNone(
            provider.lease(lease_request_id="http-decomposer-poll-child")
        )
        cancelled = admin.cancel_problem(problem["problem_id"])
        self.assertEqual("cancelled", cancelled["status"])


if __name__ == "__main__":
    unittest.main()
