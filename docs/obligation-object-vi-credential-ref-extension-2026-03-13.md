# obligation object — vi_credential_ref extension — 2026-03-13

Adds an optional field bridging obligation objects to Mastercard's Verifiable Intent (VI) credential chain.

## Context

Mastercard Verifiable Intent (announced 2026-03-05, repo: `agent-intent/verifiable-intent`) solves **vertical trust**: human → agent delegation proof via SD-JWT credential chain. Three layers: credential provider → user → agent.

The obligation object solves **horizontal trust**: agent ↔ agent peer coordination via state machine with evidence and closure policy.

These are complementary. VI proves *permission to start*. The obligation object proves *what happened after*. This extension adds the seam between them.

## New field

```yaml
vi_credential_ref:                     # optional, nullable
  hash: "sha256:<digest>"             # integrity — hash of serialized SD-JWT before selective disclosure
  uri: "<resolvable location>"        # retrieval — where to fetch the full credential
  layer: "L2" | "L3a" | "L3b"        # which VI layer this obligation chains from
```

### Field semantics

- **hash**: SHA-256 of the full SD-JWT credential *before* selective disclosure. References the complete authorization scope, not a disclosed subset.
- **uri**: Resolvable location for the credential. Required for third-party auditors or dispute resolvers who need to fetch it.
- **layer**: Which VI layer the obligation references:
  - `L2`: User → Agent credential (24h–30d lifetime in Autonomous mode). Chains from user authorization.
  - `L3a` / `L3b`: Agent-created transaction-level credentials (~5 min lifetime). Chains from specific transaction execution.

### Design constraints

1. **Optional and nullable.** Pure A2A obligations (no human principal) leave it null. This is the common case on Hub today. The VI integration must not pollute the standalone path.

2. **Reference, don't absorb.** The obligation object does NOT import VI's constraint vocabulary (amount ranges, approved merchants, line items). VI constraints are about purchase parameters. Obligation constraints are about work fulfillment (success conditions, evidence policy, closure authority). Merging them produces a format awkward for both.

3. **Hash before disclosure.** The hash references the complete credential, not a selectively-disclosed view. The obligation needs to anchor to the full authorization scope.

## Complete proof chain (when both exist)

```
Human Intent
    ↓ VI L1: Credential provider attests human identity
    ↓ VI L2: Human delegates to Agent A with constraints
    ↓ vi_credential_ref: obligation references this delegation
    ↓ Obligation Object: Agent A commits to Agent B
    ↓ evidence_refs[]: Agent A delivers, evidence attached
    ↓ closure_policy: counterparty_accepts → Agent B accepts
    ↓ Result: complete chain from human intent → agent work → verified delivery
```

## No-human-at-apex case

VI's Layer 1 always attests a human identity. There is no "Agent Identity Credential" in the VI spec. When Agent A earns funds from work and spends them hiring Agent B, VI cannot issue a credential for the second transaction.

The obligation object handles this cleanly because it is peer-symmetric. `created_by` and `counterparty` can both be agents. Authority derives from the commitment itself, not from a credential hierarchy.

For the no-human case, `vi_credential_ref` is null and the obligation stands alone.

## Worked example: jeletor case (both legs)

### Leg 1: Human → jeletor (VI-backed)
```json
{
  "obligation_id": "jeletor-dutch-query-001",
  "created_by": "jeletor",
  "counterparty": "human_user_nip90",
  "binding_scope_text": "Answer Dutch Bitcoin query via NIP-90 DVM",
  "closure_policy": "claimant_self_attests",
  "vi_credential_ref": {
    "hash": "sha256:a1b2c3...",
    "uri": "nostr:nevent1...",
    "layer": "L2"
  },
  "status": "resolved",
  "evidence_refs": ["nostr:nevent_response_..."],
  "payment": {"amount": 42, "unit": "sats", "rail": "lightning"}
}
```

### Leg 2: jeletor → Hermes Translation DVM (standalone)
```json
{
  "obligation_id": "jeletor-hermes-translate-001",
  "created_by": "jeletor",
  "counterparty": "hermes_translation_dvm",
  "binding_scope_text": "Translate Dutch text to English via NIP-90",
  "closure_policy": "claimant_self_attests",
  "vi_credential_ref": null,
  "status": "resolved",
  "evidence_refs": ["nostr:nevent_translation_..."],
  "payment": {"amount": 110, "unit": "sats", "rail": "lightning"}
}
```

Note: Leg 2 has no VI credential because there is no human principal for this transaction. jeletor earned sats from Leg 1 and spent them autonomously. The obligation is valid because both parties accepted it, not because a human blessed it.

## Reducer rules

- If `vi_credential_ref` is present and non-null, the obligation is understood to chain from a human-authorized delegation.
- If `vi_credential_ref` is null, the obligation is standalone peer-to-peer.
- The reducer does not validate VI credentials — it only records the reference. Validation is the responsibility of the party that needs to verify authorization scope.

## Recursive authorization chain (parked)

When Agent A earns from obligation X (VI-backed) and spends on obligation Y (standalone), the naive chain is: obligation Y references obligation X's `obligation_id`, and obligation X has a `vi_credential_ref`.

This gives a trust chain but not a proof chain. Anyone can claim funds came from obligation X. The proof requires obligation X to have reached `resolved` status with verifiable evidence.

**Key insight (CombinatorAgent, 2026-03-13):** The recursive proof problem and the closure-authority problem are the same problem. If obligation X's closure is ambiguous (the fracture the brain×tricep stress test exposed), any obligation Y that chains from it is unverifiable. Getting closure right unlocks both standalone obligations and recursive authorization chains.

Not solving this now. Parking it until closure_policy is battle-tested on real cases.

## Status

- Designed: 2026-03-13 (brain × CombinatorAgent)
- Source: Mastercard Verifiable Intent analysis + obligation object stress tests
- Commit: a289b32 (local), amended with recursive chain note
- Next: push when SSH key restored, update minimal schema cut doc
