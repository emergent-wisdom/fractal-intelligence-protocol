# Fractal Intelligence Protocol

An experimental reference protocol and implementation for one Solver-based
realization of fractal intelligence. A centralized coordinator and node SDK let
independent operators connect agent runtimes, advertise typed Solver abilities,
pull work, and earn a supplier payable when an acceptance gate clears.

This repository implements the smallest payable loop:

```text
provider registers node -> publishes Solver offering -> coordinator admits it
    -> provider polls for lease
    -> executes locally -> submits typed Result -> deterministic gate
    -> accepted work creates provider payable + platform revenue entries
```

It does **not** claim autonomous general intelligence, decentralized trust,
crypto custody, or reliable model-based judging. This implementation accompanies
[*Fractal Intelligence: Conceptual Decomposition as Problem-Solving Infrastructure*](https://doi.org/10.5281/zenodo.19462645)
(the paper). Its planning prototype did not execute or compare solutions; this
repository supplies protocol and experiment infrastructure for executable
evaluations.

Conceptual decomposition is intended to be generative as well as organizational.
A different carve changes which dimensions receive dedicated reasoning and which
contributions can be composed; it may therefore expose previously unconsidered
candidate mechanisms or solution families that an inherited task framing does
not make salient or separately searchable. This is a central hypothesis to test,
not a guarantee that every model-authored carve is novel, useful, or better.

The experimental layer now also includes:

- model-agnostic orchestration of candidate generation and semantic judgment
  through an injected oracle, with deterministic control flow for the four
  conceptual tests, bounded completeness search, routing sanity probes, and
  marginal-value recursion;
- content-addressed, review-only decomposition proposals;
- mandatory upward-first specific-to-Root routes with genus/differentia sibling
  assessment, typed specialization and composition edges, immutable reviewed
  topology patches, multi-parent concept reuse, and explicit parent synthesis;
- a client-side advisory materialization compiler that requires clean structure
  and exact admitted Solver manifests before it emits child task specifications;
- a deterministic five-arm matched-budget harness with blind evaluation and
  reservation/settlement accounting for calls, input, output, reasoning tokens,
  and monetary micro-units;
- a backend-neutral, content-addressed execution plan plus a Solo backend that
  runs isolated conceptual roles and synthesis through one model identity;
- structured-output schemas, deterministic gates, usage ceilings, sanitized
  traces, and content-addressed Solo run records validated against their exact
  trusted plans while explicitly leaving semantic verification unproven;
- a schema-v2, content-addressed archive of accepted submissions; and
- an administrator-only root reframe that can bind selected accepted descendant
  artifacts into a separately funded successor problem.

These are algorithm and experiment foundations, not evidence that conceptual
decomposition improves answers. Topology construction remains a reviewed,
portable artifact rather than an autonomous ontology learner or coordinator
authority. Reframing is operator-authorized reuse, not autonomous FrameError
detection, semantic deduplication, learned routing, or evidence that reuse
improves outcomes. See the dedicated design documents for the exact trust
boundaries and remaining work.

## Why the first version is centralized

The coordinator controls admission, task assignment, acceptance, and accounting.
Nodes make outbound HTTP requests, so an operator can connect an agent behind
NAT without deploying a public API. Protocol records keep nodes separate from
Solver manifests, making later registry federation and signed identities possible.

Sema remains the owner of content-addressed semantic identity. This project
stores opaque `concept_ref` values and adds deployment, execution, routing, and
economic state; it does not create a competing semantic registry.

## Run it

No third-party runtime dependencies are required.

```bash
python3 -m pip install .

fractal-coordinator serve \
  --database ./fractal-coordinator.db \
  --admin-token local-admin \
  --platform-fee-bps 1000
```

To run the source tests directly from a repository checkout:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The built-in server is plaintext and refuses non-loopback binding by default.
For remote nodes, keep it on loopback behind a TLS reverse proxy. The
`--allow-insecure-http` escape hatch is only for an isolated development network;
bearer credentials and task inputs must never cross an untrusted plaintext link.

Then run the complete local demonstration in another terminal:

```bash
PYTHONPATH=src python3 examples/demo_paid_word_count.py
```

The demonstration registers one provider node with two Solver offerings, funds
a problem through the development-only manual funding path, leases a task,
submits a result, clears a deterministic gate, and prints the provider payable.

Three deterministic, non-network examples exercise the new research layer:

```bash
PYTHONPATH=src python3 examples/demo_conceptual_decomposition.py
PYTHONPATH=src python3 examples/demo_matched_budget_harness.py
PYTHONPATH=src python3 examples/demo_solo_mode.py
```

The first uses a hand-authored oracle fixture to show the full decomposition
record. The second validates benchmark accounting with scripted arms. The third
compiles reviewed conceptual axes into isolated same-model calls and synthesis.
They validate contracts and control flow; none is a model-performance result.

## Connect an agent

An adapter only needs to:

1. Register once using an invite token plus a stable registration id; retain both
   the returned node token and registration id.
2. Publish one manifest per Solver ability. A node may publish many abilities;
   an administrator must admit each offering before it can receive inputs.
3. Poll `POST /v1/node/leases` with a unique, persisted lease-request id.
4. Execute the returned task inside the operator's own runtime.
5. Journal the prepared Result durably, then submit it with a persisted submission
   id and the one-time lease token.

An administrator creates each short-lived, single-use invite through the client
or `POST /v1/node-invites`; there is no reusable public registration secret.
Registration and lease acquisition are replay-safe if an HTTP response is lost.
Registration recovery is available only until the one-time invite expires.

The reusable client is in `fractal_protocol.client.CoordinatorClient`.
`ConnectedNode` requires a `SQLiteResultJournal` (or equivalent durable journal),
so a lost submission response reuses the byte-identical prepared Result instead
of executing the Solver twice. SQLite claims execution atomically across local
processes and namespaces records by coordinator plus node credential; completed
rows discard the full Result and retain only a receipt/idempotency tombstone.
The demo shows the full sequence without hiding protocol details; set
`FI_NODE_JOURNAL` to choose its provider-state path.

## Current verification boundary

Automatic earnings are enabled only for deterministic clauses (`exists`,
`equals`, `type`, `minimum`, `maximum`, and `contains`). Critical clauses are
non-compensatory. A rejected result produces a typed failure trace and no
payable. Human and probabilistic gates are modeled as future work, not silently
treated as trustworthy.

Security placement constraints such as region, retention, confidentiality, or
TEE requirements are rejected in v1 because the coordinator cannot enforce them
yet. Do not submit sensitive workloads to this alpha.

## Current money boundary

The ledger records:

- externally confirmed platform funding,
- problem escrow allocation,
- provider payables for accepted work,
- platform fee revenue, and
- refund-pending liabilities for unused order funding.

It is not a stored-value wallet. The manual funding endpoint is for development
and is admin-authenticated. External collection, refund, custody, and payout are
deployment boundaries rather than functions of this protocol kernel.

## Design documents

- [Architecture](docs/architecture.md) — boundaries, object model, and phases
- [Protocol v1](docs/protocol-v1.md) — HTTP contract and gate semantics
- [Verification](docs/verification.md) — why TEEs are later infrastructure
- [Conceptual decomposition](docs/conceptual-decomposition.md) — executable four-test algorithm, marginal value, and materialization boundary
- [Mandatory-abstraction topology](docs/topology-construction.md) — upward-first routing, typed graph evolution, multi-parent reuse, and reviewed execution compilation
- [Benchmarking](docs/benchmarking.md) — five-arm matched-budget experiment contract
- [Solo mode](docs/solo-mode.md) — shared execution plan, one-model mode, budgets, and run records

## License

The code and protocol documentation in this repository are available under the
[MIT License](LICENSE).
You may use, modify, distribute, and build commercial systems from them subject
to the license terms.
