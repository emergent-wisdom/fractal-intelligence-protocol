from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .client import CoordinatorClient
from .errors import require
from .protocol import (
    MAX_RESULT_BYTES,
    canonical_json,
    require_string,
    validate_result,
)


ExecuteFunction = Callable[[dict[str, Any]], dict[str, Any]]
JOURNAL_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 512 * 1024

JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS result_journal (
    node_namespace TEXT NOT NULL,
    task_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    lease_identity TEXT NOT NULL,
    result_json TEXT,
    result_digest TEXT NOT NULL,
    receipt_json TEXT,
    receipt_digest TEXT,
    created_at REAL NOT NULL,
    completed_at REAL,
    receipt_pruned_at REAL,
    PRIMARY KEY (node_namespace, task_id, submission_id)
);

CREATE TABLE IF NOT EXISTS execution_claims (
    node_namespace TEXT NOT NULL,
    task_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    lease_identity TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (node_namespace, task_id, submission_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_claim_lease
ON execution_claims (node_namespace, task_id, lease_identity);

CREATE UNIQUE INDEX IF NOT EXISTS idx_result_journal_lease
ON result_journal (node_namespace, task_id, lease_identity);

CREATE INDEX IF NOT EXISTS idx_result_journal_completed
ON result_journal (completed_at, receipt_pruned_at);
"""

JOURNAL_TABLE_SHAPES = {
    "result_journal": {
        "node_namespace": ("TEXT", 1, 1),
        "task_id": ("TEXT", 1, 2),
        "submission_id": ("TEXT", 1, 3),
        "lease_identity": ("TEXT", 1, 0),
        "result_json": ("TEXT", 0, 0),
        "result_digest": ("TEXT", 1, 0),
        "receipt_json": ("TEXT", 0, 0),
        "receipt_digest": ("TEXT", 0, 0),
        "created_at": ("REAL", 1, 0),
        "completed_at": ("REAL", 0, 0),
        "receipt_pruned_at": ("REAL", 0, 0),
    },
    "execution_claims": {
        "node_namespace": ("TEXT", 1, 1),
        "task_id": ("TEXT", 1, 2),
        "submission_id": ("TEXT", 1, 3),
        "claim_token": ("TEXT", 1, 0),
        "lease_identity": ("TEXT", 1, 0),
        "created_at": ("REAL", 1, 0),
    },
}


class ResultExecutionInProgress(RuntimeError):
    """Another process owns execution for the same node/task/submission lease."""


class ResultReceiptPruned(RuntimeError):
    """A completed idempotency tombstone remains but its local receipt was pruned."""


@dataclass(frozen=True)
class ResultClaim:
    prepared: dict[str, Any] | None
    receipt: dict[str, Any] | None
    claim_token: str | None


class ResultJournal(Protocol):
    """Durable outbox for a prepared Result and its coordinator receipt."""

    def claim(
        self,
        node_namespace: str,
        task_id: str,
        submission_id: str,
        lease_identity: str,
    ) -> ResultClaim: ...

    def prepare(
        self,
        node_namespace: str,
        task_id: str,
        submission_id: str,
        claim_token: str,
        result: dict[str, Any],
    ) -> dict[str, Any]: ...

    def record_receipt(
        self,
        node_namespace: str,
        task_id: str,
        submission_id: str,
        receipt: dict[str, Any],
    ) -> None: ...


class SQLiteResultJournal:
    """Crash-safe local Result outbox; lease secrets are deliberately not stored."""

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("SQLiteResultJournal requires a durable file path")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            self._initialize(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }
        if version == 0 and tables:
            raise RuntimeError(
                "The Result journal is unversioned and cannot be upgraded safely; "
                "use a new journal path."
            )
        if version not in {0, JOURNAL_SCHEMA_VERSION}:
            raise RuntimeError(
                f"Unsupported Result journal schema version {version}; "
                f"this build requires {JOURNAL_SCHEMA_VERSION}."
            )
        if version == 0:
            connection.executescript(JOURNAL_SCHEMA)
            connection.execute(f"PRAGMA user_version = {JOURNAL_SCHEMA_VERSION}")
            connection.commit()

        for table, expected_shape in JOURNAL_TABLE_SHAPES.items():
            actual_shape = {
                row["name"]: (
                    row["type"].upper(),
                    row["notnull"],
                    row["pk"],
                )
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if actual_shape != expected_shape:
                raise RuntimeError(
                    f"Result journal table {table} has an incompatible schema"
                )

        for table, index_name in (
            ("execution_claims", "idx_execution_claim_lease"),
            ("result_journal", "idx_result_journal_lease"),
        ):
            indexes = {
                row["name"]: bool(row["unique"])
                for row in connection.execute(
                    f"PRAGMA index_list({table})"
                ).fetchall()
            }
            index_columns = [
                row["name"]
                for row in connection.execute(
                    f"PRAGMA index_info({index_name})"
                ).fetchall()
            ]
            if not indexes.get(index_name) or index_columns != [
                "node_namespace",
                "task_id",
                "lease_identity",
            ]:
                raise RuntimeError(
                    f"Result journal lease index {index_name} is incompatible"
                )

    def claim(
        self,
        node_namespace: str,
        task_id: str,
        submission_id: str,
        lease_identity: str,
    ) -> ResultClaim:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT result_json, receipt_json, completed_at
                FROM result_journal
                WHERE node_namespace = ? AND task_id = ? AND submission_id = ?
                """,
                (node_namespace, task_id, submission_id),
            ).fetchone()
            if row is not None:
                if row["completed_at"] is not None:
                    if row["receipt_json"] is None:
                        raise ResultReceiptPruned(
                            "The Result completed, but its local receipt was pruned"
                        )
                    connection.commit()
                    return ResultClaim(
                        prepared=None,
                        receipt=json.loads(row["receipt_json"]),
                        claim_token=None,
                    )
                if row["result_json"] is None:
                    raise RuntimeError("Prepared Result journal row is inconsistent")
                connection.commit()
                return ResultClaim(
                    prepared=json.loads(row["result_json"]),
                    receipt=None,
                    claim_token=None,
                )

            prior_result_for_lease = connection.execute(
                """
                SELECT submission_id FROM result_journal
                WHERE node_namespace = ? AND task_id = ? AND lease_identity = ?
                """,
                (node_namespace, task_id, lease_identity),
            ).fetchone()
            if prior_result_for_lease is not None:
                raise ResultExecutionInProgress(
                    "This lease already prepared a Result under another submission id"
                )

            lease_claim = connection.execute(
                """
                SELECT submission_id FROM execution_claims
                WHERE node_namespace = ? AND task_id = ? AND lease_identity = ?
                """,
                (node_namespace, task_id, lease_identity),
            ).fetchone()
            if lease_claim is not None:
                raise ResultExecutionInProgress(
                    "Another worker already owns execution for this lease"
                )

            connection.execute(
                """
                DELETE FROM execution_claims
                WHERE node_namespace = ? AND task_id = ? AND lease_identity <> ?
                """,
                (node_namespace, task_id, lease_identity),
            )
            claim_token = secrets.token_urlsafe(24)
            connection.execute(
                """
                INSERT INTO execution_claims (
                    node_namespace, task_id, submission_id, claim_token,
                    lease_identity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (node_namespace, task_id, submission_id) DO UPDATE SET
                    claim_token = excluded.claim_token,
                    lease_identity = excluded.lease_identity,
                    created_at = excluded.created_at
                """,
                (
                    node_namespace,
                    task_id,
                    submission_id,
                    claim_token,
                    lease_identity,
                    time.time(),
                ),
            )
            connection.commit()
            return ResultClaim(None, None, claim_token)

    def prepare(
        self,
        node_namespace: str,
        task_id: str,
        submission_id: str,
        claim_token: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = canonical_json(result)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError(f"A prepared Result must be at most {MAX_RESULT_BYTES} bytes")
        result_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT result_json, result_digest, completed_at FROM result_journal
                WHERE node_namespace = ? AND task_id = ? AND submission_id = ?
                """,
                (node_namespace, task_id, submission_id),
            ).fetchone()
            if row is not None:
                if row["result_digest"] != result_digest:
                    raise RuntimeError(
                        "The submission id is already journaled with another Result"
                    )
                if row["result_json"] is None:
                    raise RuntimeError("The prepared Result was already completed")
                connection.commit()
                return json.loads(row["result_json"])
            claim = connection.execute(
                """
                SELECT claim_token, lease_identity FROM execution_claims
                WHERE node_namespace = ? AND task_id = ? AND submission_id = ?
                """,
                (node_namespace, task_id, submission_id),
            ).fetchone()
            if claim is None or not secrets.compare_digest(
                claim["claim_token"], claim_token
            ):
                raise ResultExecutionInProgress(
                    "This worker no longer owns the execution claim"
                )
            now = time.time()
            connection.execute(
                """
                INSERT INTO result_journal (
                    node_namespace, task_id, submission_id, lease_identity,
                    result_json, result_digest, receipt_json, receipt_digest,
                    created_at, completed_at, receipt_pruned_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL)
                """,
                (
                    node_namespace,
                    task_id,
                    submission_id,
                    claim["lease_identity"],
                    encoded,
                    result_digest,
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM execution_claims
                WHERE node_namespace = ? AND task_id = ? AND submission_id = ?
                """,
                (node_namespace, task_id, submission_id),
            )
            connection.commit()
        return json.loads(encoded)

    def record_receipt(
        self,
        node_namespace: str,
        task_id: str,
        submission_id: str,
        receipt: dict[str, Any],
    ) -> None:
        encoded = canonical_json(receipt)
        if len(encoded.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise ValueError(f"A Result receipt must be at most {MAX_RECEIPT_BYTES} bytes")
        receipt_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT receipt_json, receipt_digest, completed_at, receipt_pruned_at
                FROM result_journal
                WHERE node_namespace = ? AND task_id = ? AND submission_id = ?
                """,
                (node_namespace, task_id, submission_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Cannot record a receipt before preparing its Result")
            if row["completed_at"] is not None:
                if row["receipt_digest"] != receipt_digest:
                    raise RuntimeError(
                        "The coordinator returned a different receipt for one submission"
                    )
                connection.commit()
                return
            connection.execute(
                """
                UPDATE result_journal
                SET result_json = NULL, receipt_json = ?, receipt_digest = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE node_namespace = ? AND task_id = ? AND submission_id = ?
                """,
                (
                    encoded,
                    receipt_digest,
                    time.time(),
                    node_namespace,
                    task_id,
                    submission_id,
                ),
            )
            connection.commit()

    def prune_completed_receipts(
        self, *, completed_before: float, limit: int = 1000
    ) -> int:
        now = time.time()
        if (
            not isinstance(completed_before, (int, float))
            or isinstance(completed_before, bool)
            or not math.isfinite(completed_before)
            or completed_before > now
        ):
            raise ValueError("completed_before must be a finite, non-future timestamp")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT node_namespace, task_id, submission_id
                FROM result_journal
                WHERE completed_at < ? AND receipt_json IS NOT NULL
                ORDER BY completed_at LIMIT ?
                """,
                (completed_before, limit),
            ).fetchall()
            connection.executemany(
                """
                UPDATE result_journal
                SET receipt_json = NULL, receipt_pruned_at = ?
                WHERE node_namespace = ? AND task_id = ? AND submission_id = ?
                """,
                [
                    (now, row["node_namespace"], row["task_id"], row["submission_id"])
                    for row in rows
                ],
            )
            connection.commit()
            return len(rows)

@dataclass
class SolverHandler:
    manifest: dict[str, Any]
    execute: ExecuteFunction


class ConnectedNode:
    """Small adapter for connecting ordinary Python callables as Solver abilities."""

    def __init__(
        self, client: CoordinatorClient, *, result_journal: ResultJournal
    ) -> None:
        if not client.node_token:
            raise ValueError("ConnectedNode requires a client with node_token")
        self.client = client
        self.result_journal = result_journal
        self._journal_namespace = hashlib.sha256(
            f"{client.base_url}\x1f{client.node_token}".encode("utf-8")
        ).hexdigest()
        self._handlers: dict[str, SolverHandler] = {}

    def publish(self, handler: SolverHandler) -> dict[str, Any]:
        offering = self.client.publish_offering(handler.manifest)
        self._handlers[offering["offering_id"]] = handler
        return offering

    def work_once(
        self, *, lease_request_id: str, submission_id: str
    ) -> dict[str, Any] | None:
        submission_id = require_string(submission_id, "submission_id")
        require(
            len(submission_id) <= 200,
            "invalid_result",
            "submission_id is too long",
        )
        lease = self.client.lease(lease_request_id=lease_request_id)
        if lease is None:
            return None
        task_id = lease["task"]["task_id"]
        claim = self.result_journal.claim(
            self._journal_namespace,
            task_id,
            submission_id,
            hashlib.sha256(lease["lease_token"].encode("utf-8")).hexdigest(),
        )
        if claim.receipt is not None:
            return claim.receipt
        prepared = claim.prepared
        if prepared is None:
            if claim.claim_token is None:
                raise RuntimeError("Result journal returned no prepared Result or claim")
            offering_id = lease["offering"]["offering_id"]
            handler = self._handlers.get(offering_id)
            if handler is None:
                raise RuntimeError(f"No local handler is bound to offering {offering_id}")

            started = time.monotonic()
            try:
                task_context = dict(lease["task"])
                task_context["problem"] = lease["problem"]
                outputs = handler.execute(task_context)
                if not isinstance(outputs, dict):
                    raise TypeError("A Solver handler must return a JSON object")
                status = "success"
                stop_reason = "completed"
                evidence: dict[str, Any] = {"adapter": "python-callable-v1"}
            except Exception as exc:  # Report a typed failure instead of losing the lease.
                outputs = {}
                status = "fail"
                stop_reason = "quality"
                evidence = {
                    "adapter": "python-callable-v1",
                    "error_type": type(exc).__name__,
                }
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            try:
                normalized = validate_result(
                    {
                        "submission_id": submission_id,
                        "lease_token": lease["lease_token"],
                        "status": status,
                        "stop_reason": stop_reason,
                        "outputs": outputs,
                        "evidence": evidence,
                        "usage": {"duration_ms": duration_ms},
                    }
                )
            except Exception as exc:
                normalized = validate_result(
                    {
                        "submission_id": submission_id,
                        "lease_token": lease["lease_token"],
                        "status": "fail",
                        "stop_reason": "quality",
                        "outputs": {},
                        "evidence": {
                            "adapter": "python-callable-v1",
                            "error_type": type(exc).__name__,
                            "stage": "result_validation",
                        },
                        "usage": {"duration_ms": duration_ms},
                    }
                )
            prepared = self.result_journal.prepare(
                self._journal_namespace,
                task_id,
                submission_id,
                claim.claim_token,
                {
                    key: value
                    for key, value in normalized.items()
                    if key not in {"submission_id", "lease_token"}
                },
            )

        receipt = self.client.submit(
            task_id,
            submission_id=submission_id,
            lease_token=lease["lease_token"],
            status=prepared["status"],
            stop_reason=prepared["stop_reason"],
            outputs=prepared["outputs"],
            evidence=prepared["evidence"],
            usage=prepared["usage"],
        )
        self.result_journal.record_receipt(
            self._journal_namespace, task_id, submission_id, receipt
        )
        return receipt
