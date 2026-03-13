# Sample Continuity Claim Proof Pack

## 1. Claim header
- **claim_id:** `claim_continuity_rpc_topup_2026_03_07_001`
- **budget_type:** `continuity_budget`
- **policy_type:** `continuity_budget`
- **trust_tier:** `new`
- **intent_id:** `intent_cont_rpc_topup_2026_03_07_001`
- **receipt_id:** `rcpt_cont_rpc_topup_2026_03_07_001`
- **tx_hash:** `EXAMPLE_TX_HASH_ALCHEMY_TOPUP_001`
- **claim_window:** `2026-03-07T04:30:00Z` → `2026-03-07T16:30:00Z`

## 2. Trigger context
- **declared trigger:** `combined_failure_rate > 5% for 10 consecutive rolling 1-minute bins`
- **pre-action metrics:**
  - `provider_failure_rate = 3.1%`
  - `rate_limit_rate = 2.4%`
  - `combined_failure_rate = 5.5%`
- **interpretation:** system was above the declared continuity trigger before intervention.

## 3. Preventive action evidence
- **action:** primary RPC top-up
- **vendor:** `vendor:alchemy`
- **wallet:** `ops_hot_01`
- **spend_class:** `rpc_service`
- **amount_usd:** `$68`
- **action timestamp:** `2026-03-07T04:41:00Z`
- **allowlist match:** yes
- **policy binding passed:** yes
- **intent hash verified:** yes
- **receipt binding verified:** yes
- **pre-sign decision:** `allow`
- **post-execution receipt status:** `complete`

## 4. Counterfactual model
Without the top-up, sustained rate limiting and elevated failure pressure would likely have forced manual founder/on-call intervention within the claim window.

### Predeclared unit costs
- `labor_hour_usd = 200` (month-1 flat mode)

### Assumptions
- Month-1 continuity value is measured primarily via labor avoided.
- No downtime revenue-loss claim is counted.
- The failure pattern would have required manual intervention before escalating into a formal incident-response cycle.

## 5. Quantified avoided cost
- `emergency_engineer_hours_avoided = 4`
- `incident_response_hours_avoided = 0`
- `other_allowed_buckets = 0`

### Cost math
- `4 * $200 = $800` emergency labor avoided
- `0 * $200 = $0` incident-response labor avoided
- **proven_replacement_cost_avoided_usd = $800**
- **unproven_estimate_usd = $0**

## 6. Post-event verification
- **post-action metrics:**
  - `provider_failure_rate = 1.2%`
  - `rate_limit_rate = 0.3%`
  - `combined_failure_rate = 1.5%`
- **trend summary:** combined failure rate fell below threshold and stayed below it through the rest of the claim window.
- **incident threshold tripped after action?** no
- **Sev1 occurred?** no

## 7. Decision status
### Proof completeness checklist
- [x] logged risk event
- [x] timestamped preventive action
- [x] defensible counterfactual model
- [x] predeclared unit cost assumptions
- [x] post-event verification

### Verdict
- **status:** `proven`
- **counted in cap-raise math:** yes
- **proven_replacement_cost_avoided_usd:** `$800`
- **unproven_estimate_usd:** `$0`

---

**Note:** This is an illustrative example. Replace placeholder IDs, tx hash, and telemetry values with real production data before using it for decisioning.

**Month-1 caution:** If any Tier 1/Tier 2 amount is only weakly inferred rather than fully evidenced, move that amount into `unproven_estimate_usd` and exclude it from cap-raise math. For first-cycle claims without hard components, a conservative proven number in the `$400-$600` range may be more appropriate until 2-3 cycles establish evidence quality.
