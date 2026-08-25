# Solo execution mode

## Purpose

Solo mode makes the orchestration useful before an open provider network has
enough supply. One configured model identity performs several isolated Solver
roles, then performs synthesis. Network mode will consume the same logical
execution-plan and run-record envelope while replacing local model calls with
coordinator dispatch.

"One model" means one recorded model identity across multiple calls. It does not
mean one large prompt that asks a model to imitate a team internally.

The implemented path is:

```text
review-required decomposition proposal
        |
        | explicit review of this exact proposal digest
        v
content-addressed execution plan
        |
        | one isolated call per root axis, in deterministic DAG order
        v
schema + deterministic AcceptSpec checks
        |
        | only contract-valid branch outputs
        v
synthesis call -> schema + deterministic AcceptSpec -> run record
```

## Shared contract, separate deployment binding

`execution.ExecutionBackend` is the mode-level seam:

```python
class ExecutionBackend(Protocol):
    identity: dict[str, Any]

    def execute(self, plan_value: Any, *, run_id: str) -> dict[str, Any]: ...
```

`SingleModelBackend` implements it in version 0.2.0. A coordinator-backed
implementation remains deferred.

The logical plan deliberately excludes network deployment details such as
manifest digests, rewards, delegation allowances, currency, deadlines, node
identity, and lease credentials. Those belong in a network task binder. This
keeps Solo mode from inventing fake admissions or fake payments while preserving
the conceptual task graph that both modes need.

The coordinator currently represents a task tree, not an arbitrary DAG. The
first network backend can submit independent coordinator problems for logical
nodes; a reviewed initial-graph endpoint can come later if experiments justify
it.

## Execution plans

An execution plan contains:

- a problem statement plus JSON context;
- one to 64 logical tasks with stable ids, roles, objectives, optional
  dependencies, structured-output schemas, and deterministic acceptance specs;
- a required synthesis contract;
- an explicit seed;
- source provenance, including a decomposition proposal digest when applicable;
- a canonical `plan_digest`.

Dependencies form a DAG. The compiler rejects duplicate ids, unknown
dependencies, self-dependencies, cycles, unsupported schemas, oversized plans,
digest tampering, and unsupported fields that are not covered by the canonical
plan schema. Execution order is deterministic and recorded in the plan.

`execution_plan_from_decomposition` binds every root conceptual axis exactly
once. It refuses to compile unless:

- the proposal validates as the review-only artifact produced by the current
  decomposition algorithm;
- the root is structurally clean and passes completeness and routing checks;
- routing probes were caller-supplied rather than generated inside the semantic
  oracle; and
- a reviewer explicitly approves that exact `proposal_digest` and records a
  review basis.

`probe_source: provided` records the caller's assertion; it does not prove that
the probes were created independently or kept held out from the oracle. Likewise,
the review approves the decomposition proposal only. It does not approve later
task objectives, contexts, schemas, gates, or synthesis instructions. The local
plan author remains trusted. Review fields are provenance, not authentication or
payment approval. An application must add caller/operator authentication,
final-plan approval, and persistence around these boundaries.

The root compiler does not claim that marginal-value evidence is sufficient for
deeper recursion. It executes reviewed root responsibilities only. Recursive
materialization remains subject to the stricter evidence requirements described
in `conceptual-decomposition.md`.

## Model adapter

Solo mode accepts a provider-neutral structured-output adapter:

```python
class ModelAdapter(Protocol):
    identity: dict[str, Any]  # adapter_id, adapter_version, model

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]: ...
```

Each request provides:

- a deterministic call id, purpose, and seed;
- the problem;
- one task or the synthesis objective;
- only the direct dependency outputs needed by that call;
- the declared output schema; and
- the remaining run budget plus the per-call output-token ceiling.

The adapter receives the acceptance contract it is entitled to know: public
clauses remain intact, while `expected` is removed from every hidden clause.
This mirrors the provider view in the coordinator and prevents hidden expected
values from leaking into the model prompt. A live adapter is responsible for
turning the request into provider-specific messages and structured-output
configuration without exposing API credentials to the core or run record.

The complete problem, task context, and required dependency outputs are sent to
the adapter. Solo mode currently provides no confidentiality, residency,
retention, or provider-isolation guarantee; do not send sensitive workloads until
a deployment policy enforces those properties.

The response contract is:

```json
{
  "output": {"answer": "structured JSON"},
  "finish_reason": "completed",
  "response_id": "provider-response-id",
  "usage": {
    "input_tokens": 100,
    "output_tokens": 25,
    "reasoning_tokens": 10,
    "monetary_microunits": 2500,
    "complete": true
  }
}
```

The adapter must report complete non-negative usage for every successful call.
Provider response ids are retained for audit, but raw prompts, credentials, and
arbitrary exception messages are not written to the canonical trace.

## Budget behavior

The backend rejects a plan before model access if its minimum number of calls
(one per task plus synthesis) exceeds the call budget. Before each call it sends
the remaining entitlements to the adapter. After each response it adds actual
reported usage and fails closed if any ceiling is exceeded. The actual overrun
is retained, while the over-budget output is discarded.

