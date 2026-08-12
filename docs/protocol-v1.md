# Coordinator protocol v1

All request and response bodies are JSON. Protected routes use:

```http
Authorization: Bearer <token>
```

The admin token is deployment configuration. Provider invites are short-lived,
single-use database records. Registration is keyed by a provider-generated stable
id, and the database stores only bearer-token SHA-256 digests. An exact replay
after a lost response returns the same node identity and credential until the
one-time invite's original expiry. After that bounded recovery window, the invite
cannot regenerate the node bearer token.

## Node lifecycle

### Register

An administrator first calls `POST /v1/node-invites` with a label and expiry, then
delivers the returned one-time token to the provider. The provider calls
`POST /v1/nodes/register` using that invite token.

```json
{
  "registration_id": "registration_018f...",
  "operator_name": "example-lab",
  "metadata": {"region": "eu-north"}
}
```

The response contains `node_id` and `node_token`. Persist `registration_id`
before the first request; reusing it with different registration fields is a
conflict. Registration admits the node identity, not its abilities or competence.

### Publish a Solver offering

`POST /v1/node/offerings` using the node token.

```json
{
  "concept_ref": "sema:WordCount#mh:SHA-256:<64-hex-digest>",
  "name": "Word Count",
  "description": "Counts whitespace-delimited words",
  "cognitive_mode": "convergent",
  "operations": ["count_words"],
  "surfaces": ["manifest", "execute"],
  "input_schema": {"type": "object"},
  "output_schema": {"type": "object"}
}
```

The coordinator canonicalizes these immutable fields and returns a full
`sha256:<hex>` manifest digest. Mutable capacity and pricing belong on the
offering, not in the manifest identity.

The coordinator treats `concept_ref` as an opaque string. The example uses
Sema's full identifier syntax, but this protocol version does not validate or
resolve it.

New offerings are `pending`. Before a provider can receive task inputs, an
administrator inspects `GET /v1/offerings?status=pending` and calls
`POST /v1/offerings/{offering_id}/approve`. Admission is per node/offering, so
an invited node cannot activate a copied manifest by itself.
The admin-only review response includes the complete canonical manifest,
operator name, node status, and clearly labeled self-asserted node metadata.

Protocol v1 accepts exactly one operation and the `manifest`/`execute` surfaces
per manifest. Consult, Verify, and Feedback remain paper-level reserved surfaces
until this network provides callable transports for them; nodes cannot advertise
an interface they cannot honor.

Paid tasks route only by that full manifest digest. A Sema `concept_ref` is a
semantic discovery input, not sufficient execution authority; the coordinator
must resolve it to an admitted manifest before creating a task.

### Heartbeat

`POST /v1/node/heartbeat` using the node token.

### Lease work

`POST /v1/node/leases` using the node token and a provider-generated request id:

```json
{"lease_request_id": "poll_018f..."}
```

The response is `204 No Content` when no compatible task exists. A successful
response includes a task, accepted child results when this is a resumed parent,
the immutable root intent/problem class, an explicit gross/fee/net compensation
quote, and a one-time `lease_token`. A stale or
mismatched lease cannot submit. If the response is lost, retrying the same
node-scoped request id returns the identical lease token without consuming a
second attempt. A node has one in-flight lease by default; deployments can raise
that explicit cap.

A successor root created through an authorized reframe also receives its
explicitly named retained-artifact bindings and archived envelopes. A source
pass is scoped to the source contract: receiving its envelope does not accept it
under the successor contract, and the successor root must still clear its own
gate. Any admitted provider that can lease or re-lease the successor capability
can receive those envelopes; bindings do not have provider-specific access lists.

### Delegate child tasks

`POST /v1/node/tasks/{task_id}/children` using the node token.

The body contains a node/task-scoped `idempotency_key`, `lease_token`, and
`children`. Every child declares capability, operation, inputs, constraints,
AcceptSpec, reward, and attempt limit. Delegation ends the current parent lease
and returns a server-generated `delegation_id` in `proposed` state. It does not
create payable work yet.

The coordinator lists proposals with `GET /v1/delegations?status=proposed` and
authorizes one with `POST /v1/delegations/{delegation_id}/approve`. Approval is the
economic permission boundary: it creates the children only after a trusted policy
or operator reviews their capabilities, gates, and rewards. By default, the
proposing node cannot execute those children. An administrator must deliberately
set `{"allow_self_execution": true}` to waive executor independence. The parent
resumes after all approved children pass.

The default approval may omit its JSON body and means
`allow_self_execution: false`; an explicit body must be a JSON object.

The coordinator may instead call `POST /v1/delegations/{delegation_id}/reject`
with an audit reason. Rejection is idempotent and reopens the parent so it can
solve shallowly or propose a different frame; it does not strand the order.
Proposal churn is bounded separately from execution attempts. Cancellation,
expiry, or blocking changes undecided proposals to the terminal `void` state.

### Submit a Result

`POST /v1/node/tasks/{task_id}/submissions` using the node token.

