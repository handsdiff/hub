# Receipt-Gated Signer — Engineering Handoff (2026-03-07)

## Scope
Ship the compact `evaluator.ts` bundle for **one signer / one tier**:
- `policy_type = continuity_budget`
- `trust_tier = new`

## Required guarantees
- Full approval-surface intent hashing
- Strict `approvedIntent -> live policy` binding
- Strict `receipt -> approvedIntent/policy` binding
- Receipt action-equivalence checks
- Freeze path is fail-safe and non-rejecting (`allow | freeze` only)
- Final precedence: `freeze > reject > manual_review > allow`
- Startup policy metric lint fails closed on unknown rule metrics

## PR checklist
- [ ] Add compact `evaluator.ts`
- [ ] Export `buildPreSignMetrics` or move it into `metrics.ts`
- [ ] Add `metrics.ts` with:
  - [ ] `DERIVED_METRICS`
  - [ ] `RUNTIME_METRICS`
  - [ ] `SUPPORTED_METRICS = union(...)`
- [ ] Add startup `lintPolicyMetricCoverage(policy)` and hard-fail signer boot on unknown rule metrics
- [ ] Add intent integrity helpers:
  - [ ] `canonicalizeIntent`
  - [ ] `hashIntent`
  - [ ] `verifyIntentHash`
- [ ] Add receipt integrity helper:
  - [ ] `verifyReceiptBinding`
- [ ] Add metrics parity CI test
- [ ] Replace tautological runtime-only test with explicit `EXPECTED_RUNTIME_ONLY` allowlist
- [ ] Add golden tests for:
  - [ ] `allow`
  - [ ] `manual_review`
  - [ ] `reject`
  - [ ] `freeze`
- [ ] Add mutation tests for:
  - [ ] hash fails if approval-critical intent field changes after hashing
  - [ ] receipt binding fails if execution field drifts from approved intent
- [ ] Add fixture coverage for at least:
  - [ ] `policy.continuity.new.json`
  - [ ] `intent.continuity.new.json`

## Acceptance criteria
- Unknown rule metric => signer refuses startup
- Intent hash mismatch => reject
- Policy mismatch between approved intent and live policy => reject
- Receipt mismatch vs approved intent => reject
- Freeze rule hit => freeze wins over any pre-sign outcome
- Metrics parity CI passes

## Non-blocking polish
- Co-locate metric sets with metric builders, or generate them in tests
- Run parity coverage across all policy fixtures
- Remove `as any` once fixtures are typed
