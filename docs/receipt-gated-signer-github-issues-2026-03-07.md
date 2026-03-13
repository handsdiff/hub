# Receipt-Gated Signer v1 — GitHub Issue Templates

---

## Issue 1 — `feat/signer: implement receipt-gated evaluator core (continuity/new tier v1)`

**Labels:** `backend`, `signer`, `continuity`, `v1`
**Owner role:** Backend engineer
**Estimate:** 1.5d
**Dependencies:** none
**Merge order:** 1

### Scope
Implement the core evaluator and integrity/binding primitives for one signer:
- `policy_type = continuity_budget`
- `trust_tier = new`

### Tasks
- [ ] Add `evaluator.ts` with:
  - [ ] `evaluatePreSign(policy, intent, metrics)`
  - [ ] `evaluateFreeze(policy, metrics)` (**allow|freeze only**)
  - [ ] `evaluateFinalDecision(preSign, freeze)` with precedence `freeze > reject > manual_review > allow`
- [ ] Add intent integrity helpers:
  - [ ] `canonicalizeIntent`
  - [ ] `hashIntent`
  - [ ] `verifyIntentHash`
- [ ] Add receipt binding verifier:
  - [ ] `verifyReceiptBinding(receipt, approvedIntent, policy)`
- [ ] Enforce approvedIntent → live policy checks
- [ ] Enforce receipt → approvedIntent/policy checks
- [ ] Enforce action-equivalence checks:
  - [ ] `budget_type`
  - [ ] `wallet_id`
  - [ ] `spend_class`
  - [ ] `counterparty_or_protocol`
  - [ ] `amount_usd`

### Acceptance Criteria
- [ ] Intent hash mismatch returns reject
- [ ] Approved intent/live policy mismatch returns reject
- [ ] Receipt/action drift returns reject
- [ ] Freeze path never returns reject (`allow|freeze` only)
- [ ] Final precedence behaves as specified

---

## Issue 2 — `feat/signer: add metrics registry + startup policy metric lint (fail-closed)`

**Labels:** `backend`, `signer`, `lint`, `safety`
**Owner role:** Backend engineer
**Estimate:** 0.75d
**Dependencies:** Issue 1
**Merge order:** 2

### Scope
Add metric registry and enforce strict startup lint so policy rules cannot reference unknown metrics.

### Tasks
- [ ] Add `metrics.ts` with:
  - [ ] `DERIVED_METRICS`
  - [ ] `RUNTIME_METRICS`
  - [ ] `SUPPORTED_METRICS = union(DERIVED_METRICS, RUNTIME_METRICS)`
- [ ] Export `buildPreSignMetrics` (or move into `metrics.ts`)
- [ ] Add `lintPolicyMetricCoverage(policy)`:
  - [ ] scan `metric` + `metric_ref` across:
    - [ ] `pre_sign_reject_rules`
    - [ ] `pre_sign_manual_review_rules`
    - [ ] `freeze_rules`
- [ ] Hard-fail startup on unknown metric

### Acceptance Criteria
- [ ] Unknown rule metric causes signer startup failure
- [ ] Lint runs on startup path before evaluator becomes active

---

## Issue 3 — `test/signer: golden tests + mutation tests + typed fixtures`

**Labels:** `test`, `backend`, `signer`, `qa`
**Owner role:** QA/Backend engineer
**Estimate:** 1d
**Dependencies:** Issues 1–2
**Merge order:** 3

### Scope
Cover deterministic control-path behavior and binding drift attacks.

### Tasks
- [ ] Add golden tests for outcomes:
  - [ ] `allow`
  - [ ] `manual_review`
  - [ ] `reject`
  - [ ] `freeze`
- [ ] Add mutation tests:
  - [ ] mutate approval-critical intent field after hashing → `intent_hash_mismatch`
  - [ ] mutate receipt `counterparty_or_protocol` vs approved intent → reject
- [ ] Add typed fixtures:
  - [ ] policy fixture(s)
  - [ ] intent fixture(s)
  - [ ] receipt fixture(s)
- [ ] Remove `as any` where practical from core tests

### Acceptance Criteria
- [ ] Golden tests pass in CI
- [ ] Mutation tests prove drift is rejected
- [ ] Core fixture paths are typed and compile cleanly

---

## Issue 4 — `test/ci: add metrics parity CI guardrail`

**Labels:** `ci`, `test`, `safety`
**Owner role:** QA/Infra engineer
**Estimate:** 0.5d
**Dependencies:** Issues 2–3
**Merge order:** 4

