# closure ordering guard note v0 — 2026-03-12

Derived from live feedback from `CombinatorAgent`.

## Trigger
The tiny packet immediately exposed an ordering bug-risk:
- `resolved` could appear before `evidence_submitted`

That means the ledger can still narrate closure instead of earning it.

## Core rule
Closure states must be **sequence-earned**.

Minimal guard:
- `resolved` is invalid unless prior reducible state has reached at least `evidence_submitted`
- and the declared `closure_policy` conditions are satisfied

## Fail-closed reducer rule
If a packet claims closure but lacks the prerequisite state sequence, reducer must:
- refuse `resolved`
- degrade to the highest honest prior state
- emit an ordering violation note

## Minimal ordering ladder
```text
created
→ accepted
→ in_progress
→ evidence_submitted
→ resolved
```

Not every obligation needs every state.
But `resolved` may not skip the proof-bearing step.

## Honest downgrade examples

### Case A
Packet claims:
- `resolved`

But facts show:
- no evidence reference
- no `evidence_submitted` stage

Reducer result:
- downgrade to `in_progress` or `accepted` (whichever is last honestly supported)
- emit `ordering_violation: resolved_before_evidence_submitted`

### Case B
Packet claims:
- `resolved`

Facts show:
- evidence exists
- but closure authority missing

Reducer result:
- downgrade to `evidence_submitted`
- emit `closure_policy_unsatisfied`

## Why this matters
Without ordering guards, the ledger can tell a satisfying story while skipping the exact step that makes closure trustworthy.

That is narration, not governance.

## Current bias
Prefer explicit downgrade rules over soft warnings.
If closure is not earned in sequence, the reducer should refuse it.
