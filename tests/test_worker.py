from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from fractal_protocol.errors import DomainError
from fractal_protocol.worker import (
    ResultExecutionInProgress,
    ResultReceiptPruned,
    ConnectedNode,
    SolverHandler,
    SQLiteResultJournal,
)


class FakeCoordinatorClient:
    base_url = "http://coordinator.test"

    def __init__(
        self, *, node_token: str = "test-node-token", lose_first_response: bool = True
    ) -> None:
        self.node_token = node_token
        self.lose_first_response = lose_first_response
        self.lease_count = 0
        self.submissions: list[dict[str, Any]] = []

    def publish_offering(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {"offering_id": "offering-test", "manifest": manifest}

    def lease(self, *, lease_request_id: str) -> dict[str, Any]:
        self.lease_count += 1
        return {
            "lease_request_id": lease_request_id,
            "lease_token": "stable-lease-token",
            "lease_expires_at": time.time() + 60,
            "offering": {"offering_id": "offering-test"},
            "problem": {"problem_id": "problem-test"},
            "task": {"task_id": "task-test", "inputs": {"value": 21}},
        }

    def submit(self, task_id: str, **result: Any) -> dict[str, Any]:
        self.submissions.append({"task_id": task_id, **result})
        if self.lose_first_response and len(self.submissions) == 1:
            raise ConnectionError("response lost after coordinator accepted it")
        return {
            "submission_receipt_id": "receipt-test",
            "gate": {"outcome": "pass"},
        }


class WorkerTests(unittest.TestCase):
    def test_invalid_submission_id_is_rejected_before_lease_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeCoordinatorClient(lose_first_response=False)
            node = ConnectedNode(
                client,  # type: ignore[arg-type]
                result_journal=SQLiteResultJournal(Path(temporary) / "outbox.db"),
            )
            execution_count = 0

            def execute(task: dict[str, Any]) -> dict[str, Any]:
                nonlocal execution_count
                execution_count += 1
                return {"answer": 42}

            node.publish(SolverHandler(manifest={"name": "test"}, execute=execute))
            for invalid in ("", "x" * 201):
                with self.subTest(invalid_length=len(invalid)):
                    with self.assertRaises(DomainError):
                        node.work_once(
                            lease_request_id="unused-poll",
                            submission_id=invalid,
                        )
            self.assertEqual(0, client.lease_count)
            self.assertEqual(0, execution_count)

    def test_lost_submission_response_reuses_journal_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeCoordinatorClient()
            journal = SQLiteResultJournal(Path(temporary) / "provider-outbox.db")
            node = ConnectedNode(
                client,  # type: ignore[arg-type] - intentional protocol-shaped fake
                result_journal=journal,
            )
            execution_count = 0

            def execute(task: dict[str, Any]) -> dict[str, Any]:
                nonlocal execution_count
                execution_count += 1
                return {"answer": task["inputs"]["value"] * 2}

            node.publish(SolverHandler(manifest={"name": "test"}, execute=execute))

            with self.assertRaisesRegex(ConnectionError, "response lost"):
                node.work_once(
                    lease_request_id="persisted-poll",
                    submission_id="persisted-submission",
                )
            prepared_request = client.submissions[0]

            receipt = node.work_once(
                lease_request_id="persisted-poll",
                submission_id="persisted-submission",
            )
            self.assertEqual(1, execution_count)
            self.assertEqual(prepared_request, client.submissions[1])
            self.assertEqual("receipt-test", receipt["submission_receipt_id"])

            replayed_receipt = node.work_once(
                lease_request_id="persisted-poll",
                submission_id="persisted-submission",
            )
            self.assertEqual(receipt, replayed_receipt)
            self.assertEqual(2, len(client.submissions))

            with closing(sqlite3.connect(journal.path)) as connection:
                stored = connection.execute(
                    "SELECT result_json, receipt_json FROM result_journal"
                ).fetchone()
            self.assertIsNone(stored[0])
            self.assertIsNotNone(stored[1])
            self.assertEqual(
                1,
                journal.prune_completed_receipts(
                    completed_before=time.time()
                ),
            )
            with closing(sqlite3.connect(journal.path)) as connection:
                node_namespace = connection.execute(
                    "SELECT node_namespace FROM result_journal"
                ).fetchone()[0]
            journal.record_receipt(
                node_namespace,
                "task-test",
                "persisted-submission",
                receipt,
            )
            with closing(sqlite3.connect(journal.path)) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT receipt_json FROM result_journal"
                    ).fetchone()[0]
                )
            with self.assertRaisesRegex(RuntimeError, "different receipt"):
                journal.record_receipt(
                    node_namespace,
                    "task-test",
                    "persisted-submission",
                    {"submission_receipt_id": "different"},
                )
            with self.assertRaises(ResultReceiptPruned):
                node.work_once(
                    lease_request_id="persisted-poll",
                    submission_id="persisted-submission",
                )
            self.assertEqual(1, execution_count)

    def test_concurrent_calls_claim_execution_before_running_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeCoordinatorClient(lose_first_response=False)
            node = ConnectedNode(
                client,  # type: ignore[arg-type]
                result_journal=SQLiteResultJournal(Path(temporary) / "outbox.db"),
            )
            entered = threading.Event()
            release = threading.Event()
            execution_count = 0
            errors: list[BaseException] = []

            def execute(task: dict[str, Any]) -> dict[str, Any]:
                nonlocal execution_count
                execution_count += 1
                entered.set()
                release.wait(timeout=2)
                return {"answer": task["inputs"]["value"] * 2}

            def work(submission_id: str) -> None:
                try:
                    node.work_once(
                        lease_request_id="same-poll",
                        submission_id=submission_id,
                    )
                except BaseException as error:  # pragma: no cover - asserted below
                    errors.append(error)

            node.publish(SolverHandler(manifest={"name": "test"}, execute=execute))
            first = threading.Thread(target=work, args=("submission-a",))
            second = threading.Thread(target=work, args=("submission-b",))
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            second.join(timeout=1)
            release.set()
            first.join(timeout=2)

            self.assertEqual(1, execution_count)
            self.assertEqual(1, len(client.submissions))
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], ResultExecutionInProgress)
            with self.assertRaises(ResultExecutionInProgress):
                node.work_once(
                    lease_request_id="same-poll",
                    submission_id="submission-after-prepare",
                )
            self.assertEqual(1, execution_count)

    def test_shared_journal_scopes_receipts_by_coordinator_and_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteResultJournal(Path(temporary) / "shared-outbox.db")
            first_client = FakeCoordinatorClient(
                node_token="node-a", lose_first_response=False
            )
            second_client = FakeCoordinatorClient(
                node_token="node-b", lose_first_response=False
            )
            execution_count = 0

            def execute(task: dict[str, Any]) -> dict[str, Any]:
                nonlocal execution_count
                execution_count += 1
                return {"answer": 42}

            for client in (first_client, second_client):
                node = ConnectedNode(  # type: ignore[arg-type]
                    client, result_journal=journal
                )
                node.publish(
                    SolverHandler(manifest={"name": "test"}, execute=execute)
                )
                node.work_once(
                    lease_request_id="ordinary-poll",
                    submission_id="submission-1",
                )

            self.assertEqual(2, execution_count)
            self.assertEqual(1, len(first_client.submissions))
            self.assertEqual(1, len(second_client.submissions))

    def test_journal_fails_fast_and_rejects_oversized_prepared_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            incompatible = Path(temporary) / "incompatible.db"
            with closing(sqlite3.connect(incompatible)) as connection:
                connection.execute("PRAGMA user_version = 99")
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "Unsupported Result journal"):
                SQLiteResultJournal(incompatible)

            malformed = Path(temporary) / "malformed-v1.db"
            with closing(sqlite3.connect(malformed)) as connection:
                connection.executescript(
                    """
                    PRAGMA user_version = 1;
                    CREATE TABLE result_journal (
                        node_namespace TEXT, task_id TEXT, submission_id TEXT,
                        lease_identity TEXT, result_json TEXT, result_digest TEXT,
                        receipt_json TEXT, receipt_digest TEXT, created_at REAL,
                        completed_at REAL, receipt_pruned_at REAL
                    );
                    CREATE TABLE execution_claims (
                        node_namespace TEXT, task_id TEXT, submission_id TEXT,
                        claim_token TEXT, lease_identity TEXT,
                        created_at REAL
                    );
                    """
                )
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "incompatible schema"):
                SQLiteResultJournal(malformed)

            missing_index = Path(temporary) / "missing-index.db"
            SQLiteResultJournal(missing_index)
            with closing(sqlite3.connect(missing_index)) as connection:
                connection.execute("DROP INDEX idx_execution_claim_lease")
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "lease index"):
                SQLiteResultJournal(missing_index)

            journal = SQLiteResultJournal(Path(temporary) / "bounded.db")
            claim = journal.claim(
                "node-namespace",
                "task-id",
                "submission-id",
                "lease-identity",
            )
            assert claim.claim_token is not None
            with self.assertRaisesRegex(ValueError, "at most"):
                journal.prepare(
                    "node-namespace",
                    "task-id",
                    "submission-id",
                    claim.claim_token,
                    {
                        "status": "success",
                        "stop_reason": "completed",
                        "outputs": {"value": "x" * (300 * 1024)},
                        "evidence": {},
                        "usage": {},
                    },
                )
            with self.assertRaisesRegex(ValueError, "non-future"):
                journal.prune_completed_receipts(
                    completed_before=time.time() + 120
                )
            with self.assertRaises(ResultExecutionInProgress):
                journal.claim(
                    "node-namespace",
                    "task-id",
                    "submission-id",
                    "lease-identity",
                )
            with closing(sqlite3.connect(journal.path)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM result_journal"
                ).fetchone()[0]
            self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
