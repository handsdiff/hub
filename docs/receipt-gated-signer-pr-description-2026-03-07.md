# PR: Add receipt-gated continuity signer evaluator (new-tier rollout)

## Summary
This PR adds the first production-ready receipt-gated signer path for a **single signer / single tier** rollout:
- `policy_type = continuity_budget`
- `trust_tier = new`

The goal is to make low-risk continuity spend enforceable without relying on narrative review after the fact. The signer now validates approval-critical intent fields before signing, binds receipts back to the approved intent and live policy after execution, and fails closed on unknown policy metrics at startup.

## Scope
### In scope
- Compact `evaluator.ts` for continuity/new-tier policy enforcement
- Full approval-surface intent hashing
- Approved-intent → live-policy binding checks
- Receipt → approved-intent / policy binding checks
- Receipt action-equivalence checks for execution-critical fields
- Non-rejecting freeze evaluator (`allow | freeze` only)
- Final precedence: `freeze > reject > manual_review > allow`
- Startup metric-coverage lint (`lintPolicyMetricCoverage`) with hard failure on unknown rule metrics
- Metric parity CI coverage for derived metrics, runtime metrics, and rule-referenced metrics

### Out of scope
- Multi-tier rollout (`calibrated`, `proven`)
- `performance_budget` rollout
- Dynamic cap adjustment / month-end decision engine
- UI / dashboarding
- Multi-signer orchestration
- Historical migration of old receipts/intents

## Guarantees enforced
- **Intent integrity:** approval-critical fields are hashed and verified before sign
- **Policy integrity:** approved intent must match the currently loaded policy id/version/hash
- **Receipt integrity:** receipt must bind to the approved intent and live policy
- **Action equivalence:** receipt execution fields must match approved intent on:
  - `budget_type`
  - `wallet_id`
  - `spend_class`
  - `counterparty_or_protocol`
  - `amount_usd`
- **Fail-safe freeze behavior:** freeze path never silently downgrades to reject logic
- **Fail-closed policy loading:** unknown rule metrics prevent signer startup

## Test plan
### Unit tests
- `verifyIntentHash` passes for unchanged canonical intent
- `verifyIntentHash` fails if any approval-critical field mutates after hashing
- `verifyReceiptBinding` passes for matching receipt / intent / policy
- `verifyReceiptBinding` fails on:
  - intent hash mismatch
  - policy id/version/hash mismatch
  - receipt execution-field drift
- `evaluatePreSign` returns:
  - `allow`
  - `manual_review`
  - `reject`
- `evaluateFreeze` returns:
  - `allow`
  - `freeze`
- `evaluateFinalDecision` enforces precedence:
  - `freeze > reject > manual_review > allow`

### Golden tests
- continuity/new-tier autosign case
- continuity/new-tier manual-review case
- continuity/new-tier reject case
- continuity/new-tier freeze case

### Mutation tests
- mutate `spend_class` after hashing → intent hash verification fails
- mutate receipt `counterparty_or_protocol` vs approved intent → receipt binding fails

### CI parity
- `SUPPORTED_METRICS = DERIVED_METRICS ∪ RUNTIME_METRICS`
- every metric emitted by `buildPreSignMetrics()` is declared in `DERIVED_METRICS`
- every declared derived metric is actually emitted
- every rule-referenced `metric` / `metric_ref` is present in `SUPPORTED_METRICS`
- runtime-only metrics are explicitly allowlisted (not implied)

## Acceptance criteria
- Unknown rule metric => signer refuses startup
- Intent hash mismatch => pre-sign reject
- Approved-intent / live-policy mismatch => reject
- Receipt / approved-intent mismatch => reject
- Receipt / live-policy mismatch => reject
- Freeze rule hit => final decision is `freeze` regardless of pre-sign outcome
- Manual-review band is real (`manual_review_threshold_usd < single_action_max_usd`)
- Metrics parity CI passes

## Rollback plan
If this rollout misbehaves:
1. Disable signer startup on the new continuity policy
2. Revert to manual approval for continuity spend
3. Keep receipt logging enabled for postmortem evidence
4. Preserve frozen state if a freeze triggered; do not auto-unfreeze during rollback
5. Revert PR or feature-flag the evaluator path off

Safe rollback condition: no funds move through the automated continuity signer until policy load, pre-sign checks, and freeze path all pass cleanly.
