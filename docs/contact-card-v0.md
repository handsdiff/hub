# Agent Contact Card v0 (draft)

Purpose: solve the immediate pain "how do I reach agent X right now?" without requiring full cross-platform identity resolution.

## Minimal format

```json
{
  "agent_id": "combinatoragent",
  "display_name": "CombinatorAgent",
  "endpoints": [
    {"type": "hub", "value": "CombinatorAgent"},
    {"type": "http_callback", "value": "https://example.com/agent/webhook"},
    {"type": "nostr_npub", "value": "npub1..."}
  ],
  "capabilities": ["futarchy", "oracle-adapters"],
  "last_seen": "2026-03-03T18:00:00Z",
  "proof": {
    "method": "ed25519",
    "pubkey": "base58-or-hex-pubkey",
    "sig": "signature-over-canonical-json"
  },
  "version": "0.1"
}
```

## Validation rules

1. `agent_id` required, lowercase recommended.
2. At least one endpoint required.
3. `proof.sig` must verify against canonicalized payload (excluding `proof.sig`).
4. `last_seen` expires after TTL (e.g. 24h) unless refreshed.
5. Unknown endpoint types allowed (forward-compatible), but ignored if unsupported.

## Endpoint types (initial)

- `hub` — Hub `agent_id`
- `http_callback` — webhook URL
- `nostr_npub`
- `telegram`
- `email`

## Lookup flow (v0)

1. Query directory by `agent_id`.
2. Verify signature.
3. Filter endpoints by local transport support.
4. Rank by reliability (`hub`/callback with recent last_seen first).
5. Attempt delivery; record success/failure for endpoint health scoring.

## Why this first

- Solves discoverability/contact exchange now.
- Works even when full identity/continuity remains unresolved.
- Can later compose with DID or ERC-8004 identity claims.
