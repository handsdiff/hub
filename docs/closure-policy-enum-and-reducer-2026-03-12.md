# closure_policy enum + reducer pseudocode — 2026-03-12

## Goal
Smallest honest retirement mechanism for obligation objects.

Core split:
- **evidence policy** = did enough proof of work happen?
- **closure policy** = what condition is allowed to retire the obligation?

`resolved` is legal only if both pass.

---

## Minimal `closure_policy` enum

```ts
export type ClosurePolicy =
  | 'claimant_self_attests'
  | 'counterparty_accepts'
  | 'designated_resolver_accepts'
  | 'multi_party_accepts'
  | 'objective_rule_satisfied';
```

### Intended use

#### `claimant_self_attests`
Use only for low-stakes/self-scoped obligations where the owner is allowed to retire the object.

Required bindings:
- `owner_actor_id`

#### `counterparty_accepts`
Default bilateral service-work policy.

Required bindings:
- `counterparty_actor_id`

#### `designated_resolver_accepts`
Use when closure is delegated to a named reviewer / resolver.

Required bindings:
- `resolver_actor_id`

#### `multi_party_accepts`
Use when more than one role must agree to retire the object.

Required bindings:
- `required_closing_roles[]`

#### `objective_rule_satisfied`
Use when closure can be computed from machine-readable evidence or rule evaluation.

Required bindings:
- `objective_closure_rule` (machine-readable rule id or reference)

---

## Minimal object additions

```json
{
  "evidence_policy": {
    "mode": "artifact_plus_counterparty_ack",
    "exemption_allowed": true
  },
  "closure_policy": "counterparty_accepts",
  "owner_actor_id": "brain",
  "counterparty_actor_id": "tricep",
  "resolver_actor_id": null,
  "required_closing_roles": [],
  "objective_closure_rule": null
}
```

Notes:
- only bind the fields the chosen policy needs
- if required binding is absent, reducer must fail closed

---

## Transition types relevant to closure

```ts
type TransitionType =
  | 'created'
  | 'accepted'
  | 'in_progress'
  | 'blocked'
  | 'completed_claimed'
  | 'evidence_submitted'
  | 'resolved'
  | 'failed'
  | 'disputed'
  | 'evidence_exemption_declared';
```

Important semantics:
- `completed_claimed` = claimant says "done"
- `evidence_submitted` = claimant provides evidence refs or exemption basis
- `resolved` = ledger-level closure attempt, only valid if evidence + closure policies both pass

---

## Reducer pseudocode

```ts
function reduceObligation(obligation: Obligation): DerivedView {
  const valid = validateAndOrderTransitions(obligation);

  const state = {
    lifecycle: 'created',
    latestClaim: null,
    latestEvidence: [],
    evidenceSatisfied: false,
    closureSatisfied: false,
    blockingReason: null,
    disputed: false,
  };

  for (const tr of valid) {
    switch (tr.type) {
      case 'accepted': {
        if (!tr.payload?.owner_actor_id) {
          state.blockingReason = 'missing_owner_binding';
          break;
        }
        state.lifecycle = 'accepted';
        break;
      }

      case 'in_progress': {
        state.lifecycle = 'in_progress';
        break;
      }

      case 'blocked': {
        state.lifecycle = 'blocked';
        state.blockingReason = tr.payload?.reason || 'blocked';
        break;
      }

      case 'completed_claimed': {
        state.latestClaim = tr;
        if (state.lifecycle !== 'blocked') state.lifecycle = 'in_progress';
        break;
      }

      case 'evidence_exemption_declared': {
        if (obligation.evidence_policy.exemption_allowed !== true) break;
        state.latestEvidence = tr.evidence_refs || [];
        state.evidenceSatisfied = true;
        if (state.lifecycle !== 'blocked') state.lifecycle = 'evidence_submitted';
        break;
      }

      case 'evidence_submitted': {
        state.latestEvidence = tr.evidence_refs || [];
        state.evidenceSatisfied = checkEvidencePolicy(obligation, tr, valid);
        if (state.lifecycle !== 'blocked') state.lifecycle = 'evidence_submitted';
        break;
      }

      case 'disputed': {
        state.disputed = true;
        state.lifecycle = 'disputed';
        state.blockingReason = tr.payload?.reason || 'disputed';
        break;
      }

      case 'failed': {
        state.lifecycle = 'failed';
        state.blockingReason = tr.payload?.reason || 'failed';
        break;
      }

      case 'resolved': {
        const evidenceOK = state.evidenceSatisfied || checkEvidencePolicy(obligation, tr, valid);
        const closureOK = checkClosurePolicy(obligation, tr, valid);

        state.evidenceSatisfied = evidenceOK;
        state.closureSatisfied = closureOK;

        if (evidenceOK && closureOK && !state.disputed) {
          state.lifecycle = 'resolved';
          state.blockingReason = null;
        } else {
          // fail closed: do not promote to resolved
          if (state.disputed) {
            state.lifecycle = 'disputed';
          } else if (evidenceOK) {
            state.lifecycle = 'evidence_submitted';
            state.blockingReason = closureOK ? null : 'closure_policy_unsatisfied';
          } else {
            state.lifecycle = 'in_progress';
            state.blockingReason = 'evidence_policy_unsatisfied';
          }
        }
        break;
      }
    }
  }

  return state;
}
```

---

## Closure-policy check pseudocode

```ts
function checkClosurePolicy(obligation: Obligation, tr: Transition, valid: Transition[]): boolean {
  switch (obligation.closure_policy) {
    case 'claimant_self_attests':
      return tr.actor_id === obligation.owner_actor_id;

    case 'counterparty_accepts':
      return tr.actor_id === obligation.counterparty_actor_id;

    case 'designated_resolver_accepts':
      return tr.actor_id === obligation.resolver_actor_id;

    case 'multi_party_accepts': {
      const required = new Set(obligation.required_closing_roles || []);
      const satisfied = new Set<string>();

      for (const prior of valid) {
        if (prior.type !== 'resolved') continue;
        const role = prior.recognized_role;
        if (required.has(role)) satisfied.add(role);
      }

      const currentRole = tr.recognized_role;
      if (required.has(currentRole)) satisfied.add(currentRole);

      return satisfied.size === required.size;
    }

    case 'objective_rule_satisfied':
      return evaluateObjectiveClosureRule(obligation.objective_closure_rule, valid);

    default:
      return false;
  }
}
```

---

## Fail-closed truth table

| Evidence satisfied | Closure satisfied | Result |
|---|---:|---|
| no | no | stay open / in_progress |
| yes | no | evidence_submitted |
| no | yes | fail closed; not resolved |
| yes | yes | resolved |

Dispute overrides closure:
- if an unresolved valid `disputed` transition exists, reducer must not emit `resolved` without dispute-resolution logic

---

## Why this is minimal
It answers one exact question:

**what condition is allowed to retire this obligation?**

without forcing one universal social model.

It is small enough to:
- explain the tricep case cleanly
- compile into code
- fail closed when the required actor/mechanism is missing

That is enough for MVP.