### Scope
Prevent rule/evaluator metric drift via CI.

### Tasks
- [ ] Add `metrics-parity.test.ts`
- [ ] Assert emitted keys from `buildPreSignMetrics` == `DERIVED_METRICS`
- [ ] Assert every rule-referenced metric is in `SUPPORTED_METRICS`
- [ ] Assert no declared derived metric is missing from emitted keys
- [ ] Replace tautological runtime-only check with explicit `EXPECTED_RUNTIME_ONLY`
- [ ] Fail with readable diffs:
  - [ ] `missing_from_supported`
  - [ ] `unknown_rule_metrics`
  - [ ] `declared_but_not_emitted`
  - [ ] `runtime_only`

### Acceptance Criteria
- [ ] CI fails on any metric drift class above
- [ ] Runtime-only allowlist is explicit and reviewable

---

## Issue 5 — `feat/monitor: emit RPC continuity trigger metrics`

**Labels:** `monitoring`, `sre`, `continuity`, `backend`
**Owner role:** SRE/Backend engineer
**Estimate:** 1d
**Dependencies:** none (parallelizable)
**Merge order:** 5

### Scope
Emit trigger metrics and apply month-1 trigger semantics.

### Tasks
- [ ] Emit:
  - [ ] `provider_failure_rate`
  - [ ] `rate_limit_rate`
  - [ ] `combined_failure_rate` (trigger metric)
- [ ] Trigger condition:
  - [ ] `combined_failure_rate > 5%` for 10 consecutive 1-minute bins
- [ ] Sampling guard:
  - [ ] `min_requests_per_bin = max(20, min(100, 0.5 * median_requests_per_min_last_24h))`
  - [ ] fallback if no baseline: `min_requests_per_bin = 50`
  - [ ] evaluate trigger only when `total_requests >= min_requests_per_bin`
- [ ] Ensure 429 included in trigger numerator and broken out separately

### Acceptance Criteria
- [ ] Trigger/diagnostic metrics emitted in staging
- [ ] Threshold and floor behavior matches spec under low/high traffic cases

---

## Issue 6 — `feat/proof-pack: schema + generator for continuity claims (v1.0.0)`

**Labels:** `backend`, `data`, `continuity`, `evidence`
**Owner role:** Backend/Data engineer
**Estimate:** 1d
**Dependencies:** Issues 1, 5
**Merge order:** 6

### Scope
Implement proof-pack generation with strict month-1 decisioning semantics.

### Tasks
- [ ] Add proof-pack schema with `proof_pack_schema_version = 1.0.0`
- [ ] Generate required sections:
  - [ ] claim header
  - [ ] trigger context
  - [ ] preventive action evidence
  - [ ] counterfactual model
  - [ ] quantified avoided cost
  - [ ] post-event verification
  - [ ] decision status / proof completeness
- [ ] Add fields:
  - [ ] `proven_replacement_cost_avoided_usd`
  - [ ] `unproven_estimate_usd`
- [ ] Enforce:
  - [ ] missing proof requirements => proven amount = `0`
  - [ ] month-1 labor mode supports flat `labor_hour_usd = 200`

### Acceptance Criteria
- [ ] Generator output validates against schema
- [ ] Unproven claims never leak into proven decision metric

---

## Issue 7 — `feat/rollout: feature flag, operator wiring, freeze/unfreeze + rollback`

**Labels:** `sre`, `platform`, `rollout`, `ops`
**Owner role:** SRE/Platform engineer
**Estimate:** 0.75d
**Dependencies:** Issues 1, 2, 5, 6
**Merge order:** 7 (go-live gate)

### Scope
Production wiring for month-1 continuity rollout.

### Tasks
- [ ] Add feature flag for one-signer continuity/new-tier path
- [ ] Wire rollout lanes:
  - [ ] Tx #1 autosign RPC top-up
  - [ ] Tx #2 deliberate manual-review-band continuity spend
- [ ] Wire freeze actions:
  - [ ] disable autosign
  - [ ] require manual review
  - [ ] notify operator
- [ ] Wire unfreeze requirements:
  - [ ] root-cause note
  - [ ] operator approval
  - [ ] 24h clean window
- [ ] Add rollback procedure and staging drill

### Acceptance Criteria
- [ ] Rollout path works in staging end-to-end
- [ ] Freeze/unfreeze controls behave deterministically
- [ ] Rollback path tested and documented
