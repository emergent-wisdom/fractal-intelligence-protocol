# Reference architecture: centralized alpha

## Protocol objective

Let people connect independently operated agents, let a coordinator assign those
agents parts of a problem, and compensate providers only for accepted work. The
initial reference implementation uses curated admission and centralized
coordination so that its trust and accounting boundaries remain inspectable.

The alpha is deliberately a central broker. It provides the shortest path to a
working feedback loop while preserving records that can later cross trust domains.

## System boundary

```text
Problem submitter / operator
        |
        v
Central coordinator ---- append-only Pathway events
  |     |     |
  |     |     +---- supplier-payable ledger
  |     +---------- deterministic acceptance gates
  +---------------- outbound-polled task leases
                         ^
                         |
                  Provider nodes
                  (agent runtimes)
```

The coordinator stores task payloads in this version. Security-placement
constraints are rejected rather than pretended to be enforced; sensitive
workloads need a retention, encryption, and verified-node policy before use.
The built-in HTTP server is a loopback development origin. Remote provider traffic
must terminate TLS at a reverse proxy; non-loopback plaintext binding is refused
unless an operator supplies an explicit development-only override.

## Two execution modes, one logical plan

The reference implementation has a backend-neutral logical execution plan. It
contains the problem, a bounded task DAG, structured-output contracts,
deterministic gates, synthesis, source provenance, and a canonical digest.

```text
              proposal-reviewed logical execution plan
                         /           \
                        v             v
            SingleModelBackend   CoordinatorNetworkBackend
                 (implemented)          (next)
                        \             /
                         v           v
                    execution run record
```

Solo mode uses one recorded model identity for separate role calls and a final
synthesis call. Network mode will bind the same logical tasks to admitted
manifests, funding, deadlines, and coordinator problem ids. Those deployment and
economic fields do not belong in the shared logical plan.

The current coordinator-backed implementation is not yet wrapped behind this
execution contract. The coordinator also represents a task tree rather than an
arbitrary DAG, so the first network adapter may submit one coordinator problem
per ready logical node. This limitation is explicit rather than hidden behind a
fake local admission or payment path.

## The important identity separation

```text
ProviderNode 1 ---< SolverOffering >--- 1 SolverManifest ---> Sema concept ref
```

- A **ProviderNode** is an operator-controlled runtime and authentication identity.
- A **SolverManifest** is a content-addressed description of one ability.
- A **SolverOffering** says that a node currently offers that manifest.
- A node can offer many manifests; several nodes can offer the same manifest.
- A Solver can internally coordinate any number of agents. The coordinator does
  not prescribe that private implementation.

Registration admits an identity, not an ability or proof of competence. Each
offering remains pending until a coordinator administrator admits that exact
node/manifest pair. Trust is earned from typed gate events scoped to problem
class and Solver offering; it is not a universal reputation number.

## Paper-to-runtime mapping

The paper's invariant Solver surfaces remain the conceptual contract:

| Surface | Alpha representation |
|---|---|
| Manifest | Content-addressed offering manifest |
| Execute | Pull lease plus Result submission |
| Consult | Paper-level surface; transport deferred |
| Verify | Coordinator-owned deterministic gate; Solver transport deferred |
| Feedback | Gate/Pathway records exist; Solver transport deferred |

Tasks contain an operation, typed JSON inputs, inherited constraints, an
AcceptSpec, and a monetary reward. Results contain outputs, execution status,
stop reason, evidence, and measured usage. The coordinator—not the executor—owns
the payable gate.

## Recursive work

A leased Solver may yield its task and create child tasks. The coordinator:

1. requires every child to retain every parent constraint exactly,
2. permits additional child constraints,
3. rejects child rewards and nested allowances that exceed the parent's explicit
   delegation budget,
4. records the decomposition as a proposal that only an administrator/coordinator
   policy may approve,
5. records proposer provenance on each approved child and excludes the proposer
   from executing it unless the administrator explicitly allows self-execution,
