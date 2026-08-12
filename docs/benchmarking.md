# Matched-budget experimental harness

`MatchedBudgetHarness` is an offline, deterministic experiment recorder for the
paper's decisive comparison. It does not use the paid scheduler or economic
ledger. Benchmark records are observational and cannot create provider payables.

## Required comparison arms

The default harness requires the five conditions described in the paper:

1. `frontier_model` — a single model using the trial budget.
2. `role_based_team` — conventional task/role agents.
3. `conceptual_decomposition` — concept-first decomposition and typed boundaries.
4. `scrambled_decomposition` — the same machinery with concept/work alignment
   deliberately scrambled. A valid adapter must preserve prompts, topology,
   machinery, and budget while applying a preregistered concept-to-work
   permutation.
5. `role_based_team_with_typed_gates` — the same role-based team upgraded with
   typed gates while retaining the baseline topology and prompts.

Requiring both ablations helps distinguish gains from conceptual carving from
gains caused by orchestration or typed isolation alone.

The harness verifies the five arm labels, not their scientific fidelity. Strategy
implementers remain responsible for showing that each adapter really instantiates
the declared architecture and that the two ablations change only their intended
variable.

## What the harness enforces

Every arm receives the same per-trial limits for:

- model calls,
- input tokens,
- visible output tokens,
- hidden reasoning tokens, and
- monetary micro-units.

Each report also commits to a currency, pricing-snapshot digest, tokenizer id,
dataset digest, evaluator digest, model identity, and prompt digest. The harness
validates digest form and incorporates those commitments into the report hash; it
cannot prove that a caller derived a commitment from the claimed private asset.

`BudgetMeter.reserve()` claims a maximum entitlement before an inference call and
`settle()` accepts the complete authoritative usage afterward. An over-reservation,
provider overrun, incomplete usage record, or unsettled reservation invalidates the
trial. Planning, decomposition, routing probes, gates, retries, re-carves, and
synthesis must all be charged by the strategy adapter.

The meter's state transitions are lock-protected for parallel branches. Trial
closure atomically seals the meter, preventing retained background workers from
adding late usage after evaluation. If a provider exceeds its reservation, the
trial fails but the authoritative actual spend is still recorded and included in
the aggregate rather than disappearing from cost accounting.

Arm order is deterministically randomized per case. Strategy identities include
the implementation, version, model, and prompt digest. The evaluator receives
only the frozen final JSON output—not the arm name or trace. Reports include every
trial, usage event, error, objective evaluation, aggregate, execution order, seed,
and a canonical SHA-256 digest. A scripted run is byte-reproducible.
Primary mean score and pass rate use every assigned trial, counting budget,
strategy, validation, and evaluator failures as zero. Completed-only values are
reported separately as diagnostics so fragile arms cannot improve their headline
metric by failing on difficult cases.

## Trust boundary

The harness can prevent a cooperating adapter from spending beyond its declared
entitlement. It cannot detect a strategy that secretly calls a model outside the
passed meter. A credible production experiment must isolate model/network access
and use an adapter that reports complete input, visible-output, and reasoning-token
usage. If a provider cannot expose complete usage, describe the result as a
matched declared-budget experiment—not matched compute.

Token totals are comparable only under the recorded tokenizer/accounting policy;
different providers may expose reasoning and cached tokens differently. Likewise,
the in-process API hides arm identity from the evaluator but is not process-level
isolation: stateful strategies or evaluators could still communicate through
shared Python state. Credible runs should isolate them in separate processes or
workers.

Private expected answers belong inside each case evaluator and must never enter a
strategy's `public_input`. Objective proof, code, and exact-JSON tasks should use a
trusted deterministic evaluator. Open-ended outputs require a blind arbiter and a
separate reliability study.

## What this does not establish

The harness is the experimental accounting substrate. Scripted examples test its
invariants, not Fractal Intelligence's performance. A real result still needs a
preregistered held-out dataset, multiple models and seeds, paired case-level
analysis, complete usage, and implementations of all five arms.

The sample should span both tacit/thin-expertise problems and problems whose
reasoning is already written down. The paper predicts gains should concentrate in
the first regime; a uniform gain everywhere would count against that distinctive
claim rather than automatically supporting it.

It does not yet test persistent concept reuse, graph learning, decentralized
execution, recursive Frame Error re-carving, or whether independent decomposition
seeds converge on the same concepts.

Run the deterministic harness demonstration with:

```bash
PYTHONPATH=src python3 examples/demo_matched_budget_harness.py
```
