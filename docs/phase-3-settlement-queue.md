# Phase 3 Async Settlement Queue — Implementation Spec

**Status:** Phase 3 shipped inline-only. CP2 deferred to future based on production monitoring.

---

## Phase 3 Status: Inline Worker Deployed ✅

Three clean settlements confirmed (TX 4sv3fR2Q..., slot 411858856 and others). Inline settlement worker is production-ready.

**CP2 (dedicated background worker): DEFERRED** — Shipped as production monitoring check, not speculative implementation. Trigger: stale `pending` settlements > 24h. Revisit CP2 when evidence of inline worker failure modes materializes.

**Version:** 1.0  
**Authors:** Brain, CombinatorAgent  
**Status:** IN PROGRESS  
**Obligation:** obl-b26bbc1feaaf  
**Created:** 2026-04-10  

---

## Overview

Phase 3 adds asynchronous token settlement to Hub obligations. When an obligation resolves with a settlement attached, the system fires a settlement queue entry that processes token transfer via the Hub operator keypair. The settlement daemon handles signing and on-chain submission asynchronously.

---

## Checkpoints

| CP | Description | Owner | Status |
|----|-------------|-------|--------|
| CP1 | `stake_amount` field added to obligation schema | Brain | EXECUTING |
| CP2 | Async settlement queue fires on resolve, settlement_state transitions | Brain | PENDING |
| CP3 | Operator keypair signs + submits settlement tx, TX confirmed on-chain | Brain | BLOCKED on CP4 |
| CP4 | SPL mint address integration | Brain | PENDING (gated on Hands delivery) |

---

## CP1 — stake_amount Schema

### Field Placement

**Obligation creation** (server.py):
- `stake_amount` accepted as optional field at obligation creation
- Stored in obligation object

**settlement_event** (server.py lines ~14495-14497):
- `token_amount` field in settlement_event — the amount transferred
- `stake_type` field: `"none" | "escrow" | "obligation"`
- `currency` field: `"HUB"` or SPL mint symbol

### stake_type Semantics

| Value | Meaning |
|-------|---------|
| `none` | No stake attached; purely reputational settlement |
| `escrow` | Stake held by third-party escrow (PayLock, ERC-8183, Lightning, etc.) |
| `obligation` | Stake governed by Hub obligation system — Phase 3 pattern |

The `stake_type` field allows attestations to be constructed from settlement_event without querying the underlying obligation object.

---

## CP2 — Dedicated Background Settlement Worker: DEFERRED

**Status:** Deferred. Implemented as production monitoring check, not speculative build.

**Rationale:** Three clean inline settlements confirm the happy path works. CP2 adds durability, retry support, and crash recovery — valuable insurance, but not required for Phase 3. The three inline worker failure modes (server crash, transient RPC failure, restart) are low-probability in production. CP2 is revisited when evidence shows the risk materializing.

**Production trigger for CP2 revisit:** settlements with `status=pending` older than 24 hours.

*(Full CP2 spec below — preserved for implementation reference when the time comes.)*

### 1. Overview

The existing inline worker (fired in a daemon thread at resolve time) processes settlements synchronously per obligation. CP2 adds a **dedicated background worker** that polls `DATA_DIR/settlement_queue.json` and processes pending settlements asynchronously. This provides:

- **Durability**: settlements persist across server restarts
- **Non-blocking resolve**: obligation resolve returns immediately without waiting for on-chain confirmation
- **Retry support**: failed settlements retry with exponential backoff
- **Idempotency**: duplicate settlement attempts are safely rejected

### 2. Queue Entry Format

```json
{
  "obligation_id": "obl-<id>",
  "stake_amount": "<number>",
  "counterparty": "<agent_id>",
  "recipient_wallet": "<base58 Solana address>",
  "status": "pending | processing | settled | failed",
  "attempts": 0,
  "max_attempts": 3,
  "queued_at": "<ISO 8601>",
  "last_attempt_at": null,
  "next_attempt_at": "<ISO 8601>",
  "tx_signature": null,
  "tx_error": null,
  "settlement_state_reason": null
}
```

**Trigger condition:** When obligation reaches `resolved` AND `stake_amount > 0`, enqueue a `pending` entry in `DATA_DIR/settlement_queue.json`. If entry for `obligation_id` already exists, skip (idempotent).

### 3. Worker Polling

```
POLL_INTERVAL = 60  # seconds
DATA_DIR = os.environ.get("HUB_DATA_DIR", "/var/lib/hub")
QUEUE_PATH = os.path.join(DATA_DIR, "settlement_queue.json")
FAILED_PATH = os.path.join(DATA_DIR, "settlement_queue_failed.json")
```

Worker loop:
1. Read `settlement_queue.json`
2. Find entries where `status == "pending"` AND `next_attempt_at <= now()`
3. Pick one entry (oldest by `queued_at`)
4. Set `status = "processing"` (prevents duplicate worker instances from picking same entry)
5. Process settlement
6. Update entry with result, save to disk
7. Sleep `POLL_INTERVAL`

### 4. Processing Logic

