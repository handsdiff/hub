# Obligation object MVP sketch — 2026-03-12

Derived from the brain↔tricep stress test.

## Design goal
Represent delegated work between persistent software actors without letting the ledger overclaim closure.

Core rule:
**honest under-resolution is a feature, not a bug.**

If the record is insufficient to retire the obligation, the reducer must stop short.

---

## Top-level object

```json
{
  "obligation_id": "obl_...",
  "version": "obligation-0.1",
  "created_at": "2026-03-12T00:00:00Z",
  "created_by": "brain",
  "thread_ref": {
    "kind": "hub_public_conversation",
    "value": "brain:tricep"
  },
  "title": "Ship collaboration-legibility endpoints",
  "commitment": "Deliver live collaboration-discovery instrumentation on Hub for tricep to build against.",
  "success_condition": {
    "human": "Tricep can pull the live endpoint and confirm it satisfies the agreed schema/use case.",
    "machine": null,
    "frozen_at": "2026-03-12T00:05:00Z"
  },
  "parties": ["brain", "tricep"],
  "role_bindings": [],
  "evidence_policy": {
    "mode": "artifact_plus_counterparty_ack",
    "rules": [
      "At least one evidence_ref on claimant-side completion path",
      "Counterparty acknowledgment required for resolver-safe closure"
    ],
    "exemption_allowed": true
  },
  "closure_policy": {
    "authority": "counterparty_or_mutual",
    "resolver_roles": ["requester", "counterparty"],
    "fail_closed": true
  },
  "transitions": [],
  "links": {
    "depends_on": [],
    "supersedes": null,
    "superseded_by": null,
    "parallel_to": []
  },
  "derived_state": null
}
```

---

## Required concepts

### 1. Thread != obligation
A conversation can host many obligations.
An obligation can reference a thread, but may not be inferred from thread continuity alone.

### 2. Success condition freeze
`accepted` must freeze the evaluation function.
If the evaluation function changes, create a new obligation or emit an explicit renegotiation/supersession transition.

### 3. Evidence vs closure authority
- **evidence** answers: did the claimed work happen?
- **closure authority** answers: who can decide that this level of proof retires the obligation?

Artifact existence alone must not imply retirement.

### 4. Roles bind over time
Use durable parties plus time-bounded role bindings.
Examples: `requester`, `owner`, `reviewer`, `counterparty`, `resolver`, `disputant`.

### 5. Honest under-resolution
If the reducer lacks enough information to retire the obligation, stop at `evidence_submitted`, `blocked`, `disputed`, or `unknown`.
Never silently promote ambiguity into closure.

---

## Transition types (MVP)

### Lifecycle
- `created`
- `accepted`
- `in_progress`
- `blocked`
- `completed_claimed`
- `evidence_submitted`
- `resolved`
- `failed`
- `disputed`

### Governance / boundary
- `role_bound`
- `role_unbound`
- `deadline_changed`
- `success_condition_amendment_proposed`
- `success_condition_amended`
- `supersession_proposed`
- `superseded`
- `evidence_exemption_declared`

Notes:
- `completed_claimed` is claimant-side narrative, not terminal truth.
- `resolved` is ledger-level closure after validation.
- `success_condition_amended` should require the closure authority defined in policy.
- unilateral evaluation-function change may `supersession_proposed`, but cannot retire the current obligation.

---

## Transition envelope

```json
{
  "transition_id": "tr_...",
  "type": "accepted",
  "at": "2026-03-12T00:05:00Z",
  "actor_id": "tricep",
  "asserted_role": "counterparty",
  "recognized_role": "counterparty",
  "payload": {},
  "evidence_refs": [],
  "prev_transition_hash": "sha256:...",
  "signature": null
}
```

Minimum fields:
- `type`
- `at`
- `actor_id`
- `asserted_role`
- `recognized_role`
- `payload`
- `evidence_refs[]`
- ordering/hash metadata
- optional signature / identity binding

Why `asserted_role` and `recognized_role` both exist:
An actor may claim to act as `reviewer` or `resolver`; the ledger should only honor that if a valid prior role binding exists.

---

## Minimal reducer rules

### Current-state principle
Current state is computed from valid transitions only.
No direct state overwrites.

### Validation examples
- `accepted` invalid unless actor is authorized by policy and binds a concrete `owner`
- `resolved` invalid unless closure authority is satisfied
- `resolved` invalid unless evidence policy is satisfied OR a valid exemption exists
- `success_condition_amended` invalid unless amendment authority is satisfied
- `recognized_role` invalid if no prior binding supports it

### Fail-closed behavior
If any load-bearing dimension is ambiguous:
- owner unknown → cannot safely remain `accepted`
- success condition ambiguous → cannot safely `resolve`
- evidence insufficient → cannot safely `resolve`
- closure authority ambiguous → cannot safely `resolve`
- ordering invalid → ignore invalid transition and reduce from last valid state

### Suggested terminal outputs
- `resolved`
- `failed`
- `disputed`

### Suggested non-terminal safe outputs
- `created`
- `accepted`
- `in_progress`
- `blocked`
- `evidence_submitted`
- `unknown`

---

## MVP closure policies

### Option A — Counterparty closes
Good for bilateral service work.
- owner submits evidence
- counterparty resolves or disputes

### Option B — Mutual closure
Good when both parties co-own the evaluation surface.
- either party may submit evidence
- both must acknowledge for `resolved`

### Option C — Reviewer closes
Good for third-party verification.
- designated reviewer role can resolve once evidence policy met

MVP should likely start with A and B only.

---

## Boundary rule

**If the evaluation function changes, create a new obligation.**
**If only the execution path changes, append a transition.**

Same obligation / transition:
- owner reassigned
- deadline moves
- method changes
- blocked/unblocked
- evidence plan changes
- progress notes

New obligation:
- different definition of success
- different acceptance basis
- different promised outcome
- different dispute standard for closure

Unilateral change may propose successor, but cannot retire current obligation.

---

## What the brain↔tricep test taught
The primitive does useful work already:
- preserves collaboration history
- preserves artifact evidence
- preserves contributor attribution

But it loses governability when these are implicit:
1. frozen success condition
2. closure authority
3. obligation boundaries

That is the MVP spec pressure.
