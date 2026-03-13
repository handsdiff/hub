# Receipt-Gated Signer v1 — Implementation Tickets

## 1) feat/signer: implement receipt-gated evaluator core (continuity/new tier v1)
- **Owner role:** Backend engineer
- **Estimate:** 1.5 days
- **Dependencies:** none
- **Acceptance criteria:**
  - `evaluator.ts` added with:
    - `evaluatePreSign(policy, intent, metrics)`
    - `evaluateFreeze(policy, metrics)` (`allow | freeze` only)
    - `evaluateFinalDecision(preSign, freeze)` with precedence `freeze > reject > manual_review > allow`
  - `canonicalizeIntent`, `hashIntent`, `verifyIntentHash` implemented
  - `verifyReceiptBinding(receipt, approvedIntent, policy)` implemented with:
    - approvedIntent → live policy checks
    - receipt → intent/policy checks
    - action-equivalence checks (`budget_type`, `wallet_id`, `spend_class`, `counterparty_or_protocol`, `amount_usd`)
- **Merge order:** 1

## 2) feat/signer: add metrics registry + startup policy metric lint (fail-closed)
- **Owner role:** Backend engineer
- **Estimate:** 0.75 day
- **Dependencies:** Ticket 1
- **Acceptance criteria:**
  - `metrics.ts` added with:
    - `DERIVED_METRICS`
    - `RUNTIME_METRICS`
    - `SUPPORTED_METRICS = union(...)`
  - `buildPreSignMetrics` exported (or moved to `metrics.ts`)
  - `lintPolicyMetricCoverage(policy)` added
  - signer startup hard-fails on unknown `metric` / `metric_ref`
- **Merge order:** 2

## 3) test/signer: golden tests + mutation tests + fixtures
- **Owner role:** QA/Backend engineer
- **Estimate:** 1 day
- **Dependencies:** Tickets 1–2
- **Acceptance criteria:**
  - Golden tests pass for: `allow`, `manual_review`, `reject`, `freeze`
  - Mutation tests:
    - intent field drift after hash => `intent_hash_mismatch`
    - receipt counterparty drift => `receipt_counterparty_or_protocol_mismatch`
  - Typed fixtures added:
    - policy fixture(s)
    - intent fixture(s)
    - receipt fixture(s)
  - Remove `as any` from core test paths
- **Merge order:** 3

## 4) test/ci: add metrics parity CI guardrail
- **Owner role:** QA/Infra engineer
- **Estimate:** 0.5 day
- **Dependencies:** Tickets 2–3
- **Acceptance criteria:**
  - `metrics-parity.test.ts` added and running in CI
  - Asserts:
    - emitted keys from `buildPreSignMetrics` == `DERIVED_METRICS`
    - all rule-referenced metrics are in `SUPPORTED_METRICS`
    - no declared derived metric is missing from emitted keys
    - runtime-only metrics checked against explicit `EXPECTED_RUNTIME_ONLY`
  - CI fails with readable diffs:
    - `missing_from_supported`
    - `unknown_rule_metrics`
    - `declared_but_not_emitted`
    - `runtime_only`
- **Merge order:** 4

## 5) feat/monitor: emit RPC continuity trigger metrics (provider/rate-limit/combined)
- **Owner role:** SRE/Backend engineer
- **Estimate:** 1 day
- **Dependencies:** none (can run parallel to 1–4)
- **Acceptance criteria:**
  - Monitor emits:
    - `provider_failure_rate`
    - `rate_limit_rate`
    - `combined_failure_rate` (trigger metric)
  - Trigger logic implemented:
    - `combined_failure_rate > 5%` for 10 consecutive 1-min bins
    - evaluate only when `total_requests >= min_requests_per_bin`
    - `min_requests_per_bin = max(20, min(100, 0.5 * median_requests_per_min_last_24h))`
    - fallback when no baseline: `min_requests_per_bin = 50`
  - 429 included in trigger numerator and broken out separately
- **Merge order:** 5

## 6) feat/proof-pack: schema + generator for continuity claims (v1.0.0)
- **Owner role:** Backend/Data engineer
- **Estimate:** 1 day
- **Dependencies:** Tickets 1, 5
- **Acceptance criteria:**
  - Proof-pack schema versioned: `proof_pack_schema_version = 1.0.0`
  - Generator outputs required sections (header, trigger context, action evidence, counterfactual, quantified value, verification, decision)
  - Includes fields:
    - `proven_replacement_cost_avoided_usd`
    - `unproven_estimate_usd`
  - Enforces rule: missing required proof => proven amount = 0
  - Month-1 labor mode supports flat `labor_hour_usd = 200`
- **Merge order:** 6

## 7) feat/rollout: feature flag, runbook wiring, freeze/unfreeze ops + rollback
- **Owner role:** SRE/Platform engineer
- **Estimate:** 0.75 day
- **Dependencies:** Tickets 1, 2, 5, 6
- **Acceptance criteria:**
  - Feature flag for signer rollout (continuity/new only)
  - Rollout path supports:
    - Tx #1 autosign lane
    - Tx #2 manual-review-band lane
  - Freeze action wiring:
    - disable autosign
    - require manual review
    - notify operator
  - Unfreeze requirements enforced:
    - root-cause note
    - operator approval
    - 24h clean window
  - Rollback path documented + tested in staging
- **Merge order:** 7 (final go-live gate)
