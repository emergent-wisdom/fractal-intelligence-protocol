# Verification and TEEs

## What is payable now

The alpha automatically pays only results checked by objective, deterministic
clauses. Code tasks with tests, exact transformations, proof checking, schema
conformance, and reproducible data calculations are natural first markets.

Expected answers and private thresholds are redacted from node leases by default.
Executors receive the public shape of the acceptance contract, while the
coordinator evaluates against the complete clause. Production code evaluation
should similarly keep hidden tests in an isolated verifier rather than sending
them to the worker.

Hidden clause outcomes are also withheld after rejection, and a rejected node
cannot re-lease the same task. This prevents simple adaptive answer probing by one
identity. It does not solve collusion or Sybil identities; invite-only admission
is the alpha's explicit trust boundary.

Every Solver offering also requires administrator admission before it can receive
inputs. For recursively proposed work, the proposing node is excluded from
executing its own children by default, because it may know their hidden expected
values. An administrator can explicitly waive that separation for trusted local
workflows, but the waiver is visible in the delegation record.

Retained-artifact envelopes omit hidden acceptance clauses and gate traces, but
their inputs, constraints, outputs, and evidence receive no further redaction.
Reframing authorizes disclosure to any admitted provider that may lease or
re-lease the successor root's required capability; there is no per-binding
recipient access list or artifact deletion/retention API. The alpha's prohibition
on sensitive workloads still applies.

The coordinator privately commits the full source contract and gate for audit,
but does not expose those digests to providers: low-entropy hidden expected values
could otherwise be guessed offline. Provider-visible acceptance remains a
coordinator assertion linked to the immutable source receipt, not independently
reproducible proof.

Open-ended research, strategy, and creative work require customer or expert
acceptance. A model judge can assist, but treating it as ground truth introduces
collusion, sycophancy, Goodhart effects, and false rejection risk.

## What a TEE can prove

A trusted execution environment can attest that an approved verifier or model
container ran with a particular configuration, and it can protect confidential
inputs or model weights. That improves execution integrity and auditability.

It cannot prove that an open-ended answer is true, useful, or aligned with the
customer's unstated intent. The acceptance criterion still needs objective tests,
independent evaluation, or human judgment.

## Appropriate later use

Add attested execution only after the ordinary gate produces value. A useful
receipt would bind:

```text
Task + AcceptSpec + Result + GateDecision + verifier measurement + attestation
```

This could support confidential deterministic verifiers, reproducible inference,
or disputes about which code ran. It should not be marketed as proof of semantic
correctness.

Bond-style onchain execution histories are useful as auditable evidence scoped
to a task class. They are not a universal reputation score and do not replace the
coordinator's acceptance policy.

## External references checked 2026-07-17

- [Bond.credit report](https://www.bond.credit/report)
- [iExec confidential-computing documentation](https://docs.iex.ec/)
- [EigenAI whitepaper](https://docs.eigencloud.xyz/assets/files/EigenAI_Whitepaper-f1c89ddb88c1e28ccadff250523a273c.pdf)