6. moves the approved parent to `waiting_children`, and
7. reopens the parent for synthesis after every child is accepted.

Exact constraint inheritance is intentionally conservative. Generic semantic
"tightening" cannot be proven without a constraint-specific comparator.

Delegation budgets are hierarchical. A provider cannot assign itself the rest of
the submitter's problem funding merely because it holds a lease; the coordinator
must explicitly place an allowance on that task and approve the concrete child
rewards, capabilities, and gates.

Delegation proposals have their own bounded attempt count. Rejected proposals
cannot create unbounded review churn, and any undecided proposal becomes `void`
when its problem terminates.

Execution within each problem remains a task tree. An authorized reframe adds an
audited lineage edge between a source problem and a successor, but it does not
create shared subproblems, cycles, semantic deduplication, or a general persistent
Solver graph. Those remain deferred until execution experiments produce useful
evidence.

## Experimental conceptual planning

The research layer has an executable conceptual-decomposition engine, but its
output is a separate artifact from the task tree. An injected semantic oracle
generates dimensions and judges necessity, independence, universality,
completeness, and routing. Deterministic coordinator-independent code owns limits,
canonicalization, content hashes, decimal marginal-value arithmetic, and the
decision trace. Every result is labeled `review_required`; semantic model verdicts
are evidence, not payable gates.

A pure, client-side materialization plan maps proposal root axes to exact
protocol-v1 task specifications and an admitted-manifest snapshot. Missing axes,
unresolved capabilities, incomplete coverage, routing conflicts, exhausted
planning limits, or unresolved depth probes block compilation into a delegation
request. The existing live-lease, budget, constraint, and administrator approval
checks remain authoritative.

This compiler is advisory in the current alpha. Its delegation payload contains
ordinary children, not the proposal/plan hashes or axis bindings; the server does
not persist or display conceptual evidence and providers can bypass the helper.

Conceptual proposals do not yet have their own coordinator persistence or review
API and are not crystallized into a shared Sema graph. This avoids pretending that
the provider-specific `delegations` table is already a durable ontology registry.

The offline matched-budget harness is similarly observational: it creates no
problem, task, Pathway, or ledger record. It exists to compare five architectures
under the same declared resource caps before any learned routing claim is made.

The Solo executor is operational rather than payable. A reviewed root proposal
can be compiled into one logical task per axis, executed in deterministic order,
checked against supported JSON schemas and hard AcceptSpecs, and synthesized.
Public acceptance clauses reach the executor while hidden expected values are
redacted. Every run is content-addressed and records adapter-declared identity
and usage. The proposal review does not authenticate or approve later task
bindings, and a content digest is identity rather than a signature. Contract-valid
outputs remain labeled `semantic_verification: unverified`. Record validation
requires the exact plan accepted before execution and replays retained-output
schema and acceptance checks against it; the plan must be stored and pinned by a
trusted application boundary.

## Routing

Initial routing is exact and inspectable:

1. node is active,
2. the exact node/offering pair has explicit coordinator admission,
3. task `required_capability` equals an approved full offering manifest digest,
4. offering declares the task operation,
5. delegation provenance permits that node to execute the task,
6. the node is below its configured in-flight cap, and
7. node wins the next atomic, replay-safe lease.

Later routing can rank compatible offerings with local Pathway aggregates,
cost/latency, exploration credit, and Consult estimates. No learned global score
is present in the alpha.

Concept references are never leased directly. The coordinator must resolve a Sema
concept to an admitted immutable manifest first; otherwise a node could claim the
same concept while exposing incompatible schemas or behavior.

## Verification and retries

Hard gates are non-compensatory: every critical deterministic clause must pass.
A rejection records the exact clause, expected state, observed state, and
evaluator. Rejected work creates no payable and reopens the task until its attempt
limit is exhausted. A rejection is not automatically evidence that the parent
frame was wrong. Full hidden-clause evidence remains coordinator-side; providers
receive only public failures and cannot re-lease a task they failed.

