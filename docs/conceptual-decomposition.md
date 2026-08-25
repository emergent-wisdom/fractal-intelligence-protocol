# Conceptual decomposition: experimental v1

The repository now contains an executable, model-agnostic interpretation of a
bounded subset of the paper's first conceptual-decomposition algorithm. It is an
experimental planning layer, not a payable verification gate and not a claim that
the generated concepts are objectively correct.

## Generative purpose

A conceptual carve is not merely a partition of known work. It supplies search
coordinates: when an accepted dimension receives a distinct Solver boundary,
reasoning can pursue mechanisms native to that dimension, and parent synthesis
can combine the resulting contributions. Changing the carve may therefore make
previously unconsidered solution candidates locally generable when an inherited
task framing would not expose them.

Neither structural acceptance nor model self-report establishes novelty or
usefulness. Those are relational outcome claims: a credible experiment must
compare equally resourced carves, retain the route from frame and dimension to
candidate and final result, and judge distinctness and utility without revealing
the condition. The reviewed topology and execution-plan path preserve
frame-and-axis-to-task provenance. Candidate-mechanism and
candidate-to-final-result attribution still require suitable caller-supplied
output schemas; the protocol core does not guarantee them or assign a dedicated
novelty score.

## Control flow

`ConceptualDecompositionEngine` owns the bounded, deterministic orchestration:

1. Ask an injected `DecompositionOracle` for candidate dimensions.
2. Evaluate necessity, independence, and universality for every candidate.
3. Retain only candidates that pass all three tests; preserve rejected candidates
   and their rationales in the audit artifact.
4. Evaluate set-level completeness. Search for and retest at most three missing
   dimensions by default.
5. Route three concrete representative tasks by default. Every task must select
   exactly one accepted axis. A zero-axis route is a gap; a multi-axis route is a
   conflict.
6. Calculate the marginal-value rule from supplied shallow/deep evidence and
   recurse only when the ratio is strictly greater than the configured threshold.
7. Return a canonical, content-addressed proposal whose status is always
   `review_required`.

Candidate generation and semantic judgments remain probabilistic. The engine
does not relabel them as proofs; every artifact carries the boundary
`model_judgments_are_estimates_not_proof` and records the generator, judge,
router, marginal-value source, adapter version, limits, usage, and warnings.
The engine counts oracle interface invocations, not internal model calls; a real
adapter must maintain its own authoritative token and monetary usage record.

The oracle interface is defined in
`src/fractal_protocol/decomposition.py`. It can be implemented by one model, an
ensemble, recorded human judgments, or deterministic fixtures. No vendor model
SDK or credential handling is embedded in the protocol core.

## Routing probes

The oracle can generate routing probes, which is useful as a smoke test. A serious
experiment should instead pass a fixed held-out probe set to:

```python
proposal = engine.decompose(concept, routing_probes=held_out_probes)
```

The proposal records whether probes were `provided` or `oracle_generated`. Fixed,
domain-diverse probes avoid letting the same model invent both the decomposition
and the evidence that appears to confirm it.

The list form above supplies only the root set. Recursive experiments should pass
a `RoutingProbeProvider` with a stable `provider_id`; the engine invokes it with
the current concept, depth, path, and required count at every node. Materialization
requires `provided` probes at every included depth, so a recursive proposal cannot
self-certify its descendants with oracle-generated tasks.

## Marginal value

The engine computes the paper's rule rather than accepting a model-authored final
score:

```text
(deep value - shallow value + exploration credit)
-------------------------------------------------  >  threshold
             deep cost - shallow cost
```

The oracle supplies the observations, units, sample count, uncertainty, basis, and
rationale. Decimal arithmetic derives the ratio and uses strict `>` at the
threshold. If paired evidence is absent, the oracle must report
`evidence_status: insufficient`; the engine emits a `probe` decision instead of
inventing a default estimate.

