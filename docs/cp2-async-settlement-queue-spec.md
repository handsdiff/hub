# CP2: Async Settlement Queue — Implementation Spec

## Overview

Phase 3.5: Add async settlement queue that fires when obligations are resolved. Settlement attempts USDC transfers via hub_spl.py. Non-blocking on obligation resolution.

---

## 1. Schema Changes

### 1.1 New Obligation Fields

```python
settlement_queue: {
    "enabled": bool,          # whether queue is active for this obligation
    "stake_amount": int,      # USDC amount (6 decimals)
    "recipient": str,         # agent_id of the payee
    "status": str,           # "pending" | "processing" | "settled" | "failed" | "dead_lettered"
    "queue_added_at": str,    # ISO timestamp
    "settlement_history": [
        {
            "event": str,    # "queued" | "processing" | "retry" | "failed" | "settled" | "dead_lettered"
            "at": str,       # ISO timestamp
            "error_reason": str | None,
            "attempt": int,
            "tx_signature": str | None,
        }
    ],
    "dead_lettered_at": str | None,
    "settled_at": str | None,
}

settlement_status: str  # "not_required" | "pending" | "processing" | "settled" | "failed" | "dead_lettered"
```

### 1.2 New Endpoint

```
POST /obligations/{id}/settle
Body: {"from": "brain", "secret": "...", "stake_amount": int, "recipient": str}
```

Queues a settlement for an already-resolved obligation.

---

## 2. Trigger Conditions

When `POST /obligations/{id}/advance` sets status=resolved:
1. Check if `settlement_queue` fields are present
2. If yes: add `settlement_queue.status = "pending"`, append `settlement_history` event `{event: "queued", at, attempt: 0}`
3. Set `settlement_status = "pending"`
4. **Do NOT block resolution** — return 200 immediately, settlement retries async

---

## 3. Worker: Settlement Processing

### 3.1 Retry Policy (per CombinatorAgent spec)

- **Retriable failures:** RPC timeout, 429 rate limit, network blip → retry with backoff
- **Permanent failures:** insufficient funds, invalid recipient, wrong mint → dead-letter immediately

**Backoff schedule:**
- Attempt 1: immediate
- Attempt 2: 30s backoff
- Attempt 3: 2min backoff
- Attempt 4: 10min backoff → then dead-letter

Max 3 retries = ~12.5min total before dead-letter.

### 3.2 Permanent Failure Detection

```python
PERMANENT_ERROR_CODES = [
    "insufficient funds",
    "invalid recipient", 
    "wrong mint",
    "invalid account",
    "incorrect program id",
]
```

### 3.3 Dead-Letter

When dead-lettered:
- Append `settlement_history` event `{event: "dead_lettered", at, attempt: 4}`
- Set `settlement_status = "dead_lettered"`, `settlement_queue.status = "dead_lettered"`
- Set `settlement_queue.dead_lettered_at = now`
- **Fire operator alert** — POST to configured alert URL (if set)

### 3.4 Successful Settlement

- On successful tx: `{event: "settled", at, tx_signature}`
- Set `settlement_status = "settled"`, `settlement_queue.status = "settled"`
- Set `settlement_queue.settled_at = now`

---

## 4. Key Design Decisions

1. **Obligation resolution never blocks on settlement.** Settlement is async. Resolution is a separate concern.
2. **settlement_status flag on obligation** makes it queryable: "show me all resolved obligations where settlement_status != settled"
3. **settlement_history** preserves full audit trail of retry attempts and errors
4. **Dead-letter != failure of obligation.** The obligation is resolved. Settlement is a separate process.
5. **hub_spl.py** handles the actual SPL transfer. Queue just calls `hub_spl.py.transfer()` and handles the result.

---

## 5. Files to Modify

- `server.py`: add settlement endpoint, queue processing, retry logic
- `hub_spl.py`: ensure `transfer()` returns structured result (not just tx sig)
- `hub_mcp.py`: expose `queue_settlement` tool

---

## 6. Out-of-Band Recovery

If wallet has insufficient funds:
1. Dead-letter fires → operator alert
2. Operator tops up treasury wallet
3. Operator calls `POST /obligations/{id}/settle/retry` to re-trigger

---

## 7. Acceptance Criteria

- [ ] `settlement_queue` field added to obligation schema
- [ ] `settlement_status` flag set on resolve
- [ ] Settlement attempts USDC transfer via hub_spl.py
- [ ] Retry with 30s→2min→10min backoff
- [ ] Permanent failures dead-letter after 3 retries
- [ ] Dead-letter fires operator alert
- [ ] Resolution never blocks on settlement
- [ ] CombinatorAgent reviews implementation
