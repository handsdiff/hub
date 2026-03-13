# First Live Continuity Transaction Spec (2026-03-07)

## Rollout order
1. **Tx #1: autosign path**
   - First live continuity transaction should stay inside autosign.
   - Goal: validate happy-path intent hashing, policy binding, receipt binding, and settlement logging end-to-end.
2. **Tx #2: manual-review path**
   - Deliberately hit the manual-review band.
   - Goal: validate reviewer queueing, approval path, and post-approval execution.

## First live spend
- **Use case:** prepay / top-up primary RPC provider for production agent wallet
- **Recommended vendor:** allowlisted provider (example: `vendor:alchemy`)
- **spend_class:** `rpc_service`
- **amount_usd:** approximately `$60-$100`
- **expected_value_driver:** `uptime`
- **expected_loss_ceiling_usd:** approximately `$20-$30`

## Incident trigger for first live test
- **Primary trigger:** RPC failure rate `> 5%` for `10` consecutive rolling `1-minute` bins
- Optional faster variant: `3` consecutive `1-minute` windows
- **Minimum sample floor per bin:**
  - `min_requests_per_bin = max(20, min(100, 0.5 * median_requests_per_min_last_24h))`
  - only evaluate trigger in bins where `total_requests >= min_requests_per_bin`
  - if no 24h baseline exists yet, use temporary default `min_requests_per_bin = 50`

## Failure definition (counts in trigger numerator)
Count as RPC error:
- timeouts / connection failures
- HTTP 5xx / transport server errors
- malformed / invalid / empty RPC payloads
- provider-declared infra failures (e.g. JSON-RPC infra error objects)
- HTTP 429 rate limits

Do **not** count as error:
- latency breach alone (track as separate SLI unless request also fails/times out)

## Reporting split
Use all three:
- `provider_failure_rate`
  - `(timeouts + conn failures + 5xx + malformed/empty + provider-declared infra errors) / total_requests`
- `rate_limit_rate`
  - `(429 responses) / total_requests`
- `combined_failure_rate` *(trigger metric)*
  - `provider_failure_rate + rate_limit_rate`

Trigger on `combined_failure_rate > 5%` for 10 consecutive 1-minute bins.
Diagnose from the two components.

## Labor classification boundary
- **Emergency engineer hours avoided** = pre-incident intervention before declared incident trigger trips
- **Incident-response hours avoided** = triage / mitigation / recovery work after declared incident trigger trips
- Boundary rule: **did the declared incident threshold trip?**
  - no trip -> emergency bucket
  - trip occurred -> incident-response bucket

## Month-1 labor rate card
Use flat anti-gaming mode for the first live month:
- `labor_hour_usd = 200`

If evidence quality stays clean, split in month 2 to:
- `emergency_engineer_hour_usd = 225`
- `incident_response_hour_usd = 175`

## Evidence-backed replacement-cost avoided
Only count claims that pass all five:
1. logged risk event / threshold breach signal
2. timestamped preventive action (the continuity tx)
3. defensible counterfactual model
4. predeclared unit cost assumptions
5. post-event verification

No proof pack -> claim value = `0`

## Ranked allowlist for continuity value claims
### Tier 1 (always counts)
- avoided SLA penalty
- avoided failover / migration vendor cost

### Tier 2 (counts with preset rate card)
- avoided emergency engineer hours
- avoided incident-response hours

### Tier 3 (informational in month 1 unless pre-registered)
- avoided downtime revenue loss

## Month-1 continuity cap raise gate (`$500 -> $750`)
Raise only if **all** are true:
- `receipt_completeness = 100%`
- `policy_breaches = 0`
- `continuity_attributable_sev1_incidents = 0`
- `unknown_purpose_continuity_spend = 0%`
- `uptime_slo_attainment >= 99.5%`
- `proven_replacement_cost_avoided_usd >= 750`
- `unresolved_reconciliation_variance = 0`

Else:
- hold at `$500`
- or cut if breach / variance is serious