```json
{
  "submission_id": "provider-generated-idempotency-key",
  "lease_token": "one-time-secret",
  "status": "success",
  "stop_reason": "completed",
  "outputs": {"count": 4},
  "evidence": {"method": "split"},
  "usage": {"duration_ms": 2}
}
```

The response includes the gate decision and whether earnings were created.
Repeating the same submission ID and Result returns its original outcome; reusing
the ID with a changed Result is a conflict. Client submission ids are scoped to
the authenticated node and task; the coordinator also returns a globally unique
`submission_receipt_id` for audit references.
Persist the submission id before sending. The Python `ConnectedNode` adapter
therefore requires the caller to supply registration, lease-request, and
submission ids rather than hiding crash-sensitive random values in memory. It
also requires a durable Result journal. The adapter writes the exact status,
outputs, evidence, and usage before submission; retry after a lost response sends
that same prepared Result and never re-executes the handler. Before invoking a
handler, its SQLite journal atomically claims the coordinator/node/task/submission
and hashed-lease scope, preventing two local processes—or two submission ids—from
executing one lease concurrently. A
claim abandoned by a process crash is not reused under the same lease; a later
coordinator lease may supersede it. Solver handlers with external side effects
should still be idempotent by task id because no local journal can make an
arbitrary external side effect and its subsequent Result write atomic.

The journal applies the protocol Result-size bound before persistence and never
stores a lease or node bearer. On receipt, it drops the full prepared Result and
keeps the smaller receipt plus digest. Operators may call
`prune_completed_receipts` after their retry/audit period; a compact tombstone
retains both Result and lease identity digests, so an old idempotency key or lease
cannot execute again. The cleanup call rejects
future cutoffs so a clock/configuration mistake cannot remove a current receipt.
Abandoned execution claims remain as compact safety records and are superseded
only by a different coordinator-issued lease identity; provider and coordinator
wall clocks are never compared. Journal files have their own schema version and
reject incompatible files at startup.

An approved child reports `earning_status: available` when its own gate passes.
That payable is irrevocable even if an ancestor later fails; otherwise a parent
could consume honest child output and deliberately fail to avoid paying. A blocked
problem refunds only its unspent order balance.

### Accepted artifact archive

Every passing network submission is archived once as a content-addressed
accepted-artifact envelope, and its submission response includes
`accepted_artifact_digest`. The envelope records the source problem class,
solver-visible capability, operation, inputs, constraints, outputs, and evidence,
plus the coordinator's assertion that the deterministic v1 gate passed. It omits
the AcceptSpec, GateDecision, usage, provider identity, and accounting fields, so
the envelope alone cannot reproduce that verdict.

The coordinator keeps full source-contract and GateDecision digests privately
beside the immutable source-submission reference and checks those commitments
before authorizing reuse. They are not provider-visible because deterministic
hashes of low-entropy hidden expected values would create a guessing oracle.

The digest identifies one exact accepted submission envelope. It is not a Sema
semantic identity, a global deduplication key, a signature, or proof that the
artifact satisfies a later contract. Schema-v1 passing submissions are archived
idempotently when a schema-v2 coordinator starts.

Protocol v1 exposes no general artifact lookup or cross-problem reuse API. A
provider receives an archived envelope only through an explicitly authorized
successor-root binding.

## Problem creation

`POST /v1/problems` uses the admin token in this alpha. `funding_reference` is a
unique external idempotency key. In production, only a verified payment webhook
may provide it.

```json
{
  "intent": "Count the words in the supplied text",
  "problem_class": "objective.text.counting",
  "funded_amount_minor": 1000,
  "currency": "USD",
  "funding_reference": "dev-payment-001",
  "deadline_at": 1800000000,
  "task": {
    "required_capability": "sha256:<manifest-digest>",
    "operation": "count_words",
    "inputs": {"text": "one two three four"},
    "constraints": {"workflow_scope": "task_only"},
    "reward_minor": 500,
    "delegation_budget_minor": 0,
    "max_attempts": 3,
    "accept_spec": {
      "seam": "hard",
      "minimum_pass_rate": 1.0,
      "clauses": [
        {
          "id": "correct-count",
          "path": "/count",
          "operator": "equals",
          "expected": 4,
          "critical": true,
          "disclosure": "hidden"
        }
      ]
    }
  }
}
```

`deadline_at` is a required future Unix timestamp. On expiry, every open/live lease is
invalidated and unused funding moves to refund-pending. Already accepted child
payables remain irrevocable. An administrator can trigger the same safe transition with
`POST /v1/problems/{problem_id}/cancel`. The HTTP server runs a background reaper
for both problem deadlines and lease timeouts; `reap_expired` is also an idempotent
maintenance entry point.

Constraints are solver-visible semantic invariants and must be inherited by
children. V1 rejects security-placement keys such as retention, region,
confidentiality, data residency, execution environment, and TEE requirements
because node eligibility is not yet capable of enforcing them.

### Operator-authorized root reframe

`POST /v1/problems/{problem_id}/reframes` is an admin-only, replay-safe operation:

