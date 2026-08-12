from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Callable

from .database import Database
from .errors import DomainError, require
from .gates import evaluate_result
from .protocol import (
    MAX_RESULT_BYTES,
    canonical_json,
    content_digest,
    require_minor_units,
    require_object,
    require_string,
    validate_child_constraints,
    validate_manifest,
    validate_result,
    validate_task_spec,
)
from .schema import validate_instance


_MISSING = object()


@dataclass(frozen=True)
class ServiceConfig:
    lease_seconds: int = 60
    platform_fee_bps: int = 0
    max_depth: int = 12
    max_inflight_per_node: int = 1
    max_delegation_proposals: int = 3
    max_tasks_per_problem: int = 100
    max_offerings_per_node: int = 50

    def __post_init__(self) -> None:
        if not 5 <= self.lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 5 and 86400")
        if not 0 <= self.platform_fee_bps < 10_000:
            raise ValueError("platform_fee_bps must be between 0 and 9999")
        if not 1 <= self.max_depth <= 100:
            raise ValueError("max_depth must be between 1 and 100")
        if not 1 <= self.max_inflight_per_node <= 100:
            raise ValueError("max_inflight_per_node must be between 1 and 100")
        if not 1 <= self.max_delegation_proposals <= 20:
            raise ValueError("max_delegation_proposals must be between 1 and 20")
        if not 1 <= self.max_tasks_per_problem <= 10_000:
            raise ValueError("max_tasks_per_problem must be between 1 and 10000")
        if not 1 <= self.max_offerings_per_node <= 10_000:
            raise ValueError("max_offerings_per_node must be between 1 and 10000")