Token and monetary totals ultimately rely on truthful, complete provider usage.
`accounting_complete` means the adapter asserted completeness, not that a trusted
meter independently observed it. Adapter and model identities are also declared
by the adapter. Separate request envelopes do not prove process or provider
isolation.

If invocation or response validation fails before usage can be trusted, the run
sets `accounting_complete` to `false`; it does not pretend the call was free.
Provider-side maximums and request timeouts still need to be implemented in each
live adapter.

This production runner does not reuse the matched-budget harness meter. The
harness has stricter reservation semantics for controlled experiments and
remains the authority for architecture comparisons.

## Verification boundary

A task becomes `contract_valid` only when:

1. the adapter returned a completed structured response;
2. the output satisfies the declared supported JSON schema; and
3. every critical deterministic AcceptSpec clause passes, with the configured
   minimum pass rate.

Synthesis is checked the same way. A contract failure stops the run and failed
branch outputs never reach synthesis.

This does **not** establish that an open-ended answer is true, useful, safe, or
better than a baseline. Every run record therefore contains:

```json
{
  "verification_boundary": "deterministic_schema_and_accept_spec_only",
  "semantic_verification": "unverified"
}
```

Only objective, customer-supplied gates can strengthen that statement. Model
judges, majority votes, and TEEs must not be relabeled as semantic proof.

## Run records

The canonical record includes:

- mode and exact model-adapter identity;
- source `plan_digest`;
- task outputs, contract decisions, mode-neutral executor identity,
  backend-specific references, response and output commitments, and per-call
  usage;
- a synthesis execution result with executor, backend reference, response digest,
  contract status, and usage, plus one retained final output and decision;
- aggregate usage and declared limits;
- accounting completeness;
- a sequence-numbered trace containing request digests instead of raw prompts;
- a sanitized terminal error when applicable; and
- a `record_digest` covering the complete record.

`validate_execution_record(record, plan_value=trusted_plan)` requires the exact
plan accepted before execution. It checks the plan digest and task mapping, then
replays every retained task and synthesis output through that plan's schema and
AcceptSpec. The replay binds clause order and metadata, hidden expected values,
the configured minimum pass rate, injected protocol checks, observations,
failure traces, and the deterministic evaluator. A record cannot substitute an
easier gate while retaining the original plan digest.

The validator also prevents a completed Solo record from claiming success
without all planned task contracts, a passing synthesis contract, complete
reported accounting, and no error. It checks the terminal status matrix, backend
start/terminal event pairing, task coverage, every task/synthesis result and
output commitment against its completed backend event, known per-call usage
against aggregate usage, and declared budgets. An unmetered backend failure
forces `accounting_complete: false`.

Plans and records are strict canonical envelopes. Validation compares the raw
artifact (excluding its digest field) with the fully rebuilt core and applies the
byte cap before normalization. Unknown fields are rejected rather than silently
dropped, so a digest or future signature cannot appear to authorize data it did
not actually commit.

Executor provenance is mode-neutral. Solo records use a `model_adapter` executor;
a network backend can use `network_node` identities plus coordinator problem,
task, offering, capability, and submission references without pretending the node
is one local model call.

Large expected or observed gate values are stored once in the output and replaced
inside repeated clause traces with a canonical content-digest reference. This
keeps a bounded model response capable of producing a bounded audit record.
Caller clause ids beginning with `protocol:` are rejected because that namespace
is reserved for injected schema and execution checks.

The runner preflights synthesis against cumulative branch outputs and stops once
the request cannot fit. If full final audit material would still exceed the
record cap after a paid response, the terminal record fails closed, retains task
outputs as digest references, preserves executor/usage evidence, and does not
publish an unauditable final answer. Digest-only task evidence is accepted only
on this `failed` / `record` path; it can never support a completed or payable
claim.

The plan and record digests establish canonical content identity, not
authenticity: anyone can modify an artifact and calculate a new digest unless a
trusted system pins, signs, or externally anchors the accepted plan and resulting
record. The validator's `plan_value` must therefore come from that trusted
boundary, not from an untrusted record author. Records contain no node, admin,
invite, lease, or provider API tokens.

Raw run records are administrator-private artifacts. Their full gate traces may
contain hidden expected and observed values. Do not expose them through a website
or public receipt until a dedicated public-view redactor removes hidden clauses
and sensitive submitter data.

Canonical records intentionally omit ambient timestamps and random UUIDs so a
scripted run with the same inputs can be reproduced byte for byte. A persistence
layer may store operational timestamps outside the digested core.

## Run the deterministic example

```bash
PYTHONPATH=src python3 examples/demo_solo_mode.py
```

The example performs a real end-to-end orchestration using a scripted adapter.
It validates the adapter and record contracts; it is not a model-quality result.

A completed Solo run means every declared JSON schema and deterministic
AcceptSpec passed. It does not mean the answer is true or useful.

## Deferred work

- one live hosted-model adapter and one local-model adapter;
- model-backed implementation of the decomposition oracle;
- persistent run/plan storage and resume after process failure;
- explicit cancellation and provider timeouts;
- bounded, charged structured-output repair policy;
- tool and code-sandbox execution;
- coordinator-backed execution of the same logical plan;
- objective benchmark cases comparing Solo and Network at matched budget; and
- caller authentication and redacted operator-facing trace views.
