from __future__ import annotations

import json
import os
import time

from fractal_protocol.client import CoordinatorClient
from fractal_protocol.worker import ConnectedNode, SolverHandler, SQLiteResultJournal


BASE_URL = os.environ.get("FI_COORDINATOR_URL", "http://127.0.0.1:8787")
ADMIN_TOKEN = os.environ.get("FI_ADMIN_TOKEN", "local-admin")
NODE_JOURNAL = os.environ.get("FI_NODE_JOURNAL", "demo-node-journal.db")


def manifest(concept_ref: str, name: str, operation: str) -> dict:
    return {
        "concept_ref": concept_ref,
        "name": name,
        "description": f"Deterministic demonstration Solver for {operation}",
        "cognitive_mode": "convergent",
        "operations": [operation],
        "surfaces": ["manifest", "execute"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }


admin = CoordinatorClient(BASE_URL, admin_token=ADMIN_TOKEN)
invite = admin.create_node_invite("local demo provider")
bootstrap = CoordinatorClient(BASE_URL, node_invite_token=invite["invite_token"])
registration = bootstrap.register_node(
    "local-demo-provider",
    {"demo": True},
    registration_id="local-demo-provider-v1",
)
node_client = CoordinatorClient(BASE_URL, node_token=registration["node_token"])
node = ConnectedNode(
    node_client,
    result_journal=SQLiteResultJournal(NODE_JOURNAL),
)

word_count = node.publish(
    SolverHandler(
        manifest=manifest("urn:fractal:demo:word-count", "Word Count", "count_words"),
        execute=lambda task: {"count": len(task["inputs"]["text"].split())},
    )
)
character_count = node.publish(
    SolverHandler(
        manifest=manifest("urn:fractal:demo:character-count", "Character Count", "count_characters"),
        execute=lambda task: {"count": len(task["inputs"]["text"])},
    )
)
admin.approve_offering(word_count["offering_id"])
admin.approve_offering(character_count["offering_id"])

problem = admin.create_problem(
    {
        "intent": "Count the words in the supplied text",
        "problem_class": "objective.text.counting",
        "funded_amount_minor": 1000,
        "currency": "USD",
        "funding_reference": f"demo-{registration['node_id']}",
        "deadline_at": time.time() + 3600,
        "task": {
            "required_capability": word_count["manifest_digest"],
            "operation": "count_words",
            "inputs": {"text": "fractal agents solve typed problems"},
            "constraints": {"workflow_scope": "task_only"},
            "reward_minor": 500,
            "max_attempts": 3,
            "accept_spec": {
                "seam": "hard",
                "minimum_pass_rate": 1.0,
                "clauses": [
                    {
                        "id": "correct-count",
                        "path": "/count",
                        "operator": "equals",
                        "expected": 5,
                        "critical": True,
                    }
                ],
            },
        },
    }
)
decision = node.work_once(
    lease_request_id=f"demo-poll-{problem['problem_id']}",
    submission_id=f"demo-result-{problem['problem_id']}",
)
earnings = node_client.earnings()
final_problem = admin.get_problem(problem["problem_id"])

print(
    json.dumps(
        {
            "node_id": registration["node_id"],
            "offerings_published": 2,
            "problem_id": problem["problem_id"],
            "gate_outcome": decision["gate"]["outcome"],
            "problem_status": final_problem["status"],
            "accepted_outputs": final_problem["accepted_result"]["result"]["outputs"],
            "provider_payable": earnings["balances"],
            "classification": earnings["classification"],
        },
        indent=2,
        sort_keys=True,
    )
)
