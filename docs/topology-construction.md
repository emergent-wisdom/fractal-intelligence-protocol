# Mandatory-abstraction topology construction

This experimental planning profile adds the motion that the original
conceptual-decomposition engine did not perform: a concrete problem must first
be located upward in a reusable hierarchy before it is decomposed downward.
It is a review-only research layer. It does not change protocol-v1 transport,
provider admission, deterministic acceptance, or payment authority.

The upward-and-downward motion is intended to change the search space, not merely
to file a problem under a category. A higher frame can make different dimensions
available for decomposition; composition gives those dimensions separate
reasoning channels; synthesis can combine the mechanisms they expose. The result
may be a candidate solution that was not salient under the original task framing.
The profile records this hypothesized generative path for evaluation but does not
treat a structurally accepted carve as proof of novelty or quality.

## The complete motion

For each problem presented to this profile:

1. Identify the most specific reusable capability that frames the problem.
   Record why the concrete problem is an instance of that exact immutable
   capability and what becomes visible when it is framed there.
2. Follow adjacent `specialization` relationships upward until the configured
   `RootSolver` is reached.
3. At every step, record the parent genus; why each observed child belongs under
   it; each child's differentia, reframing effect, and contract fit; whether a
   more informative intermediate parent was omitted; and how existing or
   potential siblings were searched.
4. Traverse downward through the exact reverse sequence. A shorter or alternate
   route is not an equivalent record.
5. At the specific composite, apply the existing four-test algorithm to ask what
   the capability is made of.
6. Bind every accepted dimension to one immutable concept node through a
   `composition` edge.
7. Declare how those child contributions integrate into the outward capability
   visible at the parent boundary.

The two edge types are deliberately different:

- `specialization`: the child is a kind or realization of the parent. These
  nodes provide the abstraction route and may be passive membranes.
- `composition`: the child supplies one differentiated contribution required to
  realize the parent's outward capability. These children may become executable
  work after review.

Only a `composite` node may own composition edges, and only a `solver` or another
`composite` may be a composition child. `passive` and `abstraction_parent` nodes
are routing structure and cannot be compiled into work.

Routing through an abstraction node is not itself a model call. The execution
compiler materializes only reviewed composition work.

`parent_synthesis` is the local emergence, or membrane, contract. Composition
children can act independently in their parent-local edge roles; synthesis turns
their contributions into the single outward capability promised by the parent.
A case cannot replace that promise: its outward capability must equal the
immutable subject node's `produces` field, while its integration rationale
explains how the exact active children realize it.
A composite may itself serve as a child of another composite, so this pattern is
recursively representable. Schema v1 compiles only one direct composition level
at a time and does not by itself prove that useful emergence occurred.

## MECE and the Aristotelian criterion

The four-test decomposition artifact remains authoritative for constitutive
children:

- **Necessity**: removing the dimension breaks the parent capability.
- **Independence**: the dimension can vary without merely duplicating another.
- **Universality**: every instance of the parent contains the dimension.
- **Completeness**: the accepted set spans what the parent must resolve.

Independence approximates non-overlap and completeness approximates coverage,
supplying MECE-style discipline without turning semantic judgments into a formal
proof. The upward
classification uses the Aristotelian genus-and-differentia form: identify the
more general kind shared with siblings, then state what differentiates the
selected child. Universality asks whether each constitutive dimension belongs to
every instance of its parent. The topology proposal embeds the existing
content-addressed four-test record rather than reimplementing those judgments.

Specialization is open-world in schema version 1. Its sibling assessments use
genus, differentia, and observed-sibling independence, but cannot claim that all
possible kinds have been enumerated. New evidence may introduce another sibling
or a more informative intermediate parent. This avoids confusing a complete
constitutive carve with an exhaustive taxonomy of future problem kinds.

The objective is not to maximize child count. It is to find the smallest
non-redundant set that satisfies completeness. The validator floor of two
constituents prevents a composite from merely renaming itself; marginal-value
decisions inside the decomposition still prevent gratuitous deeper branching.

## Immutable graph and multi-parent reuse

A concept node is the digest of one exact contract:

```text
name, description, kind, accepts, produces, boundary, excludes, supersedes
```

`node_ref` identifies those exact bytes, not an objectively unique real-world
concept. Because schema v1 includes `supersedes`, it identifies a history-bearing
version artifact: identical capability text with different ancestry has different
references. Sema remains the intended owner of cross-run semantic identity; a
future profile may separate a stable contract reference from revision lineage.

Relationship meaning and parent-local role live on the edge. Consequently, one
unchanged node may serve several parents and contribute differently in each
composition. Reuse does not copy or silently widen its contract.

If later evidence shows that a boundary is wrong, a topology patch creates a new
node whose `supersedes` list cites the previous contract. The old contract is
never edited in place. Generalization, splitting, insertion of a shared parent,
and removal of obsolete shortcuts are auditable combinations of four operations:

