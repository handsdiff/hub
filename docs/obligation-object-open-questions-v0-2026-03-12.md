# obligation object open questions v0 — 2026-03-12

Derived from:
- `hub/docs/brain-tricep-obligation-stress-test-2026-03-12.md`
- `hub/docs/obligation-object-minimal-schema-cut-v0-2026-03-12.md`

## Locked for MVP core
These now look load-bearing enough to treat as core:
- append-only obligation object
- explicit lifecycle states
- fail-closed reduction
- `closure_policy`
- evidence pointers
- role bindings

## Still open

### 1. What freezes at `accepted`?
We know implicit success conditions fail.
But the minimum frozen payload is still unclear.

Candidates:
- `success_condition_text`
- `evidence_policy`
- `closure_policy`
- `deadline` or `review_window`
- `owner` / `counterparty`

Open question:
What is the smallest accepted-state payload that makes later reduction honest without turning creation into contract law?

### 2. What is the minimum renegotiation surface?
We have a likely rule:
- evaluation function changes → new obligation
- execution path changes → transition

Still unclear:
What exact transition types are enough to distinguish:
- amendment
- supersession
- parallel derived work
- pure implementation update

### 3. How much role history is enough?
A static owner/counterparty model is too weak.
But full event-sourced role history may be overkill for v0.

Open question:
Is `role_bindings[]` at creation + explicit role-change transitions enough?

### 4. Can closure policy stay tiny?
Current candidate enum:
- `claimant_self_attests`
- `counterparty_accepts`
- `claimant_plus_reviewer`
- `arbiter_rules`

Open question:
Does this survive a second messy live case without exploding?

### 5. What counts as evidence pointer in v0?
Current documents treat evidence as URLs / artifacts / chat acknowledgments.
That may be enough for reduction, but it may be too loose for implementation.

Open question:
Do we need a typed evidence object in v0, or is `evidence_refs[]` enough?

## Working heuristic
If an open question cannot be grounded in a messy real case,
do not solve it yet.

## Next useful tests
1. Run the schema cut against a second live collaboration.
2. Try one case with an explicit `closure_policy` from the start.
3. See whether the tiny enum survives without adding adjudication complexity too early.

## Current bias
Prefer:
- conservative under-resolution
- tiny top-level fields
- explicit transitions
- multiple obligations per thread

Avoid:
- thread-level magical inference
- silent resolution from positive vibes
- rich schema before second live test
