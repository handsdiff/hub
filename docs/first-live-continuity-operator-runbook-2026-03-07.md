# First-Live Continuity Signer Runbook (One-Page)

## 1. Objective
Run the first live `continuity_budget` signer cycle for `trust_tier = new` using a low-ambiguity RPC top-up, proving:
- autosign path works end-to-end
- manual-review path works end-to-end
- receipt/policy binding works
- evidence collection is good enough for month-end cap decisions

## 2. Preconditions / Allowlists
Before any live tx:
- policy loaded for `policy_type = continuity_budget`
- startup metric lint passes (`lintPolicyMetricCoverage(policy)`)
- allowlisted wallet configured
- allowlisted vendor configured (example: `vendor:alchemy`)
- `spend_class = rpc_service`
- `buildPreSignMetrics` exported and CI green
- month-1 labor accounting mode = `labor_hour_usd = 200`
- incident trigger predeclared:
  - `combined_failure_rate > 5%` for `10` consecutive rolling `1-minute` bins
- minimum sample size per bin predeclared:
  - `min_requests_per_bin = max(20, min(100, 0.5 * median_requests_per_min_last_24h))`
  - evaluate trigger only when `total_requests >= min_requests_per_bin` in a `1-minute` bin
  - if no 24h baseline exists yet, use temporary default `min_requests_per_bin = 50`
- metric definitions live:
  - `provider_failure_rate = (timeouts + conn_failures + 5xx + malformed/empty + provider_declared_infra_failures) / total_requests`
  - `rate_limit_rate = 429s / total_requests`
  - `combined_failure_rate = provider_failure_rate + rate_limit_rate`

## 3. Tx #1 Autosign Procedure
Goal: validate the lowest-friction happy path.

Suggested tx:
- vendor: allowlisted primary RPC provider
- `spend_class = rpc_service`
- amount: keep clearly inside autosign band (example: `$60-$70`)
- `expected_value_driver = uptime`
- `expected_loss_ceiling_usd = $20-$30`

Steps:
1. Create `PreSignIntent` with full approval-surface fields.
2. Compute `intent_hash` from canonicalized intent.
3. Run `evaluatePreSign(policy, intent, extraMetrics)`.
4. Expect decision = `allow`.
5. Execute tx and persist receipt.
6. Run `verifyReceiptBinding(receipt, approvedIntent, policy)`.
7. Confirm settlement log, receipt storage, and metric collection all succeed.

Required success condition:
- no policy breach
- no binding mismatch
- no unresolved variance

## 4. Tx #2 Manual-Review Procedure
Goal: validate reviewer queue + approval + post-approval path.

Suggested tx:
- same vendor / same spend class as Tx #1
- amount deliberately inside manual-review band, not reject band
- example if policy is `manual_review_threshold_usd = 80`, `single_action_max_usd = 100`:
  - tx amount: `$90`

Steps:
1. Create second `PreSignIntent`.
2. Run `evaluatePreSign(...)`.
3. Expect decision = `manual_review`.
4. Route to reviewer queue.
5. Reviewer approves or rejects explicitly.
6. If approved, execute tx and persist receipt.
7. Run `verifyReceiptBinding(...)` after execution.
8. Confirm audit trail contains both pre-review and post-review states.

Required success condition:
- manual-review routing works
- approval path is explicit
- no post-approval drift between intent, receipt, and policy

## 5. Trigger + Metric Definitions
Use these operational metrics:
- `provider_failure_rate`
- `rate_limit_rate`
- `combined_failure_rate` *(trigger metric)*

Trigger definition:
- incident trigger fires when `combined_failure_rate > 5%` for `10` consecutive rolling `1-minute` bins

Classification rule:
- no trigger trip -> `emergency_engineer_hours_avoided`
- trigger trip -> `incident_response_hours_avoided`

Month-1 labor rate:
- `labor_hour_usd = 200`

## 6. Evidence Pack Requirements
A continuity claim counts only if all five exist:
1. logged risk event / threshold breach signal
2. timestamped preventive action (the continuity tx)
3. defensible counterfactual model
4. predeclared unit cost assumptions
5. post-event verification

No proof pack => `proven_replacement_cost_avoided_usd = 0`

Proof-pack schema lock for month 1:
- `proof_pack_schema_version = 1.0.0`

Allowed month-1 value buckets for cap-raise math:
- Tier 1: avoided SLA penalty, avoided failover/migration vendor cost
- Tier 2: avoided emergency engineer hours, avoided incident-response hours
- Tier 3: downtime revenue loss = informational only unless pre-registered conservatively

## 7. Month-End Raise Gate Checklist
Raise continuity cap from `$500 -> $750` only if all are true:
- [ ] receipt completeness = `100%`
- [ ] policy breaches = `0`
- [ ] continuity-attributable Sev1 incidents = `0`
- [ ] unknown-purpose continuity spend = `0%`
- [ ] uptime/SLO attainment `>= 99.5%`
- [ ] `proven_replacement_cost_avoided_usd >= 750`
- [ ] unresolved reconciliation variance = `0`

If any hard gate fails:
- hold at `$500`
- or cut if breach / variance is serious

## 8. Fail-Safe / Rollback Steps
If anything misbehaves:
1. freeze: disable autosign + require manual review + notify operator
2. disable signer startup on the continuity policy if policy load or lint is suspect
3. revert continuity spend to manual approval only
4. keep receipt logging and evidence capture on
5. preserve frozen state if a freeze triggered; do not auto-unfreeze during rollback
6. revert PR or feature-flag evaluator path off

Unfreeze requirements:
- root cause note written
- operator approval recorded
- clean window observed (default: `24h`)

Safe default:
- no automated continuity spend unless policy load, pre-sign checks, binding checks, and freeze path are all passing cleanly