class CoordinatorService:
    def __init__(
        self,
        database: Database,
        *,
        config: ServiceConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.config = config or ServiceConfig()
        self.clock = clock
        self.database.initialize()
        self._backfill_accepted_artifacts()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _derive_token(secret: str, namespace: str, *parts: str) -> str:
        """Derive a replayable bearer token without storing its plaintext."""
        message = "\x1f".join((namespace, *parts)).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    @staticmethod
    def _loads(value: str) -> Any:
        return json.loads(value)

    def _retained_binding_descriptors(
        self, connection: sqlite3.Connection, task_id: str
    ) -> list[dict[str, str]]:
        rows = connection.execute(
            """
            SELECT binding_name, artifact_digest
            FROM retained_artifact_bindings
            WHERE successor_task_id = ?
            ORDER BY binding_name, id
            """,
            (task_id,),
        ).fetchall()
        return [
            {
                "binding": row["binding_name"],
                "artifact_digest": row["artifact_digest"],
            }
            for row in rows
        ]

    def _task_contract(
        self, connection: sqlite3.Connection, task_id: str
    ) -> dict[str, Any]:
        task = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise RuntimeError("Stored task contract is missing")
        return {
            "required_capability": task["required_capability"],
            "operation": task["operation"],
            "inputs": self._loads(task["inputs_json"]),
            "constraints": self._loads(task["constraints_json"]),
            "accept_spec": self._loads(task["accept_spec_json"]),
            "reward_minor": task["reward_minor"],
            "delegation_budget_minor": task["delegation_budget_minor"],
            "max_attempts": task["max_attempts"],
            "retained_artifact_bindings": self._retained_binding_descriptors(
                connection, task_id
            ),
        }

    def _load_artifact_envelope(
        self, envelope_json: str, artifact_digest: str
    ) -> dict[str, Any]:
        envelope = self._loads(envelope_json)
        if content_digest(envelope) != artifact_digest:
            raise RuntimeError("Stored accepted artifact digest is inconsistent")
        return envelope

    def _backfill_accepted_artifacts(self) -> None:
        """Idempotently archive passing submissions created before schema v2."""

        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT s.id FROM submissions s
                LEFT JOIN accepted_artifacts a ON a.source_submission_id = s.id
                WHERE s.gate_outcome = 'pass' AND a.digest IS NULL
                ORDER BY s.created_at, s.id
                """
            ).fetchall()
            for row in rows:
                self._archive_accepted_submission(connection, row["id"])

    def _archive_accepted_submission(
        self, connection: sqlite3.Connection, submission_id: str
    ) -> dict[str, Any]:
        existing = connection.execute(
            "SELECT * FROM accepted_artifacts WHERE source_submission_id = ?",
            (submission_id,),
        ).fetchone()
        if existing is not None:
            return {
                "artifact_digest": existing["digest"],
                "envelope": self._load_artifact_envelope(
                    existing["envelope_json"], existing["digest"]
                ),
            }
        row = connection.execute(
            """
            SELECT s.id AS submission_id, s.result_json, s.gate_json,
                   s.gate_outcome,
                   s.created_at, t.id AS task_id, t.problem_id,
                   t.required_capability, t.operation, t.inputs_json,
                   t.constraints_json, p.problem_class
            FROM submissions s
            JOIN tasks t ON t.id = s.task_id
            JOIN problems p ON p.id = t.problem_id
            WHERE s.id = ?
            """,
            (submission_id,),
        ).fetchone()
        require(
            row is not None and row["gate_outcome"] == "pass",
            "invalid_artifact_source",
            "Only a passing submission can become an accepted artifact",
        )
        result = self._loads(row["result_json"])
        source_contract_digest = content_digest(
            self._task_contract(connection, row["task_id"])
        )
        gate_digest = content_digest(self._loads(row["gate_json"]))
        envelope = {
            "protocol_version": "1",
            "kind": "accepted_solver_artifact",
            "source": {
                "problem_id": row["problem_id"],
                "problem_class": row["problem_class"],
                "task_id": row["task_id"],
                "submission_receipt_id": row["submission_id"],
            },
            "contract": {
                "required_capability": row["required_capability"],
                "operation": row["operation"],
                "inputs": self._loads(row["inputs_json"]),
                "constraints": self._loads(row["constraints_json"]),
            },
            "result": {
                "status": result["status"],
                "stop_reason": result["stop_reason"],
                "outputs": result["outputs"],
                "evidence": result["evidence"],
            },
            "verification": {
                "outcome": "pass",
                "evaluator": "coordinator:deterministic-v1",
                "scope": "source_contract_only",
            },
        }
        digest = content_digest(envelope)
        connection.execute(
            """
            INSERT INTO accepted_artifacts (
                digest, source_submission_id, source_contract_digest,
                gate_digest, envelope_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                digest,
                submission_id,
                source_contract_digest,
                gate_digest,
                canonical_json(envelope),
                row["created_at"],
            ),
        )
        return {"artifact_digest": digest, "envelope": envelope}

    def create_node_invite(self, body: Any) -> dict[str, Any]:
        payload = require_object(body, "body")
        label = require_string(payload.get("label", "provider invite"), "label")
        expires_in = payload.get("expires_in_seconds", 3600)
        valid_expiry = isinstance(expires_in, int) and not isinstance(expires_in, bool)
        require(
            valid_expiry and 60 <= expires_in <= 604_800,
            "invalid_invite",
            "expires_in_seconds must be between 60 and 604800",
        )
        invite_id = self._new_id("invite")
        invite_token = self._new_token()
        with self.database.transaction() as connection:
            now = self.clock()
            expires_at = now + expires_in
            connection.execute(
                """
                INSERT INTO node_invites (
                    id, token_hash, label, expires_at, max_uses, use_count, created_at
                ) VALUES (?, ?, ?, ?, 1, 0, ?)
                """,
                (
                    invite_id,
                    self._token_hash(invite_token),
                    label,
                    expires_at,
                    now,
                ),
            )
        return {
            "invite_id": invite_id,
            "invite_token": invite_token,
            "label": label,
            "expires_at": expires_at,
            "max_uses": 1,
            "warning": "The invite token is returned once and is single-use.",
        }

    def register_node(self, invite_token: str | None, body: Any) -> dict[str, Any]:
        payload = require_object(body, "body")
        registration_id = require_string(
            payload.get("registration_id"), "registration_id"
        )
        require(
            len(registration_id) <= 200,
            "invalid_registration",
            "registration_id is too long",
        )
        operator_name = require_string(payload.get("operator_name"), "operator_name")
        metadata = require_object(payload.get("metadata", {}), "metadata")
        request_digest = content_digest(
            {"operator_name": operator_name, "metadata": metadata}
        )
        with self.database.transaction() as connection:
            now = self.clock()
            invite = connection.execute(
                "SELECT * FROM node_invites WHERE token_hash = ?",
                (self._token_hash(invite_token or ""),),
            ).fetchone()
            if invite is None:
                raise DomainError(
                    "invalid_invite",
                    "The node invite is invalid, expired, or already used",
                    status=401,
                )
            if invite["expires_at"] <= now:
                raise DomainError(
                    "invalid_invite",
                    "The node invite is invalid, expired, or already used",
                    status=401,
                )
            prior = connection.execute(
                "SELECT * FROM node_registrations WHERE invite_id = ?",
                (invite["id"],),
            ).fetchone()
            if prior is not None:
                if (
                    prior["registration_id"] != registration_id
                    or prior["request_digest"] != request_digest
                ):
                    raise DomainError(
                        "idempotency_conflict",
                        "The invite was already used for another registration request",
                        status=409,
                    )
                node_id = prior["node_id"]
                node_token = self._derive_token(
                    invite_token or "",
                    "node-registration-v1",
                    registration_id,
                    request_digest,
                )
                return {
                    "registration_id": registration_id,
                    "node_id": node_id,
                    "node_token": node_token,
                    "status": "active",
                    "warning": "Store the node token and registration id securely.",
                }
            if invite["use_count"] >= invite["max_uses"]:
                raise DomainError(
                    "invalid_invite",
                    "The node invite is invalid, expired, or already used",
                    status=401,
                )
            node_id = self._new_id("node")
            node_token = self._derive_token(
                invite_token or "",
                "node-registration-v1",
                registration_id,
                request_digest,
            )
            connection.execute(
                "UPDATE node_invites SET use_count = use_count + 1 WHERE id = ?",
                (invite["id"],),
            )
            connection.execute(
                """
                INSERT INTO nodes (
                    id, operator_name, token_hash, metadata_json,
                    status, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    node_id,
                    operator_name,
                    self._token_hash(node_token),
                    canonical_json(metadata),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO node_registrations (
                    invite_id, registration_id, request_digest, node_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (invite["id"], registration_id, request_digest, node_id, now),
            )
        return {
            "registration_id": registration_id,
            "node_id": node_id,
            "node_token": node_token,
            "status": "active",
            "warning": "Store the node token and registration id securely.",
        }

    def _authenticate_node(
        self, connection: sqlite3.Connection, token: str | None
    ) -> sqlite3.Row:
        if not token:
            raise DomainError("unauthorized", "A node bearer token is required", status=401)
        token_hash = self._token_hash(token)
        row = connection.execute(
            "SELECT * FROM nodes WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None or row["status"] != "active":
            raise DomainError("unauthorized", "The node token is invalid or suspended", status=401)
        connection.execute(
            "UPDATE nodes SET last_seen_at = ? WHERE id = ?", (self.clock(), row["id"])
        )
        return row

    def publish_offering(self, node_token: str | None, body: Any) -> dict[str, Any]:
        manifest = validate_manifest(body)
        manifest_digest = content_digest(manifest)
        with self.database.transaction() as connection:
            now = self.clock()
            node = self._authenticate_node(connection, node_token)
            existing = connection.execute(
                """
                SELECT * FROM offerings
                WHERE node_id = ? AND manifest_digest = ?
                """,
                (node["id"], manifest_digest),
            ).fetchone()
            if existing is not None:
                offering_id = existing["id"]
                created = False
                active = bool(existing["active"])
            else:
                offering_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM offerings WHERE node_id = ?",
                    (node["id"],),
                ).fetchone()["count"]
                require(
                    offering_count < self.config.max_offerings_per_node,
                    "offering_limit",
                    "The node reached its configured offering limit",
                )
                offering_id = self._new_id("offering")
                connection.execute(
                    """
                    INSERT INTO offerings (
                        id, node_id, manifest_digest, concept_ref,
                        manifest_json, active, created_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        offering_id,
                        node["id"],
                        manifest_digest,
                        manifest["concept_ref"],
                        canonical_json(manifest),
                        now,
                    ),
                )
                created = True
                active = False
        return {
            "offering_id": offering_id,
            "node_id": node["id"],
            "manifest_digest": manifest_digest,
            "concept_ref": manifest["concept_ref"],
            "created": created,
            "active": active,
            "admission_status": "active" if active else "pending",
        }

    def approve_offering(self, offering_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            now = self.clock()
            offering = connection.execute(
                "SELECT * FROM offerings WHERE id = ?", (offering_id,)
            ).fetchone()
            if offering is None:
                raise DomainError("not_found", "Offering not found", status=404)
            connection.execute(
                """
                UPDATE offerings
                SET active = 1, approved_at = COALESCE(approved_at, ?)
                WHERE id = ?
                """,
                (now, offering_id),
            )
            node = connection.execute(
                "SELECT status FROM nodes WHERE id = ?", (offering["node_id"],)
            ).fetchone()
            return {
                "offering_id": offering_id,
                "node_id": offering["node_id"],
                "manifest_digest": offering["manifest_digest"],
                "concept_ref": offering["concept_ref"],
                "active": True,
                "admission_status": "active",
                "node_status": node["status"],
                "approved_at": offering["approved_at"] or now,
            }

    def list_offerings(self, status: str | None = None) -> dict[str, Any]:
        if status is not None:
            require(
                status in {"pending", "active"},
                "invalid_status",
                "Offering status must be pending or active",
            )
        with closing(self.database.connect()) as connection:
            parameters: tuple[Any, ...] = ()
            where = ""
            if status is not None:
                where = "WHERE o.active = ?"
                parameters = (1 if status == "active" else 0,)
            rows = connection.execute(
                f"""
                SELECT o.id, o.node_id, o.manifest_digest, o.concept_ref, o.active,
                       o.manifest_json, o.created_at, o.approved_at,
                       n.operator_name, n.metadata_json,
                       n.status AS node_status
                FROM offerings o
                JOIN nodes n ON n.id = o.node_id
                {where} ORDER BY o.created_at, o.id
                """,
                parameters,
            ).fetchall()
            return {
                "offerings": [
                    {
                        "offering_id": row["id"],
                        "node_id": row["node_id"],
                        "operator_name": row["operator_name"],
                        "node_status": row["node_status"],
                        "self_asserted_node_metadata": self._loads(
                            row["metadata_json"]
                        ),
                        "manifest_digest": row["manifest_digest"],
                        "concept_ref": row["concept_ref"],
                        "manifest": self._loads(row["manifest_json"]),
                        "active": bool(row["active"]),
                        "admission_status": "active" if row["active"] else "pending",
                        "created_at": row["created_at"],
                        "approved_at": row["approved_at"],
                    }
                    for row in rows
                ]
            }

    def heartbeat(self, node_token: str | None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            node = self._authenticate_node(connection, node_token)
            now = self.clock()
        return {"node_id": node["id"], "status": "active", "last_seen_at": now}

    def _normalize_problem_request(
        self, body: Any, *, field: str = "body"
    ) -> dict[str, Any]:
        payload = require_object(body, field)
        intent = require_string(payload.get("intent"), "intent")
        problem_class = require_string(payload.get("problem_class"), "problem_class")
        funded = require_minor_units(payload.get("funded_amount_minor"), "funded_amount_minor")
        currency = require_string(payload.get("currency", "USD"), "currency").upper()
        require(
            len(currency) == 3 and currency.isalpha(),
            "invalid_currency",
            "currency must be a three-letter alphabetic code",
        )
        funding_reference = require_string(payload.get("funding_reference"), "funding_reference")
        deadline_at = payload.get("deadline_at")
        if deadline_at is not None:
            valid_deadline = isinstance(deadline_at, (int, float)) and not isinstance(
                deadline_at, bool
            )
            require(
                valid_deadline,
                "invalid_deadline",
                "deadline_at must be a Unix timestamp",
            )
            deadline_at = float(deadline_at)
        task_spec = validate_task_spec(payload.get("task"))
        require(
            task_spec["reward_minor"] + task_spec["delegation_budget_minor"] <= funded,
            "insufficient_funding",
            "The initial task reward and delegation budget exceed problem funding",
        )
        normalized = {
            "intent": intent,
            "problem_class": problem_class,
            "funded_amount_minor": funded,
            "currency": currency,
            "funding_reference": funding_reference,
            "deadline_at": deadline_at,
            "task": task_spec,
        }
        normalized["request_digest"] = content_digest(normalized)
        return normalized

    def create_problem(self, body: Any) -> dict[str, Any]:
        request = self._normalize_problem_request(body)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM problems WHERE funding_reference = ?",
                (request["funding_reference"],),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request["request_digest"]:
                    raise DomainError(
                        "idempotency_conflict",
                        "The funding reference was already used with different request fields",
                        status=409,
                    )
                return self._problem_view(connection, existing["id"])

            problem_id, _ = self._insert_problem_request(
                connection, request, now=self.clock()
            )
            return self._problem_view(connection, problem_id)

    def _insert_problem_request(
        self,
        connection: sqlite3.Connection,
        request: dict[str, Any],
        *,
        now: float,
    ) -> tuple[str, str]:
        deadline_at = request["deadline_at"]
        require(
            deadline_at is not None and deadline_at > now,
            "invalid_deadline",
            "A future deadline_at is required so funded work cannot remain locked indefinitely",
        )
        task_spec = request["task"]
        self._require_task_manifest(connection, task_spec)
        problem_id = self._new_id("problem")
        task_id = self._new_id("task")
        connection.execute(
            """
            INSERT INTO problems (
                id, intent, problem_class, funded_amount_minor, currency, platform_fee_bps,
                funding_reference, request_digest, deadline_at,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                problem_id,
                request["intent"],
                request["problem_class"],
                request["funded_amount_minor"],
                request["currency"],
                self.config.platform_fee_bps,
                request["funding_reference"],
                request["request_digest"],
                deadline_at,
                now,
                now,
            ),
        )
        self._insert_task(
            connection,
            task_id=task_id,
            problem_id=problem_id,
            parent_id=None,
            depth=0,
            spec=task_spec,
            now=now,
        )
        self._insert_transfer(
            connection,
            currency=request["currency"],
            amount=request["funded_amount_minor"],
            from_account="external:funding",
            to_account=f"escrow:{problem_id}",
            reason="confirmed_platform_funding",
            idempotency_key=f"funding:{request['funding_reference']}",
            problem_id=problem_id,
            task_id=None,
            node_id=None,
            now=now,
        )
        return problem_id, task_id

    def reframe_problem(self, source_problem_id: str, body: Any) -> dict[str, Any]:
        """Create an operator-authorized successor after a rejected root frame.

        This is deliberately not an autonomous diagnosis. The administrator
        supplies a structured FrameError, a separately versioned root contract,
        and the exact accepted descendant artifacts that may be reused.
        """

        payload = require_object(body, "body")
        idempotency_key = require_string(
            payload.get("idempotency_key"), "idempotency_key"
        )
        require(
            len(idempotency_key) <= 200,
            "invalid_reframe",
            "idempotency_key is too long",
        )
        source_submission_id = require_string(
            payload.get("source_submission_receipt_id"),
            "source_submission_receipt_id",
        )
        diagnosis_value = require_object(payload.get("diagnosis"), "diagnosis")
        require(
            diagnosis_value.get("kind") == "frame_error",
            "invalid_reframe",
            "diagnosis.kind must be frame_error",
        )
        required_changes_value = diagnosis_value.get("required_changes")
        require(
            isinstance(required_changes_value, list)
            and 1 <= len(required_changes_value) <= 50,
            "invalid_reframe",
            "diagnosis.required_changes must contain between 1 and 50 items",
        )
        diagnosis = {
            "kind": "frame_error",
            "summary": require_string(
                diagnosis_value.get("summary"), "diagnosis.summary"
            ),
            "diagnosed_by": require_string(
                diagnosis_value.get("diagnosed_by"), "diagnosis.diagnosed_by"
            ),
            "required_changes": [
                require_string(item, "diagnosis.required_changes[]")
                for item in required_changes_value
            ],
            "evidence": require_object(
                diagnosis_value.get("evidence", {}), "diagnosis.evidence"
            ),
        }
        require(
            len(canonical_json(diagnosis).encode("utf-8")) <= 64 * 1024,
            "invalid_reframe",
            "The FrameError diagnosis is too large",
        )
        retained_values = payload.get("retained_artifacts")
        require(
            isinstance(retained_values, list) and 1 <= len(retained_values) <= 100,
            "invalid_reframe",
            "retained_artifacts must contain between 1 and 100 bindings",
        )
        normalized_retained: list[dict[str, str]] = []
        for index, item in enumerate(retained_values):
            binding = require_object(item, f"retained_artifacts[{index}]")
            binding_name = require_string(
                binding.get("binding"), f"retained_artifacts[{index}].binding"
            )
            require(
                len(binding_name) <= 200,
                "invalid_reframe",
                "A retained artifact binding is too long",
            )
            normalized_retained.append(
                {
                    "binding": binding_name,
                    "source_submission_receipt_id": require_string(
                        binding.get("source_submission_receipt_id"),
                        f"retained_artifacts[{index}].source_submission_receipt_id",
                    ),
                }
            )
        require(
            len({item["binding"] for item in normalized_retained})
            == len(normalized_retained),
            "invalid_reframe",
            "retained artifact binding names must be unique",
        )
        require(
            len(
                {
                    item["source_submission_receipt_id"]
                    for item in normalized_retained
                }
            )
            == len(normalized_retained),
            "invalid_reframe",
            "retained source submissions must be unique",
        )
        normalized_retained.sort(key=lambda item: item["binding"])
        successor = self._normalize_problem_request(
            payload.get("successor_problem"), field="successor_problem"
        )
        request_digest = content_digest(
            {
                "source_problem_id": source_problem_id,
                "source_submission_receipt_id": source_submission_id,
                "diagnosis": diagnosis,
                "retained_artifacts": normalized_retained,
                "successor_problem": successor,
            }
        )

        with self.database.transaction() as connection:
            now = self.clock()
            self._expire_due_problems(connection, now)
            self._expire_leases(connection, now)
            existing = connection.execute(
                """
                SELECT * FROM problem_reframes
                WHERE source_problem_id = ? AND idempotency_key = ?
                """,
                (source_problem_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise DomainError(
                        "idempotency_conflict",
                        "The reframe idempotency key was reused with different fields",
                        status=409,
                    )
                return self._reframe_view(connection, existing["id"])

            source = connection.execute(
                "SELECT * FROM problems WHERE id = ?", (source_problem_id,)
            ).fetchone()
            if source is None:
                raise DomainError("not_found", "Source problem not found", status=404)
            require(
                source["status"] in {"active", "blocked"},
                "reframe_not_allowed",
                "Only an active or blocked source problem can be reframed",
                status=409,
            )
            source_submission = connection.execute(
                """
                SELECT s.*, t.parent_id, t.depth, t.constraints_json,
                       t.state AS task_state
                FROM submissions s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id = ? AND t.problem_id = ?
                """,
                (source_submission_id, source_problem_id),
            ).fetchone()
            require(
                source_submission is not None
                and source_submission["parent_id"] is None
                and source_submission["depth"] == 0
                and source_submission["gate_outcome"] == "reject"
                and self._loads(source_submission["gate_json"]).get("evaluator")
                == "coordinator:deterministic-v1",
                "invalid_frame_error_source",
                "A reframe must cite a rejected submitted root Result, "
                "not a lease timeout",
            )
            latest_root_submission = connection.execute(
                """
                SELECT s.id FROM submissions s
                JOIN tasks t ON t.id = s.task_id
                WHERE t.problem_id = ? AND t.parent_id IS NULL
                ORDER BY s.rowid DESC LIMIT 1
                """,
                (source_problem_id,),
            ).fetchone()
            require(
                latest_root_submission is not None
                and latest_root_submission["id"] == source_submission_id,
                "stale_frame_error",
                "A reframe must cite the latest root submission",
                status=409,
            )
            require(
                source_submission["task_state"] in {"open", "failed", "blocked"},
                "reframe_not_quiescent",
                "The source root must not be leased or waiting on delegation",
                status=409,
            )
            unresolved_descendants = connection.execute(
                """
                SELECT COUNT(*) AS count FROM tasks
                WHERE problem_id = ? AND parent_id IS NOT NULL
                  AND state <> 'accepted'
                """,
                (source_problem_id,),
            ).fetchone()["count"]
            require(
                unresolved_descendants == 0,
                "reframe_not_quiescent",
                "All descendant work must be accepted before the root is reframed",
                status=409,
            )
            proposed_delegations = connection.execute(
                """
                SELECT COUNT(*) AS count FROM delegations d
                JOIN tasks t ON t.id = d.parent_task_id
                WHERE t.problem_id = ? AND d.status = 'proposed'
                """,
                (source_problem_id,),
            ).fetchone()["count"]
            require(
                proposed_delegations == 0,
                "reframe_not_quiescent",
                "Pending delegation proposals must be decided before reframing",
                status=409,
            )
            prior_reframe = connection.execute(
                "SELECT id FROM problem_reframes WHERE source_submission_id = ?",
                (source_submission_id,),
            ).fetchone()
            require(
                prior_reframe is None,
                "reframe_conflict",
                "The cited root rejection already has a successor",
                status=409,
            )
            require(
                successor["intent"] == source["intent"]
                and successor["problem_class"] == source["problem_class"],
                "reframe_identity_mismatch",
                "A successor must preserve the source intent and problem class",
            )
            require(
                successor["currency"] == source["currency"],
                "reframe_currency_mismatch",
                "A successor must use the source problem currency",
            )
            source_root_contract = self._task_contract(
                connection, source_submission["task_id"]
            )
            source_root_contract_digest = content_digest(source_root_contract)
            source_frame_contract = {
                key: source_root_contract[key]
                for key in (
                    "required_capability",
                    "operation",
                    "inputs",
                    "constraints",
                    "accept_spec",
                )
            }
            successor_frame_contract = {
                key: successor["task"][key]
                for key in (
                    "required_capability",
                    "operation",
                    "inputs",
                    "constraints",
                    "accept_spec",
                )
            }
            require(
                content_digest(successor_frame_contract)
                != content_digest(source_frame_contract),
                "unversioned_root_contract",
                "A reframe must change the structural root task contract",
            )
            validate_child_constraints(
                self._loads(source_submission["constraints_json"]),
                successor["task"]["constraints"],
            )
            funding_collision = connection.execute(
                "SELECT id FROM problems WHERE funding_reference = ?",
                (successor["funding_reference"],),
            ).fetchone()
            require(
                funding_collision is None,
                "idempotency_conflict",
                "The successor funding reference is already in use",
                status=409,
            )

            retained: list[dict[str, Any]] = []
            for retained_binding in normalized_retained:
                retained_id = retained_binding["source_submission_receipt_id"]
                row = connection.execute(
                    """
                    SELECT s.*, t.problem_id, t.parent_id, t.depth,
                           t.state AS task_state, a.digest AS artifact_digest,
                           a.source_contract_digest, a.gate_digest,
                           a.envelope_json
                    FROM submissions s
                    JOIN tasks t ON t.id = s.task_id
                    JOIN accepted_artifacts a ON a.source_submission_id = s.id
                    WHERE s.id = ?
                    """,
                    (retained_id,),
                ).fetchone()
                require(
                    row is not None
                    and row["problem_id"] == source_problem_id
                    and row["parent_id"] is not None
                    and row["depth"] > 0
                    and row["gate_outcome"] == "pass"
                    and row["task_state"] == "accepted"
                    and row["earning_status"] == "available",
                    "invalid_retained_artifact",
                    "Every retained artifact must be accepted, payable-backed "
                    "descendant work from the source problem",
                )
                authoritative_contract_digest = content_digest(
                    self._task_contract(connection, row["task_id"])
                )
                authoritative_gate_digest = content_digest(
                    self._loads(row["gate_json"])
                )
                if (
                    row["source_contract_digest"]
                    != authoritative_contract_digest
                    or row["gate_digest"] != authoritative_gate_digest
                ):
                    raise RuntimeError(
                        "Stored accepted artifact provenance is inconsistent"
                    )
                retained.append(
                    {
                        "binding": retained_binding["binding"],
                        "source_submission_id": row["id"],
                        "artifact_digest": row["artifact_digest"],
                        "artifact": self._load_artifact_envelope(
                            row["envelope_json"], row["artifact_digest"]
                        ),
                    }
                )
            retained.sort(key=lambda item: item["binding"])
            require(
                len(canonical_json(retained).encode("utf-8")) <= MAX_RESULT_BYTES,
                "retained_artifacts_too_large",
                "The retained artifact envelope exceeds the Result-size boundary",
            )
            successor_binding_descriptors = [
                {
                    "binding": item["binding"],
                    "artifact_digest": item["artifact_digest"],
                }
                for item in retained
            ]
            successor_root_contract = {
                **successor["task"],
                "retained_artifact_bindings": successor_binding_descriptors,
            }
            successor_root_contract_digest = content_digest(
                successor_root_contract
            )

            successor_problem_id, successor_task_id = self._insert_problem_request(
                connection, successor, now=now
            )
            reframe_id = self._new_id("reframe")
            connection.execute(
                """
                INSERT INTO problem_reframes (
                    id, idempotency_key, request_digest, source_problem_id,
                    source_root_task_id, source_submission_id,
                    source_root_contract_digest, diagnosis_json,
                    successor_problem_id, successor_root_task_id,
                    successor_root_contract_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reframe_id,
                    idempotency_key,
                    request_digest,
                    source_problem_id,
                    source_submission["task_id"],
                    source_submission_id,
                    source_root_contract_digest,
                    canonical_json(diagnosis),
                    successor_problem_id,
                    successor_task_id,
                    successor_root_contract_digest,
                    now,
                ),
            )
            for artifact in retained:
                connection.execute(
                    """
                    INSERT INTO retained_artifact_bindings (
                        id, reframe_id, successor_task_id, binding_name,
                        source_submission_id, artifact_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._new_id("binding"),
                        reframe_id,
                        successor_task_id,
                        artifact["binding"],
                        artifact["source_submission_id"],
                        artifact["artifact_digest"],
                        now,
                    ),
                )
            if source["status"] == "active":
                self._terminate_problem(connection, source_problem_id, "cancelled", now)
            return self._reframe_view(connection, reframe_id)

    def _reframe_view(
        self, connection: sqlite3.Connection, reframe_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM problem_reframes WHERE id = ?", (reframe_id,)
        ).fetchone()
        if row is None:
            raise DomainError("not_found", "Reframe not found", status=404)
        bindings = connection.execute(
            """
            SELECT id, binding_name, source_submission_id,
                   artifact_digest, created_at
            FROM retained_artifact_bindings
            WHERE reframe_id = ? ORDER BY binding_name, id
            """,
            (reframe_id,),
        ).fetchall()
        return {
            "reframe_id": row["id"],
            "detection_mode": "operator_authorized",
            "idempotency_key": row["idempotency_key"],
            "source_problem_id": row["source_problem_id"],
            "source_root_task_id": row["source_root_task_id"],
            "source_submission_receipt_id": row["source_submission_id"],
            "source_root_contract_digest": row["source_root_contract_digest"],
            "diagnosis": self._loads(row["diagnosis_json"]),
            "successor_problem_id": row["successor_problem_id"],
            "successor_root_task_id": row["successor_root_task_id"],
            "successor_root_contract_digest": row[
                "successor_root_contract_digest"
            ],
            "retained_artifacts": [
                {
                    "binding_id": binding["id"],
                    "binding": binding["binding_name"],
                    "source_submission_receipt_id": binding[
                        "source_submission_id"
                    ],
                    "artifact_digest": binding["artifact_digest"],
                    "acceptance_scope": "source_contract_only",
                    "economic_effect": (
                        "source_payable_already_created_no_new_transfer"
                    ),
                    "created_at": binding["created_at"],
                }
                for binding in bindings
            ],
            "created_at": row["created_at"],
        }

    def _insert_task(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        problem_id: str,
        parent_id: str | None,
        depth: int,
        spec: dict[str, Any],
        now: float,
        delegated_by_node_id: str | None = None,
        allow_self_execution: bool = False,
    ) -> None:
        connection.execute(
            """
            INSERT INTO tasks (
                id, problem_id, parent_id, depth, required_capability,
                operation, inputs_json, constraints_json, accept_spec_json,
                reward_minor, delegation_budget_minor, max_attempts, attempt_count, state,
                delegated_by_node_id, allow_self_execution,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'open', ?, ?, ?, ?)
            """,
            (
                task_id,
                problem_id,
                parent_id,
                depth,
                spec["required_capability"],
                spec["operation"],
                canonical_json(spec["inputs"]),
                canonical_json(spec["constraints"]),
                canonical_json(spec["accept_spec"]),
                spec["reward_minor"],
                spec["delegation_budget_minor"],
                spec["max_attempts"],
                delegated_by_node_id,
                int(allow_self_execution),
                now,
                now,
            ),
        )

    def _require_task_manifest(
        self, connection: sqlite3.Connection, spec: dict[str, Any]
    ) -> dict[str, Any]:
        offering = connection.execute(
            """
            SELECT manifest_json FROM offerings
            WHERE manifest_digest = ? AND active = 1
            ORDER BY created_at, id LIMIT 1
            """,
            (spec["required_capability"],),
        ).fetchone()
        if offering is None:
            raise DomainError(
                "capability_not_admitted",
                "The task must target an admitted immutable manifest",
                status=409,
            )
        manifest = self._loads(offering["manifest_json"])
        require(
            spec["operation"] in manifest["operations"],
            "operation_mismatch",
            "The task operation is not declared by its target manifest",
        )
        errors = validate_instance(spec["inputs"], manifest["input_schema"])
        if errors:
            raise DomainError(
                "input_schema_violation",
                "Task inputs do not satisfy the target manifest",
                details={"violations": errors},
            )
        return manifest

    def lease_work(
        self, node_token: str | None, lease_request_id: str
    ) -> dict[str, Any] | None:
        request_id = require_string(lease_request_id, "lease_request_id")
        require(
            len(request_id) <= 200,
            "invalid_lease_request",
            "lease_request_id is too long",
        )
        with self.database.transaction() as connection:
            now = self.clock()
            node = self._authenticate_node(connection, node_token)
            self._expire_due_problems(connection, now)
            self._expire_leases(connection, now)
            prior = connection.execute(
                """
                SELECT * FROM lease_requests
                WHERE node_id = ? AND request_id = ?
                """,
                (node["id"], request_id),
            ).fetchone()
            if prior is not None:
                lease_token = self._derive_token(
                    node_token or "",
                    "task-lease-v1",
                    request_id,
                    prior["task_id"],
                )
                if not hmac.compare_digest(
                    self._token_hash(lease_token), prior["lease_token_hash"]
                ):
                    raise RuntimeError("Stored lease token digest is inconsistent")
                response = self._loads(prior["response_json"])
                response["lease_token"] = lease_token
                return response
            inflight = connection.execute(
                """
                SELECT COUNT(*) AS count FROM tasks
                WHERE state = 'leased' AND lease_node_id = ?
                """,
                (node["id"],),
            ).fetchone()["count"]
            if inflight >= self.config.max_inflight_per_node:
                return None

            candidates = connection.execute(
                """
                SELECT
                    t.*,
                    o.id AS matched_offering_id,
                    o.manifest_digest AS matched_manifest_digest,
                    o.concept_ref AS matched_concept_ref,
                    o.manifest_json AS matched_manifest_json,
                    p.intent AS root_intent,
                    p.problem_class AS matched_problem_class,
                    p.currency AS matched_currency,
                    p.platform_fee_bps AS matched_fee_bps,
                    p.deadline_at AS matched_deadline_at
                FROM tasks t
                JOIN problems p ON p.id = t.problem_id AND p.status = 'active'
                JOIN offerings o
                    ON o.node_id = ?
                    AND o.active = 1
                    AND o.manifest_digest = t.required_capability
                WHERE t.state = 'open'
                  AND t.attempt_count < t.max_attempts
                  AND p.deadline_at >= ?
                  AND (
                      t.delegated_by_node_id IS NULL
                      OR t.allow_self_execution = 1
                      OR t.delegated_by_node_id <> ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM submissions prior
                      WHERE prior.task_id = t.id
                        AND prior.node_id = ?
                        AND prior.gate_outcome = 'reject'
                  )
                ORDER BY t.created_at, t.id
                """,
                (
                    node["id"],
                    now + self.config.lease_seconds,
                    node["id"],
                    node["id"],
                ),
            ).fetchall()

            matched = None
            for candidate in candidates:
                manifest = self._loads(candidate["matched_manifest_json"])
                if candidate["operation"] in manifest["operations"]:
                    matched = candidate
                    break
            if matched is None:
                return None

            lease_token = self._derive_token(
                node_token or "",
                "task-lease-v1",
                request_id,
                matched["id"],
            )
            lease_expires_at = now + self.config.lease_seconds
            changed = connection.execute(
                """
                UPDATE tasks
                SET state = 'leased',
                    attempt_count = attempt_count + 1,
                    lease_node_id = ?,
                    lease_offering_id = ?,
                    lease_token_hash = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = 'open'
                """,
                (
                    node["id"],
                    matched["matched_offering_id"],
                    self._token_hash(lease_token),
                    lease_expires_at,
                    now,
                    matched["id"],
                ),
            ).rowcount
            if changed != 1:
                return None
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (matched["id"],)
            ).fetchone()
            response = {
                "lease_request_id": request_id,
                "lease_expires_at": lease_expires_at,
                "offering": {
                    "offering_id": matched["matched_offering_id"],
                    "manifest_digest": matched["matched_manifest_digest"],
                    "concept_ref": matched["matched_concept_ref"],
                },
                "problem": {
                    "problem_id": matched["problem_id"],
                    "root_intent": matched["root_intent"],
                    "problem_class": matched["matched_problem_class"],
                    "currency": matched["matched_currency"],
                    "deadline_at": matched["matched_deadline_at"],
                },
                "compensation_quote": {
                    "currency": matched["matched_currency"],
                    "gross_reward_minor": task["reward_minor"],
                    "platform_fee_bps": matched["matched_fee_bps"],
                    "provider_earning_minor": self._provider_earning(
                        task["reward_minor"], matched["matched_fee_bps"]
                    ),
                },
                "task": self._task_view(
                    connection,
                    task,
                    include_child_results=True,
                    for_provider=True,
                ),
            }
            connection.execute(
                """
                INSERT INTO lease_requests (
                    node_id, request_id, task_id, lease_token_hash,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    node["id"],
                    request_id,
                    matched["id"],
                    self._token_hash(lease_token),
                    canonical_json(response),
                    now,
                ),
            )
            response["lease_token"] = lease_token
            return response

    def delegate_children(
        self, node_token: str | None, task_id: str, body: Any
    ) -> dict[str, Any]:
        payload = require_object(body, "body")
        idempotency_key = require_string(
            payload.get("idempotency_key", payload.get("delegation_id")),
            "idempotency_key",
        )
        require(
            len(idempotency_key) <= 200,
            "invalid_delegation",
            "idempotency_key is too long",
        )
        lease_token = require_string(payload.get("lease_token"), "lease_token")
        raw_children = payload.get("children")
        require(
            isinstance(raw_children, list) and raw_children,
            "invalid_delegation",
            "children must be a non-empty list",
        )
        require(len(raw_children) <= 20, "invalid_delegation", "At most 20 children may be created at once")
        children = [validate_task_spec(item) for item in raw_children]
        delegation_digest = content_digest(
            {"parent_task_id": task_id, "children": children}
        )

        with self.database.transaction() as connection:
            now = self.clock()
            node = self._authenticate_node(connection, node_token)
            self._expire_due_problems(connection, now)
            self._expire_leases(connection, now)
            prior = connection.execute(
                """
                SELECT * FROM delegations
                WHERE parent_task_id = ? AND node_id = ? AND idempotency_key = ?
                """,
                (task_id, node["id"], idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["request_digest"] != delegation_digest:
                    raise DomainError(
                        "idempotency_conflict",
                        "The idempotency key was reused with different child tasks",
                        status=409,
                    )
                return self._loads(prior["response_json"])

            task = self._require_live_lease(connection, node["id"], task_id, lease_token, now)
            require(task["depth"] < self.config.max_depth, "maximum_depth", "The task reached the configured recursion limit")
            require(
                task["delegation_proposal_count"]
                < self.config.max_delegation_proposals,
                "delegation_proposal_limit",
                "The task reached the configured delegation proposal limit",
            )
            existing_children = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE parent_id = ?", (task_id,)
            ).fetchone()["count"]
            require(existing_children == 0, "already_delegated", "This task already has child tasks")

            parent_constraints = self._loads(task["constraints_json"])
            for child in children:
                validate_child_constraints(parent_constraints, child["constraints"])
                self._require_task_manifest(connection, child)

            requested_budget = sum(
                child["reward_minor"] + child["delegation_budget_minor"]
                for child in children
            )
            require(
                requested_budget <= task["delegation_budget_minor"],
                "delegation_budget_exceeded",
                "Child rewards and delegation budgets exceed the parent allowance",
                allowance=task["delegation_budget_minor"],
                requested=requested_budget,
            )

            connection.execute(
                """
                UPDATE tasks
                SET state = 'awaiting_delegation_approval', lease_node_id = NULL,
                    lease_offering_id = NULL, lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    attempt_count = CASE
                        WHEN attempt_count > 0 THEN attempt_count - 1
                        ELSE 0
                    END,
                    delegation_proposal_count = delegation_proposal_count + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, task_id),
            )
            delegation_id = self._new_id("delegation")
            response = {
                "delegation_id": delegation_id,
                "idempotency_key": idempotency_key,
                "parent_task_id": task_id,
                "status": "proposed",
                "parent_state": "awaiting_delegation_approval",
                "child_task_ids": [],
            }
            connection.execute(
                """
                INSERT INTO delegations (
                    id, idempotency_key, parent_task_id, node_id, request_digest,
                    request_json, status, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                """,
                (
                    delegation_id,
                    idempotency_key,
                    task_id,
                    node["id"],
                    delegation_digest,
                    canonical_json({"children": children}),
                    canonical_json(response),
                    now,
                ),
            )
            return response

    def reject_delegation(
        self, delegation_id: str, body: Any
    ) -> dict[str, Any]:
        payload = require_object(body, "body")
        reason = require_string(payload.get("reason"), "reason")
        with self.database.transaction() as connection:
            now = self.clock()
            self._expire_due_problems(connection, now)
            self._expire_leases(connection, now)
            delegation = connection.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            if delegation is None:
                raise DomainError("not_found", "Delegation not found", status=404)
            if delegation["status"] == "rejected":
                if delegation["decision_reason"] != reason:
                    raise DomainError(
                        "idempotency_conflict",
                        "The delegation was already rejected with another reason",
                        status=409,
                    )
                return self._loads(delegation["response_json"])
            if delegation["status"] in {"approved", "void"}:
                raise DomainError(
                    "delegation_decided",
                    "This delegation can no longer be rejected",
                    status=409,
                )
            parent = connection.execute(
                """
                SELECT t.*, p.status AS problem_status
                FROM tasks t
                JOIN problems p ON p.id = t.problem_id
                WHERE t.id = ?
                """,
                (delegation["parent_task_id"],),
            ).fetchone()
            if parent is None or parent["problem_status"] != "active":
                raise DomainError(
                    "problem_not_active",
                    "The delegation's problem is no longer active",
                    status=409,
                )
            if parent["state"] != "awaiting_delegation_approval":
                raise DomainError(
                    "delegation_state_conflict",
                    "The parent task is not awaiting delegation approval",
                    status=409,
                )
            connection.execute(
                "UPDATE tasks SET state = 'open', updated_at = ? WHERE id = ?",
                (now, parent["id"]),
            )
            response = {
                "delegation_id": delegation_id,
                "idempotency_key": delegation["idempotency_key"],
                "parent_task_id": parent["id"],
                "status": "rejected",
                "parent_state": "open",
                "reason": reason,
                "child_task_ids": [],
            }
            connection.execute(
                """
                UPDATE delegations
                SET status = 'rejected', response_json = ?,
                    decided_at = ?, decision_reason = ?
                WHERE id = ?
                """,
                (canonical_json(response), now, reason, delegation_id),
            )
            return response

    def approve_delegation(
        self, delegation_id: str, body: Any = _MISSING
    ) -> dict[str, Any]:
        payload = {} if body is _MISSING else require_object(body, "body")
        allow_self_execution = payload.get("allow_self_execution", False)
        require(
            isinstance(allow_self_execution, bool),
            "invalid_delegation_decision",
            "allow_self_execution must be boolean",
        )
        with self.database.transaction() as connection:
            now = self.clock()
            self._expire_due_problems(connection, now)
            self._expire_leases(connection, now)
            delegation = connection.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            if delegation is None:
                raise DomainError("not_found", "Delegation not found", status=404)
            if delegation["status"] == "approved":
                response = self._loads(delegation["response_json"])
                if response.get("allow_self_execution") != allow_self_execution:
                    raise DomainError(
                        "idempotency_conflict",
                        "The delegation was already approved with another self-execution policy",
                        status=409,
                    )
                return response
            if delegation["status"] != "proposed":
                raise DomainError(
                    "delegation_decided",
                    "The delegation is no longer awaiting approval",
                    status=409,
                )

            parent = connection.execute(
                """
                SELECT t.*, p.status AS problem_status
                FROM tasks t
                JOIN problems p ON p.id = t.problem_id
                WHERE t.id = ?
                """,
                (delegation["parent_task_id"],),
            ).fetchone()
            if parent is None:
                raise DomainError("not_found", "Parent task not found", status=404)
            if parent["problem_status"] != "active":
                raise DomainError(
                    "problem_not_active",
                    "The delegation's problem is no longer active",
                    status=409,
                )
            if parent["state"] != "awaiting_delegation_approval":
                raise DomainError(
                    "delegation_state_conflict",
                    "The parent task is not awaiting delegation approval",
                    status=409,
                )

            children = self._loads(delegation["request_json"])["children"]
            parent_constraints = self._loads(parent["constraints_json"])
            for child in children:
                validate_child_constraints(parent_constraints, child["constraints"])
            requested_budget = sum(
                child["reward_minor"] + child["delegation_budget_minor"]
                for child in children
            )
            require(
                requested_budget <= parent["delegation_budget_minor"],
                "delegation_budget_exceeded",
                "The approved children exceed the parent delegation allowance",
            )
            task_count = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE problem_id = ?",
                (parent["problem_id"],),
            ).fetchone()["count"]
            require(
                task_count + len(children) <= self.config.max_tasks_per_problem,
                "problem_task_limit",
                "The delegation would exceed the problem task limit",
            )

            child_ids: list[str] = []
            for child in children:
                child_id = self._new_id("task")
                child_ids.append(child_id)
                self._insert_task(
                    connection,
                    task_id=child_id,
                    problem_id=parent["problem_id"],
                    parent_id=parent["id"],
                    depth=parent["depth"] + 1,
                    spec=child,
                    now=now,
                    delegated_by_node_id=delegation["node_id"],
                    allow_self_execution=allow_self_execution,
                )
            connection.execute(
                """
                UPDATE tasks SET state = 'waiting_children', updated_at = ?
                WHERE id = ?
                """,
                (now, parent["id"]),
            )
            response = {
                "delegation_id": delegation_id,
                "idempotency_key": delegation["idempotency_key"],
                "parent_task_id": parent["id"],
                "status": "approved",
                "parent_state": "waiting_children",
                "allow_self_execution": allow_self_execution,
                "child_task_ids": child_ids,
            }
            connection.execute(
                """
                UPDATE delegations
                SET status = 'approved', response_json = ?, decided_at = ?
                WHERE id = ?
                """,
                (canonical_json(response), now, delegation_id),
            )
            return response

    def list_delegations(self, status: str | None = None) -> dict[str, Any]:
        if status is not None:
            require(
                status in {"proposed", "approved", "rejected", "void"},
                "invalid_status",
                "Unsupported delegation status",
            )
        with closing(self.database.connect()) as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM delegations ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM delegations WHERE status = ? ORDER BY created_at, id",
                    (status,),
                ).fetchall()
            return {
                "delegations": [
                    {
                        "delegation_id": row["id"],
                        "idempotency_key": row["idempotency_key"],
                        "parent_task_id": row["parent_task_id"],
                        "node_id": row["node_id"],
                        "status": row["status"],
                        "request": self._loads(row["request_json"]),
                        "response": self._loads(row["response_json"]),
                        "created_at": row["created_at"],
                        "decided_at": row["decided_at"],
                        "decision_reason": row["decision_reason"],
                    }
                    for row in rows
                ]
            }

    def submit_result(
        self, node_token: str | None, task_id: str, body: Any
    ) -> dict[str, Any]:
        result = validate_result(body)
        with self.database.transaction() as connection:
            now = self.clock()
            node = self._authenticate_node(connection, node_token)
            self._expire_due_problems(connection, now)
            self._expire_leases(connection, now)
            existing = connection.execute(
                """
                SELECT * FROM submissions
                WHERE task_id = ? AND node_id = ? AND client_submission_id = ?
                """,
                (task_id, node["id"], result["submission_id"]),
            ).fetchone()
            if existing is not None:
                incoming = {
                    key: value for key, value in result.items() if key != "lease_token"
                }
                if existing["result_json"] != canonical_json(incoming):
                    raise DomainError(
                        "idempotency_conflict",
                        "The submission id was reused with a different Result",
                        status=409,
                    )
                return self._submission_response(connection, existing)

            task = self._require_live_lease(
                connection, node["id"], task_id, result["lease_token"], now
            )
            gate = evaluate_result(
                self._loads(task["accept_spec_json"]),
                outputs=result["outputs"],
                status=result["status"],
                stop_reason=result["stop_reason"],
                output_schema_errors=validate_instance(
                    result["outputs"],
                    self._loads(
                        connection.execute(
                            "SELECT manifest_json FROM offerings WHERE id = ?",
                            (task["lease_offering_id"],),
                        ).fetchone()["manifest_json"]
                    )["output_schema"],
                ),
            )
            terminal_rejection = (
                gate["outcome"] == "reject"
                and task["attempt_count"] >= task["max_attempts"]
            )
            next_state = (
                "accepted"
                if gate["outcome"] == "pass"
                else ("failed" if terminal_rejection else "open")
            )
            quoted_earning = (
                self._provider_earning(
                    task["reward_minor"], task["problem_fee_bps"]
                )
                if gate["outcome"] == "pass"
                else 0
            )
            earning_status = "pending" if gate["outcome"] == "pass" else "none"
            stored_result = {key: value for key, value in result.items() if key != "lease_token"}
            submission_receipt_id = self._new_id("submission")
            connection.execute(
                """
                INSERT INTO submissions (
                    id, client_submission_id, task_id, node_id, offering_id, result_json,
                    gate_outcome, gate_json, task_state_after, attempt_count,
                    earning_minor, earning_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_receipt_id,
                    result["submission_id"],
                    task_id,
                    node["id"],
                    task["lease_offering_id"],
                    canonical_json(stored_result),
                    gate["outcome"],
                    canonical_json(gate),
                    next_state,
                    task["attempt_count"],
                    quoted_earning,
                    earning_status,
                    now,
                ),
            )
            problem = connection.execute(
                "SELECT * FROM problems WHERE id = ?", (task["problem_id"],)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO pathway_events (
                    id, problem_id, task_id, submission_id, node_id, offering_id,
                    problem_class, currency, capability, operation, depth, gate_outcome,
                    reward_minor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_id("path"),
                    problem["id"],
                    task_id,
                    submission_receipt_id,
                    node["id"],
                    task["lease_offering_id"],
                    problem["problem_class"],
                    problem["currency"],
                    task["required_capability"],
                    task["operation"],
                    task["depth"],
                    gate["outcome"],
                    task["reward_minor"],
                    now,
                ),
            )

            if gate["outcome"] == "pass":
                self._archive_accepted_submission(
                    connection, submission_receipt_id
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET state = 'accepted', lease_node_id = NULL,
                        lease_offering_id = NULL, lease_token_hash = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, task_id),
                )
                self._credit_accepted_task(
                    connection, problem, task, node["id"], now
                )
                connection.execute(
                    """
                    UPDATE submissions SET earning_status = 'available'
                    WHERE id = ?
                    """,
                    (submission_receipt_id,),
                )
                self._refresh_parent_after_acceptance(connection, task["parent_id"], now)
                self._refresh_problem_status(connection, problem["id"], now)
            else:
                connection.execute(
                    """
                    UPDATE tasks
                    SET state = ?, lease_node_id = NULL,
                        lease_offering_id = NULL, lease_token_hash = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_state, now, task_id),
                )
                if terminal_rejection:
                    self._block_problem(connection, problem["id"], now)

            stored = connection.execute(
                "SELECT * FROM submissions WHERE id = ?", (submission_receipt_id,)
            ).fetchone()
            return self._submission_response(connection, stored)

    def _require_live_lease(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        task_id: str,
        lease_token: str,
        now: float,
    ) -> sqlite3.Row:
        task = connection.execute(
            """
            SELECT t.*, p.status AS problem_status
                 , p.platform_fee_bps AS problem_fee_bps
            FROM tasks t
            JOIN problems p ON p.id = t.problem_id
            WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        if task is None:
            raise DomainError("not_found", "Task not found", status=404)
        if task["problem_status"] != "active":
            raise DomainError(
                "problem_not_active",
                "The task's problem is no longer active",
                status=409,
            )
        if task["state"] != "leased" or task["lease_node_id"] != node_id:
            raise DomainError("lease_conflict", "The node does not hold this task lease", status=409)
        if task["lease_expires_at"] is None or task["lease_expires_at"] <= now:
            raise DomainError("lease_expired", "The task lease has expired", status=409)
        if not hmac.compare_digest(task["lease_token_hash"], self._token_hash(lease_token)):
            raise DomainError("lease_conflict", "The lease token is invalid", status=409)
        return task

    @staticmethod
    def _provider_earning(reward_minor: int, fee_bps: int) -> int:
        fee = reward_minor * fee_bps // 10_000
        return reward_minor - fee

    def _credit_accepted_task(
        self,
        connection: sqlite3.Connection,
        problem: sqlite3.Row,
        task: sqlite3.Row,
        node_id: str,
        now: float,
    ) -> int:
        fee = task["reward_minor"] * problem["platform_fee_bps"] // 10_000
        provider_amount = task["reward_minor"] - fee
        escrow = f"escrow:{problem['id']}"
        self._insert_transfer(
            connection,
            currency=problem["currency"],
            amount=provider_amount,
            from_account=escrow,
            to_account=f"payable:{node_id}",
            reason="accepted_solver_work",
            idempotency_key=f"task:{task['id']}:provider",
            problem_id=problem["id"],
            task_id=task["id"],
            node_id=node_id,
            now=now,
        )
        if fee:
            self._insert_transfer(
                connection,
                currency=problem["currency"],
                amount=fee,
                from_account=escrow,
                to_account="revenue:platform",
                reason="platform_fee",
                idempotency_key=f"task:{task['id']}:fee",
                problem_id=problem["id"],
                task_id=task["id"],
                node_id=node_id,
                now=now,
            )
        return provider_amount

    def _insert_transfer(
        self,
        connection: sqlite3.Connection,
        *,
        currency: str,
        amount: int,
        from_account: str,
        to_account: str,
        reason: str,
        idempotency_key: str,
        problem_id: str | None,
        task_id: str | None,
        node_id: str | None,
        now: float,
    ) -> None:
        require(amount > 0, "invalid_transfer", "A ledger transfer must be positive")
        connection.execute(
            """
            INSERT INTO ledger_transfers (
                id, currency, amount_minor, from_account, to_account,
                reason, idempotency_key, problem_id, task_id, node_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._new_id("transfer"),
                currency,
                amount,
                from_account,
                to_account,
                reason,
                idempotency_key,
                problem_id,
                task_id,
                node_id,
                now,
            ),
        )

    def _expire_leases(self, connection: sqlite3.Connection, now: float) -> int:
        expired_count = 0
        expired = connection.execute(
            """
            SELECT *
            FROM tasks
            WHERE state = 'leased' AND lease_expires_at <= ?
            """,
            (now,),
        ).fetchall()
        for task in expired:
            current = connection.execute(
                "SELECT state FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()
            if current is None or current["state"] != "leased":
                continue
            expired_count += 1
            terminal = task["attempt_count"] >= task["max_attempts"]
            next_state = "failed" if terminal else "open"
            submission_id = self._new_id("timeout")
            submission_receipt_id = self._new_id("submission")
            result = {
                "submission_id": submission_id,
                "status": "fail",
                "stop_reason": "budget",
                "outputs": {},
                "evidence": {"kind": "lease_expired"},
                "usage": {},
            }
            clause = {
                "clause_id": "protocol:lease-completed-before-expiry",
                "path": "",
                "operator": "protocol",
                "critical": True,
                "disclosure": "public",
                "passed": False,
                "expected": "Result submitted before lease expiry",
                "observed": "lease expired without Result",
                "observed_missing": False,
            }
            gate = {
                "outcome": "reject",
                "seam": "hard",
                "pass_rate": 0.0,
                "clauses": [clause],
                "failure_trace": {
                    "kind": "lease_timeout",
                    "violations": [clause],
                    "evaluator": "coordinator:lease-v1",
                },
                "evaluator": "coordinator:lease-v1",
            }
            connection.execute(
                """
                INSERT INTO submissions (
                    id, client_submission_id, task_id, node_id, offering_id, result_json,
                    gate_outcome, gate_json, task_state_after, attempt_count,
                    earning_minor, earning_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reject', ?, ?, ?, 0, 'none', ?)
                """,
                (
                    submission_receipt_id,
                    submission_id,
                    task["id"],
                    task["lease_node_id"],
                    task["lease_offering_id"],
                    canonical_json(result),
                    canonical_json(gate),
                    next_state,
                    task["attempt_count"],
                    now,
                ),
            )
            problem = connection.execute(
                "SELECT * FROM problems WHERE id = ?", (task["problem_id"],)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO pathway_events (
                    id, problem_id, task_id, submission_id, node_id, offering_id,
                    problem_class, currency, capability, operation, depth,
                    gate_outcome, reward_minor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reject', ?, ?)
                """,
                (
                    self._new_id("path"),
                    task["problem_id"],
                    task["id"],
                    submission_receipt_id,
                    task["lease_node_id"],
                    task["lease_offering_id"],
                    problem["problem_class"],
                    problem["currency"],
                    task["required_capability"],
                    task["operation"],
                    task["depth"],
                    task["reward_minor"],
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE tasks
                SET state = ?, lease_node_id = NULL, lease_offering_id = NULL,
                    lease_token_hash = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (next_state, now, task["id"]),
            )
            if terminal:
                self._block_problem(connection, task["problem_id"], now)
        return expired_count

    def _refresh_parent_after_acceptance(
        self, connection: sqlite3.Connection, parent_id: str | None, now: float
    ) -> None:
        if parent_id is None:
            return
        children = connection.execute(
            "SELECT state FROM tasks WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        if children and all(child["state"] == "accepted" for child in children):
            connection.execute(
                """
                UPDATE tasks SET state = 'open', updated_at = ?
                WHERE id = ? AND state = 'waiting_children'
                """,
                (now, parent_id),
            )

    def _block_problem(
        self, connection: sqlite3.Connection, problem_id: str, now: float
    ) -> None:
        self._terminate_problem(connection, problem_id, "blocked", now)

    def _terminate_problem(
        self,
        connection: sqlite3.Connection,
        problem_id: str,
        status: str,
        now: float,
    ) -> None:
        connection.execute(
            "UPDATE problems SET status = ?, updated_at = ? WHERE id = ? AND status = 'active'",
            (status, now, problem_id),
        )
        connection.execute(
            """
            UPDATE tasks
            SET state = 'blocked', lease_node_id = NULL,
                lease_offering_id = NULL, lease_token_hash = NULL,
                lease_expires_at = NULL, updated_at = ?
            WHERE problem_id = ?
              AND state NOT IN ('accepted', 'failed')
            """,
            (now, problem_id),
        )
        connection.execute(
            """
            UPDATE submissions
            SET earning_status = 'void'
            WHERE earning_status = 'pending'
              AND task_id IN (SELECT id FROM tasks WHERE problem_id = ?)
            """,
            (problem_id,),
        )
        proposals = connection.execute(
            """
            SELECT d.* FROM delegations d
            JOIN tasks t ON t.id = d.parent_task_id
            WHERE t.problem_id = ? AND d.status = 'proposed'
            """,
            (problem_id,),
        ).fetchall()
        for delegation in proposals:
            reason = f"problem_{status}"
            response = {
                "delegation_id": delegation["id"],
                "idempotency_key": delegation["idempotency_key"],
                "parent_task_id": delegation["parent_task_id"],
                "status": "void",
                "parent_state": "blocked",
                "reason": reason,
                "child_task_ids": [],
            }
            connection.execute(
                """
                UPDATE delegations
                SET status = 'void', response_json = ?, decided_at = ?,
                    decision_reason = ?
                WHERE id = ?
                """,
                (canonical_json(response), now, reason, delegation["id"]),
            )
        self._settle_unused_funding(connection, problem_id, now)

    def _expire_due_problems(
        self, connection: sqlite3.Connection, now: float
    ) -> int:
        due = connection.execute(
            """
            SELECT id FROM problems
            WHERE status = 'active' AND deadline_at IS NOT NULL AND deadline_at <= ?
            """,
            (now,),
        ).fetchall()
        for problem in due:
            self._terminate_problem(connection, problem["id"], "expired", now)
        return len(due)

    def expire_due_problems(self) -> int:
        with self.database.transaction() as connection:
            now = self.clock()
            return self._expire_due_problems(connection, now)

    def reap_expired(self) -> dict[str, int]:
        """Expire deadlines and leases; safe for a scheduler to call repeatedly."""
        with self.database.transaction() as connection:
            now = self.clock()
            problems = self._expire_due_problems(connection, now)
            leases = self._expire_leases(connection, now)
            return {
                "problems": problems,
                "leases": leases,
            }

    def cancel_problem(self, problem_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            now = self.clock()
            problem = connection.execute(
                "SELECT status FROM problems WHERE id = ?", (problem_id,)
            ).fetchone()
            if problem is None:
                raise DomainError("not_found", "Problem not found", status=404)
            if problem["status"] == "completed":
                raise DomainError(
                    "problem_completed",
                    "A completed problem cannot be cancelled",
                    status=409,
                )
            if problem["status"] == "active":
                self._terminate_problem(connection, problem_id, "cancelled", now)
            return self._problem_view(connection, problem_id)

    def _refresh_problem_status(
        self, connection: sqlite3.Connection, problem_id: str, now: float
    ) -> None:
        counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN state = 'accepted' THEN 1 ELSE 0 END) AS accepted,
                COUNT(*) AS total
            FROM tasks WHERE problem_id = ?
            """,
            (problem_id,),
        ).fetchone()
        if counts["total"] and counts["accepted"] == counts["total"]:
            changed = connection.execute(
                """
                UPDATE problems SET status = 'completed', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, problem_id),
            ).rowcount
            if changed != 1:
                return
            self._settle_unused_funding(connection, problem_id, now)

    def _settle_unused_funding(
        self, connection: sqlite3.Connection, problem_id: str, now: float
    ) -> None:
        problem = connection.execute(
            "SELECT currency FROM problems WHERE id = ?", (problem_id,)
        ).fetchone()
        escrow = f"escrow:{problem_id}"
        balance = connection.execute(
            """
            SELECT COALESCE(SUM(
                CASE
                    WHEN to_account = ? THEN amount_minor
                    WHEN from_account = ? THEN -amount_minor
                    ELSE 0
                END
            ), 0) AS balance
            FROM ledger_transfers
            WHERE currency = ? AND (from_account = ? OR to_account = ?)
            """,
            (escrow, escrow, problem["currency"], escrow, escrow),
        ).fetchone()["balance"]
        if balance > 0:
            self._insert_transfer(
                connection,
                currency=problem["currency"],
                amount=balance,
                from_account=escrow,
                to_account=f"refund_pending:{problem_id}",
                reason="unused_funding_refund_pending",
                idempotency_key=f"problem:{problem_id}:unused-refund",
                problem_id=problem_id,
                task_id=None,
                node_id=None,
                now=now,
            )

    def get_problem(
        self,
        problem_id: str,
        *,
        submission_limit: int = 50,
        submission_offset: int = 0,
    ) -> dict[str, Any]:
        valid_limit = isinstance(submission_limit, int) and not isinstance(
            submission_limit, bool
        )
        valid_offset = isinstance(submission_offset, int) and not isinstance(
            submission_offset, bool
        )
        require(
            valid_limit and 1 <= submission_limit <= 200,
            "invalid_pagination",
            "submission_limit must be between 1 and 200",
        )
        require(
            valid_offset and 0 <= submission_offset <= 1_000_000,
            "invalid_pagination",
            "submission_offset must be between 0 and 1000000",
        )
        with self.database.transaction() as connection:
            now = self.clock()
            self._expire_due_problems(connection, now)
            self._expire_leases(connection, now)
            return self._problem_view(
                connection,
                problem_id,
                submission_limit=submission_limit,
                submission_offset=submission_offset,
            )

    def _problem_view(
        self,
        connection: sqlite3.Connection,
        problem_id: str,
        *,
        submission_limit: int = 50,
        submission_offset: int = 0,
    ) -> dict[str, Any]:
        problem = connection.execute(
            "SELECT * FROM problems WHERE id = ?", (problem_id,)
        ).fetchone()
        if problem is None:
            raise DomainError("not_found", "Problem not found", status=404)
        tasks = connection.execute(
            "SELECT * FROM tasks WHERE problem_id = ? ORDER BY depth, created_at",
            (problem_id,),
        ).fetchall()
        submissions = connection.execute(
            """
            SELECT s.*, t.parent_id, a.digest AS artifact_digest
            FROM submissions s
            JOIN tasks t ON t.id = s.task_id
            LEFT JOIN accepted_artifacts a ON a.source_submission_id = s.id
            WHERE t.problem_id = ?
            ORDER BY s.created_at, s.id
            LIMIT ? OFFSET ?
            """,
            (problem_id, submission_limit, submission_offset),
        ).fetchall()
        submission_total = connection.execute(
            """
            SELECT COUNT(*) AS count FROM submissions s
            JOIN tasks t ON t.id = s.task_id
            WHERE t.problem_id = ?
            """,
            (problem_id,),
        ).fetchone()["count"]
        transfers = connection.execute(
            """
            SELECT currency, amount_minor, from_account, to_account, reason,
                   task_id, node_id, created_at
            FROM ledger_transfers
            WHERE problem_id = ? ORDER BY rowid
            """,
            (problem_id,),
        ).fetchall()
        escrow_account = f"escrow:{problem_id}"
        escrow_balance = sum(
            row["amount_minor"]
            * ((1 if row["to_account"] == escrow_account else 0) - (1 if row["from_account"] == escrow_account else 0))
            for row in transfers
        )
        refund_account = f"refund_pending:{problem_id}"
        refund_pending = sum(
            row["amount_minor"]
            * (
                (1 if row["to_account"] == refund_account else 0)
                - (1 if row["from_account"] == refund_account else 0)
            )
            for row in transfers
        )
        accepted_root = connection.execute(
            """
            SELECT s.* FROM submissions s
            JOIN tasks t ON t.id = s.task_id
            WHERE t.problem_id = ? AND t.parent_id IS NULL
              AND s.gate_outcome = 'pass'
            ORDER BY s.created_at, s.id LIMIT 1
            """,
            (problem_id,),
        ).fetchone()
        predecessor = connection.execute(
            """
            SELECT * FROM problem_reframes
            WHERE successor_problem_id = ?
            """,
            (problem_id,),
        ).fetchone()
        successor = connection.execute(
            """
            SELECT * FROM problem_reframes
            WHERE source_problem_id = ?
            ORDER BY created_at, id LIMIT 1
            """,
            (problem_id,),
        ).fetchone()
        view = {
            "problem_id": problem["id"],
            "intent": problem["intent"],
            "problem_class": problem["problem_class"],
            "funded_amount_minor": problem["funded_amount_minor"],
            "currency": problem["currency"],
            "platform_fee_bps": problem["platform_fee_bps"],
            "funding_reference": problem["funding_reference"],
            "deadline_at": problem["deadline_at"],
            "status": problem["status"],
            "escrow_balance_minor": escrow_balance,
            "refund_pending_minor": refund_pending,
            "tasks": [self._task_view(connection, row) for row in tasks],
            "submissions": [
                {
                    "submission_id": row["client_submission_id"],
                    "submission_receipt_id": row["id"],
                    "task_id": row["task_id"],
                    "node_id": row["node_id"],
                    "result": self._loads(row["result_json"]),
                    "gate": self._loads(row["gate_json"]),
                    "task_state_after": row["task_state_after"],
                    "earning_minor": row["earning_minor"],
                    "earning_status": row["earning_status"],
                    "accepted_artifact_digest": row["artifact_digest"],
                    "created_at": row["created_at"],
                }
                for row in submissions
            ],
            "submission_page": {
                "limit": submission_limit,
                "offset": submission_offset,
                "returned": len(submissions),
                "total": submission_total,
            },
            "accepted_result": (
                {
                    "submission_id": accepted_root["client_submission_id"],
                    "submission_receipt_id": accepted_root["id"],
                    "node_id": accepted_root["node_id"],
                    "result": self._loads(accepted_root["result_json"]),
                    "gate": self._loads(accepted_root["gate_json"]),
                }
                if accepted_root is not None
                else None
            ),
            "ledger_transfers": [dict(row) for row in transfers],
            "created_at": problem["created_at"],
            "updated_at": problem["updated_at"],
        }
        if predecessor is not None or successor is not None:
            view["reframe_lineage"] = {
                "predecessor": (
                    self._reframe_summary(predecessor) if predecessor else None
                ),
                "successor": self._reframe_summary(successor) if successor else None,
            }
        return view

    @staticmethod
    def _reframe_summary(reframe: sqlite3.Row) -> dict[str, Any]:
        return {
            "reframe_id": reframe["id"],
            "detection_mode": "operator_authorized",
            "source_problem_id": reframe["source_problem_id"],
            "source_root_task_id": reframe["source_root_task_id"],
            "source_submission_receipt_id": reframe["source_submission_id"],
            "source_root_contract_digest": reframe[
                "source_root_contract_digest"
            ],
            "successor_problem_id": reframe["successor_problem_id"],
            "successor_root_task_id": reframe["successor_root_task_id"],
            "successor_root_contract_digest": reframe[
                "successor_root_contract_digest"
            ],
            "created_at": reframe["created_at"],
        }

    def _task_view(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        include_child_results: bool = False,
        for_provider: bool = False,
    ) -> dict[str, Any]:
        accept_spec = self._loads(task["accept_spec_json"])
        if for_provider:
            for clause in accept_spec["clauses"]:
                if clause.get("disclosure") == "hidden":
                    clause.pop("expected", None)
        view: dict[str, Any] = {
            "task_id": task["id"],
            "problem_id": task["problem_id"],
            "parent_id": task["parent_id"],
            "depth": task["depth"],
            "required_capability": task["required_capability"],
            "operation": task["operation"],
            "inputs": self._loads(task["inputs_json"]),
            "constraints": self._loads(task["constraints_json"]),
            "accept_spec": accept_spec,
            "reward_minor": task["reward_minor"],
            "delegation_budget_minor": task["delegation_budget_minor"],
            "max_attempts": task["max_attempts"],
            "attempt_count": task["attempt_count"],
            "delegation_proposal_count": task["delegation_proposal_count"],
            "delegated_by_node_id": task["delegated_by_node_id"],
            "allow_self_execution": bool(task["allow_self_execution"]),
            "state": task["state"],
        }
        retained_rows = connection.execute(
            """
            SELECT b.binding_name, b.source_submission_id,
                   b.artifact_digest, a.envelope_json
            FROM retained_artifact_bindings b
            JOIN accepted_artifacts a ON a.digest = b.artifact_digest
            WHERE b.successor_task_id = ?
            ORDER BY b.binding_name, b.id
            """,
            (task["id"],),
        ).fetchall()
        if retained_rows:
            view["retained_artifact_bindings"] = [
                {
                    "binding": row["binding_name"],
                    "source_submission_receipt_id": row[
                        "source_submission_id"
                    ],
                    "artifact_digest": row["artifact_digest"],
                }
                for row in retained_rows
            ]
            if include_child_results:
                view["retained_artifacts"] = [
                    {
                        "binding": row["binding_name"],
                        "artifact_digest": row["artifact_digest"],
                        "artifact": self._load_artifact_envelope(
                            row["envelope_json"], row["artifact_digest"]
                        ),
                        "acceptance_scope": "source_contract_only",
                        "economic_effect": (
                            "source_payable_already_created_no_new_transfer"
                        ),
                    }
                    for row in retained_rows
                ]
        if include_child_results:
            rows = connection.execute(
                """
                SELECT t.id AS task_id, t.required_capability, t.operation,
                       t.inputs_json, t.constraints_json, s.result_json
                FROM tasks t
                JOIN submissions s ON s.task_id = t.id AND s.gate_outcome = 'pass'
                WHERE t.parent_id = ? AND t.state = 'accepted'
                ORDER BY t.created_at
                """,
                (task["id"],),
            ).fetchall()
            view["accepted_child_results"] = [
                {
                    "task": {
                        "task_id": row["task_id"],
                        "required_capability": row["required_capability"],
                        "operation": row["operation"],
                        "inputs": self._loads(row["inputs_json"]),
                        "constraints": self._loads(row["constraints_json"]),
                    },
                    "result": self._loads(row["result_json"]),
                }
                for row in rows
            ]
        return view

    def _submission_response(
        self, connection: sqlite3.Connection, submission: sqlite3.Row
    ) -> dict[str, Any]:
        task = connection.execute(
            "SELECT max_attempts FROM tasks WHERE id = ?",
            (submission["task_id"],),
        ).fetchone()
        gate = self._loads(submission["gate_json"])
        hidden_clauses = [
            clause
            for clause in gate["clauses"]
            if clause.get("disclosure") == "hidden"
        ]
        gate["clauses"] = [
            clause
            for clause in gate["clauses"]
            if clause.get("disclosure") != "hidden"
        ]
        gate["hidden_clause_count"] = len(hidden_clauses)
        if hidden_clauses:
            gate.pop("pass_rate", None)
        failure_trace = gate.get("failure_trace")
        if failure_trace:
            violations = failure_trace.get("violations", [])
            hidden_violation = any(
                item.get("disclosure") == "hidden" for item in violations
            )
            failure_trace["violations"] = [
                item
                for item in violations
                if item.get("disclosure") != "hidden"
            ]
            if hidden_violation:
                failure_trace["hidden_details_withheld"] = True
        artifact = connection.execute(
            """
            SELECT digest FROM accepted_artifacts
            WHERE source_submission_id = ?
            """,
            (submission["id"],),
        ).fetchone()
        return {
            "submission_id": submission["client_submission_id"],
            "submission_receipt_id": submission["id"],
            "task_id": submission["task_id"],
            "gate": gate,
            "task_state": submission["task_state_after"],
            "attempt_count": submission["attempt_count"],
            "max_attempts": task["max_attempts"],
            "earning_minor": submission["earning_minor"],
            "earning_status": submission["earning_status"],
            "accepted_artifact_digest": artifact["digest"] if artifact else None,
        }

    def get_node_earnings(self, node_token: str | None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            node = self._authenticate_node(connection, node_token)
            transfers = connection.execute(
                """
                SELECT currency, amount_minor, from_account, to_account,
                       reason, task_id, created_at
                FROM ledger_transfers
                WHERE node_id = ? ORDER BY created_at, id
                """,
                (node["id"],),
            ).fetchall()
            balances: dict[str, int] = {}
            account = f"payable:{node['id']}"
            for row in transfers:
                delta = (1 if row["to_account"] == account else 0) - (
                    1 if row["from_account"] == account else 0
                )
                balances[row["currency"]] = balances.get(row["currency"], 0) + delta * row["amount_minor"]
            pending_rows = connection.execute(
                """
                SELECT p.currency, COALESCE(SUM(s.earning_minor), 0) AS amount
                FROM submissions s
                JOIN tasks t ON t.id = s.task_id
                JOIN problems p ON p.id = t.problem_id
                WHERE s.node_id = ? AND s.earning_status = 'pending'
                GROUP BY p.currency
                """,
                (node["id"],),
            ).fetchall()
            return {
                "node_id": node["id"],
                "balances": balances,
                "pending_balances": {
                    row["currency"]: row["amount"] for row in pending_rows
                },
                "classification": "supplier_payable_not_wallet",
                "transfers": [dict(row) for row in transfers],
            }

    def pathway_summary(self, capability: str | None = None) -> dict[str, Any]:
        with closing(self.database.connect()) as connection:
            where = "WHERE capability = ?" if capability else ""
            parameters = (capability,) if capability else ()
            rows = connection.execute(
                f"""
                SELECT capability, problem_class, currency, node_id, offering_id,
                       COUNT(*) AS invocation_count,
                       SUM(CASE WHEN gate_outcome = 'pass' THEN 1 ELSE 0 END) AS pass_count,
                       AVG(reward_minor) AS average_reward_minor
                FROM pathway_events
                {where}
                GROUP BY capability, problem_class, currency, node_id, offering_id
                ORDER BY invocation_count DESC, capability
                """,
                parameters,
            ).fetchall()
            aggregates = []
            for row in rows:
                item = dict(row)
                item["gate_clearance_rate"] = item["pass_count"] / item["invocation_count"]
                aggregates.append(item)
            return {"aggregates": aggregates}
