from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 2


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    operator_name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended')),
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS node_invites (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    expires_at REAL NOT NULL,
    max_uses INTEGER NOT NULL CHECK (max_uses > 0),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS node_registrations (
    invite_id TEXT PRIMARY KEY REFERENCES node_invites(id),
    registration_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    node_id TEXT NOT NULL UNIQUE REFERENCES nodes(id),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS offerings (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    manifest_digest TEXT NOT NULL,
    concept_ref TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    created_at REAL NOT NULL,
    approved_at REAL,
    UNIQUE (node_id, manifest_digest)
);

CREATE TABLE IF NOT EXISTS problems (
    id TEXT PRIMARY KEY,
    intent TEXT NOT NULL,
    problem_class TEXT NOT NULL,
    funded_amount_minor INTEGER NOT NULL CHECK (funded_amount_minor > 0),
    currency TEXT NOT NULL,
    platform_fee_bps INTEGER NOT NULL CHECK (
        platform_fee_bps >= 0 AND platform_fee_bps < 10000
    ),
    funding_reference TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    deadline_at REAL,
    status TEXT NOT NULL CHECK (
        status IN ('active', 'completed', 'blocked', 'cancelled', 'expired')
    ),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL REFERENCES problems(id),
    parent_id TEXT REFERENCES tasks(id),
    depth INTEGER NOT NULL CHECK (depth >= 0),
    required_capability TEXT NOT NULL,
    operation TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    accept_spec_json TEXT NOT NULL,
    reward_minor INTEGER NOT NULL CHECK (reward_minor > 0),
    delegation_budget_minor INTEGER NOT NULL DEFAULT 0 CHECK (delegation_budget_minor >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    delegation_proposal_count INTEGER NOT NULL DEFAULT 0 CHECK (
        delegation_proposal_count >= 0
    ),
    delegated_by_node_id TEXT REFERENCES nodes(id),
    allow_self_execution INTEGER NOT NULL DEFAULT 0 CHECK (
        allow_self_execution IN (0, 1)
    ),
    state TEXT NOT NULL CHECK (
        state IN (
            'open', 'leased', 'awaiting_delegation_approval',
            'waiting_children', 'accepted', 'failed', 'blocked'
        )
    ),
    lease_node_id TEXT REFERENCES nodes(id),
    lease_offering_id TEXT REFERENCES offerings(id),
    lease_token_hash TEXT,
    lease_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lease_requests (
    node_id TEXT NOT NULL REFERENCES nodes(id),
    request_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    lease_token_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (node_id, request_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    client_submission_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    node_id TEXT NOT NULL REFERENCES nodes(id),
    offering_id TEXT NOT NULL REFERENCES offerings(id),
    result_json TEXT NOT NULL,
    gate_outcome TEXT CHECK (gate_outcome IN ('pass', 'reject')),
    gate_json TEXT,
    task_state_after TEXT NOT NULL CHECK (
        task_state_after IN ('open', 'accepted', 'failed')
    ),
    attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
    earning_minor INTEGER NOT NULL DEFAULT 0 CHECK (earning_minor >= 0),
    earning_status TEXT NOT NULL CHECK (
        earning_status IN ('none', 'pending', 'available', 'void')
    ),
    created_at REAL NOT NULL,
    UNIQUE (task_id, node_id, client_submission_id)
);

CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    parent_task_id TEXT NOT NULL REFERENCES tasks(id),
    node_id TEXT NOT NULL REFERENCES nodes(id),
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'void')),
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    decided_at REAL,
    decision_reason TEXT,
    UNIQUE (parent_task_id, node_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ledger_transfers (
    id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    from_account TEXT NOT NULL,
    to_account TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    problem_id TEXT REFERENCES problems(id),
    task_id TEXT REFERENCES tasks(id),
    node_id TEXT REFERENCES nodes(id),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pathway_events (
    id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL REFERENCES problems(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    submission_id TEXT NOT NULL REFERENCES submissions(id),
    node_id TEXT NOT NULL REFERENCES nodes(id),
    offering_id TEXT NOT NULL REFERENCES offerings(id),
    problem_class TEXT NOT NULL,
    currency TEXT NOT NULL,
    capability TEXT NOT NULL,
    operation TEXT NOT NULL,
    depth INTEGER NOT NULL,
    gate_outcome TEXT NOT NULL CHECK (gate_outcome IN ('pass', 'reject')),
    reward_minor INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS tasks_routing_idx
    ON tasks(state, required_capability, created_at);
CREATE INDEX IF NOT EXISTS offerings_routing_idx
    ON offerings(node_id, active, manifest_digest, concept_ref);
CREATE INDEX IF NOT EXISTS pathway_lookup_idx
    ON pathway_events(capability, problem_class, gate_outcome);
CREATE INDEX IF NOT EXISTS ledger_node_idx
    ON ledger_transfers(node_id, currency, created_at);
"""


REFRAME_SCHEMA = """
CREATE TABLE IF NOT EXISTS accepted_artifacts (
    digest TEXT PRIMARY KEY,
    source_submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
    source_contract_digest TEXT NOT NULL,
    gate_digest TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS problem_reframes (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    source_problem_id TEXT NOT NULL REFERENCES problems(id),
    source_root_task_id TEXT NOT NULL REFERENCES tasks(id),
    source_submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
    source_root_contract_digest TEXT NOT NULL,
    diagnosis_json TEXT NOT NULL,
    successor_problem_id TEXT NOT NULL UNIQUE REFERENCES problems(id),
    successor_root_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    successor_root_contract_digest TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (source_problem_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS retained_artifact_bindings (
    id TEXT PRIMARY KEY,
    reframe_id TEXT NOT NULL REFERENCES problem_reframes(id),
    successor_task_id TEXT NOT NULL REFERENCES tasks(id),
    binding_name TEXT NOT NULL,
    source_submission_id TEXT NOT NULL REFERENCES submissions(id),
    artifact_digest TEXT NOT NULL REFERENCES accepted_artifacts(digest),
    created_at REAL NOT NULL,
    UNIQUE (reframe_id, source_submission_id),
    UNIQUE (successor_task_id, source_submission_id),
    UNIQUE (successor_task_id, binding_name)
);

CREATE INDEX IF NOT EXISTS problem_reframes_source_idx
    ON problem_reframes(source_problem_id, created_at);
CREATE INDEX IF NOT EXISTS accepted_artifacts_source_idx
    ON accepted_artifacts(source_submission_id);
CREATE INDEX IF NOT EXISTS retained_artifacts_successor_idx
    ON retained_artifact_bindings(successor_task_id, created_at);
"""


SCHEMA = BASE_SCHEMA + REFRAME_SCHEMA


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError(
                "Database(':memory:') is unsupported because the coordinator "
                "uses short-lived connections; use a temporary file instead."
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
            if version == 0 and tables:
                raise RuntimeError(
                    "The coordinator database is unversioned and cannot be upgraded "
                    "safely. Export it with its original build or start with a new file."
                )
            if version not in {0, 1, SCHEMA_VERSION}:
                raise RuntimeError(
                    f"Unsupported coordinator database schema version {version}; "
                    f"this build requires version {SCHEMA_VERSION}."
                )
            if version == 1:
                connection.executescript(REFRAME_SCHEMA)
            else:
                connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