```json
{
  "idempotency_key": "root-frame-repair-1",
  "source_submission_receipt_id": "submission_...",
  "diagnosis": {
    "kind": "frame_error",
    "summary": "The root contract omitted cross-child arbitration.",
    "diagnosed_by": "operator:review-board",
    "required_changes": ["Add explicit arbitration to the root output"],
    "evidence": {}
  },
  "retained_artifacts": [
    {
      "binding": "leaf_finding",
      "source_submission_receipt_id": "submission_..."
    }
  ],
  "successor_problem": {
    "...": "a complete ordinary problem-creation request"
  }
}
```

The coordinator does not diagnose a FrameError. It executes an administrator's
diagnosis only when the cited receipt is the latest rejected root attempt, the
root is not leased or waiting on delegation, every descendant is accepted, and
no delegation proposal remains pending. The source must be active or blocked.
An automatic lease-timeout rejection cannot serve as the FrameError source.
The successor preserves intent, problem class, currency, and inherited
constraints. Its structural root contract must change, and it uses fresh funding
and a fresh funding reference.

The successor's full root-contract digest commits both the ordinary normalized
TaskSpec and an ordered descriptor for every retained binding (`binding` plus
`artifact_digest`). The coordinator validates that fixed descriptor shape and the
artifact content digest before leasing the successor. Full archived envelopes are
expanded as read-only context keyed by those committed digests.

Selected artifacts must be accepted, payable-backed descendants of the source
problem. Each retained binding creates no separate execution task, payable,
Pathway event, or ledger transfer; the separately funded successor still creates
and pays for its own root task normally. An active source is cancelled and only
its unused balance moves to refund-pending; an already blocked source remains
blocked. The response records `detection_mode: operator_authorized`, both full
root-contract digests, lineage, and the exact retained bindings. The coordinator
validates these structural preconditions, not whether the diagnosis is
scientifically justified.

`retained_artifacts` requires between 1 and 100 entries. Binding names and source
receipts must each be unique, and one cited root rejection can have only one
successor. The idempotency key is scoped to the source problem. `diagnosed_by` is
caller-supplied audit text; the coordinator authenticates the administrator's
request but does not authenticate the identity named in that field.

`GET /v1/problems/{problem_id}` returns the task tree, a page of the
coordinator-side submission/gate audit trail, accepted-artifact digests for
submissions in that selected page, ledger transfers, reframe lineage when present,
and `accepted_result` for the root when complete. This route is admin-protected
because it contains task inputs, hidden verifier details, and provider evidence.

The audit trail is paged with `submission_limit` (default 50, maximum 200) and
`submission_offset` (maximum 1,000,000); `submission_page` reports the total. The accepted root
result is returned independently of the selected page. Task specifications and
Results are capped at 256 KiB, manifests at 64 KiB, HTTP bodies at 512 KiB, and
each problem at a configured task count (100 by default). Nodes may publish 50
offerings by default. Each monetary input is capped at
1,000,000,000,000 minor units. The collection of retained envelopes attached to
one successor is capped at the Result-size boundary. External storage for larger
payloads remains a future artifact adapter.

## Deterministic AcceptSpec

JSON Pointer paths address Result `outputs`. Supported operators are:

- `exists`
- `equals`
- `type` (`object`, `array`, `string`, `number`, `integer`, `boolean`, `null`)
- `minimum`
- `maximum`
- `contains`

Every critical clause must pass. The remaining pass rate must meet
`minimum_pass_rate`. Failed clauses become a typed failure trace. A Result with
`partial`/`fail` status or a non-`completed` stop reason is rejected before
content clauses and never earns automatically.

All payable v1 seams are hard. Soft/divergent composition will be added only with
an explicit non-payable or parent-reviewed settlement policy; merely labeling the
same evaluator `soft` would provide no real semantics.

Clauses with `disclosure: hidden` retain their expected value only inside the
coordinator; leased nodes see the clause shape but not the answer/test threshold.
Exact-equality and containment clauses default to hidden. Structural type and
numeric threshold clauses default to public because they normally define the
executor's contract; mark one hidden explicitly when it is private test data.
This is a basic hidden-test boundary, not cryptographic proof against a malicious
coordinator.

Provider responses omit hidden clause-level outcomes, values, and pass rates. A
node whose Result is rejected is excluded from re-leasing that task; otherwise
the overall reject signal itself becomes a small-domain answer oracle. Another
eligible provider may try the task. Invite admission remains the Sybil boundary
in this centralized alpha, while offering admission controls which invited nodes
may receive each immutable capability's inputs.

## Accounting

`GET /v1/node/earnings` returns the authenticated node's payable balance and
accepted-work transfers. It is a supplier accounts-payable record, not a wallet
balance and not a promise of instant redemption.

SQLite files carry an explicit schema version. Schema-v1 coordinator databases
are upgraded additively to schema v2, which adds accepted-artifact and reframe
records; passing legacy submissions are then archived idempotently at startup.
Unversioned databases and unsupported schema versions still fail fast.