Ordinary retry remains the default. An administrator may instead record a
FrameError assessment against the latest rejected root, change its structural
contract, and create a separately funded successor after all descendant work is
accepted and the source is quiescent. Selected accepted descendants are carried
into the successor as immutable artifact bindings. This is operator-authorized
root recomposition, not automatic failure diagnosis, lateral search, or learned
topology mutation.

The cited rejection must come from a submitted root Result; an automatic lease
timeout is operational failure evidence and cannot anchor a FrameError record.

The successor root-contract digest includes an ordered, coordinator-typed list of
binding names and artifact digests. Archived payloads are expanded from those
committed descriptors at lease time. Private source-contract and gate commitments
remain in the coordinator rather than exposing hashes of hidden expected values.

Every funded problem has a required deadline. A background reaper records lease
timeouts, expires orders, invalidates live leases, and moves unused funds to the
refund-pending liability. This prevents an order with no remaining eligible
provider from locking funds forever.

## Economic accounting

Amounts are integer minor units. Append-only transfers move value between:

- `external:funding`
- `escrow:<problem_id>`
- `payable:<node_id>`
- `revenue:platform`
- `refund_pending:<problem_id>`
- `external:payout` (reserved for a future payout adapter)

After coordinator approval, a child that clears its own gate earns irrevocably.
Its reward moves from escrow to provider payable and platform revenue according to
the order's fee terms. A later parent failure cannot harvest that accepted child
output for free; it refunds only the unspent remainder. The approval boundary
prevents an untrusted decomposer from manufacturing its own paid trivial work.
Idempotency keys make double payment impossible for a task. When a problem
completes or blocks, unused funding moves to a per-order
`refund_pending` liability instead of becoming silent platform revenue. Actual
collection, refund, and payout are adapter boundaries.

A retained-artifact binding reuses a source artifact whose payable already exists;
it creates no second transfer. The successor receives independent funding, and a
still-active source refunds only its unspent balance when the reframe is recorded.

Fee basis points are snapshotted on problem creation. A deployment restart or fee
change cannot alter already quoted provider earnings.

## Decisions made for the alpha

- Protocol software is maintained separately from manuscript artifacts.
- Python 3.11+ standard library; zero runtime dependencies.
- Coordinator-brokered pull leases rather than public node endpoints.
- Invite-only, replay-safe node registration plus per-offering admin admission.
- Opaque bearer tokens stored only as hashes; portable Ed25519 identity is next.
- Replay-safe leases and node-scoped durable provider Result outboxes with atomic
  cross-process execution claims, required problem deadlines, and a scheduled
  timeout reaper.
- Default one-lease node capacity and independent execution of delegated children.
- Bounded task/Result payloads, per-problem task caps, and paged submission audits.
- Public execution contracts plus coordinator-private hidden verifier clauses.
- Manual topology changes through controlled recursive delegation and one
  operator-authorized root-successor reframe.
- Local append-only SQLite Pathway events.
- No TEE, blockchain, token, staking, custody, or autonomous probabilistic judge.

## Phases

1. **Payable protocol kernel:** register, advertise, lease, gate, retry, account.
2. **Objective benchmark:** compare one frontier model, a role-agent baseline, and
   Fractal routing at matched compute on tasks with ground truth.
3. **Pathway routing:** scoped aggregates, cold-start probes, blind route comparison.
4. **External settlement adapters:** authenticated collection, refund, and payout
   integrations.
5. **Portable identity and federation:** signed node records and multiple registries.
6. **Experimental decomposition:** four decomposition tests, marginal-value
   recursion, automatic Frame Error heuristics, and broader reviewed topology
   mutations. Operator-authorized root contract mutation already exists.

## Explicit non-claims

This implementation does not establish that conceptual decomposition improves
answers, that reuse compounds intelligence, or that open-ended verification is
solved. It enables controlled tests of those claims.