The threshold is finite and non-negative but otherwise domain-specific. One
configured value-unit/cost-unit pair applies to the entire proposal, making sibling
ratios comparable. Decimal input size is bounded and arithmetic uses a fixed local
precision and rounding policy, independent of the caller's ambient decimal
context. Sibling estimates are collected before recursion and the bounded node
budget is assigned to the highest supplied ratios first. This is still a one-shot
allocator: uncertainty-aware portfolio allocation and the paper's lateral reframe
when marginal value is flat are not implemented.

Separately, the network coordinator can execute an administrator-authorized root
successor and carry selected accepted descendant artifacts into it. That mechanism
is not driven by this decomposition engine and does not implement automatic
flat-progress detection or a lateral FrameError search policy.

## From concepts to executable work

Concept axes and payable tasks are deliberately separate artifacts.
`build_materialization_plan` is a client-side advisory compiler. It maps every
proposal root axis to an exact protocol-v1 `TaskSpec` and an explicit snapshot of
admitted manifest digests. A plan remains `blocked` when:

- completeness or clean routing was not established at any included depth,
- oracle/node limits were exhausted or marginal-value evidence still requires a
  probe,
- a root axis has no task binding, or
- the required Solver manifest was not admitted.

Plans with more than the coordinator's 20-child limit are also blocked. Compilation
rechecks every bound capability against the current admitted set, the relevant
capability commitment, idempotency-key length, and the HTTP request-size limit.
Unrelated newly admitted capabilities do not invalidate an existing plan.

Only a `ready` plan can be compiled by `delegation_payload_from_plan` into the
existing provider delegation request. That request still requires a live lease,
constraint and budget validation, and administrator approval. Model judgments
therefore cannot create work or authorize payment by themselves.

The current delegation payload carries only the compiled child task specs. It does
not persist the proposal digest, plan digest, or axis bindings, and a provider can
still submit an ordinary child proposal without using this helper. Consequently,
the coordinator administrator is reviewing the executable children—not yet the
conceptual evidence. Dedicated proposal persistence and review are required before
this becomes an authoritative bridge.

Mapping exactly one root axis to one child task is a conservative v1 execution
policy, not a theorem of Fractal Intelligence. The paper's intended steady state
has many tasks routing through reusable persistent concept Solvers.

The separate [mandatory-abstraction topology profile](topology-construction.md)
now supplies a review-only realization of that routing structure. It requires a
specific-to-Root specialization ascent, its exact reverse traversal, immutable
multi-parent graph reuse, and bindings from this four-test artifact to direct
composition children. It does not change this engine's semantic judgments or
turn them into proofs.

## Current limits

This is the first executable algorithm, not the final ontology system:

- Independence follows the paper's incremental pseudocode and is therefore
  potentially order-dependent. A later version should record a full pairwise
  independence matrix and generator/judge disagreement.
- Boolean semantic verdicts do not yet carry witness sets, counterexamples, or an
  `uncertain` state.
- Proposals, portable topology snapshots, and materialization plans are
  content-addressed artifacts but are not yet persisted in a dedicated
  coordinator review table or crystallized into a Sema concept graph.
- There is no production LLM adapter, evaluator quorum, semantic deduplication,
  Frame Error detector, automatic semantic merge/reframe search, or
  routing-warning repair loop. The topology profile can validate an
  all-or-nothing pure transition for an explicitly reviewed structural patch; it
  is not an automatic topology learner. A persistent store must add transactional
  compare-and-swap. The coordinator's bounded operator-authorized root successor
  remains separate.
- Existing Pathway events do not contain paired shallow/deep outcome and cost data,
  so they cannot yet calibrate causal marginal value.
- A proposal digest identifies one exact trace artifact. It is not a Sema semantic
  identity: equivalent concepts found by independent runs will generally have
  different proposal hashes, and convergence/synonymy remains an open test.

Run the deterministic demonstration with:

```bash
PYTHONPATH=src python3 examples/demo_conceptual_decomposition.py
```