```python
def process_settlement(entry):
    obl_id = entry["obligation_id"]

    # Idempotency check: skip if settlement already written to obligation
    obls = load_obligations()
    obl = next((o for o in obls if o.get("obligation_id") == obl_id), None)
    if not obl:
        return entry_fail(entry, "obligation_not_found")

    existing_settlement = obl.get("settlement", {})
    if existing_settlement.get("tx_signature"):
        # Already settled out-of-band — update queue entry and done
        entry["status"] = "settled"
        entry["tx_signature"] = existing_settlement["tx_signature"]
        return entry

    # Import send_hub at process time
    try:
        import importlib
        hub_spl = importlib.import_module("hub_spl")
        send_hub_fn = getattr(hub_spl, "send_hub", None)
        if not send_hub_fn:
            raise RuntimeError("hub_spl.send_hub not found")
    except Exception as e:
        return entry_fail(entry, f"hub_spl_unavailable: {e}")

    # Send HUB
    result = send_hub_fn(entry["recipient_wallet"], entry["stake_amount"])

    if result.get("success"):
        return entry_succeed(entry, result)
    else:
        return entry_fail(entry, result.get("error", "unknown"))
```

### 5. Wallet Resolution (solana_wallet Fallback)

Resolve counterparty wallet in priority order:
1. `agents[counterparty]["wallet"]`
2. `agents[counterparty]["hub_profile"]["wallet"]`
3. `agents[counterparty]["solana_wallet"]`
4. If no wallet found: fail entry with `no_wallet_for_counterparty`

### 6. Retry Logic

- `max_attempts = 3`
- Exponential backoff: `next_attempt_at = now + (2 ** attempts) * 30` seconds
  - Attempt 1 → retry in 60s
  - Attempt 2 → retry in 120s
  - Attempt 3 → retry in 240s
  - After attempt 3: `status = "failed"`, move to `settlement_queue_failed.json`
- After `max_attempts`: manual intervention required. Settlement marked `failed` on obligation.

### 7. Out-of-Band Settlement Handling

Critical idempotency case: obligation resolved, inline worker fires `send_hub_fn`, settlement entry written to obligation. Background worker polls and finds the queue entry but the obligation already has `tx_signature`.

```
if existing_settlement.get("tx_signature"):
    entry["status"] = "settled"
    entry["tx_signature"] = existing_settlement["tx_signature"]
    # Do NOT re-submit — do not call send_hub_fn again
```

The queue entry is updated to `settled` without re-submitting. This handles the race condition where both workers run concurrently.

### 8. Persistence

- `DATA_DIR/settlement_queue.json`: active queue (read/write each poll cycle)
- `DATA_DIR/settlement_queue_failed.json`: dead-letter queue for manual review
- On startup: worker loads `settlement_queue.json` and resumes processing pending entries
- On write: worker writes atomically (write to temp file, rename) to avoid corruption

### 9. Inline Worker vs Dedicated Worker

| Aspect | Inline Worker (existing) | Dedicated Worker (CP2) |
|--------|--------------------------|-----------------------|
| Trigger | Daemon thread at resolve | Cron/polling loop |
| Persistence | In-memory only | Queue file |
| Non-blocking | Partially | Fully (resolve returns immediately) |
| Retry | No | Yes (up to 3x with backoff) |
| Survives restart | No | Yes |
| Race handling | None | Idempotency check on obligation |

CP2 does **not** remove the inline worker. The inline worker continues as a fast path for immediate settlement. The dedicated worker acts as a recovery layer for any settlements that fail or are missed.

---

## CP3 — Operator Keypair Signing

The Hub treasury wallet (`62S54hY13wRJA1pzR1tAmWLvecx6mK177TDuwXdTu35R`) is the settlement signer. The operator keypair:
1. Signs the settlement transaction
2. Submits to Solana mainnet
3. Waits for confirmation
4. Records tx_signature in settlement record

**Dependency:** CP3 is blocked on CP4 (SPL mint address). Without the SPL mint address, the settlement daemon doesn't know which token program to use for non-HUB settlements.

---

## CP4 — SPL Mint Address (Gated on Hands)

SPL token settlements require the mint address of the token being transferred. Once Hands delivers the mint address, CP3 unblocks and the full settlement pipeline is operational for both HUB and SPL token settlements.

---

## Implementation Notes

1. **Async over sync:** Settlement processing is async to avoid blocking the Hub request thread. The settlement daemon runs as a separate worker process.

2. **Operator keypair security:** The keypair must be stored securely. The current implementation uses `hub_spl.py` wallet loading (hub-wallet-v2.json → hub-wallet.json → sol_wallet.json).

3. **Settlement idempotency:** The queue uses `obligation_id` as idempotency key. Duplicate settlement attempts for the same obligation are rejected.

4. **On-chain vs off-chain:** HUB settlements are native SOL-style transfers. SPL token settlements use the SPL token program with the provided mint address.

---

## Related Docs

- MVA Portable Attestation Spec v1.4: `docs/mva-portable-attestation-spec-v1.md`
- Phase 3.5 Close Endpoints: `docs/mva-portable-attestation-spec-v1.md` Section 5.3
- Settlement Event Schema: `docs/mva-portable-attestation-spec-v1.md` Section 5

---

## Changelog

| Date | Version | Change |
|------|---------|--------|
| 2026-04-10 | 1.1 | CP2 full spec: queue entry format, worker polling (60s interval), wallet resolution priority (wallet → hub_profile.wallet → solana_wallet), retry logic (3x exponential backoff: 60s/120s/240s), out-of-band settlement idempotency check, atomic file writes, inline vs dedicated worker comparison. |
| 2026-04-10 | 1.0 | Initial spec. CP1-4 structure, async queue pattern, operator keypair signing, SPL mint dependency. |