- `add_node`
- `add_edge`
- `retire_edge`
- `retire_node`

The pure transition applies all operations or returns an error against one exact
base snapshot. Added edges must follow their added nodes, and a node can be
retired only after its incident edges. The result must still be a rooted acyclic
graph in which every active node is reachable from `RootSolver`. A persistent
store must perform a transactional compare-and-swap on the current snapshot
digest; this in-memory function cannot prevent two writers from racing.

## Artifacts and review

`fractal_protocol.topology` provides three content-addressed artifacts:

| Artifact | Purpose |
|---|---|
| `fi_concept_graph_snapshot` | One immutable active graph with exactly one root |
| `fi_topology_patch` | A review-required structural delta against an exact snapshot |
| `fi_case_proposal` | Problem and exact subject framing, patch, ascent, reverse route, four-test record, bindings, synthesis, provenance, and declared usage |

`TopologyConstructionEngine` calls one injected `TopologyOracle.propose_case`
method. This broad boundary intentionally leaves the model free to reason about
the whole carve. Deterministic code validates graph mechanics and artifact
consistency; it does not pretend to decide whether an abstraction is insightful.
An adapter may internally use `ConceptualDecompositionEngine` to construct the
embedded four-test proposal.

The schema enforces an upward-first public record, not the oracle's hidden order
of thought. The required intermediate-parent and sibling-search rationales make
flattening decisions reviewable, but they cannot prove that the search was good.

Only concise public rationales are retained. Extra oracle fields—including a
private reasoning field—are not copied into the case artifact.

Applying a case requires an explicit review that approves the exact
`case_proposal_digest`. The resulting snapshot must equal the proposal's committed
post-patch digest. A content digest establishes identity, not reviewer authority
or semantic correctness. The review object is an unauthenticated application
assertion, and the returned snapshot alone is not a transition receipt. A trusted
application must authenticate the reviewer and retain the base snapshot, case,
review, and resulting snapshot together.

## Execution boundary

`execution_plan_from_fi_case` accepts only:

- the exact base snapshot;
- the exact active post-patch snapshot;
- an approved review of the exact case digest;
- one operational binding for every reviewed composition work item; and
- an operational synthesis schema, gate, and problem-local context.

The caller supplies problem-local operational objectives and contexts plus output
schemas and hard `AcceptSpec`s. The compiler injects the exact parent, child,
edge, and decomposition-axis contracts into every work item. It also injects the
problem framing, full Root-to-subject route, and ascent assessments, so the
mandatory abstraction can influence child and synthesis reasoning rather than
remaining audit metadata. The compiler uses the reviewed edge-specific role and
derives the conceptual synthesis objective and context from the reviewed parent
synthesis. It owns work identity, dependencies, and topology provenance, so a
caller cannot turn a passive specialization node into work or change the
reviewed route. The result is the existing backend-neutral `ExecutionPlan`; all
existing schema, gate, usage, and record validation continues to apply.

Case review covers the topology, conceptual contracts, route, four-test evidence,
bindings, and parent synthesis. It does not approve the later operational
objectives, contexts, schemas, or gates. An authoritative application must review
the final content-addressed execution plan separately.

This planning review does not admit a Solver manifest, create a coordinator task,
approve a delegation, or authorize a payable. Network materialization still
requires the existing live lease, immutable admitted manifest, inherited
constraints, budget, administrator approval, and deterministic result gate.

## Current limits

- The module represents and returns snapshots as portable artifacts; it does not
  add a shared graph database or coordinator API.
- A standalone snapshot cannot prove its claimed predecessor or revision lineage;
  those claims are trustworthy only when the application retains and validates
  the complete transition chain.
- Problem-to-subject fit, omitted intermediate abstractions or siblings, semantic
  adjacency, genus, differentia, independence, universality, completeness, and
  contract fit remain oracle estimates plus human or policy review—not proofs.
- The validator requires a configured composite named `RootSolver`, but the
  breadth of that root contract is itself a semantic review responsibility.
- Specialization sibling sets remain open; closed taxonomic partitions are not
  supported in schema version 1.
- The execution compiler materializes the specific subject's direct composition
  children. It records but does not expand deeper recursive decomposition.
- The current case profile represents composite subjects with at least two
  constitutive children. A terminal leaf is executed through the existing
  single-Solver protocol path rather than by inventing a one-child composition.
- `new_version`, `split`, and `shared_parent` are reviewed fit classifications.
  Schema v1 checks their immediate local shape but does not prove that a proposed
  split is exhaustive or that no better shared parent exists.
- There is no production model adapter, semantic deduplication, automatic
  contract repair, learned routing, or outcome-based topology promotion.
- The new 100-problem construction run motivated these invariants but did not
  validate contract revision, independent child execution, or improved solution
  quality.
