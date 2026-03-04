# Verified A2A Loop Evidence Template (Audit-Grade)

Use this template to record one candidate **non-human → agent → agent** loop with reproducible evidence.

---

## Loop Metadata

- `loop_id`:
- `recorded_at_utc`:
- `recorder`:
- `status`: `candidate | verified | rejected`
- `rejection_reason` (if rejected):

---

## 1) Income Leg (Non-human → Agent)

- `tx_in_id`:
- `t_in_utc`:
- `network_or_rail`:
- `payer_id` (agent/service):
- `payer_type` (`agent|service|unknown`):
- `payee_agent_id`:
- `payee_wallet_or_account`:
- `amount_in`:
- `asset_in`:

### Raw Evidence
- Receipt / explorer / API link(s):
- Raw JSON / screenshot hash / export path:

---

## 2) Spend Leg (Same Agent Wallet → Another Agent Service)

- `tx_out_id`:
- `t_out_utc`:
- `network_or_rail`:
- `payer_agent_id`:
- `payer_wallet_or_account`:
- `recipient_agent_or_service_id`:
- `service_purchased`:
- `amount_out`:
- `asset_out`:

### Raw Evidence
- Receipt / explorer / API link(s):
- Raw JSON / screenshot hash / export path:

---

## 3) Wallet Continuity Proof

Show that both legs were controlled by the same agent wallet/account scope.

- `continuity_method` (`same_wallet_address|same_nwc_account|same_alby_account|other`):
- `continuity_artifact` (address/account ids, signer policy, logs):
- `delegated_signer_present` (`yes|no`):
- `delegation_details` (if yes):

---

## 4) Counterparty Verification

Show that counterparties are agents/services, not manual human operations.

- `income_leg_counterparty_verified` (`yes|no|partial`):
- `spend_leg_counterparty_verified` (`yes|no|partial`):
- `verification_method` (agent card/contact card/API identity/protocol metadata):
- `counterparty_evidence_links`:

---

## 5) Provenance + Funding Source Check

Prevent false positives from mixed human funding.

- `wallet_funding_chain_reviewed` (`yes|no`):
- `human_deposit_overlap_present` (`yes|no|unknown`):
- `provenance_notes`:

---

## 6) Normalized Summary Row

```text
loop_id, t_in_utc, tx_in_id, t_out_utc, tx_out_id, payer_agent_id, payer_wallet_or_account, recipient_agent_or_service_id, amount_in, asset_in, amount_out, asset_out, continuity_method, status
```

---

## Verification Decision

- `decision`: `verified | rejected | needs_more_data`
- `confidence`: `high | medium | low`
- `decider`:
- `decision_time_utc`:
- `decision_notes`:

---

## Minimal Acceptance Criteria (all required for VERIFIED)

- [ ] Income leg has tx/proof id + timestamp + non-human payer evidence
- [ ] Spend leg has tx/proof id + timestamp + agent/service recipient evidence
- [ ] Wallet continuity between legs is explicitly demonstrated
- [ ] Raw evidence artifacts are attached/linked
- [ ] Funding provenance reviewed for human-deposit contamination
