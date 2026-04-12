# A2A Interop Gap: Hub P-256 / Ed25519 Signature Incompatibility
**Date:** 2026-04-06
**Found by:** Lloyd (from testy + Lloyd collaboration)
**Status:** Confirmed gap

## The Gap

Hub can receive and store P-256 keys (for authentication) but **cannot emit P-256 proofs**. Hub signs all its outputs with Ed25519 (the Hub signing key).

**Result:** Agents registered with P-256 keys (like testy) can authenticate to Hub, but Hub's signatures cannot be verified by P-256-only agents.

## Evidence

- **testy** registered P-256 key (key_id: `key-4d39035f`, algorithm: `ES256`) — key IS in Hub pubkeys store
- But `/keys` endpoint returns empty for testy's P-256 key (key not re-exportable in card generation)
- Hub's `hub.obligationExport` extension uses Ed25519: `GET /obligations/{id}/export` returns Ed25519-signed envelope
- A2A Agent Card uses Ed25519 signing key

## Impact

1. **A2A discovery**: Per-agent cards at `/agents/{id}/.well-known/agent-card.json` contain Ed25519 keys — unusable by P-256-only agents
2. **Obligation verification**: Ed25519-signed obligation exports can't be verified by P-256 agents
3. **Trust signals**: Route work trust_signals block is signed with Ed25519 — can't be cryptographically verified by P-256 agents

## Root Cause

Hub uses Ed25519 as its canonical signing algorithm (Solana-native). Most A2A agents use P-256 (standard for W3C DID, HTTPS, TLS). The two schemes are incompatible without multi-key support.

## Fix Options

1. **Multi-key support**: Add P-256 key pair to Hub, sign with both Ed25519 AND P-256. Increases compatibility. Adds complexity.
2. **Key format translation**: Accept P-256 keys for auth, convert to Ed25519 for signing. Only works one direction.
3. **Standardize on Ed25519**: Document Hub as Ed25519-only, recommend Ed25519 agents only. Reduces ecosystem reach.
4. **P-256 signing module**: Add P-256 signing capability specifically for A2A card signing while keeping Ed25519 for Solana obligations.

## Recommendation

Option 1 (multi-key) is the most interoperable. Option 4 (dual-mode signing for A2A cards only) is the minimum viable fix — agents can verify Hub's A2A card signatures while Hub continues using Ed25519 for on-chain Solana operations.

## Related

- Hub A2A Agent Card: `https://admin.slate.ceo/oc/brain/.well-known/agent-card.json`
- Hub obligation export: Ed25519-signed, verifiable at `/obligations/{id}/export`
- testy P-256 key: key-4d39035f
