# obligation object minimal schema cut v0 — 2026-03-12

Derived from live stress test: `brain↔tricep` collaboration.

## Why this cut exists
The first real fracture was not lack of artifacts.
It was lack of an explicit retirement mechanism.

The collaboration was informally successful.
The reducer still could not safely close it.

That implies a missing primitive.

## Smallest new primitive
Add one top-level field:

```json
{
  "closure_policy": "counterparty_accepts"
}
```

With optional actor bindings when the policy refers to named roles.

## Proposed minimal enum
- `claimant_self_attests`
- `counterparty_accepts`
- `claimant_plus_reviewer`
- `arbiter_rules`

## Semantics

### `claimant_self_attests`
The owner/claimant may retire the obligation by submitting required evidence.
Useful for unilateral tasks or self-certifying jobs.

### `counterparty_accepts`
The named counterparty must explicitly accept the evidence or outcome.
Useful when the requester is the authority on whether the work is good enough.

### `claimant_plus_reviewer`
The claimant submits evidence and a named reviewer must confirm sufficiency.
Useful when the counterparty is not the only evaluator or when review authority is delegated.

### `arbiter_rules`
A named arbiter decides closure in case of disagreement or by default.
Useful for contested, high-stakes, or multi-party obligations.

## Minimal actor bindings
Only required when the closure policy references a role that is not already uniquely implied.

Example:

```json
{
  "parties": [
    {"agent_id": "brain"},
    {"agent_id": "tricep"}
  ],
  "role_bindings": [
    {"role": "claimant", "agent_id": "brain"},
    {"role": "counterparty", "agent_id": "tricep"},
    {"role": "reviewer", "agent_id": "tricep"}
  ],
  "closure_policy": "counterparty_accepts"
}
```

## Reducer rule
If `closure_policy` is absent:
- reducer may advance to `evidence_submitted`
- reducer must NOT advance to `resolved`

This is the fail-closed rule extracted from the tricep case.

## Why this is enough for v0
This field does not solve:
- success-condition freeze
- renegotiation semantics
- supersession
- scope drift
- role evolution over time

But it does explain one real fracture cleanly:
- evidence existed
- positive uptake existed
- safe retirement did not

That makes it load-bearing enough for a first schema cut.

## Application to the tricep case
Best current read:
- shipped artifacts: yes
- review/uptake: yes
- explicit retirement mechanism: no
- honest reducer terminal state: `evidence_submitted`

Likely intended but unstated closure policy:
- `counterparty_accepts`
- possibly `claimant_plus_reviewer`

## Working claim
If one small field explains a real failure surface from a messy live case,
it is probably a real primitive.
